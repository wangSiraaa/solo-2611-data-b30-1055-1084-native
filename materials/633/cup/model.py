"""六队杯抽签的纯逻辑层：排名快照 → 固定主签位/客队、场馆目录、完签枚举与部分可行性。

设计要点
========
* 主签位按排名快照前三的真实名次固定：``seed_order = snapshot.order[:3]``，
  后三名是客队 ``away_order = snapshot.order[3:]``。目录必须与某个快照版本绑定，
  只接受“该快照的种子/客队”所对应的场馆，否则拒绝——排名、馆单都指回当时版本。
* 每个主签一座自用馆；中立馆可被任一主签换用；客队馆只表示客队在截止时的驻地，
  不是比赛场地。**一场比赛恰好占用一座馆，且每座馆只办一场**。
* 比赛 (主签 s, 客队 a) 能放在馆 V 当且仅当：
    - V 是 s 的自用馆且 V 的“可接待客队”含 a；或
    - V 是中立馆且其“可接待客队”含 a。
* 一张**完签**只按三个客队的先后辨认：给定客队的先后，只要存在至少一种
  互不重复的场馆摆法就可办，登记为一张。因此同一先后即使有多种场馆摆法，
  也只算一张（换馆不会把同签重复入册）。
* 本模块不抽签、不依赖随机；抽签在 :mod:`cup.draw`。
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations, permutations
from typing import Any, Iterable, Mapping, Sequence

VENUE_KINDS = ("primary", "neutral", "away")
_SEAT_COUNT = 3


class CatalogError(ValueError):
    """目录与排名快照版本不一致，或场馆声明不合法。"""


# --------------------------------------------------------------------- 数据类型
@dataclass(frozen=True)
class Venue:
    """一座场馆。

    ``kind == 'primary'``：``host`` 为该主签球队；
    ``kind == 'neutral'``：``host`` 为 ``None``，可被任一主签换用；
    ``kind == 'away'``：``host`` 为客队，仅表示客队在截止时的驻地，不办赛。
    ``receives`` 为可接待的客队集合（客队馆恒为空）。
    """

    venue_id: str
    kind: str
    host: str | None
    receives: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"venue_id": self.venue_id, "kind": self.kind,
                "host": self.host, "receives": list(self.receives)}


@dataclass(frozen=True)
class Catalog:
    """一份绑定到排名快照版本的场馆目录。"""

    catalog_id: str
    snapshot_ref: Mapping[str, Any]
    seeds: tuple[str, ...]
    aways: tuple[str, ...]
    venues: tuple[Venue, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "catalog_id": self.catalog_id,
            "snapshot_ref": dict(self.snapshot_ref),
            "seeds": list(self.seeds),
            "aways": list(self.aways),
            "venues": [v.to_dict() for v in self.venues],
        }

    # ---------------------------------------------------------------- 查询
    def primary(self, seed: str) -> Venue:
        for v in self.venues:
            if v.kind == "primary" and v.host == seed:
                return v
        raise KeyError(f"主签 {seed} 没有自用馆")

    def away_base(self, away: str) -> Venue:
        for v in self.venues:
            if v.kind == "away" and v.host == away:
                return v
        raise KeyError(f"客队 {away} 没有驻地馆")

    def neutrals(self) -> list[Venue]:
        return [v for v in self.venues if v.kind == "neutral"]

    def venue(self, venue_id: str) -> Venue:
        for v in self.venues:
            if v.venue_id == venue_id:
                return v
        raise KeyError(f"未知场馆 {venue_id}")


# --------------------------------------------------------------------- 目录构建
def build_catalog(
    catalog_id: str,
    snapshot_ref: Mapping[str, Any],
    venue_specs: Sequence[Mapping[str, Any]],
) -> Catalog:
    """从排名快照 + 场馆声明构建目录，并校验二者版本一致。

    ``snapshot_ref`` 至少含 ``order``（六队真实名次）与 ``snapshot_id``、
    ``edition_id``、``seq_floor`` 等版本字段。``venue_specs`` 每项形如
    ``{"venue_id", "kind", "host", "receives"}``。
    """
    order = list(snapshot_ref.get("order") or [])
    if len(order) != 6 or len(set(order)) != 6:
        raise CatalogError("snapshot_ref.order 必须是 6 支互异球队的真实名次")
    seeds = tuple(order[:_SEAT_COUNT])
    aways = tuple(order[_SEAT_COUNT:])

    venues: list[Venue] = []
    seen_ids: set[str] = set()
    for spec in venue_specs:
        vid = spec.get("venue_id")
        kind = spec.get("kind")
        host = spec.get("host")
        receives = tuple(spec.get("receives") or ())
        if not isinstance(vid, str) or not vid:
            raise CatalogError("场馆必须有非空 venue_id")
        if vid in seen_ids:
            raise CatalogError(f"场馆 id 重复：{vid}")
        seen_ids.add(vid)
        if kind not in VENUE_KINDS:
            raise CatalogError(f"场馆 {vid} kind={kind!r} 非法")
        if kind == "primary":
            if host not in seeds:
                raise CatalogError(f"自用馆 {vid} 的 host={host!r} 不是本版本前三主签")
        elif kind == "away":
            if host not in aways:
                raise CatalogError(f"客队馆 {vid} 的 host={host!r} 不是本版本后三客队")
            if receives:
                raise CatalogError(f"客队馆 {vid} 只表驻地，不应声明可接待客队")
        else:  # neutral
            if host is not None:
                raise CatalogError(f"中立馆 {vid} 不应绑定 host")
        unknown = [a for a in receives if a not in aways]
        if unknown:
            raise CatalogError(f"场馆 {vid} 声明接待了非本版本客队：{unknown}")
        venues.append(Venue(vid, kind, host, receives))

    primaries = {v.host for v in venues if v.kind == "primary"}
    away_bases = {v.host for v in venues if v.kind == "away"}
    missing_seed = [s for s in seeds if s not in primaries]
    if missing_seed:
        raise CatalogError(f"每个主签须恰有一座自用馆，缺：{missing_seed}")
    extra_seed = [s for s in primaries if sum(
        1 for v in venues if v.kind == "primary" and v.host == s) > 1]
    if extra_seed:
        raise CatalogError(f"主签自用馆多于一座：{extra_seed}")
    missing_base = [a for a in aways if a not in away_bases]
    if missing_base:
        raise CatalogError(f"每个客队须声明一座驻地馆，缺：{missing_base}")

    ref = {
        "snapshot_id": snapshot_ref.get("snapshot_id"),
        "edition_id": snapshot_ref.get("edition_id"),
        "seq_floor": snapshot_ref.get("seq_floor"),
        "order": order,
    }
    return Catalog(catalog_id, ref, seeds, aways, tuple(venues))


def assert_catalog_matches_snapshot(cat: Catalog, snapshot_ref: Mapping[str, Any]) -> None:
    """复核目录仍绑定同一快照版本；不一致抛 :class:`CatalogError`。"""
    order = list(snapshot_ref.get("order") or [])
    if order != list(cat.snapshot_ref["order"]):
        raise CatalogError("目录绑定的排名顺序与给定快照不一致")
    for key in ("snapshot_id", "edition_id", "seq_floor"):
        if snapshot_ref.get(key) is not None and cat.snapshot_ref.get(key) is not None:
            if snapshot_ref[key] != cat.snapshot_ref[key]:
                raise CatalogError(f"目录绑定的 {key} 与给定快照不一致")


# --------------------------------------------------------------------- 单场可用馆
def match_venues(cat: Catalog, seed: str, away: str) -> list[str]:
    """比赛 (seed, away) 实际可用的场馆（按 venue_id 稳定排序）。"""
    out: list[str] = []
    for v in cat.venues:
        if away not in v.receives:
            continue
        if v.kind == "primary" and v.host == seed:
            out.append(v.venue_id)
        elif v.kind == "neutral":
            out.append(v.venue_id)
    return sorted(out)


def _options_by_match(cat: Catalog, away_order: Sequence[str]) -> list[list[str]]:
    return [match_venues(cat, cat.seeds[i], away_order[i]) for i in range(_SEAT_COUNT)]


def _assign_distinct(options: Sequence[Sequence[str]]) -> list[str] | None:
    """在“每场一馆、馆不重复”下找一个可行摆法（稳定的回溯）。"""
    used: set[str] = set()
    chosen: list[str] = [-1] * len(options)

    def walk(i: int) -> bool:
        if i == len(options):
            return True
        for vid in sorted(options[i]):
            if vid in used:
                continue
            used.add(vid)
            chosen[i] = vid
            if walk(i + 1):
                return True
            used.discard(vid)
            chosen[i] = -1
        return False

    return list(chosen) if walk(0) else None


def count_venue_arrangements(cat: Catalog, away_order: Sequence[str]) -> int:
    """同一客队先后有多少种互不重复的场馆摆法（用于清点票本不重复）。"""
    options = _options_by_match(cat, away_order)
    used: set[str] = set()
    total = 0

    def walk(i: int) -> None:
        nonlocal total
        if i == len(options):
            total += 1
            return
        for vid in sorted(options[i]):
            if vid in used:
                continue
            used.add(vid)
            walk(i + 1)
            used.discard(vid)

    walk(0)
    return total


def evaluate_pairing(cat: Catalog, away_order: Sequence[str]) -> dict[str, Any]:
    """核对一张完整配对：列实际可用馆；不可办时给出霍尔型缺额依据。

    返回 ``feasible``、逐场 ``options``/``chosen``、以及不可办时的
    ``deficiencies``（比赛集合与其可用馆并集不足的证据）。
    """
    if len(away_order) != _SEAT_COUNT:
        raise ValueError("配对必须恰好三个客队")
    if len(set(away_order)) != _SEAT_COUNT or any(a not in cat.aways for a in away_order):
        raise ValueError(f"配对客队非法或不在本版本：{away_order}")
    options = _options_by_match(cat, away_order)
    chosen = _assign_distinct(options)
    per_match = []
    for i, away in enumerate(away_order):
        per_match.append({
            "seat": i + 1,
            "seed": cat.seeds[i],
            "away": away,
            "away_base": cat.away_base(away).venue_id,
            "options": list(options[i]),
            "chosen": None if chosen is None else chosen[i],
        })
    deficiencies: list[dict[str, Any]] = []
    if chosen is None:
        for size in range(1, _SEAT_COUNT + 1):
            for combo in combinations(range(_SEAT_COUNT), size):
                union = set().union(*(options[i] for i in combo)) if combo else set()
                if len(union) < size:
                    deficiencies.append({
                        "matches": [{"seat": i + 1, "seed": cat.seeds[i],
                                     "away": away_order[i],
                                     "options": options[i]} for i in combo],
                        "union_venues": sorted(union),
                        "need": size,
                    })
    return {
        "feasible": chosen is not None,
        "away_order": list(away_order),
        "matches": per_match,
        "chosen_venues": chosen,
        "deficiencies": deficiencies,
        "reason": None if chosen is not None else (
            "存在一组比赛，其可用馆并集少于比赛数（且馆不可重复），无法同时安下"
            if deficiencies else "没找到互不重复的场馆摆法"),
    }


# --------------------------------------------------------------------- 完签枚举
def enumerate_away_orders(cat: Catalog) -> list[tuple[str, ...]]:
    """所有合法完签，仅按客队先后辨认（稳定顺序：按本版本客队名次序枚举）。"""
    out: list[tuple[str, ...]] = []
    for order in permutations(cat.aways):
        if _assign_distinct(_options_by_match(cat, order)) is not None:
            out.append(order)
    return out


def complete_draws(cat: Catalog) -> list[dict[str, Any]]:
    """每个合法完签的详细报告（含一种见证摆法与比赛）。"""
    reports = []
    for order in enumerate_away_orders(cat):
        ev = evaluate_pairing(cat, order)
        matches = []
        for m in ev["matches"]:
            matches.append({
                "seat": m["seat"], "seed": m["seed"], "away": m["away"],
                "away_base": m["away_base"], "venue": m["chosen"],
                "available_venues": m["options"],
            })
        reports.append({
            "away_order": list(order),
            "matches": [
                {"seat": i + 1, "seed": cat.seeds[i], "away": order[i],
                 "home_venue": ev["chosen_venues"][i],
                 "away_base": cat.away_base(order[i]).venue_id}
                for i in range(_SEAT_COUNT)
            ],
            "available_venues": [m["options"] for m in ev["matches"]],
            "venue_arrangement_count": count_venue_arrangements(cat, order),
        })
    return reports


def _normalise_prefix(prefix: Iterable[tuple[int, str]] | Mapping[int, str] | None
                      ) -> list[tuple[int, str]]:
    if prefix is None:
        return []
    if isinstance(prefix, Mapping):
        items = sorted(prefix.items())
    else:
        items = list(prefix)
    seats = [s for s, _ in items]
    if seats != list(range(len(items))):
        raise ValueError("前缀必须从第 1 席起按固定主签位顺序连续给出")
    return [(s, a) for s, a in items]


def consistent_orders(cat: Catalog, prefix=None) -> list[tuple[str, ...]]:
    """与已公开前缀相容的全部合法完签。"""
    pre = _normalise_prefix(prefix)
    allowed = enumerate_away_orders(cat)
    for seat, away in pre:
        if seat >= _SEAT_COUNT:
            raise ValueError("前缀席号越界")
        if away not in cat.aways:
            raise ValueError(f"前缀客队 {away!r} 不在本版本")
        allowed = [o for o in allowed if o[seat] == away]
    return allowed


def seat_opportunities(cat: Catalog, prefix=None, seat: int | None = None
                       ) -> dict[str, Any]:
    """给定前缀后，下一（或指定）主签位各客队对应的“还剩多少种完签”。

    机会 = 与该客队相容的完签数 / 当前相容完签总数；返回计数与分数。
    """
    pre = _normalise_prefix(prefix)
    seat = len(pre) if seat is None else seat
    if seat < 0 or seat >= _SEAT_COUNT:
        raise ValueError("席号越界")
    if seat < len(pre):
        raise ValueError("该席已在前缀中，不能再次询问其机会")
    base = consistent_orders(cat, pre)
    counts: dict[str, int] = {}
    for order in base:
        counts[order[seat]] = counts.get(order[seat], 0) + 1
    total = len(base)
    return {
        "seat": seat + 1,
        "seed": cat.seeds[seat],
        "total": total,
        "counts": {a: counts[a] for a in cat.aways if counts.get(a)},
        "fractions": {a: (counts[a], total) for a in cat.aways if counts.get(a)},
    }


def recount_integrity(cat: Catalog, orders: Sequence[Sequence[str]]) -> dict[str, Any]:
    """清点票本：每张签只按客队先后辨认，证明换馆未重复入册。

    ``orders`` 为已登记的完签先后。检查：每张可办、各出现恰好一次、
    登记集合等于独立可达集合；并报告每题存在多少种场馆摆法。
    """
    seen: dict[tuple[str, ...], int] = {}
    for o in orders:
        t = tuple(o)
        seen[t] = seen.get(t, 0) + 1
    duplicates = [list(k) for k, c in seen.items() if c > 1]
    feasible = enumerate_away_orders(cat)
    missing = [list(o) for o in feasible if tuple(o) not in seen]
    extra = [list(o) for o in seen if o not in set(feasible)]
    arrangements = {tuple(o): count_venue_arrangements(cat, o) for o in seen}
    return {
        "registered": len(orders),
        "distinct_orders": len(seen),
        "duplicates": duplicates,
        "missing": missing,
        "extra": extra,
        "feasible_total": len(feasible),
        "venue_arrangement_counts": [{"away_order": list(k), "arrangements": v}
                                      for k, v in arrangements.items()],
        "ok": not duplicates and not missing and not extra,
    }
