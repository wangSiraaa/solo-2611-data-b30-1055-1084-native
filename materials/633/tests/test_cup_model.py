"""杯签纯逻辑：排名快照绑定、完整配对核对、完签枚举、机会相等与票本清点。

期望数字均按题面与显式手算给出，不依赖抽签函数生成。
"""

from __future__ import annotations

import unittest

from cup.model import (
    CatalogError,
    assert_catalog_matches_snapshot,
    build_catalog,
    complete_draws,
    consistent_orders,
    count_venue_arrangements,
    enumerate_away_orders,
    evaluate_pairing,
    match_venues,
    recount_integrity,
    seat_opportunities,
)
from cup.scenario import (
    AWAY_ORDER,
    ORIGINAL_VENUES,
    SUPPLEMENTAL_VENUES,
    build_snapshot,
    contention_venues,
)


class CupTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = build_snapshot()
        cls.snap_before = dict(cls.snap)  # 版本字段保持

    def cat(self, specs=None, cid="orig", snap=None):
        return build_catalog(cid, snap or self.snap, specs or ORIGINAL_VENUES)


class TestSnapshotVersionBinding(CupTestBase):
    def test_seeds_and_aways_come_from_snapshot_order(self):
        # 前三固定主签位、后三客队，全部由真实快照 order 决定
        self.assertEqual(self.snap["promotion"]["teams"], ["甲", "乙", "丙"])
        self.assertEqual(self.snap["relegation"]["teams"], ["丁", "戊", "己"])
        cat = self.cat()
        self.assertEqual(cat.seeds, ("甲", "乙", "丙"))
        self.assertEqual(cat.aways, ("丁", "戊", "己"))
        self.assertEqual(cat.snapshot_ref["snapshot_id"], self.snap["snapshot_id"])
        self.assertEqual(cat.snapshot_ref["edition_id"], self.snap["edition_id"])
        self.assertEqual(cat.snapshot_ref["seq_floor"], self.snap["seq_floor"])

    def test_catalog_rejects_wrong_version_teams(self):
        # 馆单声称自用馆属于“丙”，但用另一个把丙挤出前三的快照 -> 拒绝
        other = dict(self.snap)
        other["order"] = ["丁", "甲", "乙", "丙", "戊", "己"]
        with self.assertRaises(CatalogError):
            build_catalog("bad", other, ORIGINAL_VENUES)
        # 客队馆绑定了非本版本客队 -> 拒绝
        bad = [dict(v) for v in ORIGINAL_VENUES]
        bad[-1] = {"venue_id": "VX", "kind": "away", "host": "乙", "receives": []}
        with self.assertRaises(CatalogError):
            build_catalog("bad2", self.snap, bad)

    def test_assert_catalog_matches_snapshot(self):
        cat = self.cat()
        assert_catalog_matches_snapshot(cat, self.snap)  # 版本一致，不抛
        mismatch = dict(self.snap)
        mismatch["seq_floor"] = self.snap["seq_floor"] + 99
        with self.assertRaises(CatalogError):
            assert_catalog_matches_snapshot(cat, mismatch)

    def test_each_seed_has_exactly_one_primary_away_one_base(self):
        cat = self.cat()
        for s in cat.seeds:
            self.assertEqual(cat.primary(s).kind, "primary")
        for a in cat.aways:
            self.assertEqual(cat.away_base(a).kind, "away")


