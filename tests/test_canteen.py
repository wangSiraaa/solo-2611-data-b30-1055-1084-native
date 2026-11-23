"""餐签与后厨场景的单元测试。

覆盖题面的每一处约束：实付面额、取消保留、加餐基金限分部、同一点不重复
支取、联合审批最小见证、余味/洗锅/时隙/备料排程、仅追加执行与交班，以及
高峰两种经营方式的正反例。
"""

from __future__ import annotations

import unittest

from canteen import (
    CLEAN,
    SHIFT,
    Cabinet,
    Charge,
    Dish,
    DishUpgrade,
    Pot,
    ReservationLedger,
    SessionRuntime,
    SessionSpec,
    Table,
    TablePlan,
    TicketBook,
    Voucher,
    VoucherBook,
    can_schedule,
    issue_voucher,
    plan_and_serve,
    plan_peak_named_tickets,
    plan_peak_shared_cabinet,
    schedule,
    trial_serve,
)
from canteen.errors import (
    CabinetExhausted,
    OrderNotFulfillable,
    SlotConflict,
    StockShortfall,
    TicketExceedsReservation,
    VoucherConflict,
    VoucherError,
)
from tests.support import ServiceTestCase

BLUE = Dish("blue_soup", residual="blue", allowed_prev=frozenset({CLEAN}),
            ingredient="blue_base")
WHITE = Dish("white_soup", residual="clean", allowed_prev=frozenset({CLEAN}),
             ingredient="white_base")
DISHES = {"blue_soup": BLUE, "white_soup": WHITE}


def two_table_spec(
    *, noon_slots=(2,), white_stock=2, blue_stock=1, slots=2, pots=None
) -> SessionSpec:
    tables = {
        "AM": Table("AM", "morning", "blue_soup", 2, (1,),
                    DishUpgrade("white_soup", 1)),
        "NOON": Table("NOON", "noon", "white_soup", 2, tuple(noon_slots)),
    }
    return SessionSpec(
        division="fair",
        slots=slots,
        pots=pots or (Pot("p1", CLEAN),),
        dishes=DISHES,
        tables=tables,
        stock={"blue_base": blue_stock, "white_base": white_stock},
    )


# ====================================================================== 面额与来源
class VoucherIssuanceTest(ServiceTestCase):
    def _setup_paid(self, paid: int, committed: int, dept_points: int = 5):
        version = self.build_tree([("HQ", None), ("D", "HQ")])
        product = self.org.get_product(version)
        self.budget.register_root_budget(
            version, dept_points, self.weights_for(product)
        )
        self.budget.create_commitment("SP-1", "D", committed)
        if paid:
            self.budget.register_spend("SP-1", paid, "S-1")

    def test_voucher_amount_is_actual_payment_not_commitment(self) -> None:
        self._setup_paid(paid=3, committed=5)
        voucher = issue_voucher(self.budget, "V1", "SP-1", division="fair")
        # 只承诺 5、实付 3：未支付的 2 点不印签。
        self.assertEqual(voucher.amount, 3)

    def test_zero_paid_commitment_cannot_issue(self) -> None:
        self._setup_paid(paid=0, committed=5)
        with self.assertRaises(VoucherError):
            issue_voucher(self.budget, "V1", "SP-1", division="fair")

    def test_cancelled_commitment_keeps_paid_part(self) -> None:
        self._setup_paid(paid=4, committed=5)
        self.budget.cancel_commitment("SP-1")
        voucher = issue_voucher(self.budget, "V1", "SP-1", division="fair")
        # 取消后已付 4 点仍在，未付的 1 点被释放且不印签。
        self.assertEqual(voucher.amount, 4)

    def test_top_up_fund_only_in_same_division(self) -> None:
        fund = Voucher("F1", "fair", 3, top_up=True)
        same = SessionSpec("fair", 2, (Pot("p1"),), DISHES, {})
        other = SessionSpec("other", 2, (Pot("p1"),), DISHES, {})
        self.assertTrue(fund.usable_in(same))
        self.assertFalse(fund.usable_in(other))


