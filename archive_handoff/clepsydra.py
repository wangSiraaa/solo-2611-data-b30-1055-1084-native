"""Schematic water-clock (漏刻) collation and exhibition reproduction.

Domain model
------------
A schematic clepsydra has one integer bucket and a fixed integer *scale line*
``scale``.  At every beat the bucket first receives an inflow: either an
integer in ``1 .. scale-1`` or a *stop-flow* marker (:data:`STOP`, no water at
all).  If the resulting level reaches the scale line the clock rings exactly
once and pours out exactly ``scale`` units; the remainder stays in the bucket.
A single-beat natural inflow is strictly smaller than the scale line, so the
water level is always in ``[0, scale)`` and "reaches the line" means
``level + inflow >= scale``.

Two concerns live in this module:

* **Collation** -- :func:`explain` decides whether a whole string of records
  can be *jointly* explained by one clock run.  Several copies (抄本) may
  witness the same watch (更); a mis-bound fragment (错接残页) may only be
  attached in one of its listed binding directions.  The result leaves the
  canonical initial value, the chosen binding direction per fragment, and the
  per-beat water amounts.

* **Performance** -- the exhibition stand has its own per-beat inflow table and
  only knows the opening water *bracket* (every integer value in one naked-eye
  bracket is indistinguishable to the audience).  :func:`plan_fixed_cups`
  searches a single fixed calibration-cup list that rings correctly under
  *every* allowed initial value; :func:`plan_adaptive` lets an operator open a
  read-scale gate at allowed beats and adapt.  A beat on which the original
  book still disagrees is *disclosed*, never asserted as history.

Observation convention
----------------------
An observation is the water level read at the *start* of a beat, i.e. *before*
that beat's inflow.  Hence an observation at beat 0 is exactly the opening
water level -- what an opening calibration cup reads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

#: Inflow marker meaning "no water this beat" (a stop-flow record).
STOP = 0


# ---------------------------------------------------------------------------
# Single run
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Beat:
    """Outcome of one beat of a clock run."""

    index: int
    before: int        # water level read at the start of the beat
    inflow: int        # water added this beat (natural inflow + any cup)
    ring: bool         # did the clock ring this beat?
    after: int         # water level after the ring/pour, carried to next beat


def run_clock(scale: int, opening: int, inflows: Iterable[int]) -> list[Beat]:
    """Run one clock.

    ``opening`` is the level at the start of beat 0 and must be in
    ``[0, scale)``.  Each inflow must be in ``[0, scale)`` (``0`` == stop).
    """
    if scale < 1:
        raise ValueError("scale must be >= 1")
    if not (0 <= opening < scale):
        raise ValueError(f"opening {opening} outside [0,{scale})")
    level = opening
    out: list[Beat] = []
    for i, inf in enumerate(inflows):
        if not (0 <= inf < scale):
            raise ValueError(f"inflow {inf} at beat {i} outside [0,{scale})")
        before = level
        total = level + inf
        ring = total >= scale
        after = total - scale if ring else total
        out.append(Beat(i, before, inf, ring, after))
        level = after
    return out


def ring_pattern(scale: int, opening: int, inflows: Iterable[int]) -> tuple[bool, ...]:
    """Just the ring/no-ring sequence of a run."""
    return tuple(b.ring for b in run_clock(scale, opening, inflows))


# ---------------------------------------------------------------------------
# Records: entries, witnesses, binding directions
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Entry:
    """One transcribed beat of a witness, indexed by its *local* position.

    ``inflow``  -- a STOP marker or an integer inflow, or ``None`` if silent.
    ``ring``    -- claimed ring (True), confirmed silent (False) or ``None``.
    ``obs``     -- allowed water-level readings at the *start* of the beat
                   (empty tuple == no observation).
    """

    inflow: Optional[int] = None
    ring: Optional[bool] = None
    obs: tuple[int, ...] = ()


@dataclass
class Witness:
    """A copy (抄本) or a mis-bound fragment (残页).

    ``entries`` are in local order.  ``directions`` is a list of possible
    bindings; each binding is a dict mapping *local* index -> *absolute* beat.
    A normal full copy has exactly one identity direction (see
    :func:`full_direction`).  ``initial_span`` optionally states the range of
    the opening water level (only meaningful when the witness binds beat 0).
    """

    name: str
    entries: list[Entry]
    directions: list[dict[int, int]]
    initial_span: Optional[tuple[int, int]] = None


def full_direction(n: int) -> dict[int, int]:
    """Identity binding for a full ``n``-beat copy."""
    return {i: i for i in range(n)}


def forward_direction(n: int, start: int) -> dict[int, int]:
    """Bind a fragment's local beats forward from ``start``."""
    return {i: start + i for i in range(n)}


