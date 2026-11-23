"""洗衣子系统测试：约束交集、待洗派生、两法、失效、重开与旧仓联动。

所有可行性断言都与 :mod:`laundry.oracle`（独立枚举）双向对照，
不依赖待测搜索代码本身给出结论。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from league.schedule_service import ScheduleService
from league.standings_repo import StandingsRepository

from laundry.model import (BAG_DONE, BAG_SEALED, BAG_WASHING, Bag, Constraint,
                           Drum, EquipmentLedger, ItemInfo, PROGRAMS,
                           machine_conflicts)
from laundry.oracle import enumerate_feasible, is_feasible
from laundry.solver import (WashProblem, drum_first, invalidated_schemes,
                            item_program_report, plan, replan, scheme_first)
from laundry.towash import Issuance, IssuanceBook, derive_to_wash
from laundry.service import WashRequest, plan_request, replan_request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ledger(items):
    """items: (item_id, team, side, kind, size, [allowed sets...])。"""
    led = EquipmentLedger()
    for iid, team, side, kind, size, sets in items:
        led.register(ItemInfo(iid, team, side, kind, size))
        for n, allow in enumerate(sets):
            led.add_constraint(iid, Constraint(f"c{n}", set(allow)))
    return led


class TestConstraintIntersection(unittest.TestCase):
    def test_each_constraint_gives_allowed_set_and_intersection(self):
        led = _ledger([
            ("y", "A", "home", "set", "M",
             [{"warm", "quick"}, {"quick"}]),  # liner quick-only
        ])
        y = led.build_item("y")
        self.assertEqual(y.allowed, {"quick"})
        self.assertTrue(y.feasible)

    def test_empty_intersection_is_unsendable(self):
        led = _ledger([
            ("z", "A", "home", "set", "M",
             [{"warm"}, {"quick"}]),  # warm-only and quick-only conflict
        ])
        z = led.build_item("z")
        self.assertEqual(z.allowed, frozenset())
        self.assertFalse(z.feasible)
        prob = WashProblem(items={"z": z}, bags={}, drums=[], deadline=10)
        allowed, reasons = item_program_report(prob)
        self.assertEqual(reasons[0]["code"], "no_allowed_program")
        # 即使没有任何下一场，也可据此驳回错误送洗
        self.assertFalse(plan(prob).feasible)

    def test_default_program_space_is_warm_quick(self):
        led = _ledger([("t", "A", "away", "jersey", "L", [])])
        self.assertEqual(led.build_item("t").allowed, PROGRAMS)


class TestToWashDerivation(unittest.TestCase):
    def setUp(self):
        self.led = _ledger([
            ("x", "A", "home", "jersey", "M", [{"warm"}]),
            ("y", "A", "home", "set", "M", [{"warm", "quick"}]),
        ])
        self.book = IssuanceBook()
        self.book.record(Issuance("ab", "home", ("x", "y"),
                                  Constraint("usage-ab-home", {"warm"})))
        self.matches = [
            {"match_id": "ab", "round": 1, "home": "A", "away": "B", "slot": "s"},
        ]

    def test_played_creates_to_wash_with_usage(self):
        res = derive_to_wash(self.matches,
                             {"ab": {"state": "played"}},
                             self.book, self.led)
        self.assertEqual(res.item_ids(), ["x", "y"])
        self.assertEqual(res.items["x"].allowed, {"warm"})

    def test_cancelled_only_returns_unused(self):
        res = derive_to_wash(self.matches,
                             {"ab": {"state": "cancelled"}},
                             self.book, self.led)
        self.assertEqual(res.item_ids(), [])
        self.assertEqual(res.reasons[0]["effect"], "excluded_from_to_wash")

    def test_pending_creates_nothing(self):
        res = derive_to_wash(self.matches, {"ab": {"state": "pending"}},
                             self.book, self.led)
        self.assertEqual(res.item_ids(), [])

    def test_reported_home_side_matters(self):
        # 只在 home 登记；away 无领用 -> 不产生待洗
        self.assertEqual(
            derive_to_wash(self.matches, {"ab": {"state": "played"}},
                           self.book, self.led).item_ids(), ["x", "y"])

    def test_wrong_team_issuance_rejected_not_substituted(self):
        # 另队 B 同尺码布件被错误登记到 A 的主场领用 -> 驳回，不进入待洗
        led = _ledger([
            ("x", "A", "home", "jersey", "M", [{"warm"}]),
            ("b1", "B", "home", "jersey", "M", [{"warm"}]),
        ])
        book = IssuanceBook()
        book.record(Issuance("ab", "home", ("x", "b1"),
                             Constraint("usage", {"warm"})))
        res = derive_to_wash(self.matches, {"ab": {"state": "played"}},
                             book, led)
        self.assertEqual(res.item_ids(), ["x"])
        self.assertIn("wrong_team_issuance",
                      {r["effect"] for r in res.reasons})


class TestA_scenarios(unittest.TestCase):
    """题面给定的 A 队场景。"""

    def _base(self, y_sets):
        led = _ledger([
            ("x", "A", "home", "jersey", "M", [{"warm"}]),
            ("y", "A", "home", "set", "M", y_sets),
        ])
        return led

    def test_base_one_warm_drum_cap2_fits_whole_set(self):
        led = self._base([{"warm", "quick"}])
        prob = WashProblem(
            items={"x": led.build_item("x"), "y": led.build_item("y")},
            bags={"b0": ["x"], "b1": ["y"]},
            drums=[Drum("w0", "warm", 0, 2, 2)], deadline=2)
        sol = plan(prob)
        self.assertTrue(sol.feasible)
        self.assertTrue(sol.schemes[0].complete)
        self.assertEqual(sol.schemes[0].drum_loads["w0"], ["x", "y"])
        # 与 oracle 一致
        self.assertTrue(is_feasible(prob))

    def test_quick_only_liner_makes_whole_undeliverable(self):
        led = self._base([{"warm", "quick"}, {"quick"}])  # liner -> quick only
        prob = WashProblem(
            items={"x": led.build_item("x"), "y": led.build_item("y")},
            bags={"b0": ["x"], "b1": ["y"]},
            drums=[Drum("w0", "warm", 0, 2, 2)], deadline=2)
        sol = plan(prob)
        self.assertFalse(sol.feasible)
        self.assertIn("no_drum_for_program",
                      {r["code"] for r in sol.reasons})
        self.assertFalse(is_feasible(prob))

    def test_warm_capacity_one_short(self):
        led = self._base([{"warm", "quick"}])
        prob = WashProblem(
            items={"x": led.build_item("x"), "y": led.build_item("y")},
            bags={"b0": ["x"], "b1": ["y"]},
            drums=[Drum("w0", "warm", 0, 1, 2)], deadline=2)
        sol = plan(prob)
        self.assertFalse(sol.feasible)
        self.assertFalse(is_feasible(prob))

    def test_mixed_bag_must_split_before_sealing(self):
        led = self._base([{"warm", "quick"}, {"quick"}])
        x, y = led.build_item("x"), led.build_item("y")
        drums = [Drum("w0", "warm", 0, 2, 2), Drum("q0", "quick", 0, 2, 1)]
        # 开放混袋：可拆 -> 可行
        open_prob = WashProblem(items={"x": x, "y": y},
                                bags={"mixed": ["x", "y"]},
                                drums=drums, deadline=2)
        self.assertTrue(plan(open_prob).feasible)
        sol = plan(open_prob)
        loads = sol.schemes[0].drum_loads
        self.assertEqual(set(loads["w0"]), {"x"})
        self.assertEqual(set(loads["q0"]), {"y"})
        # 已封同回条袋：不可拆 -> 不可行
        sealed_prob = WashProblem(items={"x": x, "y": y},
                                  bags={"mixed": ["x", "y"]},
                                  drums=drums, deadline=2,
                                  sealed={"mixed": "SLIP-1"})
        bad = plan(sealed_prob)
        self.assertFalse(bad.feasible)
        self.assertIn("sealed_bag_no_common_program",
                      {r["code"] for r in bad.reasons})
        # oracle 也判不可行
        self.assertFalse(is_feasible(sealed_prob))


class TestBothModes(unittest.TestCase):
    def _problem(self, drums, deadline=10):
        led = _ledger([
            ("x", "A", "home", "jersey", "M", [{"warm"}]),
            ("y", "A", "home", "set", "M", [{"warm", "quick"}]),
            ("q", "B", "home", "jersey", "M", [{"quick"}]),
        ])
        return WashProblem(
            items={"x": led.build_item("x"), "y": led.build_item("y"),
                   "q": led.build_item("q")},
            bags={"bx": ["x"], "by": ["y"], "bq": ["q"]},
            drums=drums, deadline=deadline)

    def test_scheme_first_lists_mutually_exclusive_legal_schemes(self):
        prob = self._problem([Drum("d0", "warm", 0, 2, 2),
                              Drum("d1", "quick", 0, 2, 1)])
        sol = scheme_first(prob, limit=8)
        self.assertTrue(sol.feasible)
        self.assertGreaterEqual(len(sol.schemes), 1)
        for s in sol.schemes:
            self.assertTrue(s.complete)
            # 各件只在一处
            ids = [a.item_id for a in s.assignments.values()]
            self.assertEqual(len(ids), len(set(ids)))

    def test_scheme_first_failed_drum_invalidation(self):
        prob = self._problem([Drum("d0", "warm", 0, 2, 2),
                              Drum("d1", "quick", 0, 2, 1)])
        sol = scheme_first(prob, limit=8)
        bad = invalidated_schemes(sol.schemes, "d1")
        for s in sol.schemes:
            if "d1" in s.used_drums():
                self.assertIn(s.scheme_id, bad)
            else:
                self.assertNotIn(s.scheme_id, bad)

    def test_drum_first_no_cross_drum_collision(self):
        prob = self._problem([Drum("d0", "warm", 0, 2, 2),
                              Drum("d1", "quick", 0, 2, 1)])
        sol = drum_first(prob, limit=8)
        self.assertTrue(sol.feasible)
        for s in sol.schemes:
            ids = [a.item_id for a in s.assignments.values()]
            self.assertEqual(len(ids), len(set(ids)))

    def _quick_only_problem(self, drums, deadline=10):
        led = _ledger([
            ("x", "A", "home", "jersey", "M", [{"quick"}]),
            ("y", "A", "home", "set", "M", [{"quick"}]),
        ])
        return WashProblem(items={"x": led.build_item("x"),
                                  "y": led.build_item("y")},
                           bags={"bx": ["x"], "by": ["y"]},
                           drums=drums, deadline=deadline)

    def test_drum_first_rejects_machine_time_overlap(self):
        # 同一台机器的两筒时间重叠 -> 不可执行
        drums = [Drum("d0", "quick", 0, 1, 5, machine_id="M1"),
                 Drum("d1", "quick", 1, 1, 5, machine_id="M1")]
        prob = self._quick_only_problem(drums)
        self.assertFalse(drum_first(prob).feasible)

    def test_drum_first_allows_disjoint_machine_times(self):
        drums = [Drum("d0", "quick", 0, 1, 1, machine_id="M1"),
                 Drum("d1", "quick", 1, 1, 1, machine_id="M1")]
        prob = self._quick_only_problem(drums)
        self.assertTrue(drum_first(prob).feasible)


class TestReplanAfterStart(unittest.TestCase):
    def _problem(self, drums):
        led = _ledger([
            ("x", "A", "home", "jersey", "M", [{"warm"}]),
            ("y", "A", "home", "set", "M", [{"warm"}]),
        ])
        return WashProblem(
            items={"x": led.build_item("x"), "y": led.build_item("y")},
            bags={"bx": ["x"], "by": ["y"]}, drums=drums, deadline=10)

    def test_replan_does_not_rewash_frozen_item(self):
        drums = [Drum("d0", "warm", 0, 2, 2), Drum("d1", "warm", 2, 2, 2)]
        prob = self._problem(drums)
        sol = replan(prob, "d0", {"x": "d0"}, available_drums=[drums[1]])
        self.assertTrue(sol.feasible)
        s = sol.schemes[0]
        self.assertEqual(s.assignments["x"].drum_id, "d0")  # 冻结，不重洗
        self.assertEqual(s.assignments["y"].drum_id, "d1")
        ids = [a.item_id for a in s.assignments.values()]
        self.assertEqual(len(ids), len(set(ids)))  # 不同时许给两队

    def test_replan_started_drum_late_flags_shortfall(self):
        drums = [Drum("d0", "warm", 0, 2, 20)]  # ends at 20 > deadline 10
        prob = self._problem(drums)
        sol = replan(prob, "d0", {"x": "d0"}, available_drums=drums)
        self.assertFalse(sol.feasible)
        self.assertIn("started_drum_late", {r["code"] for r in sol.reasons})


class TestOldRepoIntegration(unittest.TestCase):
    """端到端：旧仓 edition + 比分事实，改报取消改变待洗集合。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sdb = os.path.join(self.tmp.name, "s.db")
        self.rdb = os.path.join(self.tmp.name, "r.db")
        self.svc = ScheduleService(self.sdb)
        with open(os.path.join(HERE, "case.json"), encoding="utf-8") as f:
            self.case = json.load(f)
        c = self.case
        reg = {m["match_id"]: m["round"] for m in c["before_fixture"]}
        self.svc.initialize(c["clubs"], reg,
                            {r: {"s": c["slot_capacity"]}
                             for r in sorted(set(reg.values()))},
                            {t: list(c["blackouts"]) for t in c["clubs"]})
        self.ed = self.svc.publish(c["before_fixture"])["edition_id"]
        self.repo = StandingsRepository(self.rdb, self.sdb)
        entries = [
            {"result_id": f"r-{r['match_id']}", "match_id": r["match_id"],
             "state": "played", "score_home": r["home_goals"],
             "score_away": r["away_goals"]}
            for r in c["scores"]
        ]
        self.repo.apply_result_batch("init", entries)

        # A 主场在 ab 场领 x/y（ab 的 before home=A）；A 客场在 ac 场领 z
        fx_ab = self.svc.locate_match(self.ed, "ab")
        self.assertEqual(fx_ab["home"], "A")  # 确认 ab before 主队就是 A
        self.ledger = _ledger([
            ("x", "A", "home", "jersey", "M", [{"warm"}]),
            ("y", "A", "home", "set", "M", [{"warm", "quick"}]),
            ("z", "C", "home", "jersey", "M", [{"warm"}]),
        ])
        self.book = IssuanceBook()
        # ab 的 home 方（A）领 x,y
        self.book.record(Issuance("ab", "home", ("x", "y"),
                                  Constraint("usage-ab", {"warm"})))
        # ac before home=C 领 z
        self.book.record(Issuance("ac", "home", ("z",),
                                  Constraint("usage-ac", {"warm"})))

    def tearDown(self):
        self.repo.close()
        self.svc.close()
        self.tmp.cleanup()

    def test_played_then_cancelled_changes_to_wash(self):
        req = WashRequest(edition_id=self.ed,
                          drums=[Drum("w0", "warm", 0, 3, 2)],
                          deadline=2, bags={"bx": ["x"], "by": ["y"],
                                            "bz": ["z"]})
        towash, sol = plan_request(self.svc, self.repo, self.book,
                                   self.ledger, req)
        self.assertEqual(towash.item_ids(), ["x", "y", "z"])
        self.assertTrue(sol.feasible)

        # 把 ab 改报取消（同一比赛，不同 request 批次）
        self.repo.apply_result_batch("cancel-ab", [
            {"result_id": "r-ab", "match_id": "ab", "state": "cancelled"}])
        towash2, _ = plan_request(self.svc, self.repo, self.book,
                                  self.ledger, req)
        # ab 取消 -> x/y 不再是待洗；ac 的 z 仍在
        self.assertEqual(towash2.item_ids(), ["z"])
        self.assertIn("ab", {r["match_id"] for r in towash2.reasons})

    def test_edition_drives_home_side(self):
        # ab before home=A；若该场 side=home 的领用按 edition 解析就是 A
        fx = self.svc.locate_match(self.ed, "ab")
        self.assertEqual(fx["home"], "A")


