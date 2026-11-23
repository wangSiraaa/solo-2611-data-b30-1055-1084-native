"""场记读法测试：种子定色、同色四邻、合并色块、定位孔、整形/朝向/定位比对。

所有画布都用 numpy 直接构造“已合成好的显示画布”，不经过 GIF 处置——
读法本就只吃最终显示画面。
"""

from __future__ import annotations

import unittest

import numpy as np

from animation_pipeline.puppets import (
    ORIENTATION_LEFT,
    ORIENTATION_RIGHT,
    Hole,
    Mark,
    Pattern,
    Seed,
    read_scene,
)

RED = (220, 40, 40)
GREEN = (40, 180, 60)
BLUE = (50, 80, 220)


def canvas(w=8, h=6, bg=(0, 0, 0)):
    arr = np.zeros((h, w, 4), dtype=np.uint8)
    arr[:, :, :3] = bg
    arr[:, :, 3] = 255
    return arr


def put(arr, x, y, rgb):
    arr[y, x, :3] = rgb
    arr[y, x, 3] = 255


DOUBLE_LEFT = Pattern("double", frozenset({(0, 0), (1, 0)}), notch=(1, 0),
                      front_costume="cf", back_costume="cb", orientation=ORIENTATION_LEFT)
LEFT_ONLY = Pattern("one", frozenset({(0, 0)}), notch=(0, 0),
                    front_costume="cf", back_costume=None, orientation=ORIENTATION_LEFT)
BOTH = frozenset({ORIENTATION_LEFT, ORIENTATION_RIGHT})


def mark(puppet="p", x=0, y=0, color=RED, hole=Hole("h", 0, 0), orientations=BOTH):
    return Mark(Seed(puppet, x, y, color), hole, frozenset(orientations))


class ComponentTests(unittest.TestCase):
    def test_same_color_four_neighbors_make_body(self) -> None:
        c = canvas()
        put(c, 0, 0, RED)
        put(c, 1, 0, RED)  # 4 邻接
        put(c, 2, 1, RED)  # 对角不连（4 邻域）
        r = read_scene(c, (mark(),), (DOUBLE_LEFT,), frame_id=0, time_ms=0)
        self.assertEqual(r.puppets["p"].body_cells, frozenset({(0, 0), (1, 0)}))
        self.assertTrue(r.puppets["p"].visible)

    def test_transparent_pixels_do_not_join(self) -> None:
        c = canvas()
        put(c, 0, 0, RED)
        c[0, 1, 3] = 0  # 中间被透明隔开
        put(c, 2, 0, RED)
        r = read_scene(c, (mark(),), (DOUBLE_LEFT,), frame_id=0, time_ms=0)
        self.assertEqual(r.puppets["p"].body_cells, frozenset({(0, 0)}))

    def test_seed_wrong_color_invisible(self) -> None:
        c = canvas()
        put(c, 0, 0, GREEN)
        r = read_scene(c, (mark(color=RED),), (DOUBLE_LEFT,), frame_id=0, time_ms=0)
        pr = r.puppets["p"]
        self.assertFalse(pr.visible)
        self.assertIsNone(pr.pose)
        self.assertIn("不可见", pr.review_basis)

    def test_seed_transparent_invisible(self) -> None:
        c = canvas()
        c[0, 0, 3] = 0
        r = read_scene(c, (mark(),), (DOUBLE_LEFT,), frame_id=0, time_ms=0)
        self.assertFalse(r.puppets["p"].visible)

    def test_two_seeds_same_block_merged_no_pattern(self) -> None:
        c = canvas()
        put(c, 0, 0, RED)
        put(c, 1, 0, RED)  # 两块同色相接
        marks = (
            mark("a", 0, 0),
            mark("b", 1, 0),
        )
        r = read_scene(c, marks, (DOUBLE_LEFT,), frame_id=0, time_ms=0)
        self.assertTrue(r.puppets["a"].merged)
        self.assertTrue(r.puppets["b"].merged)
        self.assertIsNone(r.puppets["a"].pose)
        self.assertIsNone(r.puppets["b"].pose)
        self.assertIn("同一个色块", r.puppets["a"].review_basis)

    def test_hole_relative_local_coordinates(self) -> None:
        c = canvas()
        put(c, 3, 2, RED)
        put(c, 4, 2, RED)
        m = mark(x=3, y=2, hole=Hole("h", 3, 2))
        r = read_scene(c, (m,), (DOUBLE_LEFT,), frame_id=0, time_ms=0)
        self.assertEqual(r.puppets["p"].body_cells, frozenset({(0, 0), (1, 0)}))
        self.assertEqual(r.puppets["p"].pose.name, "double")


