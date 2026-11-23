"""虚构工坊的成本联算纯函数：互供网络上的精确分数归线。

模型
----

网络节点分两类：

* **服务中心**：可注入直接成本，各自签一张分送表，把收到的全部金额按
  比例（合计严格 100%）分送出去，目标可以是另一中心或某条生产线；
* **生产线（线）**：只进不出，是成本的最终承担者。

“已受入的内部服务也要按同一比例再分”意味着每个中心的吞吐量满足

    q_i = d_i + Σ_{j→i} q_j · share(j, i)

其中 ``d_i`` 是中心 i 的直接成本。线 L 的最终金额是

    t_L = Σ_{j→L} q_j · share(j, L)

中心之间允许成环（互相供气、洗版室给动力室供气等），因此本模块用
强连通分量（SCC）把网络缩成 DAG，源向逐个分量求解；每个开 SCC 内部
用 :class:`fractions.Fraction` 做高斯消元，全程无浮点、全部精确分数。

闭圈语义（题面）
----------------

* 完全内部打转、没有任何钱流入的空转圈：从零开始的实际传递解释，
  所有节点吞吐量为 0，不污染任何线；
* 闭圈内（含从外部注入后被吸入闭圈的金额）若有正成本注入，方程无解，
  这笔钱无法归线，按**来源**报告“困圈金额”，并指出钱从哪条注入来。

本模块不含任何 I/O，便于独立测试与独立重算审计。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .errors import DistributionError, NetworkError, SingularNetwork

ZERO = Fraction(0)
ONE = Fraction(1)


# ---------------------------------------------------------------- 数据结构


@dataclass(frozen=True)
class Distribution:
    """一个中心签出的分送表：目标 -> 比例，比例合计必须严格等于 1。"""

    source: str
    shares: dict[str, Fraction]

    def __post_init__(self) -> None:
        if not self.shares:
            raise DistributionError(f"中心 {self.source} 的分送表为空，无法分送")
        if len(set(self.shares)) != len(self.shares):
            raise DistributionError(f"中心 {self.source} 的分送目标重复")
        total = ZERO
        for target, share in self.shares.items():
            if not isinstance(share, Fraction):
                raise DistributionError(f"{self.source}->{target} 比例必须是 Fraction")
            if share <= 0:
                raise DistributionError(
                    f"{self.source}->{target} 比例必须为正数: {share}"
                )
            total += share
        if total != ONE:
            raise DistributionError(
                f"中心 {self.source} 分送比例合计 {total} != 1（必须 100%）"
            )


@dataclass(frozen=True)
class SourceTrace:
    """一项直接来源的完整走向（按单位注入的实际传递解释）。

    * ``line_share`` / ``line_amount``：该来源最终落到每条线的比例与金额；
    * ``trap_share`` / ``trap_amount``：该来源被闭圈吞掉、无法归线的部分；
    * ``edge_flow_share`` / ``edge_flow_amount``：每条中心间分送边上承载的
      该来源流量，买方可据此逐跳复核“每一项直接来源怎样走进最终数”。

    守恒：``Σline_share + trap_share = 1``（仅对正注入来源报告）。
    """

    source: str
    injected: Fraction
    line_share: dict[str, Fraction]
    line_amount: dict[str, Fraction]
    trap_share: Fraction
    trap_amount: Fraction
    edge_flow_share: dict[tuple[str, str], Fraction]
    edge_flow_amount: dict[tuple[str, str], Fraction]


@dataclass(frozen=True)
class TrapReport:
    """一笔无法归线的成本：金额、注入来源、困在哪个闭圈（SCC 成员）。"""

    amount: Fraction
    sources: dict[str, Fraction]
    component: tuple[str, ...]


@dataclass(frozen=True)
class AllocationResult:
    """一次完整成本联算的全部结果。"""

    center_throughput: dict[str, Fraction]
    line_amount: dict[str, Fraction]
    line_by_source: dict[str, dict[str, Fraction]]
    source_traces: dict[str, SourceTrace]
    traps: tuple[TrapReport, ...]
    components: tuple[tuple[str, ...], ...]
    closed_components: tuple[tuple[str, ...], ...]

    def total_direct_cost(self) -> Fraction:
        return sum(self.source_traces[s].injected for s in self.source_traces) or ZERO

    def total_trapped(self) -> Fraction:
        return sum(t.amount for t in self.traps) or ZERO


# ---------------------------------------------------------------- Tarjan SCC


def _tarjan_scc(nodes: list[str], succ: dict[str, list[str]]) -> list[list[str]]:
    """Tarjan 强连通分量。返回分量列表，顺序天然为汇 -> 源。"""
    index_of: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    counter = 0
    components: list[list[str]] = []

    def strong(v: str) -> None:
        nonlocal counter
        index_of[v] = low[v] = counter
        counter += 1
        stack.append(v)
        on_stack.add(v)
        for w in succ[v]:
            if w not in index_of:
                strong(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index_of[w])
        if low[v] == index_of[v]:
            comp: list[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            components.append(comp)

    for v in nodes:
        if v not in index_of:
            strong(v)
    return components


def _gauss_solve(matrix: list[list[Fraction]], rhs: list[Fraction]) -> list[Fraction]:
    """精确分数高斯-约当消元。调用方保证方程组有唯一解。"""
    n = len(matrix)
    a = [row[:] + [rhs[i]] for i, row in enumerate(matrix)]
    row = 0
    for col in range(n):
        pivot = next((r for r in range(row, n) if a[r][col] != ZERO), None)
        if pivot is None:
            raise SingularNetwork(f"第 {col} 列找不到主元，闭圈注入无法归线")
        a[row], a[pivot] = a[pivot], a[row]
        pivot_val = a[row][col]
        a[row] = [x / pivot_val for x in a[row]]
        for r in range(n):
            if r != row and a[r][col] != ZERO:
                factor = a[r][col]
                a[r] = [x - factor * y for x, y in zip(a[r], a[row])]
        row += 1
    return [a[i][n] for i in range(n)]


# ---------------------------------------------------------------- 网络与求解


@dataclass
class _Network:
    centers: list[str]
    lines: list[str]
    dist: dict[str, Distribution]
    succ: dict[str, list[str]]
    center_succ: dict[str, list[str]]
    line_succ: dict[str, list[str]]
    share: dict[tuple[str, str], Fraction]
    components: list[list[str]]
    comp_of: dict[str, int]
    is_closed: list[bool]


def _build_network(
    distributions: list[Distribution], lines: list[str]
) -> _Network:
    centers = sorted({d.source for d in distributions})
    line_set = set(lines)
    if len(line_set) != len(lines):
        raise NetworkError(f"生产线标识重复: {lines}")
    if set(centers) & line_set:
        raise NetworkError("同一节点不能既是服务中心又是生产线")
    dist = {d.source: d for d in distributions}
    if len(dist) != len(centers):
        raise DistributionError("存在重复登记的分送表")

    succ: dict[str, list[str]] = {c: [] for c in centers}
    center_succ: dict[str, list[str]] = {c: [] for c in centers}
    line_succ: dict[str, list[str]] = {c: [] for c in centers}
    share: dict[tuple[str, str], Fraction] = {}
    for d in distributions:
        for target, frac in d.shares.items():
            if target not in dist and target not in line_set:
                raise NetworkError(
                    f"中心 {d.source} 的分送目标 {target} 既不是中心也不是生产线"
                )
            succ[d.source].append(target)
            share[(d.source, target)] = frac
            if target in line_set:
                line_succ[d.source].append(target)
            else:
                center_succ[d.source].append(target)
    for c in centers:
        succ[c].sort()
        center_succ[c].sort()
        line_succ[c].sort()

    raw_components = _tarjan_scc(centers, center_succ)
    comp_of = {node: idx for idx, comp in enumerate(raw_components) for node in comp}
    # 闭 SCC：分量内中心没有任何一条边通向分量外（含通向线的出口）。
    is_closed: list[bool] = []
    for idx, comp in enumerate(raw_components):
        member = set(comp)
        has_outlet = any(
            t not in member for c in comp for t in succ[c]
        )
        is_closed.append(not has_outlet)
    return _Network(
        centers=centers,
        lines=sorted(line_set),
        dist=dist,
        succ=succ,
        center_succ=center_succ,
        line_succ=line_succ,
        share=share,
        components=raw_components,
        comp_of=comp_of,
        is_closed=is_closed,
    )


def _component_order(net: _Network) -> list[int]:
    """SCC 缩 DAG 上的源 -> 汇拓扑序（Tarjan 给出的是汇 -> 源，取反）。"""
    comp_pred: dict[int, set[int]] = {i: set() for i in range(len(net.components))}
    for u in net.centers:
        cu = net.comp_of[u]
        for v in net.center_succ[u]:
            cv = net.comp_of[v]
            if cv != cu:
                comp_pred[cv].add(cu)
    indegree = {i: len(comp_pred[i]) for i in comp_pred}
    ready = sorted(i for i, deg in indegree.items() if deg == 0)
    order: list[int] = []
    while ready:
        idx = ready.pop(0)
        order.append(idx)
        successors = {
            net.comp_of[v]
            for u in net.components[idx]
            for v in net.center_succ[u]
        } - {idx}
        for s in successors:
            indegree[s] -= 1
            if indegree[s] == 0:
                ready.append(s)
        ready.sort()
    if len(order) != len(net.components):
        raise SingularNetwork("SCC 缩 DAG 仍有环，拓扑排序失败")  # 防御性
    return order


def _solve_source(net: _Network, source: str) -> dict[str, object]:
    """单位/实际注入下，求该来源在各中心的承载量、到线比例与困圈量。"""
    node_value: dict[str, Fraction] = {c: ZERO for c in net.centers}
    edge_flow: dict[tuple[str, str], Fraction] = {}
    line_share: dict[str, Fraction] = {L: ZERO for L in net.lines}
    trap_share: dict[int, Fraction] = {i: ZERO for i in range(len(net.components))}

    for idx in _component_order(net):
        comp = net.components[idx]

        # 每个节点的外部受入 = 单位注入（仅来源节点）+ 分量外中心沿边的流量。
        # 不能只看分量入口：跨 SCC 边可以指向分量内任意节点。
        def external_in(node: str) -> Fraction:
            value = ONE if node == source else ZERO
            for u in net.centers:
                if net.comp_of[u] == idx:
                    continue
                if node in net.center_succ[u]:
                    value += node_value[u] * net.share[(u, node)]
            return value

        if net.is_closed[idx]:
            # 闭圈：内部金额无法离开。外部受入全部困在本分量
            # （空转且无注入时受入为 0，不污染任何线）。
            incoming = sum(external_in(c) for c in comp)
            trap_share[idx] += incoming
            for c in comp:
                # 从零开始的实际传递：无出口即不产生吞吐量。
                node_value[c] = ZERO
            continue

        if len(comp) == 1:
            # 单点 SCC：唯一可能的分量内边是自环 c->c。
            # q_c - share(c,c)·q_c = 外部受入  =>  q_c = 受入 / (1 - 自环比例)。
            c = comp[0]
            self_loop = net.share.get((c, c), ZERO)
            rhs_in = external_in(c)
            node_value[c] = rhs_in / (ONE - self_loop) if self_loop else rhs_in
        else:
            # 吞吐量方程 q_c - Σ_{u∈comp, u->c} share(u,c)·q_u = c 的外部受入。
            # 系数来自 u 的**入边**比例 share(u,c)，不是 c 的出边；
            # u == c 即自环，计入对角元，不能漏。
            matrix: list[list[Fraction]] = []
            rhs: list[Fraction] = []
            for c in comp:
                row = [ONE if v == c else ZERO for v in comp]
                for u in comp:
                    if c in net.center_succ[u]:
                        row[comp.index(u)] -= net.share[(u, c)]
                matrix.append(row)
                rhs.append(external_in(c))
            vals = _gauss_solve(matrix, rhs)
            for c, val in zip(comp, vals):
                node_value[c] = val

        for c in comp:
            q = node_value[c]
            for v in net.center_succ[c]:
                flow = q * net.share[(c, v)]
                edge_flow[(c, v)] = edge_flow.get((c, v), ZERO) + flow
            for L in net.line_succ[c]:
                line_share[L] += q * net.share[(c, L)]

    return {
        "node_value": node_value,
        "line_share": line_share,
        "edge_flow": edge_flow,
        "trap_share": trap_share,
    }


def solve_allocation(
    distributions: list[Distribution],
    direct_cost: dict[str, Fraction],
    lines: list[str],
) -> AllocationResult:
    """对整张互供网络做一次完整的精确分数成本联算。

    ``direct_cost`` 只允许给中心（生产线不能注成本）；零成本中心可只参与
    传递。返回每个中心吞吐量、每条线最终金额、逐来源走向与困圈报告。
    """
    net = _build_network(distributions, lines)
    for c in direct_cost:
        if c not in net.dist:
            raise NetworkError(f"直接成本中心 {c} 没有分送表，不在网络内")
        if direct_cost[c] < 0:
            raise NetworkError(f"中心 {c} 直接成本不能为负")

    sources = sorted(c for c, amount in direct_cost.items() if amount > 0)
    per_source: dict[str, dict[str, object]] = {
        s: _solve_source(net, s) for s in sources
    }

    # 总吞吐量 = 各来源承载量按实际注入金额加权。
    throughput: dict[str, Fraction] = {c: ZERO for c in net.centers}
    line_amount: dict[str, Fraction] = {L: ZERO for L in net.lines}
    line_by_source: dict[str, dict[str, Fraction]] = {
        s: {L: ZERO for L in net.lines} for s in sources
    }
    for s in sources:
        amount = direct_cost[s]
        unit = per_source[s]
        for c in net.centers:
            throughput[c] += amount * unit["node_value"][c]  # type: ignore[index]
        for L in net.lines:
            money = amount * unit["line_share"][L]  # type: ignore[index]
            line_amount[L] += money
            line_by_source[s][L] = money

    # 守恒核对（一）：每个开中心 吞吐量 = 直接成本 + 全部中心受入。
    for c in net.centers:
        incoming = direct_cost.get(c, ZERO) + sum(
            throughput[u] * net.share[(u, c)]
            for u in net.centers if c in net.center_succ[u]
        )
        idx = net.comp_of[c]
        if not net.is_closed[idx]:
            if throughput[c] != incoming:
                raise SingularNetwork(
                    f"中心 {c} 吞吐量 {throughput[c]} != 受入 {incoming}，守恒被破坏"
                )

    # 守恒核对（二）：总直接成本 = 总线金额 + 总困圈金额。
    traces: dict[str, SourceTrace] = {}
    trap_by_comp: dict[int, dict[str, Fraction]] = {
        i: {} for i in range(len(net.components)) if net.is_closed[i]
    }
    for s in sources:
        amount = direct_cost[s]
        unit = per_source[s]
        line_share = unit["line_share"]  # type: ignore[assignment]
        edge_flow = unit["edge_flow"]  # type: ignore[assignment]
        trap_share = unit["trap_share"]  # type: ignore[assignment]
        trap_total = ZERO
        for idx, share in trap_share.items():  # type: ignore[union-attr]
            if share > 0:
                trap_by_comp[idx][s] = amount * share
                trap_total += amount * share
        share_sum = sum(line_share.values(), ZERO) + sum(trap_share.values(), ZERO)
        if share_sum != ONE:
            raise SingularNetwork(f"来源 {s} 走向合计 {share_sum} != 1")
        traces[s] = SourceTrace(
            source=s,
            injected=amount,
            line_share={L: line_share[L] for L in net.lines if line_share[L]},
            line_amount={L: amount * line_share[L]
                         for L in net.lines if line_share[L]},
            trap_share=sum(trap_share.values(), ZERO),
            trap_amount=trap_total,
            edge_flow_share={e: f for e, f in edge_flow.items() if f},
            edge_flow_amount={e: amount * f for e, f in edge_flow.items() if f},
        )

    traps = tuple(
        TrapReport(
            amount=sum(by_source.values(), ZERO),
            sources=dict(sorted(by_source.items())),
            component=tuple(sorted(net.components[idx])),
        )
        for idx, by_source in sorted(trap_by_comp.items())
        if by_source
    )

    total_in = sum(direct_cost.values(), ZERO)
    total_out = sum(line_amount.values(), ZERO) + sum(t.amount for t in traps)
    if total_in != total_out:
        raise SingularNetwork(
            f"全局守恒被破坏：注入 {total_in} != 归线+困圈 {total_out}"
        )

    return AllocationResult(
        center_throughput=throughput,
        line_amount=line_amount,
        line_by_source=line_by_source,
        source_traces=traces,
        traps=traps,
        components=tuple(tuple(sorted(c)) for c in net.components),
        closed_components=tuple(
            tuple(sorted(net.components[i]))
            for i in range(len(net.components))
            if net.is_closed[i]
        ),
    )


def unit_burden(line_amount: Fraction, standard_output: Fraction) -> Fraction:
    """单件负担 = 该线本期签出金额 / 正数标准产量。"""
    if standard_output <= 0:
        raise NetworkError(f"标准产量必须为正数: {standard_output}")
    return line_amount / standard_output
