"""Truck compartments, arrival grading and collection planning.

The truck has compartments, each with a registered material grade (or grades it
may receive) and a capacity.  A partition is fitted **once at the depot before
departure** and locked for the whole run.  Two planning methods are supported:

``plan_with_partition`` (depot-fixed assignments)
    The depot pre-assigns each bin a compartment plus an optional fallback
    clause; the driver, offline, follows those clauses.  Terms whose
    *issued grade* no longer matches the bin's current grade (e.g. after a late
    drying certificate) are reported in ``reissue_needed``.

``plan_late`` (lock the partition, decide co-loading later)
    Only the partition and each station's allowed weight/grade combinations are
    locked; the driver arranges co-loading after seeing the real bin grades.
    The lexicographic objective is: first maximise *completed committed
    stations*, then maximise recyclable (paper + glass) weight.  A reservation
    helper proves that freedom left to earlier stations does not consume the
    capacity already promised to later stations.

Every bin is loaded whole into a single compartment (a bin is never split).

Arrival grading, per compartment, at the plant:

* any wet-mixed load in the compartment -> the whole compartment is mixed;
* dry paper and glass together in one compartment -> the whole compartment is
  mixed;
* otherwise the single grade survives only if the compartment's registered
  line receives it, else the compartment is downgraded to mixed.

The weighing slip separately totals qualified paper, qualified glass and mixed
weight.  ``mandatory`` bins (bins already dumped and therefore already in the
truck) cannot be left behind by either method.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .recycling import GLASS, PAPER, WET_MIXED, NONE

MIXED = "mixed"

# Canonical grade ordering (also the set accepted by a bin manifest).
_GRADES = (PAPER, GLASS, WET_MIXED)


class PartitionLockedError(RuntimeError):
    """Raised when the partition is changed after departure."""


@dataclass(frozen=True)
class Compartment:
    compartment_id: str
    capacity: int
    accepts: tuple[str, ...]
    mix_permitted: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "compartment_id": self.compartment_id,
            "capacity": self.capacity,
            "accepts": list(self.accepts),
            "mix_permitted": self.mix_permitted,
        }


class Truck:
    """A truck whose partition is fitted once at the depot and then locked.

    ``fit_partition`` may be called only while parked (before ``depart``); after
    departure any attempt to refit raises :class:`PartitionLockedError`.
    """

    def __init__(self) -> None:
        self._compartments: list[Compartment] = []
        self._fitted = False
        self._departed = False

    @property
    def locked(self) -> bool:
        return self._departed

    @property
    def compartments(self) -> list[Compartment]:
        return list(self._compartments)

    def fit_partition(self, specs: Sequence[Compartment | dict[str, Any]]) -> None:
        if self._departed:
            raise PartitionLockedError("partition is locked after departure")
        if self._fitted:
            raise PartitionLockedError("partition can be fitted only once")
        comps = [
            s
            if isinstance(s, Compartment)
            else Compartment(
                s["compartment_id"],
                int(s["capacity"]),
                tuple(s.get("accepts", [])),
                bool(s.get("mix_permitted", False)),
            )
            for s in specs
        ]
        ids = [c.compartment_id for c in comps]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate compartment ids")
        self._compartments = comps
        self._fitted = True

    def depart(self) -> None:
        if not self._fitted:
            raise PartitionLockedError("no partition fitted")
        self._departed = True


@dataclass(frozen=True)
class Bin:
    bin_id: str
    station_id: str
    grade: str
    weight: int
    mandatory: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "bin_id": self.bin_id,
            "station_id": self.station_id,
            "grade": self.grade,
            "weight": self.weight,
            "mandatory": self.mandatory,
        }


def make_bin(
    bin_id: str, station_id: str, grade: str, weight: int, *, mandatory: bool = False
) -> Bin:
    if grade not in _GRADES:
        raise ValueError(f"unknown bin grade {grade!r}")
    if int(weight) <= 0:
        raise ValueError("bin weight must be positive")
    if not station_id:
        raise ValueError("station_id required")
    return Bin(bin_id, station_id, grade, int(weight), bool(mandatory))


def bins_from_manifest(manifest: Iterable[dict[str, Any]]) -> list[Bin]:
    return [
        make_bin(
            m["bin_id"],
            m["station_id"],
            m["grade"],
            m["weight"],
            mandatory=bool(m.get("mandatory", False)),
        )
        for m in manifest
    ]


def _can_hold(comp: Compartment, grade: str) -> bool:
    """Whether a bin of *grade* may enter *comp* at all.

    Wet-mixed can only enter a compartment that explicitly permits mixing; a
    grade outside ``accepts`` likewise only enters a mixing compartment.
    """
    if grade == WET_MIXED:
        return comp.mix_permitted
    return grade in comp.accepts or comp.mix_permitted


def arrival_grade(comp: Compartment, grades: Sequence[str]) -> str:
    """The grade the compartment line receives at the plant."""
    if not grades:
        return NONE
    present = set(grades)
    if WET_MIXED in present:
        return MIXED
    if PAPER in present and GLASS in present:
        return MIXED
    (only,) = present
    return only if only in comp.accepts else MIXED


# ---------------------------------------------------------------------------
# Evaluation of a concrete assignment
# ---------------------------------------------------------------------------


def _weighing_slip(compartments, by_comp) -> dict[str, Any]:
    lines = []
    paper = glass = mixed = 0
    for comp in compartments:
        loaded = by_comp[comp.compartment_id]
        grade = arrival_grade(comp, [b.grade for b in loaded])
        weight = sum(b.weight for b in loaded)
        if grade == PAPER:
            paper += weight
        elif grade == GLASS:
            glass += weight
        elif grade == MIXED:
            mixed += weight
        lines.append(
            {
                "compartment_id": comp.compartment_id,
                "arrival_grade": grade,
                "weight": weight,
                "line_accepts": grade in comp.accepts
                or (grade == MIXED and comp.mix_permitted),
                "bins": [b.bin_id for b in loaded],
            }
        )
    return {
        "qualified_paper_weight": paper,
        "qualified_glass_weight": glass,
        "mixed_weight": mixed,
        "recyclable_weight": paper + glass,
        "total_weight": paper + glass + mixed,
        "lines": lines,
    }


def simulate(
    bins: Sequence[Bin],
    route: Sequence[str],
    compartments: Sequence[Compartment],
    assignment: dict[str, str | None],
    *,
    station_rules: dict[str, dict[str, Any]] | None = None,
    committed_stations: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Evaluate a fixed ``bin_id -> compartment_id | None`` assignment."""
    station_rules = station_rules or {}
    route = list(route)
    comp_lookup = {c.compartment_id: c for c in compartments}
    bin_by_id = {b.bin_id: b for b in bins}

    if set(assignment) - set(bin_by_id):
        raise ValueError("assignment names unknown bins")

    problems: list[str] = []
    by_comp: dict[str, list[Bin]] = {c.compartment_id: [] for c in compartments}
    for bin_id, comp_id in assignment.items():
        if comp_id is None:
            continue
        if comp_id not in comp_lookup:
            problems.append(f"{bin_id}: unknown compartment {comp_id}")
            continue
        if not _can_hold(comp_lookup[comp_id], bin_by_id[bin_id].grade):
            problems.append(
                f"{bin_id}: grade {bin_by_id[bin_id].grade} not allowed in {comp_id}"
            )
            continue
        by_comp[comp_id].append(bin_by_id[bin_id])

    off_route = sorted({b.station_id for b in bins if b.station_id not in set(route)})
    if off_route:
        problems.append("stations not on route: " + ",".join(off_route))

    for comp in compartments:
        total = sum(b.weight for b in by_comp[comp.compartment_id])
        if total > comp.capacity:
            problems.append(
                f"{comp.compartment_id}: load {total} exceeds capacity {comp.capacity}"
            )

    for b in bins:
        if b.mandatory and assignment.get(b.bin_id) is None:
            problems.append(f"{b.bin_id}: mandatory (already dumped) but left behind")

    station_report, completed = _station_report(
        bins, route, assignment, station_rules
    )
    committed = set(committed_stations) if committed_stations is not None else set(route)
    completed_committed = sum(
        1 for s in station_report if s["station_id"] in committed and s["completed"]
    )
    slip = _weighing_slip(compartments, by_comp)

    collection = [
        {
            "bin_id": b.bin_id,
            "station_id": b.station_id,
            "grade": b.grade,
            "weight": b.weight,
            "compartment_id": assignment.get(b.bin_id),
        }
        for b in bins
    ]
    return {
        "feasible": not problems,
        "problems": problems,
        "compartments": [
            {
                "compartment_id": c.compartment_id,
                "capacity": c.capacity,
                "accepts": list(c.accepts),
                "mix_permitted": c.mix_permitted,
                "bins": [b.bin_id for b in by_comp[c.compartment_id]],
                "weight": sum(b.weight for b in by_comp[c.compartment_id]),
                "arrival_grade": arrival_grade(
                    c, [b.grade for b in by_comp[c.compartment_id]]
                ),
            }
            for c in compartments
        ],
        "stations": station_report,
        "assignment": {b.bin_id: assignment.get(b.bin_id) for b in bins},
        "collection_list": collection,
        "weighing_slip": slip,
        "completed_stations": completed,
        "completed_committed_stations": completed_committed,
        "recyclable_weight": slip["recyclable_weight"],
        "mixed_weight": slip["mixed_weight"],
    }


