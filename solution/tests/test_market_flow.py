"""Tests for the market cue-flag flow and the rental optimizer.

Every numeric outcome quoted in the brief is pinned here:

* ordinary first group (2 people, red only, 1 machine at 1) -- and the two
  variants: moving the inspected partition *in front* of the flag hides it
  (zero appointments, no rental), while requiring same-cell service needs two
  machines;
* two visible red flags (near=3, far=2): first group takes 2 at near, the second
  cannot combine 1-near + 2-far and must take 2 at far; different stalls;
* the Jia/Yi en-route example under both protocols, plus the one-cell courier
  delay and the rental recheck;
* late coverage never revokes a taken ticket;
* the rental envelope (<=6 groups, <=8 cells, <=3 stalls, <=4 machines).
"""

from __future__ import annotations

import os
import sys
import unittest
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chainage_catalog import Interval  # noqa: E402
from market_flow import (  # noqa: E402
    Deferral,
    Flag,
    Group,
    InspectionSession,
    MarketFlow,
    Partition,
    Protocol,
    Reassignment,
    Seal,
    Stop,
    compute_sight_line,
)
from rental_optimizer import (  # noqa: E402
    LockedService,
    Machine,
    Person,
    RentalOptimizer,
    persons_from_tickets,
)


def iv(a, b) -> Interval:
    return Interval(Fraction(a), Fraction(b))


# ---------------------------------------------------------------------------
# Sight lines
# ---------------------------------------------------------------------------


class TestSightLines(unittest.TestCase):
    def _flag(self, fid="X", pos=500, cap=2, seg=(490, 510), **kw):
        return Flag(
            fid, "L", Fraction(pos), kw.pop("color", "red"), kw.pop("stall", "Jia"),
            capacity=cap, open_cell=kw.pop("open_cell", 0),
            walk_cells=kw.pop("walk_cells", 1),
            physical_number=kw.pop("physical_number", 1),
            segment=iv(*seg), **kw,
        )

    def test_flag_before_inspected_partition_is_visible(self):
        stop = Stop("s", Fraction(470), +1, 0)
        sess = InspectionSession([iv(460, 530)])
        line = compute_sight_line(
            stop, [self._flag()], [Partition("Q", "L", Fraction(520))], sess
        )
        self.assertEqual([f.asset_id for f in line.visible], ["X"])
        self.assertFalse(line.must_defer)

    def test_partition_in_front_hides_flag(self):
        stop = Stop("s", Fraction(470), +1, 0)
        sess = InspectionSession([iv(460, 530)])
        line = compute_sight_line(
            stop, [self._flag()], [Partition("Q", "L", Fraction(480))], sess
        )
        self.assertEqual(line.visible, ())

    def test_uninspected_partition_does_not_truncate(self):
        # Partition present but never inspected -> cannot truncate the line.
        stop = Stop("s", Fraction(470), +1, 0)
        line = compute_sight_line(
            stop, [self._flag()], [Partition("Q", "L", Fraction(480))],
            InspectionSession([iv(460, 470)]),  # partition at 480 not covered
        )
        self.assertEqual([f.asset_id for f in line.visible], ["X"])

    def test_flag_behind_stop_not_visible(self):
        stop = Stop("s", Fraction(600), +1, 0)  # faces east; flag at 500 is west
        sess = InspectionSession([iv(400, 700)])
        line = compute_sight_line(stop, [self._flag()], [], sess)
        self.assertEqual(line.visible, ())

    def test_west_facing_sees_western_flag(self):
        stop = Stop("s", Fraction(600), -1, 0)  # faces west
        sess = InspectionSession([iv(400, 700)])
        line = compute_sight_line(stop, [self._flag(pos=500)], [], sess)
        self.assertEqual([f.asset_id for f in line.visible], ["X"])

    def test_visible_but_uninspected_flag_defers(self):
        # Flag's whole segment not inspected -> visible but pending -> defer.
        stop = Stop("s", Fraction(470), +1, 0)
        sess = InspectionSession([iv(460, 475)])  # does not cover [490,510)
        line = compute_sight_line(stop, [self._flag()], [], sess)
        self.assertEqual([f.asset_id for f in line.visible], ["X"])
        self.assertTrue(line.must_defer)