class TestBagAndMachineRules(unittest.TestCase):
    def test_open_bag_splittable_sealed_by_same_slip_not(self):
        b = Bag("b", ["x", "y"])
        self.assertTrue(b.can_split())
        b.state = BAG_SEALED
        b.return_slip = "SLIP-1"
        self.assertFalse(b.can_split("SLIP-1"))   # 同一回条不能在更衣室拆
        self.assertTrue(b.can_split("SLIP-NEW"))  # 新回条重新处理可行
        self.assertFalse(b.can_split())           # 无回条也视为不可拆

    def test_washing_bag_cannot_split_in_machine(self):
        for st in (BAG_WASHING, BAG_DONE):
            b = Bag("b", ["x"], state=st, return_slip="S")
            self.assertFalse(b.can_split("S"))
            self.assertFalse(b.can_split("OTHER"))

    def test_machine_conflict_detection(self):
        a = Drum("a", "warm", 0, 2, 5, machine_id="M1")
        b = Drum("b", "warm", 3, 2, 5, machine_id="M1")  # overlaps a (0..5)
        c = Drum("c", "warm", 5, 2, 5, machine_id="M1")  # starts at a.end
        self.assertEqual(machine_conflicts([a, b]), [("a", "b")])
        self.assertEqual(machine_conflicts([a, c]), [])
        # 不同机器不冲突
        d = Drum("d", "warm", 0, 2, 5, machine_id="M2")
        self.assertEqual(machine_conflicts([a, d]), [])