# ====================================================================== 联合审批
class JointApprovalTest(unittest.TestCase):
    def _spec(self):
        tables = {
            "A": Table("A", "morning", "blue_soup", 2, (1,)),
            "B": Table("B", "morning", "blue_soup", 2, (2,)),
        }
        return SessionSpec("fair", 2, (Pot("p1", CLEAN),), {"blue_soup": BLUE}, tables)

    def test_two_tables_sharing_one_shift_point_detected(self) -> None:
        spec = self._spec()
        # 一张限班点，只覆盖 morning 班，面额正好只够一桌。
        shared = Voucher("SH-1", "fair", 2, kind=SHIFT, target="morning")
        book = VoucherBook([shared])
        # 分开各自够。
        self.assertTrue(book.can_approve(spec, [TablePlan("A", "blue_soup", 2)]))
        self.assertTrue(book.can_approve(spec, [TablePlan("B", "blue_soup", 2)]))
        # 合起来都争同一张限班点 -> 失败，最小见证为两桌。
        with self.assertRaises(VoucherConflict) as ctx:
            book.approve_together(
                spec,
                [TablePlan("A", "blue_soup", 2), TablePlan("B", "blue_soup", 2)],
            )
        self.assertIn("A", str(ctx.exception))
        self.assertIn("B", str(ctx.exception))

    def test_general_and_limited_points_pooled_once(self) -> None:
        spec = self._spec()
        book = VoucherBook([
            Voucher("G1", "fair", 2),
            Voucher("SH-1", "fair", 2, kind=SHIFT, target="morning"),
        ])
        approval = book.approve_together(
            spec, [TablePlan("A", "blue_soup", 2), TablePlan("B", "blue_soup", 2)]
        )
        # 每个点只被一桌扣一次：两桌合计恰 4 点，且来源互不重复。
        sources = [vid for a in approval.allocations for vid, _ in a.parts]
        self.assertEqual(len(sources), len(set(sources)))
        self.assertEqual(sum(a.total for a in approval.allocations), 4)

    def test_table_scoped_voucher_only_pays_its_table(self) -> None:
        spec = self._spec()
        book = VoucherBook([Voucher("T-A", "fair", 2, kind="table", target="A")])
        self.assertTrue(book.can_approve(spec, [TablePlan("A", "blue_soup", 2)]))
        self.assertFalse(book.can_approve(spec, [TablePlan("B", "blue_soup", 2)]))

    def test_insufficient_points_rejected(self) -> None:
        spec = self._spec()
        book = VoucherBook([Voucher("G1", "fair", 3)])
        with self.assertRaises(VoucherConflict):
            book.approve_together(
                spec, [TablePlan("A", "blue_soup", 2), TablePlan("B", "blue_soup", 2)]
            )


