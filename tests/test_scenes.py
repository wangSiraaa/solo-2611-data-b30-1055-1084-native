"""端到端：从原目录生成小动图 → 合成 → 场记读法 → 补拍单 → 核对。

只走本仓库的目录/合成管线，显示画布由 FramePlayer 合成；读法只吃合成画布。
"""

from __future__ import annotations

import unittest

from animation_pipeline.puppets import read_scene
from animation_pipeline.scenes import (
    base_jobs,
    build_crowd_scene,
    build_example_scene,
    build_two_act_scene,
    build_variant_clear_scene,
    build_variant_no_back_scene,
    no_back_jobs,
    two_act_jobs,
)
from animation_pipeline.shoot import (
    STATION_DOUBLE_FRAME,
    STATION_SINGLE_TURNTABLE,
    Worksheet,
    plan_shoot,
    verify_plan,
)


def read_all(scene) -> dict:
    readings = {}
    for fid, marks in scene.marks_by_frame.items():
        r = read_scene(
            scene.frames[fid].canvas,
            marks,
            tuple(scene.patterns.values()),
            frame_id=fid,
            time_ms=fid * 10,
        )
        for name, pr in r.puppets.items():
            readings[(fid, name)] = pr
    return readings


def make_ws(scene, jobs) -> Worksheet:
    return Worksheet(
        jobs=jobs,
        cameras=scene.cameras,
        puppets=scene.puppets,
        patterns=scene.patterns,
        binding=scene.binding,
        readings=read_all(scene),
    )


class EndToEndTests(unittest.TestCase):
    def test_example_double_frame_delivers_and_verifies(self) -> None:
        sc = build_example_scene()
        ws = make_ws(sc, base_jobs())
        plan = plan_shoot(ws, STATION_DOUBLE_FRAME)
        self.assertEqual(plan.undelivered, [])
        self.assertEqual({e.frame_id for e in plan.exposures}, {1, 2})
        self.assertEqual(verify_plan(plan, ws), [])
        # 片序：曝光编号严格递增。
        ids = [int(e.exposure_id[1:]) for e in plan.exposures]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(set(ids)), len(ids))

    def test_example_turntable_cannot_do_simultaneous(self) -> None:
        sc = build_example_scene()
        ws = make_ws(sc, base_jobs())
        plan = plan_shoot(ws, STATION_SINGLE_TURNTABLE)
        self.assertEqual(plan.exposures, [])
        self.assertEqual(len(plan.undelivered), 2)

    def test_variant_clear_leaves_last_frame_unfilled(self) -> None:
        sc = build_variant_clear_scene()
        ws = make_ws(sc, base_jobs())
        plan = plan_shoot(ws, STATION_DOUBLE_FRAME)
        self.assertEqual({e.frame_id for e in plan.exposures}, {1})
        self.assertIn(2, {u.frame_id for u in plan.undelivered})

    def test_variant_no_back_rejects(self) -> None:
        sc = build_variant_no_back_scene()
        ws = make_ws(sc, no_back_jobs())
        plan = plan_shoot(ws, STATION_DOUBLE_FRAME)
        self.assertEqual(plan.exposures, [])
        self.assertEqual(len(plan.undelivered), 2)

    def test_two_act_seal_stops_act2(self) -> None:
        sc = build_two_act_scene()
        ws = make_ws(sc, two_act_jobs())
        plan = plan_shoot(ws, STATION_DOUBLE_FRAME)
        self.assertEqual({e.frame_id for e in plan.exposures}, {0})
        und = [u for u in plan.undelivered if u.frame_id == 1]
        self.assertIn("frame 0", und[0].trace)
        self.assertEqual(verify_plan(plan, ws), [])

    def test_crowd_reads_and_merges(self) -> None:
        sc = build_crowd_scene()
        rs = read_all(sc)
        self.assertEqual(rs[(0, "redpup")].pose.name, "crowd_red")
        self.assertTrue(rs[(1, "redpup")].merged)
        self.assertTrue(rs[(1, "redpup2")].merged)

    def test_every_delivered_plan_verifies(self) -> None:
        for sc, jobs in (
            (build_example_scene(), base_jobs()),
            (build_variant_clear_scene(), base_jobs()),
            (build_two_act_scene(), two_act_jobs()),
        ):
            ws = make_ws(sc, jobs)
            for station in (STATION_DOUBLE_FRAME, STATION_SINGLE_TURNTABLE):
                plan = plan_shoot(ws, station)
                self.assertEqual(verify_plan(plan, ws), [], f"{station} 计划不自洽")


if __name__ == "__main__":
    unittest.main()
