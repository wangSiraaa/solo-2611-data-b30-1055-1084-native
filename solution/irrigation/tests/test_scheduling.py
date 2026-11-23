"""畦节奏、受水单枚举与跨班间歇测试。"""

import unittest

import _path  # noqa: F401
from scheduling import Bed, ScheduleError, enumerate_schedules

ALLOW_ALL = lambda group: True  # 试水环节由阀门测试覆盖，这里先全放行


class ScheduleShapeTests(unittest.TestCase):
    def test_bed_validation(self):
        with self.assertRaises(ScheduleError):
            Bed("x", frozenset(), frozenset({1}), need=1)
        with self.assertRaises(ScheduleError):
            Bed("x", frozenset({"A"}), frozenset({1}), need=2)
        with self.assertRaises(ScheduleError):
            Bed("x", frozenset({"A"}), frozenset({1, 2}), need=1, gap=-1)

    def test_two_beds_first_two_slots_has_four_raw_orders(self):
        beds = [
            Bed("A", frozenset({"a"}), frozenset({1, 2}), need=1),
            Bed("B", frozenset({"b"}), frozenset({1, 2}), need=1),
        ]
        result = enumerate_schedules(beds, [1, 2], ALLOW_ALL)
        self.assertEqual(len(result.feasible), 4)
        # 先 A 后 B 在其中。
        self.assertTrue(
            any(o.assignments[1] == {"A": "a"} and o.assignments[2] == {"B": "b"}
                for o in result.feasible)
        )

    def test_one_nozzle_one_bed_per_slot(self):
        # 两畦共用同一嘴且同格窗口：同格不能双双受水，只能错开。
        beds = [
            Bed("A", frozenset({"z"}), frozenset({1, 2}), need=1),
            Bed("B", frozenset({"z"}), frozenset({1, 2}), need=1),
        ]
        result = enumerate_schedules(beds, [1, 2], ALLOW_ALL)
        for order in result.feasible:
            for amap in order.assignments.values():
                self.assertLessEqual(len(amap), 1)

    def test_gap_between_two_waterings(self):
        bed = Bed("C", frozenset({"a"}), frozenset({1, 2, 3, 4}), need=2, gap=1)
        result = enumerate_schedules([bed], [1, 2, 3, 4], ALLOW_ALL)
        pairs = {o.watered_slots("C") for o in result.feasible}
        self.assertNotIn((1, 2), pairs)   # 中间没空格
        self.assertIn((1, 3), pairs)
        self.assertIn((2, 4), pairs)

    def test_history_carries_across_shift_boundary(self):
        # 上一班格 0 已浇；gap=1 时本班格 1 禁开，最早格 2。
        bed = Bed("C", frozenset({"a"}), frozenset({1, 2, 3}), need=1, gap=1)
        blocked = enumerate_schedules([bed], [1, 2, 3], ALLOW_ALL,
                                      history={"C": (0,)})
        slots = {o.watered_slots("C") for o in blocked.feasible}
        self.assertEqual(slots, {(2,), (3,)})
        # 无历史则格 1 即可开。
        fresh = enumerate_schedules([bed], [1, 2, 3], ALLOW_ALL)
        self.assertIn((1,), {o.watered_slots("C") for o in fresh.feasible})

    def test_pressure_failure_marks_blocker_not_feasible(self):
        beds = [
            Bed("A", frozenset({"a"}), frozenset({1}), need=1),
            Bed("B", frozenset({"b"}), frozenset({1}), need=1),
        ]
        only_single = lambda g: len(g) == 1  # 同开组不放行
        result = enumerate_schedules(beds, [1], only_single)
        self.assertFalse(result.feasible)
        self.assertTrue(any("试水" in b for b in result.blockers))

    def test_multi_nozzle_bed_choice(self):
        bed = Bed("A", frozenset({"a1", "a2"}), frozenset({1}), need=1)
        result = enumerate_schedules([bed], [1], ALLOW_ALL)
        nozzles = {next(iter(o.assignments[1].values())) for o in result.feasible}
        self.assertEqual(nozzles, {"a1", "a2"})


if __name__ == "__main__":
    unittest.main()
