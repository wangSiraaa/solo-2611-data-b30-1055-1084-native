"""成本联算纯函数测试：互供环、精确分数、空转圈与困圈、逐来源走向。"""

from __future__ import annotations

import unittest
from fractions import Fraction as F

from orgbudget.costflow import Distribution, solve_allocation, unit_burden
from orgbudget.errors import DistributionError, NetworkError

HALF = F(1, 2)


def xy_distributions() -> list[Distribution]:
    return [
        Distribution("X", {"Y": HALF, "M": HALF}),
        Distribution("Y", {"X": HALF, "N": HALF}),
    ]


class WorkedExampleTest(unittest.TestCase):
    """题面 X/Y 互供例与两次拒签前的成本变化。"""

    def test_basic_xy_lines_are_six_each(self) -> None:
        result = solve_allocation(
            xy_distributions(), {"X": F(6), "Y": F(6)}, ["M", "N"]
        )
        self.assertEqual(result.center_throughput, {"X": F(12), "Y": F(12)})
        self.assertEqual(result.line_amount, {"M": F(6), "N": F(6)})
        # 逐来源：X 的 6 经环后 4 进 M、2 进 N；Y 对称。
        self.assertEqual(result.line_by_source["X"], {"M": F(4), "N": F(2)})
        self.assertEqual(result.line_by_source["Y"], {"M": F(2), "N": F(4)})
        self.assertEqual(result.traps, ())
        self.assertEqual(result.closed_components, ())

    def test_xy_edge_level_lineage_is_reviewable(self) -> None:
        result = solve_allocation(
            xy_distributions(), {"X": F(6), "Y": F(6)}, ["M", "N"]
        )
        trace_x = result.source_traces["X"]
        # X 注一单位：q_X=4/3、q_Y=2/3；X->Y 载 2/3，Y->X 回载 1/3。
        self.assertEqual(trace_x.edge_flow_share[("X", "Y")], F(2, 3))
        self.assertEqual(trace_x.edge_flow_share[("Y", "X")], F(1, 3))
        # 乘以注入额 6：回载金额 2，X->Y 金额 4。
        self.assertEqual(trace_x.edge_flow_amount[("Y", "X")], F(2))
        self.assertEqual(trace_x.edge_flow_amount[("X", "Y")], F(4))
        self.assertEqual(
            sum(trace_x.line_share.values()) + trace_x.trap_share, F(1)
        )

    def test_extra_three_on_x_gives_eight_and_seven(self) -> None:
        result = solve_allocation(
            xy_distributions(), {"X": F(9), "Y": F(6)}, ["M", "N"]
        )
        self.assertEqual(result.line_amount, {"M": F(8), "N": F(7)})
        self.assertEqual(sum(result.line_amount.values()), F(15))

    def test_unit_burden_standard_output_one(self) -> None:
        result = solve_allocation(
            xy_distributions(), {"X": F(6), "Y": F(6)}, ["M", "N"]
        )
        self.assertEqual(unit_burden(result.line_amount["M"], F(1)), F(6))
        with self.assertRaises(NetworkError):
            unit_burden(result.line_amount["M"], F(0))


class DistributionValidationTest(unittest.TestCase):
    def test_shares_must_sum_to_one(self) -> None:
        with self.assertRaises(DistributionError):
            Distribution("A", {"B": F(1, 3), "L": F(1, 3)})

    def test_shares_must_be_positive(self) -> None:
        with self.assertRaises(DistributionError):
            Distribution("A", {"B": F(0), "L": F(1)})

    def test_empty_table_rejected(self) -> None:
        with self.assertRaises(DistributionError):
            Distribution("A", {})

    def test_unknown_target_rejected(self) -> None:
        with self.assertRaises(NetworkError):
            solve_allocation(
                [Distribution("A", {"GHOST": F(1)})], {"A": F(1)}, ["L"]
            )

    def test_direct_cost_requires_table(self) -> None:
        with self.assertRaises(NetworkError):
            solve_allocation(
                [Distribution("A", {"L": F(1)})], {"A": F(1), "Z": F(2)}, ["L"]
            )


