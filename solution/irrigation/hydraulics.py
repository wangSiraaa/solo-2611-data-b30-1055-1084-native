"""具名水嘴、有根干管树与试水压力（虚构灌水展台，纯合成教学模型）。

口径要点
--------
* 接头表把实物端点连成一棵有根树；水嘴是旧巡检命中的**具名实体**。不同断链
  段里标签相同的水嘴是两个不同节点（:class:`Nozzle` 以节点 id 为身份），重名
  不会被就近合并，树边只在接头表登记的端点之间形成。
* 按报告当时的实际覆盖核定本批检查过的水嘴和干管段；**跨未检段的支路不给
  出水许可**——只要试水组的任一水嘴位于未检段下游，整组拒批。
* 每段压降 = 系数 × 该段流过的总流量，自首端沿途相减。首端压头、喷口流量、
  系数均为整数，余压亦为整数。
* 试水按“同时开的一组喷口”独立出证；分别单开的达标证不能相加。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet, Mapping


class HydraulicError(ValueError):
    """管网构造或试水请求不合法。"""


@dataclass(frozen=True)
class Nozzle:
    """具名水嘴。``label`` 可在不同断链段重号，``node_id`` 才是身份。"""

    node_id: str
    label: str
    flow: int
    min_pressure: int
    inspected: bool = True

    def __post_init__(self) -> None:
        if self.flow <= 0:
            raise HydraulicError(f"水嘴 {self.node_id} 流量必须为正整数")
        if self.min_pressure < 0:
            raise HydraulicError(f"水嘴 {self.node_id} 最低压力不能为负")


@dataclass(frozen=True)
class Pipe:
    parent: str
    child: str
    coeff: int
    inspected: bool = True

    def __post_init__(self) -> None:
        if self.coeff < 0:
            raise HydraulicError(f"管段 {self.parent}->{self.child} 系数不能为负")


@dataclass(frozen=True)
class PressureReading:
    """单嘴试水读数。"""

    nozzle_id: str
    residual: int
    min_pressure: int
    ok: bool


@dataclass(frozen=True)
class PressureTest:
    """一组同时打开的水嘴的独立试水结果。"""

    open_nozzles: FrozenSet[str]
    permitted: bool
    reason: str | None
    readings: tuple[PressureReading, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return self.permitted and all(r.ok for r in self.readings)


class PipeTree:
    """有根干管树：节点（含水嘴节点）+ 父子管段。"""

    def __init__(
        self,
        root_id: str,
        head: int,
        pipes: list[Pipe],
        nozzles: list[Nozzle],
    ) -> None:
        if head < 0:
            raise HydraulicError("首端压头不能为负")
        self.root_id = root_id
        self.head = head
        self._pipe_by_child: dict[str, Pipe] = {}
        self._children: dict[str, list[str]] = {root_id: []}
        for pipe in pipes:
            if pipe.child in self._pipe_by_child:
                raise HydraulicError(f"节点 {pipe.child} 有两个父管，不是树")
            self._pipe_by_child[pipe.child] = pipe
            self._children.setdefault(pipe.parent, []).append(pipe.child)
            self._children.setdefault(pipe.child, [])
        self._nozzles: dict[str, Nozzle] = {}
        for nozzle in nozzles:
            if nozzle.node_id in self._nozzles:
                raise HydraulicError(f"水嘴节点 id 重复：{nozzle.node_id}")
            if nozzle.node_id not in self._children and nozzle.node_id != root_id:
                raise HydraulicError(f"水嘴 {nozzle.node_id} 未挂在接头表的树上")
            self._nozzles[nozzle.node_id] = nozzle
        # 每个节点到根的路径都必须连通（接头表确实给出一棵树）。
        for node in self._children:
            self._path_to_root(node)

    # ------------------------------------------------------------------
    @property
    def nozzles(self) -> Mapping[str, Nozzle]:
        return dict(self._nozzles)

    def _path_to_root(self, node: str) -> list[Pipe]:
        path: list[Pipe] = []
        current = node
        seen = set()
        while current != self.root_id:
            if current in seen:
                raise HydraulicError("接头表存在环，不是有根树")
            seen.add(current)
            pipe = self._pipe_by_child.get(current)
            if pipe is None:
                raise HydraulicError(f"节点 {current} 到不了根 {self.root_id}")
            path.append(pipe)
            current = pipe.parent
        return path

    def _blocked_by_uninspected(self, nozzle_id: str) -> bool:
        """水嘴本身或其上游任一管段未检，则跨未检段、不给出水许可。"""
        nozzle = self._nozzles[nozzle_id]
        if not nozzle.inspected:
            return True
        return any(not pipe.inspected for pipe in self._path_to_root(nozzle_id))

    # ------------------------------------------------------------------
    def pressure_test(self, open_nozzles: set[str] | FrozenSet[str]) -> PressureTest:
        opened = frozenset(open_nozzles)
        unknown = opened - self._nozzles.keys()
        if unknown:
            raise HydraulicError(f"试水组含未知水嘴：{sorted(unknown)}")
        blocked = sorted(z for z in opened if self._blocked_by_uninspected(z))
        if blocked:
            return PressureTest(
                open_nozzles=opened,
                permitted=False,
                reason=f"跨未检段，不给出水许可：{blocked}",
            )

        # 子树总流量：每个水嘴节点注入自身流量，沿父管累加。
        subtree_flow = dict.fromkeys(self._children, 0)
        for nozzle_id in opened:
            subtree_flow[nozzle_id] += self._nozzles[nozzle_id].flow
        for node in sorted(
            self._children,
            key=lambda n: len(self._path_to_root(n)),
            reverse=True,
        ):
            if node == self.root_id:
                continue
            pipe = self._pipe_by_child[node]
            subtree_flow[pipe.parent] += subtree_flow[node]

        # 自根向下沿途相减；水嘴读数取其节点处的到达压力。
        pressure = {self.root_id: self.head}
        for node in sorted(
            self._children, key=lambda n: len(self._path_to_root(n))
        ):
            if node == self.root_id:
                continue
            pipe = self._pipe_by_child[node]
            pressure[node] = (
                pressure[pipe.parent] - pipe.coeff * subtree_flow[node]
            )

        readings = tuple(
            PressureReading(
                nozzle_id=z,
                residual=pressure[z],
                min_pressure=self._nozzles[z].min_pressure,
                ok=pressure[z] >= self._nozzles[z].min_pressure,
            )
            for z in sorted(opened)
        )
        return PressureTest(open_nozzles=opened, permitted=True, reason=None,
                            readings=readings)

    def all_subset_tests(self) -> dict[FrozenSet[str], PressureTest]:
        """枚举全部非空喷口子集的独立试水证（八嘴以内，2^8 可承受）。"""
        ids = sorted(self._nozzles)
        if len(ids) > 8:
            raise HydraulicError("展台最多八嘴")
        result: dict[FrozenSet[str], PressureTest] = {}
        for mask in range(1, 1 << len(ids)):
            subset = frozenset(ids[i] for i in range(len(ids)) if mask >> i & 1)
            result[subset] = self.pressure_test(subset)
        return result
