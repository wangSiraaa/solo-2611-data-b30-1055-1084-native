"""像素剧场走位工具测试：接触拍识别、踩实重建、站台禁线与两种保对位修复。

所有结论按合同口径做整数坐标核验；识别用图由本仓库 compositor 的
FramePlayer 真实合成产生（含处置 1 残轮廓、处置 2 清除与透明补画），
不手画合成结果。识别读法的 heel/toe 是**帧局部坐标**（帧原点＝角色根点），
世界落点 = 当拍根点 + 局部坐标；根点只沿 x 轴走位，y 为脚的固定行。
"""

from __future__ import annotations

import unittest

import numpy as np

from animation_pipeline.catalog import FrameCatalog
from animation_pipeline.compositor import FramePlayer
from animation_pipeline.footwork import (
    REASON_CARRIED_OVER_LINE,
    REASON_CONFLICT_DELTA,
    REASON_EXIT_MISMATCH,
    REASON_GLUED_SUPPORT,
    REASON_LOCKED_BEAT,
    REASON_NO_READING,
    REASON_PHASE_BOUNDARY,
    REASON_SHIM_CONTENDED,
    REASON_UNSUPPORTED,
    BlockedBeat,
    FootPhase,
    SoleTemplate,
    StepSolution,
    reconstruct,
    recognize_foot,
)
from animation_pipeline.staging import (
    MAX_BEATS,
    MAX_PLATFORMS,
    Line,
    Platform,
    ShimLedger,
    ShimRequest,
    ShimSlot,
    Stage,
    StagePlanner,
    StepSegment,
)

LEFT_RGB = (200, 40, 40)
RIGHT_RGB = (40, 80, 220)
BG = (250, 250, 250)
L, R = "L", "R"
LY, RY = 0, 1
W, H = 16, 2
BOX = (0, 0, W, H)


def sole(foot_id: str, rgb) -> SoleTemplate:
    """两格鞋底放在该脚所在行；另一行为透明格（不参与匹配）。"""
    row = LY if foot_id == L else RY
    return SoleTemplate(
        foot_id=foot_id,
        sample_rgb=rgb,
        follow_point=(0, row),
        toe_point=(1, row),
        cells=((0, row, rgb), (1, row, rgb)),
        width=2,
        height=2,
    )


def canvas() -> np.ndarray:
    arr = np.zeros((H, W, 4), dtype=np.uint8)
    arr[:, :, :3] = BG
    arr[:, :, 3] = 255
    return arr


def put(arr: np.ndarray, x: int, y: int, rgb, alpha: int = 255) -> None:
    arr[y, x] = (rgb[0], rgb[1], rgb[2], alpha)


def draw_foot(arr: np.ndarray, heel_x: int, rgb, row: int) -> None:
    put(arr, heel_x, row, rgb)
    put(arr, heel_x + 1, row, rgb)


def draw_l(arr: np.ndarray, heel_x: int) -> None:
    draw_foot(arr, heel_x, LEFT_RGB, LY)


def draw_r(arr: np.ndarray, heel_x: int) -> None:
    draw_foot(arr, heel_x, RIGHT_RGB, RY)


def recognize_all(img_by_beat: dict) -> dict:
    from animation_pipeline.footwork import recognize_beat

    tpl = {L: sole(L, LEFT_RGB), R: sole(R, RIGHT_RGB)}
    boxes = {L: BOX, R: BOX}
    return {b: recognize_beat(img, tpl, boxes, b) for b, img in img_by_beat.items()}


# --------------------------------------------------------------------------- #
# 一、接触拍识别
# --------------------------------------------------------------------------- #

