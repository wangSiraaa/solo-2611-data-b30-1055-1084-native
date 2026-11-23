"""公证抽签：均匀整数范围、等机会、封箱凭证、独立重放与停电接班、无签不抽。

抽签源用固定小整数（注入 urand），不依赖网络随机服务。
"""

from __future__ import annotations

import unittest

from cup.model import (
    build_catalog,
    enumerate_away_orders,
    seat_opportunities,
)
from cup.draw import (
    DrawError,
    UniformSource,
    make_draw,
    replay_draw,
    seat_state_after_prefix,
    uniform_numbers,
)

from cup.scenario import ORIGINAL_VENUES, SUPPLEMENTAL_VENUES, build_snapshot


def seat_opportunities_of(cat, prefix_order, seat):
    return seat_opportunities(cat, [(s, prefix_order[s]) for s in range(len(prefix_order))],
                              seat=seat)["fractions"]


def forced_source(value: int) -> UniformSource:
    return UniformSource(urand=lambda mod: value if 0 <= value < mod else 0)


class DrawTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = build_snapshot()
        cls.orig = build_catalog("orig", cls.snap, ORIGINAL_VENUES)
        cls.supp = build_catalog("supp", cls.snap, SUPPLEMENTAL_VENUES)


class TestUniformIntegerSource(DrawTestBase):
    def test_int_in_records_legit_range_and_value(self):
        src = UniformSource(seed=1)
        v = src.int_in(5, 9, "x")
        self.assertGreaterEqual(v, 5)
        self.assertLessEqual(v, 9)
        d = src.trace[-1]
        self.assertEqual((d["lo"], d["hi"], d["range"]), (5, 9, 5))
        self.assertEqual(d["value"], v)

    def test_choose_bounds(self):
        src = UniformSource(seed=2)
        v = src.choose(3, "y")
        self.assertIn(v, (0, 1, 2))
        self.assertEqual((src.trace[-1]["lo"], src.trace[-1]["hi"]), (0, 2))

    def test_reject_bad_range(self):
        with self.assertRaises(DrawError):
            UniformSource(seed=3).choose(0, "z")
        with self.assertRaises(DrawError):
            UniformSource(seed=3).int_in(4, 2, "z")


class TestEqualOpportunity(DrawTestBase):
    def test_table_mode_is_uniform_over_feasible_draws(self):
        # 四种完签、固定种子跑很多次，四种都应出现；每个整数 0..3 恰对应一张
        seen = set()
        for seed in range(200):
            env = make_draw(self.orig, f"t{seed}", UniformSource(seed=seed), "table")
            seen.add(tuple(env["away_order"]))
        self.assertEqual(seen, set(enumerate_away_orders(self.orig)))

    def test_sequential_mode_is_uniform_over_feasible_draws(self):
        seen = set()
        for seed in range(400):
            env = make_draw(self.orig, f"s{seed}", UniformSource(seed=seed),
                            "sequential")
            seen.add(tuple(env["away_order"]))
        self.assertEqual(seen, set(enumerate_away_orders(self.orig)))

    def test_first_seat_opportunities_follow_fractions(self):
        # 原始馆单首签机会 丁/戊 各 1/4、己 1/2；逐席抽数的第一段长即计数
        env = make_draw(self.orig, "s0", UniformSource(urand=lambda m: 0),
                        "sequential")
        segs = env["segments"][0]["layout"]
        self.assertEqual([(s["away"], s["count"]) for s in segs],
                         [("丁", 1), ("戊", 1), ("己", 2)])
        self.assertEqual(env["segments"][0]["value"] + 1 <= 4, True)

    def test_supplemental_three_way_equal(self):
        env = make_draw(self.supp, "sup-seq", UniformSource(urand=lambda m: 0),
                        "sequential")
        self.assertEqual([s["count"] for s in env["segments"][0]["layout"]],
                         [2, 2, 2])
        # 表模式下 6 张完签里甲对三队各 2 种 -> 各 1/3
        rep = replay_draw(self.supp, make_draw(
            self.supp, "sup-t", UniformSource(urand=lambda m: 0), "table"))
        self.assertEqual(rep["opportunities"][0]["fractions"],
                         {"丁": (2, 6), "戊": (2, 6), "己": (2, 6)})


