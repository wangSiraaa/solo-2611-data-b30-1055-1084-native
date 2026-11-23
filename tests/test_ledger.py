"""实付账服务测试：子树就近归集、取消只留实付、换版拒绝沿旧树。"""

from __future__ import annotations

import unittest
from fractions import Fraction as F

from orgbudget.audit import audit_costflow
from orgbudget.errors import (
    PaymentConflict,
    TableVersionConflict,
    UnpostablePayment,
)
from orgbudget.ledger import LedgerService
from orgbudget.org_service import OrgService
from tests.support import ServiceTestCase


class LedgerTreeTest(ServiceTestCase):
    def _build(self) -> int:
        # HQ -> {X, Y}；X 下有叶 X1/X2，Y 下有叶 Y1。中心 X、Y 各占一棵子树。
        for dept, parent in [
            ("HQ", None), ("X", "HQ"), ("Y", "HQ"),
            ("X1", "X"), ("X2", "X"), ("Y1", "Y"),
        ]:
            self.org.create_department(dept, parent)
        version = self.org.publish("两中心树").version
        self.ledger = LedgerService(self.tmp / "ledger.db", self.org)
        self.ledger.register_center("X", version, "X")
        self.ledger.register_center("Y", version, "Y")
        self.ledger.bind_budget_version(version)
        return version

    def test_payments_collect_to_nearest_center_once(self) -> None:
        self._build()
        self.ledger.register_payment("P1", "X1", 6)
        self.ledger.register_payment("P2", "X2", 4)
        self.ledger.register_payment("P3", "Y1", 6)
        direct = self.ledger.collect_direct_costs()
        self.assertEqual(direct["X"].amount, F(10))
        self.assertEqual(direct["X"].payment_count, 2)
        self.assertEqual(direct["Y"].amount, F(6))

    def test_payment_on_center_itself_collects_there(self) -> None:
        self._build()
        self.ledger.register_payment("PC", "X", 3)
        self.ledger.register_payment("PL", "X1", 2)
        direct = self.ledger.collect_direct_costs()
        self.assertEqual(direct["X"].amount, F(5))

    def test_cancel_keeps_only_actual_paid(self) -> None:
        self._build()
        self.ledger.register_partial_payment("PP", "X1", amount=10, paid=4)
        self.ledger.register_payment("PF", "Y1", 6)
        cancelled = self.ledger.cancel_payment("PP")
        self.assertTrue(cancelled.cancelled)
        self.assertEqual(cancelled.actual_paid, 4)
        direct = self.ledger.collect_direct_costs()
        self.assertEqual(direct["X"].amount, F(4))   # 余款 6 不计
        self.assertEqual(direct["Y"].amount, F(6))

    def test_payment_idempotency_conflict(self) -> None:
        self._build()
        self.ledger.register_payment("P1", "X1", 6)
        # 同号同内容重试：幂等返回。
        again = self.ledger.register_payment("P1", "X1", 6)
        self.assertEqual(again.actual_paid, 6)
        with self.assertRaises(PaymentConflict):
            self.ledger.register_payment("P1", "Y1", 6)

    def test_unpostable_when_no_center_ancestor(self) -> None:
        version = self._build()
        # 新组织版本在 HQ 下加一个无中心的叶。
        self.org.create_department("Z1", "HQ")
        self.org.publish("加 Z1")
        self.ledger.register_payment("PZ", "Z1", 5)
        # 预算仍绑旧版：Z1 不在旧树，无法归集。
        with self.assertRaises(UnpostablePayment):
            self.ledger.collect_direct_costs()
        self.assertEqual(self.ledger.bound_version(), version)

    def test_rejected_revision_keeps_old_tree_for_posting(self) -> None:
        """分部迁移但预算换版拒绝：账仍按预算所用旧树归集。"""
        version = self._build()
        # 迁移 X2 到 Y 下并发布新产品，但预算仓不换版（拒绝场景）。
        self.org.move_subtree("X2", "Y")
        self.org.publish("X2 迁到 Y")
        # 账绑定的仍是旧预算版本。
        self.assertEqual(self.ledger.bound_version(), version)
        self.ledger.register_payment("PM", "X2", 8)
        direct = self.ledger.collect_direct_costs()
        # 旧树里 X2 属于 X 子树，仍归 X，不按新树归 Y。
        self.assertEqual(direct["X"].amount, F(8))
        self.assertEqual(direct["Y"].amount, F(0))

    def test_distribution_version_chain_and_conflict(self) -> None:
        self._build()
        v1 = self.ledger.submit_distribution("X", {"Y": F(1, 2), "M": F(1, 2)})
        self.assertEqual(v1, 1)
        v2 = self.ledger.submit_distribution(
            "X", {"Y": F(1, 3), "M": F(2, 3)}, expected_version=1
        )
        self.assertEqual(v2, 2)
        with self.assertRaises(TableVersionConflict):
            self.ledger.submit_distribution(
                "X", {"Y": F(1, 4), "M": F(3, 4)}, expected_version=1
            )
        current = self.ledger.current_distribution("X")
        self.assertEqual(current.shares["M"], F(2, 3))
        self.assertEqual(self.ledger.distribution_versions("X"), (1, 2))

    def test_end_to_end_xy_allocation_and_audit(self) -> None:
        self._build()
        self.ledger.register_line("M")
        self.ledger.register_line("N")
        self.ledger.register_payment("PX", "X1", 6)
        self.ledger.register_payment("PY", "Y1", 6)
        self.ledger.submit_distribution("X", {"Y": F(1, 2), "M": F(1, 2)})
        self.ledger.submit_distribution("Y", {"X": F(1, 2), "N": F(1, 2)})
        result = self.ledger.run_allocation()
        self.assertEqual(result.line_amount, {"M": F(6), "N": F(6)})

        audit = audit_costflow(
            self.ledger.db_path, self.org.db_path,
            self.ledger.bound_version(), ["M", "N"],
        )
        self.assertEqual(audit["line_amount"], {"M": F(6), "N": F(6)})
        self.assertEqual(audit["trapped"], F(0))
        self.assertGreaterEqual(len(audit["checks"]), 4)


if __name__ == "__main__":
    unittest.main()
