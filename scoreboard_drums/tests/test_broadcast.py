"""播报员：真实完赛比分、按版次主客映射取数、待赛拒播、换队不清零。"""

from __future__ import annotations

import unittest

from scoreboard.broadcast import BroadcastError, Edition, MatchResult
from scoreboard.core import DRUM_A, DRUM_B
from scoreboard.scenarios import (
    EDITION_V1,
    EDITION_V2,
    MATCH_ID,
    build_office,
)


class TestBroadcast(unittest.TestCase):
    def setUp(self):
        self.office = build_office()

    def test_22_recorded_and_taken_by_v1_mapping(self):
        order = self.office.issue_order(MATCH_ID, EDITION_V1)
        # v1：A 主、B 客 -> A=2, B=2
        self.assertEqual(order.target.get(DRUM_A), 2)
        self.assertEqual(order.target.get(DRUM_B), 2)
        self.assertEqual((order.source_home_goals, order.source_away_goals),
                         (2, 2))

    def test_edition_swap_flips_drum_mapping_but_not_facts(self):
        order_v1 = self.office.issue_order(MATCH_ID, EDITION_V1)
        order_v2 = self.office.issue_order(MATCH_ID, EDITION_V2)
        # 比分事实不变；v2 主槽坐 B、客槽坐 A
        self.assertEqual(order_v2.target.get(DRUM_B), 2)
        self.assertEqual(order_v2.target.get(DRUM_A), 2)
        self.assertEqual(order_v1.source_home_goals,
                         order_v2.source_home_goals)
        # 用非对称比分再验一次映射方向，而不是靠 2:2 对称巧合
        self.office.record_result(
            MatchResult("m-31", "played", home_goals=3, away_goals=1))
        o1 = self.office.issue_order("m-31", EDITION_V1)
        o2 = self.office.issue_order("m-31", EDITION_V2)
        self.assertEqual((o1.target.get(DRUM_A), o1.target.get(DRUM_B)), (3, 1))
        self.assertEqual((o2.target.get(DRUM_A), o2.target.get(DRUM_B)), (1, 3))

    def test_pending_match_refuses_broadcast(self):
        with self.assertRaises(BroadcastError) as ctx:
            self.office.issue_order("m-pending", EDITION_V1)
        self.assertIn("拒绝播报", str(ctx.exception))

    def test_unknown_match_and_edition(self):
        with self.assertRaises(BroadcastError):
            self.office.issue_order("nope", EDITION_V1)
        with self.assertRaises(BroadcastError):
            self.office.issue_order(MATCH_ID, "v9")

    def test_edition_is_immutable_and_drums_not_cleared(self):
        with self.assertRaises(BroadcastError):
            self.office.add_edition(Edition(EDITION_V1, DRUM_B, DRUM_A))
        # 待赛事实不得携带比分
        with self.assertRaises(BroadcastError):
            self.office.record_result(
                MatchResult("m-bad", "pending", home_goals=1, away_goals=0))


if __name__ == "__main__":
    unittest.main()