class TestEnvelopeAndReplay(DrawTestBase):
    def test_numbering_is_equal_length_and_venue_independent(self):
        nums = uniform_numbers(self.orig)
        self.assertEqual(len(nums), 4)
        self.assertEqual({len(n) for n in nums.values()}, {3})
        # 同一先后只有一张编号，即使有多种场馆摆法
        self.assertEqual(len(set(nums.values())), 4)

    def test_table_replay_reconstructs_matches_without_trusting_envelope(self):
        env = make_draw(self.orig, "t0", forced_source(0), "table")
        rep = replay_draw(self.orig, env)
        self.assertEqual(rep["away_order"],
                         list(enumerate_away_orders(self.orig)[0]))
        # 机会由目录重算：首签 1/4、1/4、1/2
        self.assertEqual(rep["opportunities"][0]["fractions"],
                         {"丁": (1, 4), "戊": (1, 4), "己": (2, 4)})
        # 比赛场馆互不重复
        venues = [m["venue"] for m in rep["matches"]]
        self.assertEqual(len(set(venues)), 3)

    def test_sequential_replay_with_partial_prefix(self):
        # 逐席固定选“最后一段”，得到确定的先后；再只公开第一席前缀重放
        from cup.draw import _segment_layout
        feasible = enumerate_away_orders(self.orig)
        remaining = list(feasible)
        offsets = []
        expected = []
        for seat in range(3):
            layout = _segment_layout(self.orig, remaining, seat)
            seg = layout[-1]
            offsets.append(seg["lo"])
            expected.append(seg["away"])
            remaining = [o for o in remaining if o[seat] == seg["away"]]
        state = {"i": 0}

        def urand(mod):
            v = offsets[state["i"]]
            state["i"] += 1
            return v

        env = make_draw(self.orig, "s1", UniformSource(urand=urand), "sequential")
        self.assertEqual(env["away_order"], expected)
        # 只公开第一席前缀：重放完整重建整张，但只报露出的第一席
        rep = replay_draw(self.orig, env, trace_prefix=[(0, expected[0])])
        self.assertEqual(rep["revealed_seats"], 1)
        self.assertEqual(rep["revealed_order"], expected[:1])
        self.assertEqual(rep["legit_ranges"][0], [0, 3])
        # 第二席机会由目录按第一席前缀重算
        self.assertEqual(rep["opportunities"][1]["fractions"],
                         seat_opportunities_of(self.orig, expected[:1], 1))

    def test_replay_rejects_wrong_declared_range(self):
        env = make_draw(self.orig, "t0", forced_source(0), "table")
        bad = dict(env)
        bad["raw"] = {"draws": [{"lo": 0, "hi": 5, "range": 6, "value": 0}]}
        with self.assertRaises(DrawError):
            replay_draw(self.orig, bad)

    def test_replay_rejects_version_mismatch(self):
        env = make_draw(self.orig, "t0", forced_source(0), "table")
        other = build_catalog("other", dict(self.snap, seq_floor=self.snap["seq_floor"] + 1),
                              ORIGINAL_VENUES)
        with self.assertRaises(DrawError):
            replay_draw(other, env)


class TestPowerCutHandover(DrawTestBase):
    def test_sequential_effective_draw_after_one_reveal(self):
        env = make_draw(self.orig, "pc", UniformSource(urand=lambda m: 0),
                        "sequential")
        # 已揭 1 席并公开该前缀：接班人看到的是第 1 次抽数已生效
        st = seat_state_after_prefix(self.orig, env, 1,
                                     revealed_prefix=[(0, env["away_order"][0])],
                                     draws_seen=env["raw"]["draws"][:1])
        self.assertEqual(len(st["effective_draws"]), 1)
        self.assertEqual(st["effective_draws"][0]["tag"], "seat_1")
        self.assertEqual(st["effective_ranges"], [[0, 3]])
        # 下一席机会由目录按第一席前缀重算
        self.assertEqual(st["next_seat_opportunities"]["fractions"],
                         seat_opportunities_of(self.orig, env["away_order"][:1], 1))

    def test_cannot_reroll_on_prefix_mismatch(self):
        env = make_draw(self.orig, "pc", UniformSource(urand=lambda m: 0),
                        "sequential")
        wrong = [(0, "己")] if env["away_order"][0] != "己" else [(0, "丁")]
        with self.assertRaises(DrawError):
            seat_state_after_prefix(self.orig, env, 1, revealed_prefix=wrong,
                                    draws_seen=env["raw"]["draws"][:1])

    def test_table_effective_row_after_one_reveal(self):
        env = make_draw(self.orig, "tb", forced_source(2), "table")
        st = seat_state_after_prefix(self.orig, env, 1,
                                     revealed_prefix=[(0, env["away_order"][0])],
                                     draws_seen=env["raw"]["draws"])
        # 表模式封箱时一次抽定同一行号，已揭一席后仍是同一抽数
        self.assertEqual(st["effective_draws"][0]["value"], 2)
        self.assertEqual(len(st["effective_draws"]), 1)


class TestNoDraw(DrawTestBase):
    def test_no_complete_draw_means_no_lottery(self):
        specs = [dict(v) for v in ORIGINAL_VENUES]
        specs[1] = {"venue_id": "VM乙", "kind": "primary", "host": "乙", "receives": []}
        specs[3] = {"venue_id": "VN1", "kind": "neutral", "host": None, "receives": []}
        cat = build_catalog("empty", self.snap, specs)
        env = make_draw(cat, "none", UniformSource(seed=9), "table")
        self.assertEqual(env["status"], "no_draw")
        self.assertIsNone(env["away_order"])
        self.assertIsNone(env["raw"])
        # 没消耗任何抽数
        rep = replay_draw(cat, env)
        self.assertEqual(rep["status"], "no_draw")
        self.assertTrue(rep["agree_no_draw"])


class TestSeatOpportunitiesTrail(DrawTestBase):
    def test_each_seat_opportunities_match_replay(self):
        env = make_draw(self.orig, "o", forced_source(0), "table")
        rep = replay_draw(self.orig, env)
        for seat in range(3):
            self.assertEqual(env["opportunities"][seat]["fractions"],
                             rep["opportunities"][seat]["fractions"])


if __name__ == "__main__":
    unittest.main()