def _station_report(bins, route, assignment, station_rules):
    report = []
    completed = 0
    for station_id in route:
        at_station = [b for b in bins if b.station_id == station_id]
        if not at_station:
            continue
        loaded = [b for b in at_station if assignment.get(b.bin_id) is not None]
        left = [b for b in at_station if assignment.get(b.bin_id) is None]
        rule = station_rules.get(station_id, {})
        max_weight = rule.get("max_weight")
        allowed = rule.get("allowed_grades")
        loaded_weight = sum(b.weight for b in loaded)
        notes: list[str] = []
        ok = True
        if max_weight is not None and loaded_weight > max_weight:
            ok = False
            notes.append(f"loaded {loaded_weight} exceeds station max {max_weight}")
        if allowed is not None:
            bad = [b.bin_id for b in loaded if b.grade not in allowed]
            if bad:
                ok = False
                notes.append("grade not allowed at station: " + ",".join(bad))
        complete = not left and ok
        if complete:
            completed += 1
        report.append(
            {
                "station_id": station_id,
                "bins_total": [b.bin_id for b in at_station],
                "bins_collected": [b.bin_id for b in loaded],
                "bins_left": [b.bin_id for b in left],
                "collected_weight": loaded_weight,
                "completed": complete,
                "rule_notes": notes,
            }
        )
    return report, completed


