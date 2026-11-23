"""Tests for the daytime shadow-puppet stage (shadow_stage + scene_730).

Run:  python3 -m unittest discover -s solution/tests -v
"""

from __future__ import annotations

import os
import sys
import unittest
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scene_730 as S  # noqa: E402
from shadow_stage import (  # noqa: E402
    EAST,
    WEST,
    all_slot_lights,
    certify_slots,
    check_limits,
    compile_show,
    grid_cells,
    light_report,
    move_duration,
    run_plan,
    slot_lighting,
    visible_faces,
)


MATERIALS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "materials", "730",
)


# ---------------------------------------------------------------------------
# Lighting
# ---------------------------------------------------------------------------


def _slot(grid_x=0, walked=True):
    return {"slot_id": "s", "grid_x": grid_x, "position": "0",
            "survey_status": "walked" if walked else "partial"}


def _panel(grid_x, height, surveyed=True):
    return {"panel_id": f"p{grid_x}", "grid_x": grid_x, "height": height,
            "position": "0", "surveyed": surveyed}


class TestLighting(unittest.TestCase):
    def test_height_gt_d_slope_blocks(self):
        # Board west at d=1, light from west slope 1 -> is the up-light panel.
        r = slot_lighting(_slot(0), {"dir": "W", "slope": 1}, [_panel(-1, 2)])
        self.assertEqual(r["status"], "dark")
        self.assertEqual(r["blocker"]["panel_id"], "p-1")
        self.assertEqual(r["blocker"]["grid_distance"], 1)

    def test_equality_leaves_light(self):
        r = slot_lighting(_slot(0), {"dir": "W", "slope": 1}, [_panel(-1, 1)])
        self.assertEqual(r["status"], "lit")

    def test_slope_two_raises_threshold(self):
        # d=1, height 2: 2 > 1*1 dark at slope 1, but 2 > 1*2 false at slope 2.
        panel = [_panel(-1, 2)]
        self.assertEqual(
            slot_lighting(_slot(0), {"dir": "W", "slope": 1}, panel)["status"],
            "dark",
        )
        self.assertEqual(
            slot_lighting(_slot(0), {"dir": "W", "slope": 2}, panel)["status"],
            "lit",
        )

    def test_panel_on_down_light_side_ignored(self):
        # Light from the west: a board east of the slot cannot shade it.
        r = slot_lighting(_slot(0), {"dir": "W", "slope": 1}, [_panel(1, 5)])
        self.assertEqual(r["status"], "lit")

    def test_panel_on_east_side_blocks_when_light_from_east(self):
        r = slot_lighting(_slot(0), {"dir": "E", "slope": 1}, [_panel(1, 5)])
        self.assertEqual(r["status"], "dark")

    def test_farther_tall_board_still_blocks(self):
        # Nearest board (d=1, h=1) is too short: beam passes it.  A farther
        # board (d=3, h=5) still satisfies 5 > 3*1, so the slot is dark and
        # the cited blocker is that farther board.
        r = slot_lighting(
            _slot(0), {"dir": "W", "slope": 1},
            [_panel(-1, 1), _panel(-3, 5)],
        )
        self.assertEqual(r["status"], "dark")
        self.assertEqual(r["blocker"]["panel_id"], "p-3")

    def test_nearest_blocking_board_cited(self):
        # Two boards both tall enough; the nearer one is cited.
        r = slot_lighting(
            _slot(0), {"dir": "W", "slope": 1},
            [_panel(-1, 3), _panel(-2, 5)],
        )
        self.assertEqual(r["status"], "dark")
        self.assertEqual(r["blocker"]["panel_id"], "p-1")
        self.assertEqual(r["blocker"]["grid_distance"], 1)

    def test_unmeasured_board_beyond_shade_is_irrelevant(self):
        # A nearer measured board already blocks; an unmeasured board behind
        # it does not matter (the beam never reaches it).
        r = slot_lighting(
            _slot(0), {"dir": "W", "slope": 1},
            [_panel(-1, 5), _panel(-3, 5, surveyed=False)],
        )
        self.assertEqual(r["status"], "dark")

    def test_missing_measurement_unresolved(self):
        r = slot_lighting(_slot(0), {"dir": "W", "slope": 1},
                          [_panel(-1, 5, surveyed=False)])
        self.assertEqual(r["status"], "unresolved")

    def test_unwalked_slot_unresolved(self):
        r = slot_lighting(_slot(0, walked=False), {"dir": "W", "slope": 1}, [])
        self.assertEqual(r["status"], "unresolved")

    def test_all_slot_lights_per_beat(self):
        env = {
            "slots": {"s": _slot(0)},
            "panels": [_panel(-1, 2)],
            "beats": [
                {"beat": 1, "light": {"dir": "W", "slope": 1}},
                {"beat": 2, "light": {"dir": "W", "slope": 2}},
            ],
        }
        lights = all_slot_lights(env)
        self.assertEqual(lights["s"][1]["status"], "dark")
        self.assertEqual(lights["s"][2]["status"], "lit")

    def test_light_report_names_blocker(self):
        env = S.build_handoff_env(MATERIALS, board_height=2)
        rep = light_report(env)
        self.assertEqual(rep["beats"][1]["slots"]["S"]["status"], "dark")
        self.assertEqual(rep["beats"][1]["slots"]["S"]["blocker"]["height"], "2")
        self.assertEqual(rep["beats"][2]["slots"]["S"]["status"], "lit")


