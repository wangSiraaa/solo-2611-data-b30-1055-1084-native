"""Unit tests for the schematic water-clock engine and collation case."""

from __future__ import annotations

import os
import tempfile
import unittest

from archive_handoff import HandoffRepo, RejectError, RuleEventStore
from archive_handoff.clepsydra import (
    STOP,
    Entry,
    Witness,
    determined_beats,
    earliest_discerning_beat,
    explain,
    flatten_policy,
    forward_direction,
    full_direction,
    plan_adaptive,
    plan_fixed_cups,
    possible_ring_patterns,
    reversed_direction,
    ring_pattern,
    run_clock,
    run_stand,
)
from archive_handoff.clepsydra_case import (
    FORMAL,
    PROVISIONAL,
    WaterClockCase,
)
from archive_handoff.clepsydra_scenario import run_clepsydra_demo
from archive_handoff.materials import import_materials


class RunClockTests(unittest.TestCase):
    def test_example_two_book(self):
        # opening 0, scale 3, inflows 1,1,1 -> only beat 2 rings.
        self.assertEqual(ring_pattern(3, 0, [1, 1, 1]),
                         (False, False, True))

    def test_pour_leaves_remainder(self):
        beats = run_clock(3, 2, [1])
        self.assertTrue(beats[0].ring)
        self.assertEqual(beats[0].after, 0)
        beats = run_clock(5, 3, [4])
        self.assertTrue(beats[0].ring)
        self.assertEqual(beats[0].after, 2)

    def test_stop_adds_nothing(self):
        self.assertEqual(ring_pattern(3, 1, [STOP, STOP]), (False, False))

    def test_inflow_bounds_enforced(self):
        with self.assertRaises(ValueError):
            run_clock(3, 0, [3])
        with self.assertRaises(ValueError):
            run_clock(3, 3, [1])


class StandTests(unittest.TestCase):
    def test_cup_adds_one_before_inflow(self):
        # stand [1,0,1] opening 0, cup at 2 -> (F,F,T)
        self.assertEqual(
            [b.ring for b in run_stand(3, 0, [1, 0, 1], cup_beats={2})],
            [False, False, True],
        )

    def test_gate_stops_natural_inflow(self):
        beats = run_stand(3, 0, [1, 1, 1], gate_beats={1})
        self.assertEqual(beats[1].inflow, 0)

    def test_gate_and_cup_cannot_share_beat(self):
        with self.assertRaises(ValueError):
            run_stand(3, 0, [1, 1, 1], cup_beats={1}, gate_beats={1})


