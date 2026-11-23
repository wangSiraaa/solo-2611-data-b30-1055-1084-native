"""公证抽签：从均匀整数抽签源取结果，生成可独立重放的封存凭证。

原则
====
* 没有合法完签就不抽（``status='no_draw'``，凭证里保留空签集证据）。
* 每张合法完签机会完全相等：从 ``feasible_orders`` 表中按均匀整数抽一行
  （``mode='table'``），或逐席在“剩余完签按客队分号段”里抽一个整数
  （``mode='sequential'``）。两种都是等概率。
* 抽签源只接受“均匀整数”：入口是 :meth:`UniformSource.int_in`（闭区间）与
  :meth:`UniformSource.choose`（0..n-1）。凭证为每次抽数记录合法范围
  ``[lo, hi]``，即该整数允许取的全部值。允许用 ``random.Random(seed)`` 作种子源，
  也可注入任意确定性/硬件源，不依赖网络。
* 整张完签只按客队先后辨认：抽中的是先后表的一行，无论有多少种场馆摆法都只算
  一张；场馆摆放由 :mod:`cup.model` 见证，:func:`cup.model.recount_integrity`
  用于清点票本、证明换馆未重复入册。
* 重放：仅凭目录 + 抽数（可含中途公开前缀）独立重算当时各客队机会与最终比赛，
  不信任凭证自报的计数与结果。
"""

from __future__ import annotations

import random
from typing import Any, Callable, Sequence

from .model import (
    Catalog,
    count_venue_arrangements,
    enumerate_away_orders,
    evaluate_pairing,
    seat_opportunities,
)

_SEAT_COUNT = 3


class DrawError(ValueError):
    """抽签参数或重放输入不合法。"""


# --------------------------------------------------------------------- 均匀整数源
class UniformSource:
    """均匀整数抽签源，记录每次抽数的合法区间与取值。

    ``urand(mod)`` 返回 ``[0, mod)`` 的均匀整数；默认用 ``random.Random(seed)``，
    也可注入自定义函数（例如硬件随机源）。每次抽数都记入 ``trace``，供公开与重放。
    """

    def __init__(self, seed: int | str | None = None,
                 urand: Callable[[int], int] | None = None):
        self._rng = random.Random(seed)
        self._urand = urand or (lambda mod: self._rng.randrange(mod))
        self.trace: list[dict[str, Any]] = []

    def _next_below(self, modulus: int) -> int:
        if modulus < 1:
            raise DrawError("抽数模数必须 >=1")
        return int(self._urand(modulus))

    def choose(self, n: int, tag: str) -> int:
        """在 ``0..n-1`` 均匀取一个整数（n>=1）。"""
        if n < 1:
            raise DrawError("choose(n) 要求 n>=1")
        val = self._next_below(n)
        self.trace.append({"tag": tag, "mode": "choose", "lo": 0,
                           "hi": n - 1, "range": n, "value": val})
        return val

    def int_in(self, lo: int, hi: int, tag: str) -> int:
        """在闭区间 ``[lo, hi]`` 均匀取整数。"""
        if hi < lo:
            raise DrawError("int_in 要求 hi>=lo")
        span = hi - lo + 1
        val = lo + self._next_below(span)
        self.trace.append({"tag": tag, "mode": "int_in", "lo": lo,
                           "hi": hi, "range": span, "value": val})
        return val

    def snapshot(self) -> dict[str, Any]:
        return {"draws": [dict(d) for d in self.trace],
                "consumed": len(self.trace)}


# --------------------------------------------------------------------- 签号/整数段
def away_index(cat: Catalog, away: str) -> int:
    return list(cat.aways).index(away)


def uniform_numbers(cat: Catalog) -> dict[tuple[str, ...], str]:
    """给每张不同完签一个等长编号（各席用本版本客队序号 1..3）。

    编号只由客队先后生成，与场馆摆法无关：换馆不会得到第二个编号。
    长度恒为席数，便于封箱前逐张登记。
    """
    out: dict[tuple[str, ...], str] = {}
    for order in enumerate_away_orders(cat):
        out[order] = "".join(str(away_index(cat, a) + 1) for a in order)
    lengths = {len(s) for s in out.values()}
    if lengths != {_SEAT_COUNT}:
        raise DrawError(f"完签编号长度不一致：{lengths}")
    return out


def _segment_layout(cat: Catalog, remaining: Sequence[tuple[str, ...]],
                    seat: int) -> list[dict[str, Any]]:
    """把剩余完签按第 ``seat`` 席客队分号段（按本版本客队次序，段连续）。

    返回每段 ``away``、闭区间 ``lo``/``hi``、``count``，段和覆盖整个
    ``[0, len(remaining)-1]``。一次均匀整数按段长等比例选中客队。
    """
    counts: dict[str, int] = {}
    for o in remaining:
        counts[o[seat]] = counts.get(o[seat], 0) + 1
    segments = []
    cursor = 0
    for away in cat.aways:
        c = counts.get(away, 0)
        if c == 0:
            continue
        segments.append({"away": away, "lo": cursor, "hi": cursor + c - 1,
                         "count": c})
        cursor += c
    if cursor != len(remaining):
        raise DrawError("号段划分未覆盖全部剩余完签")
    return segments


