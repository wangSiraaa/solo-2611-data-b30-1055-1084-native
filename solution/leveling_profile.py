"""Leveling network -> road profile.

The survey crew hands over a set of *level setups*.  Each setup is one
instrument standing: every stake read from that setup shares an arbitrary
local zero, so only the *differences* between readings of the same setup are
facts.  A reading is a height-signed backsight/foresight-style value ``r`` on
a stake; for two stakes ``a, b`` read from setup ``s`` the hard constraint is

    elev(b) - elev(a) = r_b - r_a

The setup's own zero cancels and is never an elevation.  This module never
treats a raw reading as a height.

Exactly one *benchmark* stake has a given elevation.  The network is a
difference-constraint graph:

* the component containing the benchmark gets absolute elevations;
* a component disconnected from the benchmark keeps only *relative*
  elevations.  Its absolute level is unknown and is **never assumed zero**;
* a loop whose alternative paths disagree is a *misclosure*: the two
  conflicting records are named, and no single-valued profile is published
  for that component until the conflict is resolved.

A second, independent fact layer is the *walked coverage*: the merged
intervals the crew actually walked and measured.  Linear interpolation is
allowed only between two adjacent measured stakes that lie in the same
walked interval.  A gap that nobody walked cannot be bridged by
interpolation.

All arithmetic is exact :class:`fractions.Fraction`; nothing here does
least-squares adjustment or invents a missing measurement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Iterable, Optional, Sequence

from chainage_catalog import Interval, as_fraction, merge_intervals


# ---------------------------------------------------------------------------
# Input records
# ---------------------------------------------------------------------------


class LevelingError(ValueError):
    """Base error for leveling network construction."""


@dataclass(frozen=True)
class Stake:
    """A named survey stake at a fixed horizontal coordinate."""

    stake_id: str
    x: Fraction

    def __post_init__(self):
        object.__setattr__(self, "x", as_fraction(self.x))


@dataclass(frozen=True)
class Reading:
    """One stake read from one setup. Readings are arbitrary-zero values."""

    stake_id: str
    reading: Fraction

    def __post_init__(self):
        object.__setattr__(self, "reading", as_fraction(self.reading))


@dataclass(frozen=True)
class LevelSetup:
    """One instrument standing with its own arbitrary zero.

    Only *differences* between the readings of one setup are facts.  A setup
    must read at least two stakes, otherwise it determines no difference.
    """

    setup_id: str
    readings: tuple[Reading, ...]

    def __post_init__(self):
        if len(self.readings) < 2:
            raise LevelingError(
                f"setup {self.setup_id!r} must read at least two stakes"
            )
        ids = [r.stake_id for r in self.readings]
        if len(set(ids)) != len(ids):
            raise LevelingError(
                f"setup {self.setup_id!r} reads a stake more than once"
            )

    def read_stakes(self) -> frozenset[str]:
        return frozenset(r.stake_id for r in self.readings)

    def reading_of(self, stake_id: str) -> Optional[Fraction]:
        for r in self.readings:
            if r.stake_id == stake_id:
                return r.reading
        return None


@dataclass(frozen=True)
class Benchmark:
    """The single stake whose absolute elevation is given."""

    stake_id: str
    elevation: Fraction

    def __post_init__(self):
        object.__setattr__(self, "elevation", as_fraction(self.elevation))


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Misclosure:
    """A loop whose two paths predict different elevations for one stake.

    The new edge (setup ``setup_id`` between ``stake_a`` and ``stake_b``)
    implies ``implied_elevation`` for ``stake_b`` while the already-walked
    path fixes it at ``previous_elevation``.  The signed gap is
    ``misclosure``.  Both records are named so the crew can re-observe them.
    """

    stake_id: str
    setup_id: str
    stake_a: str
    stake_b: str
    previous_elevation: Fraction
    implied_elevation: Fraction

    @property
    def misclosure(self) -> Fraction:
        return self.implied_elevation - self.previous_elevation

    def describe(self) -> str:
        return (
            f"misclosure at stake {self.stake_id}: setup {self.setup_id} "
            f"({self.stake_a}->{self.stake_b}) implies "
            f"{self._f(self.implied_elevation)} but the earlier path fixes "
            f"{self._f(self.previous_elevation)} "
            f"(gap {self._f(self.misclosure)})"
        )

    @staticmethod
    def _f(v: Fraction) -> str:
        return str(v.numerator) if v.denominator == 1 else f"{v.numerator}/{v.denominator}"


# ---------------------------------------------------------------------------
# Difference-constraint graph
# ---------------------------------------------------------------------------


@dataclass
class _Edge:
    setup_id: str
    other: str
    # Elevation increment along cur -> other.  The task's reading convention
    # is height-signed: within one setup the reading differences equal the
    # elevation differences, so
    #   elev(other) - elev(cur) = r_other - r_cur
    # (e.g. readings 4 then 5 on stakes 0 then 1 give elev 0 then 1 once the
    # benchmark fixes stake 0 at 0).  The setup's arbitrary zero cancels in
    # the difference; only differences are ever used.
    delta: Fraction


class LevelingNetwork:
    """Builds stake elevations from setup differences and one benchmark."""

    def __init__(
        self,
        stakes: Sequence[Stake],
        setups: Sequence[LevelSetup],
        benchmark: Benchmark,
    ):
        self.stakes = tuple(stakes)
        self.setups = tuple(setups)
        self.benchmark = benchmark
        self._by_id: dict[str, Stake] = {}
        self._adj: dict[str, list[_Edge]] = {}
        self._validate_and_build()
        self._misclosures: list[Misclosure] = []
        self._components: list[list[str]] = []
        self._relative: dict[str, dict[str, Fraction]] = {}
        self._absolute: dict[str, Fraction] = {}
        self._anchored: set[str] = set()
        self._solve()

    # -- construction -------------------------------------------------
    def _validate_and_build(self) -> None:
        for st in self.stakes:
            if st.stake_id in self._by_id:
                raise LevelingError(f"duplicate stake id {st.stake_id!r}")
            self._by_id[st.stake_id] = st
            self._adj[st.stake_id] = []
        if self.benchmark.stake_id not in self._by_id:
            raise LevelingError(
                f"benchmark stake {self.benchmark.stake_id!r} is not a known stake"
            )
        for setup in self.setups:
            known = setup.read_stakes()
            unknown = sorted(known - set(self._by_id))
            if unknown:
                raise LevelingError(
                    f"setup {setup.setup_id!r} reads unknown stakes {unknown}"
                )
            # Every reading pair of a setup is a difference constraint.  Keep
            # one edge anchored on the first reading plus edges to it, so the
            # arbitrary local zero cancels exactly.
            base = setup.readings[0]
            for rd in setup.readings[1:]:
                # Height-signed readings: elev(rd) - elev(base) = r_rd - r_base
                d = rd.reading - base.reading
                self._adj[base.stake_id].append(
                    _Edge(setup.setup_id, rd.stake_id, d)
                )
                self._adj[rd.stake_id].append(
                    _Edge(setup.setup_id, base.stake_id, -d)
                )

    # -- solving ------------------------------------------------------
    def _solve(self) -> None:
        visited: set[str] = set()
        for root in self._by_id:
            if root in visited:
                continue
            comp, rel = self._walk_component(root)
            visited |= comp
            self._components.append(sorted(comp))
            self._relative[root] = rel
            if self.benchmark.stake_id in comp:
                self._anchor_component(root, rel)

    def _walk_component(
        self, root: str
    ) -> tuple[set[str], dict[str, Fraction]]:
        """BFS in the component; records every loop disagreement."""
        rel: dict[str, Fraction] = {root: Fraction(0)}
        queue = [root]
        comp: set[str] = {root}
        while queue:
            cur = queue.pop(0)
            for edge in self._adj[cur]:
                # edge.delta = elev(other) - elev(cur)
                implied = rel[cur] + edge.delta
                if edge.other not in rel:
                    rel[edge.other] = implied
                    comp.add(edge.other)
                    queue.append(edge.other)
                elif rel[edge.other] != implied:
                    self._misclosures.append(
                        Misclosure(
                            stake_id=edge.other,
                            setup_id=edge.setup_id,
                            stake_a=cur,
                            stake_b=edge.other,
                            previous_elevation=rel[edge.other],
                            implied_elevation=implied,
                        )
                    )
        return comp, rel

    def _anchor_component(self, root: str, rel: dict[str, Fraction]) -> None:
        shift = self.benchmark.elevation - rel[self.benchmark.stake_id]
        for stake_id, local in rel.items():
            self._absolute[stake_id] = local + shift
            self._anchored.add(stake_id)

    # -- results ------------------------------------------------------
    @property
    def misclosures(self) -> tuple[Misclosure, ...]:
        # De-duplicate identical reports while preserving first-seen order.
        seen: set[tuple] = set()
        out: list[Misclosure] = []
        for m in self._misclosures:
            key = (
                m.stake_id,
                m.setup_id,
                m.stake_a,
                m.stake_b,
                m.previous_elevation,
                m.implied_elevation,
            )
            if key not in seen:
                seen.add(key)
                out.append(m)
        return tuple(out)

    def has_conflict(self) -> bool:
        return bool(self._misclosures)

    def component_of(self, stake_id: str) -> tuple[str, ...]:
        for comp in self._components:
            if stake_id in comp:
                return tuple(comp)
        raise KeyError(stake_id)

    def is_anchored(self, stake_id: str) -> bool:
        return stake_id in self._anchored

    def elevation(self, stake_id: str) -> Optional[Fraction]:
        """Absolute elevation, or ``None`` when disconnected from benchmark."""
        return self._absolute.get(stake_id)

    def relative_elevation(self, root: str, stake_id: str) -> Fraction:
        """Elevation of ``stake_id`` expressed in ``root``'s local zero."""
        for comp_root, rel in self._relative.items():
            if root in rel and stake_id in rel:
                return rel[stake_id] - rel[root]
        raise LevelingError(
            f"stakes {root!r} and {stake_id!r} are not in one component"
        )

    def profile_points(self) -> list[dict]:
        """All stakes with solved elevations, sorted by horizontal coordinate.

        Conflicted components are omitted (no single elevation exists) and
        disconnected stakes report ``elevation=None`` rather than a fabricated
        zero.
        """
        conflicted = {m.stake_id for m in self.misclosures}
        out: list[dict] = []
        for st in sorted(self.stakes, key=lambda s: s.x):
            if st.stake_id in conflicted:
                continue
            out.append(
                {
                    "stake_id": st.stake_id,
                    "x": st.x,
                    "elevation": self.elevation(st.stake_id),
                    "anchored": self.is_anchored(st.stake_id),
                }
            )
        return out


