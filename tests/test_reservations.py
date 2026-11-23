"""预订与挂签测试：保改约占用、次日改约、不可复原、接真实余额。"""

from __future__ import annotations

import unittest

from orchard.errors import (
    AmendmentNotHonorable,
    ReservationConflict,
    ReservationNotFound,
)
from orchard.model import HarvestSession, Orchard
from orchard.reservations import (
    AmendmentDecision,
    BalanceTakeover,
    HoldItem,
    ReservationBook,
    available_branches_for_takeover,
    honor_amendment,
)


def green_yellow_orchard() -> Orchard:
    """两根可剪侧枝各挂一串：左黄 L、右青 G，均挂在保留根 M 下。"""
    o = Orchard()
    o.add_tree("T1")
    o.add_main_branch("T1", "M", buds=2, min_buds=0)
    o.add_branch("T1", "L", "M", buds=1)
    o.add_branch("T1", "R", "M", buds=1)
    o.add_string("SY", "L", row="R", variety="YELLOW", grade="A")
    o.add_string("SG", "R", row="R", variety="GREEN", grade="A")
    o.validate()
    return o


class HoldTest(unittest.TestCase):
    def test_hold_blocks_real_order_then_release_frees(self) -> None:
        o = green_yellow_orchard()
        book = ReservationBook(o)
        book.hold("H1", "JIA", (HoldItem("STRING", "SY"),))
        with self.assertRaises(ReservationConflict):
            book.take_string("SY")           # 乙首日不能带走被挂的黄
        self.assertTrue(book.is_string_free("SG"))
        book.release("H1")
        book.take_string("SY")               # 释放后黄可用

    def test_branch_hold_blocks_real_take(self) -> None:
        o = green_yellow_orchard()
        book = ReservationBook(o)
        book.hold("H1", "JIA", (HoldItem("BRANCH", "L"),))
        self.assertFalse(book.is_branch_free("L"))
        with self.assertRaises(ReservationConflict):
            book.take_branch("L")
        book.release("H1")
        book.take_branch("L")

    def test_main_root_cannot_be_held_as_replacement(self) -> None:
        o = green_yellow_orchard()
        book = ReservationBook(o)
        with self.assertRaises(ReservationConflict):
            book.hold("H1", "JIA", (HoldItem("BRANCH", "M"),))

    def test_double_hold_conflict(self) -> None:
        o = green_yellow_orchard()
        book = ReservationBook(o)
        book.hold("H1", "JIA", (HoldItem("STRING", "SY"),))
        with self.assertRaises(ReservationConflict):
            book.hold("H2", "YI", (HoldItem("STRING", "SY"),))

    def test_release_unknown(self) -> None:
        book = ReservationBook(green_yellow_orchard())
        with self.assertRaises(ReservationNotFound):
            book.release("nope")


class AmendmentTest(unittest.TestCase):
    def test_cannot_promise_what_was_cut_away(self) -> None:
        """乙首日先剪走黄 L；次日期望甲仍拿黄已不可能。"""
        o = green_yellow_orchard()
        session = HarvestSession(o)
        session.cut("L")
        with self.assertRaises(AmendmentNotHonorable):
            honor_amendment(o, session, AmendmentDecision("JIA", "SY", "L"))

    def test_amendment_switches_to_surviving_branch(self) -> None:
        """甲约青但翌日改黄；乙已剪走黄 -> 只剩青，不能承诺黄；
        反之若甲次日改青（青尚存），可从青兑现。"""
        o = green_yellow_orchard()
        session = HarvestSession(o)
        session.cut("L")  # 乙带走黄
        opt = honor_amendment(o, session, AmendmentDecision("JIA", "SG", "R"))
        self.assertEqual(opt.string_id, "SG")
        self.assertEqual(opt.branch_id, "R")
        # 总芽 4（M2+L1+R1），两刀各带走 1 -> 余 2。
        self.assertEqual(session.remaining_buds()["M"], 2)

    def test_takeover_without_hold_exposes_branches(self) -> None:
        """甲改约前不替他封候选：接乙时可占实际可用的枝。"""
        o = green_yellow_orchard()
        session = HarvestSession(o)
        takeover = BalanceTakeover("YI", "CY", reserved_branches=frozenset())
        branches = available_branches_for_takeover(o, session, takeover, ("SY",))
        self.assertEqual(branches, ("L",))

    def test_takeover_respects_reserved_and_cut(self) -> None:
        o = green_yellow_orchard()
        session = HarvestSession(o)
        session.cut("L")
        takeover = BalanceTakeover("YI", "CY", reserved_branches=frozenset({"L"}))
        # 黄枝已被剪走、又（名义上）被挂，实际不可用。
        self.assertEqual(available_branches_for_takeover(o, session, takeover, ("SY",)), ())


if __name__ == "__main__":
    unittest.main()
