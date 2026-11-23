"""校场引擎测试：标杆横轴、同步灯时钟、跨机闪灯、卡册上限。"""

from __future__ import annotations

import unittest

from review.calibration import admit, solve_axis, solve_offsets
from review.model import (AdmissionCode, AxisSolution, BenchmarkPair,
                          CameraSpec, FlashSighting)


def spec(cid, benchmarks, flashes=(), album=(-1, 0, 1)):    return CameraSpec(
        camera_id=cid, benchmarks=tuple(benchmarks),
        flashes=tuple(flashes), offset_album=frozenset(album))


def bp(grid, *actuals):
    return BenchmarkPair(grid, frozenset(actuals))


def flash(fid, tick, *beats):
    return FlashSighting(fid, tick, frozenset(beats))


class TestAxis(unittest.TestCase):
    def test_identity_two_benchmarks_unique(self):
        sols = solve_axis(spec("c", [bp(10, 10), bp(90, 90)]))
        self.assertEqual(sols, [AxisSolution(+1, 0)])

    def test_reversed_axis(self):
        sols = solve_axis(spec("c", [bp(90, 10), bp(10, 90)]))
        self.assertEqual(sols, [AxisSolution(-1, 100)])
        self.assertEqual(sols[0].to_field(39), 61)

    def test_ambiguous_actuals_keep_multiple_translations(self):
        # 单条标杆给两个可能实际位置：+1 下 t 可为 0 或 5
        sols = solve_axis(spec("c", [bp(10, 10, 15)]))
        self.assertIn(AxisSolution(+1, 0), sols)
        self.assertIn(AxisSolution(+1, 5), sols)

    def test_contradictory_benchmarks_no_solution(self):
        sols = solve_axis(spec("c", [bp(10, 10), bp(10, 20)]))
        self.assertEqual(sols, [])

    def test_intersection_across_benchmarks(self):
        # 第一张卡 t∈{0,5}，第二张卡只留 t=0
        sols = solve_axis(spec("c", [bp(10, 10, 15), bp(20, 20)]))
        self.assertEqual(sols, [AxisSolution(+1, 0)])


class TestOffsets(unittest.TestCase):
    def test_flash_derives_offset_and_album_filters(self):
        camera = spec("c", [bp(10, 10)], [flash("F1", 4, 5)], album=(0, -1))
        offsets, failures = solve_offsets(camera)
        self.assertEqual(offsets, {-1})         # 4 = 5 + offset
        self.assertEqual(failures, [])

    def test_offset_outside_album_rejected(self):
        camera = spec("c", [bp(10, 10)], [flash("F1", 4, 5)], album=(0,))
        offsets, failures = solve_offsets(camera)
        self.assertEqual(offsets, set())
        self.assertEqual(failures[0].code, AdmissionCode.NO_OFFSET_IN_ALBUM)

    def test_two_flashes_intersection(self):
        camera = spec("c", [bp(10, 10)],
                      [flash("F1", 4, 4, 5), flash("F2", 6, 5)],
                      album=(0, 1))
        offsets, _ = solve_offsets(camera)
        # F1: off∈{0,-1}；F2: off=1；交集为空
        self.assertEqual(offsets, set())

    def test_no_flash_falls_back_to_album(self):
        camera = spec("c", [bp(10, 10)], (), album=(0, -1))
        offsets, failures = solve_offsets(camera)
        self.assertEqual(offsets, {0, -1})
        self.assertEqual(failures, [])


class TestJointAdmission(unittest.TestCase):
    def test_shared_flash_locks_same_beat(self):
        a = spec("A", [bp(10, 10), bp(90, 90)], [flash("F1", 4, 5)],
                 album=(0, -1))
        b = spec("B", [bp(10, 10), bp(90, 90)], [flash("F1", 4, 5)],
                 album=(0, -1))
        result = admit((a, b))
        self.assertTrue(result.admitted)
        self.assertEqual(len(result.interpretations), 1)
        self.assertEqual(result.interpretations[0].offsets,
                         {"A": -1, "B": -1})

    def test_flash_conflict_between_cameras_rejected(self):
        a = spec("A", [bp(10, 10), bp(90, 90)], [flash("F1", 4, 4)],
                 album=(0, -1))
        b = spec("B", [bp(10, 10), bp(90, 90)], [flash("F1", 4, 5)],
                 album=(-1, 0))
        result = admit((a, b))
        self.assertFalse(result.admitted)
        self.assertTrue(
            any(f.code == AdmissionCode.FLASH_CONFLICT for f in result.failures))

    def test_no_flash_two_album_values_yields_four_interpretations(self):
        a = spec("A", [bp(10, 10), bp(90, 90)], (), album=(0, -1))
        b = spec("B", [bp(10, 10), bp(90, 90)], (), album=(0, -1))
        result = admit((a, b))
        self.assertTrue(result.admitted)
        self.assertEqual(len(result.interpretations), 4)

    def test_axis_failure_reported_per_camera(self):
        a = spec("A", [bp(10, 10), bp(10, 20)], (), album=(0,))
        result = admit((a,))
        self.assertFalse(result.admitted)
        self.assertEqual(result.failures[0].code,
                         AdmissionCode.NO_AXIS_SOLUTION)

    def test_opposing_camera_directions_both_admitted(self):
        # 两台机看见的方向相反：A 正向 sign=+1/t=0，B 反向 sign=-1/t=100
        a = spec("A", [bp(10, 10), bp(90, 90)], album=(0,))
        b = spec("B", [bp(90, 10), bp(10, 90)], album=(0,))
        result = admit((a, b))
        self.assertTrue(result.admitted)
        axes = result.interpretations[0].axes
        self.assertEqual((axes["A"].sign, axes["A"].translation), (1, 0))
        self.assertEqual((axes["B"].sign, axes["B"].translation), (-1, 100))
        # 同一物理点 61：A 格 61，B 格 39，还原到同一真位置
        self.assertEqual(axes["A"].to_field(61), axes["B"].to_field(39))


if __name__ == "__main__":
    unittest.main()
