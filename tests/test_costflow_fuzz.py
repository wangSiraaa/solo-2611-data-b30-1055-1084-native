"""随机模糊交叉核对：随机互供网络上的守恒与逐来源归线。

用与生产代码不同的视角核对：

* 全局守恒：直接成本 = 各线金额 + 困圈金额；
* 逐来源守恒：每个正注入来源的线份额 + 困圈份额 = 1；
* 吞吐量方程：每个开中心 q = 直接成本 + 全部受入；
* 多次重复求解结果完全确定（无随机状态泄漏）。
"""

from __future__ import annotations

import random
import unittest
from fractions import Fraction as F

from orgbudget.costflow import Distribution, solve_allocation


def _random_network(rng: random.Random):
    centers = [f"C{i}" for i in range(rng.randint(2, 6))]
    lines = [f"L{i}" for i in range(rng.randint(1, 4))]
    targets = centers + lines
    distributions: list[Distribution] = []
    for c in centers:
        # 随机选 1~3 个目标，给整数权重后归一化为 Fraction。
        k = rng.randint(1, min(3, len(targets)))
        chosen = rng.sample(targets, k)
        weights = [rng.randint(1, 5) for _ in chosen]
        total = sum(weights)
        shares = {t: F(w, total) for t, w in zip(chosen, weights)}
        distributions.append(Distribution(c, shares))
    direct = {
        c: F(rng.randint(0, 12)) for c in centers
    }
    return distributions, direct, lines


class FuzzConservationTest(unittest.TestCase):
    def test_1000_random_networks_conserve(self) -> None:
        rng = random.Random(20261123)
        for _ in range(1000):
            distributions, direct, lines = _random_network(rng)
            result = solve_allocation(distributions, direct, lines)

            total_in = sum(direct.values(), F(0))
            total_lines = sum(result.line_amount.values(), F(0))
            total_trapped = result.total_trapped()
            self.assertEqual(total_in, total_lines + total_trapped)

            # 逐来源份额守恒。
            for source, trace in result.source_traces.items():
                share_sum = sum(trace.line_share.values(), F(0)) + trace.trap_share
                self.assertEqual(share_sum, F(1))
                self.assertEqual(trace.injected, direct[source])
                # 金额口径与份额口径一致。
                for line, amount in trace.line_amount.items():
                    self.assertEqual(
                        amount, direct[source] * trace.line_share[line]
                    )

            # 开中心吞吐量方程：q = 直接成本 + 受入。
            share_lookup = {
                (d.source, t): f for d in distributions for t, f in d.shares.items()
            }
            closed_members = {
                m for comp in result.closed_components for m in comp
            }
            for c in result.center_throughput:
                if c in closed_members:
                    self.assertEqual(result.center_throughput[c], F(0))
                    continue
                incoming = direct.get(c, F(0))
                for (u, v), f in share_lookup.items():
                    if v == c:
                        incoming += result.center_throughput[u] * f
                self.assertEqual(result.center_throughput[c], incoming)

            # 确定性：同样输入再算一遍，结果逐字段一致。
            again = solve_allocation(distributions, direct, lines)
            self.assertEqual(again.line_amount, result.line_amount)
            self.assertEqual(
                again.center_throughput, result.center_throughput
            )
            self.assertEqual(
                tuple((t.component, t.amount, t.sources) for t in again.traps),
                tuple((t.component, t.amount, t.sources) for t in result.traps),
            )

    def test_fuzz_no_singular_on_open_networks(self) -> None:
        """保证每张网络至少一个中心能通向生产线时，不应有困圈。"""
        rng = random.Random(99)
        clean_runs = 0
        for _ in range(300):
            distributions, direct, lines = _random_network(rng)
            center_succ = {
                d.source: [t for t in d.shares if t not in lines]
                for d in distributions
            }
            # 若每个中心都能沿中心边最终走到某个带线出口的中心，则无闭圈。
            def reaches_line(start: str) -> bool:
                seen: set[str] = set()
                stack = [start]
                while stack:
                    cur = stack.pop()
                    if cur in seen:
                        continue
                    seen.add(cur)
                    table = next(d for d in distributions if d.source == cur)
                    if any(t in lines for t in table.shares):
                        return True
                    stack.extend(center_succ[cur])
                return False

            if all(reaches_line(c) for c in center_succ):
                result = solve_allocation(distributions, direct, lines)
                self.assertEqual(result.traps, ())
                self.assertEqual(result.closed_components, ())
                clean_runs += 1
        self.assertGreater(clean_runs, 50)


if __name__ == "__main__":
    unittest.main()