def reversed_direction(n: int, start: int) -> dict[int, int]:
    """Bind a fragment's local beats *backward* from ``start`` (reversed)."""
    return {i: start - i for i in range(n)}


def _validate_direction(direction: dict[int, int], beats: int) -> bool:
    vals = list(direction.values())
    if len(set(vals)) != len(vals):
        return False
    return all(0 <= v < beats for v in vals)


# ---------------------------------------------------------------------------
# Collation solver
# ---------------------------------------------------------------------------
@dataclass
class Solution:
    """One canonical joint explanation."""

    opening: int
    directions: dict[str, dict[int, int]]   # witness name -> chosen direction
    inflows: tuple[int, ...]                # one per absolute beat
    rings: tuple[bool, ...]

    def as_dict(self) -> dict:
        return {
            "opening": self.opening,
            "directions": {k: dict(sorted(v.items())) for k, v in sorted(self.directions.items())},
            "inflows": list(self.inflows),
            "rings": list(self.rings),
        }


class _Constraints:
    """Per-absolute-beat constraints gathered from all bound witnesses."""

    def __init__(self, beats: int, scale: int):
        self.beats = beats
        self.scale = scale
        self.inflow: dict[int, int] = {}
        self.ring: dict[int, bool] = {}
        self.obs: dict[int, frozenset[int]] = {}
        self.span: Optional[tuple[int, int]] = None
        self.ok = True

    def _clamp_span(self, lo: int, hi: int) -> None:
        lo = max(lo, 0)
        hi = min(hi, self.scale - 1)
        if lo > hi:
            self.ok = False
        elif self.span is None:
            self.span = (lo, hi)
        else:
            nlo = max(self.span[0], lo)
            nhi = min(self.span[1], hi)
            if nlo > nhi:
                self.ok = False
            else:
                self.span = (nlo, nhi)

    def add_witness(self, w: Witness, direction: dict[int, int]) -> None:
        for local, entry in enumerate(w.entries):
            if local not in direction:
                continue
            absb = direction[local]
            if not (0 <= absb < self.beats):
                self.ok = False
                return
            if entry.inflow is not None:
                if absb in self.inflow and self.inflow[absb] != entry.inflow:
                    self.ok = False
                    return
                self.inflow[absb] = entry.inflow
            if entry.ring is not None:
                if absb in self.ring and self.ring[absb] != entry.ring:
                    self.ok = False
                    return
                self.ring[absb] = entry.ring
            if entry.obs:
                allowed = frozenset(entry.obs)
                if absb in self.obs:
                    allowed = self.obs[absb] & allowed
                    if not allowed:
                        self.ok = False
                        return
                self.obs[absb] = allowed
        if w.initial_span is not None:
            self._clamp_span(*w.initial_span)


def _witness_directions(w: Witness, beats: int) -> list[dict[int, int]]:
    out = [d for d in w.directions if _validate_direction(d, beats)]
    return out


def _opening_range(c: _Constraints) -> range:
    if c.span is None:
        return range(0, c.scale)
    return range(c.span[0], c.span[1] + 1)


def _dfs_openings(c: _Constraints, opening: int, first_only: bool) -> Iterable[Solution]:
    beats = c.beats
    scale = c.scale

    def bounds(beat: int, level: int) -> tuple[int, int]:
        lo, hi = 0, scale - 1
        if beat in c.ring:
            if c.ring[beat]:
                lo = max(lo, scale - level)
            else:
                hi = min(hi, scale - level - 1)
        return lo, hi

    inflows: list[int] = []

    def dfs(beat: int, level: int):
        if beat == beats:
            rings = _derive_rings(scale, opening, inflows)
            return [Solution(opening, {}, tuple(inflows), rings)]
        if beat in c.obs and level not in c.obs[beat]:
            return []
        if beat in c.inflow:
            cand: Iterable[int] = (c.inflow[beat],)
        else:
            lo, hi = bounds(beat, level)
            if lo > hi:
                return []
            cand = range(lo, hi + 1)
        results = []
        for inf in cand:
            total = level + inf
            ring = total >= scale
            if beat in c.ring and ring != c.ring[beat]:
                continue
            after = total - scale if ring else total
            inflows.append(inf)
            results.extend(dfs(beat + 1, after))
            inflows.pop()
            if first_only and results:
                return results
        return results

    for sol in dfs(0, opening):
        yield sol


