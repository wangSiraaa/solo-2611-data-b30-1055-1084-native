"""Scale / cost demonstration for the market flow and rental optimizer.

Exercises the brief's stated envelope (<=6 groups, <=8 cells, <=4 flags,
<=3 stalls, <=4 machines) and reports the time cost of:

  * simulating number-taking for a busy multi-stop flow;
  * the exact minimum-rental search (subset enumeration + backtracking).

Run: python3 solution/bench_market.py
"""

from __future__ import annotations

import os
import sys
import time
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from chainage_catalog import Interval  # noqa: E402
from market_flow import (  # noqa: E402
    Flag,
    Group,
    InspectionSession,
    MarketFlow,
    Partition,
    Stop,
)
from rental_optimizer import Machine, RentalOptimizer, persons_from_tickets  # noqa: E402


def _iv(a, b):
    return Interval(Fraction(a), Fraction(b))


def build_envelope():
    # 4 flags on 4 chain segments, 3 stalls, 6 stops across <=8 cells.
    flags = [
        Flag("F1", "L", Fraction(100), "red", "A", capacity=6, open_cell=0,
             walk_cells=1, physical_number=1, segment=_iv(90, 110)),
        Flag("F2", "L", Fraction(200), "red", "B", capacity=6, open_cell=0,
             walk_cells=1, physical_number=2, segment=_iv(190, 210)),
        Flag("F3", "L", Fraction(300), "blue", "C", capacity=6, open_cell=0,
             walk_cells=1, physical_number=3, segment=_iv(290, 310)),
        Flag("F4", "L", Fraction(360), "red", "A", capacity=6, open_cell=0,
             walk_cells=2, physical_number=4, segment=_iv(350, 370)),
    ]
    partitions = [Partition("P1", "L", Fraction(370))]
    sess = InspectionSession([_iv(80, 380)])
    groups = [
        Group(f"G{i}", 2,
              (Stop(f"s{i}", Fraction(90), +1, arrival_cell=i % 3, travel_cells_to_next=1),),
              ("red", "blue"), fixed_order=i, max_wait_cells=2)
        for i in range(6)
    ]
    return flags, partitions, groups, sess


def main() -> int:
    flags, partitions, groups, sess = build_envelope()
    print("=" * 72)
    print("Market flow + rental optimizer scale (brief envelope)")
    print("=" * 72)
    print(f"flags={len(flags)} partitions={len(partitions)} "
          f"groups={len(groups)} cells<=8 stalls<=3 machines<=4")

    t0 = time.perf_counter()
    res = MarketFlow(flags, partitions, groups, sess).run()
    flow_ms = (time.perf_counter() - t0) * 1e3
    served = sum(1 for g in res.outcomes if res.ticket_for(g))
    print(f"\n[1] number-taking simulation: {flow_ms:.3f} ms "
          f"({served}/{len(groups)} groups served, "
          f"{len(res.squeezes)} squeeze events)")

    machines = [
        Machine("M1", 1, frozenset({"A"}), move_cells=1),
        Machine("M2", 2, frozenset({"B"}), move_cells=1),
        Machine("M3", 3, frozenset({"C"}), move_cells=1),
        Machine("M4", 4, frozenset({"A", "B", "C"}), move_cells=0),
    ]
    persons = persons_from_tickets(res.tickets, {g: 2 for g in res.outcomes})
    t1 = time.perf_counter()
    plan = RentalOptimizer(machines, 8).solve(persons)
    rent_ms = (time.perf_counter() - t1) * 1e3
    print(f"\n[2] exact minimum-rental search: {rent_ms:.3f} ms")
    if plan.feasible:
        print(f"    minimum whole-shift rent = {plan.cost}, "
              f"rented = {list(plan.rented)}")
        print(f"    people served = {len(plan.assignments)}")
    else:
        print(f"    infeasible; earliest unserviceable = {plan.earliest_unserviceable}")
    print("\n    cost model: O(2^#machines) subsets x backtracking over people; "
          "exact and small because the envelope caps machines at 4.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
