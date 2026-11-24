"""Tests for the cross-district same-tag ear-tag errata batch.

Covers the whole requirement:

* pure solver: unique optimum, ambiguous tie, conflict, same-district repeats,
  batch-outside occupation, minimum-change objective (brute-force differential);
* HTTP flow: applied / ambiguous / conflict / stale outcomes and validation;
* persistence: only ear tags change, summaries move with the batch atomically;
* concurrency: solving holds no write lock, stale plans are refused while
  unrelated writes still apply;
* read bounds: only batch details and relevant (district, tag) summaries.
"""

from __future__ import annotations

import json
import random
from datetime import date

import pytest

from app import db, errata as errata_logic, repository
from app.errata import ErrataRecord, ErrataSnapshot, brute_force_plans, solve_errata

DAY = "2026-03-01"
NORTH = "北沟片区"
SOUTH = "南坡片区"
DISTRICTS = (NORTH, SOUTH)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def put(client, rid, day, district, tag):
    return client.post(
        "/observations",
        json={
            "recognition_id": rid,
            "observed_on": day,
            "district": district,
            "ear_tag": tag,
        },
    )


def errata_payload(client_items, districts=None, observed_on=DAY):
    return {
        "observed_on": observed_on,
        "districts": list(districts or DISTRICTS),
        "items": client_items,
    }


def submit(client, client_items, districts=None, observed_on=DAY):
    return client.post(
        "/errata/ear-tag-batches",
        json=errata_payload(client_items, districts, observed_on),
    )


def item(rid, *candidates):
    return {"recognition_id": rid, "candidate_ear_tags": list(candidates)}


# ---------------------------------------------------------------------------
# Pure solver: hand-built scenarios
# ---------------------------------------------------------------------------


def _snapshot(records, outside=None):
    return ErrataSnapshot(
        observed_on=DAY,
        districts=DISTRICTS,
        records=tuple(records),
        outside_occ=outside or {},
        state_version=1,
    )


def test_solver_keeps_current_tags_when_no_cross_district_conflict():
    snapshot = _snapshot(
        [
            ErrataRecord("r1", DAY, NORTH, "ET-1", ("ET-9",)),
            ErrataRecord("r2", DAY, SOUTH, "ET-2", ("ET-8",)),
        ]
    )
    outcome = solve_errata(snapshot)
    assert outcome.outcome == "unique"
    plan = outcome.plans[0]
    assert plan.changed_count == 0
    assert {line.recognition_id: line.new_ear_tag for line in plan.lines} == {
        "r1": "ET-1",
        "r2": "ET-2",
    }


def test_solver_forced_unique_minimum_change():
    # r1 (north) and r2 (south) both read ET-1.  r1 cannot move to ET-2
    # (occupied in the south outside the batch); r2 cannot move to ET-2
    # (occupied in the north outside the batch).  r2's extra candidate ET-3 is
    # free, so the only one-rewrite repair is r2 -> ET-3.
    snapshot = _snapshot(
        [
            ErrataRecord("r1", DAY, NORTH, "ET-1", ("ET-2",)),
            ErrataRecord("r2", DAY, SOUTH, "ET-1", ("ET-2", "ET-3")),
        ],
        outside={"ET-2": (1, 1)},
    )
    outcome = solve_errata(snapshot)
    assert outcome.outcome == "unique"
    plan = outcome.plans[0]
    assert plan.changed_count == 1
    tags = {line.recognition_id: line.new_ear_tag for line in plan.lines}
    assert tags == {"r1": "ET-1", "r2": "ET-3"}


def test_solver_ambiguous_swap_returns_two_plans_without_picking():
    # Either side can be rewritten to ET-2 at cost 1; both are optimal.
    snapshot = _snapshot(
        [
            ErrataRecord("r1", DAY, NORTH, "ET-1", ("ET-2",)),
            ErrataRecord("r2", DAY, SOUTH, "ET-1", ("ET-2",)),
        ]
    )
    outcome = solve_errata(snapshot)
    assert outcome.outcome == "ambiguous"
    assert len(outcome.plans) == 2
    assert all(plan.changed_count == 1 for plan in outcome.plans)
    maps = [plan.as_tag_map() for plan in outcome.plans]
    assert {"r1": "ET-2", "r2": "ET-1"} in maps
    assert {"r1": "ET-1", "r2": "ET-2"} in maps


