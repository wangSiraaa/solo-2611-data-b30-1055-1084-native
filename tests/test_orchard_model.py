"""果园树模型与剪枝核算测试：主枝留芽、一刀不可复原、最小反链。"""

from __future__ import annotations

import unittest

from orchard.errors import BudLimitError, InvalidOrchard, UnknownBranch
from orchard.model import HarvestSession, Orchard, evaluate_cuts, validate_cut_set


def two_lobes(min_buds: int = 2) -> Orchard:
    """题面主枝：保留根 M(2 芽)，左 L(1 芽)挂青，右 R(1 芽)挂黄。"""
    o = Orchard()
    o.add_tree("T1")
    o.add_main_branch("T1", "M", buds=2, min_buds=min_buds)
    o.add_branch("T1", "L", "M", buds=1)
    o.add_branch("T1", "R", "M", buds=1)
    o.add_string("SG", "L", row="R1", variety="GREEN", grade="A")
    o.add_string("SY", "R", row="R1", variety="YELLOW", grade="A")
    o.validate()
    return o


class OrchardModelTest(unittest.TestCase):
    def test_main_root_buds_and_subtree(self) -> None:
        o = two_lobes()
        self.assertEqual(o.total_buds("M"), 4)
        self.assertEqual(o.subtree("M"), ("M", "L", "R"))
        self.assertEqual(o.ancestors("L"), ("M",))
        # 主枝根不可剪，侧枝可剪。
        self.assertFalse(o.is_cuttable("M"))
        self.assertTrue(o.is_cuttable("L"))

    def test_evaluate_cuts_loses_subtree_buds(self) -> None:
        o = two_lobes()
        out = evaluate_cuts(o, ["L"])
        self.assertEqual(out.remaining_of("M"), 3)   # 4 - 1
        self.assertEqual([s.string_id for s in out.harvested_strings], ["SG"])

    def test_cut_is_irreversible(self) -> None:
        o = two_lobes()
        session = HarvestSession(o)
        session.cut("L")
        self.assertFalse(session.can_cut("L"))
        # 已剪的枝不能复原；再次剪同样的枝被拒。
        with self.assertRaises(BudLimitError):
            session.cut("L")

    def test_min_buds_three_allows_at_most_one_side(self) -> None:
        o = two_lobes(min_buds=3)
        session = HarvestSession(o)
        self.assertTrue(session.can_cut("L"))
        session.cut("L")
        self.assertEqual(session.remaining_buds()["M"], 3)
        # 再剪任一侧都会跌破 3 芽下限。
        self.assertFalse(session.can_cut("R"))
        with self.assertRaises(BudLimitError):
            session.cut("R")

    def test_validate_cut_set_rejects_ancestor_pairs(self) -> None:
        o = two_lobes()
        with self.assertRaises(BudLimitError):
            validate_cut_set(o, ["M"])          # 主枝根不可剪
        # L 与 R 无祖孙关系，合法。
        self.assertEqual(validate_cut_set(o, ["L", "R"]), ("L", "R"))

    def test_unknown_branch(self) -> None:
        o = two_lobes()
        with self.assertRaises(UnknownBranch):
            o.branch("Z")

    def test_unsatisfiable_min_buds_rejected_at_validate(self) -> None:
        o = Orchard()
        o.add_tree("T1")
        o.add_main_branch("T1", "M", buds=2, min_buds=9)
        o.add_branch("T1", "L", "M", buds=1)
        with self.assertRaises(InvalidOrchard):
            o.validate()

    def test_one_cut_through_fork_loses_more_buds_than_two(self) -> None:
        """一刀剪 X 带走 L,R 及其上果与 X 自身芽；两刀 L,R 反而少损芽。"""
        o = Orchard()
        o.add_tree("T1")
        o.add_main_branch("T1", "M", buds=2, min_buds=0)
        o.add_branch("T1", "X", "M", buds=2)
        o.add_branch("T1", "L", "X", buds=1)
        o.add_branch("T1", "R", "X", buds=1)
        o.add_string("SG", "L", row="R", variety="GREEN", grade="A")
        o.add_string("SY", "R", row="R", variety="YELLOW", grade="A")
        o.add_string("SU", "X", row="R", variety="YELLOW", grade="A")  # 无人要
        one = evaluate_cuts(o, ["X"])
        two = evaluate_cuts(o, ["L", "R"])
        self.assertEqual(one.buds_lost, 4)   # X(2)+L(1)+R(1)
        self.assertEqual(two.buds_lost, 2)   # L(1)+R(1)
        self.assertGreater(one.buds_lost, two.buds_lost)
        # 一刀还会多带走无人要的 SU。
        self.assertIn("SU", {s.string_id for s in one.harvested_strings})
        self.assertNotIn("SU", {s.string_id for s in two.harvested_strings})


if __name__ == "__main__":
    unittest.main()
