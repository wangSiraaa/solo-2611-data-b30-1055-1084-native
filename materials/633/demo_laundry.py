"""洗衣规划子系统端到端演示（对接旧仓 edition + 比分事实）。

覆盖题面要求：
1. A 队主场领衣 x（只准温洗）与套 y（温/快），打完都要还；0 点唯一温洗筒
   容量 2、历时 2、截止 2 —— 整套可交。
2. 装备账登记 y 补上“只准快洗”的内衬 -> 这车交不齐（并给理由）。
3. 回到原许可，把温洗容量改为 1 -> 交不齐。
4. 补好内衬的两件同袋退回；温洗、快洗各一筒；证实封车前必须拆开原混袋才能
   齐套（同回条已封袋则非法）。
5. 同一比赛改报取消，旧仓事实切面变化 -> 待洗件随之改变。
6. 两法（方案先行 / 实筒先行）、坏筒失效、已开机后重开。

运行：python3 demo_laundry.py
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile

from league.schedule_service import ScheduleService
from league.standings_repo import StandingsRepository

from laundry.model import Bag, Constraint, Drum, EquipmentLedger, ItemInfo
from laundry.solver import (WashProblem, drum_first, invalidated_schemes,
                            plan, replan, scheme_first)
from laundry.towash import Issuance, IssuanceBook, derive_to_wash
from laundry.service import WashRequest, plan_request

HERE = os.path.dirname(os.path.abspath(__file__))


def banner(t):
    print("\n" + "=" * 74)
    print(t)
    print("=" * 74)


def ledger_A(y_extra=()):
    led = EquipmentLedger()
    led.register(ItemInfo("x", "A", "home", "jersey", "M"))
    led.register(ItemInfo("y", "A", "home", "set", "M"))
    led.add_constraint("x", Constraint("wear-x", {"warm"}, "实际磨耗只准温洗"))
    led.add_constraint("y", Constraint("wear-y", {"warm", "quick"}, "套可温/快"))
    for c in y_extra:
        led.add_constraint("y", c)
    return led


def problem_A(led, drums, deadline=2, bags=None, sealed=None):
    return WashProblem(
        items={"x": led.build_item("x"), "y": led.build_item("y")},
        bags=bags or {"bx": ["x"], "by": ["y"]},
        drums=drums, deadline=deadline, sealed=sealed or {})


def show(sol):
    print("  可行:", sol.feasible)
    if sol.feasible:
        s = sol.schemes[0]
        for did, ids in sorted(s.drum_loads.items()):
            print(f"   筒 {did}: {sorted(ids)}  ({s.assignments[ids[0]].program},"
                  f" 完成 {s.assignments[ids[0]].result_time})")
        for op in s.bag_ops:
            print(f"   袋 {op.target_bag} <- {op.source_bags} {op.action} {op.item_ids}"
                  f" -> {op.drum_id}")
    else:
        for r in sol.reasons:
            print("   理由:", r.get("code"), "-", r.get("message"))


def main():
    banner("A 基准：0 点唯一温洗筒，容量 2、历时 2、截止 2")
    led = ledger_A()
    base_drums = [Drum("w0", "warm", 0, 2, 2)]
    print("x 允许:", sorted(led.build_item("x").allowed),
          " y 允许:", sorted(led.build_item("y").allowed))
    show(plan(problem_A(led, base_drums)))

    banner("2. y 补上只准快洗的内衬 -> 这车交不齐")
    led_liner = ledger_A([Constraint("liner", {"quick"}, "补好的内衬只准快洗")])
    print("y 允许变为:", sorted(led_liner.build_item("y").allowed))
    show(plan(problem_A(led_liner, base_drums)))

    banner("3. 回到原许可，温洗容量改为 1 -> 交不齐")
    show(plan(problem_A(led, [Drum("w0", "warm", 0, 1, 2)])))

    banner("4. 补内衬的两件同袋退回；温/快各一筒：封车前必须拆开原混袋")
    drums = [Drum("w0", "warm", 0, 2, 2), Drum("q0", "quick", 0, 2, 1)]
    open_prob = problem_A(led_liner, drums, bags={"mixed": ["x", "y"]})
    print("[开放混袋] 可拆袋再装:")
    show(plan(open_prob))
    sealed_prob = problem_A(led_liner, drums, bags={"mixed": ["x", "y"]},
                            sealed={"mixed": "SLIP-1"})
    print("\n[同一回条已封袋] 不能在更衣室拆:")
    show(plan(sealed_prob))

    banner("5. 两法：方案先行 vs 实筒先行；坏筒失效哪些案")
    led2 = ledger_A()
    two_drums = [Drum("w0", "warm", 0, 2, 2), Drum("w1", "warm", 1, 2, 2)]
    p_two = problem_A(led2, two_drums, deadline=10)
    sf = scheme_first(p_two, limit=8)
    print("方案先行：互斥袋案", [s.scheme_id for s in sf.schemes])
    print("  若 w0 封车前坏了 -> 失效:", invalidated_schemes(sf.schemes, "w0"))
    print("  若 w1 封车前坏了 -> 失效:", invalidated_schemes(sf.schemes, "w1"))
    df = drum_first(p_two, limit=8)
    print("实筒先行：可封袋案", [s.scheme_id for s in df.schemes])

    banner("6. 某筒已开机、回报未到：重开接着办，不重洗/不同时许两队")
    r = replan(p_two, "w0", {"x": "w0"}, available_drums=[two_drums[1]])
    print("重开可行:", r.feasible)
    if r.feasible:
        s = r.schemes[0]
        for iid, a in sorted(s.assignments.items()):
            print(f"   {iid} -> {a.drum_id}（{a.program}，完成 {a.result_time}）")
        ids = [a.item_id for a in s.assignments.values()]
        print("   各件仅一处:", len(ids) == len(set(ids)))

    banner("7. 旧仓联动：played -> 待洗；同一比赛改报取消 -> 待洗改变")
    tmp = tempfile.mkdtemp()
    try:
        sdb = os.path.join(tmp, "s.db")
        rdb = os.path.join(tmp, "r.db")
        svc = ScheduleService(sdb)
        with open(os.path.join(HERE, "case.json"), encoding="utf-8") as f:
            case = json.load(f)
        reg = {m["match_id"]: m["round"] for m in case["before_fixture"]}
        svc.initialize(case["clubs"], reg,
                       {r: {"s": case["slot_capacity"]}
                        for r in sorted(set(reg.values()))},
                       {t: [] for t in case["clubs"]})
        ed = svc.publish(case["before_fixture"])["edition_id"]
        repo = StandingsRepository(rdb, sdb)
        repo.apply_result_batch("init", [
            {"result_id": f"r-{r['match_id']}", "match_id": r["match_id"],
             "state": "played", "score_home": r["home_goals"],
             "score_away": r["away_goals"]}
            for r in case["scores"]])

        led3 = EquipmentLedger()
        for iid in ("x", "y"):
            led3.register(ItemInfo(iid, "A", "home", "jersey", "M"))
            led3.add_constraint(iid, Constraint("wear", {"warm"}))
        book = IssuanceBook()
        book.record(Issuance("ab", "home", ("x", "y"),
                             Constraint("usage-ab", {"warm"})))
        req = WashRequest(edition_id=ed,
                          drums=[Drum("w0", "warm", 0, 2, 2)], deadline=2,
                          bags={"bx": ["x"], "by": ["y"]})
        tw, sol = plan_request(svc, repo, book, led3, req)
        print("ab played 待洗:", tw.item_ids(), "可行:", sol.feasible)
        repo.apply_result_batch("cancel-ab", [
            {"result_id": "r-ab", "match_id": "ab", "state": "cancelled"}])
        tw2, sol2 = plan_request(svc, repo, book, led3, req)
        print("ab 改报取消后待洗:", tw2.item_ids(),
              "（原因:", [r["effect"] for r in tw2.reasons], "）")
        repo.close()
        svc.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n演示完成。")


if __name__ == "__main__":
    main()