# ---------------------------------------------------------------------------
# Method A: depot-fixed assignments / clauses
# ---------------------------------------------------------------------------


def plan_with_partition(
    bins: Sequence[Bin],
    route: Sequence[str],
    compartments: Sequence[Compartment],
    assignments: Sequence[dict[str, Any]],
    *,
    station_rules: dict[str, dict[str, Any]] | None = None,
    committed_stations: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Follow depot-issued per-bin compartment terms offline.

    Each term is ``{bin_id, compartment_id, fallback?, issued_grade?}``.  The
    primary compartment is tried, then the fallback; if neither can hold the
    bin it is left (and a mandatory bin makes the plan infeasible).
    ``reissue_needed`` lists terms that no longer match the bin's current grade
    -- e.g. a term issued for wet-mixed after a late drying certificate turns
    the bin into clean paper.
    """
    comp_lookup = {c.compartment_id: c for c in compartments}
    bin_by_id = {b.bin_id: b for b in bins}
    loads = {c.compartment_id: 0 for c in compartments}
    assignment: dict[str, str | None] = {}
    decisions: list[dict[str, Any]] = []
    reissue: list[dict[str, Any]] = []

    for term in assignments:
        bin_id = term["bin_id"]
        if bin_id not in bin_by_id:
            raise ValueError(f"term for unknown bin {bin_id}")
        bin = bin_by_id[bin_id]
        chosen = None
        reason = "left: no compartment clause"
        for label, comp_id in (("primary", term.get("compartment_id")),
                               ("fallback", term.get("fallback"))):
            if comp_id is None:
                continue
            comp = comp_lookup.get(comp_id)
            if comp is None:
                reason = f"left: unknown compartment {comp_id}"
                continue
            if not _can_hold(comp, bin.grade):
                reason = f"left: {comp_id} does not accept {bin.grade}"
                continue
            if loads[comp_id] + bin.weight > comp.capacity:
                reason = f"left: {comp_id} would exceed capacity"
                continue
            chosen = comp_id
            reason = f"loaded via {label}"
            loads[comp_id] += bin.weight
            break
        assignment[bin_id] = chosen
        decisions.append(
            {
                "bin_id": bin_id,
                "compartment_id": chosen,
                "grade": bin.grade,
                "weight": bin.weight,
                "reason": reason,
            }
        )

        # Re-issue whenever the issued grade no longer matches, or when the
        # issued compartment(s) cannot hold the current grade at all.
        issued_grade = term.get("issued_grade")
        if issued_grade is not None and issued_grade != bin.grade:
            reissue.append(
                {
                    "bin_id": bin_id,
                    "issued_grade": issued_grade,
                    "current_grade": bin.grade,
                    "issued_compartment": term.get("compartment_id"),
                    "why": "issued grade changed",
                }
            )
        else:
            for comp_id in (term.get("compartment_id"), term.get("fallback")):
                comp = comp_lookup.get(comp_id) if comp_id else None
                if comp is not None and not _can_hold(comp, bin.grade):
                    reissue.append(
                        {
                            "bin_id": bin_id,
                            "issued_grade": issued_grade,
                            "current_grade": bin.grade,
                            "issued_compartment": comp_id,
                            "why": "issued compartment does not accept current grade",
                        }
                    )

    termed = {t["bin_id"] for t in assignments}
    for b in bins:
        if b.bin_id not in termed:
            assignment[b.bin_id] = None

    report = simulate(
        bins,
        route,
        compartments,
        assignment,
        station_rules=station_rules,
        committed_stations=committed_stations,
    )
    report["decisions"] = decisions
    report["unassigned_bins"] = [b.bin_id for b in bins if b.bin_id not in termed]
    report["reissue_needed"] = reissue
    report["method"] = "depot_partition"
    return report


# ---------------------------------------------------------------------------
# Method B: late co-loading optimisation
# ---------------------------------------------------------------------------


def plan_late(
    bins: Sequence[Bin],
    route: Sequence[str],
    compartments: Sequence[Compartment],
    *,
    station_rules: dict[str, dict[str, Any]] | None = None,
    committed_stations: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Choose co-loading to maximise committed stations, then recyclable weight.

    Exhaustive over each bin's options (a compatible compartment, or -- for
    non-mandatory bins only -- leaving it).  Bounded by the operational limits
    of 8 bins / 3 compartments.
    """
    station_rules = station_rules or {}
    route = list(route)
    committed = set(committed_stations) if committed_stations is not None else set(route)
    comps = list(compartments)

    order = sorted(
        range(len(bins)),
        key=lambda i: (
            route.index(bins[i].station_id) if bins[i].station_id in route else 99,
            bins[i].bin_id,
        ),
    )

    best: dict[str, Any] | None = None
    best_key: tuple | None = None
    loads = [0] * len(comps)
    choice: list[int | None] = [None] * len(bins)

    def key_of(report: dict[str, Any]) -> tuple:
        # Prefer dedicated compartments over mixing ones so mixing capacity is
        # preserved for bins that genuinely need it.
        mixing_bins = sum(
            1 for i, c in enumerate(choice) if c is not None and comps[c].mix_permitted
        )
        return (
            report["completed_committed_stations"],
            report["recyclable_weight"],
            -report["mixed_weight"],
            -mixing_bins,
            tuple(
                (bins[i].bin_id, -(choice[i] if choice[i] is not None else -1))
                for i in sorted(range(len(bins)), key=lambda k: bins[k].bin_id)
            ),
        )

    def evaluate() -> None:
        nonlocal best, best_key
        assignment = {
            bins[i].bin_id: (comps[c].compartment_id if c is not None else None)
            for i, c in enumerate(choice)
        }
        report = simulate(
            bins,
            route,
            comps,
            assignment,
            station_rules=station_rules,
            committed_stations=committed,
        )
        if not report["feasible"]:
            return
        k = key_of(report)
        if best is None or k > best_key:
            best, best_key = report, k

    def recurse(pos: int) -> None:
        if pos == len(order):
            evaluate()
            return
        bi = order[pos]
        b = bins[bi]
        if not b.mandatory:
            choice[bi] = None
            recurse(pos + 1)
        for ci, comp in enumerate(comps):
            if not _can_hold(comp, b.grade):
                continue
            if loads[ci] + b.weight > comp.capacity:
                continue
            choice[bi] = ci
            loads[ci] += b.weight
            recurse(pos + 1)
            loads[ci] -= b.weight
        choice[bi] = None

    recurse(0)
    if best is None:
        return {
            "method": "late_locked_partition",
            "feasible": False,
            "problems": ["no feasible loading"],
            "assignment": {},
        }
    best["method"] = "late_locked_partition"
    return best


# ---------------------------------------------------------------------------
# Reservation proof for late planning
# ---------------------------------------------------------------------------


def prove_early_capacity(
    bins: Sequence[Bin],
    route: Sequence[str],
    compartments: Sequence[Compartment],
    *,
    split_station: str,
    station_rules: dict[str, dict[str, Any]] | None = None,
    committed_stations: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Prove early-station freedom does not consume later promised capacity.

    Later stations (at or after *split_station*) are loaded first, reserving
    their compartments' weight.  The early stations are then re-planned using
    only the residual capacity.  If they still reach their best possible
    committed-station count within the residual, the early freedom provably
    cannot take the later stations' promised compartments.
    """
    station_rules = station_rules or {}
    route = list(route)
    if split_station not in route:
        raise ValueError("split_station must be on the route")
    idx = route.index(split_station)
    early_stations = route[:idx]
    later_stations = route[idx:]
    committed = set(committed_stations) if committed_stations is not None else set(route)

    later_bins = [b for b in bins if b.station_id in later_stations]
    early_bins = [b for b in bins if b.station_id in early_stations]

    later_plan = plan_late(
        later_bins,
        later_stations,
        compartments,
        station_rules=station_rules,
        committed_stations=[s for s in committed if s in later_stations],
    )
    if not later_plan.get("feasible", False):
        return {"proved": False, "why": "later stations cannot be loaded", "later_plan": later_plan}

    reserved = {c.compartment_id: 0 for c in compartments}
    for comp in later_plan["compartments"]:
        reserved[comp["compartment_id"]] = comp["weight"]

    residual = [
        Compartment(
            c.compartment_id,
            c.capacity - reserved[c.compartment_id],
            c.accepts,
            c.mix_permitted,
        )
        for c in compartments
    ]
    early_residual = plan_late(
        early_bins,
        early_stations,
        residual,
        station_rules=station_rules,
        committed_stations=[s for s in committed if s in early_stations],
    )
    early_unconstrained = plan_late(
        early_bins,
        early_stations,
        compartments,
        station_rules=station_rules,
        committed_stations=[s for s in committed if s in early_stations],
    )

    achieved = early_residual.get("completed_committed_stations", 0)
    possible = early_unconstrained.get("completed_committed_stations", 0)
    ledger = [
        {
            "compartment_id": c.compartment_id,
            "capacity": c.capacity,
            "reserved_for_later": reserved[c.compartment_id],
            "free_for_early": c.capacity - reserved[c.compartment_id],
            "early_used": next(
                (
                    comp["weight"]
                    for comp in early_residual.get("compartments", [])
                    if comp["compartment_id"] == c.compartment_id
                ),
                0,
            ),
        }
        for c in compartments
    ]
    return {
        "proved": early_residual.get("feasible", False) and achieved == possible,
        "split_station": split_station,
        "early_stations": early_stations,
        "later_stations": later_stations,
        "early_completed_committed": achieved,
        "early_best_possible": possible,
        "capacity_ledger": ledger,
        "later_plan": later_plan,
        "early_plan": early_residual,
    }