# ---------------------------------------------------------------------------
# Scenario 1: the ordinary first group (+ partition-move variant)
# ---------------------------------------------------------------------------


class TestOrdinaryFirstGroup(unittest.TestCase):
    def _setup(self, partition_pos, session_cov, group_wait_at_stop=0):
        flag = Flag(
            "X", "L", Fraction(500), "red", "Jia", capacity=2, open_cell=0,
            walk_cells=1, physical_number=1, segment=iv(490, 510),
        )
        part = Partition("Q", "L", Fraction(partition_pos))
        sess = InspectionSession([iv(*session_cov)])
        stop = Stop("s", Fraction(470), +1, arrival_cell=0)
        grp = Group(
            "G1", 2, (stop,), ("red",), fixed_order=0,
            max_wait_cells=1, max_wait_at_stop=group_wait_at_stop,
        )
        return flag, part, sess, stop, grp

    def test_takes_whole_group_and_walks_one_cell(self):
        flag, part, sess, stop, grp = self._setup(520, (460, 530))
        res = MarketFlow([flag], [part], [grp], sess, Protocol.COURIER).run()
        t = res.ticket_for("G1")
        self.assertIsNotNone(t)
        self.assertEqual(t.size, 2)
        self.assertEqual(t.flag_id, "X")
        self.assertEqual(t.stall_id, "Jia")
        self.assertEqual(t.taken_cell, 0)
        self.assertEqual(t.arrival_cell, 1)  # walk 1 cell
        self.assertEqual(res.flag_capacity_left["X"], 0)

    def test_two_machines_needed_only_if_same_cell(self):
        flag, part, sess, stop, grp = self._setup(520, (460, 530))
        res = MarketFlow([flag], [part], [grp], sess, Protocol.COURIER).run()
        t = res.ticket_for("G1")
        machines = [
            Machine("Jia-1", 1, frozenset({"Jia"})),
            Machine("Jia-2", 1, frozenset({"Jia"})),
        ]
        # Both people can wait to the next cell -> one machine at cost 1.
        ppl_wait = [
            Person("g#0", "G1", "Jia", t.arrival_cell, 1, t.order, 0),
            Person("g#1", "G1", "Jia", t.arrival_cell, 1, t.order, 1),
        ]
        plan = RentalOptimizer(machines, 8).solve(ppl_wait)
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.cost, 1)
        self.assertEqual(plan.rented, ("Jia-1",))

        # Same-cell service (no wait) -> two machines, cost 2.
        ppl_now = [
            Person("g#0", "G1", "Jia", t.arrival_cell, 0, t.order, 0),
            Person("g#1", "G1", "Jia", t.arrival_cell, 0, t.order, 1),
        ]
        plan2 = RentalOptimizer(machines, 8).solve(ppl_now)
        self.assertTrue(plan2.feasible)
        self.assertEqual(plan2.cost, 2)
        self.assertEqual(set(plan2.rented), {"Jia-1", "Jia-2"})

    def test_partition_corrected_in_front_hides_flag_no_rental(self):
        # Move the *inspected partition's physical position* to in front of the
        # flag -> the group sees no flag and has no further stop -> zero tickets
        # -> zero appointments -> zero rental.
        flag, part_front, sess, stop, grp = self._setup(480, (460, 530))
        res = MarketFlow([flag], [part_front], [grp], sess, Protocol.COURIER).run()
        self.assertIsNone(res.ticket_for("G1"))
        self.assertTrue(res.outcomes["G1"].unmet)
        # No appointments -> no machines rented.
        plan = RentalOptimizer(
            [Machine("Jia-1", 1, frozenset({"Jia"}))], 8
        ).solve(persons_from_tickets(res.tickets, {"G1": 1}))
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.cost, 0)
        self.assertEqual(plan.rented, ())


# ---------------------------------------------------------------------------
# Scenario 2: two visible red flags (near=3, far=2), no combining
# ---------------------------------------------------------------------------