def _derive_rings(scale: int, opening: int, inflows: list[int]) -> tuple[bool, ...]:
    level = opening
    rings = []
    for inf in inflows:
        total = level + inf
        ring = total >= scale
        level = total - scale if ring else total
        rings.append(ring)
    return tuple(rings)


def _iter_constraints(scale: int, beats: int, witnesses: list[Witness]):
    per = []
    for w in witnesses:
        dirs = _witness_directions(w, beats)
        if not dirs:
            return
        per.append(dirs)

    def rec(i: int, acc: dict[str, dict[int, int]]):
        if i == len(witnesses):
            c = _Constraints(beats, scale)
            for w in witnesses:
                c.add_witness(w, acc[w.name])
            yield dict(acc), c
            return
        w = witnesses[i]
        for d in per[i]:
            acc[w.name] = d
            yield from rec(i + 1, acc)
            del acc[w.name]

    yield from rec(0, {})


def explain(
    scale: int,
    beats: int,
    witnesses: list[Witness],
    *,
    first_only: bool = True,
) -> Optional[Solution]:
    """Find a canonical joint explanation, or ``None`` if none exists.

    Enumeration order (for the canonical pick) is: the witnesses' listed
    binding directions in order, then opening ascending, then per-beat inflow
    ascending.  A mis-bound fragment therefore resolves to its first listed
    direction that makes the whole string consistent.
    """
    for choice, c in _iter_constraints(scale, beats, witnesses):
        if not c.ok:
            continue
        for opening in _opening_range(c):
            for sol in _dfs_openings(c, opening, first_only):
                sol.directions = choice
                return sol
    return None


def possible_ring_patterns(
    scale: int, beats: int, witnesses: list[Witness], *, limit: int = 10000
) -> set[tuple[bool, ...]]:
    """All distinct ring patterns reachable by a joint explanation."""
    patterns: set[tuple[bool, ...]] = set()
    for _choice, c in _iter_constraints(scale, beats, witnesses):
        if not c.ok:
            continue
        for opening in _opening_range(c):
            for sol in _dfs_openings(c, opening, first_only=False):
                patterns.add(sol.rings)
                if len(patterns) > limit:
                    return patterns
    return patterns


def determined_beats(patterns: set[tuple[bool, ...]], beats: int) -> tuple[dict[int, bool], set[int]]:
    """Split beats into determined (ring fixed) and disputed (ring unsettled)."""
    determined: dict[int, bool] = {}
    disputed: set[int] = set()
    if not patterns:
        return determined, set(range(beats))
    for b in range(beats):
        vals = {p[b] for p in patterns}
        if len(vals) == 1:
            determined[b] = next(iter(vals))
        else:
            disputed.add(b)
    return determined, disputed


# ---------------------------------------------------------------------------
# Performance: the exhibition stand
# ---------------------------------------------------------------------------
def run_stand(
    scale: int,
    opening: int,
    stand_inflows: Iterable[int],
    cup_beats: Iterable[int] = (),
    gate_beats: Iterable[int] = (),
) -> list[Beat]:
    """Run the stand: natural inflow, plus a +1 cup, minus a read-scale gate.

    * A cup at beat ``b`` adds one unit *before* the natural inflow.
    * A read-scale gate at beat ``b`` stops that beat's natural inflow entirely
      (``STOP``) so the operator can read the level; the same beat cannot also
      take a cup.
    * At most one ring per beat (a single add then a single pour).
    """
    cup = set(cup_beats)
    gate = set(gate_beats)
    if cup & gate:
        raise ValueError(f"a gate and a cup cannot share a beat: {sorted(cup & gate)}")
    inflows = list(stand_inflows)
    level = opening
    out: list[Beat] = []
    for i, nat in enumerate(inflows):
        if not (0 <= nat < scale):
            raise ValueError(f"stand inflow {nat} at beat {i} outside [0,{scale})")
        if i in gate:
            total_in = 0
        else:
            total_in = nat + (1 if i in cup else 0)
        before = level
        total = level + total_in
        ring = total >= scale
        after = total - scale if ring else total
        out.append(Beat(i, before, total_in, ring, after))
        level = after
    return out