# ====================================================================== 排程
class KitchenScheduleTest(unittest.TestCase):
    def test_white_consecutive_needs_no_wash(self) -> None:
        spec = two_table_spec()
        plans = (TablePlan("AM", "white_soup", 3), TablePlan("NOON", "white_soup", 2))
        order = schedule(spec, plans)
        self.assertEqual(order.meals["AM"][0], 1)
        self.assertEqual(order.meals["NOON"][0], 2)
        self.assertEqual(len(order.washes()), 0)
        self.assertEqual(order.ingredients_used, {"white_base": 2})

    def test_blue_then_white_needs_wash_and_misses_slot(self) -> None:
        # AM 必须槽1蓝；NOON 必须槽2白；只有一口锅，蓝后留蓝，白不接蓝。
        spec = two_table_spec()
        plans = (TablePlan("AM", "blue_soup", 2), TablePlan("NOON", "white_soup", 2))
        self.assertFalse(can_schedule(spec, plans))
        with self.assertRaises(OrderNotFulfillable):
            schedule(spec, plans)

    def test_relaxing_noon_slot_revives_blue_wash_white(self) -> None:
        spec = two_table_spec(noon_slots=(2, 3), slots=3)
        plans = (TablePlan("AM", "blue_soup", 2), TablePlan("NOON", "white_soup", 2))
        order = schedule(spec, plans)
        # 槽1蓝、槽2洗、槽3白。
        self.assertEqual(order.meals["AM"], (1, "p1", "blue_soup"))
        self.assertEqual(order.meals["NOON"], (3, "p1", "white_soup"))
        self.assertEqual([a.slot for a in order.washes()], [2])

    def test_white_stock_of_one_cannot_serve_two_white(self) -> None:
        spec = two_table_spec(white_stock=1)
        plans = (TablePlan("AM", "white_soup", 3), TablePlan("NOON", "white_soup", 2))
        with self.assertRaises(StockShortfall):
            schedule(spec, plans)

    def test_slot_not_acceptable_is_infeasible(self) -> None:
        spec = two_table_spec()
        # AM 只能槽1，但槽1给 NOON 用了（NOON 只接受槽2 -> 也不行）。
        plans = (TablePlan("AM", "blue_soup", 2), TablePlan("NOON", "blue_soup", 2))
        self.assertFalse(can_schedule(spec, plans))

    def test_dish_cannot_follow_forbidden_flavor(self) -> None:
        # 白汤不允许接蓝味：一口锅先蓝后白，中间不洗必失败。
        strict_white = Dish("white_soup", residual="clean",
                            allowed_prev=frozenset({CLEAN}), ingredient="white_base")
        tables = {
            "X": Table("X", "s", "blue_soup", 2, (1,)),
            "Y": Table("Y", "s", "white_soup", 2, (2,)),
        }
        spec = SessionSpec("fair", 2, (Pot("p1", CLEAN),),
                           {"blue_soup": BLUE, "white_soup": strict_white}, tables,
                           {"blue_base": 1, "white_base": 1})
        self.assertFalse(can_schedule(spec, [TablePlan("X", "blue_soup", 2),
                                             TablePlan("Y", "white_soup", 2)]))


# ====================================================================== 执行与交班
class ExecutionTest(unittest.TestCase):
    def test_charges_are_append_only_and_handover_keeps_them(self) -> None:
        spec = two_table_spec()
        plans = (TablePlan("AM", "white_soup", 3), TablePlan("NOON", "white_soup", 2))
        order = schedule(spec, plans)
        charges = (Charge("AM", "G1", 3), Charge("NOON", "G1", 2))
        rt = SessionRuntime(spec, plans, order, charges).execute()
        self.assertEqual(rt.points_used, 5)
        self.assertEqual(len(rt.served), 2)
        h = rt.handover()
        self.assertEqual(h.served_count, 2)
        self.assertEqual(h.points_used, 5)
        self.assertEqual(h.tables_served(), ("AM", "NOON"))
        # 事件只增不改：再次读取仍是同样的事实。
        self.assertEqual(len(rt.point_draws), 2)

    def test_cannot_overwrite_served_table_next_shift(self) -> None:
        spec = two_table_spec()
        plans = (TablePlan("AM", "white_soup", 3), TablePlan("NOON", "white_soup", 2))
        order = schedule(spec, plans)
        charges = (Charge("AM", "G1", 3), Charge("NOON", "G1", 2))
        rt = SessionRuntime(spec, plans, order, charges).execute()
        h = rt.handover()
        # 下一班试图重做 AM：已端出的桌不能被新菜单覆盖。
        with self.assertRaises(SlotConflict):
            SessionRuntime(
                spec, (TablePlan("AM", "blue_soup", 2),), order, (Charge("AM", "G2", 2),),
                prior_served=h.tables_served(),
            ).execute()

    def test_ingredient_shortfall_detected_at_execution(self) -> None:
        # 备料只够 1 份白，却排 2 份（绕过排程预检直接执行）。
        spec = two_table_spec(white_stock=2)
        plans = (TablePlan("AM", "white_soup", 3), TablePlan("NOON", "white_soup", 2))
        order = schedule(spec, plans)
        tight = SessionSpec(
            division=spec.division, slots=spec.slots, pots=spec.pots,
            dishes=spec.dishes, tables=spec.tables,
            stock={"blue_base": 1, "white_base": 1},
        )
        charges = (Charge("AM", "G1", 3), Charge("NOON", "G1", 2))
        with self.assertRaises(StockShortfall):
            SessionRuntime(tight, plans, order, charges).execute()

    def test_free_trial_serves_without_points(self) -> None:
        spec = two_table_spec()
        plans = (TablePlan("AM", "white_soup", 3), TablePlan("NOON", "white_soup", 2))
        plan, rt = trial_serve(spec, plans)
        self.assertEqual(rt.points_used, 0)
        self.assertEqual(len(rt.served), 2)
        self.assertEqual(plan.charges, ())


