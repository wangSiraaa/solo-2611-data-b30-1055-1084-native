"""交班核对：联合止位、失回执/总闸组原子性、触片过滤与矛盾定位。"""

from __future__ import annotations

import unittest

from scoreboard.core import (
    DRUM_A,
    DRUM_B,
    Contact,
    Pulse,
    PulseStatus,
    Seen,
    Stop,
    forward_distance,
)
from scoreboard.reconcile import reconcile
from scoreboard.scenarios import (
    conflicting_records,
    scenario_a_certain_11,
    scenario_b_uncertain_away,
    scenario_e_breaker_group,
)


class TestWheelAndShape(unittest.TestCase):
    def test_forward_only_and_wraparound(self):
        self.assertEqual(forward_distance(9, 0), 1)
        self.assertEqual(forward_distance(0, 9), 9)
        self.assertEqual(forward_distance(3, 3), 0)

    def test_one_digit_only(self):
        with self.assertRaises(ValueError):
            Seen(seq=0, drum=DRUM_A, digit=10)
        with self.assertRaises(ValueError):
            Seen(seq=0, drum=DRUM_A, digit=True)  # bool 不得冒充整数
        with self.assertRaises(ValueError):
            Stop.of(1, 10)

    def test_unknown_drum_and_duplicate_seq(self):
        with self.assertRaises(ValueError):
            Seen(seq=0, drum="C", digit=1)
        with self.assertRaises(ValueError):
            reconcile([
                Seen(0, DRUM_A, 0), Seen(0, DRUM_B, 0),
            ])

    def test_missing_seen_rejected(self):
        with self.assertRaises(ValueError):
            reconcile([Seen(0, DRUM_A, 0),
                       Pulse(1, DRUM_B, PulseStatus.ACK)])


class TestReconcileStops(unittest.TestCase):
    def test_scenario_a_single_stop_11(self):
        result = reconcile(list(scenario_a_certain_11().records))
        self.assertTrue(result.feasible)
        self.assertEqual(result.stop_keys(), [(1, 1)])

    def test_scenario_b_two_stops_from_one_lost_receipt(self):
        result = reconcile(list(scenario_b_uncertain_away().records))
        self.assertTrue(result.feasible)
        # 客鼓最后一下可能走也可能没走 -> (1,1) 或 (1,2)
        self.assertEqual(result.stop_keys(), [(1, 1), (1, 2)])
        # 两个止位各自有独立见证，且对失回执脉冲给出相反取值
        truths = {w.stop.key(): w.pulse_truth[4] for w in result.witnesses}
        self.assertEqual(truths, {(1, 1): False, (1, 2): True})

    def test_breaker_group_is_atomic_paired_stops(self):
        result = reconcile(list(scenario_e_breaker_group().records))
        self.assertTrue(result.feasible)
        # 全丢 ->(1,1)，全走 ->(2,2)，交叉止位被原子性排除
        self.assertEqual(result.stop_keys(), [(1, 1), (2, 2)])
        for witness in result.witnesses:
            self.assertEqual(set(witness.group_truth), {"g1"})
        g1_walked = {w.stop.key(): w.group_truth["g1"] for w in result.witnesses}
        self.assertEqual(g1_walked, {(1, 1): False, (2, 2): True})

    def test_group_mixing_ack_rejected(self):
        records = [
            Seen(0, DRUM_A, 0), Seen(1, DRUM_B, 0),
            Pulse(2, DRUM_A, PulseStatus.NAK, group="g"),
            Pulse(3, DRUM_B, PulseStatus.ACK, group="g"),
        ]
        with self.assertRaises(ValueError):
            reconcile(records)

    def test_contact_keeps_matching_worlds_only(self):
        # 失回执后 A 可能在 0 或 1；触片确认在 {1} -> 只剩走了的真相
        records = [
            Seen(0, DRUM_A, 0), Seen(1, DRUM_B, 0),
            Pulse(2, DRUM_A, PulseStatus.NAK),
            Contact(3, DRUM_A, registered=(1,), reading=True),
        ]
        result = reconcile(records)
        self.assertEqual(result.stop_keys(), [(1, 0)])

    def test_contact_negative_reading(self):
        # 同一不确定性，触片确认不在 {1} -> 只剩没走的真相
        records = [
            Seen(0, DRUM_A, 0), Seen(1, DRUM_B, 0),
            Pulse(2, DRUM_A, PulseStatus.NAK),
            Contact(3, DRUM_A, registered=(1,), reading=False),
        ]
        result = reconcile(records)
        self.assertEqual(result.stop_keys(), [(0, 0)])


class TestConflict(unittest.TestCase):
    def test_contradiction_locates_incompatible_records(self):
        result = reconcile(conflicting_records())
        self.assertFalse(result.feasible)
        self.assertEqual(result.stops, ())
        conflict_seqs = {r.seq for r in result.conflict}
        # 脉冲把 A 顶到 1，触片却要求 A 在登记齿位 {0}：二者不能同时成立
        self.assertIn(2, conflict_seqs)
        self.assertIn(3, conflict_seqs)
        self.assertTrue(result.conflict_reason)

    def test_conflict_core_is_itself_unsatisfiable_but_removable(self):
        from scoreboard.reconcile import _simulate

        records = conflicting_records()
        result = reconcile(records)

        def satisfiable(subset):
            # 定位 Seen 作为共同前提始终在场，再枚举真相模拟
            premises = [x for x in records if isinstance(x, Seen)]
            try:
                return _simulate(premises + subset, {}, {}) is not None
            except ValueError:
                return False

        # 核本身仍然矛盾（无任何真相可通过）
        self.assertFalse(satisfiable(list(result.conflict)))
        # 贪心核只保留真正的矛盾成员（脉冲与触片），定位 Seen 不列入
        self.assertEqual([r.seq for r in result.conflict], [2, 3])
        # 最小性：从核里拿掉任一成员后立即存在可通过的真相
        for r in result.conflict:
            reduced = [x for x in result.conflict if x is not r]
            self.assertTrue(satisfiable(reduced),
                            f"移除 seq={r.seq} 后应可满足")


if __name__ == "__main__":
    unittest.main()