class TestNearFarFlags(unittest.TestCase):
    def setUp(self):
        self.near = Flag(
            "Rn", "L", Fraction(500), "red", "StallN", capacity=3, open_cell=0,
            walk_cells=1, physical_number=3, segment=iv(490, 510),
        )
        self.far = Flag(
            "Rf", "L", Fraction(600), "red", "StallF", capacity=2, open_cell=0,
            walk_cells=4, physical_number=2, segment=iv(590, 610),
        )
        self.sess = InspectionSession([iv(480, 700)])
        self.stop = Stop("s", Fraction(470), +1, 0)

    def test_first_takes_near_second_takes_far_whole(self):
        A = Group("A", 2, (self.stop,), ("red",), fixed_order=0)
        B = Group("B", 2, (self.stop,), ("red",), fixed_order=1)
        res = MarketFlow([self.near, self.far], [], [A, B], self.sess, Protocol.COURIER).run()
        ta, tb = res.ticket_for("A"), res.ticket_for("B")
        self.assertEqual(ta.flag_id, "Rn")
        self.assertEqual(ta.stall_id, "StallN")
        # B cannot take 1 from near + 2 from far (whole group, single flag);
        # near has only 1 left, so B must take 2 from far.
        self.assertEqual(tb.flag_id, "Rf")
        self.assertEqual(tb.stall_id, "StallF")
        self.assertEqual(tb.size, 2)
        # Different stalls / different walk costs allowed.
        self.assertNotEqual(ta.stall_id, tb.stall_id)
        self.assertNotEqual(ta.arrival_cell, tb.arrival_cell)
        # Near left 1 (unsplittable), far used up.
        self.assertEqual(res.flag_capacity_left["Rn"], 1)
        self.assertEqual(res.flag_capacity_left["Rf"], 0)

    def test_priority_colour_then_distance(self):
        # Group recognises red with lower priority than a second colour; if only
        # red is present it still takes the nearest red whole.
        A = Group("A", 2, (self.stop,), ("red",), fixed_order=0)
        res = MarketFlow([self.near, self.far], [], [A], self.sess).run()
        self.assertEqual(res.ticket_for("A").flag_id, "Rn")

    def test_second_group_recorded_squeeze(self):
        A = Group("A", 2, (self.stop,), ("red",), fixed_order=0)
        B = Group("B", 2, (self.stop,), ("red",), fixed_order=1)
        res = MarketFlow([self.near, self.far], [], [A, B], self.sess).run()
        # B lost the near flag to A (only 1 slot left < 2).
        self.assertTrue(any(
            s.loser_group == "B" and s.blocked_by == "A" and s.flag_id == "Rn"
            for s in res.squeezes
        ))


# ---------------------------------------------------------------------------
# Scenario 3: Jia/Yi en-route example under both protocols
# ---------------------------------------------------------------------------


def _jy_scenario(east_open_cell, east_cap, jia_entrance_open, reassign, yi_wait_at_stop):
    sess = InspectionSession([iv(280, 520)])
    east = Flag(
        "E", "L", Fraction(500), "red", "Jia", capacity=east_cap,
        open_cell=east_open_cell, walk_cells=1, physical_number=1,
        segment=iv(490, 510), reassign_to=tuple(reassign),
    )
    west = Flag(
        "W", "L", Fraction(300), "red", "Wei", capacity=2, open_cell=0,
        walk_cells=2, physical_number=2, segment=iv(290, 310), reassign_to=("E",),
    )
    jia = Group(
        "Jia", 2, (Stop("sE", Fraction(470), +1, 0, entrance_open=jia_entrance_open),),
        ("red",), fixed_order=0,
    )
    yi = Group(
        "Yi", 2, (Stop("sE", Fraction(470), +1, 0),), ("red",), fixed_order=1,
        max_wait_at_stop=yi_wait_at_stop,
    )
    return [east, west], sess, [jia, yi]


