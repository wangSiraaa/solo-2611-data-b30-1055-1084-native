"""可行性求解与可执行洗程方案（纯逻辑，本地小域，不模拟物理洗涤）。

问题实例
--------
:class:`WashProblem` 汇总一次派生的待洗布件、初始网袋、实筒（程序/整点/容量/用时）
与下场实际交还截止。求解器回答两件事：

1. 每件布件允许的程序集合（由 :mod:`laundry.model` 的约束交集给出），
   因而整袋可共容的程序 = 袋内各件允许集合的交集；
2. 能否在截止前把每一套（同队同主客方）都洗净交回；能，则给出分袋、整筒、
   交回方案；不能，则给出缺件或时窗不足的具体理由。

关键不变量
----------
* 各件只准在一处：某件一旦被某筒占用，其他筒/其他队都不得再许它。
* 已开机（started）的筒，其内容冻结：重开规划不得把同一件重洗或另许别处。
* 已封袋且回条相同的袋不能在更衣室拆开：若其内布件需要分到不同筒，即判非法。
* 干净件才可交；某队一套必须齐，绝不拿别队同尺码顶替。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Sequence

from .model import Drum, Item, ItemInfo, machine_conflicts


# ------------------------------------------------------------------ 问题实例
@dataclass
class WashProblem:
    items: dict[str, Item]
    bags: dict[str, list[str]]                       # bag_id -> item_ids（当前实体袋）
    drums: list[Drum]
    deadline: int
    sealed: dict[str, str] = field(default_factory=dict)  # bag_id -> 回条号（不可同回条拆）
    set_key: Callable[[ItemInfo], tuple] = field(
        default=lambda info: (info.team, info.side))

    def set_of(self, item_id: str) -> tuple:
        return self.set_key(self.items[item_id].info)

    def sets(self) -> dict[tuple, list[str]]:
        out: dict[tuple, list[str]] = {}
        for iid, it in self.items.items():
            out.setdefault(self.set_of(iid), []).append(iid)
        for v in out.values():
            v.sort()
        return out

    def bag_of(self, item_id: str) -> str | None:
        for bid, ids in self.bags.items():
            if item_id in ids:
                return bid
        return None

    def usable_drums(self, item: Item) -> list[Drum]:
        """整机在截止前完成、且程序在该件允许集合内的筒。"""
        return [d for d in self.drums
                if d.end <= self.deadline and item.permits(d.program)]


# ------------------------------------------------------------------ 方案结构
@dataclass
class Assignment:
    """一件布件落到的筒与程序。"""

    item_id: str
    drum_id: str
    program: str
    result_time: int

    def to_dict(self) -> dict:
        return {"item_id": self.item_id, "drum_id": self.drum_id,
                "program": self.program, "result_time": self.result_time}


@dataclass
class BagOp:
    """把初始实体袋变成目标袋所需的一次操作。"""

    target_bag: str
    source_bags: list[str]
    item_ids: list[str]
    action: str          # keep / split / merge / new
    drum_id: str

    def to_dict(self) -> dict:
        return {"target_bag": self.target_bag, "source_bags": list(self.source_bags),
                "item_ids": list(self.item_ids), "action": self.action,
                "drum_id": self.drum_id}


@dataclass
class Scheme:
    """一套互斥的合法拆袋/重封 + 整筒 + 交回方案。"""

    scheme_id: str
    assignments: dict[str, Assignment]
    target_bags: dict[str, list[str]]
    bag_ops: list[BagOp]
    drum_loads: dict[str, list[str]]                # drum_id -> item_ids
    set_status: dict[tuple, dict]
    complete: bool
    reasons: list[dict] = field(default_factory=list)

    def used_drums(self) -> set[str]:
        return {a.drum_id for a in self.assignments.values()}

    def to_dict(self) -> dict:
        return {
            "scheme_id": self.scheme_id,
            "complete": self.complete,
            "assignments": {k: v.to_dict() for k, v in sorted(self.assignments.items())},
            "target_bags": {k: sorted(v) for k, v in sorted(self.target_bags.items())},
            "bag_ops": [o.to_dict() for o in self.bag_ops],
            "drum_loads": {k: sorted(v) for k, v in sorted(self.drum_loads.items())},
            "set_status": {"|".join(map(str, k)): v for k, v in sorted(self.set_status.items())},
            "reasons": self.reasons,
        }


@dataclass
class Solution:
    feasible: bool
    schemes: list[Scheme] = field(default_factory=list)
    reasons: list[dict] = field(default_factory=list)
    item_allowed: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"feasible": self.feasible,
                "item_allowed": self.item_allowed,
                "schemes": [s.to_dict() for s in self.schemes],
                "reasons": self.reasons}


# ------------------------------------------------------------------ 约束诊断
def item_program_report(problem: WashProblem) -> tuple[dict[str, list[str]], list[dict]]:
    """每件布件允许程序集合 + 因空集而无法洗的件（可驳回错误送洗）。"""
    allowed = {iid: sorted(it.allowed) for iid, it in sorted(problem.items.items())}
    reasons: list[dict] = []
    for iid, it in sorted(problem.items.items()):
        if not it.feasible:
            reasons.append({
                "code": "no_allowed_program", "item_id": iid,
                "constraints": [c.to_dict() for c in it.constraints],
                "message": f"布件 {iid} 的约束交集为空，任何程序都不可洗",
            })
    return allowed, reasons


# ------------------------------------------------------------------ 袋不可拆校验
def _sealed_split_conflict(problem: WashProblem, assign: Mapping[str, str]) -> list[dict]:
    """已封袋（同一回条）内若布件被分到不同筒，即为非法拆袋。"""
    problems: list[dict] = []
    for bid, ids in problem.bags.items():
        if bid not in problem.sealed:
            continue
        drums = {assign.get(i) for i in ids}
        if len(drums) > 1:
            problems.append({
                "code": "sealed_bag_split", "bag_id": bid,
                "return_slip": problem.sealed[bid],
                "drums": sorted(x for x in drums if x),
                "message": f"已封袋 {bid}（回条 {problem.sealed[bid]}）不能在更衣室拆开，"
                           f"但其布件落在不同筒",
            })
    return problems


# ------------------------------------------------------------------ 束（不可拆的最小指派单元）
@dataclass
class _Bundle:
    item_ids: list[str]
    allowed: frozenset[str]          # 束内各件允许集合的交集
    fixed_drum: str | None = None    # 冻结束（已开机筒内容）


def _build_bundles(problem: WashProblem,
                   frozen: Mapping[str, str]) -> tuple[list[_Bundle], list[dict]]:
    """把布件组织成指派单元。

    * 冻结件（已开机）各自固定，不参与搜索；
    * 同一已封回条袋内的件必须同筒，合并成一个可拆受限的束；
    * 其余每件独立成束（开放袋可自由拆/合）。
    """
    reasons: list[dict] = []
    used: set[str] = set()
    bundles: list[_Bundle] = []

    # 冻结束
    by_drum: dict[str, list[str]] = {}
    for iid, did in frozen.items():
        by_drum.setdefault(did, []).append(iid)
        used.add(iid)
    for did, ids in by_drum.items():
        allowed = frozenset.intersection(*[problem.items[i].allowed for i in ids]) \
            if ids else frozenset()
        bundles.append(_Bundle(sorted(ids), allowed, fixed_drum=did))

    # 已封袋束（未冻结部分）
    for bid, ids in problem.bags.items():
        if bid not in problem.sealed:
            continue
        rest = [i for i in ids if i not in used]
        if not rest:
            continue
        allowed = frozenset.intersection(*[problem.items[i].allowed for i in rest])
        if not allowed:
            reasons.append({
                "code": "sealed_bag_no_common_program", "bag_id": bid,
                "item_ids": sorted(rest),
                "message": f"已封袋 {bid} 内布件无共同可用程序，无法整袋送洗",
            })
        bundles.append(_Bundle(sorted(rest), allowed))
        used.update(rest)

    # 开放单件束
    for iid in sorted(problem.items):
        if iid in used:
            continue
        it = problem.items[iid]
        bundles.append(_Bundle([iid], it.allowed))
        used.add(iid)
    return bundles, reasons


# ------------------------------------------------------------------ 搜索
def _search_assignments(
    problem: WashProblem,
    frozen: Mapping[str, str],
    limit: int,
    available_drums: Sequence[Drum] | None = None,
) -> tuple[list[dict[str, str]], list[dict]]:
    """回溯搜索至多 ``limit`` 个可行的 item->drum 指派（去重）。

    容量按 “束内件数” 计；只考虑整机在截止前完成且程序被束允许的筒。
    """
    drums = list(problem.drums if available_drums is None else available_drums)
    bundles, reasons = _build_bundles(problem, frozen)
    if reasons:
        return [], reasons

    # 容量：冻结束先占用
    cap_left = {d.drum_id: d.capacity for d in drums}
    for b in bundles:
        if b.fixed_drum is not None:
            if b.fixed_drum in cap_left:
                cap_left[b.fixed_drum] -= len(b.item_ids)
                if cap_left[b.fixed_drum] < 0:
                    return [], [{
                        "code": "frozen_over_capacity", "drum_id": b.fixed_drum,
                        "item_ids": b.item_ids,
                        "message": f"已开机筒 {b.fixed_drum} 冻结内容超过其容量",
                    }]
        # 冻结束本身不再搜索；其程序已固定在原筒。

    def drum_options(b: _Bundle) -> list[Drum]:
        if b.fixed_drum is not None:
            return []
        out = []
        for d in drums:
            if d.drum_id == b.fixed_drum:
                continue
            if d.end > problem.deadline:
                continue
            if not b.allowed or d.program not in b.allowed:
                continue
            if cap_left.get(d.drum_id, 0) < len(b.item_ids):
                continue
            out.append(d)
        return out

    searchable = [b for b in bundles if b.fixed_drum is None]
    # 选项最少的先分配，剪枝更早生效
    opts = {id(b): drum_options(b) for b in searchable}
    searchable.sort(key=lambda b: (len(opts[id(b)]), b.item_ids[0]))

    results: list[dict[str, str]] = []
    seen: set[tuple] = set()
    choice: dict[str, str] = {}

    def snapshot() -> dict[str, str]:
        full = dict(choice)
        for iid, did in frozen.items():
            full[iid] = did
        return full

    def dfs(idx: int) -> bool:
        if idx == len(searchable):
            snap = snapshot()
            # 已封袋拆袋校验
            if _sealed_split_conflict(problem, snap):
                return False
            key = tuple(sorted(snap.items()))
            if key not in seen:
                seen.add(key)
                results.append(snap)
            return len(results) >= limit
        b = searchable[idx]
        options = opts[id(b)]
        # 动态容量检查（前面的选择可能占满）
        for d in options:
            if cap_left[d.drum_id] < len(b.item_ids):
                continue
            cap_left[d.drum_id] -= len(b.item_ids)
            for iid in b.item_ids:
                choice[iid] = d.drum_id
            stop = dfs(idx + 1)
            for iid in b.item_ids:
                choice.pop(iid, None)
            cap_left[d.drum_id] += len(b.item_ids)
            if stop:
                return True
        return False

    # 冻结束也要参与“已封袋拆袋”校验（可能已被冻结在各自不同筒）
    if _sealed_split_conflict(problem, frozen):
        return [], [{
            "code": "sealed_bag_split_frozen", "item_ids": sorted(frozen),
            "message": "已开机/已封内容把同一已封回条袋拆到不同筒",
        }]

    dfs(0)

    if not results:
        return [], _explain_infeasible(problem, bundles, drums, cap_left)
    return results, []


def _explain_infeasible(problem: WashProblem, bundles: Sequence[_Bundle],
                        drums: Sequence[Drum], cap_left: Mapping[str, int]) -> list[dict]:
    """给出为什么没有可行指派：缺程序、缺容量、时窗不足。"""
    reasons: list[dict] = []
    usable = [d for d in drums if d.end <= problem.deadline]
    if not usable:
        reasons.append({
            "code": "no_drum_before_deadline", "deadline": problem.deadline,
            "drums": [d.to_dict() for d in drums],
            "message": f"没有整机在截止 {problem.deadline} 前完成的筒",
        })
        return reasons

    for b in bundles:
        if b.fixed_drum is not None:
            continue
        if not b.allowed:
            reasons.append({"code": "bundle_no_program", "item_ids": b.item_ids,
                            "message": f"束 {b.item_ids} 无任何共同可用程序"})
            continue
        fitting = [d for d in usable if d.program in b.allowed]
        if not fitting:
            reasons.append({
                "code": "no_drum_for_program", "item_ids": b.item_ids,
                "allowed": sorted(b.allowed),
                "usable_programs": sorted({d.program for d in usable}),
                "message": f"束 {b.item_ids} 只准 {sorted(b.allowed)}，"
                           f"但截止前只剩程序 {sorted({d.program for d in usable})}",
            })
            continue
        if not any(cap_left.get(d.drum_id, 0) >= len(b.item_ids) for d in fitting):
            reasons.append({
                "code": "insufficient_capacity", "item_ids": b.item_ids,
                "allowed": sorted(b.allowed),
                "fits_program_capacity": {d.drum_id: cap_left.get(d.drum_id, 0)
                                          for d in fitting},
                "message": f"束 {b.item_ids} 可用程序 {sorted(b.allowed)} 的筒容量都不够",
            })

    total_items = sum(len(b.item_ids) for b in bundles)
    total_cap = sum(d.capacity for d in usable)
    if total_items > total_cap:
        reasons.append({
            "code": "total_capacity_short", "items": total_items,
            "usable_capacity": total_cap,
            "message": f"待洗 {total_items} 件超过截止前可用总容量 {total_cap}",
        })
    if not reasons:
        reasons.append({"code": "no_assignment", "message": "无可行指派（组合约束冲突）"})
    return reasons


# ------------------------------------------------------------------ 目标袋 / 操作
def _build_target_bags(problem: WashProblem, assign: Mapping[str, str]
                       ) -> tuple[dict[str, list[str]], list[BagOp]]:
    """由 item->drum 指派推出目标网袋与拆/合/重封操作。

    规则：初始开放袋若各件同筒则原样保留（keep）；跨筒则按筒拆成子袋（split）。
    已封袋已保证同筒（否则前面已判非法），保留为 keep。
    """
    target: dict[str, list[str]] = {}
    ops: list[BagOp] = []
    covered: set[str] = set()

    for bid in sorted(problem.bags):
        ids = [i for i in problem.bags[bid] if i in assign]
        if not ids:
            continue
        groups: dict[str, list[str]] = {}
        for i in ids:
            groups.setdefault(assign[i], []).append(i)
        if len(groups) == 1:
            did = next(iter(groups))
            target[bid] = sorted(ids)
            ops.append(BagOp(bid, [bid], sorted(ids),
                             "keep" if set(ids) == set(problem.bags[bid]) else "split", did))
            covered.update(ids)
        else:
            for k, (did, gids) in enumerate(sorted(groups.items())):
                new_bid = f"{bid}#{k}"
                target[new_bid] = sorted(gids)
                ops.append(BagOp(new_bid, [bid], sorted(gids), "split", did))
                covered.update(gids)

    # 未出现在任何初始袋的件（例如新返回件）单独装袋
    for iid in sorted(assign):
        if iid in covered:
            continue
        did = assign[iid]
        new_bid = f"new-{iid}"
        target[new_bid] = [iid]
        ops.append(BagOp(new_bid, [], [iid], "new", did))
    return target, ops


def _set_status(problem: WashProblem, assign: Mapping[str, str],
                drums_by_id: Mapping[str, Drum]) -> tuple[dict, bool]:
    """每套（同队同主客方）的齐全情况。"""
    status: dict[tuple, dict] = {}
    all_complete = True
    for key, ids in problem.sets().items():
        washed = [i for i in ids if i in assign
                  and drums_by_id[assign[i]].end <= problem.deadline]
        missing = sorted(set(ids) - set(washed))
        complete = not missing
        all_complete &= complete
        status[key] = {
            "item_ids": ids, "washed": sorted(washed),
            "missing": missing, "complete": complete,
            "programs": sorted({drums_by_id[assign[i]].program for i in washed}),
        }
    return status, all_complete


def _materialize_schemes(problem: WashProblem, assigns: list[dict[str, str]],
                         drums_by_id: Mapping[str, Drum]) -> list[Scheme]:
    schemes: list[Scheme] = []
    for n, assign in enumerate(assigns, start=1):
        target, ops = _build_target_bags(problem, assign)
        loads: dict[str, list[str]] = {}
        for iid, did in assign.items():
            loads.setdefault(did, []).append(iid)
        set_status, complete = _set_status(problem, assign, drums_by_id)
        assignment = {
            iid: Assignment(iid, did, drums_by_id[did].program,
                            drums_by_id[did].end)
            for iid, did in assign.items()
        }
        reasons = []
        if not complete:
            for key, st in set_status.items():
                if not st["complete"]:
                    reasons.append({
                        "code": "set_incomplete", "set": list(key),
                        "missing": st["missing"],
                        "message": f"套 {key} 缺件 {st['missing']}，不齐不可交",
                    })
        schemes.append(Scheme(f"scheme-{n}", assignment, target, ops, loads,
                              set_status, complete, reasons))
    return schemes


# ------------------------------------------------------------------ 公开 API
def plan(problem: WashProblem, limit: int = 8) -> Solution:
    """求可行洗程方案；无解时给出可定位理由。"""
    item_allowed, pre = item_program_report(problem)
    if pre:
        return Solution(False, [], pre, item_allowed)

    assigns, reasons = _search_assignments(problem, frozen={}, limit=limit)
    if not assigns:
        return Solution(False, [], reasons, item_allowed)

    drums_by_id = {d.drum_id: d for d in problem.drums}
    schemes = _materialize_schemes(problem, assigns, drums_by_id)
    # 两法掌握相同的实筒单：同机时间重叠的装载不能同案执行
    schemes = _drop_machine_conflicts(problem, schemes)
    complete_schemes = [s for s in schemes if s.complete]
    if not complete_schemes:
        return Solution(False, schemes, [], item_allowed)
    return Solution(True, complete_schemes, [], item_allowed)


def _drop_machine_conflicts(problem: WashProblem, schemes: list[Scheme]) -> list[Scheme]:
    kept: list[Scheme] = []
    for s in schemes:
        selected = [d for d in problem.drums if d.drum_id in s.used_drums()]
        conflicts = machine_conflicts(selected)
        if conflicts:
            s.reasons.append({
                "code": "machine_time_conflict", "pairs": conflicts,
                "message": "同机装载时间重叠，不能同案执行",
            })
            continue
        kept.append(s)
    return kept


def replan(problem: WashProblem, started_drum_id: str,
           prior_assign: Mapping[str, str],
           available_drums: Sequence[Drum] | None = None,
           limit: int = 8) -> Solution:
    """某筒已开机、回报尚未到更衣室时重开规划。

    * 该筒内的件 **冻结**：不重洗、不再许给别筒/别队；
    * 其余件在现存实筒上重新求解，保证全局每件至多一处；
    * 若冻结筒的完成时间超过截止，相应套直接判缺件给理由。
    """
    drums_by_id = {d.drum_id: d for d in problem.drums}
    started = drums_by_id.get(started_drum_id)
    if started is None:
        return Solution(False, [], [{
            "code": "unknown_drum", "drum_id": started_drum_id,
            "message": f"未知已开机筒 {started_drum_id}"}],
            {iid: sorted(it.allowed) for iid, it in problem.items.items()})

    frozen = {iid: did for iid, did in prior_assign.items() if did == started_drum_id}
    item_allowed, pre = item_program_report(problem)
    reasons: list[dict] = list(pre)
    if started.end > problem.deadline:
        reasons.append({
            "code": "started_drum_late", "drum_id": started_drum_id,
            "end": started.end, "deadline": problem.deadline,
            "frozen_items": sorted(frozen),
            "message": f"已开机筒 {started_drum_id} 完成于 {started.end}，"
                       f"晚于截止 {problem.deadline}，其内布件不可按时交回",
        })

    assigns, why = _search_assignments(problem, frozen=frozen, limit=limit,
                                       available_drums=available_drums)
    if not assigns:
        return Solution(False, [], reasons + why, item_allowed)

    drums = list(problem.drums if available_drums is None else available_drums)
    # 冻结筒若不在可用列表，仍要能解析其程序/完成时间
    lookup = dict(drums_by_id)
    for d in drums:
        lookup[d.drum_id] = d
    schemes = _materialize_schemes(problem, assigns, lookup)
    complete_schemes = [s for s in schemes if s.complete]
    all_reasons = reasons + [r for s in schemes for r in s.reasons]
    if not complete_schemes:
        return Solution(False, schemes, all_reasons or [
            {"code": "set_incomplete", "message": "重开后仍有套不齐"}], item_allowed)
    return Solution(True, complete_schemes, all_reasons, item_allowed)


# ------------------------------------------------------------------ 两法：方案先行 vs 实筒先行
def scheme_first(problem: WashProblem, limit: int = 8) -> Solution:
    """**方案先行**：装备员一次给出若干互斥的合法拆袋/重封案。

    车场随后按“全部实筒”挑定能齐套的一案，交车前照案拆合封袋。
    若某筒在封车前坏了，用 :func:`invalidated_schemes` 找出失效的整套袋案。
    """
    return plan(problem, limit=limit)


def invalidated_schemes(schemes: Sequence[Scheme], failed_drum_id: str) -> list[str]:
    """封车前某筒坏掉时，哪些整套袋案因此失效（用到该筒即失效）。"""
    return [s.scheme_id for s in schemes if failed_drum_id in s.used_drums()]


def drum_first(problem: WashProblem, limit: int = 8) -> Solution:
    """**实筒先行**：车场报实筒与时刻，装备员逐筒确认可共存分袋后封袋。

    逐筒确认等价于求一个全局指派，使各筒点交承诺在两两之间不在一件布上相撞；
    由于指派对每件至多一处，跨筒天然不冲突。``plan`` 已剔除同机时间重叠的
    装载，故返回的方案即为可直接封袋的确认书。
    """
    sol = plan(problem, limit=limit)
    for s in sol.schemes:
        seen: set[str] = set()
        for a in s.assignments.values():
            if a.item_id in seen:
                raise AssertionError(f"布件 {a.item_id} 被许给多个筒")
            seen.add(a.item_id)
    return sol
