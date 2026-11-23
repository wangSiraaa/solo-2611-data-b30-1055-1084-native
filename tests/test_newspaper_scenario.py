"""场景走查 A..E 的 unittest 封装（与 scenario.py 的 ok 断言同源）。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from newspaper.scenario import run_all


class ScenarioWalkthrough(unittest.TestCase):
    def test_all_cases_pass_and_write_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = run_all(tmp)
            self.assertTrue(report["ok"])
            for key in ("A", "B", "C", "D", "E"):
                with open(os.path.join(tmp, f"scenario_{key}.json"), encoding="utf-8") as fh:
                    payload = json.load(fh)
                self.assertTrue(payload["ok"], key)
            with open(os.path.join(tmp, "summary.json"), encoding="utf-8") as fh:
                self.assertTrue(json.load(fh)["ok"])


if __name__ == "__main__":
    unittest.main()
