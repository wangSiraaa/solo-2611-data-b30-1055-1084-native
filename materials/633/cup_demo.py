"""六队杯抽签端到端演示：真实排名快照 → 馆单 → 完签核对 → 公证抽签与重放。

运行：python3 cup_demo.py
不依赖网络随机服务；抽签用固定小整数或 random.Random(seed) 作种子源。

依次演示：
1. 原排名快照：前三甲乙丙作固定主签，后三丁戊己作客队。
2. 原馆单四张完签，甲遇丁/戊/己 的机会 1/4、1/4、1/2。
3. 乙的真实补充馆单允许接己 -> 六签都能办，甲三对手各 1/3。
4. 仅接手已揭的“甲遇丁”前缀，乙就只能抽戊。
5. 两场都想用最后一座中立馆：各自能开、合起来不行（列可用馆与缺额依据）。
6. 判罚换掉一个种子；原排名快照仍可照查。
7. 逐席封箱抽签、凭证、独立重放、停电后接班（不许换号重摇）。
8. 一个完签都安不下 -> 不举行抽签。
"""

from __future__ import annotations

import json

from cup.model import (
    build_catalog,
    complete_draws,
    enumerate_away_orders,
    evaluate_pairing,
    recount_integrity,
    seat_opportunities,
)
from cup.draw import (
    DrawError,
    UniformSource,
    make_draw,
    replay_draw,
    seat_state_after_prefix,
)
from cup.scenario import (
    ORIGINAL_VENUES,
    SUPPLEMENTAL_VENUES,
    build_snapshots,
    contention_venues,
)


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show_opp(opp: dict) -> str:
    return "，".join(f"{a} {n}/{d}" for a, (n, d) in opp["fractions"].items())


