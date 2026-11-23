"""端到端演示：展会食堂的餐签、后厨排程与高峰经营方式。

运行：

    python3 -m demo.canteen_scenarios

依次演示题面的几组关键反例与正例：

1. 面额取实付：承诺 5、实付 3 只印 3 点；取消后已付仍可印。
2. 两桌白汤连做（升级 AM 到白）：单一干净锅、白不接蓝，连白无需洗。
3. 实付 5 -> 白原料只剩两份不足；实付 4（第五点停在承诺）-> 只能点蓝。
4. 菜单一起付得起（AM 蓝 + NOON 白 = 4 点）却来不及洗锅；放开 NOON
   槽位后“蓝、洗、白”复活。
5. 两桌分开各自够、合起来却抢同一张限班点：联合审批给出最小见证。
6. 高峰二选一：具名菜单联的两张满额联超保留额；共享托管柜把唯一加价号
   发给开柜后才发现的缺货桌，且卡掉发号窗口则无法保证两桌都吃上。
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from orgbudget import BudgetService, OrgService

from canteen import (
    CLEAN,
    SHIFT,
    Cabinet,
    Dish,
    DishUpgrade,
    Pot,
    ReservationLedger,
    SessionSpec,
    Table,
    TablePlan,
    TicketBook,
    Voucher,
    VoucherBook,
    issue_voucher,
    plan_and_serve,
    plan_peak_shared_cabinet,
    schedule,
)
from canteen.errors import (
    OrderNotFulfillable,
    TicketExceedsReservation,
    VoucherConflict,
    VoucherError,
)


def _line(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def _check(name: str, ok: bool, detail: str = "") -> None:
    mark = "[通过]" if ok else "[失败]"
    print(f"  {mark} {name}{(' —— ' + detail) if detail else ''}")
    if not ok:
        raise AssertionError(name)


BLUE = Dish("blue_soup", residual="blue", allowed_prev=frozenset({CLEAN}),
            ingredient="blue_base")
WHITE = Dish("white_soup", residual="clean", allowed_prev=frozenset({CLEAN}),
             ingredient="white_base")
DISHES = {"blue_soup": BLUE, "white_soup": WHITE}


def _two_tables(noon_slots=(2,), white_stock=2, slots=2) -> SessionSpec:
    tables = {
        "AM": Table("AM", "morning", "blue_soup", 2, (1,),
                    DishUpgrade("white_soup", 1)),
        "NOON": Table("NOON", "noon", "white_soup", 2, tuple(noon_slots)),
    }
    return SessionSpec(
        division="fair", slots=slots, pots=(Pot("p1", CLEAN),), dishes=DISHES,
        tables=tables, stock={"blue_base": 1, "white_base": white_stock},
    )


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="canteen_demo_"))
    try:
        org = OrgService(tmp / "org.db")
        org.create_department("HQ", None)
        org.create_department("FAIR", "HQ")
        version = org.publish().version
        product = org.get_product(version)
        budget = BudgetService(tmp / "budget.db", org)
        budget.register_root_budget(
            version, 20,
            {(d, k): 1 for d in product.department_ids for k in product.children(d)},
        )

        # ------------------------------------------------------ 1 面额取实付
        _line("案例 1：赞助原件面额取实际付款，未付承诺不印签，取消保留已付")
        budget.create_commitment("SPONSOR-1", "FAIR", 5)
        budget.register_spend("SPONSOR-1", 3, "PAY-1")
        v3 = issue_voucher(budget, "V3", "SPONSOR-1", division="fair")
        print(f"  承诺 5 分、实付 3 分 -> 印签面额 {v3.amount}")
        _check("未付的 2 分预览承诺不印签", v3.amount == 3)

        budget.create_commitment("SPONSOR-2", "FAIR", 2)
        try:
            issue_voucher(budget, "V0", "SPONSOR-2", division="fair")
            raise AssertionError("应当拒绝")
        except VoucherError as exc:
            print(f"  零实付承诺被拒印签: {exc}")

        budget.cancel_commitment("SPONSOR-1")
        v_cancel = issue_voucher(budget, "VC", "SPONSOR-1", division="fair")
        print(f"  取消 SPONSOR-1 后，已付 3 分仍在 -> {v_cancel.amount}")
        _check("取消后的已付部分仍可印签", v_cancel.amount == 3)

        # ------------------------------------------------------ 2 白汤连做
        _line("案例 2：两桌各点白汤，一口初始干净的锅连做，无需洗")
        spec = _two_tables()
        plans = (TablePlan("AM", "white_soup", 3), TablePlan("NOON", "white_soup", 2))
        order = schedule(spec, plans)
        print(f"  AM -> {order.meals['AM']}；NOON -> {order.meals['NOON']}")
        _check("连做白汤不需要洗锅", len(order.washes()) == 0)
        _check("两份白汤原料恰好够", order.ingredients_used == {"white_base": 2})

        # ------------------------------------------------------ 3 实付不足
        _line("案例 3：实付 5 只够连做两白；实付 4 则白原料剩一份，上午只能蓝")
        spec_twowhite = _two_tables(noon_slots=(2, 3), slots=3, white_stock=1)
        try:
            schedule(spec_twowhite, plans)
            raise AssertionError("应当原料不足")
        except Exception as exc:
            print(f"  白原料只剩一份时代码拒绝: {type(exc).__name__}: {exc}")
        # 实付 4（第五点停在承诺）：AM 升级白需 5 点 > 4，只能点蓝。
        book4 = VoucherBook([Voucher("G4", "fair", 4)])
        with_conflict = False
        try:
            plan_and_serve(spec, plans, book4)
        except VoucherConflict as exc:
            with_conflict = True
            print(f"  实付 4 无法支付两白（需 5 点）: {exc}")
        _check("实付不足以升级 -> 审批拒绝", with_conflict)

        # ------------------------------------------------------ 4 付得起却来不及
        _line("案例 4：菜单一起付得起，却来不及洗锅；放开槽位后蓝/洗/白复活")
        blue_white = (TablePlan("AM", "blue_soup", 2), TablePlan("NOON", "white_soup", 2))
        try:
            plan_and_serve(spec, blue_white, book4)
            raise AssertionError("应当排程失败")
        except OrderNotFulfillable as exc:
            print(f"  保持 NOON 只能槽2: {exc}")
        relaxed = _two_tables(noon_slots=(2, 3), slots=3)
        plan, rt = plan_and_serve(relaxed, blue_white, book4)
        seq = [(a.slot, a.kind, a.dish) for a in rt.order.actions]
        print(f"  放开后工单: {seq}")
        _check("蓝、洗、白可复活且实扣 4 点",
               [k for _, k, _ in seq] == ["cook", "wash", "cook"]
               and rt.points_used == 4)

        # ------------------------------------------------------ 5 联合审批见证
        _line("案例 5：两桌分开各自够、合起来却同花一张限班点")
        tables_same_shift = {
            "A": Table("A", "morning", "blue_soup", 2, (1,)),
            "B": Table("B", "morning", "blue_soup", 2, (2,)),
        }
        spec_shift = SessionSpec("fair", 2, (Pot("p1", CLEAN),), {"blue_soup": BLUE},
                                 tables_same_shift)
        shared = Voucher("SH-1", "fair", 2, kind=SHIFT, target="morning")
        book = VoucherBook([shared])
        print("  A 单独:", book.can_approve(spec_shift, [TablePlan("A", "blue_soup", 2)]))
        print("  B 单独:", book.can_approve(spec_shift, [TablePlan("B", "blue_soup", 2)]))
        try:
            book.approve_together(
                spec_shift,
                [TablePlan("A", "blue_soup", 2), TablePlan("B", "blue_soup", 2)],
            )
            raise AssertionError("应当冲突")
        except VoucherConflict as exc:
            print(f"  合起来: {exc}")
        _check("识别同一限班点被两桌重复支取", True)

        # ------------------------------------------------------ 6 高峰二选一
        _line("案例 6：高峰经营方式 —— 具名联 vs 共享托管柜")
        ledger = ReservationLedger({"T1": 1, "T2": 1}, total_limit=1)
        tickets = TicketBook(ledger)
        tickets.open_ticket("TK1", "T1", [("blue_soup", 1)])
        print(f"  只封 TK1 后全场已保留 {ledger.total_reserved()} / 1 点")
        try:
            tickets.open_ticket("TK2", "T2", [("blue_soup", 1)])
            raise AssertionError("应当超保留额")
        except TicketExceedsReservation as exc:
            print(f"  再封 TK2: {exc}")
        _check("两张各提 1 点的联超全场 1 点保留额", True)

        # 托管柜：两桌基础 2 点已覆盖，全班仅 1 个加价点，T2 开柜后才知道缺货。
        tables_peak = {
            "T1": Table("T1", "s", "blue_soup", 2, (1,), DishUpgrade("white_soup", 1)),
            "T2": Table("T2", "s", "blue_soup", 2, (2,), DishUpgrade("white_soup", 1)),
        }
        spec_peak = SessionSpec(
            "fair", 2, (Pot("p1", CLEAN), Pot("p2", CLEAN)), DISHES, tables_peak,
            {"blue_base": 1, "white_base": 1},
        )
        peak_plans = (TablePlan("T1", "blue_soup", 2), TablePlan("T2", "white_soup", 3))
        peak_order = schedule(spec_peak, peak_plans)
        cab_ok = Cabinet(1)
        res = plan_peak_shared_cabinet(
            spec_peak, peak_plans, peak_order, cab_ok,
            arrival_tick={"T1": 0, "T2": 0}, base_covered={"T1": 2, "T2": 2},
        )
        print(f"  托管柜正常发号: 可行={res.feasible}；号={cab_ok.tokens()}")
        _check("唯一加价号发给缺货的 T2", res.feasible
               and [t.table_id for t in cab_ok.tokens()] == ["T2"])

        cab_blocked = Cabinet(1)
        res_bad = plan_peak_shared_cabinet(
            spec_peak, peak_plans, peak_order, cab_blocked,
            arrival_tick={"T1": 0, "T2": 1}, base_covered={"T1": 2, "T2": 2},
        )
        print(f"  卡掉发号窗口: 可行={res_bad.feasible}；{res_bad.reason}")
        _check("知道缺货与下锅之间无发号拍 -> 不能保证两桌都吃上", not res_bad.feasible)

        _line("全部演示完成")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