# ---------------------------------------------------------------------------
# Physical certification of slots
# ---------------------------------------------------------------------------


class TestCertification(unittest.TestCase):
    def test_locator_bound_to_physical_position(self):
        reg, assets, report, catalog = S.build_route_and_coverage(MATERIALS)
        env = S.build_handoff_env(MATERIALS)
        cert = env["certification"]["S"]
        self.assertTrue(cert["certified"])
        self.assertEqual(cert["physical_position"], "350")
        # The physical section is recorded (label 350 also names P2 elsewhere).
        self.assertEqual(cert["physical_section"], "P1")
        self.assertEqual(cert["revision_label"], "350")

    def test_outside_coverage_not_certified(self):
        from inspection_library import Asset
        env = {"slots": {"X": {"slot_id": "X", "grid_x": 0, "position": "9999",
                               "locator_asset": "FAR", "survey_status": "walked"}}}
        from chainage_catalog import Interval
        cov = [Interval(Fraction(0), Fraction(100))]
        cert = certify_slots(env, [Asset("FAR", "L", Fraction(9999))], cov)
        self.assertFalse(cert["X"]["certified"])
        self.assertFalse(cert["X"]["light_certifiable"])
        self.assertFalse(cert["X"]["usable_as_dark"])

    def test_missing_walk_never_usable_dark(self):
        env = {"slots": {"X": {"slot_id": "X", "grid_x": 0, "position": "0",
                               "locator_asset": "A", "survey_status": "partial"}}}
        from inspection_library import Asset
        cert = certify_slots(env, [Asset("A", "L", Fraction(0))])
        self.assertFalse(cert["X"]["certified"])
        self.assertFalse(cert["X"]["usable_as_dark"])


# ---------------------------------------------------------------------------
# Grid shapes / occlusion
# ---------------------------------------------------------------------------


class TestOcclusion(unittest.TestCase):
    def test_grid_cells(self):
        self.assertEqual(grid_cells({"shape": ["#.", "##"]}),
                         {(0, 0), (1, 0), (1, 1)})

    def test_front_covers_back_face_cell(self):
        env = S.build_occlusion_env(MATERIALS)
        # rabbit in front, crane behind
        on_stage = {"F": "兔", "K": "鹤"}
        lit = {"F": {"status": "lit"}, "K": {"status": "lit"}}
        vis = visible_faces(env, on_stage, lit)
        self.assertEqual(vis.get("F"), "甲")
        self.assertEqual(vis.get("K"), "乙")  # crane face (0,1) not in rabbit cells

    def test_depth_swap_hides_back_face(self):
        env = S.build_occlusion_env(MATERIALS)
        # Now crane front, rabbit back: rabbit face (0,0) IS in crane cells.
        on_stage = {"F": "鹤", "K": "兔"}
        lit = {"F": {"status": "lit"}, "K": {"status": "lit"}}
        vis = visible_faces(env, on_stage, lit)
        self.assertEqual(vis.get("F"), "乙")
        self.assertNotIn("K", vis)  # rabbit's face is covered in the back row

    def test_dark_slot_projects_nothing(self):
        env = S.build_occlusion_env(MATERIALS)
        on_stage = {"F": "兔", "K": "鹤"}
        lit = {"F": {"status": "lit"}, "K": {"status": "dark"}}
        vis = visible_faces(env, on_stage, lit)
        self.assertNotIn("K", vis)


