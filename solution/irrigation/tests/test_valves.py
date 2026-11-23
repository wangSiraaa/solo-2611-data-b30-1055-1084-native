"""两种阀门组织、调阀拍与阀牌生命周期、出水回条测试。"""

import unittest

import _path  # noqa: F401
from hydraulics import Nozzle, Pipe, PipeTree
from scheduling import Bed, enumerate_schedules
from valves import (
    BranchPermits,
    DosingLedger,
    MasterValveBoard,
    ValveError,
    ValveTag,
)


def tree() -> PipeTree:
    return PipeTree(
        root_id="R", head=3,
        pipes=[Pipe("R", "J", 1), Pipe("J", "A", 0), Pipe("J", "B", 0)],
        nozzles=[Nozzle("A", "A", 1, 2), Nozzle("B", "B", 1, 2)],
    )


def beds() -> list[Bed]:
    return [
        Bed("畦A", frozenset({"A"}), frozenset({1, 2, 3}), need=1),
        Bed("畦B", frozenset({"B"}), frozenset({1, 2, 3}), need=1),
    ]


class MasterBoardTests(unittest.TestCase):
    def test_free_reconfigure_teaching_default_costs_zero_beats(self):
        board = MasterValveBoard(tree(), adjust_beats=0)
        result = enumerate_schedules(beds(), [1, 2], board.group_ok)
        order = next(o for o in result.feasible
                     if o.assignments[1].get("畦A") and o.assignments[2].get("畦B"))
        ledger = board.execute(order, DosingLedger())
        self.assertEqual(board.beats_used, 0)
        self.assertEqual(len(ledger.records), 2)

    def test_joint_group_refused_even_under_free_mode(self):
        board = MasterValveBoard(tree())
        self.assertTrue(board.group_ok(frozenset({"A"})))
        self.assertFalse(board.group_ok(frozenset({"A", "B"})))

    def test_adjust_beat_needs_an_empty_slot(self):
        t = tree()
        board = MasterValveBoard(t, adjust_beats=1, free_reconfigure=False)
        wide = enumerate_schedules(beds(), [1, 2, 3], board.group_ok)
        # 格1 A、格3 B，中间格2空出作调阀拍 -> 可行且恰耗 1 拍。
        ok = next(o for o in wide.feasible
                  if o.assignments[1].get("畦A") == "A"
                  and o.assignments[3].get("畦B") == "B")
        board.execute(ok, DosingLedger())
        self.assertEqual(board.beats_used, 1)
        # 相邻两格换不同阀组 -> 调阀拍无处安放。
        adjacent = next(o for o in wide.feasible
                        if o.assignments[1].get("畦A") == "A"
                        and o.assignments[2].get("畦B") == "B")
        with self.assertRaises(ValveError):
            MasterValveBoard(t, 1, free_reconfigure=False).execute(
                adjacent, DosingLedger())

    def test_zero_beat_budget_blocks_any_switch(self):
        t = tree()
        result = enumerate_schedules(beds(), [1, 2],
                                     MasterValveBoard(t).group_ok)
        order = next(o for o in result.feasible if o.assignments[1] and o.assignments[2])
        with self.assertRaises(ValveError):
            MasterValveBoard(t, adjust_beats=0,
                             free_reconfigure=False).execute(order, DosingLedger())

    def test_shortened_window_makes_one_beat_unaffordable(self):
        t = tree()
        short_beds = [
            Bed("畦A", frozenset({"A"}), frozenset({1, 2, 3}), need=1),
            Bed("畦B", frozenset({"B"}), frozenset({2}), need=1),
        ]
        result = enumerate_schedules(short_beds, [1, 2, 3],
                                     MasterValveBoard(t).group_ok)
        self.assertEqual(len(result.feasible), 2)  # 节奏+试水本身可行
        for order in result.feasible:
            with self.assertRaises(ValveError):
                MasterValveBoard(t, 1, free_reconfigure=False).execute(
                    order, DosingLedger())


class BranchPermitTests(unittest.TestCase):
    def test_issuing_two_single_open_certs_rejected(self):
        permits = BranchPermits(tree(), {"A": "支1", "B": "支2"})
        permits.issue(ValveTag("T1", "支1", frozenset({"A"})))
        with self.assertRaises(ValveError):
            permits.issue(ValveTag("T2", "支2", frozenset({"B"})))

    def test_collect_one_tag_then_other_branch_kept_safe(self):
        permits = BranchPermits(tree(), {"A": "支1", "B": "支2"})
        permits.issue(ValveTag("T1", "支1", frozenset({"A"})))
        permits.collect("支1")
        permits.issue(ValveTag("T2", "支2", frozenset({"B"})))
        self.assertTrue(permits.group_ok(frozenset({"B"})))
        self.assertFalse(permits.group_ok(frozenset({"A", "B"})))

    def test_collected_tag_cannot_be_reused(self):
        permits = BranchPermits(tree(), {"A": "支1"})
        permits.issue(ValveTag("T1", "支1", frozenset({"A"})))
        permits.collect("支1")
        with self.assertRaises(ValveError):
            permits.issue(ValveTag("T1", "支1", frozenset({"A"})))
        permits.issue(ValveTag("T9", "支1", frozenset({"A"})))  # 新牌可申
        self.assertTrue(permits.group_ok(frozenset({"A"})))

    def test_wrong_branch_nozzle_rejected(self):
        permits = BranchPermits(tree(), {"A": "支1"})
        with self.assertRaises(ValveError):
            permits.issue(ValveTag("T1", "支1", frozenset({"B"})))

    def test_safe_parallel_group_can_be_issued_together(self):
        # 构造一个并开也达标的网：两支系数 0、根段系数 0 -> 并开不亏压。
        t = PipeTree(
            root_id="R", head=2,
            pipes=[Pipe("R", "A", 0), Pipe("R", "B", 0)],
            nozzles=[Nozzle("A", "A", 1, 2), Nozzle("B", "B", 1, 2)],
        )
        permits = BranchPermits(t, {"A": "支1", "B": "支2"})
        permits.issue(ValveTag("T1", "支1", frozenset({"A"})))
        permits.issue(ValveTag("T2", "支2", frozenset({"B"})))
        test = permits.open_for(frozenset({"A", "B"}))
        self.assertTrue(test.passed)


class LedgerTests(unittest.TestCase):
    def test_only_passing_tests_enter_ledger(self):
        t = tree()
        ledger = DosingLedger()
        good = t.pressure_test({"A"})
        bad = t.pressure_test({"A", "B"})
        ledger.append(1, "畦A", "A", good)
        with self.assertRaises(ValveError):
            ledger.append(2, "畦B", "B", bad)
        self.assertEqual(ledger.slots_for("畦A"), (1,))

    def test_ledger_feeds_cross_shift_history(self):
        t = tree()
        ledger = DosingLedger()
        ledger.append(0, "畦C", "A", t.pressure_test({"A"}))
        bed = Bed("畦C", frozenset({"A"}), frozenset({1, 2, 3}), need=1, gap=1)
        result = enumerate_schedules(
            [bed], [1, 2, 3], MasterValveBoard(t).group_ok,
            history=ledger.history(),
        )
        self.assertEqual(
            {o.watered_slots("畦C") for o in result.feasible}, {(2,), (3,)}
        )


if __name__ == "__main__":
    unittest.main()
