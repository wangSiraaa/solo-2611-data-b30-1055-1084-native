"""End-to-end demo: cue-flag sight lines -> number-taking -> rental plan.

Runs the concrete scenarios from ``materials/730`` and prints, for each:

* what each group sees and where it takes its whole-group number;
* who got squeezed (which later group's choice an earlier take blocked);
* the real stall appointments (which group, which cell, how many people, which
  stall) that the rental desk must honour;
* the **minimum whole-shift rental** plan with per-cell machine dispatch and the
  named person called.

Usage:  python3 solution/run_market_demo.py
"""

from __future__ import annotations

import os
import sys
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from chainage_catalog import Interval  # noqa: E402
from market_flow import (  # noqa: E402
    Flag,
    Group,
    InspectionSession,
    MarketFlow,
    Partition,
    Protocol,
    Reassignment,
    Seal,
    Stop,
)
from rental_optimizer import (  # noqa: E402
    Machine,
    Person,
    RentalOptimizer,
    persons_from_tickets,
)


def _iv(a, b) -> Interval:
    return Interval(Fraction(a), Fraction(b))


def _ticket_line(t):
    return (
        f"{t.group_id} x{t.size} -> flag {t.flag_id} ({t.color}) -> "
        f"stall {t.stall_id}  [take cell {t.taken_cell}, arrive {t.arrival_cell}]"
    )


def show_flow(title, res, wait_by_group=None):
    print("=" * 78)
    print(title)
    print("=" * 78)
    for gid, out in sorted(res.outcomes.items()):
        if out.ticket:
            print(f"  {_ticket_line(out.ticket)}")
        else:
            why = "; ".join(d.reason for d in out.deferred) or "no take"
            print(f"  {gid} x?- -> NO TICKET  ({why})")
    if res.squeezes:
        print("  squeezes (later group's choice blocked by an earlier take):")
        for s in res.squeezes:
            print(f"    {s.loser_group} blocked on flag {s.flag_id} @cell {s.cell} by {s.blocked_by}")
    for stall, tickets in sorted(res.appointments_by_stall().items()):
        parts = ", ".join(f"{t.group_id}x{t.size}@cell{t.arrival_cell}" for t in tickets)
        print(f"  appointments at {stall}: {parts}")
    left = ", ".join(f"{k}:{v}" for k, v in sorted(res.flag_capacity_left.items()))
    print(f"  flag capacity left: {left}")
    print()


def show_rental(title, plan, machines_note=""):
    print("-" * 78)
    print(title + (f"  ({machines_note})" if machines_note else ""))
    print("-" * 78)
    if not plan.feasible:
        print(f"  INFEASIBLE; earliest unserviceable ticket: {plan.earliest_unserviceable}")
        print()
        return
    print(f"  minimum whole-shift rent = {plan.cost}; rented machines = {list(plan.rented)}")
    print("  per-cell dispatch (machine -> stall, calls named person):")
    for row in plan.dispatch():
        print(
            f"    cell {row['cell']}: {row['machine']} @ {row['stall']} "
            f"calls {row['person']} (ticket {row['ticket']}, order {row['order']})"
        )
    print()


# ---------------------------------------------------------------------------
# Scenario helpers
# ---------------------------------------------------------------------------


def scenario_ordinary_first_group():
    flag = Flag(
        "X", "L", Fraction(500), "red", "Jia", capacity=2, open_cell=0,
        walk_cells=1, physical_number=1, segment=_iv(490, 510),
    )
    part = Partition("Q", "L", Fraction(520))
    sess = InspectionSession([_iv(460, 530)])
    stop = Stop("bridgehead", Fraction(470), +1, arrival_cell=0)
    group = Group(
        "g1", 2, (stop,), ("red",), fixed_order=0,
        max_wait_cells=1, max_wait_at_stop=0,
    )
    res = MarketFlow([flag], [part], [group], sess, Protocol.COURIER).run()
    show_flow("Scenario 1: ordinary first group (2 people, red only, walk 1 to Jia)", res)

    machines = [
        Machine("Jia-1", 1, frozenset({"Jia"})),
        Machine("Jia-2", 1, frozenset({"Jia"})),
    ]
    t = res.ticket_for("g1")
    if t:
        ppl_wait = persons_from_tickets(res.tickets, {"g1": 1})
        plan = RentalOptimizer(machines, 8).solve(ppl_wait)
        show_rental("Rental: both may wait one cell", plan, "each machine price 1")
        ppl_now = [
            Person(f"g1#{k}", "g1", "Jia", t.arrival_cell, 0, t.order, k)
            for k in range(2)
        ]
        plan2 = RentalOptimizer(machines, 8).solve(ppl_now)
        show_rental("Rental: must be served in the arrival cell", plan2, "each machine price 1")

    # Variant: correct the inspected partition to in front of the flag -> hides it.
    part_front = Partition("Q", "L", Fraction(480))
    res_hidden = MarketFlow([flag], [part_front], [group], sess, Protocol.COURIER).run()
    show_flow("Scenario 1b: inspected partition corrected to IN FRONT of X (X hidden)", res_hidden)
    plan0 = RentalOptimizer(machines, 8).solve(persons_from_tickets(res_hidden.tickets, {"g1": 1}))
    show_rental("Rental: no appointments -> no machine", plan0)