# ---------------------------------------------------------------------------
# Handoff show
# ---------------------------------------------------------------------------


class TestHandoffShow(unittest.TestCase):
    def _plan(self):
        return {
            "beats": {1: [["甲", "兔", "S"]], 2: [["乙", "鹤", "S"]]},
            "scenes": {
                1: {"required": [{"slot": "S", "face": "甲"}]},
                2: {"required": [{"slot": "S", "face": "甲"}]},
            },
        }

    def test_original_board_height_both_beat_lit_and_playable(self):
        env = S.build_handoff_env(MATERIALS, board_height=1)
        r = run_plan(env, self._plan())
        self.assertTrue(r["ok"], r["failures"])
        self.assertEqual([f["visible"]["S"] for f in r["frames"]], ["甲", "甲"])

    def test_board_height_two_darkens_first_beat(self):
        env = S.build_handoff_env(MATERIALS, board_height=2)
        r = run_plan(env, self._plan())
        self.assertFalse(r["ok"])
        self.assertEqual(r["earliest_failure"], 1)
        # second beat still lit (slope 2 clears height 2)
        self.assertEqual(r["frames"][1]["visible"]["S"], "甲")

    def test_single_master_cannot_rebind_without_empty_beat(self):
        env = S.build_handoff_env(MATERIALS, with_second_master=False)
        # Only master 甲 exists; it knows both roles but must use an empty
        # beat to rebind a different puppet.
        plan = {
            "beats": {1: [["甲", "兔", "S"]], 2: [["甲", "鹤", "S"]]},
            "scenes": {
                1: {"required": [{"slot": "S", "face": "甲"}]},
                2: {"required": [{"slot": "S", "face": "甲"}]},
            },
        }
        r = run_plan(env, plan)
        self.assertFalse(r["ok"])
        self.assertEqual(r["earliest_failure"], 2)
        self.assertTrue(any("rebinds" in f["error"] for f in r["failures"]))

    def test_missing_measurement_blocks_required_face(self):
        env = S.build_handoff_env(MATERIALS, slot_survey="partial")
        r = run_plan(env, self._plan())
        self.assertFalse(r["ok"])
        self.assertTrue(any("unresolved" in f["error"] for f in r["failures"]))

    def test_required_role_needs_its_own_puppet(self):
        # The crane can project '甲' too, but a scene that requires role 兔 must
        # not be satisfied by the crane puppet.
        env = S.build_handoff_env(MATERIALS)
        plan = {
            "beats": {1: [["乙", "鹤", "S"]]},
            "scenes": {1: {"required": [{"slot": "S", "face": "甲", "role": "兔"}]}},
        }
        r = run_plan(env, plan)
        self.assertFalse(r["ok"])
        self.assertTrue(any("another role" in f["error"] for f in r["failures"]))


# ---------------------------------------------------------------------------
# Standby / flip-rig show
# ---------------------------------------------------------------------------


