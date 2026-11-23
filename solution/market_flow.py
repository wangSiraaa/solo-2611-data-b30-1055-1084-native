"""Market cue-flag flow: sight lines, whole-group number-taking, issuance.

This module sits *on top of* :mod:`chainage_catalog` / :mod:`inspection_library`
and never modifies them.  It models the fair described in ``materials/730``.

Named physical objects
----------------------
Every cue flag (引客旗) and street-corner partition (街角隔板) is a **named
physical object**.  Only objects *actually inspected this session* may be
trusted.  Inspection is physical coverage (an :class:`Interval` union) and reuses
the exact half-open semantics of the chainage catalog.

Sight lines
-----------
A walking stop (步行停点) faces a direction (``+1`` east / ``-1`` west on the
1-D route axis).  From a stop a group sees flags **in front, up to the first
*actually inspected* partition** (实检隔板).  If a visible flag itself was never
inspected, the whole stretch needed to recognise it is unverified, so the group
**defers** its number-taking at that stop and may **never** substitute a
same-number flag from another (chain-broken) segment.

Taking a number
---------------
At each stop a group tries the flags it recognises ordered by *colour
priority*, then *along-path distance*, then *physical number*.  It takes the
**whole group's** worth of slots from a *single* flag or nothing -- no splitting
a group, no second number.  Same-cell arrivals are served in registered
``fixed_order``.  On success the group leaves the original path for that flag's
stall; on failure it walks to the next stop.

Issuance protocols
------------------
``SEALED`` (方案一) -- before departure a named real-number segment is sealed to
a group; other groups looking at the flag cannot take that segment, and only the
guide returning the physical ticket releases it.  Sealed numbers are still
*physically* subject to arrival (``open_cell``): while the tickets are still en
route nobody, not even the seal holder, can take them.

``COURIER`` (方案二) -- nothing is pre-sealed; a courier carries named surplus
numbers and a segment becomes takeable at a flag only once it *arrives*
(``open_cell``).  Numbers may be reassigned between flags only when **both**
flags permit the move (mutual ``reassign_to``) and the bag arrives in time.

Late coverage
-------------
A late inspection supplement may only change groups that have **not yet taken**
a number.  A group already holding a real ticket (or at its stall) is never
revoked; those are passed as ``frozen_tickets`` and the engine replays only the
still-open groups.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from enum import Enum
from fractions import Fraction
from typing import Iterable, Optional

from chainage_catalog import Interval, as_fraction, merge_intervals
from inspection_library import covered_by


def fully_covered(merged: list[Interval], lo: Fraction, hi: Fraction) -> bool:
    """True iff the half-open ``[lo, hi)`` lies inside one interval of ``merged``."""
    if lo >= hi:
        return False
    starts = [iv.start for iv in merged]
    i = bisect.bisect_right(starts, lo) - 1
    if i < 0:
        return False
    return merged[i].start <= lo and merged[i].end >= hi


# Open colour vocabulary; a group lists what it recognises, in priority order.
RED = "red"
BLUE = "blue"
GREEN = "green"


# ---------------------------------------------------------------------------
# Named physical objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Flag:
    """A named cue flag fixed at a physical position on the route axis.

    ``label`` is its chainage label; it may repeat on another segment, which is
    exactly why a global label lookup would be wrong.  ``capacity`` is the
    remaining *whole-person* slots.  ``open_cell`` is the first cell at which
    its tickets are physically present (before that they are "still en route").
    ``walk_cells`` is the walk from a stop, following the flag, to its stall.
    ``reassign_to`` names organiser-approved adjacent flags this flag's surplus
    may be moved to (COURIER, mutual approval required).

    ``segment``, when given, is the physical extent of the chain-broken section
    the flag lives on.  Recognising the flag requires that **whole** segment to
    have been inspected (not merely the flag's point).  A same-number flag on
    another segment is never an acceptable substitute.
    """

    asset_id: str
    route_id: str
    physical_position: Fraction
    color: str
    stall_id: str
    capacity: int
    open_cell: int
    walk_cells: int
    physical_number: int
    label: Optional[Fraction] = None
    reassign_to: tuple[str, ...] = ()
    segment: Optional[Interval] = None

    @classmethod
    def from_dict(cls, d: dict) -> "Flag":
        seg = d.get("segment")
        return cls(
            asset_id=str(d["asset_id"]),
            route_id=str(d["route_id"]),
            physical_position=as_fraction(d["physical_position"]),
            color=str(d["color"]),
            stall_id=str(d["stall_id"]),
            capacity=int(d["capacity"]),
            open_cell=int(d.get("open_cell", 0)),
            walk_cells=int(d.get("walk_cells", 0)),
            physical_number=int(d.get("physical_number", 0)),
            label=None if d.get("label") is None else as_fraction(d["label"]),
            reassign_to=tuple(str(x) for x in d.get("reassign_to", ())),
            segment=None
            if seg is None
            else Interval(as_fraction(seg[0]), as_fraction(seg[1])),
        )


@dataclass(frozen=True)
class Partition:
    """A street-corner partition; truncates the sight line when inspected."""

    asset_id: str
    route_id: str
    physical_position: Fraction

    @classmethod
    def from_dict(cls, d: dict) -> "Partition":
        return cls(
            asset_id=str(d["asset_id"]),
            route_id=str(d["route_id"]),
            physical_position=as_fraction(d["physical_position"]),
        )


# ---------------------------------------------------------------------------
# Inspection session
# ---------------------------------------------------------------------------


class InspectionSession:
    """The set of physical spans actually inspected this session."""

    def __init__(self, covered: Iterable[Interval] | None = None):
        self._covered: list[Interval] = merge_intervals(list(covered or []))

    @property
    def covered(self) -> list[Interval]:
        return list(self._covered)

    def is_inspected(self, position) -> bool:
        return covered_by(self._covered, as_fraction(position))

    def is_recognized(self, flag: "Flag") -> bool:
        """A flag is trusted only if its whole segment was inspected.

        When ``flag.segment`` is unset, fall back to point inspection (a flag
        with no chain-broken segment still needs to be an *actually inspected*
        named object).
        """
        if flag.segment is None:
            return covered_by(self._covered, flag.physical_position)
        return fully_covered(self._covered, flag.segment.start, flag.segment.end)

    def with_supplement(self, interval: Interval) -> "InspectionSession":
        """A *new* session including extra (late) coverage."""
        return InspectionSession([*self._covered, interval])

    def copy(self) -> "InspectionSession":
        return InspectionSession(self._covered)


# ---------------------------------------------------------------------------
# Stops / groups / protocols
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Stop:
    stop_id: str
    physical_position: Fraction
    facing: int  # +1 east, -1 west
    arrival_cell: int
    entrance_open: bool = True
    # cells walked to the *next* stop when taking fails
    travel_cells_to_next: int = 1

    @classmethod
    def from_dict(cls, d: dict) -> "Stop":
        return cls(
            stop_id=str(d["stop_id"]),
            physical_position=as_fraction(d["physical_position"]),
            facing=int(d["facing"]),
            arrival_cell=int(d["arrival_cell"]),
            entrance_open=bool(d.get("entrance_open", True)),
            travel_cells_to_next=int(d.get("travel_cells_to_next", 1)),
        )


@dataclass(frozen=True)
class Group:
    group_id: str
    size: int
    stops: tuple[Stop, ...]
    colors_priority: tuple[str, ...]
    fixed_order: int
    max_wait_cells: int = 0       # embossing wait tolerance (rental side)
    max_wait_at_stop: int = 0     # cells willing to wait at a stop for numbers

    @classmethod
    def from_dict(cls, d: dict) -> "Group":
        return cls(
            group_id=str(d["group_id"]),
            size=int(d["size"]),
            stops=tuple(Stop.from_dict(s) for s in d["stops"]),
            colors_priority=tuple(str(c) for c in d.get("colors_priority", ())),
            fixed_order=int(d.get("fixed_order", 0)),
            max_wait_cells=int(d.get("max_wait_cells", 0)),
            max_wait_at_stop=int(d.get("max_wait_at_stop", 0)),
        )


class Protocol(str, Enum):
    SEALED = "sealed"    # 方案一
    COURIER = "courier"  # 方案二


@dataclass(frozen=True)
class Seal:
    """A named real-number segment sealed to a group at a flag (SEALED)."""

    group_id: str
    flag_id: str
    count: int


@dataclass(frozen=True)
class Reassignment:
    """COURIER: move ``count`` numbers from ``source`` to ``dest`` by a cell.

    Applied only when the move is mutually approved by both flags; otherwise it
    is recorded as rejected and has no effect.
    """

    source_id: str
    dest_id: str
    count: int
    arrive_cell: int


# ---------------------------------------------------------------------------
# Sight lines
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SightLine:
    visible: tuple[Flag, ...]
    recognized: tuple[Flag, ...]
    pending: tuple[Flag, ...]           # visible but never inspected
    boundary_distance: Optional[Fraction]

    @property
    def must_defer(self) -> bool:
        return bool(self.pending)


def compute_sight_line(
    stop: Stop,
    flags: Iterable[Flag],
    partitions: Iterable[Partition],
    session: InspectionSession,
) -> SightLine:
    """Flags in front of ``stop`` up to the first *inspected* partition."""

    def dist(pos) -> Fraction:
        return (as_fraction(pos) - stop.physical_position) * stop.facing

    part_dists = sorted(
        d
        for p in partitions
        if (d := dist(p.physical_position)) > 0
        and session.is_inspected(p.physical_position)
    )
    boundary = part_dists[0] if part_dists else None

    ahead: list[tuple[Fraction, Flag]] = []
    for f in flags:
        d = dist(f.physical_position)
        if d <= 0:
            continue
        if boundary is not None and d >= boundary:
            continue
        ahead.append((d, f))
    ahead.sort(key=lambda t: (t[0], t[1].physical_number, t[1].asset_id))

    visible = tuple(f for _, f in ahead)
    recognized = tuple(f for f in visible if session.is_recognized(f))
    pending = tuple(f for f in visible if not session.is_recognized(f))
    return SightLine(visible, recognized, pending, boundary)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ticket:
    group_id: str
    size: int
    flag_id: str
    stall_id: str
    stop_id: str
    taken_cell: int
    arrival_cell: int
    order: int
    color: str
    from_seal: bool = False

    def to_dict(self) -> dict:
        return {
            "group_id": self.group_id,
            "size": self.size,
            "flag_id": self.flag_id,
            "stall_id": self.stall_id,
            "stop_id": self.stop_id,
            "taken_cell": self.taken_cell,
            "arrival_cell": self.arrival_cell,
            "order": self.order,
            "color": self.color,
            "from_seal": self.from_seal,
        }


@dataclass(frozen=True)
class Squeeze:
    """A later group's choice that an earlier take blocked on the same flag."""

    loser_group: str
    blocked_by: str
    flag_id: str
    cell: int

    def to_dict(self) -> dict:
        return {
            "loser_group": self.loser_group,
            "squeezed_by": self.blocked_by,
            "flag_id": self.flag_id,
            "cell": self.cell,
        }


@dataclass(frozen=True)
class Deferral:
    group_id: str
    stop_id: str
    cell: int
    reason: str

    def to_dict(self) -> dict:
        return {
            "group_id": self.group_id,
            "stop_id": self.stop_id,
            "cell": self.cell,
            "reason": self.reason,
        }


@dataclass
class GroupOutcome:
    group_id: str
    ticket: Optional[Ticket] = None
    deferred: list[Deferral] = field(default_factory=list)
    unmet: bool = False

    @property
    def stall_id(self) -> Optional[str]:
        return None if self.ticket is None else self.ticket.stall_id


@dataclass
class RunResult:
    protocol: Protocol
    tickets: list[Ticket]
    outcomes: dict[str, GroupOutcome]
    squeezes: list[Squeeze]
    flag_capacity_left: dict[str, int]
    frozen: tuple[str, ...] = ()
    rejected_reassignments: tuple[Reassignment, ...] = ()

    def appointments_by_stall(self) -> dict[str, list[Ticket]]:
        out: dict[str, list[Ticket]] = {}
        for t in self.tickets:
            out.setdefault(t.stall_id, []).append(t)
        return out

    def ticket_for(self, group_id: str) -> Optional[Ticket]:
        return self.outcomes[group_id].ticket


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class MarketFlow:
    """Simulate number-taking for a set of groups under one protocol."""

    def __init__(
        self,
        flags: Iterable[Flag],
        partitions: Iterable[Partition],
        groups: Iterable[Group],
        session: InspectionSession,
        protocol: Protocol = Protocol.COURIER,
        seals: Iterable[Seal] = (),
        reassignments: Iterable[Reassignment] = (),
        frozen_tickets: Iterable[Ticket] = (),
        released_seals: Iterable[Seal] = (),
    ):
        self.flags: dict[str, Flag] = {}
        for f in flags:
            if f.asset_id in self.flags:
                raise ValueError(f"duplicate flag asset_id {f.asset_id!r}")
            self.flags[f.asset_id] = f
        self.groups: dict[str, Group] = {}
        for g in groups:
            if g.group_id in self.groups:
                raise ValueError(f"duplicate group_id {g.group_id!r}")
            self.groups[g.group_id] = g
        self.partitions = tuple(partitions)
        self.session = session
        self.protocol = Protocol(protocol)
        self.seals = tuple(seals)
        self.reassignments = tuple(reassignments)
        self.frozen_tickets = tuple(frozen_tickets)
        self.released_seals = tuple(released_seals)

    # -- inventory ----------------------------------------------------
    def _init_inventory(self):
        # own_left = total physical slots remaining at each flag.
        own_left = {fid: f.capacity for fid, f in self.flags.items()}
        # sealed_left[(gid, fid)] = portion of fid's own_left reserved for gid.
        sealed_left: dict[tuple[str, str], int] = {}
        # extra[dest] = [{left, arrive, source}] numbers physically moved there.
        extra: dict[str, list[dict]] = {fid: [] for fid in self.flags}
        rejected: list[Reassignment] = []

        if self.protocol is Protocol.SEALED:
            for s in self.seals:
                if s.flag_id not in self.flags:
                    raise KeyError(f"seal targets unknown flag {s.flag_id!r}")
                key = (s.group_id, s.flag_id)
                sealed_left[key] = sealed_left.get(key, 0) + s.count
            # A seal releases only when the guide brings the physical ticket
            # back; a released seal frees that many of the group's slots again
            # (they fall back into the flag's free pool).
            for s in self.released_seals:
                key = (s.group_id, s.flag_id)
                if key in sealed_left:
                    sealed_left[key] = max(0, sealed_left[key] - s.count)
            for fid in self.flags:
                reserved = sum(v for (g, f), v in sealed_left.items() if f == fid)
                if reserved > own_left[fid]:
                    raise ValueError(f"seals over-subscribe flag {fid!r}")
        else:  # COURIER
            # The courier can handle only one station per cell: among approved
            # moves that become takeable in the same cell, keep the first
            # destination station and reject any move to a different station in
            # that cell.  (Deterministic: input order breaks ties.)
            station_at_cell: dict[int, str] = {}
            for r in self.reassignments:
                if not self._mutually_approved(r) or own_left[r.source_id] < r.count:
                    rejected.append(r)
                    continue
                pinned = station_at_cell.get(r.arrive_cell)
                if pinned is not None and pinned != r.dest_id:
                    rejected.append(r)
                    continue
                station_at_cell[r.arrive_cell] = r.dest_id
                own_left[r.source_id] -= r.count  # physically leaves the source
                extra[r.dest_id].append(
                    {"left": r.count, "arrive": r.arrive_cell, "source": r.source_id}
                )
        return own_left, sealed_left, extra, tuple(rejected)

    def _mutually_approved(self, r: Reassignment) -> bool:
        if r.source_id not in self.flags or r.dest_id not in self.flags:
            return False
        src, dst = self.flags[r.source_id], self.flags[r.dest_id]
        return r.dest_id in src.reassign_to and r.source_id in dst.reassign_to

    def _reserved(self, sealed_left, flag_id) -> int:
        return sum(v for (g, f), v in sealed_left.items() if f == flag_id)

    def _pools(self, flag: Flag, cell: int, gid: str, own_left, sealed_left, extra):
        """Takeable pools for ``gid`` at ``flag`` on ``cell``, in order.

        Physical arrival gates everything: before ``open_cell`` the flag's own
        numbers are still en route and unavailable even to a seal holder.
        """
        pools: list[tuple[str, object, int]] = []
        there = cell >= flag.open_cell
        if self.protocol is Protocol.SEALED:
            if there:
                mine = sealed_left.get((gid, flag.asset_id), 0)
                if mine > 0:
                    pools.append(("sealed", (gid, flag.asset_id), mine))
                free = own_left[flag.asset_id] - self._reserved(sealed_left, flag.asset_id)
                if free > 0:
                    pools.append(("free", flag.asset_id, free))
        else:  # COURIER
            if there and own_left[flag.asset_id] > 0:
                pools.append(("free", flag.asset_id, own_left[flag.asset_id]))
            for i, e in enumerate(extra[flag.asset_id]):
                if e["left"] > 0 and cell >= e["arrive"]:
                    pools.append(("extra", (flag.asset_id, i), e["left"]))
        return pools

    @staticmethod
    def _total(pools) -> int:
        return sum(c for _, _, c in pools)

    def _consume(self, flag_id, pools, size, own_left, sealed_left, extra):
        need = size
        used_seal = False
        for kind, key, count in pools:
            if need <= 0:
                break
            take = min(need, count)
            if kind == "sealed":
                sealed_left[key] -= take
                own_left[flag_id] -= take
                used_seal = True
            elif kind == "free":
                own_left[flag_id] -= take
            else:  # extra: already removed from the source's own_left
                f, i = key
                extra[f][i]["left"] -= take
            need -= take
        return used_seal

    # -- main loop ----------------------------------------------------
    def run(self, max_cell: Optional[int] = None) -> RunResult:
        own_left, sealed_left, extra, rejected = self._init_inventory()

        outcomes = {gid: GroupOutcome(group_id=gid) for gid in self.groups}
        tickets: list[Ticket] = []
        squeezes: list[Squeeze] = []
        # Per-flag history of takers and per-flag seal holders, for attribution.
        takers: dict[str, list[tuple[str, int]]] = {fid: [] for fid in self.flags}
        ticket_counter = 0

        # Frozen tickets: consume inventory; those groups are never revisited.
        frozen_ids: set[str] = set()
        for t in self.frozen_tickets:
            frozen_ids.add(t.group_id)
            if t.flag_id not in self.flags:
                raise KeyError(f"frozen ticket names unknown flag {t.flag_id!r}")
            key = (t.group_id, t.flag_id)
            if self.protocol is Protocol.SEALED and sealed_left.get(key, 0) > 0:
                take = min(t.size, sealed_left[key])
                sealed_left[key] -= take
                own_left[t.flag_id] -= t.size
            else:
                own_left[t.flag_id] = max(0, own_left[t.flag_id] - t.size)
            tickets.append(t)
            outcomes[t.group_id].ticket = t
            takers[t.flag_id].append((t.group_id, t.taken_cell))
            ticket_counter = max(ticket_counter, t.order)

        cursor = {gid: 0 for gid in self.groups}
        at_cell = {
            gid: (g.stops[0].arrival_cell if g.stops else 0)
            for gid, g in self.groups.items()
        }
        for gid in frozen_ids:
            at_cell[gid] = 10**9

        if max_cell is None:
            max_cell = self._upper_bound()

        for cell in range(0, max_cell + 1):
            pending = [
                gid
                for gid in self.groups
                if gid not in frozen_ids
                and outcomes[gid].ticket is None
                and cursor[gid] < len(self.groups[gid].stops)
                and at_cell[gid] == cell
            ]
            pending.sort(key=lambda gid: (self.groups[gid].fixed_order, gid))

            for gid in pending:
                group = self.groups[gid]
                stop = group.stops[cursor[gid]]

                if not stop.entrance_open:
                    outcomes[gid].deferred.append(
                        Deferral(gid, stop.stop_id, cell, "entrance closed at this stop")
                    )
                    self._advance(gid, cursor, at_cell, cell)
                    outcomes[gid].unmet = cursor[gid] >= len(group.stops)
                    continue

                sight = compute_sight_line(
                    stop, self.flags.values(), self.partitions, self.session
                )
                # Defer only when a *wanted* visible flag's segment is
                # uninspected: the stretch needed to recognise it is unverified.
                # An uninspected flag of a colour the group does not seek is
                # irrelevant and must not block a trusted flag.
                wanted_pending = [
                    f for f in sight.pending if _color_rank(group, f) is not None
                ]
                if wanted_pending:
                    outcomes[gid].deferred.append(
                        Deferral(
                            gid, stop.stop_id, cell,
                            "segment for a visible flag was not inspected; "
                            "deferred (no out-of-segment substitution)",
                        )
                    )
                    self._advance(gid, cursor, at_cell, cell)
                    outcomes[gid].unmet = cursor[gid] >= len(group.stops)
                    continue

                cands = []
                for f in sight.recognized:
                    rank = _color_rank(group, f)
                    if rank is None:
                        continue
                    cands.append((rank, _distance(stop, f), f.physical_number, f.asset_id, f))
                cands.sort(key=lambda t: (t[0], t[1], t[2], t[3]))

                took, used_seal = self._try_take(
                    gid, cell, cands, group.size,
                    own_left, sealed_left, extra, takers, squeezes,
                )
                if took is not None:
                    ticket_counter += 1
                    ticket = Ticket(
                        group_id=gid,
                        size=group.size,
                        flag_id=took.asset_id,
                        stall_id=took.stall_id,
                        stop_id=stop.stop_id,
                        taken_cell=cell,
                        arrival_cell=cell + took.walk_cells,
                        order=ticket_counter,
                        color=took.color,
                        from_seal=used_seal,
                    )
                    tickets.append(ticket)
                    outcomes[gid].ticket = ticket
                    takers[took.asset_id].append((gid, cell))
                    continue

                # Nothing fits yet.  If a recognised flag's numbers are merely
                # still en route (or a reassigned bag is a little late) and the
                # group is willing to wait at this stop within its tolerance,
                # wait for the earliest such cell instead of walking on.
                limit = stop.arrival_cell + group.max_wait_at_stop
                wait_until = self._earliest_takeable_cell(
                    gid, cands, cell + 1, limit, group.size,
                    own_left, sealed_left, extra,
                )
                if wait_until is not None:
                    at_cell[gid] = wait_until
                    outcomes[gid].deferred.append(
                        Deferral(gid, stop.stop_id, cell,
                                 f"waiting at stop until cell {wait_until} "
                                 "for numbers en route")
                    )
                    continue

                self._advance(gid, cursor, at_cell, cell)
                outcomes[gid].unmet = cursor[gid] >= len(group.stops)

        return RunResult(
            protocol=self.protocol,
            tickets=tickets,
            outcomes=outcomes,
            squeezes=squeezes,
            flag_capacity_left=dict(own_left),
            frozen=tuple(sorted(frozen_ids)),
            rejected_reassignments=rejected,
        )

    # -- taking -------------------------------------------------------
    def _earliest_takeable_cell(self, gid, cands, start, limit, size,
                                own_left, sealed_left, extra) -> Optional[int]:
        """Earliest cell in ``[start, limit]`` at which a candidate is takeable.

        Only *recognised* candidates are considered, and waiting is worthwhile
        only because numbers can arrive (flag ``open_cell`` or a reassigned bag).
        Returns None when nothing becomes takeable within the limit.
        """
        if limit < start:
            return None
        for c in range(start, limit + 1):
            for _rank, _dist, _num, _aid, flag in cands:
                pools = self._pools(flag, c, gid, own_left, sealed_left, extra)
                if self._total(pools) >= size:
                    return c
        return None

    def _try_take(self, gid, cell, cands, size, own_left, sealed_left, extra,
                  takers, squeezes):
        for rank, dist, num, aid, flag in cands:
            pools = self._pools(flag, cell, gid, own_left, sealed_left, extra)
            if self._total(pools) >= size:
                used_seal = self._consume(
                    flag.asset_id, pools, size, own_left, sealed_left, extra
                )
                return flag, used_seal
            self._record_block(gid, cell, flag, sealed_left, takers, squeezes)
        return None, False

    def _record_block(self, gid, cell, flag, sealed_left, takers, squeezes):
        """Attribute a failed candidate to a seal holder or an earlier taker."""
        if self.protocol is Protocol.SEALED:
            holders = [
                g for (g, f), v in sealed_left.items()
                if f == flag.asset_id and v > 0 and g != gid
            ]
            if holders:
                squeezes.append(Squeeze(gid, holders[0], flag.asset_id, cell))
                return
        for blocker, _blocked_cell in reversed(takers.get(flag.asset_id, [])):
            if blocker != gid:
                squeezes.append(Squeeze(gid, blocker, flag.asset_id, cell))
                return

    # -- walking ------------------------------------------------------
    def _advance(self, gid, cursor, at_cell, cell) -> None:
        group = self.groups[gid]
        stop = group.stops[cursor[gid]]
        cursor[gid] += 1
        if cursor[gid] < len(group.stops):
            at_cell[gid] = cell + max(1, stop.travel_cells_to_next)
        else:
            at_cell[gid] = 10**9

    def _upper_bound(self) -> int:
        if not self.groups:
            return 0
        cap = sum(f.capacity for f in self.flags.values())
        walk = max((f.walk_cells for f in self.flags.values()), default=0)
        hi = 0
        for g in self.groups.values():
            base = max((s.arrival_cell for s in g.stops), default=0)
            travel = sum(s.travel_cells_to_next for s in g.stops)
            hi = max(hi, base + travel + walk + cap + 4)
        return hi


def _color_rank(group: Group, flag: Flag) -> Optional[int]:
    try:
        return group.colors_priority.index(flag.color)
    except ValueError:
        return None


def _distance(stop: Stop, flag: Flag) -> Fraction:
    return (flag.physical_position - stop.physical_position) * stop.facing
