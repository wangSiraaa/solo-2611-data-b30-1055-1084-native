"""剪序求解：按已核准份额逐户交出全部整串，且不越留芽线。

目标（对应题面）：给出各户提货单后，找一组剪枝（合法最小反链、满足每主枝
留芽下限），使所有要求的整串都能被剪下并按份额交付；**先少损冬芽，同数
再少剪无主的果**。交付时注明每个主枝余芽、每串去向。

规模限定：小园不超过四树、十五个枝节、六户；超出即报
:class:`~orchard.errors.SolveTooLarge`，因为求解是穷举反链。

本模块只在内存中做规划；执行时由调用方把 :class:`HarvestPlan` 落到
:class:`~orchard.model.HarvestSession` 与
:class:`~orchard.shares.ShareLedger`（见 :func:`execute_plan`）。
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Iterable, Optional

from .errors import SolveTooLarge, Undeliverable
from .model import FruitString, HarvestSession, Orchard, evaluate_cuts
from .shares import Share, ShareLedger

MAX_TREES = 4
MAX_BRANCHES = 15
MAX_HOUSEHOLDS = 6


@dataclass(frozen=True)
class HouseholdOrder:
    """一户的提货单：从某叶领走若干具名整串。"""

    household_id: str
    dept_id: str
    string_ids: tuple[str, ...]


@dataclass(frozen=True)
class PlanAssignment:
    string_id: str
    household_id: str
    share_id: str
    commitment_no: str
    kind: str
    dept_id: str


@dataclass(frozen=True)
class HarvestPlan:
    cut_nodes: tuple[str, ...]
    assignments: tuple[PlanAssignment, ...]
    remaining_buds: dict[str, int]
    buds_lost: int
    unowned_strings: tuple[str, ...]

    def describe(self) -> str:
        lines = ["剪序：" + " -> ".join(self.cut_nodes) if self.cut_nodes else "剪序：空"]
        for a in self.assignments:
            lines.append(
                f"  串 {a.string_id} 交 {a.household_id}，来源 "
                f"{a.commitment_no}/{a.kind}({a.share_id})"
            )
        lines.append(
            "  余芽：" + ", ".join(f"{m}={b}" for m, b in sorted(self.remaining_buds.items()))
        )
        lines.append(f"  失芽 {self.buds_lost}；多剪无主串 {list(self.unowned_strings)}")
        return "\n".join(lines)


class Planner:
    def __init__(self, orchard: Orchard) -> None:
        self.orchard = orchard

    def plan(self, ledger: ShareLedger, orders: Iterable[HouseholdOrder]) -> HarvestPlan:
        orders = tuple(orders)
        self._check_size(len(orders))
        required: dict[str, str] = {}          # string_id -> household ordering it
        for order in orders:
            for sid in order.string_ids:
                self.orchard.string(sid)        # 校验存在
                if sid in required:
                    raise Undeliverable(f"果串 {sid} 被多户重复领取：{required[sid]} 与 {order.household_id}")
                required[sid] = order.household_id

        # 1) 份额分配可行性：每户每串都要有一条属性匹配、未用的可用份额。
        assignments = self._assign_shares(ledger, orders)

        # 2) 候选剪点：每个被要求果串的可剪祖先-或-自身。
        candidates: set[str] = set()
        cover_options: dict[str, set[str]] = {}
        for sid in required:
            branch_id = self.orchard.string(sid).branch_id
            options = {
                b
                for b in self.orchard.branch_ids
                if self.orchard.is_cuttable(b)
                and (b == branch_id or self.orchard.is_ancestor(b, branch_id))
            }
            if not options:
                raise Undeliverable(
                    f"果串 {sid} 位于不可剪的枝 {branch_id}（主枝根或无可剪祖先），"
                    "无法剪下"
                )
            cover_options[sid] = options
            candidates |= options
        ordered_candidates = sorted(candidates)
        if len(ordered_candidates) > 20:
            raise SolveTooLarge(
                f"候选剪点 {len(ordered_candidates)} 超过穷举上限 20，请缩小问题"
            )

        # 3) 穷举反链，取合法且覆盖全部要求串的最优解。
        best: Optional[tuple[tuple, tuple[str, ...]]] = None
        for size in range(0, len(ordered_candidates) + 1):
            for combo in combinations(ordered_candidates, size):
                if not self._is_antichain(combo):
                    continue
                out = evaluate_cuts(self.orchard, combo)
                if not self._within_bud_limits(out.remaining_buds):
                    continue
                if not self._covers(combo, cover_options):
                    continue
                harvested = {s.string_id for s in out.harvested_strings}
                unowned = tuple(sorted(harvested - set(required)))
                key = (out.buds_lost, len(unowned), len(combo), combo)
                if best is None or key < best[0]:
                    best = (key, combo)
            # 先小后大；一旦找到可行解，更大集合的失芽只会更多或相等，
            # 但无主串可能更少，因此仍需完整扫描所有大小。这里不提前退出。
        if best is None:
            raise Undeliverable(
                "在留芽下限与份额约束下，无法剪出全部要求的整串"
            )

        cut_nodes = best[1]
        out = evaluate_cuts(self.orchard, cut_nodes)
        harvested = {s.string_id for s in out.harvested_strings}
        unowned = tuple(sorted(harvested - set(required)))
        return HarvestPlan(
            cut_nodes=cut_nodes,
            assignments=assignments,
            remaining_buds=dict(out.remaining_buds),
            buds_lost=out.buds_lost,
            unowned_strings=unowned,
        )

    # ---------------------------------------------------------------- 内部

    def _check_size(self, households: int) -> None:
        if len(self.orchard.tree_ids) > MAX_TREES:
            raise SolveTooLarge(
                f"树数 {len(self.orchard.tree_ids)} 超过小园上限 {MAX_TREES}"
            )
        if len(self.orchard.branch_ids) > MAX_BRANCHES:
            raise SolveTooLarge(
                f"枝节数 {len(self.orchard.branch_ids)} 超过小园上限 {MAX_BRANCHES}"
            )
        if households > MAX_HOUSEHOLDS:
            raise SolveTooLarge(
                f"户数 {households} 超过小园上限 {MAX_HOUSEHOLDS}"
            )

    def _is_antichain(self, nodes: tuple[str, ...]) -> bool:
        for i, a in enumerate(nodes):
            for b in nodes[i + 1 :]:
                if self.orchard.is_ancestor(a, b) or self.orchard.is_ancestor(b, a):
                    return False
        return True

    def _within_bud_limits(self, remaining: dict[str, int]) -> bool:
        for main_root in self.orchard.main_root_ids:
            if remaining[main_root] < self.orchard.main_branch(main_root).min_buds:
                return False
        return True

    def _covers(self, nodes: tuple[str, ...], cover_options: dict[str, set[str]]) -> bool:
        node_set = set(nodes)
        return all(node_set & opts for opts in cover_options.values())

    def _assign_shares(
        self, ledger: ShareLedger, orders: tuple[HouseholdOrder, ...]
    ) -> tuple[PlanAssignment, ...]:
        result: list[PlanAssignment] = []
        used_share_ids: set[str] = set()
        for order in orders:
            available = [
                s
                for s in ledger.available_shares(order.dept_id)
                if s.share_id not in used_share_ids
            ]
            strings = [self.orchard.string(sid) for sid in order.string_ids]
            match = _bipartite_match(
                strings,
                available,
                lambda fs, sh: _attributes_match(sh, fs),
            )
            if match is None:
                raise Undeliverable(
                    f"户 {order.household_id} 的提货单无法用叶 {order.dept_id} 的"
                    "可用份额逐串对应（份额属性或数量不足）"
                )
            for fs, share in match:
                used_share_ids.add(share.share_id)
                result.append(
                    PlanAssignment(
                        string_id=fs.string_id,
                        household_id=order.household_id,
                        share_id=share.share_id,
                        commitment_no=share.commitment_no,
                        kind=share.kind,
                        dept_id=share.dept_id,
                    )
                )
        return tuple(result)


def _attributes_match(share: Share, fruit: FruitString) -> bool:
    if share.row not in (fruit.row, "ANY", "*"):
        return False
    if share.variety not in (fruit.variety, "ANY", "*"):
        return False
    if share.grade not in (fruit.grade, "ANY", "*"):
        return False
    return True


def _bipartite_match(
    strings: list[FruitString],
    shares: list[Share],
    compatible,
) -> Optional[list[tuple[FruitString, Share]]]:
    """为每个果串配一条不同份额（增广路），无解返回 None。"""
    share_to_string: dict[str, int] = {}

    def augment(i: int, seen: set[str]) -> bool:
        for sh in shares:
            if sh.share_id in seen or not compatible(strings[i], sh):
                continue
            seen.add(sh.share_id)
            if sh.share_id not in share_to_string or augment(
                share_to_string[sh.share_id], seen
            ):
                share_to_string[sh.share_id] = i
                return True
        return False

    for i in range(len(strings)):
        if not augment(i, set()):
            return None
    return [
        (strings[i], _share_by_id(shares, sid))
        for sid, i in share_to_string.items()
    ]


def _share_by_id(shares: list[Share], share_id: str) -> Share:
    for s in shares:
        if s.share_id == share_id:
            return s
    raise AssertionError("unreachable")  # pragma: no cover


def execute_plan(plan: HarvestPlan, session: HarvestSession, ledger: ShareLedger) -> None:
    """把已核准的剪序与份额交付落到会话与账本：先剪、后核销份额。"""
    for branch_id in plan.cut_nodes:
        session.cut(branch_id)
    for a in plan.assignments:
        ledger.consume(a.share_id, a.string_id)