class TestStandbyShow(unittest.TestCase):
    def _scenes(self):
        return {
            1: {"required": [{"slot": "J", "face": "旦"}]},
            2: {"required": [{"slot": "J", "face": "丑"}]},
        }

    def test_direct_rebind_fails(self):
        env = S.build_standby_env(MATERIALS)
        plan = {"beats": {1: [["甲", "甲P", "J"]], 2: [["甲", "乙P", "J"]]},
                "scenes": self._scenes()}
        r = run_plan(env, plan)
        self.assertFalse(r["ok"])
        self.assertEqual(r["earliest_failure"], 2)

    def test_standby_substitute_with_wing(self):
        env = S.build_standby_env(MATERIALS)
        plan = {
            "beats": {1: [["甲", "甲P", "J"]], 2: [["替", "乙P", "J"]]},
            "standby": {"乙P": {"machine": "替", "beat": 2, "hold": 2, "slot": "WING"}},
            "scenes": self._scenes(),
        }
        r = run_plan(env, plan)
        self.assertTrue(r["ok"], r["failures"])
        self.assertEqual(r["frames"][1]["operator"]["J"], "替")

    def test_standby_exclusive_next_beat(self):
        # The substitute is committed to the standby window; borrowing it on
        # the immediately following beat is forbidden.
        env = S.build_standby_env(MATERIALS)
        plan = {
            "beats": {1: [["甲", "甲P", "J"]], 2: [["替", "乙P", "J"]]},
            "standby": {"乙P": {"machine": "替", "beat": 2, "hold": 2, "slot": "WING"}},
            "scenes": self._scenes(),
        }
        r = run_plan(env, plan)
        self.assertTrue(r["ok"])
        # Now place the standby *before* beat 1 and try to borrow 替 at beat 1
        # outside the standby grant.
        plan2 = {
            "beats": {1: [["替", "乙P", "J"]]},
            "standby": {"乙P": {"machine": "替", "beat": 1, "hold": 2, "slot": "WING"}},
            "scenes": {1: self._scenes()[1]},
        }
        # This IS the standby grant, so allowed; borrow the *other* substitute
        # path instead: mark 甲 standing by and try to use it for J.
        plan3 = {
            "beats": {1: [["甲", "甲P", "J"]]},
            "standby": {"乙P": {"machine": "甲", "beat": 1, "hold": 2, "slot": "WING"}},
            "scenes": {1: self._scenes()[1]},
        }
        r3 = run_plan(env, plan3)
        self.assertFalse(r3["ok"])
        self.assertTrue(any("committed to a standby" in f["error"]
                            for f in r3["failures"]))

    def test_standby_held_across_beats(self):
        env = S.build_standby_env(MATERIALS)
        env["beats"].append({"beat": 3, "light": {"dir": "W", "slope": 1}})
        plan = {
            "beats": {1: [["甲", "甲P", "J"]],
                      2: [["替", "乙P", "J"]],
                      3: [["替", "乙P", "J"]]},
            "standby": {"乙P": {"machine": "替", "beat": 2, "hold": 2, "slot": "WING"}},
            "scenes": {
                1: {"required": [{"slot": "J", "face": "旦"}]},
                2: {"required": [{"slot": "J", "face": "丑"}]},
                3: {"required": [{"slot": "J", "face": "丑"}]},
            },
        }
        r = run_plan(env, plan)
        self.assertTrue(r["ok"], r["failures"])

    def test_flip_rig_with_right_slot_hides_old_face(self):
        env = S.build_standby_env(MATERIALS, rig_right_slot=True)
        plan = {
            "beats": {1: [["甲", "甲P", "J"]],
                      2: [["甲", "乙P", "J", "rigflip", {}]]},
            "rigs": {"甲": {"puppets": ["甲P", "乙P"], "load_beats": 1,
                            "occupy_slot": "R", "since": 1, "passing_hidden": True}},
            "scenes": {
                1: self._scenes()[1],
                2: {"required": [{"slot": "J", "face": "丑"}],
                    "forbid_passing": [{"face": "旦"}]},
            },
        }
        r = run_plan(env, plan)
        self.assertTrue(r["ok"], r["failures"])
        self.assertEqual(r["frames"][1]["visible"]["J"], "丑")

    def test_flip_rig_leak_caught_when_old_face_exposed(self):
        env = S.build_standby_env(MATERIALS, rig_right_slot=True)
        plan = {
            "beats": {1: [["甲", "甲P", "J"]],
                      2: [["甲", "乙P", "J", "rigflip", {}]]},
            "rigs": {"甲": {"puppets": ["甲P", "乙P"], "load_beats": 1,
                            "occupy_slot": "R", "since": 1, "passing_hidden": False}},
            "scenes": {
                1: self._scenes()[1],
                2: {"required": [{"slot": "J", "face": "丑"}],
                    "forbid_passing": [{"face": "旦"}]},
            },
        }
        r = run_plan(env, plan)
        self.assertFalse(r["ok"])
        self.assertEqual(r["earliest_failure"], 2)
        self.assertTrue(any("leaks forbidden passing face" in f["error"]
                            for f in r["failures"]))

    def test_flip_rig_requires_measured_right_slot(self):
        env = S.build_standby_env(MATERIALS, rig_right_slot=False)
        plan = {
            "beats": {1: [["甲", "甲P", "J"]],
                      2: [["甲", "乙P", "J", "rigflip", {}]]},
            "rigs": {"甲": {"puppets": ["甲P", "乙P"], "load_beats": 1,
                            "occupy_slot": "R", "since": 1, "passing_hidden": True}},
            "scenes": {1: self._scenes()[1],
                       2: {"required": [{"slot": "J", "face": "丑"}]}},
        }
        r = run_plan(env, plan)
        self.assertFalse(r["ok"])
        self.assertTrue(any("unknown slot 'R'" in f["error"] for f in r["failures"]))

    def test_flip_rig_dark_extra_slot_hides_passing_face(self):
        # If the rig's extra slot is genuinely dark, the old face never
        # projects, so exposing it along the seam is harmless.
        env = S.build_standby_env(MATERIALS, rig_right_slot=True)
        env["panels"] = [{"panel_id": "shadeR", "grid_x": 0, "height": 2,
                          "position": "470", "surveyed": True}]
        plan = {
            "beats": {1: [["甲", "甲P", "J"]],
                      2: [["甲", "乙P", "J", "rigflip", {}]]},
            "rigs": {"甲": {"puppets": ["甲P", "乙P"], "load_beats": 1,
                            "occupy_slot": "R", "since": 1, "passing_hidden": False}},
            "scenes": {1: self._scenes()[1],
                       2: {"required": [{"slot": "J", "face": "丑"}],
                           "forbid_passing": [{"face": "旦"}]}},
        }
        r = run_plan(env, plan)
        self.assertTrue(r["ok"], r["failures"])