# ====================================================================== 端到端
class EndToEndTest(unittest.TestCase):
    def test_plan_and_serve_white_upgrade(self) -> None:
        spec = two_table_spec()
        book = VoucherBook([Voucher("G1", "fair", 5)])
        plans = (TablePlan("AM", "white_soup", 3), TablePlan("NOON", "white_soup", 2))
        plan, rt = plan_and_serve(spec, plans, book)
        self.assertEqual(rt.points_used, 5)
        self.assertEqual(rt.ingredients_consumed(), {"white_base": 2})
        self.assertEqual(rt.served[0].dish, "white_soup")
        # 批准里给出了每桌的点数来路。
        self.assertEqual(plan.approval.for_table("AM").total, 3)
        self.assertEqual(plan.approval.for_table("NOON").total, 2)

    def test_promised_fifth_point_keeps_am_on_blue(self) -> None:
        # 实付只有 4，第五点停在承诺：AM 升级白要 3 + NOON 2 = 5 > 4，只能点蓝。
        book = VoucherBook([Voucher("G1", "fair", 4)])
        strict = two_table_spec()  # NOON 只能槽2
        with self.assertRaises(VoucherConflict):
            plan_and_serve(
                strict,
                (TablePlan("AM", "white_soup", 3), TablePlan("NOON", "white_soup", 2)),
                book,
            )
        # 改点 AM 蓝 + NOON 白合计 4 点，钱够；但槽1蓝后留蓝、NOON 必须槽2，
        # 来不及洗锅 -> 菜单一起付得起却排不出。
        with self.assertRaises(OrderNotFulfillable):
            plan_and_serve(
                strict,
                (TablePlan("AM", "blue_soup", 2), TablePlan("NOON", "white_soup", 2)),
                book,
            )

    def test_relaxing_slot_revives_blue_wash_white(self) -> None:
        # 去掉“NOON 必须在第二格”的限制后，蓝、洗、白可复活。
        book = VoucherBook([Voucher("G1", "fair", 4)])
        relaxed = two_table_spec(noon_slots=(2, 3), slots=3)
        plan, rt = plan_and_serve(
            relaxed,
            (TablePlan("AM", "blue_soup", 2), TablePlan("NOON", "white_soup", 2)),
            book,
        )
        self.assertEqual(rt.points_used, 4)
        self.assertEqual([(a.slot, a.kind) for a in rt.order.actions],
                         [(1, "cook"), (2, "wash"), (3, "cook")])


