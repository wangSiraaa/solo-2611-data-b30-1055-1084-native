"""Unittest suite for the leaf-assembly support and photography subsystem.

Covers the documented cases the job brief specifies:

* support table ``{00,10,11}``; ``00 -> 10 -> 11`` one seam at a time;
* a new signed measurement leaving ``{00,11}`` keeps ``11`` statically safe but
  makes it unreachable;
* patch-strip conflicts name the strip and its original page;
* a new patch measurement must be signed by the current custodian, and an
  unconfirmed transfer does not change that authority;
* housing limits (<= 6 seams, <= 3 levels, <= 4 targets);
* the single-clamp ordinary machine must retract before relocating a clamp;
* the double-clamp machine does engage-before-withdraw, and the engage cannot
  be skipped;
* a new clamp position that hides the right column still blocks the frame;
* a standby station with a permanently-barred right column can never shoot;
* every intermediate pose on a route is validated.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from archive_handoff.binding import (
    PatchMeasurement,
    SupportModel,
    make_assembly,
    pose_str,
)
from archive_handoff.binding import Seam
from archive_handoff.models import ConflictError, RejectError
from archive_handoff.photography import (
    ClampJigPlanner,
    PhotoStation,
    TargetShot,
    stop_frame_probe,
)


def two_seam(allowed, hold_positions=(), hold_at=(), start=(0, 0), members=("B1",)):
    return make_assembly(
        "EX", "V1",
        seams=[{"seam_id": "L", "levels": [0, 1]},
               {"seam_id": "R", "levels": [0, 1]}],
        strips=[{"strip_id": "RT", "doc_id": "B1", "leaf_group": "G",
                 "seams": ["L", "R"], "allowed": allowed,
                 "hold_positions": list(hold_positions),
                 "hold_at": [list(t) for t in hold_at]}],
        start_pose=list(start),
        members=members,
    )


def station(slots=1, columns=(1, 2, 3, 4, 5), left=(1,), right=()):
    return PhotoStation("T", "LOCAL", columns=columns,
                        left_bar=frozenset(left), right_bar=frozenset(right),
                        clamp_slots=slots)


class SupportTable(unittest.TestCase):
    def test_table_00_10_11(self):
        model = SupportModel(two_seam([[0, 0], [1, 0], [1, 1]]))
        self.assertEqual([pose_str(p) for p in model.support_table()],
                         ["00", "10", "11"])

    def test_route_is_one_seam_at_a_time(self):
        model = SupportModel(two_seam([[0, 0], [1, 0], [1, 1]]))
        route = model.shortest_route((0, 0), (1, 1))
        self.assertEqual(len(route), 2)
        self.assertEqual(route[0]["seam_id"], "L")
        self.assertEqual(route[1]["seam_id"], "R")

    def test_unsupported_target_has_no_route(self):
        # Only 00 and 11 supported: 11 is not reachable from 00 in one move.
        model = SupportModel(two_seam([[0, 0], [1, 1]]))
        self.assertEqual([pose_str(p) for p in model.support_table()], ["00", "11"])
        self.assertTrue(model.is_supported((1, 1)))
        self.assertIsNone(model.shortest_route((0, 0), (1, 1)))

    def test_route_validates_every_intermediate_pose(self):
        # A direct 00->11 would be two seams; ensure every step is supported.
        model = SupportModel(two_seam([[0, 0], [1, 0], [1, 1]]))
        route = model.shortest_route((0, 0), (1, 1))
        pose = [0, 0]
        for step in route:
            idx = model.index[step["seam_id"]]
            pose[idx] = step["to"]
            self.assertTrue(model.is_supported(tuple(pose)), f"pose {pose}")


class QueriesAndConflicts(unittest.TestCase):
    def test_combination_witness(self):
        model = SupportModel(two_seam([[0, 0], [1, 0], [1, 1]]))
        res = model.query_combination({"L": 1})
        self.assertTrue(res["holdable"])
        self.assertIn(res["witness_pose"], {"10", "11"})

    def test_combination_conflict_names_strip_and_page(self):
        model = SupportModel(two_seam([[0, 0], [1, 1]]))
        # L=1, R=0 is not in {00,11}; the strip (page B1) is the conflict.
        res = model.query_combination({"L": 1, "R": 0})
        self.assertFalse(res["holdable"])
        self.assertEqual(len(res["conflicts"]), 1)
        self.assertEqual(res["conflicts"][0]["strip_id"], "RT")
        self.assertEqual(res["conflicts"][0]["doc_id"], "B1")

    def test_seam_query_lists_levels(self):
        model = SupportModel(two_seam([[0, 0], [1, 1]]))
        seam = model.query_seam("R")
        self.assertEqual([lv["level"] for lv in seam["levels"]], [0, 1])


class MeasurementSigning(unittest.TestCase):
    def _model(self, custodian="LOCAL"):
        return SupportModel(two_seam([[0, 0], [1, 0], [1, 1]]),
                            custodian_of=lambda c: custodian)

    def test_custodian_can_sign_and_changes_table(self):
        model = self._model()
        res = model.submit_measurement(
            PatchMeasurement("M1", 1, "V1", 0, "LOCAL", {"RT": ((0, 0), (1, 1))})
        )
        self.assertTrue(res["changed"])
        self.assertEqual([pose_str(p) for p in model.support_table()], ["00", "11"])
        self.assertTrue(model.is_supported((1, 1)))
        self.assertFalse(model.target_reachable((0, 0), (1, 1)))

    def test_non_custodian_rejected(self):
        model = self._model(custodian="LOCAL")
        with self.assertRaises(RejectError) as ctx:
            model.submit_measurement(
                PatchMeasurement("M2", 1, "V1", 0, "OUTPOST", {"RT": ((0, 0),)})
            )
        self.assertEqual(ctx.exception.code, "unsigned_measurement")

    def test_replay_and_conflict(self):
        model = self._model()
        model.submit_measurement(
            PatchMeasurement("M1", 1, "V1", 0, "LOCAL", {"RT": ((0, 0), (1, 1))})
        )
        again = model.submit_measurement(
            PatchMeasurement("M1", 1, "V1", 0, "LOCAL", {"RT": ((0, 0), (1, 1))})
        )
        self.assertTrue(again["replayed"])
        with self.assertRaises(ConflictError):
            model.submit_measurement(
                PatchMeasurement("M1", 1, "V1", 0, "LOCAL", {"RT": ((0, 0),)})
            )
        # A newer revision supersedes; an older revision afterwards is stale.
        model.submit_measurement(
            PatchMeasurement("M1", 2, "V1", 0, "LOCAL", {"RT": ((0, 0),)})
        )
        with self.assertRaises(ConflictError):
            model.submit_measurement(
                PatchMeasurement("M1", 1, "V1", 0, "LOCAL", {"RT": ((0, 0), (1, 1))})
            )

    def test_custody_authority_follows_confirmed_holder(self):
        holder = {"who": "LOCAL"}
        model = SupportModel(two_seam([[0, 0], [1, 0], [1, 1]]),
                             custodian_of=lambda c: holder["who"])
        model.submit_measurement(
            PatchMeasurement("M1", 1, "V1", 0, "LOCAL", {"RT": ((0, 0),)})
        )
        holder["who"] = "OUTPOST"  # confirmed transfer happened
        with self.assertRaises(RejectError):
            model.submit_measurement(
                PatchMeasurement("M2", 1, "V1", 0, "LOCAL", {"RT": ((0, 0), (1, 1))})
            )
        # The new holder can sign.
        model.submit_measurement(
            PatchMeasurement("M3", 1, "V1", 0, "OUTPOST", {"RT": ((0, 0), (1, 1))})
        )


class HousingLimits(unittest.TestCase):
    def test_too_many_seams(self):
        with self.assertRaises(RejectError):
            make_assembly("X", "V1",
                          seams=[{"seam_id": f"S{i}", "levels": [0, 1]} for i in range(7)],
                          strips=[], start_pose=[0] * 7)

    def test_too_many_levels(self):
        with self.assertRaises(RejectError):
            make_assembly("X", "V1",
                          seams=[{"seam_id": "S", "levels": [0, 1, 2, 3]}],
                          strips=[], start_pose=[0])

    def test_seam_needs_handover_level(self):
        with self.assertRaises(RejectError):
            make_assembly("X", "V1",
                          seams=[{"seam_id": "S", "levels": [1, 2]}],
                          strips=[], start_pose=[1])

    def test_strip_uses_unapproved_level(self):
        with self.assertRaises(RejectError):
            make_assembly("X", "V1",
                          seams=[{"seam_id": "S", "levels": [0, 1]}],
                          strips=[{"strip_id": "P", "doc_id": "B1", "seams": ["S"],
                                   "allowed": [[2]]}],
                          start_pose=[0])

    def test_too_many_targets(self):
        model = SupportModel(two_seam([[0, 0], [1, 1]]))
        planner = ClampJigPlanner(station(), model)
        with self.assertRaises(RejectError):
            planner.plan([TargetShot(f"t{i}", "L", 0) for i in range(5)])


class SingleClampMachine(unittest.TestCase):
    def test_single_clamp_relocates_only_after_retract(self):
        # RT active at 10 and 11; hold can be at col 4 (clear) or 5 (hides).
        model = SupportModel(
            two_seam([[0, 0], [1, 0], [1, 1]], hold_positions=(4, 5),
                     hold_at=((1, 0), (1, 1)))
        )
        target = TargetShot("sh", "R", 1, must_see=frozenset({2, 3, 5}))
        planner = ClampJigPlanner(station(slots=1), model, initial_clamps=[5])
        plan = planner.plan([target], initial_pose=[1, 0])
        self.assertTrue(plan["feasible"])
        kinds = [a["action"] for a in plan["actions"]]
        # The clamp must leave col 5 only after every seam is back at 00.
        withdraw = next(i for i, a in enumerate(plan["actions"])
                        if a["action"] == "remove_clamp")
        self.assertEqual(plan["actions"][withdraw]["pre"]["pose"], "00")

    def test_hold_relocation_skipped_engage_not_allowed(self):
        # With two clamps, a direct remove->add (skipping engage) must not be the
        # chosen route when support would drop; the planner always engages first.
        model = SupportModel(
            two_seam([[0, 0], [1, 0], [1, 1]], hold_positions=(4, 5),
                     hold_at=((1, 1),))
        )
        target = TargetShot("sh", "R", 1, must_see=frozenset({2, 3, 5}))
        planner = ClampJigPlanner(station(slots=2), model, initial_clamps=[5])
        plan = planner.plan([target], initial_pose=[1, 0])
        self.assertTrue(plan["feasible"])
        adds = [a for a in plan["actions"] if a["action"] == "add_clamp"]
        removes = [a for a in plan["actions"] if a["action"] == "remove_clamp"]
        self.assertTrue(adds and removes)
        # engage (add col 4) precedes withdraw (remove col 5)
        first_add = next(i for i, a in enumerate(plan["actions"])
                         if a["action"] == "add_clamp")
        first_remove = next(i for i, a in enumerate(plan["actions"])
                            if a["action"] == "remove_clamp")
        self.assertLess(first_add, first_remove)


class DoubleClampHandover(unittest.TestCase):
    def test_handover_two_safe_states(self):
        model = SupportModel(
            two_seam([[0, 0], [1, 0], [1, 1]], hold_positions=(4, 5),
                     hold_at=((1, 0), (1, 1)))
        )
        planner = ClampJigPlanner(station(slots=2), model, initial_clamps=[5])
        states = planner.handover_states((1, 0), frozenset({5}),
                                         new_column=4, old_column=5)
        self.assertEqual(states[0]["step"], "engage_new")
        self.assertEqual(states[0]["clamps"], [4, 5])
        self.assertTrue(states[0]["safe"])
        self.assertEqual(states[1]["step"], "withdraw_old")
        self.assertEqual(states[1]["clamps"], [4])
        self.assertTrue(states[1]["safe"])

    def test_handover_needs_free_slot(self):
        model = SupportModel(
            two_seam([[0, 0], [1, 0], [1, 1]], hold_positions=(4, 5),
                     hold_at=((1, 0),))
        )
        planner = ClampJigPlanner(station(slots=1), model, initial_clamps=[5])
        with self.assertRaises(RejectError) as ctx:
            planner.handover_states((1, 0), frozenset({5}),
                                    new_column=4, old_column=5)
        self.assertEqual(ctx.exception.code, "no_free_slot")

    def test_new_clamp_that_occludes_right_still_unshootable(self):
        # Only hold position is col 5, which hides the must-see right column.
        model = SupportModel(
            two_seam([[0, 0], [1, 0], [1, 1]], hold_positions=(5,),
                     hold_at=((1, 1),))
        )
        target = TargetShot("sh", "R", 1, must_see=frozenset({2, 3, 5}))
        planner = ClampJigPlanner(station(slots=2), model, initial_clamps=[5])
        plan = planner.plan([target], initial_pose=[1, 0])
        self.assertFalse(plan["feasible"])
        self.assertEqual(plan["targets"][0]["reason"], "hold_occludes_must_see")


class StandbyStation(unittest.TestCase):
    def test_permanently_barred_column_unshootable(self):
        model = SupportModel(
            two_seam([[0, 0], [1, 0], [1, 1]], hold_positions=(4, 5),
                     hold_at=((1, 0), (1, 1)))
        )
        target = TargetShot("sh", "R", 1, must_see=frozenset({2, 3, 5}))
        standby = PhotoStation("STANDBY", "OUTPOST", columns=(1, 2, 3, 4, 5),
                               left_bar=frozenset({1}), right_bar=frozenset({5}),
                               clamp_slots=2)
        plan = ClampJigPlanner(standby, model, initial_clamps=[5]).plan(
            [target], initial_pose=[1, 0])
        self.assertFalse(plan["feasible"])
        self.assertEqual(plan["targets"][0]["reason"], "column_occluded")

    def test_stop_frame_probe_reports_bar_occlusion(self):
        target = TargetShot("sh", "R", 1, must_see=frozenset({2, 3, 5}))
        standby = PhotoStation("STANDBY", "OUTPOST", columns=(1, 2, 3, 4, 5),
                               left_bar=frozenset({1}), right_bar=frozenset({5}),
                               clamp_slots=2)
        probe = stop_frame_probe(standby, target, supported=True, hold_ok=True)
        self.assertEqual(probe["bar_occludes_must_see"], [5])
        self.assertFalse(probe["shootable_before_pad_swap"])

    def test_stop_frame_pad_swap_keeps_old_limits_until_swap(self):
        target = TargetShot("sh", "R", 1, must_see=frozenset({2, 3, 5}))
        standby = PhotoStation("STANDBY", "OUTPOST", columns=(1, 2, 3, 4, 5),
                               left_bar=frozenset({1}), right_bar=frozenset({5}),
                               clamp_slots=2)
        probe = stop_frame_probe(
            standby, target, supported=True, hold_ok=True,
            pad_swap={"pad": "P3", "stop": "S2", "qualified_initial_pose": True},
        )
        swap = probe["pad_swap"]
        self.assertTrue(swap["old_limits_valid_until_swap"])
        # Even after a valid pad swap the bars still hide the column.
        self.assertFalse(swap["shootable_only_after_swap"])


class DisconnectedGoal(unittest.TestCase):
    def test_statically_safe_but_unreachable(self):
        # Support only {00, 11}: 11 is safe but a two-seam jump from 00.
        model = SupportModel(two_seam([[0, 0], [1, 1]]))
        target = TargetShot("sh", "R", 1, must_see=frozenset({2}))
        plan = ClampJigPlanner(station(slots=1), model).plan([target])
        self.assertFalse(plan["feasible"])
        self.assertEqual(plan["targets"][0]["reason"], "no_route")


class JigDistinction(unittest.TestCase):
    def _model(self):
        # Support {00,10,11}; no holds needed.
        return SupportModel(two_seam([[0, 0], [1, 0], [1, 1]]))

    def _targets(self):
        return [
            TargetShot("tL", "L", 1, frozenset({2})),
            TargetShot("tR", "R", 1, frozenset({2})),
        ]

    def test_single_clamp_retracts_between_pages(self):
        model = self._model()
        plan = ClampJigPlanner(station(slots=1), model).plan(self._targets())
        self.assertTrue(plan["feasible"])
        poses = [a["post"]["pose"] for a in plan["actions"]]
        # After the first shot (page L), the machine must return to 00 before
        # shooting page R.
        first_shoot = next(i for i, a in enumerate(plan["actions"])
                           if a["action"] == "shoot")
        second_shoot = next(i for i, a in enumerate(plan["actions"])
                            if a["action"] == "shoot" and i > first_shoot)
        between = [a["post"]["pose"] for a in plan["actions"][first_shoot:second_shoot]]
        self.assertIn("00", between)

    def test_double_clamp_may_pass_through_more_angles(self):
        model = self._model()
        plan = ClampJigPlanner(station(slots=2), model).plan(self._targets())
        self.assertTrue(plan["feasible"])
        first_shoot = next(i for i, a in enumerate(plan["actions"])
                           if a["action"] == "shoot")
        second_shoot = next(i for i, a in enumerate(plan["actions"])
                            if a["action"] == "shoot" and i > first_shoot)
        between = [a["post"]["pose"] for a in plan["actions"][first_shoot:second_shoot]]
        # The bracket keeps support and steps 10 -> 11 directly; it does NOT
        # need to retract all the way to the handover pose 00.
        self.assertNotIn("00", between)
        self.assertIn("11", between)


class ShotRecord(unittest.TestCase):
    def test_each_frame_records_page_pose_and_visible_columns(self):
        model = SupportModel(
            two_seam([[0, 0], [1, 0], [1, 1]], hold_positions=(4,),
                     hold_at=((1, 0),))
        )
        target = TargetShot("sh", "R", 1, must_see=frozenset({2, 3}))
        plan = ClampJigPlanner(station(slots=1), model).plan([target])
        self.assertTrue(plan["feasible"])
        shoots = [a for a in plan["actions"] if a["action"] == "shoot"]
        self.assertEqual(len(shoots), 1)
        detail = shoots[0]["detail"]
        self.assertEqual(detail["shot_id"], "sh")
        self.assertEqual(detail["seam_id"], "R")
        self.assertEqual(detail["level"], 1)
        self.assertIn(2, detail["visible"])
        self.assertIn(3, detail["visible"])

    def test_frames_one_step_each(self):
        # Every movement shifts exactly one seam level or one clamp.
        model = SupportModel(two_seam([[0, 0], [1, 0], [1, 1]]))
        target = TargetShot("sh", "R", 1, must_see=frozenset({2}))
        plan = ClampJigPlanner(station(slots=1), model).plan([target])
        for a in plan["actions"]:
            pre, post = a["pre"]["pose"], a["post"]["pose"]
            if a["action"] == "open_seam":
                diffs = [i for i in range(len(pre)) if pre[i] != post[i]]
                self.assertEqual(len(diffs), 1, f"{a}")
            elif a["action"] == "shoot":
                self.assertEqual(pre, post)


if __name__ == "__main__":
    unittest.main()
