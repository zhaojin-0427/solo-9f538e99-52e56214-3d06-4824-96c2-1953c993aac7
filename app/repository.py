"""Data access for identification records and bounded summary queries.

This is the only module that knows SQL.

Storage model
-------------
Detail: ``observations`` -- one row per identification record, keyed by the
unique ``recognition_id``.

Summary: ``tag_summaries`` -- one row per ``(district, ear_tag)``.  Its
``day_counts`` column is a canonical JSON array of ``[day, count]`` pairs,
sorted by day.  Because a day appears at most once, repeated captures of the
same ear tag in the same district on the same day are already collapsed at
write time: the "same day counts once" rule is applied on the way in, so a
statistics query never merges photos or days.

Key guarantees
--------------
* Detail and summary change in the same transaction.  A failure rolls both
  back, so reads keep seeing the old numbers.
* Statistics read ``tag_summaries`` only.  They never touch ``observations``
  and never group by day at query time; the number of observed days inside an
  arbitrary window is two binary searches over the stored sorted days.
* One statistics query fetches at most ``MAX_SUMMARY_ROWS`` summary records in
  a single statement.  If the range really needs more, it returns
  ``SummaryLimitExceeded`` rather than a truncated answer.
* A correction reads one detail row (by unique key) plus at most two summary
  records (old and new district/ear-tag scope).  The number of observation
  rows read is constant and does not grow with the ear tag's history.
"""

from __future__ import annotations

import json
from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from .db import MAX_SUMMARY_ROWS, read_transaction, write_transaction


class NotFoundError(Exception):
    """The requested recognition record does not exist."""


class SummaryLimitExceeded(Exception):
    """The query would need more than the allowed summary records."""

    def __init__(self, limit: int = MAX_SUMMARY_ROWS) -> None:
        self.limit = limit
        super().__init__(
            f"本次查询需要访问超过 {limit} 条汇总记录；请缩小片区或日期范围后重试"
        )


