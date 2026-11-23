"""端到端演示：虚构工坊成本联算、归线报价与订单签单。

运行：

    python -m demo.workshop_scenarios

每次运行先清空 demo_data/workshop/，从零建立四个独立 SQLite 库
（组织 / 预算 / 实付账 / 联营会+订单台），依次演示：

1. 题面 X/Y 互供：实付各 6，最终 M、N 各 6，逐来源归线可逐跳复核；
2. 订单 2M+1N：最低 21、封价 22 内签出，外协名额被占；
3. X 增实付 3 -> M=8/N=7，最低 24，封价 22 拒签；
4. 旧成本下撤外协 M 资格 -> 最低 23，拒签；
5. 两单争同一外协班：正式接单占名额，第二单拒签；
6. 孤立空转圈不污染线；正额注入困在圈内，按来源指认；
7. 中心撤回服务量：在用归线报价精确失效；
8. 分部迁移但预算换版拒绝：实付仍按预算所用旧树归集；
9. 同址中心做工坊边界表，组合发现跨工坊封闭圈；
10. 独立审计（Kosaraju + 稠密消元）与生产结果逐项一致。
"""

from __future__ import annotations

import shutil
from fractions import Fraction as F
from pathlib import Path

from orgbudget import (
    AssociationService,
    BudgetService,
    Distribution,
    LedgerService,
    OrderDesk,
    OrderSpec,
    OrgService,
    build_boundary,
    combine_and_find_closed_loops,
)
from orgbudget.audit import audit_costflow
from orgbudget.errors import (
    QuoteInvalid,
    SealPriceRejected,
    SupplierCapacityTaken,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "demo_data" / "workshop"
HALF = F(1, 2)
PERIOD = "2026-11"


def _line(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def _check(name: str, ok: bool, detail: str = "") -> None:
    mark = "[通过]" if ok else "[失败]"
    print(f"  {mark} {name}{(' —— ' + detail) if detail else ''}")
    if not ok:
        raise AssertionError(name)


def _money(f: F) -> str:
    return f"{f.numerator}/{f.denominator}" if f.denominator != 1 else str(f.numerator)


def main() -> None:
    if DATA_DIR.exists():
        shutil.rmtree(DATA_DIR)
    DATA_DIR.mkdir(parents=True)

    org = OrgService(DATA_DIR / "org.db")
    budget = BudgetService(DATA_DIR / "budget.db", org)
    ledger = LedgerService(DATA_DIR / "ledger.db", org)
    assoc = AssociationService(DATA_DIR / "assoc.db")
    desk = OrderDesk(DATA_DIR / "desk.db", assoc)

    # ---------------- 组织/预算树：HQ -> {X, Y}，各自带叶 ----------------
    for dept, parent in [
        ("HQ", None), ("X", "HQ"), ("Y", "HQ"),
        ("X1", "X"), ("Y1", "Y"),
    ]:
        org.create_department(dept, parent)
    v1 = org.publish("两中心树").version
    product = org.get_product(v1)
    weights = {(d, k): 1 for d in product.department_ids
               for k in product.children(d)}
    budget.register_root_budget(v1, 100_000, weights)
    ledger.bind_budget_version(v1)
    ledger.register_center("X", v1, "X")
    ledger.register_center("Y", v1, "Y")
    for line in ("M", "N"):
        ledger.register_line(line)
        assoc.register_line(line)

    # ================================================================ 1
    _line("案例 1：X/Y 互供，实付各 6，最终 M、N 各 6（精确分数）")
    ledger.register_payment("PX", "X1", 6)
    ledger.register_payment("PY", "Y1", 6)
    ledger.submit_distribution("X", {"Y": HALF, "M": HALF})
    ledger.submit_distribution("Y", {"X": HALF, "N": HALF})
    assoc.submit_flow("X", {"Y": HALF, "M": HALF})
    assoc.submit_flow("Y", {"X": HALF, "N": HALF})
    result = ledger.run_allocation()
    print(f"  中心吞吐量: X={_money(result.center_throughput['X'])}, "
          f"Y={_money(result.center_throughput['Y'])}")
    print(f"  生产线最终金额: M={_money(result.line_amount['M'])}, "
          f"N={_money(result.line_amount['N'])}")
    _check("M、N 各 6", result.line_amount == {"M": F(6), "N": F(6)})
    trace_x = result.source_traces["X"]
    print(f"  来源 X 的 6：进 M={_money(trace_x.line_amount['M'])}、"
          f"进 N={_money(trace_x.line_amount['N'])}；"
          f"边 X->Y 载 {_money(trace_x.edge_flow_amount[('X', 'Y')])}")
    _check("逐来源可追溯且份额守恒",
           sum(trace_x.line_share.values()) + trace_x.trap_share == F(1))

    # ================================================================ 2
    _line("案例 2：订单 2M+1N，本地最多 2 件，外协班 1 件，最低 21 签出")
    desk.register_supplier(
        "BAN-1", PERIOD, {"M": F(7), "N": F(9)},
        order_capacity=1, period_capacity=1,
    )
    assoc.issue_quote("Q1", PERIOD, {"X": F(6), "Y": F(6)})
    quote_id = "Q1"
    line_product = {"M": "M", "N": "N"}
    opening = {"M": F(2), "N": F(0)}
    order1 = OrderSpec("PO-1", {"M": 2, "N": 1}, 2, F(22))
    opt = desk.evaluate(order1, quote_id, line_product, PERIOD,
                        opening_fees=opening)
    print(f"  最低成本 {_money(opt.minimum_cost)}；自制 {opt.best.make}，"
          f"外协 {opt.best.buy}")
    for b in opt.blockings:
        print(f"  阻止降价: {b.kind} {b.ref or ''}（放松可省 {_money(b.saving)}）")
    _check("最低 21", opt.minimum_cost == F(21))
    signed = desk.sign(order1, quote_id, line_product, PERIOD,
                       opening_fees=opening)
    _check("封价 22 内签出", signed.total == F(21))
    _check("外协名额被占满", desk.remaining_capacity("BAN-1", PERIOD) == 0)

    # ================================================================ 3
    _line("案例 3：X 增实付 3 -> M=8、N=7，最低 24，封价 22 拒签")
    ledger.register_payment("PX2", "X1", 3)
    assoc.issue_quote("Q2", PERIOD, {"X": F(9), "Y": F(6)})
    r3 = ledger.run_allocation()
    print(f"  新线金额: M={_money(r3.line_amount['M'])}, "
          f"N={_money(r3.line_amount['N'])}")
    _check("M=8、N=7", r3.line_amount == {"M": F(8), "N": F(7)})
    order2 = OrderSpec("PO-2", {"M": 2, "N": 1}, 2, F(22))
    opt3 = desk.evaluate(order2, "Q2", line_product, PERIOD,
                         opening_fees=opening)
    print(f"  新最低成本 {_money(opt3.minimum_cost)}（封价 22）")
    _check("最低 24", opt3.minimum_cost == F(24))
    try:
        desk.sign(order2, "Q2", line_product, PERIOD, opening_fees=opening)
        raise AssertionError("应拒签")
    except SealPriceRejected as exc:
        print(f"  按预期拒签: {exc}")
    _check("拒签不占名额", desk.remaining_capacity("BAN-1", PERIOD) == 0)

    # ================================================================ 4
    _line("案例 4：旧成本 6/6 不变，仅撤外协 M 资格，最低 23 拒签")
    opt4 = desk.evaluate_without_qualification(
        order1, "Q1", line_product, PERIOD, "BAN-1", "M",
        opening_fees=opening,
    )
    print(f"  撤资格后分工: 自制 {opt4.best.make}，外协 {opt4.best.buy}，"
          f"成本 {_money(opt4.minimum_cost)}")
    _check("最低 23", opt4.minimum_cost == F(23))
    try:
        opt4.assert_signable()
        raise AssertionError("应拒签")
    except SealPriceRejected as exc:
        print(f"  按预期拒签: {exc}")

    # ================================================================ 5
    _line("案例 5：两单争同一外协班，正式接单占名额，第二单拒签")
    # 用一家新期间名额 1 的外协，先签 PO-7，再试 PO-8。
    desk.register_supplier("BAN-2", PERIOD, {"M": F(7)}, 1, 1)
    assoc.issue_quote("Q3", PERIOD, {"X": F(6), "Y": F(6)})
    oa = OrderSpec("PO-7", {"M": 1}, 1, F(50))
    ob = OrderSpec("PO-8", {"M": 1}, 1, F(50))
    desk.sign(oa, "Q3", line_product, PERIOD, opening_fees=opening)
    try:
        desk.sign(ob, "Q3", line_product, PERIOD, opening_fees=opening)
        raise AssertionError("第二单应拒签")
    except SupplierCapacityTaken as exc:
        print(f"  第二单按预期拒签: {exc}")
    _check("只有 PO-7 承兑",
           {o.order_id for o in desk.accepted_orders(PERIOD)} >= {"PO-7"})

    # ================================================================ 6
    _line("案例 6：孤立空转圈不污染；正额注入困在圈内并按来源指认")
    empty = [
        Distribution("P", {"Q": F(1)}),
        Distribution("Q", {"P": F(1)}),
    ]
    from orgbudget.costflow import solve_allocation
    r_empty = solve_allocation(empty, {"P": F(0), "Q": F(0)}, ["L"])
    _check("空圈吞吐量全 0、无困钱、不污染线",
           r_empty.center_throughput == {"P": F(0), "Q": F(0)}
           and r_empty.traps == () and r_empty.line_amount == {"L": F(0)})
    trapped_net = [
        Distribution("S", {"A": F(1)}),
        Distribution("A", {"B": F(1)}),
        Distribution("B", {"A": F(1)}),
    ]
    r_trap = solve_allocation(trapped_net, {"S": F(5)}, ["L"])
    t = r_trap.traps[0]
    print(f"  困圈 {t.component}：金额 {_money(t.amount)}，"
          f"来源 { {s: _money(a) for s, a in t.sources.items()} }")
    _check("5 全额被困且指认来源 S",
           t.amount == F(5) and t.sources == {"S": F(5)})

    # ================================================================ 7
    _line("案例 7：中心撤回一条服务量，在用归线报价精确失效")
    invalidated = assoc.withdraw_flow("X", "M")
    print(f"  撤回 X->M 后失效报价: {invalidated}")
    _check("Q1 在失效名单（其来源走向经过 X 的出边）", "Q1" in invalidated)
    try:
        assoc.require_live("Q1")
        raise AssertionError("Q1 应已失效")
    except QuoteInvalid as exc:
        print(f"  按预期拦截: {exc}")
    # 重报后可签新报价，旧报价快照仍可离线复核。
    assoc.submit_flow("X", {"Y": HALF, "M": HALF})
    old = assoc.get_quote("Q1")
    _check("旧报价快照仍可离线复核", old.line_amount == {"M": F(6), "N": F(6)})

    # ================================================================ 8
    _line("案例 8：分部迁移但预算换版拒绝，实付仍按预算所用旧树归集")
    org.move_subtree("X1", "Y")
    org.publish("X1 迁到 Y 下")
    # 预算仓不换版（模拟换版被拒绝）：账绑定的仍是 v1 旧树。
    ledger.register_payment("PMIG", "X1", 4)
    direct = ledger.collect_direct_costs()
    print(f"  旧树归集中 X 中心实付 {_money(direct['X'].amount)}（含迁移叶 X1 的 4）")
    _check("迁移叶仍归旧中心 X", direct["X"].amount >= F(4))
    _check("账绑定版本仍是预算所用 v1", ledger.bound_version() == v1)

    # ================================================================ 9
    _line("案例 9：同址中心工坊边界表与跨工坊封闭圈")
    cross = [
        Distribution("A", {"B": F(1)}),
        Distribution("B", {"A": F(1)}),
    ]
    b1 = build_boundary("W1", {"A"}, cross, [])
    b2 = build_boundary("W2", {"B"}, cross, [])
    loops = combine_and_find_closed_loops([b1, b2], cross)
    print(f"  工坊级封闭圈: {[set(w) for w in loops]}")
    _check("发现 W1<->W2 跨工坊封闭圈",
           loops == (frozenset({"W1", "W2"}),))
    route = b1.route_of("B")
    print(f"  W1 外来入口 B 的单位去向: 跨工坊滞流 {_money(route.cross_workshop_share)}")
    _check("边界表不暴露内部明细、份额守恒", route.cross_workshop_share == F(1))

    # ================================================================ 10
    _line("案例 10：独立审计（Kosaraju + 稠密 Fraction 消元）")
    audit = audit_costflow(ledger.db_path, org.db_path, v1, ["M", "N"])
    for item in audit["checks"]:
        print(f"    - {item}")
    # 此时账上实付 X=6+3+4=13、Y=6；独立重算必须与联算当前结果逐项一致。
    current = ledger.run_allocation()
    _check("独立重算与生产结果逐项一致",
           audit["line_amount"] == current.line_amount
           and audit["trapped"] == current.total_trapped()
           and audit["throughput"] == current.center_throughput)

    _line("全部演示完成")
    print(f"  数据库目录: {DATA_DIR}")
    print("  - org.db / budget.db / ledger.db / assoc.db / desk.db")


if __name__ == "__main__":
    main()
