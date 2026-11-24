"""Cross-district same-tag errata: pure constraint solving and batch plan.

The patrol scenario: two fenced sample districts that never connect produced
the same ear tag on one day, so at least one identification is a misread.  A
ranger submits one errata batch -- an observation day, the two districts and at
most 16 identification records from that day -- with 1-3 candidate ear tags
per record.  Only ear tags may change; day and district stay fixed.

The solved condition
--------------------
After the batch is applied, every ear tag that was involved in the batch (the
original tags and every candidate) may occupy at most one of the two districts
on the observation day, counting *all* records, including identifications that
are not part of the batch.  Repeated captures of one tag inside one district
stay allowed.

Each record's choices are its current tag (cost 0, the record is untouched) and
its candidate tags (cost 1, the record is rewritten).  The solver minimizes the
number of rewritten records and returns:

* exactly one optimum -> ``applied`` is performed by the repository layer;
* several optima     -> two of them, nothing is written;
* no solution        -> a conflict explanation, nothing is written.

This module contains no SQL and no I/O: :func:`solve_errata` works on a
:class:`ErrataSnapshot` produced by the repository, so the same input always
gives the same answer and the search can be cross-checked against brute force
in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


# ---------------------------------------------------------------------------
# Snapshot and result data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ErrataRecord:
    """One identification record of the errata batch, as currently stored."""

    recognition_id: str
    observed_on: str  # ISO day, identical for every record of the batch
    district: str
    ear_tag: str
    candidates: tuple[str, ...]  # de-duplicated, 1-3 candidate ear tags

    def domain(self) -> tuple[str, ...]:
        """Choosable tags: the current tag first (cost 0), then candidates."""
        choices: list[str] = [self.ear_tag]
        for tag in self.candidates:
            if tag != self.ear_tag:
                choices.append(tag)
        return tuple(choices)


@dataclass(frozen=True)
class ErrataSnapshot:
    """Everything one solve needs, taken from one committed state.

    ``outside_occ`` maps ``ear_tag -> (count_in_district_a, count_in_district_b)``
    for records that are *not* part of the batch.  Counts inside the batch are
    derived from the records themselves.
    """

    observed_on: str
    districts: tuple[str, str]
    records: tuple[ErrataRecord, ...]
    outside_occ: dict[str, tuple[int, int]]
    state_version: int

    def outside_districts_of(self, tag: str) -> set[int]:
        """Districts (0/1) where ``tag`` is already occupied outside the batch."""
        counts = self.outside_occ.get(tag, (0, 0))
        return {idx for idx, count in enumerate(counts) if count > 0}


@dataclass
class ErrataLine:
    """The new ear tag decided for one record (``changed`` flags a rewrite)."""

    recognition_id: str
    district: str
    original_ear_tag: str
    new_ear_tag: str
    changed: bool


@dataclass
class ErrataPlan:
    """One complete assignment, in the same order as the submitted records."""

    lines: tuple[ErrataLine, ...]
    changed_count: int

    def as_tag_map(self) -> dict[str, str]:
        return {line.recognition_id: line.new_ear_tag for line in self.lines}


@dataclass
class ErrataOutcome:
    """Solver verdict: applied-ready plan(s), or a conflict."""

    outcome: str  # "unique" | "ambiguous" | "conflict"
    plans: list[ErrataPlan] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pure search
# ---------------------------------------------------------------------------


def solve_errata(snapshot: ErrataSnapshot) -> ErrataOutcome:
    """Find minimum-change plans for one errata snapshot.

    The branch-and-bound search keeps the two cheapest *distinct* plans.  When
    two plans share the optimum cost the problem is ambiguous; when no complete
    assignment exists the hard conflicts are reported instead.
    """
    records = snapshot.records
    n = len(records)
    domains = [record.domain() for record in records]
    assignment: list[str | None] = [None] * n
    unassigned: set[int] = set(range(n))

    # Batch assignment state so far: tag -> {district_idx: record count}.
    # A tag may legally occupy at most one district, but several records in the
    # same district may share one tag (same-district repeats are allowed).
    tag_counts: dict[str, dict[int, int]] = {}

    best_plans: list[ErrataPlan] = []
    best_cost = n + 1

    def district_index(record: ErrataRecord) -> int:
        return snapshot.districts.index(record.district)

    def feasible(tag: str, district_idx: int) -> bool:
        """Whether ``tag`` may be placed in district ``district_idx`` now."""
        if district_idx in snapshot.outside_districts_of(tag):
            return False
        counts = tag_counts.get(tag)
        if not counts:
            return True
        # Already used by the batch: legal only when it stays in the same
        # district (the count there is positive, every other key is absent).
        return district_idx in counts

    def build_plan() -> ErrataPlan:
        lines: list[ErrataLine] = []
        changed = 0
        for idx, record in enumerate(records):
            new_tag = assignment[idx]
            assert new_tag is not None
            is_changed = new_tag != record.ear_tag
            changed += int(is_changed)
            lines.append(
                ErrataLine(
                    recognition_id=record.recognition_id,
                    district=record.district,
                    original_ear_tag=record.ear_tag,
                    new_ear_tag=new_tag,
                    changed=is_changed,
                )
            )
        return ErrataPlan(lines=tuple(lines), changed_count=changed)

    def search(spent: int) -> None:
        nonlocal best_cost, best_plans

        if spent > best_cost:
            return
        if not unassigned:
            plan = build_plan()
            if plan.changed_count < best_cost:
                best_cost = plan.changed_count
                best_plans = [plan]
            elif plan.changed_count == best_cost:
                if len(best_plans) < 2 and plan.as_tag_map() not in (
                    existing.as_tag_map() for existing in best_plans
                ):
                    best_plans.append(plan)
            return

        # MRV: extend the record with the fewest currently legal choices; ties
        # break on submission order so results are deterministic.
        def legal_options(idx: int) -> list[str]:
            district_idx = district_index(records[idx])
            return [
                tag for tag in domains[idx] if feasible(tag, district_idx)
            ]

        var = min(
            unassigned,
            key=lambda idx: (len(legal_options(idx)), idx),
        )
        record = records[var]
        district_idx = district_index(record)
        unassigned.remove(var)

        for tag in legal_options(var):
            next_spent = spent + (tag != record.ear_tag)
            if next_spent > best_cost:
                continue
            counts = tag_counts.setdefault(tag, {})
            counts[district_idx] = counts.get(district_idx, 0) + 1
            assignment[var] = tag
            search(next_spent)
            assignment[var] = None
            counts[district_idx] -= 1
            if counts[district_idx] == 0:
                del counts[district_idx]
            if not counts:
                del tag_counts[tag]

        unassigned.add(var)

    search(0)

    if not best_plans:
        relevant = {tag for domain in domains for tag in domain}
        blocked = sorted(
            tag for tag in relevant if len(snapshot.outside_districts_of(tag)) == 2
        )
        reasons = ["候选耳标组合无解：任何赋值都至少有一个耳标当天跨两个片区"]
        if blocked:
            reasons.append(
                "以下耳标在两个片区都有批外记录，批内无法消除："
                + "、".join(blocked)
            )
        return ErrataOutcome(outcome="conflict", reasons=reasons)
    if len(best_plans) >= 2:
        return ErrataOutcome(outcome="ambiguous", plans=best_plans[:2])
    return ErrataOutcome(outcome="unique", plans=best_plans[:1])


def brute_force_plans(snapshot: ErrataSnapshot) -> list[ErrataPlan]:
    """Enumerate every legal assignment (tests only).

    Used by the differential tests to prove the branch-and-bound result: the
    cheapest brute-force assignments must equal the solver's optimum, and the
    number of optimum assignments decides unique vs ambiguous.
    """
    records = snapshot.records
    domains = [record.domain() for record in records]
    assignment: list[str | None] = [None] * len(records)
    results: list[ErrataPlan] = []

    def visit(idx: int) -> None:
        if idx == len(records):
            lines = tuple(
                ErrataLine(
                    recognition_id=records[j].recognition_id,
                    district=records[j].district,
                    original_ear_tag=records[j].ear_tag,
                    new_ear_tag=assignment[j],  # type: ignore[arg-type]
                    changed=assignment[j] != records[j].ear_tag,
                )
                for j in range(len(records))
            )
            changed = sum(1 for line in lines if line.changed)
            results.append(ErrataPlan(lines=lines, changed_count=changed))
            return
        record = records[idx]
        district_idx = snapshot.districts.index(record.district)
        for tag in domains[idx]:
            # Batch-outside occupation blocks the tag in that district outright.
            if district_idx in snapshot.outside_districts_of(tag):
                continue
            batch_districts = {
                snapshot.districts.index(records[j].district)
                for j, chosen in enumerate(assignment)
                if chosen == tag
            }
            # Same-district repeats stay legal; another occupied district does not.
            if batch_districts and district_idx not in batch_districts:
                continue
            assignment[idx] = tag
            visit(idx + 1)
            assignment[idx] = None

    visit(0)
    if not results:
        return []
    minimum = min(plan.changed_count for plan in results)
    return [plan for plan in results if plan.changed_count == minimum]


# ---------------------------------------------------------------------------
# Request validation shared by API and repository
# ---------------------------------------------------------------------------


def validate_request(
    observed_on: date,
    districts: tuple[str, str],
    items: Any,
) -> None:
    """Cross-field validation that pydantic cannot express alone."""
    if districts[0] == districts[1]:
        raise ErrataValidationError("两个片区必须互不相同")
    ids = [item.recognition_id for item in items]
    if len(ids) != len(set(ids)):
        raise ErrataValidationError("同一识别记录号在勘误批中只能出现一次")
    for item in items:
        if len(item.candidate_ear_tags) != len(set(item.candidate_ear_tags)):
            raise ErrataValidationError(
                f"记录 {item.recognition_id} 的备选耳标存在重复"
            )


class ErrataValidationError(ValueError):
    """The errata request itself is malformed (HTTP 422)."""


class ErrataStaleError(Exception):
    """The committed state moved between solving and saving (HTTP 409)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)
