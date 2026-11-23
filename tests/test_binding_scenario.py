"""End-to-end test for the documented binding-photography walkthrough."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from archive_handoff.binding_scenario import run_binding_demo


class BindingDemo(unittest.TestCase):
    def test_documented_cases(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        result = run_binding_demo(os.path.join(tmp.name, "demo"))
        self.assertTrue(result["ok"], json.dumps(result["expected"], indent=2))
        for label, holds in result["expected"].items():
            self.assertTrue(holds, f"documented expectation failed: {label}")

    def test_artifacts_written(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data = os.path.join(tmp.name, "demo")
        run_binding_demo(data)
        for name in ("binding_demo.json", "work_order.json"):
            self.assertTrue(os.path.exists(os.path.join(data, "artifacts", name)), name)

    def test_support_and_imaging_orders_are_separate(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        result = run_binding_demo(os.path.join(tmp.name, "demo"))
        wo = result["work_order"]
        self.assertEqual(wo["support_order"]["order"], "true_support")
        self.assertEqual(wo["imaging_order"]["order"], "imaging")
        # A single jig family is implemented: the clamp bracket.
        self.assertIn("clamp_bracket", wo["imaging_order"]["jig"])

    def test_case_a_unreachable_after_measurement(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        result = run_binding_demo(os.path.join(tmp.name, "demo"))
        a = result["cases"]["A"]
        self.assertEqual(a["table_before"], ["00", "10", "11"])
        self.assertEqual(a["after_measurement"]["table"], ["00", "11"])
        self.assertTrue(a["after_measurement"]["goal_11_statically_safe"])
        self.assertFalse(a["after_measurement"]["goal_11_reachable_from_00"])

    def test_case_d_custody(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        result = run_binding_demo(os.path.join(tmp.name, "demo"))
        steps = {s["step"]: s for s in result["cases"]["D"]["steps"]}
        self.assertEqual(steps["outside_signer_rejected"]["code"],
                         "unsigned_measurement")
        self.assertTrue(steps["cannot_split_held_volume_out"]["rejected"])
        self.assertTrue(steps["origin_equipment_may_work"]["feasible"])
        self.assertFalse(steps["prepared_not_confirmed"]["outside_frame_countable"])


if __name__ == "__main__":
    unittest.main()
