"""遮幕规划：题面四组拍数断言 + 独立 oracle 双向对照 + 策略树可执行性。"""

from __future__ import annotations

import unittest

from scoreboard import MAX_BEATS
from scoreboard.controller import decide
from scoreboard.core import Seen, Stop
from scoreboard.planner import (
    Action,
    apply_advance,
    apply_read,
    initial_belief,
    plan,
)
from scoreboard.reconcile import reconcile
from scoreboard.scenarios import (
    scenario_a_certain_11,
    scenario_b_uncertain_away,
    scenario_c_one_beat_curtain,
    scenario_d_wraparound,
    scenario_e_breaker_group,
)

from oracle import brute_min_beats


class TestHeadlineBeats(unittest.TestCase):
    def test_a_two_beats_issuable_for_22(self):
        s = scenario_a_certain_11()
        result = reconcile(list(s.records))
        decision = decide(result, s.target, s.curtain_beats)
        self.assertTrue(decision.issuable)
        self.assertEqual(decision.plan.worst_beats, 2)

    def test_b_two_beats_cannot_guarantee_but_three_reads_away_first(self):
        s = scenario_b_uncertain_away()
        result = reconcile(list(s.records))

        two = plan(result.stops, s.target, horizon=2)
        self.assertFalse(two.issuable(2))
        decision = decide(result, s.target, curtain_beats=2)
        self.assertFalse(decision.issuable)

        three = plan(result.stops, s.target, horizon=3)
        self.assertTrue(three.issuable(3))
        self.assertEqual(three.worst_beats, 3)
        # 第一拍先认清客鼓，再按读数决定补不补那一格
        first = three.root
        self.assertEqual(first.action, Action("read", "B"))
        for digit, child in first.branches.items():
            if digit == 1:  # 客鼓真在 1：两鼓都差一格，先走哪只都可
                self.assertEqual(child.action.kind, "advance")
                self.assertIn(child.action.drum, ("A", "B"))
            else:  # 客鼓真在 2：只差主鼓一格
                self.assertEqual(child.action, Action("advance", "A"))

    def test_c_one_beat_curtain_refuses_even_though_certain(self):
        s = scenario_c_one_beat_curtain()
        result = reconcile(list(s.records))
        decision = decide(result, s.target, s.curtain_beats)
        self.assertFalse(decision.issuable)
        # 放宽回两拍立即恢复可发
        self.assertTrue(decide(result, s.target, 2).issuable)

    def test_d_wraparound_9_to_0_is_one_step_each(self):
        s = scenario_d_wraparound()
        result = reconcile(list(s.records))
        self.assertEqual(result.stop_keys(), [(9, 9)])
        decision = decide(result, s.target, s.curtain_beats)
        self.assertTrue(decision.issuable)
        self.assertEqual(decision.plan.worst_beats, 2)
        # 直接验证走格函数：9 走一格是 0，不是 -1
        self.assertEqual(apply_advance(initial_belief(result), "A"),
                         frozenset({(0, 9)}))

    def test_e_breaker_group_plannable_within_six_beats(self):
        s = scenario_e_breaker_group()
        result = reconcile(list(s.records))
        decision = decide(result, s.target, s.curtain_beats)
        self.assertTrue(decision.issuable)
        self.assertLessEqual(decision.plan.worst_beats, MAX_BEATS)


class TestOracleCrossCheck(unittest.TestCase):
    def test_planner_matches_brute_force_on_all_scenario_beliefs(self):
        for builder in (scenario_a_certain_11,
                        scenario_b_uncertain_away,
                        scenario_d_wraparound,
                        scenario_e_breaker_group):
            s = builder()
            result = reconcile(list(s.records))
            belief = initial_belief(result)
            target = s.target.key()
            for horizon in range(0, MAX_BEATS + 1):
                with self.subTest(scenario=s.name, horizon=horizon):
                    got = plan(result.stops, s.target, horizon)
                    brute = brute_min_beats(belief, target, horizon)
                    if brute is None:
                        self.assertFalse(got.feasible)
                    else:
                        self.assertTrue(got.feasible)
                        self.assertEqual(got.worst_beats, brute)

    def test_oracle_rejects_unreachable_in_horizon(self):
        # 从 0 到 9 只向前转需要 9 格，六拍遮幕内无策略
        result = reconcile([
            Seen(0, "A", 0), Seen(1, "B", 0),
        ])
        self.assertIsNone(brute_min_beats(frozenset({(0, 0)}), (9, 9),
                                          MAX_BEATS))
        self.assertFalse(plan(result.stops, Stop.of(9, 9), MAX_BEATS).feasible)


class TestBeliefUpdatesAndPolicyExecution(unittest.TestCase):
    def test_advance_moves_every_world_forward(self):
        belief = frozenset({(1, 1), (1, 2)})
        self.assertEqual(apply_advance(belief, "B"), frozenset({(1, 2), (1, 3)}))

    def test_read_partitions_belief(self):
        belief = frozenset({(1, 1), (1, 2)})
        self.assertEqual(apply_read(belief, "B", 1), frozenset({(1, 1)}))
        self.assertEqual(apply_read(belief, "B", 2), frozenset({(1, 2)}))
        self.assertEqual(apply_read(belief, "B", 9), frozenset())

    def test_policy_tree_reaches_target_under_every_truth(self):
        # 用策略树对每个可能真相手推一遍，最终必须落在目标
        s = scenario_b_uncertain_away()
        result = reconcile(list(s.records))
        p = plan(result.stops, s.target, 3)
        for stop in result.stops:
            digits = {d: stop.get(d) for d in ("A", "B")}
            node = p.root
            beats = 0
            while not node.done:
                action = node.action
                if action.kind == "advance":
                    digits[action.drum] = (digits[action.drum] + 1) % 10
                    node = node.next
                else:
                    node = node.branches[digits[action.drum]]
                beats += 1
            self.assertEqual(digits, {"A": 2, "B": 2})
            self.assertLessEqual(beats, 3)


if __name__ == "__main__":
    unittest.main()
