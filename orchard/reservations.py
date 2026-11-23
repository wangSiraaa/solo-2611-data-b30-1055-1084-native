"""预订与挂签：跨两个早晨的“保改约订剪单”。

题面把一天的采收拆成两个早晨，并给出两类关键操作：

* **保改约订剪单**（``AmendmentHold``）：第一天就给具名果串与替枝挂牌，
  挂牌期间另一张真来单不得使用被挂的果串/替枝；次日由实际领果者决定用
  哪一项，释放从未剪下的候补枝。
* **接真实余额**（``BalanceTakeover``）：让果农接带真实余额的份额，甲改约
  前不替他封候补果枝；临时来户可占实际可用的枝。一刀既落，果农只能从尚
  实存、满足留芽线的枝答应甲的改约。

核心不变量：
* 已剪走的枝回不到树上，改约不能复原剪过的枝；
* 挂签是真占用：被挂的果串/替枝在挂牌期间对真来单不可用；
* 不能把次日才开口的改约提前拿去指导第一刀。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import (
    AmendmentNotHonorable,
    ReservationConflict,
    ReservationNotFound,
)
from .model import HarvestSession, Orchard, evaluate_cuts


@dataclass(frozen=True)
class HoldItem:
    """挂签的一项：要么是具名果串，要么是候补替枝。"""

    kind: str                 # "STRING" / "BRANCH"
    target: str               # string_id 或 branch_id


@dataclass
class AmendmentHold:
    """一张保改约订剪单。"""

    hold_id: str
    household_id: str
    items: tuple[HoldItem, ...]
    released: bool = False

    def covers_string(self, string_id: str) -> bool:
        return any(i.kind == "STRING" and i.target == string_id for i in self.items)

    def covers_branch(self, branch_id: str) -> bool:
        return any(i.kind == "BRANCH" and i.target == branch_id for i in self.items)


class ReservationBook:
    """管理挂签的占用、查询与释放。"""

    def __init__(self, orchard: Orchard) -> None:
        self.orchard = orchard
        self._holds: dict[str, AmendmentHold] = {}
        self._branch_owner: dict[str, str] = {}
        self._string_owner: dict[str, str] = {}

    def hold(
        self,
        hold_id: str,
        household_id: str,
        items: tuple[HoldItem, ...],
    ) -> AmendmentHold:
        """为某户挂牌具名果串/候补替枝；期间真来单不得占用。"""
        if hold_id in self._holds:
            raise ReservationConflict(f"挂签 {hold_id} 已存在")
        if not items:
            raise ReservationConflict("保改约订剪单至少需要一项")
        for item in items:
            self._validate_item(item)
        # 先整体检查冲突，再落地，避免半挂。
        for item in items:
            if item.kind == "BRANCH" and item.target in self._branch_owner:
                raise ReservationConflict(
                    f"替枝 {item.target} 已被挂签 {self._branch_owner[item.target]} 占用"
                )
            if item.kind == "STRING" and item.target in self._string_owner:
                raise ReservationConflict(
                    f"果串 {item.target} 已被挂签 {self._string_owner[item.target]} 占用"
                )
        hold = AmendmentHold(hold_id, household_id, items)
        self._holds[hold_id] = hold
        for item in items:
            if item.kind == "BRANCH":
                self._branch_owner[item.target] = hold_id
            else:
                self._string_owner[item.target] = hold_id
        return hold

    def _validate_item(self, item: HoldItem) -> None:
        if item.kind == "STRING":
            self.orchard.string(item.target)
        elif item.kind == "BRANCH":
            branch = self.orchard.branch(item.target)
            if branch.is_main_root:
                raise ReservationConflict(f"主枝根 {item.target} 不能作为候补替枝")
        else:
            raise ReservationConflict(f"未知挂签类型 {item.kind}")

    def is_branch_free(self, branch_id: str) -> bool:
        return branch_id not in self._branch_owner

    def is_string_free(self, string_id: str) -> bool:
        return string_id not in self._string_owner

    def release(self, hold_id: str) -> AmendmentHold:
        """释放挂签：从未剪下的候补枝回到可用；已剪走的枝本就不在树上。"""
        try:
            hold = self._holds[hold_id]
        except KeyError:
            raise ReservationNotFound(f"挂签 {hold_id} 不存在") from None
        if hold.released:
            return hold
        hold.released = True
        for item in hold.items:
            table = self._branch_owner if item.kind == "BRANCH" else self._string_owner
            if table.get(item.target) == hold_id:
                del table[item.target]
        return hold

    def take_string(self, string_id: str) -> None:
        """真来单使用果串：若仍被挂签占用则拒绝。"""
        owner = self._string_owner.get(string_id)
        if owner is not None:
            raise ReservationConflict(
                f"果串 {string_id} 在挂签 {owner} 期间不得被真来单使用"
            )

    def take_branch(self, branch_id: str) -> None:
        """真来单使用替枝：若仍被挂签占用则拒绝。"""
        owner = self._branch_owner.get(branch_id)
        if owner is not None:
            raise ReservationConflict(
                f"替枝 {branch_id} 在挂签 {owner} 期间不得被真来单使用"
            )


# ---------------------------------------------------------------- 次日改约


@dataclass(frozen=True)
class AmendmentOption:
    """改约可选项：一串果 + 一条通往它的可剪替枝。"""

    household_id: str
    string_id: str
    branch_id: str


@dataclass
class AmendmentDecision:
    household_id: str
    string_id: str
    branch_id: str
    honor_from: tuple[str, ...] = ()


def honor_amendment(
    orchard: Orchard,
    session: HarvestSession,
    decision: AmendmentDecision,
) -> AmendmentOption:
    """在已落第一刀之后兑现次日的改约。

    果农只能从**尚实存、满足留芽线**的枝答应改约；被剪走的枝回不到树上，
    若原替枝已不在，则尝试其它仍可剪、能覆盖该果串的枝；都没有则拒绝。
    绝不允许复原已剪枝，也不允许把改约提前去指导第一刀。
    """
    string = orchard.string(decision.string_id)
    target = string.branch_id
    # 若果串所在的枝还在树上，原始替枝就是通往它的那条可剪祖先-或-自身。
    cut_before = set(session.cut_nodes)
    removed = evaluate_cuts(orchard, cut_before).removed_branches
    if target in removed:
        raise AmendmentNotHonorable(
            f"果串 {decision.string_id} 已随枝 {target} 剪走，无法兑现改约"
        )
    candidates = [
        b
        for b in (decision.branch_id, target, *orchard.ancestors(target))
        if b in orchard.branch_ids
        and orchard.is_cuttable(b)
        and (b == target or orchard.is_ancestor(b, target))
        and b not in removed
    ]
    for branch_id in candidates:
        if session.can_cut(branch_id):
            session.cut(branch_id)
            return AmendmentOption(
                decision.household_id, decision.string_id, branch_id
            )
    raise AmendmentNotHonorable(
        f"改约无法从尚实存且满足留芽线的枝兑现：果串 {decision.string_id} "
        f"的原替枝/祖先枝均已被剪或会越留芽线"
    )


# ---------------------------------------------------------------- 接真实余额


@dataclass(frozen=True)
class BalanceTakeover:
    """让果农接带真实余额的份额：改约前不替甲封候补果枝。

    ``reserved`` 为此刻被该份额实际占用（已挂签）的枝集合；没有挂签则空，
    临时来户可占实际可用的枝。
    """

    household_id: str
    share_id: str
    reserved_branches: frozenset[str] = frozenset()

    def occupies(self, branch_id: str) -> bool:
        return branch_id in self.reserved_branches


def available_branches_for_takeover(
    orchard: Orchard,
    session: HarvestSession,
    takeover: BalanceTakeover,
    desired: tuple[str, ...],
) -> tuple[str, ...]:
    """列出接真实余额时实际可占、且能覆盖所需果串的枝。

    甲改约前不替他封候补果枝：只要 ``takeover`` 没有挂签占用，临时来户就能
    占实际可用的枝。已剪走/越留芽线的枝不算实际可用。
    """
    result: set[str] = set()
    removed = set(session.outcome().removed_branches)
    for string_id in desired:
        branch = orchard.string(string_id).branch_id
        if branch in removed:
            continue
        for b in (branch, *orchard.ancestors(branch)):
            if not orchard.is_cuttable(b):
                continue
            if b in removed or takeover.occupies(b):
                continue
            if session.can_cut(b):
                result.add(b)
                break
    return tuple(sorted(result))