# ---------------------------------------------------------------------------
# Walked coverage + interpolated ground profile
# ---------------------------------------------------------------------------


class GroundProfile:
    """Measured ground polyline, restricted to the intervals actually walked.

    ``measured`` maps a stake id to its absolute elevation.  Stakes unknown
    to the network or disconnected from the benchmark are simply absent: the
    profile never assigns them a height.
    """

    def __init__(
        self,
        network: LevelingNetwork,
        coverage: Iterable[Interval],
    ):
        if network.has_conflict():
            raise LevelingError(
                "cannot publish a profile while misclosures are unresolved: "
                + "; ".join(m.describe() for m in network.misclosures)
            )
        self.network = network
        self.coverage = tuple(merge_intervals(coverage))
        self._points: dict[str, tuple[Fraction, Fraction]] = {}
        for st in network.stakes:
            elev = network.elevation(st.stake_id)
            if elev is not None:
                self._points[st.stake_id] = (st.x, elev)
        self._ordered = sorted(self._points.values(), key=lambda p: p[0])

    # -- coverage test ------------------------------------------------
    def _cover_index(self, x: Fraction) -> Optional[int]:
        """Index of the walked half-open interval containing x, else None.

        A point exactly on an interval's exclusive right end is not a member
        of that interval even if the next interval happens to start there.
        """
        x = as_fraction(x)
        for i, iv in enumerate(self.coverage):
            if iv.start <= x < iv.end:
                return i
        return None

    def is_walked(self, x: Fraction) -> bool:
        return self._cover_index(x) is not None

    def _same_cover(self, x1: Fraction, x2: Fraction) -> bool:
        i1, i2 = self._cover_index(x1), self._cover_index(x2)
        return i1 is not None and i1 == i2

    # -- measured stakes ----------------------------------------------
    def measured_stakes(self) -> tuple[str, ...]:
        return tuple(
            sid
            for sid, _ in sorted(self._points.items(), key=lambda kv: kv[1][0])
        )

    def stake_elevation(self, stake_id: str) -> Fraction:
        try:
            return self._points[stake_id][1]
        except KeyError as exc:
            raise LevelingError(
                f"stake {stake_id!r} has no anchored elevation"
            ) from exc

    # -- interpolation ------------------------------------------------
    def ground_at(self, x) -> Optional[Fraction]:
        """Ground height at ``x``.

        Returns ``None`` when ``x`` is outside walked coverage or when the
        bracketing measured stakes are not adjacent inside one walked
        interval.  It never interpolates across an un-walked gap.
        """
        x = as_fraction(x)
        if not self.is_walked(x):
            return None
        # Exact measured stake?
        for sid, (sx, elev) in self._points.items():
            if sx == x:
                return elev
        # Find bracketing measured points.
        left = right = None
        for sx, elev in self._ordered:
            if sx < x:
                left = (sx, elev)
            elif sx > x:
                right = (sx, elev)
                break
        if left is None or right is None:
            return None
        if not self._same_cover(left[0], right[0]):
            return None  # an un-walked gap sits between the brackets
        span = right[0] - left[0]
        t = (x - left[0]) / span
        return left[1] + t * (right[1] - left[1])

    def ground_polyline(self, step: int = 1) -> list[tuple[Fraction, Fraction]]:
        """Dense polyline at integer (or given) steps, only where known."""
        if not self._ordered:
            return []
        lo = self._ordered[0][0]
        hi = self._ordered[-1][0]
        out: list[tuple[Fraction, Fraction]] = []
        x = lo
        step = as_fraction(step)
        while x <= hi:
            g = self.ground_at(x)
            if g is not None:
                out.append((x, g))
            x += step
        return out
