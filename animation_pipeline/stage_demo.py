"""像素剧场走位工具端到端演示。

跑 ``python -m animation_pipeline.stage_demo``：在内存合成图上做接触拍识别、
踩实根点重建，再分别演示
  1. 活动台把踩实脚带过禁线 -> 实垫板冻结后走完；
  2. 同垫两演员分班次可同获、合班竞争不可同时承诺；
  3. 不装板、在合法脚相边界换合格动作段，通知迟到则拒绝从已播帧选步。
全程整数坐标核验，无物理引擎，最多八拍六板。
"""

from __future__ import annotations

import numpy as np

from .footwork import (
    SoleTemplate,
    reconstruct,
    recognize_beat,
)
from .staging import (
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
W, H = 16, 2
BOX = (0, 0, W, H)


def _sole(foot_id: str, rgb) -> SoleTemplate:
    row = 0 if foot_id == L else 1
    return SoleTemplate(
        foot_id=foot_id,
        sample_rgb=rgb,
        follow_point=(0, row),
        toe_point=(1, row),
        cells=((0, row, rgb), (1, row, rgb)),
        width=2,
        height=2,
    )


def _canvas() -> np.ndarray:
    arr = np.zeros((H, W, 4), dtype=np.uint8)
    arr[:, :, :3] = BG
    arr[:, :, 3] = 255
    return arr


def _draw(arr: np.ndarray, x: int, rgb, row: int) -> None:
    arr[row, x] = (rgb[0], rgb[1], rgb[2], 255)
    arr[row, x + 1] = (rgb[0], rgb[1], rgb[2], 255)


def _example_placements() -> dict:
    # 规范示例：拍0 L=1 R=2；拍1 L=0 R=2；拍2 R=1。
    img = {0: _canvas(), 1: _canvas(), 2: _canvas()}
    _draw(img[0], 1, LEFT_RGB, 0)
    _draw(img[0], 2, RIGHT_RGB, 1)
    _draw(img[1], 0, LEFT_RGB, 0)
    _draw(img[1], 2, RIGHT_RGB, 1)
    _draw(img[2], 1, RIGHT_RGB, 1)
    tpl = {L: _sole(L, LEFT_RGB), R: _sole(R, RIGHT_RGB)}
    boxes = {L: BOX, R: BOX}
    return {b: recognize_beat(im, tpl, boxes, b) for b, im in img.items()}


def _alt_placements() -> dict:
    # 续接两拍：入口根 (1,0)，R 局部 2->1 给出 delta +1，末根到 2。
    img = {0: _canvas(), 1: _canvas()}
    _draw(img[0], 0, LEFT_RGB, 0)
    _draw(img[0], 2, RIGHT_RGB, 1)
    _draw(img[1], 0, LEFT_RGB, 0)
    _draw(img[1], 1, RIGHT_RGB, 1)
    tpl = {L: _sole(L, LEFT_RGB), R: _sole(R, RIGHT_RGB)}
    boxes = {L: BOX, R: BOX}
    return {b: recognize_beat(im, tpl, boxes, b) for b, im in img.items()}


def _segment(seg_id: str, placements: dict, plants: dict, entry: tuple, exit_: tuple) -> StepSegment:
    result = reconstruct(placements, plants, entry, exit_root=exit_)
    assert result.ok, result.blocked
    sol = result.solutions[0]
    return StepSegment(seg_id, sol, dict(plants), sol.entry_phase, sol.exit_phase)


def _shim_stage() -> Stage:
    # 活动台 B 独占全部落点：区间1 滚动把右脚带过禁线，只能靠垫位冻结。
    moving = Platform(
        "B",
        frozenset({(0, 0), (1, 0), (1, 1), (2, 1), (3, 1)}),
        timetable={2: (1, 0)},
    )
    return Stage(
        platforms={"B": moving},
        lines=(Line(2, 0, -7, name="white"),),
        shim_slots={"pad0": ShimSlot("pad0", "B", window=(0, 3))},
    )


def _swap_stage() -> Stage:
    # B/C 分工：原段区间1 被 B 滚动堵住；替段落点全在固定台 C。
    moving = Platform(
        "B",
        frozenset({(0, 0), (1, 0), (1, 1), (2, 1)}),
        timetable={2: (1, 0)},
    )
    safe = Platform("C", frozenset({(1, 0), (2, 0), (3, 1)}))
    return Stage(
        platforms={"B": moving, "C": safe},
        lines=(Line(2, 0, -7, name="white"),),
        shim_slots={"pad0": ShimSlot("pad0", "B", window=(0, 3))},
    )


def main() -> None:
    placements = _example_placements()
    plants = {0: (L,), 1: (R,)}
    walk = _segment("walk", placements, plants, (0, 0), (2, 0))
    print("== 重建根点轨迹 ==")
    print("roots:", walk.solution.roots, "deltas:", walk.solution.root_deltas)

    print("\n== 无修复核验（换段舞台）==")
    planner = StagePlanner(_swap_stage())
    _sup, blocked = planner.verify_segment(walk, (0, 0), (2, 0))
    print("blocked:", blocked)

    print("\n== 方式一：实垫板（装垫舞台）==")
    plan_shim = StagePlanner(_shim_stage()).plan_with_shims(
        walk, (0, 0), (2, 0), actor_id="a1", shift="mon"
    )
    print("mode:", plan_shim.mode, "freezes:", plan_shim.freezes)

    print("\n== 实垫台账：两演员 ==")
    ledger = ShimLedger()
    ledger.reserve(ShimRequest("a1", "pad0", "mon", (1, 3)))
    print("a2 同班重叠可承诺? ", ledger.can_reserve(ShimRequest("a2", "pad0", "mon", (1, 3))))
    print("a2 异班次可承诺? ", ledger.can_reserve(ShimRequest("a2", "pad0", "tue", (1, 3))))

    print("\n== 方式二：换动作段（换段舞台）==")
    alt = _segment("alt", _alt_placements(), {0: (R,)}, (1, 0), (2, 0))
    plan_swap = StagePlanner(_swap_stage()).plan_with_swap(
        {"walk": walk, "alt": alt},
        "walk",
        (0, 0),
        (2, 0),
        locked_through=-1,
        notice_beat=0,
    )
    print("mode:", plan_swap.mode, "chain:", plan_swap.segment_chain, "roots:", plan_swap.roots)
    late = StagePlanner(_swap_stage()).plan_with_swap(
        {"walk": walk, "alt": alt},
        "walk",
        (0, 0),
        (2, 0),
        locked_through=0,
        notice_beat=1,
    )
    print("通知迟到(拍1)结果:", late)
    print("\n入口脚相:", walk.legal_entry_phase, " 出口脚相:", walk.legal_exit_phase)


if __name__ == "__main__":
    main()
