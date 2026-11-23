"""订单员的最低成本分工（纯函数，精确分数）。

输入是联营会签出的本期生产线金额（由成本联算得到）与外协名单：

* 单件负担 = 该线本期签出金额 ÷ 正数标准产量；
* 订单按品种给数量，现场自制总件数受单一共同上限约束；
* 某品种只要自制至少一件，就发生一次该品种（开线）的固定开线费；
* 每家外协给出可做品种、逐件价与该单共同产能；
* 必须交足每个品种，且最低成本不超过买方封价才可签。

规模上限（6 中心 / 4 品种 / 十来件）下，对自制向量做完全枚举、对每个
向量用记忆化递归求外协最低分配，得到**可复核的全局最低成本**，并给出
每个约束放松一档后的反算成本，直接回答“哪条真实开线或外协额度阻止
降价”。全部金额为 :class:`fractions.Fraction`。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .errors import InfeasibleOrder, SealPriceRejected

ZERO = Fraction(0)


@dataclass(frozen=True)
class LineCost:
    """一条生产线本期的计价参数。"""

    line_id: str
    product: str
    burden: Fraction          # 单件负担（线金额 / 标准产量）
    opening_fee: Fraction     # 自制该品种一次的开线费

    def __post_init__(self) -> None:
        if self.burden < 0 or self.opening_fee < 0:
            raise ValueError("负担与开线费不能为负")


@dataclass(frozen=True)
class Supplier:
    """一家外协：可做品种的逐件价 + 该单共同产能 + 本期共同名额。"""

    supplier_id: str
    prices: dict[str, Fraction]
    order_capacity: int
    period_capacity: int | None = None

    def __post_init__(self) -> None:
        if self.order_capacity < 0:
            raise ValueError("单订单共同产能不能为负")
        if self.period_capacity is not None and self.period_capacity < 0:
            raise ValueError("本期共同名额不能为负")
        for product, price in self.prices.items():
            if price < 0:
                raise ValueError(f"外协 {self.supplier_id} 对 {product} 报价不能为负")

    @property
    def effective_period_capacity(self) -> int:
        return self.period_capacity if self.period_capacity is not None else self.order_capacity


@dataclass(frozen=True)
class OrderSpec:
    order_id: str
    demand: dict[str, int]           # 品种 -> 件数
    local_capacity: int              # 现场总共最多自制件数
    seal_price: Fraction             # 买方封价

    def __post_init__(self) -> None:
        if self.local_capacity < 0:
            raise ValueError("本地产能不能为负")
        if self.seal_price < 0:
            raise ValueError("封价不能为负")
        for product, qty in self.demand.items():
            if not isinstance(qty, int) or qty <= 0:
                raise ValueError(f"品种 {product} 需求必须是正整数")


@dataclass(frozen=True)
class Plan:
    """一份可复核的分工方案。"""

    make: dict[str, int]                       # 品种 -> 自制件数
    buy: dict[str, dict[str, int]]             # 外协 -> 品种 -> 件数
    self_cost: Fraction
    opening_cost: Fraction
    subcontract_cost: Fraction
    opened_lines: tuple[str, ...]

    @property
    def total(self) -> Fraction:
        return self.self_cost + self.opening_cost + self.subcontract_cost

    def signature(self) -> tuple:
        return (
            tuple(sorted(self.make.items())),
            tuple((s, tuple(sorted(items.items())))
                  for s, items in sorted(self.buy.items()) if items),
        )


@dataclass(frozen=True)
class BlockingConstraint:
    """一条阻止继续降价的约束：放松一档后最低成本严格下降。"""

    kind: str          # 'local_capacity' | 'supplier_capacity' | 'opening_fee'
    ref: str           # 外协号 / 线号，本地产能为 ''
    detail: str
    current_min: Fraction
    relaxed_min: Fraction

    @property
    def saving(self) -> Fraction:
        return self.current_min - self.relaxed_min


@dataclass(frozen=True)
class OptimizationResult:
    order_id: str
    feasible: bool
    best: Plan | None
    alternatives: tuple[Plan, ...]
    seal_price: Fraction
    blockings: tuple[BlockingConstraint, ...]
    reason: str = ""

    @property
    def minimum_cost(self) -> Fraction:
        if self.best is None:
            raise InfeasibleOrder(f"订单 {self.order_id} 无可行分工: {self.reason}")
        return self.best.total

    def assert_signable(self) -> Plan:
        """封价校验：交足且不超封价，否则拒签。"""
        if self.best is None:
            raise InfeasibleOrder(f"订单 {self.order_id} 无法交足: {self.reason}")
        if self.best.total > self.seal_price:
            raise SealPriceRejected(
                f"最低成本 {self.best.total} 超过买方封价 {self.seal_price}，拒签"
            )
        return self.best


# ---------------------------------------------------------------- 求解


def _line_index(lines: list[LineCost]) -> dict[str, LineCost]:
    index: dict[str, LineCost] = {}
    for line in lines:
        if line.product in index:
            raise ValueError(f"品种 {line.product} 同时挂在多条线上，模型不支持")
        index[line.product] = line
    return index


def _enumerate_supplier_choices(
    supplier: Supplier, products: tuple[str, ...], remaining: tuple[int, ...]
) -> list[tuple[tuple[int, ...], Fraction]]:
    """枚举一家外协在给定残差下的全部可行（新残差向量, 报价）。

    全程使用不可变元组，避免递归分支间的可变状态污染；做不了的品种
    残差原样保留，交给后续外协。
    """
    outcomes: list[tuple[tuple[int, ...], Fraction]] = []

    def walk(pos: int, rem: tuple[int, ...], used: int, cost: Fraction) -> None:
        if used > supplier.order_capacity:
            return
        if pos == len(products):
            outcomes.append((rem, cost))
            return
        product = products[pos]
        if product not in supplier.prices:
            walk(pos + 1, rem, used, cost)
            return
        price = supplier.prices[product]
        max_qty = min(rem[pos], supplier.order_capacity - used)
        for qty in range(max_qty + 1):
            new_rem = rem[:pos] + (rem[pos] - qty,) + rem[pos + 1:]
            walk(pos + 1, new_rem, used + qty, cost + price * qty)

    walk(0, remaining, 0, ZERO)
    return outcomes


def _best_subcontract(
    suppliers: list[Supplier], residual: dict[str, int]
) -> tuple[Fraction, dict[str, dict[str, int]]] | None:
    """外协最低分配：逐外协做迭代式状态扩展 DP（残差向量 -> 最低成本）。

    每条状态记录它由**哪一层（第几家外协）**扩展而来以及前驱残差，
    重建时沿层号严格回退；零取件扩展不会让状态自指。规模受限于
    4 品种 / 十来件订单。
    """
    products = tuple(sorted(residual))
    initial = tuple(residual[p] for p in products)

    # 每条目：(最低成本, 来源层号, 本层取件向量, 前驱残差)。
    # 初始状态来源层号为 -1，且不与任何供应商层混淆。
    Entry = tuple[Fraction, int, tuple[int, ...], tuple[int, ...]]
    layers: list[dict[tuple[int, ...], Entry]] = [
        {initial: (ZERO, -1, (), ())}
    ]

    for i, sup in enumerate(suppliers):
        nxt: dict[tuple[int, ...], Entry] = {}
        for rem, (cost, _layer, _picks, _prev) in layers[i].items():
            for new_rem, add_cost in _enumerate_supplier_choices(sup, products, rem):
                total = cost + add_cost
                if new_rem not in nxt or total < nxt[new_rem][0]:
                    picks = tuple(rem[p] - new_rem[p]
                                  for p in range(len(products)))
                    nxt[new_rem] = (total, i, picks, rem)
        layers.append(nxt)

    zero = tuple(0 for _ in products)
    final = layers[-1]
    if zero not in final:
        return None
    best_cost = final[zero][0]

    # 从最后一层向前驱残差回退，每跳一层层号减 1，直到初始层（层号 -1）。
    assignment: dict[str, dict[str, int]] = {s.supplier_id: {} for s in suppliers}
    cur = zero
    layer = len(suppliers)
    while layer > 0:
        _cost, src_layer, picks, prev_rem = layers[layer][cur]
        assert src_layer == layer - 1
        for p, qty in enumerate(picks):
            if qty:
                assignment[suppliers[src_layer].supplier_id][products[p]] = qty
        cur = prev_rem
        layer -= 1
    return best_cost, {s: items for s, items in assignment.items() if items}


def optimize_order(
    order: OrderSpec,
    lines: list[LineCost],
    suppliers: list[Supplier],
    *,
    _with_blockings: bool = True,
) -> OptimizationResult:
    """枚举全部可行自制向量，求全局最低成本分工与阻止降价的约束。"""
    line_of = _line_index(lines)
    products = sorted(order.demand)
    unknown = [p for p in products if p not in line_of
               and not any(p in s.prices for s in suppliers)]
    if unknown:
        return OptimizationResult(
            order_id=order.order_id, feasible=False, best=None,
            alternatives=(), seal_price=order.seal_price, blockings=(),
            reason=f"品种无人可做: {unknown}",
        )

    plans: list[Plan] = []
    feasible = False
    make_vector = [0] * len(products)

    def enumerate_make(pos: int, used: int) -> None:
        nonlocal feasible
        if pos == len(products):
            residual = {p: order.demand[p] - make_vector[i]
                        for i, p in enumerate(products)}
            sub = _best_subcontract(suppliers, residual)
            if sub is None:
                return
            feasible = True
            sub_cost, buy = sub
            make = {p: make_vector[i] for i, p in enumerate(products) if make_vector[i]}
            self_cost = sum(
                line_of[p].burden * qty for p, qty in make.items()
            )
            opened = tuple(sorted(line_of[p].line_id for p in make))
            opening_cost = sum(line_of[p].opening_fee for p in make)
            plans.append(Plan(
                make=make, buy=buy, self_cost=self_cost,
                opening_cost=opening_cost, subcontract_cost=sub_cost,
                opened_lines=opened,
            ))
            return
        product = products[pos]
        max_make = min(order.demand[product], order.local_capacity - used)
        if product not in line_of:
            max_make = 0
        for qty in range(max_make + 1):
            make_vector[pos] = qty
            enumerate_make(pos + 1, used + qty)

    enumerate_make(0, 0)
    if not feasible:
        return OptimizationResult(
            order_id=order.order_id, feasible=False, best=None,
            alternatives=(), seal_price=order.seal_price, blockings=(),
            reason="自制与外协产能之和不足以交足订单",
        )

    minimum = min(p.total for p in plans)
    best_plans = sorted((p for p in plans if p.total == minimum),
                        key=lambda p: p.signature())
    alternatives = tuple(sorted(
        (p for p in plans if p.total > minimum), key=lambda p: (p.total, p.signature())
    ))
    best = best_plans[0]
    # 反算放松约束时只重新求解，不再递归做阻塞分析，避免指数级展开。
    blockings = (
        _blocking_report(order, lines, suppliers, best, minimum)
        if _with_blockings else ()
    )
    return OptimizationResult(
        order_id=order.order_id, feasible=True, best=best,
        alternatives=alternatives, seal_price=order.seal_price,
        blockings=blockings,
    )


def _blocking_report(
    order: OrderSpec,
    lines: list[LineCost],
    suppliers: list[Supplier],
    best: Plan,
    current_min: Fraction,
) -> tuple[BlockingConstraint, ...]:
    """逐类约束放松一档反算，严格降本的才是真实阻止项。"""
    found: list[BlockingConstraint] = []

    relaxed_local = OrderSpec(
        order.order_id, dict(order.demand),
        order.local_capacity + 1, order.seal_price,
    )
    r = optimize_order(relaxed_local, lines, suppliers, _with_blockings=False)
    if r.feasible and r.minimum_cost < current_min:
        found.append(BlockingConstraint(
            kind="local_capacity", ref="",
            detail=f"现场自制上限 {order.local_capacity} 件，增 1 件可降本",
            current_min=current_min, relaxed_min=r.minimum_cost,
        ))

    for sup in suppliers:
        relaxed_sup = Supplier(
            sup.supplier_id, dict(sup.prices),
            sup.order_capacity + 1, sup.period_capacity,
        )
        others = [relaxed_sup if s.supplier_id == sup.supplier_id else s
                  for s in suppliers]
        r = optimize_order(order, lines, others, _with_blockings=False)
        if r.feasible and r.minimum_cost < current_min:
            found.append(BlockingConstraint(
                kind="supplier_capacity", ref=sup.supplier_id,
                detail=f"外协 {sup.supplier_id} 该单共同产能 {sup.order_capacity}，"
                       f"增 1 件可降本",
                current_min=current_min, relaxed_min=r.minimum_cost,
            ))

    for line in lines:
        if line.line_id not in best.opened_lines:
            continue
        relaxed_lines = [
            LineCost(l.line_id, l.product, l.burden,
                     ZERO if l.line_id == line.line_id else l.opening_fee)
            for l in lines
        ]
        r = optimize_order(order, relaxed_lines, suppliers, _with_blockings=False)
        if r.feasible and r.minimum_cost < current_min:
            found.append(BlockingConstraint(
                kind="opening_fee", ref=line.line_id,
                detail=f"开线 {line.line_id}（{line.product}）固定开线费 "
                       f"{line.opening_fee}，免除可降本",
                current_min=current_min, relaxed_min=r.minimum_cost,
            ))
    return tuple(found)


def optimize_without_qualification(
    order: OrderSpec,
    lines: list[LineCost],
    suppliers: list[Supplier],
    supplier_id: str,
    product: str,
) -> OptimizationResult:
    """撤掉某外协对某品种的资格后重新求最优（题面“撤掉外协 M 资格”）。"""
    adjusted: list[Supplier] = []
    for sup in suppliers:
        if sup.supplier_id != supplier_id:
            adjusted.append(sup)
            continue
        prices = {p: price for p, price in sup.prices.items() if p != product}
        adjusted.append(Supplier(supplier_id, prices, sup.order_capacity,
                                 sup.period_capacity))
    return optimize_order(order, lines, adjusted)
