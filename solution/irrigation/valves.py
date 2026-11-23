"""两种阀门组织 + 出水回条（园丁只交所选的一套）。

方式一 :class:`MasterValveBoard`
    泵房守住总阀台，看到同格全部点名请求后签真实阀组，核共管余压后一次开闭。
    每次换阀组占一个调阀拍，该拍无一畦记作浇过；教学例可把调阀拍预算设为 0
    （每格自由重配，不耗拍）。

方式二 :class:`BranchPermits`
    开工前锁定每根出支流量上限，把阀牌发给支房小阀。支房不必等总阀拍，前提
    是各支房按各自已领许可同时开，也不会让公管上任何一嘴亏压——安全条件直接
    用并开试水核验。阀牌 ``issue`` -> ``collect`` 即终结，同一支房换班后不能
    复用已收走的牌，再要开度必须重新申领。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet

from hydraulics import PipeTree, PressureTest
from scheduling import WateringOrder


class ValveError(ValueError):
    """阀组、阀牌或回条操作不合法。"""


# ---------------------------------------------------------------------------
# 出水回条：实浇历史的唯一合法来源，跨班间歇从这里续
# ---------------------------------------------------------------------------


@dataclass
class DosingLedger:
    records: list[tuple[int, str, str, PressureTest]] = field(default_factory=list)

    def append(
        self, slot: int, bed_id: str, nozzle_id: str, test: PressureTest
    ) -> None:
        if not test.passed or nozzle_id not in test.open_nozzles:
            raise ValveError("只有试水达标的真实出水才能上回条")
        self.records.append((slot, bed_id, nozzle_id, test))

    def slots_for(self, bed_id: str) -> tuple[int, ...]:
        return tuple(sorted({s for s, b, _, _ in self.records if b == bed_id}))

    def history(self) -> dict[str, tuple[int, ...]]:
        beds = {b for _, b, _, _ in self.records}
        return {b: self.slots_for(b) for b in beds}


# ---------------------------------------------------------------------------
# 方式一：泵房总阀台
# ---------------------------------------------------------------------------


@dataclass
class MasterValveBoard:
    tree: PipeTree
    adjust_beats: int = 0
    free_reconfigure: bool = True
    beats_used: int = 0

    def group_ok(self, group: FrozenSet[str]) -> bool:
        """纯试水探针：该阀组本身能否一次开闭达标，不改任何阀台状态。

        调阀拍是执行期的换组成本，在枚举阶段不计数；由 :meth:`execute`
        按真实换组序列核定。
        """
        return self.tree.pressure_test(frozenset(group)).passed

    def execute(self, order: WateringOrder, ledger: DosingLedger) -> DosingLedger:
        """按受水单逐格开闭，回条只登记真实达标出水；调阀拍不登记。

        * ``free_reconfigure=True``（教学例默认，调阀拍为零）：每格自由重配，
          换组不耗拍、不占格。
        * ``free_reconfigure=False``：换一次非空阀组必须独占一个调阀拍，该拍
          无一畦记作浇过——即两个不同的非空阀组之间至少隔着一个空格；每次
          换组计 1 拍，预算用尽即拒交场。
        """
        signed: FrozenSet[str] = frozenset()
        beats_used = 0
        prev_water_slot: int | None = None
        for slot, amap in sorted(order.assignments.items()):
            group = frozenset(amap.values())
            if not group:
                continue
            if group != signed and not self.free_reconfigure:
                if signed:  # 首次非空组免费就位，之后换组才占拍
                    if beats_used >= self.adjust_beats:
                        raise ValveError(
                            f"调阀拍预算 {self.adjust_beats} 用尽，"
                            f"无法在格 {slot} 换成 {sorted(group)}"
                        )
                    if (
                        prev_water_slot is not None
                        and slot == prev_water_slot + 1
                    ):
                        raise ValveError(
                            f"格 {slot} 与上一实浇格 {prev_water_slot} 相邻，"
                            "调阀拍无处安放（该拍不能有畦受水）"
                        )
                    beats_used += 1
            test = self.tree.pressure_test(group)
            if not test.passed:
                raise ValveError(
                    f"阀组 {sorted(group)} 试水不达标，拒签：{test.reason}"
                )
            signed = group
            prev_water_slot = slot
            for bed_id, nozzle_id in amap.items():
                ledger.append(slot, bed_id, nozzle_id, test)
        self.beats_used = beats_used
        return ledger


# ---------------------------------------------------------------------------
# 方式二：出支流量上限 + 支房阀牌
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValveTag:
    tag_id: str
    branch_id: str
    allowed_group: FrozenSet[str]  # 该牌允许本支出的水嘴集合


@dataclass
class BranchPermits:
    tree: PipeTree
    branch_of_nozzle: dict[str, str]
    _live: dict[str, ValveTag] = field(default_factory=dict)
    _collected: set[str] = field(default_factory=set)

    def issue(self, tag: ValveTag) -> None:
        for nozzle in tag.allowed_group:
            if self.branch_of_nozzle.get(nozzle) != tag.branch_id:
                raise ValveError(
                    f"水嘴 {nozzle} 不属于支房 {tag.branch_id}，不能发牌"
                )
        if tag.tag_id in self._collected:
            raise ValveError(f"阀牌 {tag.tag_id} 已收走终结，不能复用")
        if tag.branch_id in self._live:
            raise ValveError(f"支房 {tag.branch_id} 已持活牌，先收旧牌")
        # 安全前提：当前全部活许可之并，必须本身是试水达标组。
        union = self._live_union() | tag.allowed_group
        test = self.tree.pressure_test(union)
        if not test.passed:
            raise ValveError(
                f"发牌后活许可并开将亏压（{test.readings}），拒发；"
                "单开达标证不能相加"
            )
        self._live[tag.branch_id] = tag

    def collect(self, branch_id: str) -> None:
        tag = self._live.pop(branch_id, None)
        if tag is None:
            raise ValveError(f"支房 {branch_id} 无活牌可收")
        self._collected.add(tag.tag_id)

    def reissue(self, tag: ValveTag) -> None:
        """换班后重新开度：必须是新 tag_id，旧牌不复用。"""
        self.issue(tag)

    def _live_union(self) -> FrozenSet[str]:
        union: set[str] = set()
        for tag in self._live.values():
            union |= tag.allowed_group
        return frozenset(union)

    def group_ok(self, group: FrozenSet[str]) -> bool:
        """支房按各自已领许可同时开：每嘴都须落在某张活牌的许可内。"""
        return group <= self._live_union()

    def open_for(self, group: FrozenSet[str]) -> PressureTest:
        if not self.group_ok(group):
            raise ValveError(f"组 {sorted(group)} 超出活牌许可，支房不得自开")
        test = self.tree.pressure_test(group)
        if not test.passed:
            raise ValveError("活牌并开竟亏压，发牌校验失效")
        return test

    def execute(self, order: WateringOrder, ledger: DosingLedger) -> DosingLedger:
        for slot, amap in sorted(order.assignments.items()):
            group = frozenset(amap.values())
            if group:
                test = self.open_for(group)
                for bed_id, nozzle_id in amap.items():
                    ledger.append(slot, bed_id, nozzle_id, test)
        return ledger
