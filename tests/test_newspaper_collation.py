"""编年校勘引擎的 unittest 覆盖。

覆盖：历书展开与闰同名月身份、闰字漏写歧义求解、合刊相邻两次应刊日、印次号
连续性、同张/异张占位互斥、指定归期核验、历书订正后不能入目的回报。
"""

from __future__ import annotations

import unittest

from newspaper import (
    Almanac,
    CollationEngine,
    CollationError,
    Issue,
    Masthead,
    Month,
)


def almanac() -> Almanac:
    return Almanac(
        almanac_id="AL1", revision=1, epoch_day=0,
        months=(
            Month(0, "四月", False, 2),
            Month(1, "四月", True, 2),
            Month(2, "五月", False, 2),
        ),
    )


def holdings() -> list[Issue]:
    return [
        Issue("I0", "S0", 0, (Masthead("四月", 1),)),
        Issue("I1", "S1", 1, (Masthead("四月", 2),)),
        Issue("I2S", "S2", 2, (Masthead("四月", 1, leap=None),)),
        Issue("I2", "S3", 3, (Masthead("四月", 2, leap=None),
                              Masthead("五月", 1))),
        Issue("I4", "S4", 4, (Masthead("五月", 2),)),
    ]


class AlmanacExpansion(unittest.TestCase):
    def test_day_zero_epoch_and_double_fourth_month_identity(self):
        days = almanac().days()
        self.assertEqual([d.day for d in days], [0, 1, 2, 3, 4, 5])
        self.assertEqual(days[1].label(), "四月2")
        self.assertEqual(days[2].label(), "闰四月1")
        self.assertEqual(days[3].label(), "闰四月2")
        self.assertEqual(days[4].label(), "五月1")
        # 同名月是两个不同身份。
        self.assertNotEqual((days[1].month_name, days[1].leap),
                            (days[3].month_name, days[3].leap))

    def test_duplicate_identity_rejected(self):
        bad = Almanac("AL", 1, 0, (
            Month(0, "四月", False, 1), Month(1, "四月", False, 1),
        ))
        with self.assertRaises(CollationError):
            CollationEngine(bad, [0, 1], holdings()[:2])

    def test_schedule_must_live_inside_almanac(self):
        with self.assertRaises(CollationError):
            CollationEngine(almanac(), [0, 9], holdings()[:2])


class CombinedIssueCollation(unittest.TestCase):
    def test_ambiguous_first_column_converges_to_leap_pair_days_3_4(self):
        engine = CollationEngine(almanac(), [0, 1, 2, 3, 4, 5], holdings())
        solutions = engine.solve()
        self.assertEqual(len(solutions), 1)
        placement = next(p for p in solutions[0] if p.issue_id == "I2")
        self.assertEqual(placement.days, (3, 4))
        self.assertEqual(placement.labels, ("闰四月2", "五月1"))

    def test_explicit_leap_marker_is_fixed_identity(self):
        issues = holdings()
        issues[3] = Issue("I2", "S3", 3,
                          (Masthead("四月", 2, leap=True), Masthead("五月", 1)))
        engine = CollationEngine(almanac(), [0, 1, 2, 3, 4, 5], issues)
        self.assertEqual(len(engine.solve()), 1)

    def test_explicit_plain_marker_breaks_adjacency(self):
        # 明写“平四月二”（日1）后，再无与“五月一”（日4）相邻的应刊日。
        issues = holdings()
        issues[3] = Issue("I2", "S3", 3,
                          (Masthead("四月", 2, leap=False), Masthead("五月", 1)))
        engine = CollationEngine(almanac(), [0, 1, 2, 3, 4, 5], issues)
        self.assertEqual(engine.solve(), [])
        blocked = engine.undatable_sheets()
        self.assertTrue(any(s["issue_id"] == "I2"
                            and s["reason"] == "no_adjacent_scheduled_pair"
                            for s in blocked))

    def test_designation_holds_in_every_chronology(self):
        engine = CollationEngine(almanac(), [0, 1, 2, 3, 4, 5], holdings())
        verdict = engine.verify_designation("I2", 3)
        self.assertTrue(verdict["holds_in_every_chronology"])
        self.assertEqual(verdict["date_label"], "闰四月2")
        self.assertFalse(engine.verify_designation("I2", 1)["holds_in"])


class RunNumberAndOccupancy(unittest.TestCase):
    def test_run_numbers_must_follow_day_order(self):
        issues = holdings()
        issues[0] = Issue("I0", "S0", 4, (Masthead("四月", 1),))  # 印次号倒挂
        issues[4] = Issue("I4", "S4", 0, (Masthead("五月", 2),))
        engine = CollationEngine(almanac(), [0, 1, 2, 3, 4, 5], issues)
        self.assertEqual(engine.solve(), [])

    def test_gap_in_run_numbers_rejected_upfront(self):
        issues = holdings()
        issues[4] = Issue("I4", "S4", 9, (Masthead("五月", 2),))
        with self.assertRaises(CollationError):
            CollationEngine(almanac(), [0, 1, 2, 3, 4, 5], issues)

    def test_two_sheets_cannot_occupy_same_scheduled_day(self):
        # 两张都声称“四月一”：没有排年能让异张原件各占独立应刊日。
        issues = holdings()
        issues[0] = Issue("I0", "S0", 0, (Masthead("四月", 1),))
        issues[1] = Issue("I1", "S1", 1, (Masthead("四月", 1, leap=None),))
        engine = CollationEngine(almanac(), [0, 1, 2, 3, 4, 5], issues)
        self.assertEqual(engine.solve(), [])

    def test_every_sheet_must_be_catalogued(self):
        # 应刊日清单少给一天，五张原件无法全部入目。
        engine = CollationEngine(almanac(), [0, 1, 2, 3, 4], holdings())
        self.assertEqual(engine.solve(), [])

    def test_same_sheet_id_rejected(self):
        issues = holdings()
        issues[1] = Issue("I1", "S0", 1, (Masthead("四月", 2),))
        with self.assertRaises(CollationError):
            CollationEngine(almanac(), [0, 1, 2, 3, 4, 5], issues)


class AmendedAlmanac(unittest.TestCase):
    def test_inserted_short_month_destroys_adjacent_reading(self):
        amended = Almanac("AL1", 2, 0, (
            Month(0, "四月", False, 2),
            Month(1, "四月", True, 2),
            Month(2, "四月后小月", False, 1),
            Month(3, "五月", False, 2),
        ))
        engine = CollationEngine(amended, list(range(7)), holdings())
        self.assertEqual(engine.solve(), [])
        blocked = engine.undatable_sheets()
        sheet = next(s for s in blocked if s["issue_id"] == "I2")
        self.assertEqual(sheet["reason"], "no_adjacent_scheduled_pair")


if __name__ == "__main__":
    unittest.main()