class MatchTests(unittest.TestCase):
    def test_shape_mismatch_no_pattern(self) -> None:
        c = canvas()
        put(c, 0, 0, RED)
        put(c, 1, 0, RED)
        put(c, 2, 0, RED)  # 三格
        r = read_scene(c, (mark(),), (DOUBLE_LEFT,), frame_id=0, time_ms=0)
        pr = r.puppets["p"]
        self.assertEqual(pr.alternatives, ())
        self.assertIsNone(pr.pose)
        self.assertIn("没有", pr.review_basis)

    def test_mirrored_shape_matches_only_mirror(self) -> None:
        c = canvas()
        put(c, 0, 0, RED)
        put(c, -1 + 0, 0, RED)  # 占位，下面改用正确坐标
        c2 = canvas()
        # 身形在孔左侧：(0,0) 与 (-1,0)
        c2[0, 4] = (220, 40, 40, 255)
        c2[0, 3] = (220, 40, 40, 255)
        m = mark(x=4, y=0, hole=Hole("h", 4, 0))
        r = read_scene(c2, (m,), (DOUBLE_LEFT,), frame_id=0, time_ms=0)
        pr = r.puppets["p"]
        self.assertEqual(len(pr.alternatives), 1)
        self.assertTrue(pr.alternatives[0].mirrored)
        self.assertEqual(pr.alternatives[0].orientation, ORIENTATION_RIGHT)

    def test_orientation_not_allowed_blocks_mirror(self) -> None:
        c = canvas()
        c[0, 4] = (220, 40, 40, 255)
        c[0, 3] = (220, 40, 40, 255)
        m = mark(x=4, y=0, hole=Hole("h", 4, 0), orientations={ORIENTATION_LEFT})
        r = read_scene(c, (m,), (DOUBLE_LEFT,), frame_id=0, time_ms=0)
        self.assertEqual(r.puppets["p"].alternatives, ())
        self.assertIsNone(r.puppets["p"].pose)

    def test_symmetric_single_cell_is_ambiguous_group(self) -> None:
        # 单格在孔上，原样与镜像格集相同，两向都允许 → 成组替选。
        c = canvas()
        put(c, 0, 0, RED)
        r = read_scene(c, (mark(),), (LEFT_ONLY,), frame_id=0, time_ms=0)
        pr = r.puppets["p"]
        self.assertEqual(len(pr.alternatives), 2)
        self.assertFalse(pr.determinate)
        self.assertIsNone(pr.pose)
        self.assertIn("替选", pr.review_basis)

    def test_unique_match_is_determinate(self) -> None:
        c = canvas()
        put(c, 0, 0, RED)
        put(c, 1, 0, RED)
        r = read_scene(c, (mark(),), (DOUBLE_LEFT,), frame_id=0, time_ms=0)
        self.assertTrue(r.puppets["p"].determinate)
        self.assertEqual(r.puppets["p"].pose.name, "double")

    def test_two_patterns_same_shape_yield_alternatives(self) -> None:
        twin = Pattern("double", frozenset({(0, 0), (1, 0)}), notch=(0, 0),
                       front_costume="cf", back_costume="cb")
        c = canvas()
        put(c, 0, 0, RED)
        put(c, 1, 0, RED)
        r = read_scene(c, (mark(),), (DOUBLE_LEFT, twin), frame_id=0, time_ms=0)
        # 两张同整形纸样都能解释 → 替选。
        self.assertEqual(len(r.puppets["p"].alternatives), 2)
        self.assertFalse(r.puppets["p"].determinate)


class OcclusionTests(unittest.TestCase):
    """短暂遮住后露出的造型，仍按那一刻的显示画面读取。"""

    def test_covered_then_revealed(self) -> None:
        # 直接喂三张显示画布，模拟“显示在遮住/露出那一刻的样子”。
        shown = canvas()
        put(shown, 0, 0, RED)
        put(shown, 1, 0, RED)

        covered = canvas()
        put(covered, 0, 0, GREEN)  # 别的东西盖住了种子
        put(covered, 1, 0, GREEN)

        revealed = canvas()
        put(revealed, 0, 0, RED)
        put(revealed, 1, 0, RED)

        marks = (mark(),)
        pats = (DOUBLE_LEFT,)
        r0 = read_scene(shown, marks, pats, frame_id=0, time_ms=0)
        r1 = read_scene(covered, marks, pats, frame_id=1, time_ms=10)
        r2 = read_scene(revealed, marks, pats, frame_id=2, time_ms=20)
        self.assertEqual(r0.puppets["p"].pose.name, "double")
        self.assertFalse(r1.puppets["p"].visible)  # 遮住时读不到
        # 露出后按那一刻画面读回双格红，绝不回填记忆之外的东西。
        self.assertEqual(r2.puppets["p"].pose.name, "double")


if __name__ == "__main__":
    unittest.main()