class RecognitionTests(unittest.TestCase):
    def test_01_integer_coordinates_all_exposed_pixels_match(self) -> None:
        img = canvas()
        draw_foot(img, 5, LEFT_RGB, LY)
        reads = recognize_foot(img, sole(L, LEFT_RGB), beat=0, search_box=BOX)
        self.assertEqual(len(reads), 1)
        p = reads[0]
        self.assertEqual((p.heel, p.toe), ((5, 0), (6, 0)))
        self.assertFalse(p.ambiguous)

    def test_02_transparent_fill_does_not_constrain(self) -> None:
        # 纸型只有两格不透明；纸型外的异色像素不参与匹配（透明补画保真）。
        img = canvas()
        draw_foot(img, 7, RIGHT_RGB, RY)
        put(img, 10, RY, (0, 0, 0))
        reads = recognize_foot(img, sole(R, RIGHT_RGB), 0, BOX)
        self.assertEqual([p.heel for p in reads], [(7, 1)])

    def test_03_alpha_zero_pixel_blocks_only_that_origin(self) -> None:
        img = canvas()
        put(img, 3, LY, LEFT_RGB)
        put(img, 4, LY, LEFT_RGB, alpha=0)  # 第二格透明 -> 原点3 不可贴
        reads = recognize_foot(img, sole(L, LEFT_RGB), 0, BOX)
        self.assertNotIn((3, 0), [p.heel for p in reads])

    def test_04_overlapping_two_readings_both_kept_and_flagged(self) -> None:
        # 连续三个同色像素：两格纸型可贴原点 2 与 3，读法在 x=3 重叠。
        img = canvas()
        for x in (2, 3, 4):
            put(img, x, LY, LEFT_RGB)
        reads = recognize_foot(img, sole(L, LEFT_RGB), 0, BOX)
        self.assertEqual(sorted(p.heel for p in reads), [(2, 0), (3, 0)])
        self.assertTrue(all(p.ambiguous for p in reads))

    def test_05_search_box_clips_origins(self) -> None:
        img = canvas()
        draw_foot(img, 1, LEFT_RGB, LY)
        reads = recognize_foot(img, sole(L, LEFT_RGB), 0, (3, 0, W, H))
        self.assertEqual(reads, [])
        # 高度不足两行的搜索区装不下两行高的纸型。
        with self.assertRaises(ValueError):
            recognize_foot(img, sole(L, LEFT_RGB), 0, (0, 1, W, H))


class ResidualOutlineTests(unittest.TestCase):
    """前一帧处置后留下的轮廓必须照常可贴（识别只吃完整合成图）。"""

    def _catalog(self, disposal_first: int) -> FrameCatalog:
        cat = FrameCatalog(W, H, BG)
        body0 = np.zeros((1, 8, 4), dtype=np.uint8)
        put(body0, 4, 0, LEFT_RGB)
        put(body0, 5, 0, LEFT_RGB)
        cat.add_frame((0, 0, 8, 1), body0, delay_centis=10, disposal=disposal_first)
        body1 = np.zeros((1, 8, 4), dtype=np.uint8)
        put(body1, 1, 0, RIGHT_RGB)
        put(body1, 2, 0, RIGHT_RGB)
        cat.add_frame((8, 1, 8, 1), body1, delay_centis=10, disposal=1)
        return cat

    def test_06_residual_outline_remains_matchable(self) -> None:
        # 处置1：帧0 的左脚轮廓保留到帧1 合成图，与新画右脚同时可识别。
        canvases = [d.canvas for d in FramePlayer(self._catalog(1)).replay()]
        left_reads = recognize_foot(canvases[1], sole(L, LEFT_RGB), 1, BOX)
        right_reads = recognize_foot(canvases[1], sole(R, RIGHT_RGB), 1, BOX)
        self.assertEqual([p.heel for p in left_reads], [(4, 0)])
        self.assertEqual([p.heel for p in right_reads], [(9, 1)])

    def test_07_early_frame_disposal_edit_erases_contact_foot(self) -> None:
        # 处置2：帧0 子矩形在帧1 前恢复背景，左脚接触读法消失。
        canvases = [d.canvas for d in FramePlayer(self._catalog(2)).replay()]
        self.assertEqual(recognize_foot(canvases[1], sole(L, LEFT_RGB), 1, BOX), [])
        self.assertEqual(
            [p.heel for p in recognize_foot(canvases[1], sole(R, RIGHT_RGB), 1, BOX)],
            [(9, 1)],
        )


# --------------------------------------------------------------------------- #
# 二、踩实重建（规范示例的三拍走位）
# --------------------------------------------------------------------------- #

def example_images(erase: bool = False) -> dict:
    """规范示例的局部坐标：
    拍0 L=1 R=2；拍1 L=0 R=2；拍2 R=1（缺 L）。
    区间0 左脚局部 1->0，根点位移 = 前-后 = +1；
    区间1 右脚局部 2->1，根点位移同样 = +1；末根恰到 2。
    """
    img = {0: canvas(), 1: canvas(), 2: canvas()}
    draw_l(img[0], 1)
    draw_r(img[0], 2)
    draw_l(img[1], 0)
    draw_r(img[1], 2)
    if not erase:
        draw_r(img[2], 1)
    return img


