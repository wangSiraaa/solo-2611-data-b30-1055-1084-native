"""像素剧场走位工具（下半）：分层站台、禁线与保对位规划。

舞台口径：

* 分层站台图由若干**具名台面**组成，每块台给名义格子表与按拍时刻表
  （每拍整体位移 delta，缺省 (0,0) 即固定台）。可放脚的只有具名台面。
* 禁线为整数系数直线 ``ax*x+ay*y+c=0``；踩实期间活动台把脚下格子带得
  越过禁线（相邻拍带符号值异号）即堵拍——踩实脚世界位置不动，绝不
  允许“滑过白线”。
* 入场根点与末拍对台口的对位固定；接步必须延续根位置与交替脚相。
* 两种保对位方式二选一，规划器按 mode 分别产出真实走位：

  ``fixed_shim``：买少量实垫板。一块板在一个时段只能固定到一个具名
  基座（ShimSlot 给出合规垫位与窗口），安装期内所在台不按原表滚动
  （整体冻在安装拍位置），仍受禁线约束，窗口结束准时下架。申请经
  ShimLedger 记账：同垫可在不同班次分别批给不同演员，同一班次互斥。

  ``swap``：不用固定板，换从原画脚相推出的合格动作段。换步拍必须
  晚于当前撑脚落地拍，已签领拍/已播帧（locked_through）不能重来，
  步段只在自己合法的脚相边界可替。

* 反胶住守卫：不允许把支脚在上一格的世界位置当作新格胶住
  （根点不动而撑脚世界位置变了的段直接拒）。

规模上限：最多八拍、六块台板，超出在构造期拒绝；无物理引擎，
全部结论由整数坐标的离散核验给出。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .footwork import (
    BlockedBeat,
    FootPhase,
    REASON_CARRIED_OVER_LINE,
    REASON_EXIT_MISMATCH,
    REASON_GLUED_SUPPORT,
    REASON_LOCKED_BEAT,
    REASON_PHASE_BOUNDARY,
    REASON_SHIM_CONTENDED,
    REASON_UNSUPPORTED,
    StepSolution,
    Vec,
)

MAX_BEATS = 8
MAX_PLATFORMS = 6

MODE_FIXED_SHIM = "fixed_shim"
MODE_SWAP = "swap"


# --------------------------------------------------------------------------- #
# 站台、禁线、实垫位
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Line:
    """禁线 ax*x + ay*y + c = 0；side 为带符号值，异号即穿线。"""

    a_x: int
    a_y: int
    c: int
    name: str = "line"

    def side(self, p: Vec) -> int:
        return self.a_x * p[0] + self.a_y * p[1] + self.c

    def crosses(self, p0: Vec, p1: Vec) -> bool:
        s0, s1 = self.side(p0), self.side(p1)
        return s0 * s1 < 0


@dataclass(frozen=True)
class Platform:
    """具名台面：拍 0 名义格子 + 每拍整体位移时刻表。"""

    name: str
    cells: frozenset[Vec]
    timetable: dict[int, Vec] = field(default_factory=dict)

    def delta_at(self, beat: int) -> Vec:
        return self.timetable.get(beat, (0, 0))

    def world_cells(self, beat: int, frozen_delta: Optional[Vec] = None) -> frozenset[Vec]:
        dx, dy = self.delta_at(beat) if frozen_delta is None else frozen_delta
        return frozenset((x + dx, y + dy) for x, y in self.cells)

    @property
    def moving(self) -> bool:
        return any(d != (0, 0) for d in self.timetable.values())


@dataclass(frozen=True)
class ShimSlot:
    """合规固定垫位：绑死一个具名基座，给可安装的半开时间窗。"""

    name: str
    platform_name: str
    window: tuple[int, int]        # [start, end)，结束即准时下架


@dataclass(frozen=True)
class Stage:
    platforms: dict[str, Platform]
    lines: tuple[Line, ...] = ()
    shim_slots: Optional[dict[str, ShimSlot]] = None

    def __post_init__(self) -> None:
        if len(self.platforms) > MAX_PLATFORMS:
            raise ValueError(f"台板数超过上限 {MAX_PLATFORMS}")
        for slot in (self.shim_slots or {}).values():
            if slot.platform_name not in self.platforms:
                raise ValueError(f"垫位 {slot.name} 绑定了不存在的台 {slot.platform_name}")

    def support(
        self, point: Vec, beat: int, freezes: tuple["Freeze", ...] = ()
    ) -> Optional[str]:
        """返回该拍托住 point 的具名台名；无支承返回 None。"""
        for name, platform in self.platforms.items():
            frozen = next((f.freeze_delta for f in freezes if f.platform_name == name), None)
            if point in platform.world_cells(beat, frozen):
                return name
        return None


@dataclass(frozen=True)
class Freeze:
    """实垫安装期内对一块台的冻结记录。"""

    slot_name: str
    platform_name: str
    window: tuple[int, int]
    freeze_delta: Vec


# --------------------------------------------------------------------------- #
# 实垫台账：班次互斥
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ShimRequest:
    actor_id: str
    slot_name: str
    shift: str
    window: tuple[int, int]


@dataclass(frozen=True)
class ShimReservation:
    request: ShimRequest
    freeze_delta: Vec


class ShimLedger:
    """一块实垫在一个时段只能固定到一个具名基座。

    同垫不同班次可分别承诺；同一班次内不同演员的时间窗不得重叠，
    重叠即独占竞争（can_reserve 返回 False）。
    """

    def __init__(self) -> None:
        self._rows: list[ShimRequest] = []

    @staticmethod
    def _overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
        return a[0] < b[1] and b[0] < a[1]

    def can_reserve(self, req: ShimRequest) -> bool:
        for row in self._rows:
            if row.slot_name != req.slot_name or row.shift != req.shift:
                continue  # 异班次不互斥
            if row.actor_id == req.actor_id:
                continue
            if self._overlap(row.window, req.window):
                return False
        return True

    def reserve(self, req: ShimRequest, freeze_delta: Vec = (0, 0)) -> Optional[ShimReservation]:
        if not self.can_reserve(req):
            return None
        self._rows.append(req)
        return ShimReservation(req, freeze_delta)


# --------------------------------------------------------------------------- #
# 步段与规划结果
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class StepSegment:
    """从原画脚相推出的可替步段；包一份重建解与合法脚相边界。"""

    segment_id: str
    solution: StepSolution
    plants: dict[int, tuple[str, ...]]     # 区间起点拍 -> 踩实脚
    legal_entry_phase: FootPhase
    legal_exit_phase: FootPhase

    @property
    def length(self) -> int:
        return len(self.solution.roots)


@dataclass(frozen=True)
class FootSupport:
    beat: int
    foot: str
    point: Vec
    platform: Optional[str]
    shim: Optional[str] = None


@dataclass(frozen=True)
class StagePlan:
    mode: str
    segment_chain: tuple[str, ...]
    roots: tuple[Vec, ...]
    supports: tuple[FootSupport, ...]
    freezes: tuple[Freeze, ...]
    entry_phase: FootPhase
    exit_phase: FootPhase


def validate_no_glue(segment: StepSegment) -> Optional[BlockedBeat]:
    """反胶住：踩实区间内撑脚世界位置必须不动。

    根点位移必须等于局部坐标前值减后值；若根点被胶在原地而撑脚世界
    位置变了（用上一格位置冒充新格支承），直接判 blocked。
    """
    sol = segment.solution
    for b, feet in segment.plants.items():
        for f in feet:
            pb = sol.readings.get((b, f))
            pn = sol.readings.get((b + 1, f))
            if pb is None or pn is None:
                continue
    sol = segment.solution
    for b, feet in segment.plants.items():
        for f in feet:
            pb = sol.readings.get((b, f))
            pn = sol.readings.get((b + 1, f))
            if pb is None or pn is None:
                continue
            root_delta = (
                sol.roots[b + 1][0] - sol.roots[b][0],
                sol.roots[b + 1][1] - sol.roots[b][1],
            )
            # 踩实脚世界不动 => root 位移必须等于局部前值减后值。
            local_delta = (
                pb.heel[0] - pn.heel[0],
                pb.heel[1] - pn.heel[1],
            )
            if root_delta != local_delta:
                return BlockedBeat(b, REASON_GLUED_SUPPORT, f, "根点位移不等于踩实脚局部差")
            # 根点被胶在原地、撑脚局部坐标却变了 => 世界落点被挪动，
            # 即把支脚在上一格的位置当作新格胶住，直接拒。
            if root_delta == (0, 0) and pb.heel != pn.heel:
                return BlockedBeat(b, REASON_GLUED_SUPPORT, f, "根点胶住但撑脚世界位置移动")
    return None


def _planted_feet(segment: StepSegment, beat: int) -> tuple[str, ...]:
    """跨过 beat->beat+1 区间的踩实脚。"""
    return segment.plants.get(beat, ())


class StagePlanner:
    """给定舞台与获准步段，产出可放映走位或指出被堵住的拍。"""

    def __init__(self, stage: Stage, ledger: Optional[ShimLedger] = None) -> None:
        if len(stage.platforms) > MAX_PLATFORMS:
            raise ValueError("台板超限")
        self.stage = stage
        self.ledger = ledger or ShimLedger()

    # -- 单段核验（无修复） ---------------------------------------------- #
    def verify_segment(
        self,
        segment: StepSegment,
        entry_root: Vec,
        exit_target: Optional[Vec] = None,
        freezes: tuple[Freeze, ...] = (),
    ) -> tuple[Optional[list[FootSupport]], Optional[BlockedBeat]]:
        """逐拍核验支承与禁线；返回 (撑脚落点表, 阻断)。"""
        if segment.solution.roots[0] != entry_root:
            return None, BlockedBeat(0, REASON_EXIT_MISMATCH, detail="入场根点不延续")
        glued = validate_no_glue(segment)
        if glued is not None:
            return None, glued
        sol = segment.solution
        if len(sol.roots) > MAX_BEATS:
            return None, BlockedBeat(0, REASON_LOCKED_BEAT, detail=f"超过 {MAX_BEATS} 拍上限")

        supports: list[FootSupport] = []
        for b in range(segment.length):
            placements = [
                (f, p) for (bb, f), p in sol.readings.items() if bb == b
            ]
            for f, placement in sorted(placements, key=lambda z: z[0]):
                point = placement.world(sol.roots[b])
                platform = self.stage.support(point, b, freezes)
                if platform is None:
                    return None, BlockedBeat(
                        b, REASON_UNSUPPORTED, f, f"落点{point}不在任何具名台面"
                    )
                shim = next(
                    (
                        fz.slot_name
                        for fz in freezes
                        if fz.platform_name == platform and b in range(*fz.window)
                    ),
                    None,
                )
                supports.append(FootSupport(b, f, point, platform, shim))

        # 踩实区间：活动台不得把踩实脚带过禁线，也不得把台从脚下挪走。
        for b in range(segment.length - 1):
            for f in _planted_feet(segment, b):
                pb = sol.readings.get((b, f))
                pn = sol.readings.get((b + 1, f))
                if pb is None or pn is None:
                    continue  # 无读法由重建层负责
                p_b = pb.world(sol.roots[b])
                p_n = pn.world(sol.roots[b + 1])
                if p_b != p_n:
                    # 真踩实要求世界落点不动；动了说明读法/胶住有问题。
                    return None, BlockedBeat(
                        b, REASON_GLUED_SUPPORT, f, "踩实脚世界落点发生移动"
                    )
                p = p_b
                name_b = self.stage.support(p, b, freezes)
                name_n = self.stage.support(p, b + 1, freezes)
                if name_b is None or name_n is None:
                    return None, BlockedBeat(b + 1, REASON_UNSUPPORTED, f, "活动台从脚下挪开")
                if name_b != name_n:
                    return None, BlockedBeat(
                        b,
                        REASON_CARRIED_OVER_LINE,
                        f,
                        f"活动台滚动使踩实脚失去同台支承：{name_b}->{name_n}",
                    )
                platform = self.stage.platforms[name_b]
                frozen_b = next((fz for fz in freezes if fz.platform_name == name_b and b in range(*fz.window)), None)
                frozen_n = next((fz for fz in freezes if fz.platform_name == name_b and b + 1 in range(*fz.window)), None)
                if frozen_b is not None and frozen_n is not None:
                    continue  # 实垫冻结期，台不滚动
                d0 = platform.delta_at(b)
                d1 = platform.delta_at(b + 1)
                # 脚在拍 b 占据的名义格 = 世界点 - 当时台位移；台滚动后
                # 该名义格被搬到 cell_b + d1，新旧位置分居禁线两侧即带脚越线。
                cell_b = (p[0] - d0[0], p[1] - d0[1])
                cell_next = (cell_b[0] + d1[0], cell_b[1] + d1[1])
                for line in self.stage.lines:
                    if line.crosses(cell_b, cell_next):
                        return None, BlockedBeat(
                            b,
                            REASON_CARRIED_OVER_LINE,
                            f,
                            f"台{name_b}把脚带过禁线{line.name}",
                        )

        if exit_target is not None and sol.roots[-1] != exit_target:
            return None, BlockedBeat(
                segment.length - 1,
                REASON_EXIT_MISMATCH,
                detail=f"末根{sol.roots[-1]}接不到台口{exit_target}",
            )
        return supports, None

    # -- 方式一：实垫板 -------------------------------------------------- #
    def plan_with_shims(
        self,
        segment: StepSegment,
        entry_root: Vec,
        exit_target: Optional[Vec],
        *,
        actor_id: str,
        shift: str,
        shim_budget: int = 2,
    ) -> StagePlan | BlockedBeat:
        """先试无垫；被活动台堵住时，按阻断拍给该台申请一块实垫冻结。

        每轮核验 -> 若阻断来自活动台滚动（禁线/挪空），就在覆盖该踩实
        区间的合规垫位装一块板，冻结 delta 锁在安装拍，然后重新核验；
        窗口结束准时下架。无合规垫位或台账互斥则返回阻断。
        """
        freezes: list[Freeze] = []
        blocked: Optional[BlockedBeat] = None
        for _ in range(shim_budget + 1):
            supports, blocked = self.verify_segment(
                segment, entry_root, exit_target, tuple(freezes)
            )
            if blocked is None:
                return StagePlan(
                    MODE_FIXED_SHIM,
                    (segment.segment_id,),
                    segment.solution.roots,
                    tuple(supports),  # type: ignore[arg-type]
                    tuple(freezes),
                    segment.legal_entry_phase,
                    segment.legal_exit_phase,
                )
            fix = self._freeze_for_block(segment, blocked, freezes, actor_id, shift)
            if isinstance(fix, BlockedBeat):
                return fix
            if fix is None:
                return blocked
            freezes.append(fix)
        return blocked  # 预算用尽仍未打通

    def _freeze_for_block(
        self,
        segment: StepSegment,
        blocked: BlockedBeat,
        freezes: list[Freeze],
        actor_id: str,
        shift: str,
    ) -> "Freeze | BlockedBeat | None":
        """为阻断拍上滚动的撑脚台找一块合规实垫并记台账。

        返回 Freeze 可装；BlockedBeat 表示该台滚动但无垫位/垫位被独占；
        None 表示阻断与活动台滚动无关，调用方保留原阻断。
        """
        if blocked.foot is None or blocked.beat >= segment.length - 1:
            return None
        placement = segment.solution.readings.get((blocked.beat, blocked.foot))
        if placement is None:
            return None
        point = placement.world(segment.solution.roots[blocked.beat])
        platform_name = self.stage.support(point, blocked.beat, tuple(freezes))
        if platform_name is None:
            return None
        platform = self.stage.platforms[platform_name]
        if not platform.moving:
            return None
        if any(f.platform_name == platform_name for f in freezes):
            return None  # 该台已冻，仍堵则垫位无解
        window = (blocked.beat, blocked.beat + 2)  # 覆盖区间两端拍，结束准时下架
        slot = self._find_slot(platform_name, window)
        if slot is None:
            return BlockedBeat(
                blocked.beat,
                REASON_UNSUPPORTED,
                blocked.foot,
                f"台{platform_name}前无合规固定垫位",
            )
        req = ShimRequest(actor_id, slot.name, shift, window)
        if not self.ledger.can_reserve(req):
            return BlockedBeat(
                blocked.beat,
                REASON_SHIM_CONTENDED,
                blocked.foot,
                f"垫位{slot.name}在班次{shift}已被独占",
            )
        freeze_delta = platform.delta_at(blocked.beat)
        reservation = self.ledger.reserve(req, freeze_delta)
        if reservation is None:
            return BlockedBeat(
                blocked.beat,
                REASON_SHIM_CONTENDED,
                blocked.foot,
                f"垫位{slot.name}在班次{shift}已被独占",
            )
        return Freeze(slot.name, platform_name, window, reservation.freeze_delta)

    def _find_slot(self, platform_name: str, window: tuple[int, int]) -> Optional[ShimSlot]:
        for slot in (self.stage.shim_slots or {}).values():
            if slot.platform_name == platform_name and slot.window[0] <= window[0] and window[1] <= slot.window[1]:
                return slot
        return None

    # -- 方式二：换步段 -------------------------------------------------- #
    def plan_with_swap(
        self,
        segments: dict[str, StepSegment],
        first_id: str,
        entry_root: Vec,
        exit_target: Optional[Vec],
        *,
        locked_through: int,
        notice_beat: Optional[int] = None,
    ) -> StagePlan | BlockedBeat:
        """不装固定板，在合法脚相边界换段；已播帧与已签领拍不可重来。

        时间轴状态：abs_offset 为当前段首拍在整条时间轴上的拍号。
        换步边界的全局拍号必须严格晚于 max(locked_through, notice_beat)，
        且晚于当前撑脚落地拍——通知迟于踩稳就无法从已播帧再选步。
        """
        first = segments[first_id]
        floor_global = max(locked_through, notice_beat if notice_beat is not None else -1)
        chain: list[StepSegment] = [first]
        cur_root = entry_root
        abs_offset = 0

        seg = first
        while True:
            _supports, blocked = self.verify_segment(seg, cur_root, exit_target)
            if blocked is None:
                return self._assemble_swap(chain, entry_root, exit_target)

            boundary_global = self._earliest_swap_beat_global(
                seg, blocked, floor_global, abs_offset
            )
            if boundary_global is None:
                return blocked
            swap_beat = boundary_global - abs_offset
            boundary_phase = self._phase_at(seg, swap_beat)
            boundary_root = (
                cur_root[0] + seg.solution.roots[swap_beat][0] - seg.solution.roots[0][0],
                cur_root[1] + seg.solution.roots[swap_beat][1] - seg.solution.roots[0][1],
            )
            candidate = self._find_replacement(
                segments, seg, boundary_phase, boundary_root, exit_target
            )
            if candidate is None:
                return BlockedBeat(
                    boundary_global,
                    REASON_PHASE_BOUNDARY,
                    detail=f"拍{boundary_global}没有脚相合法的替段",
                )
            # 截断当前段到换步拍，续上替段；领拍已锁，不能再回到此前边界。
            chain[-1] = _truncate(seg, swap_beat)
            floor_global = max(floor_global, boundary_global)
            abs_offset = boundary_global
            cur_root = boundary_root
            seg = candidate
            chain.append(seg)
            if abs_offset + seg.length > MAX_BEATS:
                return BlockedBeat(0, REASON_LOCKED_BEAT, detail="换段链超过八拍上限")

    def _earliest_swap_beat_global(
        self, seg: StepSegment, blocked: BlockedBeat, floor_global: int, abs_offset: int
    ) -> Optional[int]:
        """阻断拍之前最近的“撑脚落地拍”即换步边界，须严格晚于锁线。"""
        target_local = blocked.beat - abs_offset
        for b in range(min(target_local, seg.length - 2), 0, -1):
            has_landing = any(bb == b for (bb, _f) in seg.solution.readings)
            if has_landing and abs_offset + b > floor_global:
                return abs_offset + b
        return None

    @staticmethod
    def _phase_at(seg: StepSegment, beat: int) -> FootPhase:
        if beat >= seg.length - 1:
            return seg.legal_exit_phase
        return FootPhase.of(*seg.plants.get(beat, ()))

    def _find_replacement(
        self,
        segments: dict[str, StepSegment],
        current: StepSegment,
        boundary_phase: FootPhase,
        boundary_root: Vec,
        exit_target: Optional[Vec],
    ) -> Optional[StepSegment]:
        for cand in segments.values():
            if cand.segment_id == current.segment_id:
                continue
            if cand.legal_entry_phase != boundary_phase:
                continue  # 只在自己合法的脚相边界可替
            if cand.solution.roots[0] != boundary_root:
                continue  # 延续根位置
            if validate_no_glue(cand) is not None:
                continue
            if exit_target is not None and cand.solution.roots[-1] != exit_target:
                continue  # 末根对不上台口的替段不接（本段要求一次到位）
            return cand
        return None

    def _assemble_swap(
        self, chain: list[StepSegment], entry_root: Vec, exit_target: Optional[Vec]
    ) -> StagePlan | BlockedBeat:
        roots: list[Vec] = [entry_root]
        supports: list[FootSupport] = []
        cursor = entry_root
        prev_exit: Optional[FootPhase] = None
        for seg in chain:
            if seg.solution.roots[0] != cursor:
                return BlockedBeat(0, REASON_PHASE_BOUNDARY, detail="接步根位置不延续")
            if prev_exit is not None and seg.legal_entry_phase != prev_exit:
                return BlockedBeat(0, REASON_PHASE_BOUNDARY, detail="接步脚相不延续")
            sup, blocked = self.verify_segment(seg, cursor)
            if blocked is not None:
                return blocked
            supports.extend(sup)  # type: ignore[arg-type]
            shift = (
                seg.solution.roots[-1][0] - seg.solution.roots[0][0],
                seg.solution.roots[-1][1] - seg.solution.roots[0][1],
            )
            cursor = (cursor[0] + shift[0], cursor[1] + shift[1])
            roots.extend(seg.solution.roots[1:])
            prev_exit = seg.legal_exit_phase
        if exit_target is not None and roots[-1] != exit_target:
            return BlockedBeat(len(roots) - 1, REASON_EXIT_MISMATCH, detail="末根接不到台口")
        return StagePlan(
            MODE_SWAP,
            tuple(s.segment_id for s in chain),
            tuple(roots),
            tuple(supports),
            (),
            chain[0].legal_entry_phase,
            chain[-1].legal_exit_phase,
        )


def _truncate(seg: StepSegment, beat: int) -> StepSegment:
    """把段截到换步拍（领拍之前的帧原样保留，不重画）。"""
    sol = seg.solution
    new_roots = sol.roots[: beat + 1]
    new_readings = {k: v for k, v in sol.readings.items() if k[0] <= beat}
    new_deltas = sol.root_deltas[:beat]
    new_plants = {b: feet for b, feet in seg.plants.items() if b < beat}
    truncated = StepSolution(
        roots=new_roots,
        readings=new_readings,
        root_deltas=new_deltas,
        entry_phase=sol.entry_phase,
        exit_phase=StagePlanner._phase_at(seg, beat),
        ambiguous_beats=tuple(b for b in sol.ambiguous_beats if b <= beat),
    )
    return StepSegment(
        segment_id=seg.segment_id,
        solution=truncated,
        plants=new_plants,
        legal_entry_phase=seg.legal_entry_phase,
        legal_exit_phase=truncated.exit_phase,
    )