# ====================================================================== 高峰：具名联
class NamedTicketPeakTest(unittest.TestCase):
    def _spec(self):
        tables = {
            "T1": Table("T1", "s", "blue_soup", 2, (1,)),
            "T2": Table("T2", "s", "blue_soup", 2, (2,)),
        }
        return SessionSpec("fair", 2, (Pot("p1", CLEAN),), {"blue_soup": BLUE}, tables)

    def test_two_full_tickets_exceed_one_point_pool(self) -> None:
        # 两桌各封“可提 1 点”的联，全场只有 1 点：保留额 2 > 1。
        ledger = ReservationLedger({"T1": 1, "T2": 1}, total_limit=1)
        book = TicketBook(ledger)
        book.open_ticket("TK1", "T1", [("blue_soup", 1)])
        with self.assertRaises(TicketExceedsReservation):
            book.open_ticket("TK2", "T2", [("blue_soup", 1)])
        # 全场只保留了 1 点，只盖了口一张半的联 -> 另一桌没有保障。
        self.assertEqual(ledger.total_reserved(), 1)

    def test_two_full_tickets_exceed_reservation_of_peak(self) -> None:
        # 题面反例的端到端版本：全场 1 点，两桌各要 1 点加价号。
        spec = self._spec()
        book = VoucherBook([Voucher("G1", "fair", 1)])
        ledger = ReservationLedger({"T1": 1, "T2": 1}, total_limit=1)
        tickets = TicketBook(ledger)
        tickets.open_ticket("TK1", "T1", [("blue_soup", 1)])
        result = plan_peak_named_tickets(
            spec,
            (TablePlan("T1", "blue_soup", 2), TablePlan("T2", "blue_soup", 2)),
            book, tickets, {"T1": "TK1", "T2": "TK2"},
        )
        self.assertFalse(result.feasible)

    def test_named_ticket_convenient_path(self) -> None:
        # 两桌都有名额、都有白汤备料：照联分锅顺利办成。
        tables = {
            "T1": Table("T1", "s", "blue_soup", 2, (1,), DishUpgrade("white_soup", 1)),
            "T2": Table("T2", "s", "blue_soup", 2, (2,), DishUpgrade("white_soup", 1)),
        }
        spec = SessionSpec("fair", 2, (Pot("p1", CLEAN), Pot("p2", CLEAN),), DISHES, tables,
                           {"blue_base": 2, "white_base": 2})
        book = VoucherBook([Voucher("G1", "fair", 6)])
        ledger = ReservationLedger({"T1": 3, "T2": 3}, total_limit=6)
        tickets = TicketBook(ledger)
        tickets.open_ticket("TK1", "T1", [("blue_soup", 2), ("white_soup", 3)])
        tickets.open_ticket("TK2", "T2", [("blue_soup", 2), ("white_soup", 3)])
        result = plan_peak_named_tickets(
            spec,
            (TablePlan("T1", "white_soup", 3), TablePlan("T2", "white_soup", 3)),
            book, tickets, {"T1": "TK1", "T2": "TK2"},
        )
        self.assertTrue(result.feasible, result.reason)
        self.assertEqual(result.runtime.points_used, 6)

    def test_return_then_reallocate(self) -> None:
        ledger = ReservationLedger({"T1": 1, "T2": 1}, total_limit=1)
        book = TicketBook(ledger)
        book.open_ticket("TK1", "T1", [("blue_soup", 1)])
        # 先退回 T1 的联，保留额释放后才能给 T2。
        book.return_ticket("TK1")
        book.open_ticket("TK2", "T2", [("blue_soup", 1)])
        self.assertEqual(ledger.total_reserved(), 1)

    def test_use_ticket_settles_actual_points(self) -> None:
        ledger = ReservationLedger({"T1": 3}, total_limit=3)
        book = TicketBook(ledger)
        book.open_ticket("TK1", "T1", [("blue_soup", 2), ("white_soup", 3)])
        self.assertEqual(ledger.reserved("T1"), 3)  # 按最高可扣额保留
        pts, dish = book.use_ticket("TK1", "blue_soup", "T1")
        self.assertEqual((pts, dish), (2, "blue_soup"))
        self.assertEqual(ledger.reserved("T1"), 0)