def evaluate_pattern(
    rings: tuple[bool, ...], target: dict[int, bool], disputed: Iterable[int]
) -> bool:
    """True iff every *determined* target beat matches ``rings``."""
    disputed = set(disputed)
    for b, want in target.items():
        if b in disputed:
            continue
        if b >= len(rings) or rings[b] != want:
            return False
    return True


@dataclass
class ShowPlan:
    feasible: bool
    cup_beats: tuple[int, ...] = ()
    paths: list[dict] = field(default_factory=list)
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "feasible": self.feasible,
            "cup_beats": list(self.cup_beats),
            "paths": self.paths,
            "note": self.note,
        }


def plan_fixed_cups(
    scale: int,
    stand_inflows: Iterable[int],
    openings: Iterable[int],
    target: dict[int, bool],
    disputed: Iterable[int] = (),
    allowed_cups: Iterable[int] = (),
    max_cups: Optional[int] = None,
    *,
    require_dispute_free: bool = True,
) -> ShowPlan:
    """Search one fixed cup list that rings correctly for *all* openings.

    The cup list is common to every opening in the bracket ("同档所有初值必须
    使用同一段固定杯单").  ``require_dispute_free`` refuses a plan that would
    have to assert a still-disputed beat.
    """
    stand = list(stand_inflows)
    openings = list(openings)
    disputed = set(disputed)
    if require_dispute_free and disputed:
        return ShowPlan(False, note=f"cannot assert disputed beats {sorted(disputed)}")
    allowed = sorted(set(allowed_cups))
    limit = len(allowed) if max_cups is None else min(len(allowed), max_cups)

    # Enumerate subsets by increasing size then by mask (deterministic).
    import itertools

    for size in range(0, limit + 1):
        for combo in itertools.combinations(allowed, size):
            cup_beats = tuple(combo)
            paths = []
            ok = True
            for opening in openings:
                beats = run_stand(scale, opening, stand, cup_beats=cup_beats)
                rings = tuple(b.ring for b in beats)
                if not evaluate_pattern(rings, target, disputed):
                    ok = False
                    break
                paths.append({
                    "opening": opening,
                    "rings": list(rings),
                    "cups_used": list(cup_beats),
                    "levels_before": [b.before for b in beats],
                    "levels_after": [b.after for b in beats],
                })
            if ok:
                return ShowPlan(True, cup_beats=cup_beats, paths=paths,
                                note="single fixed cup list rings for all openings")
    return ShowPlan(False, note="no single fixed cup list rings for every opening")


# -- read-scale gate analysis -------------------------------------------------
def gate_observation(
    scale: int, opening: int, stand_inflows: Iterable[int], gate_beats: Iterable[int]
) -> list[Optional[int]]:
    """Level read at each beat: the value at a gate beat, else ``None``."""
    beats = run_stand(scale, opening, stand_inflows, gate_beats=gate_beats)
    gate = set(gate_beats)
    return [b.before if b.index in gate else None for b in beats]


def earliest_discerning_beat(
    scale: int, openings: Iterable[int], stand_inflows: Iterable[int]
) -> tuple[int, str]:
    """First beat whose *ring outcome* separates two openings (no actions).

    Returns ``(beat, kind)`` or ``(-1, "never")``.  Used to show a witness for
    which a safe action must wait an extra beat.
    """
    openings = list(openings)
    patterns = [ring_pattern(scale, o, stand_inflows) for o in openings]
    if len(set(patterns)) > 1:
        n = len(patterns[0])
        for b in range(n):
            if len({p[b] for p in patterns}) > 1:
                return b, "ring"
    # fall back to level divergence
    runs = [run_clock(scale, o, stand_inflows) for o in openings]
    n = len(runs[0])
    for b in range(n):
        if len({r[b].before for r in runs}) > 1:
            return b, "level"
    return -1, "never"