class InvalidRangeError(ValueError):
    """The requested date range is inverted."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _record(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "recognition_id": row["recognition_id"],
        "observed_on": date.fromisoformat(row["observed_on"]),
        "district": row["district"],
        "ear_tag": row["ear_tag"],
    }


def _fetch_one(conn, recognition_id: str):
    return conn.execute(
        """
        SELECT recognition_id, observed_on, district, ear_tag
        FROM observations
        WHERE recognition_id = ? AND committed = 1
        """,
        (recognition_id,),
    ).fetchone()


def _state_version(conn) -> int:
    row = conn.execute(
        "SELECT value FROM app_meta WHERE key = 'state_version'"
    ).fetchone()
    return int(row["value"]) if row is not None else 0


def _bump_state_version(conn) -> int:
    conn.execute("UPDATE app_meta SET value = value + 1 WHERE key = 'state_version'")
    return _state_version(conn)


def _parse_days(raw: str) -> list[list[Any]]:
    """``day_counts`` is a sorted JSON array of ``[day, count]`` pairs."""
    entries = json.loads(raw)
    if not isinstance(entries, list):
        raise TypeError("corrupt summary: day_counts is not an array")
    return entries


def _dump_days(entries: Sequence[Sequence[Any]]) -> str:
    return json.dumps(entries, separators=(",", ":"), ensure_ascii=False)


def _index_of(entries: Sequence[Sequence[Any]], day: str) -> int:
    """Position of ``day`` in the sorted entries, or its insertion point."""
    return bisect_left([entry[0] for entry in entries], day)


# ---------------------------------------------------------------------------
# Summary maintenance -- constant work per changed identification
# ---------------------------------------------------------------------------


def _load_summary(conn, district: str, ear_tag: str):
    return conn.execute(
        """
        SELECT first_observed_on, last_observed_on, day_counts
        FROM tag_summaries
        WHERE district = ? AND ear_tag = ? AND committed = 1
        """,
        (district, ear_tag),
    ).fetchone()


def _save_summary(conn, district: str, ear_tag: str, entries: list[list[Any]]) -> None:
    """Write back one summary record, or drop it when no day is left."""
    if not entries:
        conn.execute(
            """
            DELETE FROM tag_summaries
            WHERE district = ? AND ear_tag = ? AND committed = 1
            """,
            (district, ear_tag),
        )
        return
    conn.execute(
        """
        INSERT INTO tag_summaries
            (district, ear_tag, committed, first_observed_on,
             last_observed_on, day_counts)
        VALUES (?, ?, 1, ?, ?, ?)
        ON CONFLICT (district, ear_tag, committed) DO UPDATE SET
            first_observed_on = excluded.first_observed_on,
            last_observed_on  = excluded.last_observed_on,
            day_counts        = excluded.day_counts
        """,
        (district, ear_tag, entries[0][0], entries[-1][0], _dump_days(entries)),
    )


def _add_day(conn, district: str, ear_tag: str, day: str) -> None:
    """Record one identification in one summary record.

    Only the ``(district, ear_tag)`` summary row is read and written.  A day
    already present is incremented in place, so another capture on the same
    day never creates another day.
    """
    row = _load_summary(conn, district, ear_tag)
    entries = _parse_days(row["day_counts"]) if row is not None else []
    pos = _index_of(entries, day)
    if pos < len(entries) and entries[pos][0] == day:
        entries[pos][1] += 1
    else:
        entries.insert(pos, [day, 1])
    _save_summary(conn, district, ear_tag, entries)


def _remove_day(conn, district: str, ear_tag: str, day: str) -> None:
    """Remove one identification; drop the day / summary row when empty."""
    row = _load_summary(conn, district, ear_tag)
    if row is None:
        raise RuntimeError("summary record missing; refusing to commit")
    entries = _parse_days(row["day_counts"])
    pos = _index_of(entries, day)
    if pos >= len(entries) or entries[pos][0] != day:
        raise RuntimeError("summary day missing; refusing to commit")
    entries[pos][1] -= 1
    if entries[pos][1] <= 0:
        del entries[pos]
    _save_summary(conn, district, ear_tag, entries)


def _move_day(conn, old: Mapping[str, str], new: Mapping[str, str]) -> None:
    """Apply one identification move to the old and new summary scopes.

    At most two summary records are read and written, regardless of how many
    days of history either ear tag has.  Nothing in ``observations`` is read
    here: the caller already supplied the old and new attributes from the
    single record being corrected.
    """
    if (
        old["district"] == new["district"]
        and old["ear_tag"] == new["ear_tag"]
        and old["observed_on"] == new["observed_on"]
    ):
        return
    _remove_day(conn, old["district"], old["ear_tag"], old["observed_on"])
    _add_day(conn, new["district"], new["ear_tag"], new["observed_on"])


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def put_observation(
    recognition_id: str,
    observed_on: date,
    district: str,
    ear_tag: str,
    db_path: str | None = None,
) -> tuple[str, dict[str, Any], int]:
    """Create, or correct, the record identified by ``recognition_id``.

    Re-submitting the same record number corrects exactly the three editable
    attributes; identical values are a no-op.  Returns
    ``(action, record, state_version)``.
    """
    day = observed_on.isoformat()
    new = {"district": district, "ear_tag": ear_tag, "observed_on": day}
    with write_transaction(db_path) as conn:
        row = _fetch_one(conn, recognition_id)
        if row is None:
            conn.execute(
                """
                INSERT INTO observations
                    (recognition_id, observed_on, district, ear_tag, committed)
                VALUES (?, ?, ?, ?, 1)
                """,
                (recognition_id, day, district, ear_tag),
            )
            _add_day(conn, district, ear_tag, day)
            action = "created"
        else:
            old = {
                "district": row["district"],
                "ear_tag": row["ear_tag"],
                "observed_on": row["observed_on"],
            }
            if old == new:
                return "unchanged", _record(row), _state_version(conn)
            conn.execute(
                """
                UPDATE observations
                SET observed_on = ?, district = ?, ear_tag = ?,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE recognition_id = ? AND committed = 1
                """,
                (day, district, ear_tag, recognition_id),
            )
            _move_day(conn, old, new)
            action = "updated"

        version = _bump_state_version(conn)
        saved = _fetch_one(conn, recognition_id)
        assert saved is not None
        return action, _record(saved), version


def correct_observation(
    recognition_id: str,
    changes: Mapping[str, Any],
    db_path: str | None = None,
) -> tuple[str, dict[str, Any], int]:
    """Partially correct the three editable attributes of one record.

    ``changes`` may contain ``observed_on`` (a ``date``), ``district`` and/or
    ``ear_tag``.  The detail lookup is by unique key; summary maintenance is
    scoped to the old and new ``(district, ear_tag)`` rows only.
    """
    with write_transaction(db_path) as conn:
        row = _fetch_one(conn, recognition_id)
        if row is None:
            raise NotFoundError(recognition_id)

        old = {
            "district": row["district"],
            "ear_tag": row["ear_tag"],
            "observed_on": row["observed_on"],
        }
        new = dict(old)
        if changes.get("observed_on") is not None:
            new["observed_on"] = changes["observed_on"].isoformat()
        if changes.get("district") is not None:
            new["district"] = changes["district"]
        if changes.get("ear_tag") is not None:
            new["ear_tag"] = changes["ear_tag"]

        if old == new:
            return "unchanged", _record(row), _state_version(conn)

        conn.execute(
            """
            UPDATE observations
            SET observed_on = ?, district = ?, ear_tag = ?,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE recognition_id = ? AND committed = 1
            """,
            (new["observed_on"], new["district"], new["ear_tag"], recognition_id),
        )
        _move_day(conn, old, new)
        version = _bump_state_version(conn)
        saved = _fetch_one(conn, recognition_id)
        assert saved is not None
        return "updated", _record(saved), version


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get_observation(recognition_id: str, db_path: str | None = None) -> dict[str, Any]:
    with read_transaction(db_path) as conn:
        row = _fetch_one(conn, recognition_id)
        if row is None:
            raise NotFoundError(recognition_id)
        result = _record(row)
        result["state_version"] = _state_version(conn)
        return result


def list_observations(
    *,
    district: str | None = None,
    ear_tag: str | None = None,
    recognition_id: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = 100,
    offset: int = 0,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Paged detail listing, all statements from one read snapshot."""
    where = ["committed = 1"]
    params: list[Any] = []
    if district is not None:
        where.append("district = ?")
        params.append(district)
    if ear_tag is not None:
        where.append("ear_tag = ?")
        params.append(ear_tag)
    if recognition_id is not None:
        where.append("recognition_id = ?")
        params.append(recognition_id)
    if start_date is not None:
        where.append("observed_on >= ?")
        params.append(start_date.isoformat())
    if end_date is not None:
        where.append("observed_on <= ?")
        params.append(end_date.isoformat())
    clause = " AND ".join(where)

    with read_transaction(db_path) as conn:
        total = int(
            conn.execute(
                f"SELECT COUNT(*) AS n FROM observations WHERE {clause}", params
            ).fetchone()["n"]
        )
        rows = conn.execute(
            f"""
            SELECT recognition_id, observed_on, district, ear_tag
            FROM observations
            WHERE {clause}
            ORDER BY observed_on, recognition_id
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
        version = _state_version(conn)

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "records": [_record(r) for r in rows],
        "state_version": version,
    }


def _validate_range(start_date: date, end_date: date) -> None:
    if start_date > end_date:
        raise InvalidRangeError("start_date 不能晚于 end_date")


def _summary_page(conn, district: str | None, start_date: date, end_date: date):
    """Fetch the summary records that can overlap the window.

    Only summary records are read.  The span columns let SQLite skip records
    wholly before or after the window, and the data query itself is capped at
    ``MAX_SUMMARY_ROWS`` rows.  Only when that cap is reached may a single
    existence probe run one row deeper; if it finds anything, the query is
    rejected instead of silently returning a truncated answer.
    """
    start, end = start_date.isoformat(), end_date.isoformat()
    where = "committed = 1 AND last_observed_on >= ? AND first_observed_on <= ?"
    if district is None:
        params: list[Any] = [start, end]
    else:
        where = (
            "committed = 1 AND district = ? AND last_observed_on >= ? "
            "AND first_observed_on <= ?"
        )
        params = [district, start, end]

    rows = conn.execute(
        f"""
        SELECT district, ear_tag, first_observed_on, last_observed_on,
               day_counts
        FROM tag_summaries
        WHERE {where}
        LIMIT ?
        """,
        [*params, MAX_SUMMARY_ROWS],
    ).fetchall()

    if len(rows) == MAX_SUMMARY_ROWS:
        overflow = conn.execute(
            f"""
            SELECT 1 FROM tag_summaries
            WHERE {where}
            LIMIT 1 OFFSET ?
            """,
            [*params, MAX_SUMMARY_ROWS],
        ).fetchone()
        if overflow is not None:
            raise SummaryLimitExceeded(MAX_SUMMARY_ROWS)
    return rows, len(rows)


def _observed_days_in_range(row: Mapping[str, Any], start: str, end: str) -> int:
    """Count the distinct observed days of one summary record in a window.

    The days were already de-duplicated at write time; this is a range count
    over the stored sorted list, not a merge of identification rows.
    """
    if row["first_observed_on"] >= start and row["last_observed_on"] <= end:
        return len(_parse_days(row["day_counts"]))
    days = [entry[0] for entry in _parse_days(row["day_counts"])]
    return bisect_right(days, end) - bisect_left(days, start)


def district_stats(
    district: str,
    start_date: date,
    end_date: date,
    db_path: str | None = None,
) -> dict[str, Any]:
    """One-day vs multi-day individual counts for one district and range."""
    _validate_range(start_date, end_date)
    start, end = start_date.isoformat(), end_date.isoformat()
    with read_transaction(db_path) as conn:
        version = _state_version(conn)
        rows, rows_used = _summary_page(conn, district, start_date, end_date)

    one_day = multi_day = 0
    for row in rows:
        days = _observed_days_in_range(row, start, end)
        if days == 0:
            continue
        if days == 1:
            one_day += 1
        else:
            multi_day += 1

    return {
        "district": district,
        "start_date": start_date,
        "end_date": end_date,
        "one_day_count": one_day,
        "multi_day_count": multi_day,
        "total_individuals": one_day + multi_day,
        "summary_rows_used": rows_used,
        "state_version": version,
    }


def all_district_stats(
    start_date: date,
    end_date: date,
    district: str | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Per-district counts for every district (or one district) in the range.

    The total number of summary records fetched is capped, for the whole
    answer, at ``MAX_SUMMARY_ROWS``.
    """
    _validate_range(start_date, end_date)
    start, end = start_date.isoformat(), end_date.isoformat()
    with read_transaction(db_path) as conn:
        version = _state_version(conn)
        rows, rows_used = _summary_page(conn, district, start_date, end_date)

    grouped: dict[str, list[int]] = {}
    for row in rows:
        days = _observed_days_in_range(row, start, end)
        if days == 0:
            continue
        grouped.setdefault(row["district"], []).append(days)

    one_day = sum(1 for counts in grouped.values() for n in counts if n == 1)
    multi_day = sum(1 for counts in grouped.values() for n in counts if n >= 2)

    districts = []
    for name in sorted(grouped):
        days_counts = grouped[name]
        d_one = sum(1 for n in days_counts if n == 1)
        d_multi = sum(1 for n in days_counts if n >= 2)
        districts.append(
            {
                "district": name,
                "start_date": start_date,
                "end_date": end_date,
                "one_day_count": d_one,
                "multi_day_count": d_multi,
                "total_individuals": d_one + d_multi,
            }
        )

    return {
        "start_date": start_date,
        "end_date": end_date,
        "total_summary_rows_used": rows_used,
        "total_one_day_count": one_day,
        "total_multi_day_count": multi_day,
        "districts": districts,
        "state_version": version,
    }