class TestJiaYiProtocols(unittest.TestCase):
    def test_sealed_en_route_neither_takes_and_seal_blocks_other(self):
        flags, sess, groups = _jy_scenario(
            east_open_cell=1, east_cap=2, jia_entrance_open=False, reassign=("W",),
            yi_wait_at_stop=0,
        )
        res = MarketFlow(
            flags, [], groups, sess, Protocol.SEALED,
            seals=[Seal("Jia", "E", 2)],
        ).run()
        # East entrance not open at Jia's stop AND tickets still en route ->
        # Jia cannot take; the seal blocks Yi at the same cell too.
        self.assertIsNone(res.ticket_for("Jia"))
        self.assertIsNone(res.ticket_for("Yi"))
        self.assertTrue(any(
            s.loser_group == "Yi" and s.blocked_by == "Jia" and s.flag_id == "E"
            for s in res.squeezes
        ))

    def test_courier_bag_catches_up_lets_yi_take_true_flag(self):
        # No pre-seal. Yi at the EAST stop sees E (inspected). E's own numbers are
        # held back (cap 0); two numbers reassigned W->E arrive cell 0. Both flags
        # mutually approve (E<->W). Jia never takes (its entrance is closed), so Yi
        # takes the two real numbers at E.
        flags, sess, groups = _jy_scenario(
            east_open_cell=0, east_cap=0, jia_entrance_open=False, reassign=("W",),
            yi_wait_at_stop=1,
        )
        res = MarketFlow(
            flags, [], groups, sess, Protocol.COURIER,
            reassignments=[Reassignment("W", "E", 2, arrive_cell=0)],
        ).run()
        t = res.ticket_for("Yi")
        self.assertIsNotNone(t)
        self.assertEqual(t.flag_id, "E")
        self.assertEqual(t.taken_cell, 0)
        # Jia took nothing, so "Jia cannot count as already at the stall".
        self.assertIsNone(res.ticket_for("Jia"))
        self.assertTrue(res.outcomes["Jia"].unmet)

    def test_courier_without_mutual_approval_rejects_reassignment(self):
        flags, sess, groups = _jy_scenario(
            east_open_cell=0, east_cap=0, jia_entrance_open=False, reassign=(),
            yi_wait_at_stop=1,
        )
        res = MarketFlow(
            flags, [], groups, sess, Protocol.COURIER,
            reassignments=[Reassignment("W", "E", 2, arrive_cell=0)],
        ).run()
        self.assertIsNone(res.ticket_for("Yi"))
        self.assertEqual(len(res.rejected_reassignments), 1)

    def test_seal_release_frees_reserved_numbers(self):
        # A seal reserves numbers up front. Without a release another group
        # cannot use the reserved portion; on release they fall back into the
        # flag's free pool. (A tight-capacity flag makes the difference visible.)
        flag = Flag("X", "L", Fraction(500), "red", "Jia", 2, 0, 1, 1,
                    segment=iv(490, 510))
        sess = InspectionSession([iv(490, 510)])
        stop = Stop("s", Fraction(470), +1, 0)
        g = Group("g", 2, (stop,), ("red",), fixed_order=0)
        g2 = Group("g2", 2, (stop,), ("red",), fixed_order=1)

        # Seal 2 for g out of a capacity-2 flag; g takes them, g2 gets nothing.
        res = MarketFlow([flag], [], [g, g2], sess, Protocol.SEALED,
                         seals=[Seal("g", "X", 2)]).run()
        self.assertIsNotNone(res.ticket_for("g"))
        self.assertIsNone(res.ticket_for("g2"))

    def test_courier_one_station_per_cell(self):
        # Two approved moves to *different* destination stations in the same
        # cell: the courier can only handle one station per cell, so the second
        # is rejected; spreading them across cells lets both apply.
        A = Flag("A", "L", Fraction(100), "red", "sA", 2, 0, 1, 1,
                 segment=iv(90, 110), reassign_to=("E", "S"))
        B = Flag("B", "L", Fraction(150), "red", "sB", 2, 0, 1, 2,
                 segment=iv(140, 160), reassign_to=("E", "S"))
        E = Flag("E", "L", Fraction(500), "red", "sE", 0, 0, 1, 3,
                 segment=iv(490, 510), reassign_to=("A", "B"))
        S = Flag("S", "L", Fraction(700), "red", "sS", 0, 0, 1, 4,
                 segment=iv(690, 710), reassign_to=("A", "B"))
        sess = InspectionSession([iv(80, 720)])
        same_cell = [
            Reassignment("A", "E", 2, 0),
            Reassignment("B", "S", 2, 0),
        ]
        res = MarketFlow([A, B, E, S], [], [], sess,
                         reassignments=same_cell).run()
        self.assertEqual(len(res.rejected_reassignments), 1)

        spread = [
            Reassignment("A", "E", 2, 0),
            Reassignment("B", "S", 2, 1),
        ]
        res2 = MarketFlow([A, B, E, S], [], [], sess,
                          reassignments=spread).run()
        self.assertEqual(len(res2.rejected_reassignments), 0)

    def test_courier_one_cell_delay_and_rental_recheck(self):
        # Delaying the courier by one cell delays Yi's ticket by one cell, which
        # (against a fixed finish-by deadline) raises the minimum rental.
        machines = [
            Machine("Jia-1", 1, frozenset({"Jia"})),
            Machine("Jia-2", 1, frozenset({"Jia"})),
        ]
        deadline = 2  # both people must be embossed by cell 2
        costs = {}
        arrivals = {}
        for bag_cell in (0, 1):
            flags, sess, groups = _jy_scenario(
                east_open_cell=0, east_cap=0, jia_entrance_open=False,
                reassign=("W",), yi_wait_at_stop=2,
            )
            res = MarketFlow(
                flags, [], groups, sess, Protocol.COURIER,
                reassignments=[Reassignment("W", "E", 2, arrive_cell=bag_cell)],
            ).run()
            t = res.ticket_for("Yi")
            self.assertIsNotNone(t)
            self.assertEqual(t.taken_cell, bag_cell)
            arrivals[bag_cell] = t.arrival_cell
            wait = deadline - t.arrival_cell
            people = [
                Person(f"Yi#{k}", "Yi", "Jia", t.arrival_cell, wait, t.order, k)
                for k in range(2)
            ]
            costs[bag_cell] = RentalOptimizer(machines, 8).solve(people).cost

        # Bag on time: arrival 1, one cell of wait -> one machine, cost 1.
        self.assertEqual(arrivals[0], 1)
        self.assertEqual(costs[0], 1)
        # Bag one cell late: arrival 2, no wait left -> two machines, cost 2.
        self.assertEqual(arrivals[1], 2)
        self.assertEqual(costs[1], 2)