# ====================================================================== 高峰：托管柜
class SharedCabinetPeakTest(unittest.TestCase):
    def _spec_and_order(self):
        tables = {
            "T1": Table("T1", "s", "blue_soup", 2, (1,), DishUpgrade("white_soup", 1)),
            "T2": Table("T2", "s", "blue_soup", 2, (2,), DishUpgrade("white_soup", 1)),
        }
        spec = SessionSpec("fair", 2, (Pot("p1", CLEAN), Pot("p2", CLEAN)),
                           DISHES, tables, {"blue_base": 1, "white_base": 1})
        # 开柜后才知道哪桌缺货：T2 临时缺蓝底，改点白汤。
        plans = (TablePlan("T1", "blue_soup", 2), TablePlan("T2", "white_soup", 3))
        order = schedule(spec, plans)
        return spec, plans, order

    def test_one_top_up_point_issued_to_shortage_table(self) -> None:
        spec, plans, order = self._spec_and_order()
        cabinet = Cabinet(1)
        result = plan_peak_shared_cabinet(
            spec, plans, order, cabinet,
            arrival_tick={"T1": 0, "T2": 0},
            base_covered={"T1": 2, "T2": 2},
        )
        self.assertTrue(result.feasible, result.reason)
        # 唯一个加价号发给了缺货的 T2。
        tokens = cabinet.tokens()
        self.assertEqual(len(tokens), 1)
        self.assertEqual(tokens[0].table_id, "T2")
        self.assertEqual(tokens[0].points, 1)

    def test_blocked_issuing_window_cannot_guarantee_both(self) -> None:
        spec, plans, order = self._spec_and_order()
        cabinet = Cabinet(1)
        # T2 在拍 1 才知道缺货，槽 2 就下锅：中间没有可发号的一拍。
        result = plan_peak_shared_cabinet(
            spec, plans, order, cabinet,
            arrival_tick={"T1": 0, "T2": 1},
            base_covered={"T1": 2, "T2": 2},
        )
        self.assertFalse(result.feasible)
        self.assertIn("没有可发号的拍", result.reason)

    def test_cabinet_one_token_per_tick(self) -> None:
        cabinet = Cabinet(5)
        cabinet.issue(0, "T1", "blue_soup", 2)
        with self.assertRaises(CabinetExhausted):
            cabinet.issue(0, "T2", "white_soup", 1)
        # 下一拍可以继续发。
        token = cabinet.issue(1, "T2", "white_soup", 1)
        self.assertEqual(token.points, 1)


# ====================================================================== 上限规模
class ScaleLimitTest(unittest.TestCase):
    def test_ten_tables_three_pots_twelve_slots(self) -> None:
        # 最多 10 桌、三锅、十二时隙。让前三桌都必须在槽 1，
        # 逼出“同格多锅并行”，其余各占一个时隙。
        dishes = {"soup": Dish("soup", residual="clean",
                               allowed_prev=frozenset({CLEAN}))}
        tables = {
            f"T{i:02d}": Table(f"T{i:02d}", f"s{i}", "soup", 2,
                               (1,) if i < 3 else (i + 1,))
            for i in range(10)
        }
        spec = SessionSpec("fair", 12, tuple(Pot(f"p{j}", CLEAN) for j in range(3)),
                           dishes, tables)
        plans = tuple(TablePlan(t, "soup", 2) for t in tables)
        order = schedule(spec, plans)
        self.assertEqual(len(order.cooks()), 10)
        # 槽 1 必须同时用上三口不同的锅。
        self.assertEqual(len({(a.pot_id) for a in order.actions_at(1)}), 3)
        # 任一 (锅, 格) 至多一份。
        seen = [(a.pot_id, a.slot) for a in order.cooks()]
        self.assertEqual(len(seen), len(set(seen)))


if __name__ == "__main__":
    unittest.main()