def example_placements(erase: bool = False) -> dict:
    return recognize_all(example_images(erase))


EXAMPLE_PLANTS = {0: (L,), 1: (R,)}


class ReconstructionTests(unittest.TestCase):
    def test_08_example_walk_roots_0_to_2(self) -> None:
        result = reconstruct(example_placements(), EXAMPLE_PLANTS, (0, 0), exit_root=(2, 0))
        self.assertTrue(result.ok, result.blocked)
        sol = result.solutions[0]
        self.assertEqual(sol.roots, ((0, 0), (1, 0), (2, 0)))
        self.assertEqual(sol.root_deltas, ((1, 0), (1, 0)))
        # 共享拍读法只选一次：拍1 的 L 与 R 各自唯一（局部坐标）。
        self.assertEqual(sol.landing(0, L), (1, 0))
        self.assertEqual(sol.landing(1, L), (0, 0))
        self.assertEqual(sol.landing(1, R), (2, 1))
        self.assertEqual(sol.landing(2, R), (1, 1))
        # 世界落点 = 根点 + 局部：右脚两拍世界同为 (3,1)，踩实不动。
        self.assertEqual(sol.world_landing(1, R), (3, 1))
        self.assertEqual(sol.world_landing(2, R), (3, 1))
        self.assertEqual(sol.entry_phase, FootPhase.of(L))
        self.assertEqual(sol.exit_phase, FootPhase.of(R))

    def test_09_terminal_foot_painted_at_wrong_x_misses_stage_mouth(self) -> None:
        # 末拍右脚画在局部 x=2（规范要求 x=1）：末根只到 1，台口 2 接不到。
        img = canvas()
        draw_r(img, 2)
        placements = example_placements()
        placements[2] = {L: [], R: recognize_foot(img, sole(R, RIGHT_RGB), 2, BOX)}
        result = reconstruct(placements, EXAMPLE_PLANTS, (0, 0), exit_root=(2, 0))
        self.assertEqual(result.solutions, ())
        self.assertIn(REASON_EXIT_MISMATCH, [b.reason for b in result.blocked])

    def test_10_restore_artwork_but_demand_exit_root_3_still_fails(self) -> None:
        # 画稿合法（末根=2），仅把末根要求改到 3：动作记录本身合法，演出过不了。
        result = reconstruct(example_placements(), EXAMPLE_PLANTS, (0, 0), exit_root=(3, 0))
        self.assertEqual(result.solutions, ())
        self.assertEqual([b.reason for b in result.blocked], [REASON_EXIT_MISMATCH])
        self.assertEqual(result.blocked[0].detail, "末根(2, 0)对不上台口(3, 0)")
        # 不要求台口时同一份动作记录仍然合法。
        self.assertTrue(reconstruct(example_placements(), EXAMPLE_PLANTS, (0, 0)).ok)

    def test_11_contact_foot_disappears_after_early_edit(self) -> None:
        result = reconstruct(
            example_placements(erase=True), EXAMPLE_PLANTS, (0, 0), exit_root=(2, 0)
        )
        self.assertEqual(result.solutions, ())
        self.assertIn(REASON_NO_READING, [b.reason for b in result.blocked])

    def test_12_both_feet_planted_with_conflicting_deltas(self) -> None:
        # 双脚同时踩实却给出不同位移：区间无解。
        plants = {0: (L, R), 1: (R,)}
        result = reconstruct(example_placements(), plants, (0, 0))
        self.assertEqual(result.solutions, ())
        self.assertIn(REASON_CONFLICT_DELTA, [b.reason for b in result.blocked])

    def test_13_both_feet_planted_with_same_delta_is_one_solution(self) -> None:
        # 构造两脚在区间0 给出相同位移 +1 的读法（L 1->0，R 3->2）。
        img = {0: canvas(), 1: canvas(), 2: canvas()}
        draw_l(img[0], 1)
        draw_r(img[0], 3)
        draw_l(img[1], 0)
        draw_r(img[1], 2)
        draw_r(img[2], 1)
        placements = recognize_all(img)
        plants = {0: (L, R), 1: (R,)}
        result = reconstruct(placements, plants, (0, 0), exit_root=(2, 0))
        self.assertTrue(result.ok, result.blocked)
        self.assertEqual(result.solutions[0].root_deltas, ((1, 0), (1, 0)))

    def test_14_ambiguous_reading_marked_in_solution(self) -> None:
        img = canvas()
        for x in (0, 1, 2):  # 连续三格 -> 重叠读法
            put(img, x, LY, LEFT_RGB)
        placements = {
            0: {L: recognize_foot(img, sole(L, LEFT_RGB), 0, BOX), R: []},
            1: {L: recognize_foot(img, sole(L, LEFT_RGB), 1, BOX), R: []},
        }
        result = reconstruct(placements, {0: (L,)}, (0, 0))
        self.assertTrue(result.ok)
        self.assertTrue(all(s.ambiguous_beats for s in result.solutions))
        self.assertEqual(len(result.solutions), 4)  # 两拍各两种读法