class CollationTests(unittest.TestCase):
    def _book_witness(self, inflows):
        return Witness("book", [Entry(inflow=i) for i in inflows],
                       [full_direction(len(inflows))])

    def test_single_full_copy(self):
        w = Witness("c", [Entry(inflow=1, ring=False), Entry(inflow=1, ring=False),
                          Entry(inflow=1, ring=True)],
                    [full_direction(3)], initial_span=(0, 0))
        sol = explain(3, 3, [self._book_witness([1, 1, 1]), w])
        self.assertIsNotNone(sol)
        self.assertEqual(sol.opening, 0)
        self.assertEqual(list(sol.inflows), [1, 1, 1])

    def _pin_zero(self, beats=3):
        return Witness("pin", [Entry(obs=(0,))], [forward_direction(1, 0)],
                       initial_span=(0, 0))

    def test_fragment_picks_consistent_direction(self):
        # Pin opening 0 so the 1,1,1 book rings only at beat 2; the fragment's
        # silent,ring entries then bind only to (1,2).
        frag = Witness("frag", [Entry(inflow=1, ring=False),
                                Entry(inflow=1, ring=True)],
                       [forward_direction(2, 0), forward_direction(2, 1)])
        sol = explain(3, 3, [self._book_witness([1, 1, 1]), self._pin_zero(), frag])
        self.assertIsNotNone(sol)
        self.assertEqual(sol.opening, 0)
        self.assertEqual(sol.directions["frag"], {0: 1, 1: 2})

    def test_conflicting_witness_has_no_solution(self):
        bad = Witness("bad", [Entry(inflow=2)], [forward_direction(1, 1)])
        self.assertIsNone(explain(3, 3, [self._book_witness([1, 1, 1]), bad]))

    def test_observation_intersects(self):
        # Two copies each allow {1,2}; intersection narrows nothing but stays ok.
        a = Witness("a", [Entry(obs=(1, 2))], [forward_direction(1, 0)],
                    initial_span=(0, 2))
        b = Witness("b", [Entry(obs=(0, 1))], [forward_direction(1, 0)],
                    initial_span=(0, 2))
        sol = explain(3, 3, [self._book_witness([0, 0, 0]), a, b])
        self.assertIsNotNone(sol)
        self.assertEqual(sol.opening, 1)

    def test_reversed_direction(self):
        # A backward-transcribed fragment: local ring,silent at (1,2) needs the
        # reversed binding {0:2,1:1}.  Pin opening 0 so forward (0,1) is
        # inconsistent (beat 0 cannot ring) and the reverse is forced.
        frag = Witness("frag", [Entry(inflow=1, ring=True),
                                Entry(inflow=1, ring=False)],
                       [forward_direction(2, 0), reversed_direction(2, 2)])
        sol = explain(3, 3, [self._book_witness([1, 1, 1]), self._pin_zero(), frag])
        self.assertIsNotNone(sol)
        self.assertEqual(sol.directions["frag"], {0: 2, 1: 1})

    def test_disputed_beats(self):
        w = Witness("seen", [Entry(ring=False)],
                    [forward_direction(1, 0)], initial_span=(0, 1))
        patterns = possible_ring_patterns(3, 3, [self._book_witness([1, 1, 1]), w])
        det, disp = determined_beats(patterns, 3)
        self.assertEqual(det, {0: False})
        self.assertEqual(disp, {1, 2})


class PlanTests(unittest.TestCase):
    def test_fixed_cup_needed_at_beat_two(self):
        target = {0: False, 1: False, 2: True}
        plan = plan_fixed_cups(3, [1, 0, 1], {0}, target, allowed_cups={2})
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.cup_beats, (2,))
        none = plan_fixed_cups(3, [1, 0, 1], {0}, target, allowed_cups=set())
        self.assertFalse(none.feasible)

    def test_no_secret_split_in_bracket(self):
        # Per-opening plans exist; a single fixed list for {0,1} does not.
        target = {0: False, 1: False, 2: True}
        for o in (0, 1):
            self.assertTrue(plan_fixed_cups(
                3, [1, 0, 0], {o}, target, allowed_cups={0, 1, 2}).feasible)
        joint = plan_fixed_cups(
            3, [1, 0, 0], {0, 1}, target, allowed_cups={0, 1, 2})
        self.assertFalse(joint.feasible)

    def test_safe_cup_must_wait(self):
        target = {0: False, 1: False, 2: True}
        self.assertFalse(plan_fixed_cups(
            3, [1, 0, 1], {0, 1}, target, allowed_cups={0}).feasible)
        self.assertFalse(plan_fixed_cups(
            3, [1, 0, 1], {0, 1}, target, allowed_cups={1}).feasible)
        self.assertTrue(plan_fixed_cups(
            3, [1, 0, 1], {0, 1}, target, allowed_cups={2}).feasible)
        self.assertEqual(earliest_discerning_beat(3, {0, 1}, [1, 0, 1]),
                         (2, "ring"))

    def test_disputed_beat_cannot_be_asserted(self):
        plan = plan_fixed_cups(3, [1, 0, 1], {0}, {0: False, 1: False},
                               disputed={2}, allowed_cups={2})
        self.assertFalse(plan.feasible)

    def test_adaptive_policy_branches(self):
        pol = plan_adaptive(3, [1, 0, 1], {0, 1},
                            {0: False, 1: False, 2: True},
                            allowed_gates={1, 2}, allowed_cups={2})
        self.assertTrue(pol["feasible"])
        paths = flatten_policy(pol["tree"])
        self.assertTrue(paths)
        # Every leaf path has one action per beat.
        for p in paths:
            self.assertEqual(len(p), 3)


class CaseCustodyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rules = RuleEventStore(os.path.join(self.tmp.name, "r.db"))
        self.handoff = HandoffRepo(os.path.join(self.tmp.name, "h.db"))
        self.addCleanup(self.rules.close)
        self.addCleanup(self.handoff.close)
        import_materials(self.rules, self.handoff, "materials/751")
        self.rules.import_holds([
            {"hold_id": "H1", "revision": 2, "case_id": "C1",
             "start": 25, "end": 35},
        ])
        product = self.rules.build_product(40)
        self.rules.save_product("Q40", product)
        self.handoff.ingest_product("Q40", product)
        self.case = WaterClockCase("C1", 3, 3, [1, 1, 1])

    def test_new_reading_provisional_while_not_holder(self):
        rev = self.case.submit_revision("R", 2, STOP, "NEW", self.handoff)
        self.assertEqual(rev.status, PROVISIONAL)
        self.assertEqual(list(self.case.effective_inflows()), [1, 1, 1])
        self.assertEqual(self.case.pending_changes()[0]["proposed"], 0)

    def test_old_institution_resigns_old_reading_not_new(self):
        self.case.submit_revision("R", 2, STOP, "NEW", self.handoff)
        old = self.case.resign("R", "LOCAL", self.handoff)
        self.assertEqual(old.status, FORMAL)
        self.assertEqual(old.value, 1)  # old reading, not the stop
        self.assertEqual(list(self.case.effective_inflows()), [1, 1, 1])

    def test_promotion_waits_for_whole_case(self):
        self.case.submit_revision("R", 2, STOP, "NEW", self.handoff)
        self.assertEqual(self.case.promote_pending(self.handoff), [])
        self.handoff.submit({
            "handoff_id": "W", "case_id": "C1", "doc_ids": ["D1", "D2"],
            "from": "LOCAL", "to": "NEW", "as_of_day": 40,
        })
        promoted = self.case.promote_pending(self.handoff)
        self.assertTrue(promoted)
        self.assertEqual(list(self.case.effective_inflows()), [1, 1, 0])
        self.assertEqual(ring_pattern(3, 0, [1, 1, 0]),
                         (False, False, False))

    def test_trigger_missing_blocks_promotion(self):
        class FakeCustody:
            def current_custodian(self, case):
                return "NEW"

            def custody_history(self, case):
                return []

        fake = FakeCustody()
        self.case.submit_revision("R", 2, STOP, "NEW", fake)
        self.assertFalse(self.case.holds_whole_case("NEW", fake))
        self.assertEqual(self.case.promote_pending(fake), [])

    def test_invalid_revision_rejected(self):
        with self.assertRaises(RejectError):
            self.case.submit_revision("R", 9, 0, "NEW", self.handoff)
        with self.assertRaises(RejectError):
            self.case.submit_revision("R", 1, 5, "NEW", self.handoff)


class DemoScenario(unittest.TestCase):
    def test_all_checks_pass(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        result = run_clepsydra_demo(os.path.join(tmp.name, "cl"))
        self.assertTrue(result["ok"],
                        [k for k, v in result["checks"].items() if not v])
        self.assertTrue(all(result["checks"].values()))
        # Key documented expectations.
        self.assertTrue(result["checks"]["reproduction_matches_book"]
                        if "reproduction_matches_book" in result["checks"]
                        else result["checks"]["stand_with_cup2_matches"])


if __name__ == "__main__":
    unittest.main()
