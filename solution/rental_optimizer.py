"""Embosser-machine rental: minimum whole-shift cost, named dispatch & calls.

The rental desk receives the *real* stall appointments (tickets actually taken at
a stall, plus any pre-existing reservations).  Rules, from the brief:

* Each person carries a wait tolerance: they may be served in any cell from
  their arrival cell up to ``arrival + max_wait``.
* Same stall: first-come-first-served (by arrival, then ticket/number order);
  service cells therefore never decrease along that queue.
* One machine serves at most **one person per cell**; a customer never switches
  stalls.
* Machines are named; each has a whole-shift rent price and a set of allowed
  **first-investment stalls** (首投点), which differ per machine.  To serve at a
  different stall after that, a machine spends a given number of cells moving.
* Goal: **minimum total whole-shift rent** such that every appointment is
  honoured on time, plus the per-cell machine dispatch and the named call.
* Incremental mode: given already-called people (``locked``) and already-paid
  machines plus new in-shift group tickets, if the remainder cannot be finished,
  report the **earliest ticket that cannot be served on time**.

The search is exact for the brief's envelope (``<= 6`` groups, ``<= 8`` cells,
``<= 3`` stalls, ``<= 4`` machines): enumerate rentable-machine subsets by
ascending total price and, for each, run a pruned backtracking feasibility
search.  The first feasible subset yields the minimum-cost plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Person:
    """One physical person to be embossed at a stall."""

    person_id: str
    ticket_id: str
    stall_id: str
    arrival_cell: int
    max_wait: int
    order: int            # number / ticket order (same-stall FCFS tie-break)
    person_index: int = 0  # tie-break within a group

    @property
    def deadline(self) -> int:
        return self.arrival_cell + self.max_wait

    @property
    def queue_key(self) -> tuple:
        return (self.arrival_cell, self.order, self.person_index)


@dataclass(frozen=True)
class Machine:
    machine_id: str
    rent_price: int
    first_stalls: frozenset[str]
    move_cells: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> "Machine":
        return cls(
            machine_id=str(d["machine_id"]),
            rent_price=int(d["rent_price"]),
            first_stalls=frozenset(str(s) for s in d["first_stalls"]),
            move_cells=int(d.get("move_cells", 0)),
        )


@dataclass(frozen=True)
class LockedService:
    """An already-called service: person -> machine, cell, stall (incremental)."""

    person_id: str
    machine_id: str
    cell: int
    stall_id: str


@dataclass(frozen=True)
class Assignment:
    person_id: str
    ticket_id: str
    machine_id: str
    stall_id: str
    cell: int
    order: int
    locked: bool = False

    def to_dict(self) -> dict:
        return {
            "person_id": self.person_id,
            "ticket_id": self.ticket_id,
            "machine_id": self.machine_id,
            "stall_id": self.stall_id,
            "cell": self.cell,
            "order": self.order,
            "locked": self.locked,
        }


@dataclass
class RentalPlan:
    feasible: bool
    cost: int
    rented: tuple[str, ...]
    assignments: tuple[Assignment, ...]
    earliest_unserviceable: Optional[str] = None

    def schedule_by_cell(self) -> dict[int, list[Assignment]]:
        out: dict[int, list[Assignment]] = {}
        for a in sorted(self.assignments, key=lambda a: (a.cell, a.machine_id)):
            out.setdefault(a.cell, []).append(a)
        return out

    def dispatch(self) -> list[dict]:
        """Per-cell machine dispatch and the named person being called."""
        rows = []
        for cell, items in sorted(self.schedule_by_cell().items()):
            for a in items:
                rows.append(
                    {
                        "cell": cell,
                        "machine": a.machine_id,
                        "stall": a.stall_id,
                        "person": a.person_id,
                        "ticket": a.ticket_id,
                        "order": a.order,
                    }
                )
        return rows


class RentalError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Solver
# ---------------------------------------------------------------------------


class RentalOptimizer:
    def __init__(self, machines: Iterable[Machine], horizon: int):
        self.machines = list(machines)
        self.by_id: dict[str, Machine] = {}
        for m in self.machines:
            if m.machine_id in self.by_id:
                raise RentalError("duplicate machine_id")
            self.by_id[m.machine_id] = m
        self.horizon = horizon

    # -- public -------------------------------------------------------
    def solve(
        self,
        persons: Iterable[Person],
        locked: Iterable[LockedService] = (),
        paid_machines: Iterable[str] = (),
    ) -> RentalPlan:
        persons = list(persons)
        locked = list(locked)
        paid = set(paid_machines)
        for mid in paid:
            if mid not in self.by_id:
                raise RentalError(f"paid machine {mid!r} unknown")
        for lk in locked:
            if lk.machine_id not in self.by_id:
                raise RentalError(
                    f"locked service names unknown machine {lk.machine_id!r}"
                )

        rentable = [m for m in self.machines if m.machine_id not in paid]
        plan = self._enumerate(persons, locked, paid, rentable)
        if plan is not None:
            return plan

        earliest = self._earliest_unserviceable(persons, locked)
        return RentalPlan(
            feasible=False,
            cost=0,
            rented=(),
            assignments=(),
            earliest_unserviceable=earliest,
        )

    # -- subset enumeration ------------------------------------------
    def _enumerate(self, persons, locked, paid, rentable) -> Optional[RentalPlan]:
        n = len(rentable)
        subsets = []
        for mask in range(1 << n):
            chosen = [rentable[i] for i in range(n) if mask & (1 << i)]
            extra_cost = sum(m.rent_price for m in chosen)
            subsets.append((extra_cost, len(chosen), mask, chosen))
        subsets.sort(key=lambda t: (t[0], t[1], t[2]))

        paid_machines = [self.by_id[mid] for mid in sorted(paid)]
        for extra_cost, _, _, chosen in subsets:
            avail = list(chosen) + paid_machines
            result = self._search(persons, locked, avail)
            if result is not None:
                used = tuple(sorted({a.machine_id for a in result}))
                return RentalPlan(
                    feasible=True,
                    cost=extra_cost,
                    rented=used,
                    assignments=tuple(
                        sorted(result, key=lambda a: (a.cell, a.machine_id))
                    ),
                )
        return None

    # -- feasibility search ------------------------------------------
    def _search(self, persons, locked, machines) -> Optional[list[Assignment]]:
        """Return a full assignment list, or None if infeasible."""
        machine_ids = [m.machine_id for m in machines]
        by_id = {m.machine_id: m for m in machines}

        # Pre-place locked services.
        sched: dict[str, list[tuple[int, str, str]]] = {mid: [] for mid in machine_ids}
        assignments: list[Assignment] = []
        for lk in locked:
            if lk.machine_id not in by_id:
                return None
            sched[lk.machine_id].append((lk.cell, lk.stall_id, lk.person_id))
            assignments.append(
                Assignment(
                    lk.person_id, lk.person_id, lk.machine_id, lk.stall_id,
                    lk.cell, order=0, locked=True,
                )
            )
        for mid in sched:
            sched[mid].sort()
        if not self._all_machines_valid(sched, by_id):
            return None

        # Position in a stall's FCFS queue for people already placed.
        # stall -> list of (queue_key, cell)
        stall_placed: dict[str, list[tuple[tuple, int]]] = {}
        for a in assignments:
            stall_placed.setdefault(a.stall_id, []).append(((a.cell, 0, 0), a.cell))

        # Order people most-constrained first.
        order = sorted(
            persons,
            key=lambda p: (p.deadline, p.max_wait, p.arrival_cell, p.order, p.person_index),
        )
        return self._dfs(
            order, 0, machines, by_id, sched, assignments, stall_placed
        )

    def _dfs(self, order, idx, machines, by_id, sched, assignments, stall_placed):
        if idx == len(order):
            return list(assignments)
        person = order[idx]
        # Try each machine, then each feasible cell (earliest first).
        for machine in machines:
            s = sched[machine.machine_id]
            for cell in range(person.arrival_cell, person.deadline + 1):
                if not self._cell_ok(machine, s, cell, person.stall_id):
                    continue
                if not self._stall_fcfs_ok(stall_placed.get(person.stall_id, []), person, cell):
                    continue
                s.append((cell, person.stall_id, person.person_id))
                s.sort()
                if not self._machine_valid(machine, s):
                    s.remove((cell, person.stall_id, person.person_id))
                    continue
                assignments.append(
                    Assignment(
                        person.person_id, person.ticket_id, machine.machine_id,
                        person.stall_id, cell, person.order,
                    )
                )
                lst = stall_placed.setdefault(person.stall_id, [])
                lst.append((person.queue_key, cell))
                result = self._dfs(order, idx + 1, machines, by_id, sched, assignments, stall_placed)
                if result is not None:
                    return result
                lst.pop()
                assignments.pop()
                s.remove((cell, person.stall_id, person.person_id))
        return None

    # -- constraint checks -------------------------------------------
    def _cell_ok(self, machine, sched, cell, stall) -> bool:
        # One person per cell on a machine.
        if any(c == cell for c, _, _ in sched):
            return False
        return True

    def _machine_valid(self, machine, sched) -> bool:
        sched = sorted(sched)
        if not sched:
            return True
        if sched[0][1] not in machine.first_stalls:
            return False
        for (c0, s0, _), (c1, s1, _) in zip(sched, sched[1:]):
            if c1 <= c0:
                return False
            if s0 == s1:
                continue  # same stall: just needs the next free cell
            # Different stall: the machine spends ``move_cells`` cells moving, so
            # the next service cannot start until move_cells + 1 cells later.
            if c1 - c0 < machine.move_cells + 1:
                return False
        return True

    def _all_machines_valid(self, sched, by_id) -> bool:
        for mid, s in sched.items():
            if not self._machine_valid(by_id[mid], s):
                return False
        return True

    def _stall_fcfs_ok(self, placed, person, cell) -> bool:
        # Service cells must be nondecreasing in FCFS queue order.
        for qkey, c in placed:
            if qkey < person.queue_key and c > cell:
                return False
            if qkey > person.queue_key and c < cell:
                return False
        return True

    # -- diagnostics --------------------------------------------------
    def _earliest_unserviceable(self, persons, locked) -> Optional[str]:
        """Earliest ticket that cannot be served on time with *every* machine.

        Consider people in arrival/ticket order; the first prefix whose set of
        people cannot all be served on time marks the earliest unserviceable
        ticket.
        """
        ordered = sorted(
            persons, key=lambda p: (p.arrival_cell, p.order, p.person_index)
        )
        for i in range(1, len(ordered) + 1):
            prefix = ordered[:i]
            if self._search(prefix, locked, self.machines) is None:
                return ordered[i - 1].ticket_id
        return None


# ---------------------------------------------------------------------------
# Convenience constructors
# ---------------------------------------------------------------------------


def persons_from_tickets(tickets, wait_by_group: dict[str, int]) -> list[Person]:
    """Expand stall tickets into per-person service units.

    ``wait_by_group`` maps a group to its per-person wait tolerance.  Each
    ticket's ``size`` people share the arrival cell and order (the number-taking
    order).
    """
    persons: list[Person] = []
    for t in tickets:
        wait = wait_by_group.get(t.group_id, 0)
        for k in range(t.size):
            persons.append(
                Person(
                    person_id=f"{t.group_id}#{k}",
                    ticket_id=t.group_id,
                    stall_id=t.stall_id,
                    arrival_cell=t.arrival_cell,
                    max_wait=wait,
                    order=t.order,
                    person_index=k,
                )
            )
    return persons