# --------------------------------------------------------------------- 见证比赛
def _final_matches(cat: Catalog, away_order: Sequence[str]) -> list[dict[str, Any]]:
    ev = evaluate_pairing(cat, away_order)
    if not ev["feasible"]:
        raise DrawError("该完签不可办，目录与枚举不一致")
    return [
        {"seat": m["seat"], "seed": m["seed"], "away": m["away"],
         "venue": m["chosen"], "available_venues": m["options"],
         "away_base": m["away_base"]}
        for m in ev["matches"]
    ]


def _opportunities_trail(cat: Catalog, away_order: Sequence[str]) -> list[dict[str, Any]]:
    """沿抽中的先后记录每席在“已揭前缀”下各客队的机会。"""
    trail = []
    for seat in range(_SEAT_COUNT):
        prefix = [(s, away_order[s]) for s in range(seat)]
        trail.append(seat_opportunities(cat, prefix, seat=seat))
    return trail


# --------------------------------------------------------------------- 抽取
def make_draw(cat: Catalog, request_id: str, source: UniformSource,
              mode: str = "table") -> dict[str, Any]:
    """公开抽签，返回可封存的凭证。

    mode='table'：一次均匀整数直接在 ``feasible_orders`` 表里挑一行。
    mode='sequential'：逐席在“剩余完签按客队分号段”里均匀抽一个整数。
    """
    if mode not in ("table", "sequential"):
        raise DrawError("mode 必须是 'table' 或 'sequential'")
    feasible = enumerate_away_orders(cat)
    numbers = uniform_numbers(cat) if feasible else {}
    base: dict[str, Any] = {
        "kind": "cup_draw",
        "request_id": request_id,
        "catalog_id": cat.catalog_id,
        "snapshot_ref": dict(cat.snapshot_ref),
        "seeds": list(cat.seeds),
        "aways": list(cat.aways),
        "feasible_orders": [list(o) for o in feasible],
        "feasible_total": len(feasible),
        "numbering": [{"number": numbers[o], "away_order": list(o)} for o in feasible],
        "mode": mode,
    }
    if not feasible:
        base.update({"status": "no_draw", "away_order": None, "matches": None,
                     "opportunities": None, "segments": None, "raw": None,
                     "venue_arrangement_count": None,
                     "reason": "目录下不存在任何合法完签，不举行抽签"})
        return base

    if mode == "table":
        idx = source.choose(len(feasible), tag="pick_order_row")
        away_order = feasible[idx]
        segments = None
    else:
        remaining = list(feasible)
        picked: list[str] = []
        segments = []
        for seat in range(_SEAT_COUNT):
            layout = _segment_layout(cat, remaining, seat)
            offset = source.int_in(0, len(remaining) - 1, tag=f"seat_{seat + 1}")
            seg = next(s for s in layout if s["lo"] <= offset <= s["hi"])
            picked.append(seg["away"])
            remaining = [o for o in remaining if o[seat] == seg["away"]]
            segments.append({"seat": seat + 1, "seed": cat.seeds[seat],
                             "layout": layout, "value": offset})
        away_order = tuple(picked)

    matches = _final_matches(cat, away_order)
    base.update({
        "status": "drawn",
        "away_order": list(away_order),
        "order_number": numbers[away_order],
        "matches": matches,
        "opportunities": _opportunities_trail(cat, away_order),
        "venue_arrangement_count": count_venue_arrangements(cat, away_order),
        "segments": segments,
        "raw": source.snapshot(),
    })
    return base