def make_segment(
    seg_id: str,
    placements: dict,
    plants: dict,
    entry_root=(0, 0),
    exit_root=None,
) -> StepSegment:
    result = reconstruct(placements, plants, entry_root, exit_root=exit_root)
    if not result.ok:
        raise AssertionError(f"段 {seg_id} 重建失败: {result.blocked}")
    sol = result.solutions[0]
    return StepSegment(
        segment_id=seg_id,
        solution=sol,
        plants=dict(plants),
        legal_entry_phase=sol.entry_phase,
        legal_exit_phase=sol.exit_phase,
    )


# --------------------------------------------------------------------------- #
# 三、分层站台与禁线
# --------------------------------------------------------------------------- #

def world_cells(points) -> frozenset:
    return frozenset(points)


def basic_stage() -> Stage:
    # 世界落点：L 行 y=0 的 x=0,1；R 行 y=1 的 x=2,3。
    cells = frozenset({(0, 0), (1, 0), (2, 1), (3, 1)})
    return Stage({"A": Platform("A", cells)})


class StageVerificationTests(unittest.TestCase):
    def test_15_example_supported_on_named_surfaces(self) -> None:
        seg = make_segment("walk", example_placements(), EXAMPLE_PLANTS, (0, 0), (2, 0))
        supports, blocked = StagePlanner(basic_stage()).verify_segment(seg, (0, 0), (2, 0))
        self.assertIsNone(blocked)
        points = {(s.beat, s.foot): s.point for s in supports}
        self.assertEqual(points[(2, R)], (3, 1))
        self.assertTrue(all(s.platform == "A" for s in supports))

    def test_16_unsupported_landing_is_blocked_beat(self) -> None:
        # 抽掉右脚末拍世界落点 (3,1) 的支承。
        cells = frozenset({(0, 0), (1, 0), (2, 1)})
        stage = Stage({"A": Platform("A", cells)})
        seg = make_segment("walk", example_placements(), EXAMPLE_PLANTS, (0, 0), (2, 0))
        _supports, blocked = StagePlanner(stage).verify_segment(seg, (0, 0), (2, 0))
        self.assertIsInstance(blocked, BlockedBeat)
        self.assertEqual(blocked.reason, REASON_UNSUPPORTED)

    def test_17_moving_platform_carries_planted_foot_over_white_line(self) -> None:
        # 左脚区间0 踩实在世界 (1,0)。台 B 每拍整体多滚 +1：脚占名义格
        # 拍0=(1,0)，拍1 被搬到 (2,0)，禁线 x=2 两侧异号 -> 滑过白线，堵拍。
        cells = frozenset({(0, 0), (1, 0), (2, 0), (3, 0), (1, 1), (2, 1), (3, 1), (4, 1)})
        moving = Platform("B", cells, timetable={1: (1, 0), 2: (2, 0)})
        stage = Stage({"B": moving}, lines=(Line(2, 0, -3, name="white"),))
        seg = make_segment("walk", example_placements(), EXAMPLE_PLANTS, (0, 0), (2, 0))
        _supports, blocked = StagePlanner(stage).verify_segment(seg, (0, 0), (2, 0))
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked.reason, REASON_CARRIED_OVER_LINE)
        self.assertEqual(blocked.foot, L)

    def test_18_static_platform_never_slides(self) -> None:
        # 同一张图，固定台不滚动：踩实脚世界不动，无滑线。
        stage = Stage(
            {"A": Platform("A", world_cells({(0, 0), (1, 0), (2, 1), (3, 1)}))},
            lines=(Line(2, 0, -3, name="white"),),
        )
        seg = make_segment("walk", example_placements(), EXAMPLE_PLANTS, (0, 0), (2, 0))
        _supports, blocked = StagePlanner(stage).verify_segment(seg, (0, 0), (2, 0))
        self.assertIsNone(blocked)