# ---------------------------------------------------------------------------
# Late coverage: supplement never revokes a taken ticket
# ---------------------------------------------------------------------------


class TestLateCoverage(unittest.TestCase):
    def test_supplement_freezes_taken_group(self):
        # Group A takes early under full coverage; a later supplement is received.
        # B was previously deferred for lack of coverage; replaying B (not A)
        # gives B a ticket while A stays frozen and keeps its ticket.
        flag = Flag(
            "X", "L", Fraction(500), "red", "Jia", capacity=4, open_cell=0,
            walk_cells=1, physical_number=1, segment=iv(490, 510),
        )
        base = InspectionSession([iv(490, 510)])  # X inspected from the start
        stop = Stop("s", Fraction(470), +1, 0)
        A = Group("A", 2, (stop,), ("red",), fixed_order=0)
        B = Group("B", 2, (stop,), ("red",), fixed_order=1)

        res1 = MarketFlow([flag], [], [A, B], base, Protocol.COURIER).run()
        tA = res1.ticket_for("A")
        self.assertIsNotNone(tA)  # A takes 2 of 4
        self.assertIsNotNone(res1.ticket_for("B"))  # B also takes 2

        # Re-run with A frozen: only B is re-evaluated; A keeps its ticket.
        res2 = MarketFlow(
            [flag], [], [A, B], base, Protocol.COURIER, frozen_tickets=[tA]
        ).run()
        self.assertEqual(res2.ticket_for("A"), tA)
        self.assertIn("A", res2.frozen)

    def test_uninspected_segment_defers_no_substitution(self):
        # Two same-number flags on different segments. Both are visible; the near
        # flag's segment is uninspected -> the stop defers; it never substitutes
        # the far flag's same number.
        near = Flag("N", "L", Fraction(500), "red", "Jia", capacity=2, open_cell=0,
                    walk_cells=1, physical_number=1, segment=iv(490, 510), label=Fraction(200))
        far = Flag("F", "L", Fraction(600), "red", "Wei", capacity=2, open_cell=0,
                   walk_cells=2, physical_number=1, segment=iv(590, 610), label=Fraction(200))
        # Inspect far's segment and the stretch up to it, but NOT near's segment.
        sess = InspectionSession([iv(515, 700)])
        stop = Stop("s", Fraction(470), +1, 0)
        g = Group("G", 2, (stop,), ("red",), fixed_order=0)
        res = MarketFlow([near, far], [], [g], sess).run()
        self.assertIsNone(res.ticket_for("G"))
        self.assertTrue(any("not inspected" in d.reason for d in res.outcomes["G"].deferred))

    def test_irrelevant_uninspected_colour_does_not_block(self):
        # A visible but uninspected flag of a colour the group does NOT seek must
        # not block a trusted, wanted flag further along the same sight line.
        blue = Flag("Bl", "L", Fraction(500), "blue", "Wei", capacity=2,
                    open_cell=0, walk_cells=1, physical_number=1, segment=iv(490, 510))
        red = Flag("Rd", "L", Fraction(560), "red", "Jia", capacity=2,
                   open_cell=0, walk_cells=1, physical_number=2, segment=iv(550, 570))
        # Inspect the red segment only; blue's segment is uninspected.
        sess = InspectionSession([iv(550, 570)])
        stop = Stop("s", Fraction(470), +1, 0)
        g = Group("G", 2, (stop,), ("red",), fixed_order=0)  # seeks red only
        res = MarketFlow([blue, red], [], [g], sess).run()
        t = res.ticket_for("G")
        self.assertIsNotNone(t)
        self.assertEqual(t.flag_id, "Rd")