# ---------------------------------------------------------------------------
# Absence notices
# ---------------------------------------------------------------------------


class TestAbsenceNotices(unittest.TestCase):
    def _scenes(self):
        return {
            1: {"required": [{"slot": "J", "face": "旦"}]},
            2: {"required": [{"slot": "J", "face": "丑"}]},
        }

    def _standby_plan(self, notices):
        return {
            "beats": {1: [["甲", "甲P", "J"]], 2: [["替", "乙P", "J"]]},
            "standby": {"乙P": {"machine": "替", "beat": 2, "hold": 2, "slot": "WING"}},
            "scenes": self._scenes(),
            "notices": notices,
        }

    def test_valid_substitute_after_notice(self):
        env = S.build_standby_env(MATERIALS)
        r = run_plan(env, self._standby_plan(
            [{"role": "乙", "beat": 2, "available": ["甲", "替"]}]))
        self.assertTrue(r["ok"], r["failures"])

    def test_substitute_not_on_hand_rejected(self):
        env = S.build_standby_env(MATERIALS)
        r = run_plan(env, self._standby_plan(
            [{"role": "乙", "beat": 2, "available": ["甲"]}]))
        self.assertFalse(r["ok"])
        self.assertTrue(any("not among the on-hand" in f["error"]
                            for f in r["failures"]))

    def test_bound_master_absent_rejected(self):
        env = S.build_standby_env(MATERIALS)
        plan = {"beats": {1: [["甲", "甲P", "J"]], 2: [["乙", "乙P", "J"]]},
                "scenes": self._scenes(),
                "notices": [{"role": "乙", "beat": 2, "available": ["甲", "替"]}]}
        r = run_plan(env, plan)
        self.assertFalse(r["ok"])
        self.assertTrue(any("is absent" in f["error"] for f in r["failures"]))

    def test_premature_switch_rejected(self):
        env = S.build_standby_env(MATERIALS)
        plan = {
            "beats": {1: [["替", "乙P", "J"]]},
            "standby": {"乙P": {"machine": "替", "beat": 1, "hold": 2, "slot": "WING"}},
            "scenes": {1: self._scenes()[2]},
            "notices": [{"role": "乙", "beat": 2, "available": ["甲", "替"]}],
        }
        r = run_plan(env, plan)
        self.assertFalse(r["ok"])
        self.assertTrue(any("notice only arrives at beat 2" in f["error"]
                            for f in r["failures"]))


# ---------------------------------------------------------------------------
# Resume from observed state
# ---------------------------------------------------------------------------


