"""订单员最低成本分工测试：题面 21 / 24 / 23 三个数与封价、名额规则。"""

from __future__ import annotations

import unittest
from fractions import Fraction as F

from orgbudget.errors import InfeasibleOrder, SealPriceRejected
from orgbudget.order import (
    LineCost,
    OrderSpec,
    Supplier,
    optimize_order,
    optimize_without_qualification,
)


def lines(m_amount: F, n_amount: F) -> list[LineCost]:
    # 各线每期标准产量 1，故单件负担即线金额；M 开线费 2，N 开线费 0。
    return [
        LineCost("LINE-M", "M", m_amount, F(2)),
        LineCost("LINE-N", "N", n_amount, F(0)),
    ]


def order(seal: F = F(22)) -> OrderSpec:
    # 2 件 M、1 件 N，本地最多自制 2 件。
    return OrderSpec("PO-1", {"M": 2, "N": 1}, local_capacity=2, seal_price=seal)


def supplier(m_price: F = F(7), n_price: F = F(9)) -> Supplier:
    prices = {"M": m_price}
    if n_price is not None:
        prices["N"] = n_price
    return Supplier("BAN-1", prices, order_capacity=1, period_capacity=1)


class WorkedOrderTest(unittest.TestCase):
    def test_minimum_twenty_one_plan(self) -> None:
        result = optimize_order(order(), lines(F(6), F(6)), [supplier()])
        self.assertTrue(result.feasible)
        self.assertEqual(result.minimum_cost, F(21))
        plan = result.best
        # 自制一 M 一 N、外协一 M：6 + 6 + 开线 2 + 外协 7 = 21。
        self.assertEqual(plan.make, {"M": 1, "N": 1})
        self.assertEqual(plan.buy, {"BAN-1": {"M": 1}})
        self.assertEqual(plan.self_cost, F(12))
        self.assertEqual(plan.opening_cost, F(2))
        self.assertEqual(plan.subcontract_cost, F(7))
        result.assert_signable()  # 封价 22 内可签

    def test_seal_price_rejects_when_too_low(self) -> None:
        result = optimize_order(
            OrderSpec("PO-1", {"M": 2, "N": 1}, 2, F(20)),
            lines(F(6), F(6)), [supplier()],
        )
        with self.assertRaises(SealPriceRejected):
            result.assert_signable()

    def test_cost_change_to_eight_seven_gives_twenty_four(self) -> None:
        # X 增一笔实付 3 后 M=8、N=7：自制 M/N + 外协 M = 8+7+2+7 = 24。
        result = optimize_order(order(), lines(F(8), F(7)), [supplier()])
        self.assertEqual(result.minimum_cost, F(24))
        with self.assertRaises(SealPriceRejected):
            result.assert_signable()  # 封价 22，拒签

    def test_revoke_m_qualification_gives_twenty_three(self) -> None:
        # 旧成本 6/6 不变，仅撤掉外协 M 资格：外协只能做 N（9），
        # 自制 2M+1N 受本地 2 件限制不可行；最优为自制 2M、外协 N。
        result = optimize_without_qualification(
            order(), lines(F(6), F(6)), [supplier()], "BAN-1", "M"
        )
        self.assertTrue(result.feasible)
        self.assertEqual(result.minimum_cost, F(23))
        self.assertEqual(result.best.make, {"M": 2})
        self.assertEqual(result.best.buy, {"BAN-1": {"N": 1}})
        with self.assertRaises(SealPriceRejected):
            result.assert_signable()

    def test_blocking_constraints_name_real_causes(self) -> None:
        result = optimize_order(order(), lines(F(6), F(6)), [supplier()])
        kinds = {b.kind: b for b in result.blockings}
        # 本地 2 件上限：放松到 3 件可全自制，省掉外协 7 与开线费差异。
        self.assertIn("local_capacity", kinds)
        # 外协共同产能 1：放松可多外发（本例自制更便宜，未必降本，按实算）。
        for b in result.blockings:
            self.assertGreater(b.saving, F(0))
            self.assertEqual(b.current_min, F(21))
        # M 开线费真实存在且进入最优方案，免除应能降 2。
        fee = next(b for b in result.blockings if b.kind == "opening_fee")
        self.assertEqual(fee.ref, "LINE-M")
        self.assertEqual(fee.saving, F(2))

    def test_no_blocking_when_constraint_not_binding(self) -> None:
        # 本地产能放宽到 10：全部自制，外协额度与本地产能都不阻止降价。
        big_local = OrderSpec("PO-2", {"M": 2, "N": 1}, 10, F(100))
        result = optimize_order(big_local, lines(F(6), F(6)), [supplier()])
        self.assertEqual(result.best.make, {"M": 2, "N": 1})
        self.assertNotIn(
            "local_capacity", {b.kind for b in result.blockings}
        )

    def test_infeasible_when_capacity_insufficient(self) -> None:
        tight = OrderSpec("PO-3", {"M": 5}, local_capacity=2, seal_price=F(100))
        result = optimize_order(tight, lines(F(6), F(6)), [supplier()])
        self.assertFalse(result.feasible)
        with self.assertRaises(InfeasibleOrder):
            result.minimum_cost

    def test_product_nobody_can_make(self) -> None:
        weird = OrderSpec("PO-4", {"Z": 1}, 2, F(100))
        result = optimize_order(weird, lines(F(6), F(6)), [supplier()])
        self.assertFalse(result.feasible)
        self.assertIn("Z", result.reason)

    def test_opening_fee_charged_once_per_product(self) -> None:
        # 自制 2 件 M 只收一次开线费。
        spec = OrderSpec("PO-5", {"M": 2}, local_capacity=2, seal_price=F(100))
        result = optimize_order(spec, lines(F(6), F(6)), [supplier(F(100), None)])
        self.assertEqual(result.best.opening_cost, F(2))
        self.assertEqual(result.best.self_cost, F(12))
        self.assertEqual(result.minimum_cost, F(14))

    def test_multiple_suppliers_common_capacity(self) -> None:
        sups = [
            Supplier("S1", {"M": F(7)}, order_capacity=1),
            Supplier("S2", {"M": F(5)}, order_capacity=1),
        ]
        spec = OrderSpec("PO-6", {"M": 3}, local_capacity=1, seal_price=F(100))
        result = optimize_order(spec, lines(F(6), F(6)), sups)
        # 自制 1 件（6+开线2），外发 2 件优先便宜的 S2=5、再 S1=7 -> 20。
        self.assertEqual(result.minimum_cost, F(6 + 2 + 5 + 7))
        self.assertEqual(result.best.buy.get("S2"), {"M": 1})

    def test_deterministic_tie_break(self) -> None:
        # 两家同价：方案按字典序确定，重复求解结果一致。
        sups = [
            Supplier("S1", {"M": F(7)}, 1),
            Supplier("S2", {"M": F(7)}, 1),
        ]
        r1 = optimize_order(order(), lines(F(6), F(6)), sups)
        r2 = optimize_order(order(), lines(F(6), F(6)), sups)
        self.assertEqual(r1.best.signature(), r2.best.signature())


if __name__ == "__main__":
    unittest.main()
