"""两种现场交付：电工席操作卡（离线数据）与馆内控制器（止位/重算/交班）。"""

from __future__ import annotations

import json
import unittest

from scoreboard.cards import CardExecutor, OperationCard
from scoreboard.controller import DrumHardware, SiteController, decide
from scoreboard.core import DRUM_A, DRUM_B, Stop
from scoreboard.planner import plan
from scoreboard.reconcile import reconcile
from scoreboard.scenarios import (
    EDITION_V1,
    MATCH_ID,
    build_office,
    scenario_b_uncertain_away,
    scenario_c_one_beat_curtain,
    scenario_e_breaker_group,
)


class TestOperationCard(unittest.TestCase):
    def setUp(self):
        self.s = scenario_b_uncertain_away()
        self.result = reconcile(list(self.s.records))
        self.order = build_office().issue_order(MATCH_ID, EDITION_V1)
        self.plan3 = plan(self.result.stops, self.s.target, 3)

    def test_card_refused_when_curtain_too_short(self):
        with self.assertRaises(ValueError):
            OperationCard.build(self.order, self.plan3, curtain_beats=2)

    def test_card_roundtrip_and_offline_execution_for_every_truth(self):
        card = OperationCard.build(self.order, self.plan3, curtain_beats=3)
        # 落盘即纯数据；现场从 JSON 重建，不连电工席
        rebuilt = OperationCard.from_json(card.to_json())
        self.assertEqual(rebuilt.header, card.header)
        for stop in self.result.stops:
            hardware = DrumHardware(stop)
            report = CardExecutor(rebuilt, hardware).run(self.order)
            self.assertEqual(hardware.snapshot(), {DRUM_A: 2, DRUM_B: 2})
            self.assertLessEqual(report["beats_used"], 3)

    def test_wrong_card_header_rejected(self):
        card = OperationCard.build(self.order, self.plan3, 3)
        other_order = build_office().issue_order(MATCH_ID, EDITION_V1)
        # 篡改卡头目标模拟拿错卡
        bad = OperationCard(
            {**card.header, "target": [9, 9]}, list(card.pages.values()),
            card.root_page)
        hardware = DrumHardware(self.result.stops[0])
        with self.assertRaises(ValueError):
            CardExecutor(bad, hardware).run(other_order)


class TestSiteController(unittest.TestCase):
    def test_controller_runs_every_stop_and_accounts_beats(self):
        s = scenario_e_breaker_group()
        result = reconcile(list(s.records))
        decision = decide(result, s.target, s.curtain_beats)
        self.assertTrue(decision.issuable)
        for stop in result.stops:
            hardware = DrumHardware(stop)
            controller = SiteController(result.stops, s.target, s.curtain_beats)
            handover = controller.run(hardware)
            self.assertTrue(handover["remaining_beats_worst"] == 0)
            self.assertEqual(hardware.snapshot(), s.target.digits)
            self.assertLessEqual(handover["beats_used"], s.curtain_beats)
            # 剩余止位已收敛到唯一的真实终态
            self.assertEqual(handover["remaining_stops"],
                             [list(s.target.key())])

    def test_handover_snapshot_roundtrip_keeps_spent_beats(self):
        s = scenario_b_uncertain_away()
        result = reconcile(list(s.records))
        hardware = DrumHardware(result.stops[0])
        controller = SiteController(result.stops, s.target, 3)
        controller.step(hardware)  # 第一拍读客鼓后交班
        snapshot = json.loads(controller.to_json(hardware))
        self.assertEqual(snapshot["beats_used"], 1)

        successor = SiteController.from_handover(snapshot)
        self.assertEqual(successor.beats_used, 1)
        self.assertEqual(len(successor.actions_done), 1)
        successor.run(hardware)
        self.assertEqual(hardware.snapshot(), {DRUM_A: 2, DRUM_B: 2})
        # 已花拍数延续，不会被新班次清零
        self.assertGreaterEqual(successor.beats_used, 2)

    def test_retarget_recomputes_in_house_without_clearing_beats(self):
        s = scenario_b_uncertain_away()
        result = reconcile(list(s.records))
        hardware = DrumHardware(result.stops[1])  # 真相 (1,2)
        controller = SiteController(result.stops, s.target, 3)
        controller.step(hardware)
        spent = controller.beats_used
        # 临时换目标：馆内按当前剩余止位重算，不需要换操作卡
        new_target = Stop.of(3, 4)
        controller.retarget(new_target, horizon=6)
        controller.run(hardware)
        self.assertEqual(hardware.snapshot(), new_target.digits)
        # 已花拍数延续，重算不会把账清零
        self.assertGreaterEqual(controller.beats_used, spent)

    def test_one_beat_curtain_controller_unsatisfiable(self):
        s = scenario_c_one_beat_curtain()
        result = reconcile(list(s.records))
        with self.assertRaises(ValueError):
            SiteController(result.stops, s.target, s.curtain_beats)


if __name__ == "__main__":
    unittest.main()