# -- adaptive gate + cup planner ---------------------------------------------
def plan_adaptive(
    scale: int,
    stand_inflows: Iterable[int],
    openings: Iterable[int],
    target: dict[int, bool],
    disputed: Iterable[int] = (),
    allowed_gates: Iterable[int] = (),
    allowed_cups: Iterable[int] = (),
    max_cups: Optional[int] = None,
    *,
    require_dispute_free: bool = True,
) -> dict:
    """Plan a show with an operator who may open a read-scale gate and adapt.

    Belief state = set of ``(opening, level)`` still possible.  At each beat the
    operator may: do nothing (natural inflow), give a cup (natural + 1, allowed
    once per listed beat, count limited), or open a gate (stop that beat's
    inflow, read the level, branch on the reading).  Returns a decision tree.
    """
    stand = list(stand_inflows)
    openings = sorted(set(openings))
    disputed = set(disputed)
    gates = set(allowed_gates)
    cups = set(allowed_cups)
    budget = len(cups) if max_cups is None else max_cups

    if require_dispute_free and disputed:
        return {"feasible": False,
                "note": f"cannot assert disputed beats {sorted(disputed)}"}

    beats_n = len(stand)

    # belief: tuple of (opening, level) sorted
    def step(belief, beat, action):
        """Return (new_belief, ring_set, readings) for one action."""
        readings = {}
        nxt = {}
        rings = set()
        for opening, level in belief:
            if action == "gate":
                total_in = 0
            elif action == "cup":
                total_in = stand[beat] + 1
            else:
                total_in = stand[beat]
            total = level + total_in
            ring = total >= scale
            after = total - scale if ring else total
            rings.add(ring)
            key = level if action == "gate" else None
            nxt[(opening, after)] = nxt.get((opening, after), 0)
            if action == "gate":
                readings.setdefault(level, []).append((opening, after))
        new_belief = tuple(sorted(nxt))
        return new_belief, rings, readings

    def violations(rings, beat):
        if beat in target and beat not in disputed:
            want = target[beat]
            if any(r != want for r in rings):
                return True
        return False

    memo = {}

    def solve(beat, belief, cups_used):
        key = (beat, belief, cups_used)
        if key in memo:
            return memo[key]
        if beat == beats_n:
            return {"beat": beat, "terminal": True}
        actions = ["none"]
        if beat in cups and cups_used < budget:
            actions.append("cup")
        if beat in gates:
            actions.append("gate")
        for action in actions:
            new_belief, rings, readings = step(belief, beat, action)
            if violations(rings, beat):
                continue
            if action == "gate":
                # branch on the read level; each branch solves independently
                branches = {}
                ok = True
                for lvl, members in sorted(readings.items()):
                    sub = solve(beat + 1, tuple(sorted(members)), cups_used)
                    if sub is None:
                        ok = False
                        break
                    branches[lvl] = sub
                if not ok:
                    continue
                node = {"beat": beat, "action": "gate",
                        "rings": sorted(rings), "branches": branches}
                memo[key] = node
                return node
            sub = solve(beat + 1, new_belief,
                        cups_used + (1 if action == "cup" else 0))
            if sub is None:
                continue
            node = {"beat": beat, "action": action,
                    "rings": sorted(rings), "next": sub}
            memo[key] = node
            return node
        memo[key] = None
        return None

    start = tuple(sorted((o, o) for o in openings))
    tree = solve(0, start, 0)
    if tree is None:
        return {"feasible": False, "note": "no adaptive gate/cup policy works"}
    return {"feasible": True, "tree": tree,
            "note": "adaptive read-scale gate / cup policy"}


def flatten_policy(policy: dict, prefix: Optional[list] = None) -> list[list[dict]]:
    """Expand an adaptive decision tree into leaf paths (actions per opening)."""
    prefix = prefix or []

    def walk(node):
        if node.get("terminal"):
            return [[]]
        beat = node["beat"]
        if node["action"] == "gate":
            out = []
            for lvl, branch in sorted(node["branches"].items()):
                for tail in walk(branch):
                    out.append([{"beat": beat, "action": "gate", "read": lvl}] + tail)
            return out
        out = []
        for tail in walk(node["next"]):
            out.append([{"beat": beat, "action": node["action"]}] + tail)
        return out

    return walk(policy)
