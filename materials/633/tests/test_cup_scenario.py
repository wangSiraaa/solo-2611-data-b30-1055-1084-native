"""题给场景的整合核对：真实联赛→快照→目录；判罚换种子；原排名仍可照查。

所有数字来自固定比分 + 题面描述，不依赖抽签函数。
"""

from __future__ import annotations

import unittest

from cup.model import (
    assert_catalog_matches_snapshot,
    build_catalog,
    complete_draws,
    enumerate_away_orders,
    seat_opportunities,
)
from cup.scenario import (
    ORIGINAL_VENUES,
    SUPPLEMENTAL_VENUES,
    build_snapshots,
    contention_venues,
)


class TestScenarioIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.orig_snap, cls.pen_snap = build_snapshots()

    def test_original_ranking_and_version(self):
        s = self.orig_snap
        self.assertEqual(s["order"], ["甲", "乙", "丙", "丁", "戊", "己"])
        self.assertEqual(s["promotion"]["teams"], ["甲", "乙", "丙"])
        self.assertEqual(s["relegation"]["teams"], ["丁", "戊", "己"])
        # 每场名次靠前者 1:0 胜：前三各 5 场? 不，是每队 5 场全季、前三名各 5 胜
        for r in s["ranked"]:
            self.assertEqual(r["played"], 5)
        self.assertEqual([r["total_points"] for r in s["ranked"]],
                         [15, 12, 9, 6, 3, 0])

    def test_original_catalog_four_draws_and_probabilities(self):
        cat = build_catalog("orig", self.orig_snap, ORIGINAL_VENUES)
        assert_catalog_matches_snapshot(cat, self.orig_snap)
        self.assertEqual(len(enumerate_away_orders(cat)), 4)
        self.assertEqual(seat_opportunities(cat, [], 0)["fractions"],
                         {"丁": (1, 4), "戊": (1, 4), "己": (2, 4)})

    def test_supplemental_six_draws_equal_thirds(self):
        cat = build_catalog("supp", self.orig_snap, SUPPLEMENTAL_VENUES)
        self.assertEqual(len(enumerate_away_orders(cat)), 6)
        self.assertEqual(seat_opportunities(cat, [], 0)["fractions"],
                         {"丁": (2, 6), "戊": (2, 6), "己": (2, 6)})

    def test_penalty_replaces_one_seed_but_original_ranking_still_readable(self):
        # 判罚把甲从第一压到第二：种子变成 乙、甲、丙，客队仍是丁戊己
        self.assertEqual(self.pen_snap["order"], ["乙", "甲", "丙", "丁", "戊", "己"])
        self.assertEqual(self.pen_snap["promotion"]["teams"], ["乙", "甲", "丙"])
        self.assertEqual(self.pen_snap["relegation"]["teams"], ["丁", "戊", "己"])
        # 新版本绑定不同快照，自用馆按新种子顺序（乙 座第一）
        pen_cat = build_catalog("pen", self.pen_snap, ORIGINAL_VENUES)
        self.assertEqual(pen_cat.seeds, ("乙", "甲", "丙"))
        assert_catalog_matches_snapshot(pen_cat, self.pen_snap)
        # 原排名快照与新快照版本不同，各自目录可分别解释
        self.assertNotEqual(self.pen_snap["snapshot_id"], self.orig_snap["snapshot_id"])
        self.assertNotEqual(self.pen_snap["seq_floor"], self.orig_snap["seq_floor"])
        self.assertNotEqual(self.pen_snap["order"], self.orig_snap["order"])
        cat_orig = build_catalog("orig2", self.orig_snap, ORIGINAL_VENUES)
        assert_catalog_matches_snapshot(cat_orig, self.orig_snap)
        # 用错版本目录去核对旧快照必须失败
        from cup.model import CatalogError
        with self.assertRaises(CatalogError):
            assert_catalog_matches_snapshot(pen_cat, self.orig_snap)

    def test_penalty_draws_uses_first_seed_venue(self):
        pen_cat = build_catalog("pen", self.pen_snap, ORIGINAL_VENUES)
        for d in complete_draws(pen_cat):
            # 第一席由乙主持，座位比赛落在 乙 的自用馆
            self.assertEqual(d["matches"][0]["seed"], "乙")
            self.assertEqual(d["matches"][0]["home_venue"], "VM乙")

    def test_both_neutral_contention_example(self):
        cat = build_catalog("cont", self.orig_snap, contention_venues())
        # 甲-戊、乙-己 各自都能在中立馆开；合并三场则不行
        from cup.model import evaluate_pairing
        ev = evaluate_pairing(cat, ["戊", "己", "丁"])
        self.assertFalse(ev["feasible"])
        self.assertEqual(ev["matches"][0]["options"], ["VN1"])
        self.assertEqual(ev["matches"][1]["options"], ["VN1"])


if __name__ == "__main__":
    unittest.main()