class CycleSemanticsTest(unittest.TestCase):
    def test_empty_cycle_does_not_pollute_lines(self) -> None:
        """无钱流入的纯内部空圈：从零传递，所有线金额不变、无困圈报告。"""
        distributions = xy_distributions() + [
            Distribution("P", {"Q": F(1)}),
            Distribution("Q", {"P": F(1)}),
        ]
        result = solve_allocation(
            distributions, {"X": F(6), "Y": F(6)}, ["M", "N"]
        )
        self.assertEqual(result.line_amount, {"M": F(6), "N": F(6)})
        self.assertEqual(result.center_throughput["P"], F(0))
        self.assertEqual(result.center_throughput["Q"], F(0))
        self.assertEqual(result.traps, ())  # 空圈不产生任何困钱
        self.assertIn(("P", "Q"), result.closed_components)

    def test_positive_injection_trapped_in_closed_cycle(self) -> None:
        """正成本注入完全内部打转的圈：无法归线，按来源指出困钱。"""
        distributions = [
            Distribution("A", {"B": F(1)}),
            Distribution("B", {"A": F(1)}),
        ]
        result = solve_allocation(distributions, {"A": F(5)}, ["L"])
        self.assertEqual(result.line_amount, {"L": F(0)})
        self.assertEqual(len(result.traps), 1)
        trap = result.traps[0]
        self.assertEqual(trap.amount, F(5))
        self.assertEqual(trap.sources, {"A": F(5)})
        self.assertEqual(trap.component, ("A", "B"))

    def test_external_into_closed_cycle_trapped_with_source(self) -> None:
        """外部注入被吸入闭圈：困钱来源仍可逐笔指认。"""
        distributions = [
            Distribution("C", {"A": F(1)}),
            Distribution("A", {"B": F(1)}),
            Distribution("B", {"A": F(1)}),
        ]
        result = solve_allocation(distributions, {"C": F(5)}, ["M"])
        self.assertEqual(result.line_amount, {"M": F(0)})
        self.assertEqual(result.traps[0].amount, F(5))
        self.assertEqual(result.traps[0].sources, {"C": F(5)})
        self.assertEqual(set(result.traps[0].component), {"A", "B"})

    def test_partial_leak_then_trap_split_by_fraction(self) -> None:
        """闭圈前有分流：一部分归线、一部分被困，分数精确。"""
        distributions = [
            Distribution("S", {"L": F(1, 3), "A": F(2, 3)}),
            Distribution("A", {"B": F(1)}),
            Distribution("B", {"A": F(1)}),
        ]
        result = solve_allocation(distributions, {"S": F(9)}, ["L"])
        self.assertEqual(result.line_amount, {"L": F(3)})
        self.assertEqual(result.traps[0].amount, F(6))
        self.assertEqual(result.traps[0].sources, {"S": F(6)})

    def test_self_loop_closed_node_traps(self) -> None:
        distributions = [Distribution("A", {"A": F(1)})]
        result = solve_allocation(distributions, {"A": F(2)}, ["L"])
        self.assertEqual(result.traps[0].amount, F(2))
        self.assertEqual(result.line_amount, {"L": F(0)})


class ExactFractionTest(unittest.TestCase):
    def test_three_way_reciprocal_uses_fractions(self) -> None:
        third = F(1, 3)
        distributions = [
            Distribution("A", {"B": third, "C": third, "L1": third}),
            Distribution("B", {"A": third, "C": third, "L2": third}),
            Distribution("C", {"A": third, "B": third, "L1": third}),
        ]
        result = solve_allocation(
            distributions, {"A": F(1), "B": F(1), "C": F(1)}, ["L1", "L2"]
        )
        # 全局守恒，且结果不是浮点近似。
        self.assertEqual(
            sum(result.line_amount.values()) + result.total_trapped(), F(3)
        )
        for amount in result.line_amount.values():
            self.assertIsInstance(amount, F)
        # 线 L1 收 A 与 C 的 1/3 吞吐量；对称结构 q_A=q_B=q_C=3。
        self.assertEqual(result.center_throughput["A"], F(3))
        self.assertEqual(result.line_amount["L1"], F(2))
        self.assertEqual(result.line_amount["L2"], F(1))

    def test_zero_cost_center_only_relays(self) -> None:
        distributions = [
            Distribution("A", {"B": F(1)}),
            Distribution("B", {"L": F(1)}),
        ]
        result = solve_allocation(
            distributions, {"A": F(8)}, ["L"]
        )
        self.assertEqual(result.line_amount, {"L": F(8)})
        self.assertEqual(result.center_throughput["B"], F(8))
        self.assertNotIn("B", result.source_traces)  # 零成本不产生来源行


if __name__ == "__main__":
    unittest.main()