def main() -> None:
    orig, pen = build_snapshots()

    banner("1. 原排名快照（真实名次，绑定 edition/seq 版本）")
    print(f"snapshot_id={orig['snapshot_id']} edition={orig['edition_id']}"
          f" seq_floor={orig['seq_floor']}")
    print("  名次：", orig["order"])
    print("  主签（前三）：", orig["promotion"]["teams"],
          "  客队（后三）：", orig["relegation"]["teams"])

    cat = build_catalog("原馆单", orig, ORIGINAL_VENUES)
    banner("2. 原馆单：核对完签，列每席可用馆")
    draws = complete_draws(cat)
    print(f"完签总数：{len(draws)}")
    for d in draws:
        line = " → ".join(
            f"{m['seed']}遇{m['away']}@{m['home_venue']}"
            f"(可选{d['available_venues'][i]})"
            for i, m in enumerate(d["matches"]))
        print(f"  {d['away_order']}  {line}")
    print("  甲三个对手机会：", show_opp(seat_opportunities(cat, [], 0)))

    catS = build_catalog("乙补充馆单", orig, SUPPLEMENTAL_VENUES)
    banner("3. 乙的真实补充馆单允许接己：六签都能办")
    drawsS = complete_draws(catS)
    print(f"完签总数：{len(drawsS)}")
    print("  甲三个对手机会：", show_opp(seat_opportunities(catS, [], 0)))

    banner("4. 仅接手已揭的“甲遇丁”前缀：乙只能抽戊")
    # 前缀用 0-based 席号：第 1 席（甲）已揭为 丁
    opp2 = seat_opportunities(cat, [(0, "丁")], seat=1)
    print(f"  第 2 席（乙）机会：{show_opp(opp2)}；相容完签：",
          [list(o) for o in enumerate_away_orders(cat) if o[0] == "丁"])

    banner("5. 两场都想用最后一座中立馆：各自能开、合起来不行")
    cont = build_catalog("中立馆争用", orig, contention_venues())
    ev = evaluate_pairing(cont, ["戊", "己", "丁"])
    print(f"  配对 {ev['away_order']} 可行？{ev['feasible']}")
    for m in ev["matches"]:
        print(f"    {m['seed']}遇{m['away']} 可选馆 {m['options']}")
    for d in ev["deficiencies"]:
        ms = [f"{m['seed']}遇{m['away']}" for m in d["matches"]]
        print(f"    缺额：{len(ms)} 场 {ms} 的可用馆并集只有 {d['union_venues']}"
              f"（需 {d['need']} 座）")
    print("  依据：", ev["reason"])

    banner("6. 判罚换掉一个种子；原排名快照仍可照查")
    print(f"  判罚快照 snapshot_id={pen['snapshot_id']} seq_floor={pen['seq_floor']}"
          f" 判罚={pen['penalties']}")
    print("  新名次：", pen["order"], "  新主签：", pen["promotion"]["teams"])
    print("  原排名快照仍为：", orig["order"],
          "（独立版本，snapshot_id 与判罚快照不同）")
    pen_cat = build_catalog("判罚版馆单", pen, ORIGINAL_VENUES)
    d0 = complete_draws(pen_cat)[0]
    print("  判罚版第一席由", pen_cat.seeds[0], "主持，比赛馆：",
          d0["matches"][0]["home_venue"])

    banner("7. 逐席封箱抽签：均匀整数、凭证、独立重放、停电接班")
    src = UniformSource(seed=20261123)
    env = make_draw(cat, "draw-1", src, mode="sequential")
    print("  抽数：", [(d['tag'], f"[{d['lo']},{d['hi']}]", d['value'])
                       for d in env["raw"]["draws"]])
    print("  抽中完签：", env["away_order"], " 编号：", env["order_number"])
    for m in env["matches"]:
        print(f"    {m['seat']}. {m['seed']}遇{m['away']} @ {m['venue']}"
              f"（客队驻地 {m['away_base']}）")
    rep = replay_draw(cat, env)
    print("  独立重放先后：", rep["away_order"], " 各席机会：")
    for o in rep["opportunities"]:
        print(f"    第{o['seat']}席（{o['seed']}）：{show_opp(o)}")

    # 停电：已揭第 1 席后接班
    st = seat_state_after_prefix(cat, env, 1,
                                 revealed_prefix=[(0, env["away_order"][0])],
                                 draws_seen=env["raw"]["draws"][:1])
    print("  停电后已揭：", st["revealed_prefix"])
    print("    已生效抽数：", st["effective_draws"], "合法范围：", st["effective_ranges"])
    print("    下一席机会：", show_opp(st["next_seat_opportunities"]))
    print("    接班人据此继续，不得换号重摇。")

    banner("8. 清点票本：同一完签不因换馆重复入册")
    rep2 = recount_integrity(cat, [list(o) for o in enumerate_away_orders(cat)])
    print("  登记张数：", rep2["registered"], " 去重后：", rep2["distinct_orders"],
          " 无重复：", rep2["duplicates"] == [],
          " 无遗漏：", rep2["missing"] == [])
    print("  每张签的场馆摆法数（多种摆法仍算一张）：")
    for x in rep2["venue_arrangement_counts"]:
        print(f"    {x['away_order']}：{x['arrangements']} 种")

    banner("9. 一个完签都安不下 -> 不举行抽签")
    empty_specs = [dict(v) for v in ORIGINAL_VENUES]
    empty_specs[1] = {"venue_id": "VM乙", "kind": "primary", "host": "乙",
                      "receives": []}
    empty_specs[3] = {"venue_id": "VN1", "kind": "neutral", "host": None,
                      "receives": []}
    empty = build_catalog("空馆单", orig, empty_specs)
    envE = make_draw(empty, "draw-empty", UniformSource(seed=1), mode="table")
    print("  可行完签：", enumerate_away_orders(empty))
    print("  结果：", envE["status"], "—", envE["reason"])
    print("  抽数消耗：", envE["raw"], "（未抽）")

    print("\n演示完成。")


if __name__ == "__main__":
    main()
