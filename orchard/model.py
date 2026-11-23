"""果园树模型：主枝、枝节、挂果串与留芽约束。

领域要点（对应题面）：

* 单株是一棵有根的小枝树；每条枝（枝节）标有挂果串及其品种、级别，
  以及留在该枝段上的芽数。
* 一刀从选定的分叉剪下整条子树，其下的果与芽全离树；因此“剪某枝”
  等价于“移除该枝及其全部后代”。
* 枝上挂的每一串不可拆卖；多剪可留店，但会连带失掉该子树的芽。
* 每棵树的不同主枝各有留芽下限；主枝根是保留的，不能被剪，其自身的芽
  永远留在树上。
* 已采的枝回不到树上（剪是单调的、不可复原）。

本模块不含预算 / 份额逻辑，只做纯几何与芽数核算，便于独立测试。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from .errors import BudLimitError, InvalidOrchard, UnknownBranch, UnknownTree


@dataclass(frozen=True)
class FruitString:
    """挂在某条枝上的一整串果（不可拆卖）。"""

    string_id: str
    branch_id: str
    row: str
    variety: str
    grade: str


@dataclass(frozen=True)
class Branch:
    branch_id: str
    tree_id: str
    parent_id: Optional[str]
    buds: int                 # 本条枝段自身的芽数（不含后代）
    main_root_id: str         # 所属主枝的根枝节
    is_main_root: bool


@dataclass
class MainBranch:
    tree_id: str
    root_id: str
    min_buds: int


class Orchard:
    """果园：若干棵树，每棵由若干主枝组成，主枝由枝节构成。"""

    def __init__(self) -> None:
        self._branches: dict[str, Branch] = {}
        self._children: dict[str, list[str]] = {}
        self._strings: dict[str, FruitString] = {}
        self._tree_branches: dict[str, list[str]] = {}
        self._main_branches: dict[str, MainBranch] = {}
        self._main_root_of: dict[str, str] = {}   # branch -> main root id
        self._main_members: dict[str, list[str]] = {}

    # ---------------------------------------------------------------- 构建

    def add_tree(self, tree_id: str) -> None:
        if tree_id in self._tree_branches:
            raise InvalidOrchard(f"树 {tree_id} 已存在")
        self._tree_branches[tree_id] = []

    def add_main_branch(
        self, tree_id: str, root_id: str, *, buds: int, min_buds: int = 0
    ) -> None:
        """新增一个保留的主枝根（不可剪），并登记该主枝的留芽下限。"""
        self._require_tree(tree_id)
        if min_buds < 0 or buds < 0:
            raise InvalidOrchard("芽数与留芽下限不能为负")
        self._insert_branch(tree_id, root_id, None, buds, root_id, is_main_root=True)
        self._main_branches[root_id] = MainBranch(tree_id, root_id, min_buds)
        self._main_root_of[root_id] = root_id
        self._main_members[root_id] = [root_id]

    def add_branch(
        self, tree_id: str, branch_id: str, parent_id: str, *, buds: int
    ) -> None:
        """新增一条可剪枝节，挂到已有父枝下，继承父枝所属主枝。"""
        self._require_tree(tree_id)
        if buds < 0:
            raise InvalidOrchard("芽数不能为负")
        if parent_id not in self._branches:
            raise UnknownBranch(f"父枝 {parent_id} 不存在")
        if self._branches[parent_id].tree_id != tree_id:
            raise InvalidOrchard("父枝不属于同一棵树")
        main_root = self._main_root_of[parent_id]
        # 主枝根的后代不得再挂到别的主枝下：继承其主枝即可。
        self._insert_branch(tree_id, branch_id, parent_id, buds, main_root,
                            is_main_root=False)
        self._main_root_of[branch_id] = main_root
        self._main_members[main_root].append(branch_id)

    def add_string(
        self, string_id: str, branch_id: str, *, row: str, variety: str, grade: str
    ) -> None:
        if string_id in self._strings:
            raise InvalidOrchard(f"果串 {string_id} 已存在")
        if branch_id not in self._branches:
            raise UnknownBranch(f"枝节 {branch_id} 不存在")
        self._strings[string_id] = FruitString(string_id, branch_id, row, variety, grade)

    def _insert_branch(
        self,
        tree_id: str,
        branch_id: str,
        parent_id: Optional[str],
        buds: int,
        main_root_id: str,
        *,
        is_main_root: bool,
    ) -> None:
        if branch_id in self._branches:
            raise InvalidOrchard(f"枝节 {branch_id} 已存在")
        self._branches[branch_id] = Branch(
            branch_id, tree_id, parent_id, buds, main_root_id, is_main_root
        )
        self._children[branch_id] = []
        if parent_id is not None:
            self._children[parent_id].append(branch_id)
        self._tree_branches[tree_id].append(branch_id)

    def _require_tree(self, tree_id: str) -> None:
        if tree_id not in self._tree_branches:
            raise UnknownTree(f"树 {tree_id} 不存在")

    # ---------------------------------------------------------------- 查询

    @property
    def branch_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._branches))

    @property
    def string_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._strings))

    @property
    def tree_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._tree_branches))

    def branch(self, branch_id: str) -> Branch:
        try:
            return self._branches[branch_id]
        except KeyError:
            raise UnknownBranch(f"枝节 {branch_id} 不存在") from None

    def string(self, string_id: str) -> FruitString:
        try:
            return self._strings[string_id]
        except KeyError:
            raise UnknownBranch(f"果串 {string_id} 不存在") from None

    def main_branch(self, main_root_id: str) -> MainBranch:
        return self._main_branches[main_root_id]

    @property
    def main_root_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._main_branches))

    def strings_on_branch(self, branch_id: str) -> tuple[FruitString, ...]:
        return tuple(
            s for s in self._strings.values() if s.branch_id == branch_id
        )

    def subtree(self, branch_id: str) -> tuple[str, ...]:
        """自身 + 全部后代枝节（深度优先，包含自身）。"""
        self.branch(branch_id)
        out: list[str] = []
        stack = [branch_id]
        while stack:
            cur = stack.pop()
            out.append(cur)
            stack.extend(reversed(self._children[cur]))
        return tuple(out)

    def ancestors(self, branch_id: str) -> tuple[str, ...]:
        """父枝 → 主枝根（不含自身）。"""
        self.branch(branch_id)
        chain: list[str] = []
        cur = self._branches[branch_id].parent_id
        while cur is not None:
            chain.append(cur)
            cur = self._branches[cur].parent_id
        return tuple(chain)

    def is_ancestor(self, maybe_ancestor: str, node: str) -> bool:
        return maybe_ancestor in self.ancestors(node)

    def is_cuttable(self, branch_id: str) -> bool:
        return not self.branch(branch_id).is_main_root

    def total_buds(self, main_root_id: str) -> int:
        return sum(self._branches[b].buds for b in self._main_members[main_root_id])

    # ---------------------------------------------------------------- 校验

    def validate(self) -> None:
        if not self._main_branches:
            raise InvalidOrchard("果园至少需要一个主枝")
        # 每棵树至少一个主枝；主枝根无父；后代父唯一且成树。
        for tree in self._tree_branches:
            roots = [m.root_id for m in self._main_branches.values() if m.tree_id == tree]
            if not roots:
                raise InvalidOrchard(f"树 {tree} 没有主枝")
        for branch in self._branches.values():
            if branch.parent_id is not None and branch.parent_id not in self._branches:
                raise InvalidOrchard(f"枝节 {branch.branch_id} 的父枝不存在")
        for main in self._main_branches.values():
            if main.min_buds > self.total_buds(main.root_id):
                raise InvalidOrchard(
                    f"主枝 {main.root_id} 留芽下限 {main.min_buds} 超过总芽数 "
                    f"{self.total_buds(main.root_id)}，永不可满足"
                )
        # 无环：沿父链必须有限步到达某个主枝根。
        for branch in self._branches.values():
            seen: set[str] = set()
            cur: Optional[str] = branch.branch_id
            while cur is not None:
                if cur in seen:
                    raise InvalidOrchard(f"枝节 {cur} 处存在环")
                seen.add(cur)
                cur = self._branches[cur].parent_id
        # 每串挂果的枝存在（add_string 已保证），且主枝根不可被当作可剪。
        for s in self._strings.values():
            if s.branch_id not in self._branches:
                raise InvalidOrchard(f"果串 {s.string_id} 挂着枝不存在")


# ---------------------------------------------------------------- 剪枝核算


@dataclass(frozen=True)
class CutOutcome:
    """一组剪枝的核算结果。"""

    cut_nodes: tuple[str, ...]
    removed_branches: frozenset[str]
    harvested_strings: tuple[FruitString, ...]
    buds_lost: int
    remaining_buds: dict[str, int]     # 主枝根 -> 余芽

    def remaining_of(self, main_root_id: str) -> int:
        return self.remaining_buds[main_root_id]


def validate_cut_set(orchard: Orchard, cut_nodes: Iterable[str]) -> tuple[str, ...]:
    """校验剪集：只含可剪枝、两两不成祖先关系（最小剪集/反链）、不越留芽线。"""
    nodes = tuple(sorted(set(cut_nodes)))
    for b in nodes:
        branch = orchard.branch(b)
        if branch.is_main_root:
            raise BudLimitError(f"主枝根 {b} 是保留枝，不能被剪")
    for i, a in enumerate(nodes):
        for b in nodes[i + 1 :]:
            if orchard.is_ancestor(a, b) or orchard.is_ancestor(b, a):
                raise BudLimitError(
                    f"剪集不是最小反链：{a} 与 {b} 有祖孙关系，一刀已覆盖另一刀"
                )
    outcome = evaluate_cuts(orchard, nodes)
    for main_root, minimum in (
        (m.root_id, m.min_buds) for m in orchard._main_branches.values()
    ):
        if outcome.remaining_buds[main_root] < minimum:
            raise BudLimitError(
                f"主枝 {main_root} 剪后余芽 {outcome.remaining_buds[main_root]} "
                f"低于下限 {minimum}"
            )
    return nodes


def evaluate_cuts(orchard: Orchard, cut_nodes: Iterable[str]) -> CutOutcome:
    """计算剪集的移除集合、失芽、余芽与已采果串（不做合法性校验）。"""
    nodes = tuple(sorted(set(cut_nodes)))
    removed: set[str] = set()
    for node in nodes:
        removed.update(orchard.subtree(node))
    buds_lost = sum(orchard._branches[b].buds for b in removed)
    remaining: dict[str, int] = {}
    for main_root in orchard.main_root_ids:
        lost_here = sum(
            orchard._branches[b].buds
            for b in removed
            if orchard._main_root_of[b] == main_root
        )
        remaining[main_root] = orchard.total_buds(main_root) - lost_here
    harvested = tuple(
        sorted(
            (s for s in orchard._strings.values() if s.branch_id in removed),
            key=lambda s: s.string_id,
        )
    )
    return CutOutcome(nodes, frozenset(removed), harvested, buds_lost, remaining)


class HarvestSession:
    """跨多刀、跨早晨的采收会话：记录已落刀，保证不可复原、不越留芽线。"""

    def __init__(self, orchard: Orchard) -> None:
        self.orchard = orchard
        self._cut: list[str] = []

    @property
    def cut_nodes(self) -> tuple[str, ...]:
        return tuple(self._cut)

    def outcome(self) -> CutOutcome:
        return evaluate_cuts(self.orchard, self._cut)

    def remaining_buds(self) -> dict[str, int]:
        return self.outcome().remaining_buds

    def can_cut(self, branch_id: str) -> bool:
        """在现有已剪基础上再剪一枝是否合法（最小反链 + 留芽线）。"""
        if branch_id in self._cut:
            return False
        try:
            self.orchard.branch(branch_id)
        except UnknownBranch:
            return False
        if self.orchard.branch(branch_id).is_main_root:
            return False
        for existing in self._cut:
            if self.orchard.is_ancestor(existing, branch_id) or self.orchard.is_ancestor(
                branch_id, existing
            ):
                return False
        try:
            validate_cut_set(self.orchard, [*self._cut, branch_id])
        except BudLimitError:
            return False
        return True

    def cut(self, branch_id: str) -> CutOutcome:
        """落一刀；若枝已被祖枝覆盖或越留芽线则拒绝。"""
        if self.orchard.branch(branch_id).is_main_root:
            raise BudLimitError(f"主枝根 {branch_id} 不可剪")
        for existing in self._cut:
            if self.orchard.is_ancestor(existing, branch_id):
                raise BudLimitError(f"枝 {branch_id} 已被祖先 {existing} 的一刀带走")
        if not self.can_cut(branch_id):
            raise BudLimitError(f"在现有状态下剪 {branch_id} 会越留芽线")
        self._cut.append(branch_id)
        return self.outcome()