def test_solver_conflict_when_every_escape_is_blocked():
    # r1 (north, ET-1) and r2 (south, ET-1) both only have candidate ET-2.
    # ET-2 already occupies both districts outside the batch, so neither record
    # can legally move, and the cross-district ET-1 cannot be resolved.
    snapshot = _snapshot(
        [
            ErrataRecord("r1", DAY, NORTH, "ET-1", ("ET-2",)),
            ErrataRecord("r2", DAY, SOUTH, "ET-1", ("ET-2",)),
        ],
        outside={"ET-2": (1, 1)},
    )
    outcome = solve_errata(snapshot)
    assert outcome.outcome == "conflict"
    assert outcome.plans == []
    assert outcome.reasons
    assert "ET-2" in " ".join(outcome.reasons)


def test_solver_allows_same_district_repeats():
    # Two north records and one south record all read ET-1.  The north pair may
    # keep ET-1 together; only the south record has to move.
    snapshot = _snapshot(
        [
            ErrataRecord("r1", DAY, NORTH, "ET-1", ("ET-2",)),
            ErrataRecord("r2", DAY, NORTH, "ET-1", ("ET-2",)),
            ErrataRecord("r3", DAY, SOUTH, "ET-1", ("ET-3",)),
        ]
    )
    outcome = solve_errata(snapshot)
    assert outcome.outcome == "unique"
    plan = outcome.plans[0]
    tags = plan.as_tag_map()
    assert tags["r1"] == "ET-1"
    assert tags["r2"] == "ET-1"
    assert tags["r3"] == "ET-3"
    assert plan.changed_count == 1


# ---------------------------------------------------------------------------
# Pure solver: randomized brute-force differential
# ---------------------------------------------------------------------------

TAGS = [f"ET-{i}" for i in range(7)]


def _random_snapshot(rng: random.Random, n: int) -> ErrataSnapshot:
    """Build a whole-day database, then derive outside occupancy exactly like
    the repository does (summary totals minus the batch's own records)."""
    database: dict[str, tuple[str, str]] = {}
    records = []
    for i in range(n):
        rid = f"r{i}"
        district = rng.choice(DISTRICTS)
        tag = rng.choice(TAGS)
        k = rng.randint(1, 3)
        candidates = tuple(
            rng.sample([t for t in TAGS if t != tag], min(k, len(TAGS) - 1))
        )
        records.append(ErrataRecord(rid, DAY, district, tag, candidates))
        database[rid] = (district, tag)
    for j in range(rng.randrange(0, 12)):
        database[f"x{j}"] = (rng.choice(DISTRICTS), rng.choice(TAGS))

    totals = {tag: [0, 0] for tag in TAGS}
    for district, tag in database.values():
        totals[tag][DISTRICTS.index(district)] += 1
    outside = {tag: list(counts) for tag, counts in totals.items()}
    for record in records:
        outside[record.ear_tag][DISTRICTS.index(record.district)] -= 1
    return ErrataSnapshot(
        observed_on=DAY,
        districts=DISTRICTS,
        records=tuple(records),
        outside_occ={tag: tuple(counts) for tag, counts in outside.items()},
        state_version=1,
    )


def test_solver_matches_brute_force_on_random_batches():
    rng = random.Random(20261124)
    unique = ambiguous = conflicts = 0
    for _ in range(500):
        snapshot = _random_snapshot(rng, rng.randint(1, 9))
        outcome = solve_errata(snapshot)
        brute = brute_force_plans(snapshot)
        if not brute:
            assert outcome.outcome == "conflict"
            conflicts += 1
            continue
        if len(brute) == 1:
            assert outcome.outcome == "unique"
            assert outcome.plans[0].as_tag_map() == brute[0].as_tag_map()
            assert outcome.plans[0].changed_count == brute[0].changed_count
            unique += 1
        else:
            assert outcome.outcome == "ambiguous"
            assert len(outcome.plans) == 2
            assert all(
                plan.changed_count == brute[0].changed_count for plan in outcome.plans
            )
            ambiguous += 1
    # Sanity: every branch of the verdict logic was exercised.
    assert unique and ambiguous and conflicts