# --------------------------------------------------------------------------- #
# 四、方式一：实垫板（含班次互斥）
# --------------------------------------------------------------------------- #

def moving_stage_with_slot() -> Stage:
    cells = frozenset({(0, 0), (1, 0), (2, 0), (3, 0), (1, 1), (2, 1), (3, 1), (4, 1)})
    moving = Platform("B", cells, timetable={1: (1, 0), 2: (2, 0)})
    return Stage(
        platforms={"B": moving},
        lines=(Line(2, 0, -3, name="white"),),
        shim_slots={"pad0": ShimSlot("pad0", "B", window=(0, 3))},
    )


class FixedShimTests(unittest.TestCase):
    def test_19_shim_freezes_platform_and_walk_completes(self) -> None:
        stage = moving_stage_with_slot()
        seg = make_segment("walk", example_placements(), EXAMPLE_PLANTS, (0, 0), (2, 0))
        plan = StagePlanner(stage).plan_with_shims(
            seg, (0, 0), (2, 0), actor_id="a1", shift="mon"
        )
        self.assertNotIsInstance(plan, BlockedBeat)
        self.assertEqual(plan.mode, "fixed_shim")
        self.assertEqual(plan.roots[-1], (2, 0))
        self.assertEqual(len(plan.freezes), 1)
        freeze = plan.freezes[0]
        self.assertEqual(
            (freeze.slot_name, freeze.platform_name, freeze.window), ("pad0", "B", (0, 2))
        )
        # 冻结 delta 锁在安装拍 (0,0)，安装期内台不按原表滚动。
        self.assertEqual(freeze.freeze_delta, (0, 0))
        # 准时下架：窗口半开 [0,2)，拍2 起台恢复时刻表。
        self.assertTrue(all(s.shim is None for s in plan.supports if s.beat >= 2))
        self.assertTrue(any(s.shim == "pad0" for s in plan.supports))

    def test_20_no_compliant_slot_means_blocked(self) -> None:
        cells = frozenset({(0, 0), (1, 0), (2, 0), (3, 0), (1, 1), (2, 1), (3, 1), (4, 1)})
        moving = Platform("B", cells, timetable={1: (1, 0), 2: (2, 0)})
        stage = Stage(
            {"B": moving},
            lines=(Line(2, 0, -3, name="white"),),
            shim_slots={"pad_other": ShimSlot("pad_other", "B", window=(4, 6))},
        )
        seg = make_segment("walk", example_placements(), EXAMPLE_PLANTS, (0, 0), (2, 0))
        out = StagePlanner(stage).plan_with_shims(
            seg, (0, 0), (2, 0), actor_id="a", shift="mon"
        )
        self.assertIsInstance(out, BlockedBeat)
        self.assertEqual(out.reason, REASON_UNSUPPORTED)

    def test_21_same_pad_two_actors_separate_shifts_both_granted(self) -> None:
        ledger = ShimLedger()
        req_a = ShimRequest("a1", "pad0", "mon", (0, 2))
        req_b = ShimRequest("a2", "pad0", "tue", (0, 2))
        self.assertIsNotNone(ledger.reserve(req_a))
        self.assertTrue(ledger.can_reserve(req_b))  # 不同班次可分别承诺
        self.assertIsNotNone(ledger.reserve(req_b))

    def test_22_same_pad_same_shift_overlap_cannot_promise_both(self) -> None:
        ledger = ShimLedger()
        req_a = ShimRequest("a1", "pad0", "mon", (0, 2))
        req_b = ShimRequest("a2", "pad0", "mon", (1, 3))  # 同窗重叠
        self.assertIsNotNone(ledger.reserve(req_a))
        self.assertFalse(ledger.can_reserve(req_b))
        self.assertIsNone(ledger.reserve(req_b))
        # 不重叠窗仍可同班次共用一块垫（一个时段只固定到一个基座）。
        req_c = ShimRequest("a2", "pad0", "mon", (2, 4))
        self.assertTrue(ledger.can_reserve(req_c))

    def test_23_planner_rejects_when_pad_already_exclusive(self) -> None:
        stage = moving_stage_with_slot()
        seg = make_segment("walk", example_placements(), EXAMPLE_PLANTS, (0, 0), (2, 0))

        ledger = ShimLedger()
        ledger.reserve(ShimRequest("other_actor", "pad0", "mon", (0, 2)))
        out = StagePlanner(stage, ledger).plan_with_shims(
            seg, (0, 0), (2, 0), actor_id="a1", shift="mon"
        )
        self.assertIsInstance(out, BlockedBeat)
        self.assertEqual(out.reason, REASON_SHIM_CONTENDED)

        # 换个班次即可承诺同一块垫。
        ledger2 = ShimLedger()
        ledger2.reserve(ShimRequest("other_actor", "pad0", "tue", (0, 2)))
        plan = StagePlanner(stage, ledger2).plan_with_shims(
            seg, (0, 0), (2, 0), actor_id="a1", shift="mon"
        )
        self.assertNotIsInstance(plan, BlockedBeat)


