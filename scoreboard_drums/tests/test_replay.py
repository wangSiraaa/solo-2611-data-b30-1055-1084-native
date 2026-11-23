"""独立回放现场记录：每个可能止位一份，含证据见证、逐拍动作与终态核对。"""

from __future__ import annotations

import json
import unittest

from scoreboard.replay import replay_all, replay_stop
from scoreboard.reconcile import reconcile
from scoreboard.scenarios import (
    ALL_SCENARIOS,
    conflicting_records,
    scenario_b_uncertain_away,
)


class TestReplayRecords(unittest.TestCase):
    def test_every_scenario_emits_one_record_per_possible_stop(self):
        for builder in ALL_SCENARIOS:
            s = builder()
            record = replay_all(list(s.records), s.target, s.curtain_beats)
            with self.subTest(scenario=s.name):
                result = reconcile(list(s.records))
                self.assertTrue(record["feasible"])
                self.assertEqual(len(record["replays"]), len(result.stops))
                issuable = record["decision"]["issuable"]
                for replay in record["replays"]:
                    # 证据时间线覆盖全部输入记录
                    self.assertEqual(
                        [e["seq"] for e in replay["evidence"]["timeline"]],
                        sorted(r.seq for r in s.records))
                    # 每拍只做一件事，且动作合法
                    for step in replay["execution"]:
                        self.assertIn(step["action"], ("read", "advance"))
                    self.assertTrue(replay["reached_target"])
                    if issuable:
                        # 可发情形：六拍规划出的策略实际拍数不超本次时限
                        self.assertTrue(replay["within_curtain"])
                if not issuable:
                    # 留幕情形：至少有一条真相的最优拍数超本次时限，
                    # 现场记录如实呈现各真相拍数，不伪造可发
                    self.assertTrue(any(
                        rp["beats_used"] > s.curtain_beats
                        for rp in record["replays"]))

    def test_uncertain_case_replays_both_truths_independently(self):
        s = scenario_b_uncertain_away()
        result = reconcile(list(s.records))
        record = replay_all(list(s.records), s.target, 3)
        stops = {tuple(r["stop"]) for r in record["replays"]}
        self.assertEqual(stops, {(1, 1), (1, 2)})
        # 两份记录对失回执脉冲 seq=4 的真相相反，且各自自洽
        by_stop = {tuple(r["stop"]): r for r in record["replays"]}
        self.assertFalse(by_stop[(1, 1)]["evidence"]["pulse_truth"]["4"])
        self.assertTrue(by_stop[(1, 2)]["evidence"]["pulse_truth"]["4"])
        # 记录可 JSON 归档
        json.dumps(record, ensure_ascii=False)

    def test_single_replay_uses_full_belief_not_oracle_truth(self):
        # 回放器拿到的止位集合必须是完整集合，规划不偷看是哪条真相
        s = scenario_b_uncertain_away()
        result = reconcile(list(s.records))
        replay = replay_stop(result, result.stops[0], s.target, 3)
        self.assertTrue(replay["reached_target"])
        self.assertGreaterEqual(replay["beats_used"], 1)

    def test_conflict_record_documented_without_replays(self):
        record = replay_all(conflicting_records(), target=None, curtain_beats=6)
        self.assertFalse(record["feasible"])
        self.assertEqual(record["replays"], [])
        self.assertIn("conflict", record)
        self.assertTrue(record["conflict"]["records"])


if __name__ == "__main__":
    unittest.main()
