"""Randomized differential test.

Builds a random sequence of creates and corrections, then compares the
incrementally maintained summary against a brute-force recount taken directly
from the detail rows.  This catches off-by-one errors in same-day collapsing
and in moving an identification between old and new district/ear-tag/day.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from app import db, repository

DISTRICTS = ["北沟片区", "南坡片区", "西岭片区"]
TAGS = [f"ET-{i}" for i in range(6)]
START = date(2026, 3, 1)


def _brute_force(conn, start: date, end: date):
    """Recount per district from the detail table, collapsing days."""
    rows = conn.execute(
        """
        SELECT district, ear_tag, observed_on
        FROM observations
        WHERE committed = 1 AND observed_on >= ? AND observed_on <= ?
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    days: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        days.setdefault((row["district"], row["ear_tag"]), set()).add(
            row["observed_on"]
        )
    result: dict[str, dict[str, int]] = {}
    for (district, _tag), observed in days.items():
        bucket = result.setdefault(district, {"one": 0, "multi": 0})
        if len(observed) == 1:
            bucket["one"] += 1
        else:
            bucket["multi"] += 1
    return result


def test_random_history_matches_brute_force(db_path):
    rng = random.Random(20260301)
    existing: list[str] = []
    next_id = 0

    for _ in range(600):
        if existing and rng.random() < 0.45:
            rid = rng.choice(existing)
            changes: dict = {}
            if rng.random() < 0.7:
                changes["observed_on"] = START + timedelta(days=rng.randrange(14))
            if rng.random() < 0.4:
                changes["district"] = rng.choice(DISTRICTS)
            if rng.random() < 0.4:
                changes["ear_tag"] = rng.choice(TAGS)
            if changes:
                repository.correct_observation(rid, changes)
        else:
            rid = f"REC-{next_id}"
            next_id += 1
            # Reuse ids rarely to also exercise the upsert path.
            if existing and rng.random() < 0.1:
                rid = rng.choice(existing)
            else:
                existing.append(rid)
            repository.put_observation(
                rid,
                START + timedelta(days=rng.randrange(14)),
                rng.choice(DISTRICTS),
                rng.choice(TAGS),
            )

    with db.read_transaction() as conn:
        for start_offset, end_offset in ((0, 13), (0, 6), (7, 13), (3, 3), (5, 8)):
            start = START + timedelta(days=start_offset)
            end = START + timedelta(days=end_offset)
            expected = _brute_force(conn, start, end)
            for district in DISTRICTS:
                got = repository.district_stats(district, start, end, db_path=db_path)
                want = expected.get(district, {"one": 0, "multi": 0})
                assert got["one_day_count"] == want["one"], (district, start, end)
                assert got["multi_day_count"] == want["multi"], (district, start, end)


def test_random_history_all_district_totals_match(db_path):
    rng = random.Random(7)
    for i in range(300):
        repository.put_observation(
            f"R{i}",
            START + timedelta(days=rng.randrange(10)),
            rng.choice(DISTRICTS),
            rng.choice(TAGS),
        )
    for _ in range(300):
        rid = f"R{rng.randrange(300)}"
        repository.correct_observation(
            rid,
            {
                "observed_on": START + timedelta(days=rng.randrange(10)),
                "district": rng.choice(DISTRICTS),
                "ear_tag": rng.choice(TAGS),
            },
        )

    start, end = START, START + timedelta(days=9)
    with db.read_transaction() as conn:
        expected = _brute_force(conn, start, end)

    got = repository.all_district_stats(start, end, db_path=db_path)
    by_name = {d["district"]: d for d in got["districts"]}
    for district in DISTRICTS:
        want = expected.get(district, {"one": 0, "multi": 0})
        assert by_name[district]["one_day_count"] == want["one"]
        assert by_name[district]["multi_day_count"] == want["multi"]
    assert got["total_one_day_count"] == sum(v["one"] for v in expected.values())
    assert got["total_multi_day_count"] == sum(v["multi"] for v in expected.values())
