"""题给六队联赛 → 排名快照 → 杯签场馆目录的可复现场景。

沿用 ``tests/fixtures.py`` 的原六队单循环骨架（circle 法，5 轮 15 场）与固定成绩：
每场名次靠前者主场 1:0 胜名次靠后者。由此真实排名恰为
甲 > 乙 > 丙 > 丁 > 戊 > 己，前三作主签、后三作客队。

场馆目录（原馆单）：
* 甲、丙自用馆可接待三队；
* 乙自用馆只接丁、戊；
* 一座中立馆可被任一主签换用，原馆单只接丁、戊；
* 三个客队各报一座驻地馆（只表客队驻地，不是比赛场地）。

每座馆只办一场；一张完签只按三个客队先后辨认。
"""

from __future__ import annotations

import os
import tempfile

from league.schedule_service import ScheduleService
from league.standings_repo import StandingsRepository

from tests.fixtures import FIXTURE6, REGISTRY6, ROUND_SLOTS6, TEAMS6

# ---------------------------------------------------------------- 六队 → 甲乙丙丁戊己
# 原骨架中的球队名 T1..T6 对应真实名次，重命名只为贴合题面简称。
RENAME = {"T1": "甲", "T2": "乙", "T3": "丙", "T4": "丁", "T5": "戊", "T6": "己"}
TEAMS = [RENAME[t] for t in TEAMS6]
SEED_ORDER = TEAMS  # 甲 乙 丙 丁 戊 己
AWAY_ORDER = ["丁", "戊", "己"]


def renamed_fixture() -> list[dict]:
    return [{**r, "home": RENAME[r["home"]], "away": RENAME[r["away"]]}
            for r in FIXTURE6]


def fixed_scores() -> list[dict]:
    """名次靠前者 1:0 胜，按 fixture 的主客槽位给出槽位比分。"""
    rank = {t: i for i, t in enumerate(TEAMS)}
    scores = []
    for row in renamed_fixture():
        home, away = row["home"], row["away"]
        if rank[home] < rank[away]:
            gh, ga = 1, 0
        else:
            gh, ga = 0, 1
        scores.append({"match_id": row["match_id"],
                       "home_goals": gh, "away_goals": ga})
    return scores


def build_snapshots(penalty: dict[str, int] | None = None
                    ) -> tuple[dict, dict]:
    """在同一个排名仓里取“原排名”和“判罚后排名”两张快照。

    两份都从同一批固定比分事实出发，先取原快照，再追加判罚、取第二张快照；
    原快照之后原样可读回，用于“原排名解释可以照查”。``penalty`` 默认把
    甲扣 6 分，把甲从第一压到第二、种子变乙甲丙。
    """
    penalty = {"甲": 6} if penalty is None else penalty
    with tempfile.TemporaryDirectory() as tmp:
        sdb = os.path.join(tmp, "sched.db")
        rdb = os.path.join(tmp, "repo.db")
        svc = ScheduleService(sdb)
        svc.initialize(TEAMS, REGISTRY6, ROUND_SLOTS6, {t: [] for t in TEAMS})
        ed = svc.publish(renamed_fixture())["edition_id"]
        repo = StandingsRepository(rdb, sdb)
        entries = [
            {"result_id": f"r-{s['match_id']}", "match_id": s["match_id"],
             "state": "played", "score_home": s["home_goals"],
             "score_away": s["away_goals"]}
            for s in fixed_scores()
        ]
        repo.apply_result_batch("initial-results", entries)
        original = _snapshot_out(
            repo.standings("rank-original", ed, (1, 5),
                           promotion_places=3, relegation_places=3), {})
        events = [{"team": t, "action": "apply", "points": p}
                  for t, p in penalty.items()]
        repo.apply_penalty_batch("penalty-batch", events)
        penalised = _snapshot_out(
            repo.standings("rank-penalty", ed, (1, 5),
                           promotion_places=3, relegation_places=3), penalty)
        # 原快照之后仍原样读回（后续判罚未改写旧切面）
        reloaded = repo.get_snapshot(original["snapshot_id"])
        assert reloaded["order"] == original["order"]
        repo.close()
        svc.close()
        return original, penalised


def _snapshot_out(snap: dict, penalty: dict[str, int]) -> dict:
    return {
        "snapshot_id": snap["snapshot_id"],
        "edition_id": snap["edition_id"],
        "seq_floor": snap["seq_floor"],
        "order": snap["order"],
        "promotion": snap["promotion"],
        "relegation": snap["relegation"],
        "ranked": snap["ranked"],
        "penalties": dict(penalty),
    }


def build_snapshot(penalty: dict[str, int] | None = None) -> dict:
    """取一张排名快照。不给 ``penalty`` 时返回原排名；给了就走判罚变体。"""
    original, penalised = build_snapshots(penalty)
    return original if penalty is None else penalised


# ---------------------------------------------------------------- 场馆目录声明
def _away_bases() -> list[dict]:
    return [{"venue_id": f"V{a}", "kind": "away", "host": a, "receives": []}
            for a in AWAY_ORDER]


ORIGINAL_VENUES = [
    {"venue_id": "VM甲", "kind": "primary", "host": "甲", "receives": ["丁", "戊", "己"]},
    {"venue_id": "VM乙", "kind": "primary", "host": "乙", "receives": ["丁", "戊"]},
    {"venue_id": "VM丙", "kind": "primary", "host": "丙", "receives": ["丁", "戊", "己"]},
    {"venue_id": "VN1", "kind": "neutral", "host": None, "receives": ["丁", "戊"]},
    *_away_bases(),
]

# 乙的真实补充馆单：乙自用馆可接己（六签都能办，甲三对手各 1/3）
SUPPLEMENTAL_VENUES = [
    {"venue_id": "VM甲", "kind": "primary", "host": "甲", "receives": ["丁", "戊", "己"]},
    {"venue_id": "VM乙", "kind": "primary", "host": "乙", "receives": ["丁", "戊", "己"]},
    {"venue_id": "VM丙", "kind": "primary", "host": "丙", "receives": ["丁", "戊", "己"]},
    {"venue_id": "VN1", "kind": "neutral", "host": None, "receives": ["丁", "戊"]},
    *_away_bases(),
]


def contention_venues() -> list[dict]:
    """两场各自都能在同一中立馆开打、合起来不行：

    甲馆仅接丁，乙馆仅接丁，丙馆接三队；中立馆仅接戊、己。
    配对（甲-戊、乙-己、丙-丁）里，甲-戊与乙-己都只能去中立馆，
    两场都想用最后一座中立馆，合起来无解。
    """
    return [
        {"venue_id": "VM甲", "kind": "primary", "host": "甲", "receives": ["丁"]},
        {"venue_id": "VM乙", "kind": "primary", "host": "乙", "receives": ["丁"]},
        {"venue_id": "VM丙", "kind": "primary", "host": "丙", "receives": ["丁", "戊", "己"]},
        {"venue_id": "VN1", "kind": "neutral", "host": None, "receives": ["戊", "己"]},
        *_away_bases(),
    ]
