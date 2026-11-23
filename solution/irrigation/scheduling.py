"""畦节奏与受水单枚举（最多八嘴、八格）。

约束
----
* 一畦可挂一个或几个喷口；每个开口每个时格只浇一畦，每畦每格至多一口。
* 每畦有允许时格窗口、应浇格数 ``need``、相邻两次受水之间必须空出的格数
  ``gap``：两次受水时格 ``t1 < t2`` 必须满足 ``t2 - t1 > gap``。
* 换班不重置间歇：时格全局连续编号（上一班末尾可用 0 或负数），跨班已浇的
  一格仍算在间歇里。
* 受水单还要与试水联动：同一格真正开的阀组必须试水达标（由调用方通过
  ``group_ok`` 注入，避免本模块依赖具体阀门组织）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Callable, FrozenSet

GroupOk = Callable[[FrozenSet[str]], bool]


class ScheduleError(ValueError):
    """畦定义或枚举参数不合法。"""


@dataclass(frozen=True)
class Bed:
    bed_id: str
    nozzles: FrozenSet[str]
    window: FrozenSet[int]
    need: int
    gap: int = 0

    def __post_init__(self) -> None:
        if not self.nozzles:
            raise ScheduleError(f"畦 {self.bed_id} 至少要认一个喷口")
        if not self.window:
            raise ScheduleError(f"畦 {self.bed_id} 允许时格为空")
        if self.need <= 0:
            raise ScheduleError(f"畦 {self.bed_id} 应浇格数必须为正")
        if self.need > len(self.window):
            raise ScheduleError(
                f"畦 {self.bed_id} 需 {self.need} 格但窗口只有 {len(self.window)} 格"
            )
        if self.gap < 0:
            raise ScheduleError(f"畦 {self.bed_id} 间隔格数不能为负")


@dataclass(frozen=True)
class WateringOrder:
    """一张可交场的受水单：格 -> (畦 -> 水嘴)，另附每格真实阀组。"""

    assignments: dict[int, dict[str, str]]
    valve_groups: dict[int, FrozenSet[str]]
    feasible: bool
    note: str | None = None

    def watered_slots(self, bed_id: str) -> tuple[int, ...]:
        return tuple(
            t for t, asg in sorted(self.assignments.items()) if bed_id in asg
        )


@dataclass
class EnumerationResult:
    feasible: list[WateringOrder] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.feasible)


def _slot_options(beds: list[Bed], slot: int) -> list[dict[str, str]]:
    """该格所有“每畦至多一口、每口至多一畦”的局部指派（含空指派）。"""
    candidates = [b for b in beds if slot in b.window]
    options: list[dict[str, str]] = [{}]

    def rec(idx: int, amap: dict[str, str], used: set[str]) -> None:
        if idx == len(candidates):
            if amap:
                options.append(dict(amap))
            return
        bed = candidates[idx]
        rec(idx + 1, amap, used)  # 本格不浇这一畦
        for nozzle in sorted(bed.nozzles - used):
            amap[bed.bed_id] = nozzle
            rec(idx + 1, amap, used | {nozzle})
            del amap[bed.bed_id]

    rec(0, {}, set())
    return options


def enumerate_schedules(
    beds: list[Bed],
    slots: list[int],
    group_ok: GroupOk,
    history: dict[str, tuple[int, ...]] | None = None,
    limit: int = 100_000,
) -> EnumerationResult:
    """全量枚举可交场的阀位 + 受水单。

    ``group_ok`` 由阀门组织提供：给定同格真实开的水嘴集合，返回该阀组此刻
    是否持有有效出水许可（试水达标且阀牌未被收走）。``history`` 是换班前
    出水回条上已发生的真实受水时格，间歇检查把它与新班时格连在一起算。
    """
    if len(slots) > 8:
        raise ScheduleError("展台最多八格")
    history = history or {}
    beds_by_id = {b.bed_id: b for b in beds}
    unknown = set(history) - set(beds_by_id)
    if unknown:
        raise ScheduleError(f"历史回条属于未知畦：{sorted(unknown)}")

    per_slot = [_slot_options(beds, slot) for slot in slots]
    result = EnumerationResult()
    seen: set[tuple[tuple[int, tuple[tuple[str, str], ...]], ...]] = set()
    combos = 0

    for combo in product(*per_slot):
        combos += 1
        if combos > limit:
            raise ScheduleError("枚举组合超过上限，请缩小展台规模")
        assignments: dict[int, dict[str, str]] = {}
        watered: dict[str, list[int]] = {b.bed_id: [] for b in beds}
        fingerprint_rows = []
        for slot, amap in zip(slots, combo):
            assignments[slot] = dict(amap)
            row = tuple(sorted(amap.items()))
            fingerprint_rows.append((slot, row))
            for bed_id in amap:
                watered[bed_id].append(slot)
        fingerprint = tuple(fingerprint_rows)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)

        # 需量
        need_bad = [
            b.bed_id for b in beds if len(watered[b.bed_id]) != b.need
        ]
        if need_bad:
            result.blockers.append(f"应浇格数不符：{need_bad}")
            continue

        # 间歇：历史回条 + 本班受水，统一按时格差检查（跨班不重置）。
        gap_bad = []
        for bed in beds:
            times = sorted(set(history.get(bed.bed_id, ())) | set(watered[bed.bed_id]))
            if any(t2 - t1 <= bed.gap for t1, t2 in zip(times, times[1:])):
                gap_bad.append(bed.bed_id)
        if gap_bad:
            result.blockers.append(f"相邻受水未空够格数：{gap_bad}")
            continue

        # 同格真实阀组必须持有效出水许可。
        valve_groups: dict[int, FrozenSet[str]] = {}
        pressure_bad: list[int] = []
        for slot, amap in assignments.items():
            group = frozenset(amap.values())
            valve_groups[slot] = group
            if group and not group_ok(group):
                pressure_bad.append(slot)
        if pressure_bad:
            result.blockers.append(
                f"阀组试水不达标或无出水许可的时格：{pressure_bad}"
            )
            continue

        result.feasible.append(
            WateringOrder(
                assignments=assignments,
                valve_groups=valve_groups,
                feasible=True,
            )
        )
    return result