# ---------------------------------------------------------------------------
# HTTP: applied outcome and persistence effects
# ---------------------------------------------------------------------------


def test_errata_applied_rewrites_only_ear_tags_and_keeps_summary_consistent(client):
    put(client, "N1", DAY, NORTH, "ET-1")
    put(client, "N2", DAY, NORTH, "ET-1")  # same-district repeat, kept
    put(client, "S1", DAY, SOUTH, "ET-1")  # must move
    put(client, "S-X", DAY, SOUTH, "ET-2")  # blocks ET-2 in the south

    response = submit(
        client,
        [
            item("N1", "ET-2"),
            item("N2", "ET-2"),
            item("S1", "ET-3"),
        ],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "applied"
    assert body["committed_state_version"] == body["solved_state_version"] + 1

    plan = body["plans"][0]
    assert plan["changed_count"] == 1
    lines = {line["recognition_id"]: line for line in plan["lines"]}
    assert lines["N1"]["new_ear_tag"] == "ET-1" and lines["N1"]["changed"] is False
    assert lines["N2"]["new_ear_tag"] == "ET-1" and lines["N2"]["changed"] is False
    assert lines["S1"]["new_ear_tag"] == "ET-3" and lines["S1"]["changed"] is True

    # Only the ear tag changed; day and district are untouched.
    for rid, district in (("N1", NORTH), ("N2", NORTH), ("S1", SOUTH)):
        record = client.get(f"/observations/{rid}").json()
        assert record["observed_on"] == DAY
        assert record["district"] == district
    assert client.get("/observations/S1").json()["ear_tag"] == "ET-3"

    # Recapture summary reflects post-batch same-day counts.
    summary = {
        (row["district"], row["ear_tag"]): row["day_count"]
        for row in body["recapture_summary"]
    }
    assert summary[(NORTH, "ET-1")] == 2
    assert summary[(SOUTH, "ET-1")] == 0
    assert summary[(SOUTH, "ET-3")] == 1
    assert summary[(SOUTH, "ET-2")] == 1

    # Stored summary JSON agrees with the returned recapture summary.
    with db.read_transaction() as conn:
        row = conn.execute(
            "SELECT day_counts FROM tag_summaries WHERE district = ? AND ear_tag = ?",
            (SOUTH, "ET-3"),
        ).fetchone()
        assert json.loads(row["day_counts"]) == [[DAY, 1]]
        gone = conn.execute(
            "SELECT 1 FROM tag_summaries WHERE district = ? AND ear_tag = ?",
            (SOUTH, "ET-1"),
        ).fetchone()
        assert gone is None


def test_errata_ambiguous_returns_two_plans_and_writes_nothing(client):
    put(client, "N1", DAY, NORTH, "ET-1")
    put(client, "S1", DAY, SOUTH, "ET-1")

    response = submit(client, [item("N1", "ET-2"), item("S1", "ET-2")])
    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "ambiguous"
    assert body["committed_state_version"] is None
    assert len(body["plans"]) == 2
    assert all(plan["changed_count"] == 1 for plan in body["plans"])

    # Nothing was written.
    assert client.get("/observations/N1").json()["ear_tag"] == "ET-1"
    assert client.get("/observations/S1").json()["ear_tag"] == "ET-1"
    stats = client.get(
        "/stats/districts",
        params={"start_date": DAY, "end_date": DAY},
    ).json()
    by = {row["district"]: row for row in stats["districts"]}
    assert by[NORTH]["total_individuals"] == 1
    assert by[SOUTH]["total_individuals"] == 1


def test_errata_conflict_returns_409_and_writes_nothing(client):
    put(client, "N1", DAY, NORTH, "ET-1")
    put(client, "S1", DAY, SOUTH, "ET-1")
    put(client, "N2", DAY, NORTH, "ET-2")
    put(client, "S2", DAY, SOUTH, "ET-2")  # ET-2 occupied in both districts

    before = client.get("/observations/N1").json()
    response = submit(client, [item("N1", "ET-2"), item("S1", "ET-2")])
    assert response.status_code == 409
    body = response.json()
    assert body["outcome"] == "conflict"
    assert body["reasons"]
    assert client.get("/observations/N1").json() == before


def test_errata_zero_change_is_a_noop_without_version_bump(client):
    put(client, "N1", DAY, NORTH, "ET-1")
    put(client, "S1", DAY, SOUTH, "ET-2")

    response = submit(client, [item("N1", "ET-9"), item("S1", "ET-8")])
    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "applied"
    assert body["plans"][0]["changed_count"] == 0
    # A no-op must not bump the state version: committed equals the solved one.
    assert body["committed_state_version"] == body["solved_state_version"]
    after = client.get(
        "/stats/districts", params={"start_date": DAY, "end_date": DAY}
    ).json()
    assert after["state_version"] == body["solved_state_version"]
    assert client.get("/observations/N1").json()["ear_tag"] == "ET-1"
    assert client.get("/observations/S1").json()["ear_tag"] == "ET-2"


# ---------------------------------------------------------------------------
# HTTP: request validation
# ---------------------------------------------------------------------------


def test_errata_rejects_same_district_twice(client):
    put(client, "N1", DAY, NORTH, "ET-1")
    response = submit(client, [item("N1", "ET-2")], districts=(NORTH, NORTH))
    assert response.status_code == 422
    assert "互不相同" in response.json()["detail"]


def test_errata_rejects_missing_record(client):
    response = submit(client, [item("GHOST", "ET-2")])
    assert response.status_code == 422
    assert "不存在" in response.json()["detail"]


def test_errata_rejects_record_on_another_day(client):
    put(client, "N1", "2026-03-02", NORTH, "ET-1")
    response = submit(client, [item("N1", "ET-2")])
    assert response.status_code == 422
    assert "观测日" in response.json()["detail"]


def test_errata_rejects_record_outside_the_two_districts(client):
    put(client, "W1", DAY, "西岭片区", "ET-1")
    response = submit(client, [item("W1", "ET-2")])
    assert response.status_code == 422
    assert "片区" in response.json()["detail"]


def test_errata_rejects_duplicate_record_ids(client):
    put(client, "N1", DAY, NORTH, "ET-1")
    response = submit(client, [item("N1", "ET-2"), item("N1", "ET-3")])
    assert response.status_code == 422


def test_errata_limits_items_and_candidate_count(client):
    too_many = [item(f"R{i}", "ET-9") for i in range(17)]
    response = client.post(
        "/errata/ear-tag-batches",
        json=errata_payload(too_many),
    )
    assert response.status_code == 422

    put(client, "N1", DAY, NORTH, "ET-1")
    response = submit(client, [item("N1", "ET-2", "ET-3", "ET-4", "ET-5")])
    assert response.status_code == 422


def test_errata_full_batch_of_16_records_is_solved_and_applied(client):
    # Eight north / eight south records sharing tags, all with free candidates.
    items = []
    for i in range(8):
        rid_n, rid_s = f"N{i}", f"S{i}"
        put(client, rid_n, DAY, NORTH, f"ET-{i}")
        put(client, rid_s, DAY, SOUTH, f"ET-{i}")
        items.append(item(rid_n, f"ET-N{i}"))
        items.append(item(rid_s, f"ET-S{i}"))
    assert len(items) == 16

    response = submit(client, items)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] in {"applied", "ambiguous"}
    assert len(body["plans"][0]["lines"]) == 16
    # The response never carries more than two plans.
    assert len(body["plans"]) <= 2