# --------------------------------------------------------------------------- #
# 五、方式二：换合格动作段（通知迟到、领拍锁定、脚相边界）
# --------------------------------------------------------------------------- #

def alt_continuation_images() -> dict:
    """从拍1 边界续接的两拍替段（首根=1、末根=2）：
    拍0(全局1) L 局部0、R 局部2；拍1(全局2) R 局部1。
    区间0 只有 R 踩实（L 为摆动脚），根点位移 = 2-1 = +1；
    入口脚相为 R 相，落点全在固定台 C。"""
    img = {0: canvas(), 1: canvas()}
    draw_l(img[0], 0)
    draw_r(img[0], 2)
    draw_l(img[1], 0)
    draw_r(img[1], 1)
    return img


def alt_continuation_placements() -> dict:
    return recognize_all(alt_continuation_images())


ALT_PLANTS = {0: (R,)}


class SwapSegmentTests(unittest.TestCase):
    def _moving_stage(self) -> Stage:
        # 活动台 B 区间0 静止（delta0=delta1=0），区间1 才滚动（delta2=+1）：
        # 把拍1->2 踩实的右脚世界点 (3,1) 对应名义格 (2,1) 搬到 (3,1)，
        # 禁线 x=3.5 两侧严格异号 -> 阻断在拍1、撑脚 R，拍1 即换步边界。
        b_cells = frozenset({(0, 0), (1, 0), (1, 1), (2, 1)})
        moving = Platform("B", b_cells, timetable={2: (1, 0)})
        # 固定安全台 C 承接替段世界落点：L (1,0)/(2,0)、R 恒 (3,1)。
        safe = Platform("C", frozenset({(1, 0), (2, 0), (3, 1)}))
        return Stage(
            platforms={"B": moving, "C": safe},
            lines=(Line(2, 0, -7, name="white"),),
        )

    def test_24_swap_avoids_moving_platform_at_legal_phase_boundary(self) -> None:
        stage = self._moving_stage()
        original = make_segment("orig", example_placements(), EXAMPLE_PLANTS, (0, 0), (2, 0))
        # 替段入口根点 = 原段拍1 根点 (1,0)，入口脚相 = R 相。
        alt = make_segment(
            "alt",
            alt_continuation_placements(),
            ALT_PLANTS,
            entry_root=(1, 0),
            exit_root=(2, 0),
        )
        planner = StagePlanner(stage)
        _sup, blocked = planner.verify_segment(original, (0, 0), (2, 0))
        self.assertEqual(blocked.reason, REASON_CARRIED_OVER_LINE)
        self.assertEqual(blocked.foot, R)
        plan = planner.plan_with_swap(
            {"orig": original, "alt": alt},
            "orig",
            (0, 0),
            (2, 0),
            locked_through=-1,
            notice_beat=0,
        )
        self.assertNotIsInstance(plan, BlockedBeat)
        self.assertEqual(plan.mode, "swap")
        self.assertEqual(plan.segment_chain, ("orig", "alt"))
        self.assertEqual(plan.roots, ((0, 0), (1, 0), (2, 0)))
        # 替段撑脚落在固定安全台 C；每条撑脚记录都有具名台支承。
        self.assertTrue(any(s.platform == "C" for s in plan.supports))
        self.assertTrue(all(s.platform is not None for s in plan.supports))

    def test_25_late_notice_after_foot_settled_forbids_reselecting_broadcast_frames(self) -> None:
        stage = self._moving_stage()
        original = make_segment("orig", example_placements(), EXAMPLE_PLANTS, (0, 0), (2, 0))
        alt = make_segment(
            "alt",
            alt_continuation_placements(),
            ALT_PLANTS,
            entry_root=(1, 0),
            exit_root=(2, 0),
        )
        planner = StagePlanner(stage)
        # 通知拍 1：唯一换步边界是全局拍1，边界必须严格晚于通知拍，
        # 已播帧不能再选步 -> 无可换边界，返回原阻断。
        out = planner.plan_with_swap(
            {"orig": original, "alt": alt},
            "orig",
            (0, 0),
            (2, 0),
            locked_through=0,
            notice_beat=1,
        )
        self.assertIsInstance(out, BlockedBeat)
        self.assertEqual(out.reason, REASON_CARRIED_OVER_LINE)

    def test_26_segment_only_swap_at_its_legal_phase_boundary(self) -> None:
        stage = self._moving_stage()
        original = make_segment("orig", example_placements(), EXAMPLE_PLANTS, (0, 0), (2, 0))
        alt = make_segment(
            "alt",
            alt_continuation_placements(),
            ALT_PLANTS,
            entry_root=(1, 0),
            exit_root=(2, 0),
        )
        # 替段入口脚相改成不兼容（边界是 R 相，错段只收 L 相）：换段被拒。
        wrong = StepSegment(
            segment_id="wrong",
            solution=alt.solution,
            plants=alt.plants,
            legal_entry_phase=FootPhase.of(L),
            legal_exit_phase=alt.legal_exit_phase,
        )
        planner = StagePlanner(stage)
        out = planner.plan_with_swap(
            {"orig": original, "wrong": wrong},
            "orig",
            (0, 0),
            (2, 0),
            locked_through=-1,
            notice_beat=0,
        )
        self.assertIsInstance(out, BlockedBeat)
        self.assertEqual(out.reason, REASON_PHASE_BOUNDARY)