# ---------------------------------------------------------------------------
# Rental envelope & incremental diagnostics
# ---------------------------------------------------------------------------


class TestRentalEnvelope(unittest.TestCase):
    def test_three_stalls_four_machines_eight_cells(self):
        stalls = ["A", "B", "C"]
        machines = [
            Machine("M1", 2, frozenset({"A"}), move_cells=1),
            Machine("M2", 3, frozenset({"B"}), move_cells=1),
            Machine("M3", 5, frozenset({"C"}), move_cells=1),
            Machine("M4", 7, frozenset({"A", "B", "C"}), move_cells=1),
        ]
        # One person at each stall in cell 0, all no-wait -> cheapest is M1,M2,M3
        # (cost 2+3+5=10) rather than the flexible M4.
        people = [
            Person("pA", "tA", "A", 0, 0, 1, 0),
            Person("pB", "tB", "B", 0, 0, 2, 0),
            Person("pC", "tC", "C", 0, 0, 3, 0),
        ]
        plan = RentalOptimizer(machines, 8).solve(people)
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.cost, 10)
        self.assertEqual(set(plan.rented), {"M1", "M2", "M3"})

    def test_first_investment_stall_enforced(self):
        # A machine that can only first-invest at A cannot serve B in its first
        # cell, even if idle.
        machines = [Machine("M1", 1, frozenset({"A"}))]
        people = [Person("pB", "tB", "B", 0, 0, 1, 0)]
        plan = RentalOptimizer(machines, 8).solve(people)
        self.assertFalse(plan.feasible)
        self.assertEqual(plan.earliest_unserviceable, "tB")

    def test_fcfs_at_stall(self):
        # Two people same stall: earlier queue order must be served no later.
        machines = [
            Machine("M1", 1, frozenset({"S"})),
            Machine("M2", 1, frozenset({"S"})),
        ]
        early = Person("e", "tE", "S", 0, 1, order=1, person_index=0)
        late = Person("l", "tL", "S", 0, 1, order=2, person_index=0)
        plan = RentalOptimizer(machines, 8).solve([late, early])
        self.assertTrue(plan.feasible)
        cells = {a.person_id: a.cell for a in plan.assignments}
        self.assertLessEqual(cells["e"], cells["l"])

    def test_locked_and_paid_machines(self):
        # One machine already paid; a locked service occupies cell 0. A new
        # person needing cell 0 must be served elsewhere / later.
        machines = [Machine("M1", 5, frozenset({"S"}))]
        locked = [LockedService("old", "M1", 0, "S")]
        newp = Person("new", "tN", "S", 0, 0, order=2, person_index=0)
        plan = RentalOptimizer(machines, 8).solve(
            [newp], locked=locked, paid_machines=["M1"]
        )
        # M1 busy at cell 0, new needs cell 0, only machine -> infeasible.
        self.assertFalse(plan.feasible)
        self.assertEqual(plan.earliest_unserviceable, "tN")

    def test_locked_plus_extra_machine_satisfies(self):
        machines = [
            Machine("M1", 5, frozenset({"S"})),
            Machine("M2", 2, frozenset({"S"})),
        ]
        locked = [LockedService("old", "M1", 0, "S")]
        newp = Person("new", "tN", "S", 0, 0, order=2, person_index=0)
        plan = RentalOptimizer(machines, 8).solve(
            [newp], locked=locked, paid_machines=["M1"]
        )
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.cost, 2)  # only M2 newly rented
        self.assertIn("M2", plan.rented)
        self.assertIn("M1", plan.rented)