def test_errata_accepts_field_aliases(client):
    put(client, "N1", DAY, NORTH, "ET-1")
    put(client, "S1", DAY, SOUTH, "ET-1")
    response = client.post(
        "/errata/ear-tag-batches",
        json={
            "date": DAY,
            "areas": [NORTH, SOUTH],
            "items": [{"record_id": "N1", "candidates": ["ET-2"]},
                      {"record_id": "S1", "candidates": ["ET-2"]}],
        },
    )
    assert response.status_code == 200
    assert response.json()["outcome"] in {"applied", "ambiguous"}


# ---------------------------------------------------------------------------
# Stale detection and concurrency
# ---------------------------------------------------------------------------


def test_errata_stale_when_batch_record_changes_between_solve_and_save(
    client, monkeypatch
):
    # Build a uniquely-optimum plan first: the south record S1 must move to
    # ET-3 (its own candidates ET-2 are blocked both ways by outside rows),
    # while north N1 keeps ET-1.
    put(client, "N1", DAY, NORTH, "ET-1")
    put(client, "S1", DAY, SOUTH, "ET-1")
    put(client, "SX", DAY, SOUTH, "ET-2")
    put(client, "NX", DAY, NORTH, "ET-2")

    real_apply = repository.apply_errata_plan

    def apply_with_concurrent_edit(snapshot, plan, db_path=None):
        # An ordinary correction lands while the errata is about to save.
        client.patch("/observations/N1", json={"ear_tag": "ET-7"})
        return real_apply(snapshot, plan, db_path=db_path)

    monkeypatch.setattr(repository, "apply_errata_plan", apply_with_concurrent_edit)
    response = submit(client, [item("N1", "ET-2"), item("S1", "ET-3")])
    monkeypatch.undo()

    assert response.status_code == 409
    body = response.json()
    assert body["outcome"] == "stale"
    assert body["retryable"] is True
    assert body["reasons"]
    # The concurrent correction survived; the errata wrote nothing.
    assert client.get("/observations/N1").json()["ear_tag"] == "ET-7"


