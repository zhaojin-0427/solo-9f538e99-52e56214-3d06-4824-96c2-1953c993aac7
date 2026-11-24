"""Invariant tests that inspect the SQL actually issued by the service.

These are the requirements that are easy to *claim* but easy to get wrong:

* statistics must not read the identification detail;
* statistics must not merge rows day-by-day at query time;
* one query may read at most 1024 summary records;
* a correction's read volume must not grow with an ear tag's history;
* detail and summary must come from one committed state;
* a failed save must leave the previous numbers visible.
"""

from __future__ import annotations

import re

import pytest

from app import db, repository


class SqlRecorder:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def __call__(self, statement: str) -> None:
        self.statements.append(" ".join(statement.split()))

    def reset(self) -> None:
        self.statements.clear()

    @property
    def selects(self) -> list[str]:
        return [s for s in self.statements if s.upper().startswith("SELECT")]


@pytest.fixture()
def sql(db_path):
    recorder = SqlRecorder()
    db.set_trace_hook(recorder)
    yield recorder
    db.set_trace_hook(None)


def seed(client, count, district="北沟片区", tag_prefix="ET"):
    for i in range(count):
        client.post(
            "/observations",
            json={
                "recognition_id": f"R{i}",
                "observed_on": f"2026-03-{(i % 28) + 1:02d}",
                "district": district,
                "ear_tag": f"{tag_prefix}-{i}",
            },
        )


# ---------------------------------------------------------------------------
# Statistics read the summary only
# ---------------------------------------------------------------------------


def test_stats_never_read_detail_rows(client, sql):
    seed(client, 5)
    sql.reset()

    response = client.get(
        "/stats/districts/北沟片区",
        params={"start_date": "2026-03-01", "end_date": "2026-03-31"},
    )
    assert response.status_code == 200

    selects = sql.selects
    assert any("tag_summaries" in s for s in selects), "expected a summary SELECT"
    for statement in selects:
        assert "observations" not in statement


def test_stats_do_not_group_by_day_at_query_time(client, sql):
    seed(client, 5)
    sql.reset()

    client.get(
        "/stats/districts",
        params={"start_date": "2026-03-01", "end_date": "2026-03-31"},
    )

    for statement in sql.selects:
        assert "GROUP BY" not in statement.upper()
        assert "COUNT(" not in statement.upper()


def test_same_day_repeats_are_collapsed_at_write_time(client):
    for i in range(4):
        client.post(
            "/observations",
            json={
                "recognition_id": f"R{i}",
                "observed_on": "2026-03-01",
                "district": "北沟片区",
                "ear_tag": "ET-1",
            },
        )

    import json

    with db.read_transaction() as conn:
        row = conn.execute(
            "SELECT day_counts FROM tag_summaries WHERE district = ? AND ear_tag = ?",
            ("北沟片区", "ET-1"),
        ).fetchone()
    assert json.loads(row["day_counts"]) == [["2026-03-01", 4]]


# ---------------------------------------------------------------------------
# Summary row cap
# ---------------------------------------------------------------------------


def test_summary_budget_is_enforced(client):
    # 1025 distinct (district, ear tag) summary records, each on one day.
    with db.write_transaction() as conn:
        for i in range(repository.MAX_SUMMARY_ROWS + 1):
            repository._add_day(conn, "北沟片区", f"ET-{i}", "2026-03-01")

    response = client.get(
        "/stats/districts/北沟片区",
        params={"start_date": "2026-03-01", "end_date": "2026-03-31"},
    )
    assert response.status_code == 422
    assert "1024" in response.json()["detail"]