# --------------------------------------------------------------------------- #
# 六、反胶住与规模上限
# --------------------------------------------------------------------------- #

class GlueGuardTests(unittest.TestCase):
    def test_27_support_frozen_at_previous_cell_is_rejected(self) -> None:
        # 手工构造“根点胶在原地、撑脚局部坐标却移动”的段：世界落点被挪动。
        sol = reconstruct(example_placements(), EXAMPLE_PLANTS, (0, 0)).solutions[0]
        glued_sol = StepSolution(
            roots=((0, 0), (0, 0), (0, 0)),  # 根点不动
            readings=sol.readings,
            root_deltas=((0, 0), (0, 0)),
            entry_phase=sol.entry_phase,
            exit_phase=sol.exit_phase,
            ambiguous_beats=(),
        )
        glued_seg = StepSegment(
            "glued", glued_sol, dict(EXAMPLE_PLANTS), sol.entry_phase, sol.exit_phase
        )
        _supports, blocked = StagePlanner(basic_stage()).verify_segment(glued_seg, (0, 0))
        self.assertIsInstance(blocked, BlockedBeat)
        self.assertEqual(blocked.reason, REASON_GLUED_SUPPORT)

    def test_28_scale_caps_eight_beats_six_platforms(self) -> None:
        with self.assertRaises(ValueError):
            Stage(
                {f"P{i}": Platform(f"P{i}", frozenset({(0, 0)})) for i in range(MAX_PLATFORMS + 1)}
            )
        long_roots = tuple((i, 0) for i in range(MAX_BEATS + 2))
        long_sol = StepSolution(long_roots, {}, (), FootPhase.of(), FootPhase.of(), ())
        long_seg = StepSegment("long", long_sol, {}, FootPhase.of(), FootPhase.of())
        _supports, blocked = StagePlanner(basic_stage()).verify_segment(long_seg, (0, 0))
        self.assertEqual(blocked.reason, REASON_LOCKED_BEAT)


if __name__ == "__main__":
    unittest.main()
