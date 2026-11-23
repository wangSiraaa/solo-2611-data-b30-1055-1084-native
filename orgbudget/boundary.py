"""工坊边界表：若干同址中心作为一个工坊对外交割。

联营会可以不向外协暴露工坊内部互供明细，只对外给出：

* 每个**外来入口**进一单位时，如何在各出口流出、滞在内部闭圈、
  或进入各生产线的准确比例；
* 工坊**自有成本**（每个内部中心注一单位）的同口径走向证明。

边界表只暴露跨边界的边、线与闭圈汇总，工坊内部边的明细留在联营会侧。
相邻工坊之间的回流在工坊级缩图上仍可见，联营会据此组合边界表并发现
跨中心（跨工坊）封闭圈。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .costflow import Distribution, _build_network, _solve_source

ZERO = Fraction(0)
ONE = Fraction(1)


@dataclass(frozen=True)
class UnitRoute:
    """一单位注入（外来入口或自有成本）在工坊边界上的准确去向。"""

    entry: str
    is_own_cost: bool
    exit_share: dict[tuple[str, str], Fraction]   # 跨边界边 (内部中心 -> 外部中心)
    line_share: dict[str, Fraction]               # 进入各生产线的比例
    trapped_share: Fraction                       # 滞在工坊内部闭圈的比例
    cross_workshop_share: Fraction                # 滞在跨工坊闭圈（出边后不回）
    trapped_components: tuple[tuple[str, ...], ...]

    def total(self) -> Fraction:
        return sum(self.exit_share.values(), ZERO) + sum(
            self.line_share.values(), ZERO
        ) + self.trapped_share + self.cross_workshop_share


@dataclass(frozen=True)
class WorkshopBoundary:
    workshop_id: str
    members: tuple[str, ...]
    routes: tuple[UnitRoute, ...]

    def route_of(self, entry: str) -> UnitRoute:
        for route in self.routes:
            if route.entry == entry:
                return route
        raise KeyError(f"入口 {entry} 不在工坊 {self.workshop_id} 边界表中")


def build_boundary(
    workshop_id: str,
    members: set[str],
    distributions: list[Distribution],
    lines: list[str],
) -> WorkshopBoundary:
    """对给定同址中心集合构造单位口径边界表。

    外来入口 = 有边指向工坊成员的外部中心；自有成本入口 = 每个成员自身。
    全程在完整网络上做单位注入求解，再按边界裁剪，保证比例守恒。
    """
    members = set(members)
    net = _build_network(distributions, lines)
    unknown = members - set(net.centers)
    if unknown:
        raise ValueError(f"工坊成员不在网络中: {sorted(unknown)}")

    external_entries = sorted({
        u for u in net.centers
        if u not in members and any(v in members for v in net.center_succ[u])
    })
    # 自有入口只含本工坊成员；外来入口单独成列，二者口径不同（外来单位
    # 注入可能只有一部分跨过本工坊边界，不强制合计为 1）。
    entries = [(e, False) for e in external_entries]
    entries += sorted((m, True) for m in members)

    routes: list[UnitRoute] = []
    for entry, is_own in entries:
        unit = _solve_source(net, entry)
        edge_flow: dict[tuple[str, str], Fraction] = unit["edge_flow"]  # type: ignore[assignment]
        exits = {
            edge: flow for edge, flow in edge_flow.items()
            if edge[0] in members and edge[1] not in members
        }
        line_share = {
            L: share for L, share in unit["line_share"].items() if share  # type: ignore[union-attr]
        }
        trapped = ZERO
        trapped_components: list[tuple[str, ...]] = []
        for idx, share in unit["trap_share"].items():  # type: ignore[union-attr]
            if not share:
                continue
            comp = set(net.components[idx])
            if comp <= members:
                trapped += share
                trapped_components.append(tuple(sorted(comp)))
        # 拓扑事实：入口存在一条指向工坊成员的边，即单位注入“踏进”工坊。
        # 全局闭 SCC 情形下承载量可能为 0（钱被困），但入口资格仍成立。
        enters_topologically = any(
            v in members for v in net.center_succ.get(entry, [])
        )
        if not is_own and not enters_topologically:
            continue  # 外来注入没有任何边进入工坊，不生成路由
        # 自有入口的“进入量”是整单位；外来取跨边界入边承载量，闭圈时补 1。
        entering = ONE if is_own else sum(
            flow for (u, v), flow in edge_flow.items()
            if v in members and u not in members
        )
        if not is_own and entering == ZERO:
            entering = ONE
        accounted_internal = (
            sum(exits.values(), ZERO)
            + sum(line_share.values(), ZERO)
            + trapped
        )
        # 踏进工坊却没走出口、没入线、没困在工坊内闭圈的余额：
        # 滞在跨工坊闭圈（含全局闭 SCC 中超出本工坊边界的部分）。
        cross_workshop = entering - accounted_internal
        if cross_workshop < ZERO:
            raise ArithmeticError(
                f"工坊 {workshop_id} 入口 {entry} 边界去向超过进入量"
            )
        route = UnitRoute(
            entry=entry, is_own_cost=is_own,
            exit_share=dict(sorted(exits.items())),
            line_share=dict(sorted(line_share.items())),
            trapped_share=trapped,
            cross_workshop_share=cross_workshop,
            trapped_components=tuple(trapped_components),
        )
        if is_own and route.total() != ONE:
            raise ArithmeticError(
                f"工坊 {workshop_id} 自有入口 {entry} 边界去向合计 {route.total()} != 1"
            )
        routes.append(route)

    return WorkshopBoundary(
        workshop_id=workshop_id,
        members=tuple(sorted(members)),
        routes=tuple(routes),
    )


def combine_and_find_closed_loops(
    boundaries: list[WorkshopBoundary],
    distributions: list[Distribution] | None = None,
) -> tuple[tuple[frozenset[str], ...], ...]:
    """组合多张工坊边界表，在工坊级缩图上发现跨中心封闭圈。

    边界边 (内部中心 u -> 外部中心 v) 给出工坊 A -> 工坊 B 的回流；
    出口目标不属于任何工坊（裸中心）时归入虚拟 ``外部`` 组，不参与闭圈。
    封闭圈 = 工坊级 SCC 没有任何通向圈外工坊或生产线的出口。

    全局闭 SCC 内承载量为 0，边界表的非零流量里看不到这条边，因此
    可选传入原始分送表，用拓扑边补全工坊级图。
    """
    owner: dict[str, str] = {}
    for boundary in boundaries:
        for m in boundary.members:
            owner[m] = boundary.workshop_id
    workshop_ids = {b.workshop_id for b in boundaries}

    succ: dict[str, set[str]] = {w: set() for w in workshop_ids}
    has_real_outlet: dict[str, bool] = {w: False for w in workshop_ids}
    for boundary in boundaries:
        for route in boundary.routes:
            if route.line_share:
                has_real_outlet[boundary.workshop_id] = True
            for (u, v) in route.exit_share:
                target = owner.get(v)
                if target is None:
                    # 出口通向工坊体系外的裸中心：真实出边，不可能闭圈。
                    has_real_outlet[boundary.workshop_id] = True
                elif target != boundary.workshop_id:
                    succ[boundary.workshop_id].add(target)

    # 闭圈回流承载量为 0，边界表看不到；用原始分送表的拓扑边补全。
    if distributions is not None:
        for d in distributions:
            if d.source not in owner:
                continue
            src_workshop = owner[d.source]
            for target in d.shares:
                if target in owner:
                    dst_workshop = owner[target]
                    if dst_workshop != src_workshop:
                        succ[src_workshop].add(dst_workshop)
                else:
                    has_real_outlet[src_workshop] = True

    # Tarjan（工坊级，节点很少，内联一个简版）。
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on: set[str] = set()
    counter = 0
    components: list[list[str]] = []

    def strong(v: str) -> None:
        nonlocal counter
        index[v] = low[v] = counter
        counter += 1
        stack.append(v)
        on.add(v)
        for w in sorted(succ[v]):
            if w not in index:
                strong(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp: list[str] = []
            while True:
                w = stack.pop()
                on.discard(w)
                comp.append(w)
                if w == v:
                    break
            components.append(comp)

    for w in sorted(workshop_ids):
        if w not in index:
            strong(w)

    closed: list[frozenset[str]] = []
    for comp in components:
        member = set(comp)
        outlet = any(has_real_outlet[w] for w in comp) or any(
            t not in member for w in comp for t in succ[w]
        )
        if not outlet:
            closed.append(frozenset(comp))
    return tuple(sorted(closed, key=lambda s: tuple(sorted(s))))
