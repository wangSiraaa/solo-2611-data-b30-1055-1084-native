"""越线裁决的手算契约：待核/确认/驳回/矛盾/镜像/缺帧。

手算（进攻向右，sign=+1，D1=90、D2=60、球=50）：
- 接应 R 机内4=61、机内5=60；正式5出球。
- 偏移 -1：正式5 -> 机内4 -> R=61，61>60(D2) 且 61>50(球) -> 越线。
- 偏移 0：正式5 -> 机内5 -> R=60，与 D2 平齐 -> 未越线（平齐不算）。
"""

from __future__ import annotations

import unittest

from review.calibration import admit
from review.fixtures import (confirmed_case, conflicting_flash_case,
                             mirrored_case, pending_case, rejected_case)
from review.model import (AttackEnd, BenchmarkPair, CameraSpec, CaseFile,
                          FlashSighting, FrameObservation, Side, Verdict)
from review.offside import review


class TestScenarioContract(unittest.TestCase):
    def test_pending_two_opposite_scenarios(self):
        case = pending_case()
        result = admit(case.cameras)
        self.assertTrue(result.admitted)
        self.assertEqual(len(result.interpretations), 4)  # 2 机 × {0,-1}
        decision = review(case, result, Side.HOME)
        self.assertEqual(decision.verdict, Verdict.PENDING)
        flags = {c.offside for c in decision.cells if c.decidable}
        self.assertEqual(flags, {True, False})
        self.assertEqual(len(decision.retained_scenarios), 2)

    def test_sync_card_confirms_immediately(self):
        case = confirmed_case()
        result = admit(case.cameras)
        self.assertEqual(len(result.interpretations), 1)
        decision = review(case, result, Side.HOME)
        self.assertEqual(decision.verdict, Verdict.CONFIRMED)
        cell = decision.cells[0]
        self.assertTrue(cell.offside)
        self.assertEqual((cell.receiver_position, cell.ball_position),
                         (61, 50))
        self.assertEqual(cell.second_last_defender, ("D2", 60))

    def test_review_beat_six_is_level_and_rejected(self):
        case = rejected_case()
        result = admit(case.cameras)
        decision = review(case, result, Side.HOME)
        self.assertEqual(decision.verdict, Verdict.REJECTED)
        cell = decision.cells[0]
        self.assertFalse(cell.offside)
        self.assertEqual(cell.receiver_position, 60)
        self.assertEqual(cell.second_last_defender, ("D2", 60))  # 平齐

    def test_conflicting_flash_not_admitted(self):
        case = conflicting_flash_case()
        result = admit(case.cameras)
        self.assertFalse(result.admitted)
        decision = review(case, result, Side.HOME)
        self.assertEqual(decision.verdict, Verdict.NOT_ADMITTED)

    def test_mirrored_half_uses_reversed_axis(self):
        case = mirrored_case()
        self.assertEqual(case.home_attack_end, AttackEnd.LOW)
        result = admit(case.cameras)
        for axis in result.interpretations[0].axes.values():
            self.assertEqual((axis.sign, axis.translation), (-1, 100))
        decision = review(case, result, Side.AWAY)  # 换边后客队朝 100 端
        self.assertEqual(decision.verdict, Verdict.CONFIRMED)
        cell = decision.cells[0]
        # 机内格 39 经反向轴还原为真位置 61
        self.assertEqual(cell.receiver_position, 61)


class TestMissingFrame(unittest.TestCase):
    def _case_without_receiver_frame(self):
        base = confirmed_case()
        obs = tuple(o for o in base.observations
                    if not (o.label == "R" and o.camera_tick == 4))
        return CaseFile(**{**base.__dict__, "observations": obs})

    def test_missing_frame_reported_and_undecidable(self):
        case = self._case_without_receiver_frame()
        result = admit(case.cameras)
        decision = review(case, result, Side.HOME)
        # 唯一解释下正式5 -> 机内4，R 帧缺失：不可裁决而非编造
        self.assertEqual(decision.verdict, Verdict.PENDING)
        self.assertTrue(any("R" in m for m in decision.missing_frames))
        self.assertFalse(any(c.decidable for c in decision.cells))


class TestSameSourceConflict(unittest.TestCase):
    def test_synced_cameras_disagreeing_is_conflict(self):
        # 同步卡把两机锁到同一偏移，正式5 -> 两机机内4（同源帧）；
        # 把 CAM-B 在机内4 的 R 改成另一格号，必须报证据冲突且不可裁决。
        base = confirmed_case()
        obs = []
        for o in base.observations:
            if (o.camera_id == "CAM-B" and o.camera_tick == 4
                    and o.label == "R"):
                obs.append(FrameObservation("CAM-B", 4, "R", 70))
            else:
                obs.append(o)
        case = CaseFile(**{**base.__dict__, "observations": tuple(obs)})
        result = admit(case.cameras)
        decision = review(case, result, Side.HOME)
        self.assertFalse(decision.cells[0].decidable)
        self.assertTrue(decision.conflicts)
        self.assertIn("R", decision.conflicts[0])


class TestStrictnessAndDefenderLine(unittest.TestCase):
    def test_six_defenders_second_last_sorting(self):
        # 6 名防守人位置 95,90,80,70,60,55；靠前值升序后 [-2] 是 90（D2）
        defenders = ("D1", "D2", "D3", "D4", "D5", "D6")
        positions = {"D1": 95, "D2": 90, "D3": 80,
                     "D4": 70, "D5": 60, "D6": 55}
        camera = CameraSpec(
            "C", (BenchmarkPair(0, frozenset({0})),
                  BenchmarkPair(100, frozenset({100}))), (),
            frozenset({0}))
        obs = [FrameObservation("C", 5, label, pos)
               for label, pos in positions.items()]
        obs += [FrameObservation("C", 5, "ball", 50),
                FrameObservation("C", 5, "R", 60),   # 落后于倒数第二防线 90
                FrameObservation("C", 5, "P", 40)]
        case = CaseFile(
            match_id="x", half=1, home_team="H", away_team="A",
            home_attack_end=AttackEnd.HIGH, cameras=(camera,),
            observations=tuple(obs), release_beats=frozenset({5}),
            passer="P", receiver="R", defender_labels=defenders)
        result = admit((camera,))
        # 主队攻 HIGH 端、sign=+1：升序后 [-2] 即倒数第二防线 D2=90
        decision = review(case, result, Side.HOME)
        self.assertEqual(decision.verdict, Verdict.REJECTED)
        self.assertEqual(decision.cells[0].second_last_defender, ("D2", 90))


if __name__ == "__main__":
    unittest.main()
