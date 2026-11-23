"""测试共用工具。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orgbudget.budget_service import BudgetService
from orgbudget.equipment_service import EquipmentService
from orgbudget.org_service import OrgService


class ServiceTestCase(unittest.TestCase):
    """每个用例使用独立临时目录，互不污染。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.org = OrgService(self.tmp / "org.db", busy_timeout_ms=30_000)
        self.budget = BudgetService(
            self.tmp / "budget.db", self.org, busy_timeout_ms=30_000
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build_tree(self, edges: list[tuple[str, str | None]], root: str = "HQ") -> int:
        """按边建部门并发布，返回组织产品版本。"""
        created: set[str] = set()
        pending = list(edges)
        while pending:
            remaining = []
            for dept, parent in pending:
                if parent is None or parent in created:
                    self.org.create_department(dept, parent)
                    created.add(dept)
                else:
                    remaining.append((dept, parent))
            if len(remaining) == len(pending):
                raise RuntimeError(f"边存在未解析父项: {pending}")
            pending = remaining
        product = self.org.publish("initial")
        self.assertEqual(product.root, root)
        return product.version

    def weights_for(
        self, product, weights: dict[tuple[str, str], int] | None = None
    ) -> dict[tuple[str, str], int]:
        """默认每条父子边权重 1，可用 weights 覆盖。"""
        result: dict[tuple[str, str], int] = {}
        for dept in product.department_ids:
            for kid in product.children(dept):
                result[(dept, kid)] = 1
        if weights:
            result.update(weights)
        return result


class EquipmentTestCase(ServiceTestCase):
    """在组织 / 预算之上再挂设备会计中心仓。"""

    def setUp(self) -> None:
        super().setUp()
        self.equipment = EquipmentService(
            self.tmp / "equipment.db", self.budget, busy_timeout_ms=30_000
        )

    def build_workshop(self, leaf: str = "W", root_budget: int = 100_000) -> int:
        """建 HQ -> leaf 的工坊叶并登记根预算，返回组织产品版本。"""
        version = self.build_tree([("HQ", None), (leaf, "HQ")])
        product = self.org.get_product(version)
        self.budget.register_root_budget(version, root_budget, self.weights_for(product))
        return version

    def pay(
        self,
        request_no: str,
        dept: str,
        commit_amount: int,
        spend_amount: int,
        idem_key: str,
        *,
        cancel_rest: bool = False,
    ) -> None:
        """在预算仓办一笔实付：开承诺、登记支出，可选取消余款。"""
        self.budget.create_commitment(request_no, dept, commit_amount)
        self.budget.register_spend(request_no, spend_amount, idem_key)
        if cancel_rest:
            self.budget.cancel_commitment(request_no)
