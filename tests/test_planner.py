"""剪序规划测试：两户分工、留芽下限改 3 后不可交付、预算联动、规模上限。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchard.errors import SolveTooLarge, Undeliverable
from orchard.model import Orchard
from orchard.planner import HouseholdOrder, Planner, execute_plan
from orchard.shares import Adoption, CommitmentSnapshot, ShareLedger
from orgbudget import BudgetService, OrgService


def _household_orchard(min_buds: int = 2) -> Orchard:
    """保留主枝根 M(2 芽)，左 L(1 芽)青梨、右 R(1 芽)黄梨。"""
    o = Orchard()
    o.add_tree("T1")
    o.add_main_branch("T1", "M", buds=2, min_buds=min_buds)
    o.add_branch("T1", "L", "M", buds=1)
    o.add_branch("T1", "R", "M", buds=1)
    o.add_string("SG", "L", row="R1", variety="GREEN", grade="A")
    o.add_string("SY", "R", row="R1", variety="YELLOW", grade="A")
    o.validate()
    return o


def _ledger() -> ShareLedger:
    """甲叶青/黄不限、实付 5+担保 5；乙叶黄、实付 5+担保 5。"""
    led = ShareLedger()
    led.adopt(
        Adoption("CJ", "CJ", "JIA", "R1", "ANY", "A"),
        CommitmentSnapshot("CJ", "JIA", paid=5, guarantee=5),
    )
    led.adopt(
        Adoption("CY", "CY", "YI", "R1", "YELLOW", "A"),
        CommitmentSnapshot("CY", "YI", paid=5, guarantee=5),
    )
    return led


class PlannerTest(unittest.TestCase):
    def test_two_households_split_left_right_keeps_two_buds(self) -> None:
        o = _household_orchard()
        led = _ledger()
        orders = [HouseholdOrder("JIA", "JIA", ("SG",)),
                  HouseholdOrder("YI", "YI", ("SY",))]
        plan = Planner(o).plan(led, orders)
        self.assertEqual(plan.cut_nodes, ("L", "R"))
        self.assertEqual(plan.remaining_buds["M"], 2)
        self.assertEqual({a.string_id: a.household_id for a in plan.assignments},
                         {"SG": "JIA", "SY": "YI"})

    def test_min_buds_three_undoable_even_with_more_money(self) -> None:
        """留芽下限改 3：至多剪一边，即便加倍实付也拿不下两串。"""
        o = _household_orchard(min_buds=3)
        led = ShareLedger()
        led.adopt(Adoption("CJ", "CJ", "JIA", "R1", "ANY", "A"),
                  CommitmentSnapshot("CJ", "JIA", paid=50, guarantee=50))
        led.adopt(Adoption("CY", "CY", "YI", "R1", "ANY", "A"),
                  CommitmentSnapshot("CY", "YI", paid=50, guarantee=50))
        orders = [HouseholdOrder("JIA", "JIA", ("SG",)),
                  HouseholdOrder("YI", "YI", ("SY",))]
        with self.assertRaises(Undeliverable):
            Planner(o).plan(led, orders)

    def test_share_shortage_prevents_two_households_one_string(self) -> None:
        """只保 1 张有效份额时，两个果串都在也没法交两户。"""
        o = _household_orchard()
        led = ShareLedger()
        # 甲只有 1 串份额（实付 5、无担保），却想领两串。
        led.adopt(Adoption("CJ", "CJ", "JIA", "R1", "ANY", "A"),
                  CommitmentSnapshot("CJ", "JIA", paid=5, guarantee=0))
        orders = [HouseholdOrder("JIA", "JIA", ("SG", "SY"))]
        with self.assertRaises(Undeliverable):
            Planner(o).plan(led, orders)

    def test_execute_plan_marks_shares_consumed(self) -> None:
        o = _household_orchard()
        led = _ledger()
        orders = [HouseholdOrder("JIA", "JIA", ("SG",)),
                  HouseholdOrder("YI", "YI", ("SY",))]
        plan = Planner(o).plan(led, orders)
        from orchard.model import HarvestSession

        session = HarvestSession(o)
        execute_plan(plan, session, led)
        self.assertEqual(session.remaining_buds()["M"], 2)
        # 已交付的两串各自核销了一条份额；其余份额仍可用。
        consumed = {s.consumed_string for s in led.shares_of("CJ") if s.is_consumed}
        self.assertEqual(consumed, {"SG"})
        self.assertEqual({s.consumed_string for s in led.shares_of("CY") if s.is_consumed},
                         {"SY"})

    def test_tie_on_buds_prefers_fewer_unowned(self) -> None:
        """分枝 X(0 芽)：一刀剪 X 与两刀 L,R 失芽相同（各 2）；
        但剪 X 会多带走 X 上无人要的 SU，故应选两刀 L,R。"""
        o = Orchard()
        o.add_tree("T1")
        o.add_main_branch("T1", "M", buds=2, min_buds=0)
        o.add_branch("T1", "X", "M", buds=0)
        o.add_branch("T1", "L", "X", buds=1)
        o.add_branch("T1", "R", "X", buds=1)
        o.add_string("SU", "X", row="R1", variety="ANY", grade="A")   # 无人要
        o.add_string("SG", "L", row="R1", variety="GREEN", grade="A")
        o.add_string("SY", "R", row="R1", variety="YELLOW", grade="A")
        o.validate()
        led = _ledger()
        orders = [HouseholdOrder("JIA", "JIA", ("SG",)),
                  HouseholdOrder("YI", "YI", ("SY",))]
        plan = Planner(o).plan(led, orders)
        self.assertEqual(plan.buds_lost, 2)
        self.assertEqual(plan.cut_nodes, ("L", "R"))
        self.assertEqual(plan.unowned_strings, ())

    def test_size_limit_rejects_large_orchard(self) -> None:
        o = Orchard()
        o.add_tree("T1")
        o.add_main_branch("T1", "M", buds=20, min_buds=0)
        for i in range(20):  # 超过 15 枝节
            o.add_branch("T1", f"B{i}", "M", buds=1)
            o.add_string(f"S{i}", f"B{i}", row="R", variety="ANY", grade="A")
        led = ShareLedger()
        led.adopt(Adoption("C", "C", "A", "R", "ANY", "A"),
                  CommitmentSnapshot("C", "A", paid=500, guarantee=0))
        with self.assertRaises(SolveTooLarge):
            Planner(o).plan(led, [HouseholdOrder("A", "A", ("S0",))])


class BudgetLinkedScenarioTest(unittest.TestCase):
    """把份额发行接到真实预算承诺，核对逐户交付的完整链路。"""

    def test_first_scenario_via_transfer(self) -> None:
        """甲青/黄不限、实付 5 + 担保 5，截止前让同分部乙一份；
        甲取青（实付），乙取黄（受让的担保份额），各剪一边保 2 芽。"""
        import tempfile, pathlib

        with tempfile.TemporaryDirectory() as d:
            org = OrgService(pathlib.Path(d) / "org.db")
            org.create_department("HQ", None)
            org.create_department("DIV", "HQ")
            org.create_department("JIA", "DIV")
            org.create_department("YI", "DIV")
            product = org.publish("v1")

            led = ShareLedger()
            shares = led.adopt(
                Adoption("CJ", "CJ", "JIA", "R1", "ANY", "A"),
                CommitmentSnapshot("CJ", "JIA", paid=5, guarantee=5),
            )
            paid = next(s for s in shares if s.kind == "PAID")
            guarantee = next(s for s in shares if s.kind == "GUARANTEE")
            # 让给同分部乙一份（担保那份），树行/品种/级别随份额走。
            moved = led.transfer(guarantee.share_id, "YI", product=product)
            self.assertEqual(moved.dept_id, "YI")
            self.assertEqual((moved.row, moved.variety, moved.grade),
                             ("R1", "ANY", "A"))

            o = _household_orchard(2)
            orders = [HouseholdOrder("JIA", "JIA", ("SG",)),
                      HouseholdOrder("YI", "YI", ("SY",))]
            plan = Planner(o).plan(led, orders)
            self.assertEqual(plan.cut_nodes, ("L", "R"))
            self.assertEqual(plan.remaining_buds["M"], 2)
            # 甲用实付份额取青，乙用受让的担保份额取黄。
            by_string = {a.string_id: a for a in plan.assignments}
            self.assertEqual(by_string["SG"].share_id, paid.share_id)
            self.assertEqual(by_string["SY"].share_id, guarantee.share_id)

    def test_paid_and_guarantee_flow_through_budget(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            org = OrgService(tmp / "org.db")
            budget = BudgetService(tmp / "budget.db", org)
            org.create_department("HQ", None)
            org.create_department("DIV", "HQ")
            org.create_department("JIA", "DIV")
            org.create_department("YI", "DIV")
            product = org.publish("v1")
            weights = {(dep, kid): 1 for dep in product.department_ids
                       for kid in product.children(dep)}
            budget.register_root_budget(product.version, 4_000, weights)

            # 甲：实付 5、有效未付担保 5；乙：实付 5、有效未付担保 5。
            budget.create_commitment("CJ", "JIA", 10)
            budget.register_spend("CJ", 5, "SJ")
            budget.create_commitment("CY", "YI", 10)
            budget.register_spend("CY", 5, "SY")

            led = ShareLedger()
            led.adopt(Adoption("CJ", "CJ", "JIA", "R1", "ANY", "A"),
                      CommitmentSnapshot.from_commitment(budget.get_commitment("CJ")))
            led.adopt(Adoption("CY", "CY", "YI", "R1", "YELLOW", "A"),
                      CommitmentSnapshot.from_commitment(budget.get_commitment("CY")))
            # 每户 2 条份额（实付 + 担保）。
            self.assertEqual(len(led.shares_of("CJ")), 2)
            self.assertEqual(len(led.shares_of("CY")), 2)

            o = _household_orchard()
            orders = [HouseholdOrder("JIA", "JIA", ("SG",)),
                      HouseholdOrder("YI", "YI", ("SY",))]
            plan = Planner(o).plan(led, orders)
            self.assertEqual(plan.remaining_buds["M"], 2)

    def test_cancel_releases_guarantee_but_keeps_paid(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            org = OrgService(tmp / "org.db")
            budget = BudgetService(tmp / "budget.db", org)
            org.create_department("HQ", None)
            org.create_department("DIV", "HQ")
            org.create_department("JIA", "DIV")
            org.create_department("YI", "DIV")
            product = org.publish("v1")
            weights = {(dep, kid): 1 for dep in product.department_ids
                       for kid in product.children(dep)}
            budget.register_root_budget(product.version, 4_000, weights)
            budget.create_commitment("CJ", "JIA", 10)
            budget.register_spend("CJ", 5, "SJ")

            led = ShareLedger()
            ad = Adoption("CJ", "CJ", "JIA", "R1", "ANY", "A")
            led.adopt(ad, CommitmentSnapshot.from_commitment(budget.get_commitment("CJ")))
            # 取消：实付 5 仍买 1 串；未剪担保 5 不再有效。
            budget.cancel_commitment("CJ")
            led.revoke_guarantees("CJ")
            led.adopt(ad, CommitmentSnapshot.from_commitment(budget.get_commitment("CJ")))
            avail = led.available_shares("JIA")
            self.assertEqual([s.kind for s in avail], ["PAID"])

            # 两个果串都在树上，但只剩 1 串份额 -> 交不了两户/两串。
            o = _household_orchard()
            with self.assertRaises(Undeliverable):
                Planner(o).plan(led, [HouseholdOrder("JIA", "JIA", ("SG", "SY"))])


if __name__ == "__main__":
    unittest.main()
