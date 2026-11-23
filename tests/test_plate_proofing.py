"""Unittest suite for the plate proofing work order.

Run with::

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from plate_proofing import (
    COMPLETE,
    MISSING,
    TORN,
    CoordinateTable,
    Patch,
    PlateWorkOrder,
    ProofError,
    ProofingSession,
    Reading,
    Regrind,
    SheetRequest,
    CRAFT_CATCH_FRAME,
    CRAFT_FILM,
    load_coordinate_table,
    run_all,
)

COORD_PATH = os.path.join(os.path.dirname(__file__), "..", "materials", "751",
                          "coordinate_table.json")


def tiny_coord(**states):
    base = {p: {"x": i, "y": i, "slot": f"S-{p}", "pins": [f"pin-{p}"]}
            for i, p in enumerate("LR")}
    for p, s in states.items():
        base[p]["state"] = s
    return CoordinateTable.from_dict({"plate": "T", "points": base})


class CoordinateTableLimits(unittest.TestCase):
    def test_at_most_eight_points(self):
        points = {f"P{i}": {"x": i, "y": i, "slot": f"S{i}", "pins": []}
                  for i in range(9)}
        with self.assertRaises(ProofError) as ctx:
            CoordinateTable.from_dict({"points": points})
        self.assertEqual(ctx.exception.code, "too_many_points")

    def test_at_most_twelve_runs_and_four_patches(self):
        wo = PlateWorkOrder(tiny_coord(L=MISSING, R=MISSING))
        for run in range(12):
            wo.register_run(run)
        with self.assertRaises(ProofError) as ctx:
            wo.register_run(12)
        self.assertEqual(ctx.exception.code, "too_many_runs")

        for i in range(4):
            wo.add_patch(Patch(f"P{i}", f"PHY{i}", frozenset({"L"}), {"L": COMPLETE},
                              f"S{i}"))
        with self.assertRaises(ProofError) as ctx:
            wo.add_patch(Patch("P4", "PHY4", frozenset({"R"}), {"R": COMPLETE}, "S4"))
        self.assertEqual(ctx.exception.code, "too_many_patches")

    def test_load_local_table(self):
        coord = load_coordinate_table(COORD_PATH)
        self.assertEqual(coord.plate, "P751")
        self.assertLessEqual(len(coord.points), 8)
        # coordinate grid really has x/y
        self.assertIn("x", coord.points["L"])


class PatchValidation(unittest.TestCase):
    def test_cover_must_emit_definite_state(self):
        with self.assertRaises(ProofError):
            Patch("P", "PHY", frozenset({"L"}), {"L": TORN}, "S")

    def test_film_takes_no_pins(self):
        with self.assertRaises(ProofError):
            Patch("P", "PHY", frozenset({"L"}), {"L": COMPLETE}, "S",
                  pins=frozenset({"pin"}), film=True)


class TornIsSeparate(unittest.TestCase):
    def test_torn_is_neither_complete_nor_missing(self):
        wo = PlateWorkOrder(tiny_coord(L=COMPLETE, R=COMPLETE))
        wo.register_run(30)
        wo.add_reading(Reading("r", "s", 30, {"L": COMPLETE, "R": TORN}, "H"))
        wo.set_must_check(["L", "R"])
        sess = ProofingSession(wo, CRAFT_CATCH_FRAME)
        j = sess.process(SheetRequest("sheet", "s", 30, signer="H"))
        self.assertFalse(j.signable)
        self.assertTrue(any(r["kind"] == "unverifiable" and r.get("torn")
                            and r.get("point") == "R" for r in j.refusals))
        # L still prints and is verifiable.
        self.assertEqual(j.printed["L"], COMPLETE)


class Authority(unittest.TestCase):
    def test_plan_only_cannot_supersede_custodian(self):
        wo = PlateWorkOrder(tiny_coord(L=COMPLETE, R=COMPLETE))
        wo.register_run(10)
        wo.add_reading(Reading("old", "s", 10, {"L": COMPLETE, "R": COMPLETE}, "OLD"))
        wo.add_reading(Reading("new", "s", 10, {"L": COMPLETE, "R": MISSING}, "NEW",
                               authority=1, plan_only=True, supersedes=("old",)))
        wo.set_must_check(["L", "R"])
        self.assertEqual([r.reading_id for r in wo.authoritative_readings(10)], ["old"])
        self.assertEqual([r.reading_id for r in wo.plan_only_readings(10)], ["new"])
        sess = ProofingSession(wo, CRAFT_CATCH_FRAME)
        j = sess.process(SheetRequest("sheet", "s", 10, signer="NEW"))
        self.assertFalse(j.signable)
        self.assertTrue(any(r["kind"] == "signer_not_custodian" for r in j.refusals))

    def test_held_sibling_blocks_whole_proof(self):
        # "案中另件受保时也不能把整案视为已借到": one held member => custody_ok
        # is False, so no proof may be signed even if the pattern is correct.
        wo = PlateWorkOrder(tiny_coord(L=MISSING, R=MISSING))
        wo.register_run(10)
        wo.add_reading(Reading("r", "s", 10, {"L": MISSING, "R": COMPLETE}, "H"))
        wo.set_must_check(["L", "R"])
        wo.add_patch(Patch("A", "PHY-A", frozenset({"R"}), {"R": COMPLETE},
                           "S-R", frozenset({"pin-R"})))
        sess = ProofingSession(wo, CRAFT_CATCH_FRAME)
        j = sess.process(SheetRequest("sheet", "s", 10, mounts=("A",), signer="H",
                                      custody_ok=False))
        self.assertFalse(j.signable)
        self.assertTrue(any(r["kind"] == "custody_incomplete" for r in j.refusals))


class RegrindRules(unittest.TestCase):
    def test_unevidenced_regrind_rejected(self):
        with self.assertRaises(ProofError) as ctx:
            Regrind("R", day=5, evidence_id="")
        self.assertEqual(ctx.exception.code, "unevidenced_regrind")

    def test_notch_may_only_vanish_after_evidenced_regrind(self):
        wo = PlateWorkOrder(tiny_coord(L=COMPLETE, R=COMPLETE))
        wo.register_run(5)
        wo.register_run(9)
        wo.add_reading(Reading("a", "s", 5, {"R": MISSING}, "H"))
        wo.add_reading(Reading("b", "s", 9, {"R": COMPLETE}, "H"))
        self.assertTrue(wo.explain_transitions())
        wo.add_regrind(Regrind("GR", day=6, evidence_id="EV"))
        self.assertEqual(wo.explain_transitions(), [])


class SlotsAndPins(unittest.TestCase):
    def test_same_slot_cannot_hold_two_pieces(self):
        wo = PlateWorkOrder(tiny_coord(L=MISSING, R=MISSING))
        wo.register_run(1)
        wo.add_reading(Reading("r", "s", 1, {"L": MISSING, "R": COMPLETE}, "H"))
        wo.set_must_check(["L", "R"])
        wo.add_patch(Patch("A", "PHY-A", frozenset({"R"}), {"R": COMPLETE}, "SAME"))
        wo.add_patch(Patch("B", "PHY-B", frozenset({"R"}), {"R": COMPLETE}, "SAME"))
        sess = ProofingSession(wo, CRAFT_CATCH_FRAME)
        j = sess.process(SheetRequest("sh", "s", 1, mounts=("A", "B"), signer="H"))
        self.assertFalse(j.signable)
        self.assertTrue(any(r["kind"] == "co_mount_conflict" for r in j.refusals))

    def test_piece_still_occupies_pins_without_scan_release(self):
        wo = PlateWorkOrder(tiny_coord(L=MISSING, R=MISSING))
        wo.register_run(1)
        wo.register_run(2)
        wo.add_reading(Reading("r1", "s1", 1, {"L": MISSING, "R": COMPLETE}, "H"))
        wo.add_reading(Reading("r2", "s2", 2, {"L": MISSING, "R": COMPLETE}, "H"))
        wo.set_must_check(["L", "R"])
        wo.add_patch(Patch("A", "PHY-A", frozenset({"R"}), {"R": COMPLETE},
                           "S-R", frozenset({"pin-R"})))
        sess = ProofingSession(wo, CRAFT_CATCH_FRAME)
        first = sess.process(SheetRequest("sh1", "s1", 1, mounts=("A",), signer="H"))
        self.assertTrue(first.signable)
        # No scan-confirmed release: still occupied.
        blocked = sess.process(SheetRequest("sh2", "s2", 2, mounts=("A",),
                                            release=("sh1",), signer="H"))
        self.assertFalse(blocked.signable)
        # With a scan-confirmed dismantle it can be lent.
        ok = sess.process(SheetRequest("sh2b", "s2", 2, mounts=("A",),
                                       release=("sh1",),
                                       scan_release_confirmed=True, signer="H"))
        self.assertTrue(ok.signable)

    def test_locked_slot_keeps_appraisal_but_refuses_proof(self):
        wo = PlateWorkOrder(tiny_coord(L=MISSING, R=MISSING))
        wo.register_run(1)
        wo.add_reading(Reading("r", "s", 1, {"L": MISSING, "R": COMPLETE}, "H"))
        wo.set_must_check(["L", "R"])
        wo.add_patch(Patch("A", "PHY-A", frozenset({"R"}), {"R": COMPLETE},
                           "S-R", frozenset({"pin-R"})))
        sess = ProofingSession(wo, CRAFT_CATCH_FRAME, locked_slots={"S-R"},
                               locked_pins={"pin-R"})
        j = sess.process(SheetRequest("sh", "s", 1, mounts=("A",), signer="H"))
        self.assertFalse(j.signable)
        self.assertTrue(j.matched_readings)  # appraisal still valid
        self.assertTrue(any(r["kind"] == "slot_locked" for r in j.refusals))


class Film(unittest.TestCase):
    def test_film_consumes_piece_and_origin(self):
        wo = PlateWorkOrder(tiny_coord(L=MISSING, R=MISSING))
        wo.register_run(1)
        wo.register_run(2)
        wo.add_reading(Reading("r1", "s1", 1, {"L": MISSING, "R": COMPLETE}, "H"))
        wo.add_reading(Reading("r2", "s2", 2, {"L": MISSING, "R": COMPLETE}, "H"))
        wo.set_must_check(["L", "R"])
        wo.add_patch(Patch("F", "F-PHY", frozenset({"R"}), {"R": COMPLETE}, "S-R",
                           film=True))
        sess = ProofingSession(wo, CRAFT_FILM)
        first = sess.process(SheetRequest("sh1", "s1", 1, mounts=("F",), signer="H"))
        self.assertTrue(first.signable)
        self.assertIn("F", sess.film_consumed)
        self.assertIn("R", sess.origin_consumed)
        second = sess.process(SheetRequest("sh2", "s2", 2, mounts=("F",), signer="H"))
        self.assertFalse(second.signable)
        self.assertTrue(any(r["kind"] == "film_spent" for r in second.refusals))

    def test_craft_cannot_cross_patch_kinds(self):
        wo = PlateWorkOrder(tiny_coord(L=MISSING, R=MISSING))
        wo.register_run(1)
        wo.add_reading(Reading("r", "s", 1, {"L": MISSING, "R": COMPLETE}, "H"))
        wo.set_must_check(["L", "R"])
        wo.add_patch(Patch("A", "PHY-A", frozenset({"R"}), {"R": COMPLETE},
                           "S-R", frozenset({"pin-R"})))
        wo.add_patch(Patch("F", "F-PHY", frozenset({"R"}), {"R": COMPLETE}, "S-R",
                           film=True))
        j = ProofingSession(wo, CRAFT_FILM).process(
            SheetRequest("sh", "s", 1, mounts=("A",), signer="H"))
        self.assertTrue(any(r["kind"] == "pinned_patch_in_film_craft"
                            for r in j.refusals))
        j2 = ProofingSession(wo, CRAFT_CATCH_FRAME).process(
            SheetRequest("sh", "s", 1, mounts=("F",), signer="H"))
        self.assertTrue(any(r["kind"] == "film_patch_in_catch_frame"
                            for r in j2.refusals))


class WorkedScenarios(unittest.TestCase):
    """Every case named in the ticket, executed end to end."""

    def test_run_all_ok(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        result = run_all(out_dir=tmp.name)
        self.assertTrue(result["ok"],
                        json.dumps({k: v["expected"] for k, v in result["cases"].items()
                                    if not v["ok"]}, indent=2))
        self.assertTrue(os.path.exists(os.path.join(tmp.name, "workorder.json")))
        self.assertTrue(os.path.exists(os.path.join(tmp.name, "workorder_summary.json")))
        with open(os.path.join(tmp.name, "workorder_summary.json"), encoding="utf-8") as fh:
            summary = json.load(fh)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["signable_samples"], ["S80", "S81"])
        refused = {r["sheet"] for r in summary["refused_samples"]}
        self.assertIn("S82", refused)
        # The named cases are all present and green.
        for name, case in result["cases"].items():
            self.assertTrue(case["ok"], f"{name} failed")
            self.assertTrue(all(case["expected"].values()), name)

    def test_right_patch_then_reread(self):
        from plate_proofing.scenarios import case_right_patch_signable
        r = case_right_patch_signable(tiny_coord(L=MISSING, R=MISSING))
        self.assertTrue(r["ok"])
        self.assertTrue(r["expected"]["signable"])
        self.assertTrue(r["expected"]["reread_makes_R_only_insufficient"])

    def test_two_runs_single_piece(self):
        from plate_proofing.scenarios import case_two_runs_single_piece
        r = case_two_runs_single_piece(tiny_coord(L=MISSING, R=MISSING))
        self.assertTrue(r["ok"])
        self.assertTrue(r["expected"]["run51_blocked_without_swap"])
        self.assertTrue(r["expected"]["run51_signs_after_swap"])
        self.assertTrue(r["expected"]["film_sheet2_blocked_no_second_piece"])

    def test_pattern_ok_no_slot_film_approval(self):
        from plate_proofing.scenarios import case_pattern_ok_no_slot_film_approval
        r = case_pattern_ok_no_slot_film_approval(tiny_coord(L=MISSING, R=MISSING))
        self.assertTrue(r["ok"])
        self.assertTrue(r["expected"]["film_approval_available"])
        self.assertTrue(r["expected"]["film_signs"])

    def test_held_member_blocks_proof(self):
        from plate_proofing.scenarios import case_held_member_blocks_proof
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        r = case_held_member_blocks_proof(
            tmp.name, os.path.join(os.path.dirname(__file__), "..", "materials", "751"),
            load_coordinate_table(COORD_PATH))
        self.assertTrue(r["ok"])
        self.assertTrue(r["expected"]["held_case_refused"])
        self.assertTrue(r["expected"]["free_case_signs"])


if __name__ == "__main__":
    unittest.main()