def scenario_near_far():
    near = Flag("Rn", "L", Fraction(500), "red", "StallN", capacity=3, open_cell=0,
                walk_cells=1, physical_number=3, segment=_iv(490, 510))
    far = Flag("Rf", "L", Fraction(600), "red", "StallF", capacity=2, open_cell=0,
               walk_cells=4, physical_number=2, segment=_iv(590, 610))
    sess = InspectionSession([_iv(480, 700)])
    stop = Stop("s", Fraction(470), +1, arrival_cell=0)
    A = Group("A", 2, (stop,), ("red",), fixed_order=0)
    B = Group("B", 2, (stop,), ("red",), fixed_order=1)
    res = MarketFlow([near, far], [], [A, B], sess, Protocol.COURIER).run()
    show_flow("Scenario 2: two visible red flags (near=3, far=2); no combining", res)

    machines = [
        Machine("StallN-m", 1, frozenset({"StallN"}), move_cells=2),
        Machine("StallF-m", 1, frozenset({"StallF"}), move_cells=2),
    ]
    # Each pair accepts up to one cell of embossing wait, so one machine per
    # stall suffices (two people served in consecutive cells).
    plan = RentalOptimizer(machines, 12).solve(
        persons_from_tickets(res.tickets, {"A": 1, "B": 1})
    )
    show_rental("Rental: two stalls, different walk costs (wait 1)", plan)


def scenario_jia_yi():
    def build(east_cap, east_open, jia_open, yi_wait):
        # east flag E at 500 (stall Jia), west flag W at 300 (stall Wei).
        east = Flag("E", "L", Fraction(500), "red", "Jia", capacity=east_cap,
                    open_cell=east_open, walk_cells=1, physical_number=1,
                    segment=_iv(490, 510), reassign_to=("W",))
        west = Flag("W", "L", Fraction(300), "red", "Wei", capacity=2, open_cell=0,
                    walk_cells=2, physical_number=2, segment=_iv(290, 310),
                    reassign_to=("E",))
        sess = InspectionSession([_iv(280, 520)])
        jia = Group("Jia", 2,
                    (Stop("sE", Fraction(470), +1, 0, entrance_open=jia_open),),
                    ("red",), fixed_order=0)
        yi = Group("Yi", 2,
                   (Stop("sE", Fraction(470), +1, 0),),
                   ("red",), fixed_order=1, max_wait_at_stop=yi_wait)
        return [east, west], sess, [jia, yi]

    # SEALED: E sealed to Jia, but at Jia's stop the east entrance is not open at
    # cell 0 and the numbers are still en route -> nobody takes, and the seal
    # blocks Yi even at the same cell.
    flags, sess, groups = build(east_cap=2, east_open=1, jia_open=False, yi_wait=0)
    res_s = MarketFlow(flags, [], groups, sess, Protocol.SEALED,
                       seals=[Seal("Jia", "E", 2)]).run()
    show_flow("Scenario 3a (方案一 SEALED): E sealed to Jia, not open & en route", res_s)

    # COURIER at cell 0: Jia's entrance closed; numbers reassigned W->E, bag on
    # time -> Yi takes the real E numbers (true visible flag). Jia is not at stall.
    flags, sess, groups = build(east_cap=0, east_open=0, jia_open=False, yi_wait=1)
    res_c0 = MarketFlow(flags, [], groups, sess, Protocol.COURIER,
                        reassignments=[Reassignment("W", "E", 2, arrive_cell=0)]).run()
    show_flow("Scenario 3b (方案二 COURIER): bag on time -> Yi takes E at cell 0", res_c0)

    # COURIER delayed one cell: Yi waits, takes at cell 1, arrives one cell later.
    flags, sess, groups = build(east_cap=0, east_open=0, jia_open=False, yi_wait=2)
    res_c1 = MarketFlow(flags, [], groups, sess, Protocol.COURIER,
                        reassignments=[Reassignment("W", "E", 2, arrive_cell=1)]).run()
    show_flow("Scenario 3c (方案二 COURIER): bag one cell late -> Yi waits, takes cell 1", res_c1)

    machines = [Machine("Jia-1", 1, frozenset({"Jia"})),
                Machine("Jia-2", 1, frozenset({"Jia"}))]
    deadline = 2
    for tag, res in (("bag on time", res_c0), ("bag +1 cell", res_c1)):
        t = res.ticket_for("Yi")
        if t is None:
            show_rental(f"Rental: {tag} (no Yi ticket)", RentalOptimizer(machines, 8).solve([]))
            continue
        wait = max(0, deadline - t.arrival_cell)
        people = [
            Person(f"Yi#{k}", "Yi", "Jia", t.arrival_cell, wait, t.order, k)
            for k in range(2)
        ]
        plan = RentalOptimizer(machines, 8).solve(people)
        show_rental(
            f"Rental: {tag} (Yi arrival {t.arrival_cell}, finish-by {deadline})",
            plan, "two Jia machines, price 1 each",
        )


def main() -> int:
    scenario_ordinary_first_group()
    scenario_near_far()
    scenario_jia_yi()
    print("=" * 78)
    print("All scenarios delivered: number-taking -> appointments -> min rental.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
