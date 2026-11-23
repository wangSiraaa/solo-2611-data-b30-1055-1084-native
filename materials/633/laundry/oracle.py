"""洗衣可行性小域独立 oracle。

与小域规模（≤12 件、≤6 筒）匹配，直接在每件布件上枚举“件 -> 筒”的全部指派，
再独立复核容量、程序允许、截止、以及已封回条袋不可拆等约束。
它不复用 :mod:`laundry.solver` 的任何搜索或诊断代码，用于双向对照：

* oracle 判可行的实例，求解器必须给出可行方案；
* oracle 判不可行的实例，求解器必须给出不可行（且带理由）。
"""

from __future__ import annotations

from itertools import product
from typing import Mapping, Sequence

from .model import Drum, Item


def _sealed_split_ok(bags: Mapping[str, list[str]],
                     sealed: Mapping[str, str],
                     assign: Mapping[str, str]) -> bool:
    for bid, ids in bags.items():
        if bid in sealed and len({assign.get(i) for i in ids}) > 1:
            return False
    return True


def enumerate_feasible(problem, cap: int = 20000) -> list[dict[str, str]] | None:
    """枚举全部可行指派；超过 ``cap`` 个组合时返回 None（不服务大实例）。

    返回列表中的每个元素是 ``item_id -> drum_id`` 的完整可交方案，
    要求每套（同队同主客方）在该指派下齐全且干净。
    """
    items: dict[str, Item] = problem.items
    drums: Sequence[Drum] = problem.drums
    available = [d for d in drums if d.end <= problem.deadline]
    total = len(available) ** len(items)
    if total > cap:
        return None

    item_ids = sorted(items)
    # 每件可选筒
    choices = []
    for iid in item_ids:
        allowed = items[iid].allowed
        opts = [d for d in available if d.program in allowed]
        choices.append(opts)
    if any(not c for c in choices):
        return []

    feasible: list[dict[str, str]] = []
    for combo in product(*choices):
        cap_use: dict[str, int] = {}
        assign: dict[str, str] = {}
        ok = True
        for iid, d in zip(item_ids, combo):
            assign[iid] = d.drum_id
            cap_use[d.drum_id] = cap_use.get(d.drum_id, 0) + 1
            if cap_use[d.drum_id] > d.capacity:
                ok = False
                break
        if not ok:
            continue
        if not _sealed_split_ok(problem.bags, problem.sealed, assign):
            continue
        # 每套齐全（到这里的件都已入选且整机按时完成；只要所有套成员都出现了就齐）
        set_members: dict[tuple, list[str]] = {}
        for iid in item_ids:
            set_members.setdefault(problem.set_of(iid), []).append(iid)
        if all(set(m).issubset(set(item_ids)) for m in set_members.values()):
            feasible.append(assign)
    return feasible


def is_feasible(problem) -> bool | None:
    got = enumerate_feasible(problem)
    if got is None:
        return None
    return bool(got)