def test_exactly_at_the_budget_is_allowed(client):
    with db.write_transaction() as conn:
        for i in range(repository.MAX_SUMMARY_ROWS):
            repository._add_day(conn, "北沟片区", f"ET-{i}", "2026-03-01")

    response = client.get(
        "/stats/districts/北沟片区",
        params={"start_date": "2026-03-01", "end_date": "2026-03-31"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["summary_rows_used"] == 1024
    assert body["one_day_count"] == 1024


def test_span_index_skips_summaries_outside_the_window(client):
    with db.write_transaction() as conn:
        for i in range(50):
            repository._add_day(conn, "北沟片区", f"ET-{i}", "2020-01-01")

    response = client.get(
        "/stats/districts/北沟片区",
        params={"start_date": "2026-03-01", "end_date": "2026-03-31"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total_individuals"] == 0
    assert body["summary_rows_used"] == 0


# ---------------------------------------------------------------------------
# Correction read volume is independent of tag history
# ---------------------------------------------------------------------------


def test_correction_reads_constant_detail_rows_regardless_of_history(client, sql):
    # Build a tag with a long history plus an unrelated tag.
    for i in range(60):
        client.post(
            "/observations",
            json={
                "recognition_id": f"H{i}",
                "observed_on": f"2026-01-{(i % 28) + 1:02d}",
                "district": "北沟片区",
                "ear_tag": "ET-HISTORY",
            },
        )
    client.post(
        "/observations",
        json={
            "recognition_id": "TARGET",
            "observed_on": "2026-03-01",
            "district": "北沟片区",
            "ear_tag": "ET-HISTORY",
        },
    )

    sql.reset()
    response = client.patch(
        "/observations/TARGET",
        json={"district": "南坡片区", "ear_tag": "ET-NEW"},
    )
    assert response.status_code == 200

    detail_reads = [s for s in sql.selects if re.search(r"\bFROM observations\b", s)]
    # One detail row is read by unique key at the start, one at the end to
    # return the saved record.  Neither scales with the tag's history.
    assert len(detail_reads) == 2
    for statement in detail_reads:
        assert re.search(r"WHERE recognition_id = ", statement)


def test_correction_read_count_does_not_grow_with_days(client, sql):
    def measure(history_days: int) -> int:
        tag = f"ET-{history_days}"
        for i in range(history_days):
            client.post(
                "/observations",
                json={
                    "recognition_id": f"hist-{tag}-{i}",
                    "observed_on": f"2025-{((i // 28) % 12) + 1:02d}-{(i % 28) + 1:02d}",
                    "district": "北沟片区",
                    "ear_tag": tag,
                },
            )
        rid = f"target-{tag}"
        client.post(
            "/observations",
            json={
                "recognition_id": rid,
                "observed_on": "2026-03-01",
                "district": "北沟片区",
                "ear_tag": tag,
            },
        )
        sql.reset()
        client.patch(f"/observations/{rid}", json={"observed_on": "2026-03-09"})
        return len(sql.statements)

    small = measure(5)
    large = measure(120)
    assert small == large


# ---------------------------------------------------------------------------
# Snapshot consistency and rollback
# ---------------------------------------------------------------------------


def test_failed_save_keeps_old_detail_and_numbers(client, monkeypatch):
    client.post(
        "/observations",
        json={
            "recognition_id": "R1",
            "observed_on": "2026-03-01",
            "district": "北沟片区",
            "ear_tag": "ET-1",
        },
    )
    before = client.get(
        "/stats/districts/北沟片区",
        params={"start_date": "2026-03-01", "end_date": "2026-03-31"},
    ).json()

    def boom(conn) -> None:
        raise RuntimeError("simulated failure before commit")

    # Fail after the detail row and summary were already mutated, so the test
    # exercises the rollback rather than an early abort.
    monkeypatch.setattr(repository, "_bump_state_version", boom)
    with pytest.raises(RuntimeError):
        client.patch("/observations/R1", json={"observed_on": "2026-03-20"})
    monkeypatch.undo()

    record = client.get("/observations/R1").json()
    assert record["observed_on"] == "2026-03-01"

    after = client.get(
        "/stats/districts/北沟片区",
        params={"start_date": "2026-03-01", "end_date": "2026-03-31"},
    ).json()
    assert after["one_day_count"] == before["one_day_count"]
    assert after["multi_day_count"] == before["multi_day_count"]
    assert after["state_version"] == before["state_version"]


def test_failed_creation_leaves_nothing_behind(client, monkeypatch):
    def boom(conn) -> None:
        raise RuntimeError("simulated failure before commit")

    monkeypatch.setattr(repository, "_bump_state_version", boom)
    with pytest.raises(RuntimeError):
        client.post(
            "/observations",
            json={
                "recognition_id": "R1",
                "observed_on": "2026-03-01",
                "district": "北沟片区",
                "ear_tag": "ET-1",
            },
        )
    monkeypatch.undo()

    assert client.get("/observations/R1").status_code == 404
    stats = client.get(
        "/stats/districts/北沟片区",
        params={"start_date": "2026-03-01", "end_date": "2026-03-31"},
    ).json()
    assert stats["total_individuals"] == 0


def test_query_reads_at_most_1024_summary_rows(client, sql):
    """The full 1024-row answer is one capped statement plus one probe."""
    with db.write_transaction() as conn:
        for i in range(repository.MAX_SUMMARY_ROWS):
            repository._add_day(conn, "北沟片区", f"ET-{i}", "2026-03-01")

    sql.reset()
    response = client.get(
        "/stats/districts/北沟片区",
        params={"start_date": "2026-03-01", "end_date": "2026-03-31"},
    )
    assert response.status_code == 200

    data_reads = [
        s
        for s in sql.selects
        if "tag_summaries" in s and s.upper().startswith("SELECT DISTRICT")
    ]
    probe_reads = [
        s
        for s in sql.selects
        if "tag_summaries" in s and s.upper().startswith("SELECT 1")
    ]
    assert len(data_reads) == 1
    assert len(probe_reads) == 1
    assert data_reads[0].upper().endswith("LIMIT 1024")


def test_detail_and_summary_share_one_state_version(client):
    put = client.post
    put(
        "/observations",
        json={
            "recognition_id": "R1",
            "observed_on": "2026-03-01",
            "district": "北沟片区",
            "ear_tag": "ET-1",
        },
    )
    stats = client.get(
        "/stats/districts/北沟片区",
        params={"start_date": "2026-03-01", "end_date": "2026-03-31"},
    ).json()
    detail = client.get(
        "/observations",
        params={"district": "北沟片区"},
    ).json()
    assert stats["state_version"] == detail["state_version"]


def test_reading_during_open_write_sees_previous_committed_state(client, db_path):
    """A reader never observes an uncommitted write's rows."""
    client.post(
        "/observations",
        json={
            "recognition_id": "R1",
            "observed_on": "2026-03-01",
            "district": "北沟片区",
            "ear_tag": "ET-1",
        },
    )

    writer = db.write_transaction()
    conn = writer.__enter__()
    try:
        repository._add_day(conn, "北沟片区", "ET-1", "2026-03-02")
        # The writer has not committed yet.
        stats = client.get(
            "/stats/districts/北沟片区",
            params={"start_date": "2026-03-01", "end_date": "2026-03-31"},
        ).json()
        assert stats["multi_day_count"] == 0
        assert stats["one_day_count"] == 1
    finally:
        writer.__exit__(None, None, None)

    stats = client.get(
        "/stats/districts/北沟片区",
        params={"start_date": "2026-03-01", "end_date": "2026-03-31"},
    ).json()
    assert stats["multi_day_count"] == 1
