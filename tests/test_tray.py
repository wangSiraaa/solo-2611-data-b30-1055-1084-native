"""接盘台与齐盒测试：落点接糖、两盘不穿越、缓冲槽、延时扣、完整盒报告。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from animation_pipeline.demold import Arrival
from animation_pipeline.mold_demo import (
    buffer_scenario,
    buffer_two_late_scenario,
    coupling_two_tray_scenario,
    delay_pin_scenario,
    example_scenario,
)
from animation_pipeline.puzzle import (
    choose_device,
    demold_options,
    report_boxes,
    report_teacher,
    solve,
)
from animation_pipeline.tray import (
    BufferDevice,
    DelayPinDevice,
    Tray,
    TrayCell,
    simulate_trays,
    validate_trajectory,
)


class TrayMechanicsTests(unittest.TestCase):
    def _one_tray(self, initial: int, times, stride: int, offs) -> Tray:
        return Tray(
            "t0", initial, tuple(times), stride,
            tuple(TrayCell(n, o, box=None) for n, o in offs),
        )

    def test_catch_requires_slot_in_station(self) -> None:
        tray = self._one_tray(-5, (), 0, [("s", 0)])
        arr = [Arrival("c", "x", 5, 0)]
        # 盘格始终在 -5，落点 0 无空格 -> 落空。
        r = simulate_trays(arr, [tray], {}, {"t0": {}})
        self.assertEqual(len(r.lost), 1)
        # 盘格在 0 时则接住。
        tray2 = Tray("t0", 0, (), 0, (TrayCell("s", 0),))
        r2 = simulate_trays(arr, [tray2], {}, {"t0": {}})
        self.assertEqual(r2.lost, [])
        self.assertEqual(r2.cells[("t0", "s")].cavity, "c")

    def test_two_trays_cannot_cross(self) -> None:
        left = Tray("L", 0, (10,), 5, (TrayCell("a", 0),))
        right = Tray("R", 3, (10,), 5, (TrayCell("b", 0),))
        # 让左走到 3，右不动 -> 重叠 3>=3，非法。
        with self.assertRaises(ValueError):
            validate_trajectory([left, right], {"L": {10: 3}, "R": {}})
        # 左走到 2，右在 3 -> 合法（2<3）。
        validate_trajectory([left, right], {"L": {10: 2}, "R": {}})

    def test_stride_limit_enforced(self) -> None:
        tray = Tray("t0", 0, (10,), 2, (TrayCell("s", 0),))
        with self.assertRaises(ValueError):
            validate_trajectory([tray], {"t0": {10: 5}})


class ExampleTrayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_example_completes_box(self) -> None:
        sol = solve(example_scenario(self.dir))
        self.assertEqual(sol.complete, ("原味盒",))
        rec = sol.tray_result.cells[("tray0", "slot")]
        self.assertEqual((rec.cavity, rec.flavor), ("c1", "原味"))

    def test_clear_local_loses_sugar(self) -> None:
        # 10 拍就脱 -> 11ms 到达，盘格尚未进站，落空。
        sol = solve(example_scenario(self.dir, disposal0=2))
        self.assertEqual(sol.complete, ())
        self.assertEqual([(a.cavity, a.time_ms) for a in sol.tray_result.lost], [("c1", 11)])

    def test_misassigned_slot_starves_original_box(self) -> None:
        sol = solve(example_scenario(self.dir, slot_box="他味盒"))
        self.assertEqual(sol.complete, ())
        lines = "\n".join(report_boxes(sol.tray_result))
        self.assertIn("原味盒", lines)
        self.assertIn("没有具名盘格", lines)


class CouplingTrayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_split_rows_complete_two_boxes(self) -> None:
        sol = solve(coupling_two_tray_scenario(self.dir, shared_row=False))
        self.assertEqual(set(sol.complete), {"盒A", "盒B"})

    def test_shared_row_completes_at_most_one(self) -> None:
        sol = solve(coupling_two_tray_scenario(self.dir, shared_row=True))
        self.assertEqual(len(sol.complete), 1)

    def test_teacher_report_explains_block_and_release(self) -> None:
        s = example_scenario(self.dir)
        ds = demold_options(s)[0]
        rep = report_teacher(s, ds, "row1", [10, 20])
        self.assertFalse(rep[10]["checks"]["c1"]["ok"])
        self.assertEqual(rep[10]["checks"]["c1"]["blocked"][0], (1, 0))
        self.assertTrue(rep[20]["checks"]["c1"]["ok"])
        self.assertEqual(rep[20]["pushed_here"], ["c1"])


class BufferTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_without_buffer_late_sugar_misses_box(self) -> None:
        sol = solve(buffer_scenario(self.dir))
        self.assertEqual(sol.complete, ("盒A",))
        # b 落进无盒的 M。
        self.assertEqual(sol.tray_result.cells[("tray0", "M")].cavity, "b")

    def test_buffer_recovers_box(self) -> None:
        dev = BufferDevice("tray0", "M", "B", deliver_time_ms=20)
        sol = solve(buffer_scenario(self.dir), device=dev)
        self.assertEqual(set(sol.complete), {"盒A", "盒B"})
        self.assertEqual(sol.tray_result.cells[("tray0", "B")].cavity, "b")
        self.assertEqual(sol.tray_result.cells[("tray0", "B")].via, "buffer")

    def test_one_buffer_cannot_hold_two(self) -> None:
        dev = BufferDevice("tray0", "M", "B", deliver_time_ms=20)
        sol = solve(buffer_two_late_scenario(self.dir), device=dev)
        # 缓槽只接一颗，另一颗落空；至多 2 盒（A+B），C 缺。
        self.assertNotIn("盒C", sol.complete)
        self.assertEqual(len(sol.tray_result.lost), 1)


class DelayPinScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_baseline_pin_swaps_which_box_completes(self) -> None:
        s = delay_pin_scenario(self.dir)
        base = solve(s)
        self.assertEqual(base.complete, ("盒A",))
        pinned = solve(s, device=DelayPinDevice("rowS", 10))
        self.assertEqual(pinned.complete, ("盒B",))


class DeviceChoiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_choose_buffer_when_it_yields_more_boxes(self) -> None:
        s = buffer_scenario(self.dir)
        choice = choose_device(s, [BufferDevice("tray0", "M", "B", 20)])
        self.assertEqual(choice.device_name, "BufferDevice")
        self.assertEqual(set(choice.best.complete), {"盒A", "盒B"})
        self.assertEqual(choice.baseline.complete, ("盒A",))

    def test_choose_baseline_when_pin_does_not_help(self) -> None:
        s = delay_pin_scenario(self.dir)
        choice = choose_device(s, [DelayPinDevice("rowS", 10)])
        # 扣只把盒从 A 换成 B，并不增多，故并列保留无设备。
        self.assertEqual(choice.device_name, "无设备")
        self.assertEqual(choice.best.complete, ("盒A",))

    def test_invalid_buffer_target_rejected(self) -> None:
        s = buffer_scenario(self.dir)
        # 目标指向无盒盘格 -> 非法。
        with self.assertRaises(ValueError):
            solve(s, device=BufferDevice("tray0", "M", "M", 20))


if __name__ == "__main__":
    unittest.main()
