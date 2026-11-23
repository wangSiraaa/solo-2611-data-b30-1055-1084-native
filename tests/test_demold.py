"""开模核验测试：真实扫过形状 × 模壁坐标、共轴整排耦合、脉冲表与延时扣。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from animation_pipeline.demold import (
    Cavity,
    DemoldWorld,
    cavity_can_demold,
    explain_cavity,
    simulate_demold,
)
from animation_pipeline.mold_demo import (
    ROLES,
    coupling_two_tray_scenario,
    delay_pin_scenario,
    example_scenario,
)


class ExampleDemoldTests(unittest.TestCase):
    """合同例子：保留首帧壁时仅 20 拍能脱；清局部后 10 拍就脱。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_swept_shape_uses_real_wall_coords(self) -> None:
        s = example_scenario(self.dir)
        c = s.cavities["c1"]
        self.assertEqual(sorted(c.swept()), [(1, 0), (1, 1)])
        # 帧时间轴：0/10/20ms，第三帧起点 20ms 才是空位。
        self.assertEqual(s.world.frame_index_at_ms(10), 1)
        self.assertEqual(s.world.frame_index_at_ms(20), 2)

    def test_blocked_at_10_released_at_20(self) -> None:
        s = example_scenario(self.dir)
        c = s.cavities["c1"]
        chk10 = explain_cavity(s.world, c, 10)
        self.assertFalse(chk10.ok)
        self.assertEqual(chk10.blocked_by()[0], (1, 0))
        self.assertEqual(chk10.blocked_by()[2], "wall_a")
        self.assertTrue(cavity_can_demold(s.world, c, 20))

    def test_demold_only_at_second_pulse(self) -> None:
        s = example_scenario(self.dir)
        ds = simulate_demold(s.world, s.cavities, s.rows, {"row1": 0})
        self.assertEqual([(e.cavity, e.time_ms) for e in ds.events], [("c1", 20)])
        statuses = [(r.time_ms, r.status) for r in ds.reports]
        self.assertEqual(statuses, [(10, "blocked"), (20, "pushed")])

    def test_clear_local_makes_10_release(self) -> None:
        s = example_scenario(self.dir, disposal0=2)
        ds = simulate_demold(s.world, s.cavities, s.rows, {"row1": 0})
        self.assertEqual([(e.cavity, e.time_ms) for e in ds.events], [("c1", 10)])


class CoaxialCouplingTests(unittest.TestCase):
    """共顶排：同拍出，不能一个早一个晚。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_shared_row_forces_same_pulse(self) -> None:
        s = coupling_two_tray_scenario(self.dir, shared_row=True)
        # 唯一表：10 拍两腔同出。
        ds = simulate_demold(s.world, s.cavities, s.rows, {"rowS": 0})
        self.assertEqual(
            sorted((e.cavity, e.time_ms) for e in ds.events),
            [("a", 10), ("b", 10)],
        )

    def test_blocked_any_cavity_holds_whole_row(self) -> None:
        s = delay_pin_scenario(self.dir, wall_on_b=True)
        # a 在 20 拍本身可脱，但 b 碰新壁 -> 整排不走（pin 把本拍改到 20）。
        ds = simulate_demold(
            s.world, s.cavities, s.rows, {"rowS": 0}, pin=("rowS", 10)
        )
        self.assertEqual(ds.events, [])
        self.assertEqual(set(ds.remaining), {"a", "b"})
        # 单独看 a 在 20 拍是自由的，证明是“整排耦合”而非 a 自身受阻。
        self.assertTrue(cavity_can_demold(s.world, s.cavities["a"], 20))
        self.assertFalse(cavity_can_demold(s.world, s.cavities["b"], 20))


class DelayPinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_pin_moves_whole_row_to_next_pulse(self) -> None:
        s = delay_pin_scenario(self.dir)
        ds = simulate_demold(
            s.world, s.cavities, s.rows, {"rowS": 0}, pin=("rowS", 10)
        )
        # 10 拍整排被推迟，20 拍整排一起出。
        self.assertEqual(
            sorted((e.cavity, e.time_ms) for e in ds.events),
            [("a", 20), ("b", 20)],
        )
        self.assertEqual(
            [(r.time_ms, r.status) for r in ds.reports],
            [(10, "delayed"), (20, "pushed")],
        )

    def test_pin_requires_real_late_frame_check(self) -> None:
        # 晚帧 b 碰壁：即使 a 自由，整排也不走（见 CoaxialCouplingTests）。
        s = delay_pin_scenario(self.dir, wall_on_b=True)
        ds = simulate_demold(
            s.world, s.cavities, s.rows, {"rowS": 0}, pin=("rowS", 10)
        )
        self.assertEqual(ds.events, [])


class ReflectionAndRolesTests(unittest.TestCase):
    def test_both_wall_groups_block(self) -> None:
        # 两组模壁颜色都挡糖；倒影/背景不挡。
        from animation_pipeline.catalog import FrameCatalog
        from animation_pipeline.gif_io import frame_spec, write_gif
        from animation_pipeline.mold_demo import BG, BLUE, PALETTE

        with tempfile.TemporaryDirectory() as d:
            p = str(Path(d) / "wb.gif")
            write_gif(
                p, 3, 2, BG, PALETTE,
                [frame_spec(0, 0, 1, 1, [BLUE], delay_centis=1, disposal=1)],
            )
            world = DemoldWorld(FrameCatalog.from_gif(p, None), ROLES)
            cav = Cavity("c", frozenset({(0, 1)}), (0, -1), 1, 0, 1, "x")
            self.assertEqual(world.role_at(0, (0, 0)), "wall_b")
            self.assertFalse(cavity_can_demold(world, cav, 0))

    def test_reflection_not_wall(self) -> None:
        # 倒影色不占位：即便画在扫过格上也不挡。
        from animation_pipeline.catalog import FrameCatalog
        from animation_pipeline.gif_io import frame_spec, write_gif
        from animation_pipeline.mold_demo import BG, GRAY, PALETTE

        with tempfile.TemporaryDirectory() as d:
            p = str(Path(d) / "refl.gif")
            write_gif(
                p, 4, 4, BG, PALETTE,
                [frame_spec(1, 0, 1, 1, [GRAY], delay_centis=1, disposal=1),
                 frame_spec(1, 0, 1, 1, [GRAY], delay_centis=1, disposal=1)],
            )
            world = DemoldWorld(FrameCatalog.from_gif(p, None), ROLES)
            cav = Cavity("c", frozenset({(1, 1)}), (0, -1), 1, 0, 1, "x")
            self.assertEqual(world.role_at(0, (1, 0)), "reflection")
            self.assertTrue(cavity_can_demold(world, cav, 0))


if __name__ == "__main__":
    unittest.main()
