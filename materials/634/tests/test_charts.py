"""判图登记测试：原依据保留、断网缩窄、时钟修订换图、重开复算。"""

from __future__ import annotations

import os
import tempfile
import unittest

from review.calibration import admit
from review.charts import ChartRegistry, build_bases
from review.fixtures import confirmed_case, pending_case
from review.model import Verdict


def _bases(case, result):
    return build_bases(
        result,
        flash_ids={c.camera_id: tuple(f.flash_id for f in c.flashes)
                   for c in case.cameras},
        benchmark_count={c.camera_id: len(c.benchmarks)
                         for c in case.cameras})


class TestChartRegistry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "charts.json")
        self.registry = ChartRegistry(self.path)
        self.pending = pending_case()
        self.res_pending = admit(self.pending.cameras)
        self.confirmed = confirmed_case()
        self.res_confirmed = admit(self.confirmed.cameras)

    def tearDown(self):
        self.tmp.cleanup()

    def test_issue_keeps_original_basis(self):
        chart = self.registry.issue(
            "m1-h1", "full", Verdict.PENDING, (5,),
            ("R", "ball", "D1", "D2"), _bases(self.pending, self.res_pending),
            "evidence-v1")
        self.assertEqual(chart.evidence_version, "evidence-v1")
        offsets = {tuple(sorted(b.offsets.items())) for b in chart.bases}
        self.assertEqual(len(offsets), 4)  # 原依据：两机各 {0,1} 笛卡尔积

    def test_offline_narrow_appeal_must_stay_within_accepted_range(self):
        chart = self.registry.issue(
            "m1-h1", "full", Verdict.PENDING, (4, 5, 6),
            ("R",), _bases(self.pending, self.res_pending), "evidence-v1")
        good = self.registry.narrow_appeal("a1", chart.chart_id, (5,),
                                           Verdict.PENDING)
        self.assertTrue(good.granted)
        narrowed = self.registry.get(good.narrowed_chart_id)
        self.assertEqual(narrowed.accepted_beats, (5,))
        self.assertEqual(narrowed.parent_chart_id, chart.chart_id)

        bad = self.registry.narrow_appeal("a2", chart.chart_id, (5, 7),
                                          Verdict.PENDING)
        self.assertFalse(bad.granted)
        empty = self.registry.narrow_appeal("a3", chart.chart_id, (),
                                            Verdict.PENDING)
        self.assertFalse(empty.granted)

    def test_clock_revision_marks_superseded_and_preserves_old_chart(self):
        chart = self.registry.issue(
            "m1-h1", "full", Verdict.PENDING, (5,),
            ("R",), _bases(self.pending, self.res_pending), "evidence-v1")
        marked = self.registry.mark_clock_revision(
            "m1-h1", "CAM-A", frozenset({1}), "evidence-v2",
            Verdict.CONFIRMED,
            _bases(self.confirmed, self.res_confirmed))
        self.assertEqual([c.chart_id for c in marked], [chart.chart_id])
        old = self.registry.get(chart.chart_id)
        self.assertIsNotNone(old.superseded_by)
        self.assertIn("evidence-v2", old.supersede_reason)
        # 旧依据原样保留，未被改写
        self.assertEqual(len(old.bases), 4)
        replacement = self.registry.get(old.superseded_by)
        self.assertEqual(replacement.evidence_version, "evidence-v2")
        self.assertEqual(replacement.parent_chart_id, chart.chart_id)

    def test_reopen_registry_reproduces_state(self):
        chart = self.registry.issue(
            "m1-h1", "full", Verdict.PENDING, (5,),
            ("R",), _bases(self.pending, self.res_pending), "evidence-v1")
        self.registry.narrow_appeal("a1", chart.chart_id, (5,),
                                    Verdict.PENDING)
        reopened = ChartRegistry(self.path)
        self.assertEqual(reopened.get(chart.chart_id).accepted_beats, (5,))
        self.assertEqual(len(reopened.appeals), 1)
        self.assertTrue(reopened.appeals[0].granted)


if __name__ == "__main__":
    unittest.main()
