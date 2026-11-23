"""从旧仓（赛程 edition + 比分事实）派生“待洗布件”。

规则（严格对应题面）
====================
1. 按**实际发布**的 ``edition`` 取比赛与主客方，不从队名硬猜。
2. 只有 ``state == 'played'`` 的比赛，其对应主/客方所领、且赛后仍在运动员手里的
   布件才变成待洗，并叠加该场的使用种类（usage）约束。
3. ``cancelled`` 场只退回未用件：**不产生待洗件**；``pending`` 同样不产生。
   因此把某场改报取消，会从待洗集合中移除该场本方布件——这正是“旧仓结果改变
   待洗件”的可观测效果。
4. 具体某场比赛某方领了哪些布件，由 :class:`Issuance` 显式登记，不按尺码跨队顶替。
5. 同一布件若出现在多场 played 中，其约束取交集累积（磨耗/材料/内衬在
   ``EquipmentLedger``，用法逐场叠加）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .model import Constraint, EquipmentLedger, Item


@dataclass(frozen=True)
class Issuance:
    """某场比赛某一方（主/客）实际领用的一组布件及本场用法约束。"""

    match_id: str
    side: str            # 'home' / 'away'
    item_ids: tuple[str, ...]
    usage: Constraint    # 本场用法：其 allowed 即该场允许的程序集合


class IssuanceBook:
    """按 (match_id, side) 登记的领用记录。"""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], Issuance] = {}

    def record(self, issuance: Issuance) -> None:
        if issuance.side not in ("home", "away"):
            raise ValueError(f"side 必须是 home/away，收到 {issuance.side!r}")
        key = (issuance.match_id, issuance.side)
        if key in self._by_key:
            raise ValueError(f"{key} 已登记领用记录，不允许覆盖")
        self._by_key[key] = issuance

    def get(self, match_id: str, side: str) -> Issuance | None:
        return self._by_key.get((match_id, side))

    def all(self) -> list[Issuance]:
        return list(self._by_key.values())


@dataclass
class ToWashResult:
    """一次派生：待洗布件、被排除的场次与原因、以及无法洗的布件。"""

    items: dict[str, Item] = field(default_factory=dict)
    reasons: list[dict] = field(default_factory=list)
    infeasible: list[str] = field(default_factory=list)

    def item_ids(self) -> list[str]:
        return sorted(self.items)


def derive_to_wash(
    edition_matches: Sequence[Mapping],
    results: Mapping[str, Mapping],
    issuance: IssuanceBook,
    ledger: EquipmentLedger,
) -> ToWashResult:
    """根据某 edition 的比赛 + 比分事实切面派生待洗集合。

    参数
    ----
    edition_matches:
        赛程服务 ``get_edition(edition_id)['matches']`` 或
        ``query_round`` 的行，含 ``match_id/home/away/round``。
    results:
        ``match_id -> {state, ...}``（通常来自 ``StandingsRepository.active_results``）。
    """
    out = ToWashResult()
    # 累积每件的约束，最后再构造成 Item（同一件可能被多场引用）
    accum: dict[str, list[Constraint]] = {}

    for fx in edition_matches:
        mid = fx["match_id"]
        res = results.get(mid)
        state = None if res is None else res.get("state")
        if state != "played":
            out.reasons.append({
                "match_id": mid, "state": state,
                "effect": "excluded_from_to_wash",
                "detail": ("取消场只退回未用件，不产生待洗" if state == "cancelled"
                           else "未开打/无结果，不产生待洗"),
            })
            continue
        for side in ("home", "away"):
            resolved_team = fx[side]
            iss = issuance.get(mid, side)
            if iss is None:
                out.reasons.append({
                    "match_id": mid, "side": side, "team": resolved_team,
                    "state": state, "effect": "no_issuance_registered",
                    "detail": "该场该方无领用记录",
                })
                continue
            for iid in iss.item_ids:
                info = ledger.info(iid)
                if info.team != resolved_team:
                    # 不接受把另一队同尺码的衣服顶上；错误归属件驳回，不进入待洗
                    out.reasons.append({
                        "match_id": mid, "side": side, "team": resolved_team,
                        "state": state, "effect": "wrong_team_issuance",
                        "item_id": iid, "item_team": info.team,
                        "detail": f"布件 {iid} 属于 {info.team}，不能顶替该场 "
                                  f"{side} 方 {resolved_team} 的装备",
                    })
                    continue
                accum.setdefault(iid, []).extend(
                    ledger.item_constraints(iid) + [iss.usage])

    for iid, cons in accum.items():
        # 约束去重（同 source+allowed+note 只保留一次），保持稳定顺序
        seen: set[tuple] = set()
        uniq: list[Constraint] = []
        for c in cons:
            key = (c.source, tuple(sorted(c.allowed)), c.note)
            if key not in seen:
                seen.add(key)
                uniq.append(c)
        item = Item(ledger.info(iid), uniq)
        out.items[iid] = item
        if not item.feasible:
            out.infeasible.append(iid)
    out.infeasible.sort()
    return out
