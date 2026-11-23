"""像素剧场走位工具（上半）：接触拍识别与一步动作重建。

口径（合同，与 compositor 的合成合同配套）：

* 画师在播放时间轴上点出若干**接触拍**，每只用脚给三样标注：
  样色（鞋底像素必须相等的 RGB）、跟点（纸型上的锚点）、一小块鞋底纸型
  （稀疏格子表，透明格不参与匹配）。
* 识别只吃**当拍完整合成图**（FramePlayer.display_at 的 RGBA 画布），
  不读原始帧、不读图像库默认行为。纸型每个不透明格要求落点 alpha>0
  且 RGB 与样色逐通道相等；透明格不约束，因此“透明补画”天然保真；
  前一帧处置后留下的轮廓只要留在合成图上就照常可贴；同一脚两种互相
  重叠的读法全部保留，并用 ambiguous 标记。
* 画师另签每只脚在相邻两拍之间是否**踩实**。踩实脚世界位置不动：
  root[b+1] - root[b] == local[b] - local[b+1]；同一区间两脚同时踩实
  必须给出同一根点位移，否则该区间无解并记 BlockedBeat。
* 同一只脚在共享拍上的识别是一次选择：接触拍 b 的读法同时约束
  区间 (b-1,b) 与 (b,b+1)，CSP 不允许两边各选各的。

坐标一律为整数二维元组；示例里的一维走位取 y=0。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

Vec = tuple[int, int]
RGB = tuple[int, int, int]

# 阻断原因（逐拍核看用，字符串为稳定枚举值）。
REASON_NO_READING = "no_reading"            # 早帧处置变动后接触脚消失
REASON_CONFLICT_DELTA = "conflict_delta"    # 双脚同踩实却给出不同位移
REASON_EXIT_MISMATCH = "exit_mismatch"      # 末根对不上台口
REASON_UNSUPPORTED = "unsupported"          # 落点不在任何具名台面
REASON_CARRIED_OVER_LINE = "carried_over_line"  # 活动板把踩实脚带过禁线
REASON_GLUED_SUPPORT = "glued_support"      # 把上一格支脚位置胶成新格
REASON_LOCKED_BEAT = "locked_beat"          # 已播/已签领拍不可重来
REASON_SHIM_CONTENDED = "shim_contended"    # 同班次实垫已被独占
REASON_PHASE_BOUNDARY = "phase_boundary"    # 脚相边界不合法


# --------------------------------------------------------------------------- #
# 鞋底纸型与接触拍识别
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SoleTemplate:
    """一小块鞋底纸型。

    cells 为稀疏不透明格 (dx, dy, rgb)；纸型包围盒宽高用于限制原点范围，
    盒内其余位置视为透明（不参与匹配）。
    """

    foot_id: str
    sample_rgb: RGB
    follow_point: Vec          # 跟点在纸型局部坐标中的位置
    toe_point: Vec             # 脚尖在纸型局部坐标中的位置
    cells: tuple[tuple[int, int, RGB], ...]
    width: int
    height: int

    def __post_init__(self) -> None:
        norm_cells = tuple((int(dx), int(dy), tuple(int(c) for c in rgb)) for dx, dy, rgb in self.cells)
        object.__setattr__(self, "cells", norm_cells)
        object.__setattr__(self, "sample_rgb", tuple(int(c) for c in self.sample_rgb))
        for dx, dy, rgb in norm_cells:
            if not (0 <= dx < self.width and 0 <= dy < self.height):
                raise ValueError("纸型格越界")
            if tuple(rgb) != tuple(self.sample_rgb):
                raise ValueError("纸型不透明格必须使用样色")
        if not (0 <= self.follow_point[0] < self.width and 0 <= self.follow_point[1] < self.height):
            raise ValueError("跟点越界")
        if not (0 <= self.toe_point[0] < self.width and 0 <= self.toe_point[1] < self.height):
            raise ValueError("脚尖越界")
        if not norm_cells:
            raise ValueError("纸型至少要有一个不透明格")


@dataclass(frozen=True)
class FootPlacement:
    """一只脚在某一拍的一种整数坐标读法。

    origin/heel/toe 均为**帧局部整数坐标**：该帧坐标系原点即角色根点，
    世界坐标由 StepSolution 的当拍根点加上局部坐标得到。
    """

    foot_id: str
    beat: int
    origin: Vec                       # 纸型原点在帧坐标系中的整数坐标
    heel: Vec                         # 跟点局部坐标
    toe: Vec                          # 脚尖局部坐标
    ambiguous: bool = False           # 与同脚另一种读法是否有像素重叠
    provenance_counts: dict[str, int] = field(default_factory=dict)

    def world(self, root: Vec) -> Vec:
        """跟点世界坐标 = 当拍根点 + 局部跟点。"""
        return self.heel[0] + root[0], self.heel[1] + root[1]

    def local(self, root: Vec) -> Vec:
        """相对角色根点的局部跟点坐标（读法本身即局部坐标，root 仅作口径校验）。"""
        return self.heel


def _cell_set(template: SoleTemplate, origin: Vec) -> frozenset[Vec]:
    ox, oy = origin
    return frozenset((ox + dx, oy + dy) for dx, dy, _ in template.cells)


def recognize_foot(
    canvas: np.ndarray,
    template: SoleTemplate,
    beat: int,
    search_box: tuple[int, int, int, int],
    provenance: Optional[list[list[str]]] = None,
) -> list[FootPlacement]:
    """在当拍完整合成图的搜索区内枚举所有可贴合读法。

    搜索区为半开整数盒 [x0,y0,x1,y1)，纸型整体必须落在盒内。
    匹配条件仅针对纸型不透明格：画布对应像素 alpha>0 且 RGB 全等样色。
    透明格、处置残留轮廓、重叠读法均不做剔除。
    """
    if canvas.ndim != 3 or canvas.shape[2] != 4:
        raise ValueError("识别输入必须是完整 RGBA 合成图")
    h, w = canvas.shape[:2]
    x0, y0, x1, y1 = search_box
    if not (0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h):
        raise ValueError("搜索区越界或为空")
    if x1 - x0 < template.width or y1 - y0 < template.height:
        raise ValueError("搜索区装不下纸型，不可能有贴合原点")

    placements: list[FootPlacement] = []
    occupied: list[frozenset[Vec]] = []
    for oy in range(y0, y1 - template.height + 1):
        for ox in range(x0, x1 - template.width + 1):
            ok = True
            prov_counts: dict[str, int] = {}
            for dx, dy, rgb in template.cells:
                px = canvas[oy + dy, ox + dx]
                px_rgb = (int(px[0]), int(px[1]), int(px[2]))
                if px[3] == 0 or px_rgb != tuple(rgb):
                    ok = False
                    break
                if provenance is not None:
                    label = provenance[oy + dy][ox + dx]
                    prov_counts[label] = prov_counts.get(label, 0) + 1
            if not ok:
                continue
            origin = (ox, oy)
            cells = _cell_set(template, origin)
            ambiguous = any(cells & prior for prior in occupied)
            if ambiguous:
                # 重叠是对称关系：新读法标歧义，旧读法也要翻牌，保真两种读法。
                for idx, prior in enumerate(occupied):
                    if cells & prior:
                        placements[idx] = _mark_ambiguous(placements[idx])
            occupied.append(cells)
            placements.append(
                FootPlacement(
                    foot_id=template.foot_id,
                    beat=beat,
                    origin=origin,
                    heel=(ox + template.follow_point[0], oy + template.follow_point[1]),
                    toe=(ox + template.toe_point[0], oy + template.toe_point[1]),
                    ambiguous=ambiguous,
                    provenance_counts=dict(prov_counts),
                )
            )
    return placements


def _mark_ambiguous(p: FootPlacement) -> FootPlacement:
    if p.ambiguous:
        return p
    return FootPlacement(
        foot_id=p.foot_id,
        beat=p.beat,
        origin=p.origin,
        heel=p.heel,
        toe=p.toe,
        ambiguous=True,
        provenance_counts=dict(p.provenance_counts),
    )


def recognize_beat(
    canvas: np.ndarray,
    templates: dict[str, SoleTemplate],
    search_boxes: dict[str, tuple[int, int, int, int]],
    beat: int,
    provenance: Optional[list[list[str]]] = None,
) -> dict[str, list[FootPlacement]]:
    """同拍识别左右脚；未给纸型/搜索区的脚返回空读法表。"""
    out: dict[str, list[FootPlacement]] = {}
    for foot_id, template in templates.items():
        box = search_boxes.get(foot_id)
        out[foot_id] = (
            []
            if box is None
            else recognize_foot(canvas, template, beat, box, provenance)
        )
    return out


# --------------------------------------------------------------------------- #
# 踩实签署与一步动作重建
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class BlockedBeat:
    beat: int                     # 阻断发生的区间起点拍（或末拍）
    reason: str
    foot: Optional[str] = None
    detail: str = ""


@dataclass(frozen=True)
class FootPhase:
    """脚相：当前处于踩实状态的脚集合；首末脚相用于接步。"""

    planted: frozenset[str]

    @classmethod
    def of(cls, *feet: str) -> "FootPhase":
        return cls(frozenset(feet))


@dataclass(frozen=True)
class StepSolution:
    """一份可逐拍核看的一步动作重建。"""

    roots: tuple[Vec, ...]                       # 每拍根点世界整数坐标
    readings: dict[tuple[int, str], FootPlacement]  # (拍, 脚) -> 选定读法
    root_deltas: tuple[Vec, ...]                 # 每区间根点位移 root[b+1]-root[b]
    entry_phase: FootPhase
    exit_phase: FootPhase
    ambiguous_beats: tuple[int, ...]             # 选中歧义读法的拍

    def landing(self, beat: int, foot: str) -> Vec:
        """撑脚在某拍的局部落点（跟点帧局部坐标）。"""
        return self.readings[(beat, foot)].heel

    def world_landing(self, beat: int, foot: str) -> Vec:
        """撑脚在某拍的台面落点（跟点世界坐标）。"""
        return self.readings[(beat, foot)].world(self.roots[beat])


@dataclass(frozen=True)
class ReconstructionResult:
    solutions: tuple[StepSolution, ...]
    blocked: tuple[BlockedBeat, ...]

    @property
    def ok(self) -> bool:
        return bool(self.solutions) and not self.blocked


def _sub(a: Vec, b: Vec) -> Vec:
    return a[0] - b[0], a[1] - b[1]


def reconstruct(
    placements_by_beat: dict[int, dict[str, list[FootPlacement]]],
    plants: dict[int, tuple[str, ...]],
    entry_root: Vec,
    *,
    exit_root: Optional[Vec] = None,
    max_solutions: int = 64,
) -> ReconstructionResult:
    """从识别读法与踩实签署重建根点轨迹。

    placements_by_beat: {拍: {脚: [读法...]}}，共享拍的读法只选一次。
    plants: {区间起点拍: (踩实脚...)}，区间覆盖相邻两拍。
    entry_root: 入场根点（固定）。exit_root 给定时核对末拍台口对位。
    """
    beats = sorted(placements_by_beat)
    if not beats or beats[0] != 0 or beats != list(range(len(beats))):
        raise ValueError("接触拍必须从 0 起连续编号")
    n = len(beats)
    for b in plants:
        if not 0 <= b < n - 1:
            raise ValueError(f"踩实签署拍到不存在的区间 {b}")

    blocked: list[BlockedBeat] = []
    # 每拍每脚的选项：识别到的读法；没识别到则唯一选项 None。
    options: dict[tuple[int, str], list[Optional[FootPlacement]]] = {}
    feet = sorted({f for table in placements_by_beat.values() for f in table})
    for b in beats:
        for f in feet:
            reads = placements_by_beat[b].get(f, [])
            options[(b, f)] = list(reads) if reads else [None]

    # 先把硬缺失记为阻断（踩实区间任一端无读法），CSP 仍继续枚举其余可能。
    for b, planted_feet in plants.items():
        for f in planted_feet:
            for bb in (b, b + 1):
                if all(r is None for r in options[(bb, f)]):
                    blocked.append(
                        BlockedBeat(bb, REASON_NO_READING, f, f"拍{bb}找不到脚{f}的接触读法")
                    )

    choices: list[tuple[int, str, list[Optional[FootPlacement]]]] = [
        (b, f, options[(b, f)]) for b in beats for f in feet
    ]
    solutions: list[StepSolution] = []
    picked: dict[tuple[int, str], Optional[FootPlacement]] = {}

    def interval_delta(b: int):
        """由区间 b 的踩实签署求根点位移；两脚不一致或缺读法返回 None。

        踩实脚世界位置不动：root[b]+local[b] == root[b+1]+local[b+1]，
        故根点位移 = local[b] - local[b+1]（局部前值减后值）。
        同区间两脚同时踩实必须给出完全相同的位移，否则返回冲突哨兵。
        """
        delta = None
        for f in plants.get(b, ()):
            pb, pn = picked.get((b, f)), picked.get((b + 1, f))
            if pb is None or pn is None:
                return None
            d = _sub(pb.heel, pn.heel)
            if delta is not None and d != delta:
                return BlockedBeat  # 类型哨兵：冲突
            delta = d
        return delta

    def dfs(idx: int) -> None:
        if len(solutions) >= max_solutions:
            return
        if idx == len(choices):
            roots: list[Vec] = [entry_root]
            deltas: list[Vec] = []
            for b in range(n - 1):
                d = interval_delta(b)
                if d is BlockedBeat:
                    blocked.append(
                        BlockedBeat(
                            b,
                            REASON_CONFLICT_DELTA,
                            detail=f"拍{b}-{b+1}两脚同时踩实但位移不一致",
                        )
                    )
                    return
                if d is None:
                    # 无踩实脚的区间（腾空/过渡）：根点不由踩实律决定，
                    # 这种区间不允许出现在一步动作合同里——必须有签署。
                    blocked.append(
                        BlockedBeat(b, REASON_LOCKED_BEAT, detail=f"区间{b}没有踩实签署，无法定根点")
                    )
                    return
                deltas.append(d)  # type: ignore[arg-type]
                roots.append((roots[-1][0] + d[0], roots[-1][1] + d[1]))  # type: ignore[index]
            if exit_root is not None and roots[-1] != exit_root:
                blocked.append(
                    BlockedBeat(
                        n - 1,
                        REASON_EXIT_MISMATCH,
                        detail=f"末根{roots[-1]}对不上台口{exit_root}",
                    )
                )
                return
            readings = {k: v for k, v in picked.items() if v is not None}
            ambiguous = sorted({b for (b, _f), v in readings.items() if v.ambiguous})
            entry_phase = FootPhase.of(*plants.get(0, ()))
            exit_phase = FootPhase.of(*plants.get(n - 2, ()))
            solutions.append(
                StepSolution(
                    roots=tuple(roots),
                    readings=readings,
                    root_deltas=tuple(deltas),
                    entry_phase=entry_phase,
                    exit_phase=exit_phase,
                    ambiguous_beats=tuple(ambiguous),
                )
            )
            return

        b, f, vals = choices[idx]
        for placement in vals:
            picked[(b, f)] = placement
            dfs(idx + 1)
            if len(solutions) >= max_solutions:
                return

    dfs(0)
    # 去重阻断记录，保持拍序稳定，便于逐拍核看。
    seen: set[tuple] = set()
    uniq_blocked: list[BlockedBeat] = []
    for blk in sorted(blocked, key=lambda z: (z.beat, z.reason, z.foot or "")):
        key = (blk.beat, blk.reason, blk.foot, blk.detail)
        if key not in seen:
            seen.add(key)
            uniq_blocked.append(blk)
    return ReconstructionResult(tuple(solutions), tuple(uniq_blocked))