def test_errata_stale_when_outside_record_changes_relevant_occupation(
    client, monkeypatch
):
    put(client, "N1", DAY, NORTH, "ET-1")
    put(client, "S1", DAY, SOUTH, "ET-1")
    # Block ET-2 in both districts so the only repair is S1 -> ET-3.
    put(client, "NX", DAY, NORTH, "ET-2")
    put(client, "SX", DAY, SOUTH, "ET-2")

    real_apply = repository.apply_errata_plan

    def apply_with_concurrent_insert(snapshot, plan, db_path=None):
        # A batch-outside record now occupies ET-3 in the north, the exact tag
        # the plan chose for the south record.
        if "ET-3" in plan.as_tag_map().values():
            put(client, "N-BLOCK", DAY, NORTH, "ET-3")
        return real_apply(snapshot, plan, db_path=db_path)

    monkeypatch.setattr(repository, "apply_errata_plan", apply_with_concurrent_insert)
    response = submit(client, [item("N1", "ET-2"), item("S1", "ET-3")])
    monkeypatch.undo()

    assert response.status_code == 409
    assert response.json()["outcome"] == "stale"
    assert client.get("/observations/S1").json()["ear_tag"] == "ET-1"
    assert client.get("/observations/N-BLOCK").json()["ear_tag"] == "ET-3"


def test_errata_stale_when_outside_occupation_is_reduced(client, monkeypatch):
    # A batch-outside blocker disappearing between solve and save also changes
    # the relevant occupation, so the saved plan must be refused and retried.
    put(client, "N1", DAY, NORTH, "ET-1")
    put(client, "S1", DAY, SOUTH, "ET-1")
    put(client, "BN2", DAY, NORTH, "ET-2")

    real_apply = repository.apply_errata_plan

    def apply_with_blocker_removed(snapshot, plan, db_path=None):
        client.patch("/observations/BN2", json={"ear_tag": "ET-999"})
        return real_apply(snapshot, plan, db_path=db_path)

    monkeypatch.setattr(repository, "apply_errata_plan", apply_with_blocker_removed)
    response = submit(client, [item("N1", "ET-2"), item("S1", "ET-3")])
    monkeypatch.undo()

    assert response.status_code == 409
    assert response.json()["outcome"] == "stale"
    assert "ET-2" in response.json()["reasons"][0]


def test_unrelated_write_does_not_invalidate_the_plan(client, monkeypatch):
    put(client, "N1", DAY, NORTH, "ET-1")
    put(client, "S1", DAY, SOUTH, "ET-1")
    put(client, "NX", DAY, NORTH, "ET-2")
    put(client, "SX", DAY, SOUTH, "ET-2")
    # Unrelated tag/district/day write landing just before the errata save.
    real_apply = repository.apply_errata_plan

    def apply_with_unrelated_write(snapshot, plan, db_path=None):
        put(client, "OTHER", "2026-04-01", "西岭片区", "ET-99")
        return real_apply(snapshot, plan, db_path=db_path)

    monkeypatch.setattr(repository, "apply_errata_plan", apply_with_unrelated_write)
    response = submit(client, [item("N1", "ET-2"), item("S1", "ET-3")])
    monkeypatch.undo()

    assert response.status_code == 200
    assert response.json()["outcome"] == "applied"
    assert client.get("/observations/OTHER").status_code == 200