def _make_problem_random(seed):
    """确定性伪随机小实例，供 oracle 双向对照。"""
    import random
    rng = random.Random(seed)
    led = EquipmentLedger()
    n = rng.randint(1, 4)
    items = {}
    bags = {}
    for i in range(n):
        iid = f"i{i}"
        led.register(ItemInfo(iid, "A", "home", "jersey", "M"))
        allow = set(rng.choice([{"warm"}, {"quick"}, {"warm", "quick"}]))
        led.add_constraint(iid, Constraint("c", allow))
        items[iid] = led.build_item(iid)
        bags[f"b{i}"] = [iid]
    # 偶尔把两件合进同一袋，并可能封袋（同回条不可拆）或保持开放
    sealed = {}
    if n >= 2 and rng.random() < 0.5:
        merged = [f"i0", f"i1"]
        bags = {k: v for k, v in bags.items() if not set(v) & set(merged)}
        bags["merged"] = merged
        if rng.random() < 0.5:
            sealed["merged"] = "SLIP"
    drums = []
    for k in range(rng.randint(1, 2)):
        drums.append(Drum(f"d{k}", rng.choice(["warm", "quick"]),
                          rng.randint(0, 2), rng.randint(1, 3),
                          rng.randint(1, 2)))
    return WashProblem(items=items, bags=bags, drums=drums, deadline=5,
                       sealed=sealed)


class TestOracleCrossCheck(unittest.TestCase):
    def test_solver_matches_oracle_on_small_instances(self):
        for seed in range(60):
            prob = _make_problem_random(seed)
            oracle = is_feasible(prob)
            if oracle is None:
                continue
            sol = plan(prob)
            self.assertEqual(sol.feasible, oracle,
                             f"seed={seed} oracle={oracle} solver={sol.feasible}")

    def test_oracle_and_solver_agree_on_known_infeasible(self):
        led = _ledger([("a", "A", "home", "jersey", "M", [{"warm"}]),
                       ("b", "A", "home", "set", "M", [{"warm"}])])
        prob = WashProblem(items={"a": led.build_item("a"),
                                  "b": led.build_item("b")},
                           bags={"ba": ["a"], "bb": ["b"]},
                           drums=[Drum("w", "warm", 0, 1, 1)], deadline=1)
        self.assertFalse(is_feasible(prob))
        self.assertFalse(plan(prob).feasible)


if __name__ == "__main__":
    unittest.main()