# --------------------------------------------------------------------- 重放
def replay_from_draws(cat: Catalog, mode: str, draws: Sequence[dict[str, Any]],
                      revealed: int | None = None) -> dict[str, Any]:
    """只用目录 + 抽数（可只公开前 ``revealed`` 次）重放，得到各席结果与范围。

    这是独立核对的核心：不借助凭证自报的先后/机会；每个抽数的合法区间
    由当时的剩余完签数算出。超过 ``revealed`` 的抽数即便给出也不用于
    已揭结论。返回 ``revealed_order``、``next_opportunities``、``legit_ranges``。
    """
    if mode not in ("table", "sequential"):
        raise DrawError("mode 非法")
    feasible = enumerate_away_orders(cat)
    if not feasible:
        return {"feasible": False, "revealed_order": [], "next_opportunities": None,
                "legit_ranges": [], "draws_used": 0}
    if revealed is None:
        revealed = _SEAT_COUNT if mode == "sequential" else (1 if draws else 0)
    if mode == "sequential" and revealed > len(draws):
        raise DrawError("公开的席数多于已有抽数")
    if mode == "sequential" and revealed > 0 and not draws:
        raise DrawError("逐席模式缺少抽数")

    if mode == "table":
        if revealed < 1:
            return {"feasible": True, "revealed_order": [], "total": len(feasible),
                    "legit_ranges": [[0, len(feasible) - 1]],
                    "next_opportunities": seat_opportunities(cat, [], seat=0),
                    "draws_used": 0}
        d = draws[0]
        lo, hi = 0, len(feasible) - 1
        if "value" not in d:
            raise DrawError("抽数缺少 value")
        if "lo" in d and (d["lo"], d["hi"]) != (lo, hi):
            raise DrawError(f"抽数合法范围应为 [{lo},{hi}]，凭证记录 {d['lo']},{d['hi']}")
        if not (lo <= d["value"] <= hi):
            raise DrawError("抽数超出合法范围")
        order = feasible[d["value"]]
        # 表模式封箱时一次抽定整张先后；已揭前的若干席由该先后决定
        prefix = list(order[:revealed])
        return {"feasible": True, "revealed_order": prefix, "total": len(feasible),
                "legit_ranges": [[lo, hi]], "draws_used": 1,
                "full_order": list(order),
                "next_opportunities": (
                    seat_opportunities(cat, [(s, prefix[s]) for s in range(len(prefix))],
                                       seat=len(prefix)) if len(prefix) < _SEAT_COUNT
                    else None),
                "order_number": uniform_numbers(cat)[order]}

    remaining = list(feasible)
    picked: list[str] = []
    legit = []
    used = 0
    for seat in range(min(revealed, _SEAT_COUNT)):
        if seat >= len(draws):
            raise DrawError("抽数不足，无法重放到该席")
        d = draws[seat]
        lo, hi = 0, len(remaining) - 1
        if "lo" in d and (d["lo"], d["hi"]) != (lo, hi):
            raise DrawError(
                f"第 {seat + 1} 席抽数合法范围应为 [{lo},{hi}]，凭证记录 {d['lo']},{d['hi']}")
        if not (lo <= d["value"] <= hi):
            raise DrawError(f"第 {seat + 1} 席抽数 {d['value']} 超出 [{lo},{hi}]")
        layout = _segment_layout(cat, remaining, seat)
        seg = next(s for s in layout if s["lo"] <= d["value"] <= s["hi"])
        picked.append(seg["away"])
        legit.append({"seat": seat + 1, "lo": lo, "hi": hi,
                      "segments": layout, "value": d["value"],
                      "away": seg["away"]})
        remaining = [o for o in remaining if o[seat] == seg["away"]]
        used += 1
    out: dict[str, Any] = {
        "feasible": True, "revealed_order": picked, "order_number":
        "".join(str(away_index(cat, a) + 1) for a in picked),
        "legit_ranges": [[r["lo"], r["hi"]] for r in legit],
        "draws_used": used, "seat_details": legit,
        "next_opportunities": (
            seat_opportunities(cat, [(s, picked[s]) for s in range(len(picked))],
                               seat=len(picked)) if len(picked) < _SEAT_COUNT
            else None),
    }
    for o in remaining:
        if tuple(o[:len(picked)]) == tuple(picked):
            out["consistent_orders"] = [list(x) for x in remaining]
            break
    if len(picked) == _SEAT_COUNT:
        out["final_order"] = picked
    return out


