"""赛程主客、半场方向与换边镜像测试。"""

from __future__ import annotations

import unittest

from review.model import AttackEnd, Side
from review.schedule import (ScheduleBook, ahead_value, attack_sign,
                             team_attack_sign)

FIXTURES = (
    {"match_id": "m1", "round": 1, "home": "HOME-FC", "away": "AWAY-XI"},
)


class TestSchedule(unittest.TestCase):
    def setUp(self):
        self.book = ScheduleBook(FIXTURES)

    def test_lookup_real_home_away(self):
        slot = self.book.lookup("m1")
        self.assertEqual((slot.home, slot.away), ("HOME-FC", "AWAY-XI"))
        self.assertEqual(self.book.side_of("m1", "AWAY-XI"), Side.AWAY)
        with self.assertRaises(KeyError):
            self.book.lookup("missing")

    def test_attack_sign_per_half_and_switch(self):
        first = AttackEnd.HIGH
        second = AttackEnd.LOW  # 换边后整段反向
        self.assertEqual(attack_sign(first), +1)
        self.assertEqual(attack_sign(second), -1)
        self.assertEqual(team_attack_sign(first, Side.HOME), +1)
        self.assertEqual(team_attack_sign(first, Side.AWAY), -1)
        self.assertEqual(team_attack_sign(second, Side.AWAY), +1)

    def test_ahead_value_orientation(self):
        # 进攻 LOW 端时，数值越小越靠前
        self.assertGreater(ahead_value(40, -1), ahead_value(60, -1))
        self.assertGreater(ahead_value(60, +1), ahead_value(40, +1))


if __name__ == "__main__":
    unittest.main()