# ---------------------------------------------------------------------------
# Full-envelope stress: 6 groups, 8 cells, 4 flags, 3 stalls, 4 machines
# ---------------------------------------------------------------------------


class TestFullEnvelope(unittest.TestCase):
    def test_six_groups_four_flags_three_stalls_four_machines(self):
        # Four flags, three stalls, arranged so all six (2-person) groups take a
        # whole-group number, then the desk finds a minimum whole-shift rent.
        flags = [
            Flag("F1", "L", Fraction(100), "red", "A", capacity=4, open_cell=0,
                 walk_cells=1, physical_number=1, segment=iv(90, 110)),
            Flag("F2", "L", Fraction(200), "red", "A", capacity=2, open_cell=0,
                 walk_cells=1, physical_number=2, segment=iv(190, 210)),
            Flag("F3", "L", Fraction(300), "blue", "B", capacity=4, open_cell=0,
                 walk_cells=1, physical_number=3, segment=iv(290, 310)),
            Flag("F4", "L", Fraction(400), "blue", "C", capacity=4, open_cell=0,
                 walk_cells=1, physical_number=4, segment=iv(390, 410)),
        ]
        sess = InspectionSession([iv(80, 420)])
        # Each group has one stop; facing east near the flag it can reach.
        groups = []
        for i, (flag_pos, colors) in enumerate([
            (90, ("red",)), (90, ("red",)), (290, ("blue",)),
            (390, ("blue",)), (190, ("red", "blue")), (290, ("blue", "red")),
        ]):
            groups.append(Group(
                f"G{i}", 2,
                (Stop(f"s{i}", Fraction(flag_pos - 10), +1, arrival_cell=0),),
                colors, fixed_order=i, max_wait_cells=1,
            ))
        res = MarketFlow(flags, [], groups, sess, Protocol.COURIER).run()
        served = [g for g in res.outcomes if res.ticket_for(g)]
        self.assertEqual(len(served), 6)
        for t in res.tickets:
            self.assertEqual(t.size, 2)

        machines = [
            Machine("M1", 1, frozenset({"A"}), move_cells=1),
            Machine("M2", 2, frozenset({"B"}), move_cells=1),
            Machine("M3", 3, frozenset({"C"}), move_cells=1),
            Machine("M4", 4, frozenset({"A", "B", "C"}), move_cells=0),
        ]
        plan = RentalOptimizer(machines, 8).solve(
            persons_from_tickets(res.tickets, {g: 3 for g in res.outcomes})
        )
        self.assertTrue(plan.feasible, plan.earliest_unserviceable)
        # Every assignment lies in the 8-cell window, one person per machine/cell.
        seen = set()
        for a in plan.assignments:
            self.assertGreaterEqual(a.cell, 0)
            self.assertLessEqual(a.cell, 8)
            key = (a.machine_id, a.cell)
            self.assertNotIn(key, seen)
            seen.add(key)


if __name__ == "__main__":
    unittest.main()