class TestPairingEvaluation(CupTestBase):
    def test_original_catalog_has_four_complete_draws(self):
        draws = complete_draws(self.cat())
        self.assertEqual(len(draws), 4)
        self.assertEqual([d["away_order"] for d in draws],
                         [["丁", "戊", "己"], ["戊", "丁", "己"],
                          ["己", "丁", "戊"], ["己", "戊", "丁"]])
        # 甲遇丁、戊、己的机会 1/4、1/4、1/2
        opp = seat_opportunities(self.cat(), [], 0)
        self.assertEqual(opp["fractions"], {"丁": (1, 4), "戊": (1, 4), "己": (2, 4)})

    def test_supplemental_catalog_has_six_draws_all_equal_thirds(self):
        cat = self.cat(SUPPLEMENTAL_VENUES, "supp")
        self.assertEqual(len(enumerate_away_orders(cat)), 6)
        opp = seat_opportunities(cat, [], 0)
        self.assertEqual(opp["fractions"], {"丁": (2, 6), "戊": (2, 6), "己": (2, 6)})

    def test_prefix_jia_meets_ding_leaves_only_wu(self):
        # 原始馆单：仅让公证席接手已揭的“甲遇丁”前缀，乙就只能抽戊
        cat = self.cat()
        allowed = consistent_orders(cat, [(0, "丁")])
        self.assertEqual(allowed, [("丁", "戊", "己")])
        opp = seat_opportunities(cat, [(0, "丁")], seat=1)
        self.assertEqual(opp["fractions"], {"戊": (1, 1)})

    def test_venue_contention_jointly_infeasible(self):
        cat = self.cat(contention_venues(), "cont")
        # 甲-戊、乙-己各自都能在中立馆开打，但两场都只剩中立馆
        ev = evaluate_pairing(cat, ["戊", "己", "丁"])
        self.assertFalse(ev["feasible"])
        opts = {m["seat"]: m["options"] for m in ev["matches"]}
        self.assertEqual(opts[1], ["VN1"])
        self.assertEqual(opts[2], ["VN1"])
        # 霍尔型缺额：两场比赛的可用馆并集只有一座中立馆
        pair_def = next(d for d in ev["deficiencies"] if d["need"] == 2)
        self.assertEqual(pair_def["union_venues"], ["VN1"])
        self.assertEqual([m["away"] for m in pair_def["matches"]], ["戊", "己"])

    def test_defensible_options_and_away_base(self):
        cat = self.cat()
        # VM甲、VM乙 都排序在前（VM乙 > VM甲 > VN1 的 ASCII），逐场核实
        self.assertEqual(match_venues(cat, "甲", "丁"), ["VM甲", "VN1"])
        self.assertEqual(match_venues(cat, "甲", "己"), ["VM甲"])   # 中立馆原馆单不接己
        self.assertEqual(match_venues(cat, "乙", "己"), [])          # 乙馆不接己，也无中立馆
        self.assertEqual(match_venues(cat, "丙", "己"), ["VM丙"])
        # 客队驻地馆不作为比赛场地
        for a in cat.aways:
            self.assertNotIn(cat.away_base(a).venue_id,
                             match_venues(cat, "甲", a) + match_venues(cat, "乙", a)
                             + match_venues(cat, "丙", a))

    def test_illegal_pairing_raises(self):
        cat = self.cat()
        with self.assertRaises(ValueError):
            evaluate_pairing(cat, ["丁", "戊", "戊"])  # 重复客队
        with self.assertRaises(ValueError):
            evaluate_pairing(cat, ["丁", "戊", "乙"])  # 非本版本客队


class TestBookkeepingNoDuplicateByVenueSwap(CupTestBase):
    def test_same_order_has_multiple_arrangements_but_counts_once(self):
        cat = self.cat()
        # 甲-丁、乙-戊 各自都有自用馆或中立馆两种选择 -> 该先后有 3 种摆法
        # （丙-己 只能 VM丙：VM甲/VN1 x VM乙/VN1 去重后为 3 组）
        self.assertEqual(count_venue_arrangements(cat, ["丁", "戊", "己"]), 3)
        # 但完签集合只按先后辨认：3 种摆法仍是 1 张
        self.assertEqual(len(enumerate_away_orders(cat)), 4)
        names = {tuple(d["away_order"]) for d in complete_draws(cat)}
        self.assertEqual(names, set(enumerate_away_orders(cat)))

    def test_recount_proves_no_duplicates_from_venue_swap(self):
        cat = self.cat()
        orders = [list(o) for o in enumerate_away_orders(cat)]
        rep = recount_integrity(cat, orders)
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["distinct_orders"], 4)
        self.assertEqual(rep["duplicates"], [])
        self.assertEqual(rep["missing"], [])
        self.assertEqual(rep["extra"], [])
        # 同一先后即使有多种场馆摆法，也只登记一次
        self.assertEqual(rep["venue_arrangement_counts"][0]["arrangements"], 3)

    def test_recount_flags_venue_swapped_duplicate_and_missing(self):
        cat = self.cat()
        orders = [list(o) for o in enumerate_away_orders(cat)]
        # 把一张签的摆法换馆后再登记一次 -> 同一先后重复入册
        dup = orders + [list(orders[0])]
        rep = recount_integrity(cat, dup)
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["duplicates"], [list(orders[0])])
        # 漏登一张 -> missing 命中
        rep2 = recount_integrity(cat, orders[:-1])
        self.assertFalse(rep2["ok"])
        self.assertEqual(len(rep2["missing"]), 1)

    def test_no_draw_catalog(self):
        # 乙馆与中立馆都不接任何客队 -> 乙无馆可办，无合法完签
        specs = [dict(v) for v in ORIGINAL_VENUES]
        specs[1] = {"venue_id": "VM乙", "kind": "primary", "host": "乙", "receives": []}
        specs[3] = {"venue_id": "VN1", "kind": "neutral", "host": None, "receives": []}
        cat = self.cat(specs, "empty")
        self.assertEqual(enumerate_away_orders(cat), [])
        self.assertEqual(seat_opportunities(cat, [], 0)["total"], 0)


if __name__ == "__main__":
    unittest.main()
