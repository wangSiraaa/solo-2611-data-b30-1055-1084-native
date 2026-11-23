"""端到端演示：录入真实 2:2，逐情形核对交班、判定遮幕、回放每个止位。

运行：python3 demo.py
产物：run_data/ 下每个情形一份独立现场记录 JSON（可删后重建）。
"""

from __future__ import annotations

import json
import os
import shutil

from scoreboard.cards import OperationCard
from scoreboard.controller import decide
from scoreboard.reconcile import reconcile
from scoreboard.replay import replay_all
from scoreboard.scenarios import (
    EDITION_V1,
    EDITION_V2,
    MATCH_ID,
    ALL_SCENARIOS,
    build_office,
    conflicting_records,
)
from scoreboard.planner import plan

HERE = os.path.dirname(os.path.abspath(__file__))
RUN_DIR = os.path.join(HERE, "run_data")


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def main() -> None:
    if os.path.exists(RUN_DIR):
        shutil.rmtree(RUN_DIR)
    os.makedirs(RUN_DIR)

    office = build_office()
    order = office.issue_order(MATCH_ID, EDITION_V1)
    print(f"录入赛果 {MATCH_ID} 真实 2:2；v1 换牌单目标（按实物牌号）"
          f"{order.target.key()}")

    for builder in ALL_SCENARIOS:
        s = builder()
        banner(s.name)
        result = reconcile(list(s.records))
        print("可能联合止位：", result.stop_keys() if result.feasible
              else f"矛盾：{result.conflict_reason}")
        decision = decide(result, s.target, s.curtain_beats)
        print(f"遮幕 {s.curtain_beats} 拍判定：{decision.reason}")

        record = replay_all(list(s.records), s.target, s.curtain_beats)
        for replay in record["replays"]:
            print(f"  止位 {tuple(replay['stop'])} 回放："
                  f"{replay['beats_used']} 拍 -> {replay['final_digits']} "
                  f"达标={replay['reached_target']}")
        fname = s.name.split()[0] + ".json"
        with open(os.path.join(RUN_DIR, fname), "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------ 矛盾交班
    banner("F 矛盾交班单：定位不能同时成立的记录")
    bad = reconcile(conflicting_records())
    print(bad.conflict_reason)
    with open(os.path.join(RUN_DIR, "F-conflict.json"), "w",
              encoding="utf-8") as f:
        json.dump(replay_all(conflicting_records(), None, 6), f,
                  ensure_ascii=False, indent=2)

    # ------------------------------------------------------------ 主客换版与待赛
    banner("主客换版与待赛拒播")
    swapped = office.issue_order(MATCH_ID, EDITION_V2)
    print(f"同一 2:2 按 v2 映射的目标：{swapped.target.key()}（主槽改坐 B 鼓）")
    try:
        office.issue_order("m-pending", EDITION_V1)
    except Exception as exc:  # noqa: BLE001 - 演示需展示拒绝原因
        print(f"待赛播报被拒：{exc}")

    # ------------------------------------------------------------ 操作卡离线交付
    banner("操作卡离线交付（情形 B，三拍卡）")
    s = ALL_SCENARIOS[1]()
    result = reconcile(list(s.records))
    p = plan(result.stops, s.target, 3)
    card = OperationCard.build(order, p, 3)
    path = os.path.join(RUN_DIR, "card-B-3beats.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write(card.to_json())
    print(f"操作卡已签：{card.header}，页码 {sorted(card.pages)}，落盘 {path}")

    print(f"\n现场记录目录：{RUN_DIR}")


if __name__ == "__main__":
    main()