class TestResume(unittest.TestCase):
    def test_resume_from_held_puppet(self):
        env = S.build_handoff_env(MATERIALS)
        resume = {
            "last": {"甲": {"beat": 1, "puppet": "兔", "slot": "S", "face": "甲"}},
            "performed_on": {"甲": [1], "乙": []},
            "prev_assign": {"甲": {"slot": "S", "puppet": "兔"}},
        }
        plan = {"beats": {2: [["乙", "鹤", "S"]]},
                "scenes": {2: {"required": [{"slot": "S", "face": "甲"}]}}}
        r = run_plan(env, plan, resume=resume)
        self.assertTrue(r["ok"], r["failures"])

    def test_resume_blocks_instant_rebind(self):
        env = S.build_handoff_env(MATERIALS)
        resume = {
            "last": {"甲": {"beat": 1, "puppet": "鹤", "slot": "S", "face": "甲"}},
            "performed_on": {"甲": [1]},
            "prev_assign": {"甲": {"slot": "S", "puppet": "鹤"}},
        }
        plan = {"beats": {2: [["甲", "兔", "S"]]},
                "scenes": {2: {"required": [{"slot": "S", "face": "甲"}]}}}
        r = run_plan(env, plan, resume=resume)
        self.assertFalse(r["ok"])
        self.assertTrue(any("rebinds" in f["error"] for f in r["failures"]))


# ---------------------------------------------------------------------------
# Limits & movement
# ---------------------------------------------------------------------------


class TestLimits(unittest.TestCase):
    def test_within_limits(self):
        env = S.build_standby_env(MATERIALS)
        self.assertEqual(check_limits(env), [])

    def test_over_capacity_flagged(self):
        env = {"machines": {str(i): {} for i in range(5)},
               "puppets": {}, "beats": [], "slots": {}}
        self.assertTrue(any("masters" in p for p in check_limits(env)))

    def test_move_duration_from_rules(self):
        env = {"slots": {"a": {"grid_x": 0}, "b": {"grid_x": 2}},
               "stage": {"adjacent_move": [{"distance": 2, "beats": 3}],
                         "move_beats_per_cell": 1}}
        self.assertEqual(move_duration(env, "a", "b"), 3)
        env2 = {"slots": {"a": {"grid_x": 0}, "b": {"grid_x": 5}},
                "stage": {"adjacent_move": [], "move_beats_per_cell": 2}}
        self.assertEqual(move_duration(env2, "a", "b"), 10)


# ---------------------------------------------------------------------------
# Capacity constraints & first-break reporting
# ---------------------------------------------------------------------------


class TestCapacityConstraints(unittest.TestCase):
    def test_two_masters_one_slot_rejected(self):
        env = S.build_handoff_env(MATERIALS)
        plan = {"beats": {1: [["甲", "兔", "S"], ["乙", "鹤", "S"]]}}
        r = run_plan(env, plan)
        self.assertFalse(r["ok"])
        self.assertTrue(any("share slot" in f["error"] for f in r["failures"]))

    def test_one_master_two_puppets_rejected(self):
        env = S.build_occlusion_env(MATERIALS)
        plan = {"beats": {1: [["甲", "兔", "F"], ["甲", "鹤", "K"]]}}
        r = run_plan(env, plan)
        self.assertFalse(r["ok"])
        self.assertTrue(any("holds two puppets" in f["error"] for f in r["failures"]))

    def test_puppet_used_twice_rejected(self):
        env = S.build_occlusion_env(MATERIALS)
        plan = {"beats": {1: [["甲", "兔", "F"], ["乙", "兔", "K"]]}}
        r = run_plan(env, plan)
        self.assertFalse(r["ok"])
        self.assertTrue(any("used twice" in f["error"] for f in r["failures"]))

    def test_earliest_failure_is_first_broken_beat(self):
        # Beat 1 is fine; beat 2 rebinds without an empty beat.
        env = S.build_handoff_env(MATERIALS, with_second_master=False)
        plan = {"beats": {1: [["甲", "兔", "S"]], 2: [["甲", "鹤", "S"]]},
                "scenes": {1: {"required": [{"slot": "S", "face": "甲"}]}}}
        r = run_plan(env, plan)
        self.assertEqual(r["earliest_failure"], 2)

    def test_learned_role_required(self):
        # Master 乙 only learned 鹤; staging a role it never learned fails.
        env = S.build_handoff_env(MATERIALS)
        env["machines"]["乙"]["learned"] = []  # forget everything
        plan = {"beats": {2: [["乙", "鹤", "S"]]},
                "scenes": {2: {"required": [{"slot": "S", "face": "甲"}]}}}
        r = run_plan(env, plan)
        self.assertFalse(r["ok"])
        self.assertTrue(any("never learned" in f["error"] for f in r["failures"]))


if __name__ == "__main__":
    unittest.main()