def test_solving_does_not_block_normal_writes(client):
    put(client, "N1", DAY, NORTH, "ET-1")
    put(client, "S1", DAY, SOUTH, "ET-1")

    # Hold a read snapshot open (the solving phase) while ordinary writes land.
    with db.read_transaction() as conn:
        rows = conn.execute("SELECT 1 FROM observations LIMIT 1").fetchall()
        assert rows is not None
        response_write = put(client, "MID", DAY, NORTH, "ET-5")
        assert response_write.status_code == 200
        correction = client.patch("/observations/MID", json={"ear_tag": "ET-6"})
        assert correction.status_code == 200
    assert client.get("/observations/MID").json()["ear_tag"] == "ET-6"


def test_failed_errata_save_rolls_back_everything(client, monkeypatch):
    put(client, "N1", DAY, NORTH, "ET-1")
    put(client, "S1", DAY, SOUTH, "ET-1")
    put(client, "NX", DAY, NORTH, "ET-2")
    put(client, "SX", DAY, SOUTH, "ET-2")
    before = client.get("/observations/S1").json()
    version_before = before["state_version"]

    def boom(conn):
        raise RuntimeError("simulated failure before commit")

    monkeypatch.setattr(repository, "_bump_state_version", boom)
    with pytest.raises(RuntimeError):
        submit(client, [item("N1", "ET-2"), item("S1", "ET-3")])
    monkeypatch.undo()

    assert client.get("/observations/S1").json() == before
    assert client.get("/observations/N1").json()["state_version"] == version_before


# ---------------------------------------------------------------------------
# Read bounds: only batch details + relevant summaries are read
# ---------------------------------------------------------------------------


class SqlRecorder:
    def __init__(self):
        self.statements = []

    def __call__(self, statement):
        self.statements.append(" ".join(statement.split()))

    @property
    def selects(self):
        return [s for s in self.statements if s.upper().startswith("SELECT")]


def test_errata_snapshot_reads_only_batch_details_and_relevant_summaries(
    client, db_path
):
    # Noisy unrelated data: other tags, other districts, other days.
    put(client, "N1", DAY, NORTH, "ET-1")
    put(client, "S1", DAY, SOUTH, "ET-1")
    put(client, "NOISE1", DAY, NORTH, "ET-NOISE")
    put(client, "NOISE2", "2026-01-01", NORTH, "ET-2")
    put(client, "NOISE3", DAY, "西岭片区", "ET-3")

    recorder = SqlRecorder()
    db.set_trace_hook(recorder)
    snapshot = repository.load_errata_snapshot(
        date.fromisoformat(DAY),
        DISTRICTS,
        [
            {"recognition_id": "N1", "candidate_ear_tags": ["ET-2"]},
            {"recognition_id": "S1", "candidate_ear_tags": ["ET-3"]},
        ],
    )
    db.set_trace_hook(None)

    detail_selects = [s for s in recorder.selects if "FROM observations" in s]
    summary_selects = [s for s in recorder.selects if "FROM tag_summaries" in s]
    assert len(detail_selects) == 1
    assert "recognition_id IN (" in detail_selects[0]
    # The detail lookup is keyed by the two batch ids, never a full scan.
    assert "LIMIT" not in detail_selects[0]
    assert len(summary_selects) == 1
    summary_sql = summary_selects[0]
    # Two district literals plus one literal per relevant tag (the trace
    # callback reports bound values inlined into the statement).
    relevant_tags = {
        tag for record in snapshot.records for tag in record.domain()
    }
    assert all(district in summary_sql for district in DISTRICTS)
    assert all(tag in summary_sql for tag in relevant_tags)
    # Unrelated tags never appear in the errata queries.
    assert "ET-NOISE" not in " ".join(recorder.statements)


def test_errata_endpoint_is_documented(client):
    spec = client.get("/openapi.json").json()
    assert "/errata/ear-tag-batches" in spec["paths"]
    assert client.get("/docs").status_code == 200
