"""联营会与订单员服务测试：归线报价、撤回失效、名额占用、离线复核、边界表。"""

from __future__ import annotations

import tempfile
import threading
import unittest
from fractions import Fraction as F
from pathlib import Path

from orgbudget.association import AssociationService
from orgbudget.boundary import build_boundary, combine_and_find_closed_loops
from orgbudget.costflow import Distribution
from orgbudget.errors import (
    QuoteInvalid,
    SealPriceRejected,
    SupplierCapacityTaken,
    TableVersionConflict,
)
from orgbudget.order import LineCost, OrderSpec, Supplier
from orgbudget.order_desk import OrderDesk

HALF = F(1, 2)
PERIOD = "2026-11"


class AssociationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.assoc = AssociationService(self.tmp / "assoc.db")
        for line in ("M", "N"):
            self.assoc.register_line(line)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _submit_xy(self) -> None:
        self.assoc.submit_flow("X", {"Y": HALF, "M": HALF})
        self.assoc.submit_flow("Y", {"X": HALF, "N": HALF})

    def test_quote_carries_per_source_line_table(self) -> None:
        self._submit_xy()
        quote = self.assoc.issue_quote("Q1", PERIOD, {"X": F(6), "Y": F(6)})
        self.assertEqual(quote.line_amount, {"M": F(6), "N": F(6)})
        self.assertEqual(quote.line_by_source["X"], {"M": F(4), "N": F(2)})
        self.assertEqual(quote.line_by_source["Y"], {"M": F(2), "N": F(4)})
        # 依赖边冻结为全部实际承载边。
        self.assertIn(("X", "Y"), quote.dep_edges)
        self.assertIn(("Y", "X"), quote.dep_edges)

    def test_withdrawal_invalidates_in_use_quotes(self) -> None:
        self._submit_xy()
        self.assoc.issue_quote("Q1", PERIOD, {"X": F(6), "Y": F(6)})
        invalidated = self.assoc.withdraw_flow("X", "M")
        self.assertEqual(invalidated, ("Q1",))
        quote = self.assoc.get_quote("Q1")
        self.assertEqual(quote.status, "INVALIDATED")
        self.assertIn("X", quote.invalidate_reason)
        with self.assertRaises(QuoteInvalid):
            self.assoc.require_live("Q1")
        # 撤回留痕可查。
        self.assertEqual(self.assoc.withdrawals()[0][:2], ("X", "M"))

    def test_withdrawal_tracks_dependent_quote_only(self) -> None:
        # A->L 独立报价与 X/Y 环无关，撤回 X 的边不应误伤。
        self.assoc.submit_flow("A", {"L": F(1)})
        self.assoc.register_line("L")
        self._submit_xy()
        self.assoc.issue_quote("QA", PERIOD, {"A": F(3)})
        self.assoc.issue_quote("QXY", PERIOD, {"X": F(6), "Y": F(6)})
        invalidated = self.assoc.withdraw_flow("X", "M")
        self.assertEqual(invalidated, ("QXY",))
        self.assertTrue(self.assoc.get_quote("QA").in_use)

    def test_offline_review_uses_frozen_snapshot(self) -> None:
        self._submit_xy()
        self.assoc.issue_quote("Q1", PERIOD, {"X": F(6), "Y": F(6)})
        # 承兑后重报分送表：旧报价快照不变，仍可离线复核。
        self.assoc.mark_accepted("Q1", "PO-1")
        self.assoc.submit_flow(
            "X", {"Y": F(1, 3), "M": F(2, 3)}, expected_version=1
        )
        old = self.assoc.get_quote("Q1")
        self.assertEqual(old.status, "ACCEPTED")
        self.assertEqual(old.table_versions, {"X": 1, "Y": 1})
        self.assertEqual(old.line_amount, {"M": F(6), "N": F(6)})

    def test_stale_flow_submission_rejected(self) -> None:
        self._submit_xy()
        # X 当前版本为 1；基于过期版本 0 重报必须拒绝。
        with self.assertRaises(TableVersionConflict):
            self.assoc.submit_flow(
                "X", {"Y": F(1, 3), "M": F(2, 3)}, expected_version=0
            )
        # 带正确基准版本的重报产生下一版。
        v2 = self.assoc.submit_flow(
            "X", {"Y": F(1, 3), "M": F(2, 3)}, expected_version=1
        )
        self.assertEqual(v2, 2)

    def test_cross_center_closed_loop_detection(self) -> None:
        self.assoc.submit_flow("P", {"Q": F(1)})
        self.assoc.submit_flow("Q", {"P": F(1)})
        loops = self.assoc.cross_center_loops()
        self.assertEqual(loops, (("P", "Q"),))

    def test_trapped_quote_flags_unassignable_source(self) -> None:
        self.assoc.submit_flow("C", {"A": F(1)})
        self.assoc.submit_flow("A", {"B": F(1)})
        self.assoc.submit_flow("B", {"A": F(1)})
        quote = self.assoc.issue_quote("QT", PERIOD, {"C": F(5)})
        self.assertEqual(quote.line_amount, {"M": F(0), "N": F(0)})
        # 困圈信息随报价快照对外可指认。
        payload_quote = self.assoc.get_quote("QT")
        self.assertEqual(payload_quote.status, "ISSUED")


class OrderDeskTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.assoc = AssociationService(self.tmp / "assoc.db")
        self.assoc.register_line("M")
        self.assoc.register_line("N")
        self.assoc.submit_flow("X", {"Y": HALF, "M": HALF})
        self.assoc.submit_flow("Y", {"X": HALF, "N": HALF})
        self.desk = OrderDesk(self.tmp / "desk.db", self.assoc)
        self.desk.register_supplier(
            "BAN-1", PERIOD, {"M": F(7), "N": F(9)},
            order_capacity=1, period_capacity=1,
        )
        self.line_product = {"M": "M", "N": "N"}
        self.opening = {"M": F(2), "N": F(0)}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _quote(self, costs: dict[str, F], qid: str = "Q1") -> str:
        self.assoc.issue_quote(qid, PERIOD, costs)
        return qid

    def _order(self, seal: F = F(22), oid: str = "PO-1") -> OrderSpec:
        return OrderSpec(oid, {"M": 2, "N": 1}, 2, seal)

    def test_sign_minimum_plan_under_seal(self) -> None:
        qid = self._quote({"X": F(6), "Y": F(6)})
        signed = self.desk.sign(
            self._order(), qid, self.line_product, PERIOD,
            opening_fees=self.opening,
        )
        self.assertEqual(signed.total, F(21))
        self.assertEqual(signed.plan.buy, {"BAN-1": {"M": 1}})
        # 名额被占 1 件，剩余为 0。
        self.assertEqual(self.desk.remaining_capacity("BAN-1", PERIOD), 0)
        self.assertEqual(self.assoc.get_quote(qid).status, "ACCEPTED")

    def test_sign_rejected_over_seal(self) -> None:
        qid = self._quote({"X": F(9), "Y": F(6)})  # M=8, N=7 -> 24
        with self.assertRaises(SealPriceRejected):
            self.desk.sign(
                self._order(), qid, self.line_product, PERIOD,
                opening_fees=self.opening,
            )
        # 拒签不占名额。
        self.assertEqual(self.desk.remaining_capacity("BAN-1", PERIOD), 1)
        self.assertEqual(self.desk.accepted_orders(), ())

    def test_two_orders_cannot_share_one_slot(self) -> None:
        qid = self._quote({"X": F(6), "Y": F(6)})
        first = self.desk.sign(
            self._order(oid="PO-1"), qid, self.line_product, PERIOD,
            opening_fees=self.opening,
        )
        self.assertEqual(first.total, F(21))
        # 第二单同样要用外协 1 件，但名额已被第一单占满。
        with self.assertRaises(SupplierCapacityTaken):
            self.desk.sign(
                self._order(oid="PO-2"), qid, self.line_product, PERIOD,
                opening_fees=self.opening,
            )

    def test_concurrent_signing_only_one_wins(self) -> None:
        qid = self._quote({"X": F(6), "Y": F(6)})
        results: list[str] = []
        barrier = threading.Barrier(2)

        def worker(oid: str) -> None:
            desk = OrderDesk(self.tmp / "desk.db", self.assoc)
            barrier.wait()
            try:
                desk.sign(
                    self._order(oid=oid), qid, self.line_product, PERIOD,
                    opening_fees=self.opening,
                )
                results.append(f"ok-{oid}")
            except (SupplierCapacityTaken, QuoteInvalid):
                results.append(f"reject-{oid}")

        threads = [threading.Thread(target=worker, args=(o,))
                   for o in ("PO-A", "PO-B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len([r for r in results if r.startswith("ok")]), 1)
        self.assertEqual(self.desk.remaining_capacity("BAN-1", PERIOD), 0)

    def test_sign_blocked_when_quote_invalidated(self) -> None:
        qid = self._quote({"X": F(6), "Y": F(6)})
        self.assoc.withdraw_flow("X", "M")
        with self.assertRaises(QuoteInvalid):
            self.desk.sign(
                self._order(), qid, self.line_product, PERIOD,
                opening_fees=self.opening,
            )

    def test_revoke_qualification_raises_minimum_to_twenty_three(self) -> None:
        qid = self._quote({"X": F(6), "Y": F(6)})
        result = self.desk.evaluate_without_qualification(
            self._order(), qid, self.line_product, PERIOD, "BAN-1", "M",
            opening_fees=self.opening,
        )
        self.assertEqual(result.minimum_cost, F(23))
        with self.assertRaises(SealPriceRejected):
            result.assert_signable()

    def test_revoke_persists_for_future_signing_but_keeps_old_orders(self) -> None:
        qid = self._quote({"X": F(6), "Y": F(6)})
        self.desk.sign(
            self._order(oid="PO-1"), qid, self.line_product, PERIOD,
            opening_fees=self.opening,
        )
        self.desk.revoke_qualification("BAN-1", PERIOD, "M")
        # 历史订单原样可查。
        self.assertEqual(len(self.desk.accepted_orders(PERIOD)), 1)
        # 新单在撤资格后的名册上求解：M 外协资格消失。
        self.assoc.issue_quote("Q2", PERIOD, {"X": F(6), "Y": F(6)})
        result = self.desk.evaluate(
            OrderSpec("PO-9", {"M": 1}, 1, F(100)),
            "Q2", self.line_product, PERIOD, opening_fees=self.opening,
        )
        # 只剩自制一条路：6 + 开线 2 = 8。
        self.assertEqual(result.minimum_cost, F(8))
        self.assertEqual(result.best.make, {"M": 1})


class BoundaryTest(unittest.TestCase):
    def test_workshop_boundary_unit_routes_and_loops(self) -> None:
        # 工坊 W1 = {X, Y}（互供环 + 各自出线），工坊 W2 = {C}，C->X 回流。
        distributions = [
            Distribution("X", {"Y": HALF, "M": HALF}),
            Distribution("Y", {"X": HALF, "N": HALF}),
            Distribution("C", {"X": F(1)}),
        ]
        boundary = build_boundary("W1", {"X", "Y"}, distributions, ["M", "N"])
        # 外来入口 C 进一单位：在 W1 内传递后全部落到 M/N，无困圈。
        route_c = boundary.route_of("C")
        self.assertEqual(
            route_c.line_share["M"] + route_c.line_share["N"], F(1)
        )
        self.assertEqual(route_c.trapped_share, F(0))
        # 自有成本入口同样守恒。
        for member in ("X", "Y"):
            route = boundary.route_of(member)
            self.assertTrue(route.is_own_cost)
            self.assertEqual(
                sum(route.line_share.values(), F(0)) + route.trapped_share, F(1)
            )
        # 边界表不含工坊内部互供边（X->Y、Y->X 不出现在出口里）。
        internal = {("X", "Y"), ("Y", "X")}
        for route in boundary.routes:
            self.assertFalse(set(route.exit_share) & internal)

    def test_closed_cycle_inside_workshop_reported_at_boundary(self) -> None:
        distributions = [
            Distribution("P", {"Q": F(1)}),
            Distribution("Q", {"P": F(1)}),
            Distribution("S", {"P": F(1)}),
        ]
        boundary = build_boundary("W", {"P", "Q"}, distributions, ["L"])
        route_s = boundary.route_of("S")
        self.assertEqual(route_s.trapped_share, F(1))
        self.assertEqual(route_s.trapped_components, (("P", "Q"),))
        self.assertEqual(route_s.line_share, {})

    def test_combine_boundaries_finds_cross_workshop_loop(self) -> None:
        # W1 的 A -> W2 的 B，W2 的 B -> W1 的 A，两个工坊互锁成封闭圈。
        # 各自只对本工坊成员建边界，回流通过边界出口边在工坊级缩图上可见。
        distributions = [
            Distribution("A", {"B": F(1)}),
            Distribution("B", {"A": F(1)}),
        ]
        b1 = build_boundary("W1", {"A"}, distributions, [])
        b2 = build_boundary("W2", {"B"}, distributions, [])
        # 全局闭 SCC 吞掉承载量：B 入口无出口/入线，整单位计为跨工坊滞流。
        route_b = b1.route_of("B")
        self.assertEqual(route_b.exit_share, {})
        self.assertEqual(route_b.cross_workshop_share, F(1))
        loops = combine_and_find_closed_loops([b1, b2], distributions)
        self.assertEqual(loops, (frozenset({"W1", "W2"}),))

    def test_workshop_with_real_outlet_not_a_loop(self) -> None:
        distributions = [
            Distribution("A", {"B": HALF, "L": HALF}),
            Distribution("B", {"A": F(1)}),
        ]
        b1 = build_boundary("W1", {"A", "B"}, distributions, ["L"])
        loops = combine_and_find_closed_loops([b1])
        self.assertEqual(loops, ())


if __name__ == "__main__":
    unittest.main()