def replay_draw(cat: Catalog, envelope: dict[str, Any],
                trace_prefix: Sequence[tuple[int, str]] | None = None) -> dict[str, Any]:
    """独立重放一张封存凭证：校验版本、抽数与（可选）公开前缀，并重算比赛。

    无论只公开前几席，都会先用目录 + 抽数把整张先后完整重建，再与凭证自报的
    先后交叉核对；``trace_prefix`` 只用于确认中途已公开的部分确为最终先后的前缀。
    """
    _assert_envelope_version(cat, envelope)
    feasible = enumerate_away_orders(cat)
    if envelope.get("status") == "no_draw":
        return {"status": "no_draw", "feasible_orders": [],
                "agree_no_draw": len(feasible) == 0}
    if envelope.get("status") != "drawn":
        raise DrawError("凭证状态非法")

    raw = envelope.get("raw") or {}
    draws = raw.get("draws") or []
    mode = envelope.get("mode")

    # 完整重建（不信任凭证自报先后）
    full = replay_from_draws(cat, mode, draws, revealed=_SEAT_COUNT)
    full_order = full["revealed_order"]
    if tuple(full_order) != tuple(envelope["away_order"]):
        raise DrawError("凭证先后与抽数完整重放结果不一致")
    if tuple(full_order) not in set(feasible):
        raise DrawError("凭证先后不是合法完签")

    revealed = _SEAT_COUNT
    if trace_prefix is not None:
        revealed = len(trace_prefix)
        for s, a in trace_prefix:
            if s >= _SEAT_COUNT or full_order[s] != a:
                raise DrawError(f"公开前缀第 {s + 1} 席与抽数重放不符")

    opps = []
    for seat in range(_SEAT_COUNT):
        prefix = [(s, full_order[s]) for s in range(seat)]
        opps.append(seat_opportunities(cat, prefix, seat=seat))
    matches = _final_matches(cat, tuple(full_order))
    return {
        "status": "drawn",
        "away_order": list(full_order),
        "order_number": uniform_numbers(cat)[tuple(full_order)],
        "matches": matches,
        "opportunities": opps,
        "feasible_orders": [list(o) for o in feasible],
        "venue_arrangement_count": count_venue_arrangements(cat, tuple(full_order)),
        "revealed_seats": revealed,
        "revealed_order": full_order[:revealed],
        "revealed_prefix": [[s + 1, full_order[s]] for s in range(revealed)],
        "legit_ranges": full["legit_ranges"],
        "checks": {
            "mode": mode,
            "draws_used": full["draws_used"],
            "table_row_matches": mode == "table" and
            tuple(full_order) == feasible[draws[0]["value"]],
        },
    }


def _assert_envelope_version(cat: Catalog, envelope: dict[str, Any]) -> None:
    if envelope.get("kind") != "cup_draw":
        raise DrawError("不是杯签凭证")
    if envelope.get("catalog_id") != cat.catalog_id:
        raise DrawError("目录 id 与凭证不符")
    if envelope.get("snapshot_ref", {}).get("order") != list(cat.snapshot_ref["order"]):
        raise DrawError("目录绑定的排名版本与凭证不符")


def seat_state_after_prefix(cat: Catalog, envelope: dict[str, Any],
                            revealed: int,
                            revealed_prefix: Sequence[tuple[int, str]] | None = None,
                            draws_seen: Sequence[dict[str, Any]] | None = None
                            ) -> dict[str, Any]:
    """停电后接班：给定已揭 ``revealed`` 席，说明已生效的抽数与下一席机会。

    ``revealed_prefix`` 若给出，必须与凭证最终先后的前若干席一致；
    ``draws_seen`` 若给出，则只用已公开的抽数独立重放，并与前缀交叉核对——
    不能换号重摇。逐席模式下已生效的是前 ``revealed`` 次抽数；表模式下是
    封箱时那一行号。
    """
    _assert_envelope_version(cat, envelope)
    if envelope.get("status") != "drawn":
        raise DrawError("该凭证未产生抽签结果")
    if not (0 <= revealed <= _SEAT_COUNT):
        raise DrawError("已揭席数越界")
    away_order = tuple(envelope["away_order"])
    if revealed_prefix is not None:
        if len(revealed_prefix) != revealed:
            raise DrawError("已揭席数与公开前缀长度不一致")
        for s, a in revealed_prefix:
            if s >= _SEAT_COUNT or away_order[s] != a:
                raise DrawError("公开前缀与已封存的最终先后不符，禁止换号重摇")

    mode = envelope.get("mode")
    raw_draws = (envelope.get("raw") or {}).get("draws") or []
    draws = list(draws_seen) if draws_seen is not None else raw_draws
    # 独立重放已揭部分（验证抽数仍在各自合法范围）
    replayed = replay_from_draws(cat, mode, draws, revealed=revealed)
    if revealed_prefix is not None:
        for i, (s, a) in enumerate(revealed_prefix):
            if replayed["revealed_order"][s] != a:
                raise DrawError("公开前缀与抽数重放结果不符，禁止换号重摇")

    prefix = [(s, away_order[s]) for s in range(revealed)]
    effective = (_effective_prefix_draws(mode, draws, revealed))
    out = {
        "revealed_seats": revealed,
        "mode": mode,
        "effective_draws": effective,
        "effective_ranges": replayed["legit_ranges"],
        "revealed_prefix": [[s + 1, away_order[s]] for s in range(revealed)],
        "order_matches_final": True,
    }
    if revealed < _SEAT_COUNT:
        out["next_seat_opportunities"] = seat_opportunities(cat, prefix, seat=revealed)
    else:
        out["next_seat_opportunities"] = None
        out["final_matches"] = _final_matches(cat, away_order)
    return out


def _effective_prefix_draws(mode: str | None, draws: Sequence[dict[str, Any]],
                            revealed: int) -> list[dict[str, Any]]:
    if mode == "sequential":
        return [dict(d) for d in draws[:revealed]]
    return [dict(d) for d in draws[:1]]  # 表模式封箱即抽定同一个行号
