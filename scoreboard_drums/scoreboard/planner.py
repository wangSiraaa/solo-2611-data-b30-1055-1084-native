"""遮幕内逐拍规划：信念集上的有界动作策略树。

幕内每拍只许做一件事：
- advance：给一只鼓走一格（本次有幕操作的回执都可靠，走了就是走了）；
- read：用读头认清一只鼓的当前数字，读数决定下一拍走哪个分支。

规划器不知道真相，只知道交班核对给出的可能止位集合（信念）。它在信念上
递归搜索最坏情况下拍数最少的策略：

- 信念已全部等于目标 -> 0 拍完成；
- 读一只鼓按实际读数分叉，深度取各分支最大值（要对每种真相都可执行）；
- 走一格把信念里该鼓的数字同步前移；
- 显然无用的动作直接剪掉（对所有止位都已到位的鼓不再走；信念对该鼓只有
  一种数字时不读）。

同深度优先读头（先认清再动），其次按鼓号 A、B，保证策略可复现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import MAX_BEATS
from .core import DRUMS, Stop, advance, forward_distance
from .reconcile import ReconcileResult

Belief = frozenset[tuple[int, int]]


@dataclass(frozen=True)
class Action:
    kind: str  # "read" | "advance"
    drum: str

    def label(self) -> str:
        verb = "读头认清" if self.kind == "read" else "走一格"
        return f"{verb} {self.drum} 鼓"


@dataclass(frozen=True)
class PolicyNode:
    """策略树节点。

    done=True 为叶子；read 节点用 branches（读数 -> 子节点），
    advance 节点用 next 单子节点。
    """

    action: Optional[Action]
    done: bool = False
    branches: dict[int, "PolicyNode"] = field(default_factory=dict)
    next: Optional["PolicyNode"] = None

    def depth(self) -> int:
        if self.done:
            return 0
        if self.action is None:
            return 0
        if self.action.kind == "read":
            return 1 + max(child.depth() for child in self.branches.values())
        return 1 + (self.next.depth() if self.next else 0)

    def to_dict(self) -> dict:
        if self.done:
            return {"done": True}
        node = {"action": {"kind": self.action.kind, "drum": self.action.drum}}
        if self.action.kind == "read":
            node["branches"] = {
                str(digit): child.to_dict() for digit, child in sorted(self.branches.items())
            }
        else:
            node["next"] = self.next.to_dict() if self.next else {"done": True}
        return node


@dataclass(frozen=True)
class Plan:
    feasible: bool
    root: Optional[PolicyNode]
    worst_beats: int
    horizon: int
    target: tuple[int, int]

    def issuable(self, curtain_beats: int) -> bool:
        """遮幕时限 curtain_beats 内能否对每种真相都保证完成。"""
        return self.feasible and self.worst_beats <= curtain_beats


def initial_belief(result: ReconcileResult) -> Belief:
    return frozenset(s.key() for s in result.stops)


def belief_from_stops(stops: list[Stop] | tuple[Stop, ...]) -> Belief:
    return frozenset(s.key() for s in stops)


def apply_advance(belief: Belief, drum: str) -> Belief:
    idx = DRUMS.index(drum)
    return frozenset(
        tuple(advance(d, 1) if i == idx else d for i, d in enumerate(stop))
        for stop in belief
    )


def apply_read(belief: Belief, drum: str, observed: int) -> Belief:
    idx = DRUMS.index(drum)
    return frozenset(stop for stop in belief if stop[idx] == observed)


def possible_readings(belief: Belief, drum: str) -> tuple[int, ...]:
    idx = DRUMS.index(drum)
    return tuple(sorted({stop[idx] for stop in belief}))


def _advance_useful(belief: Belief, drum: str, target: tuple[int, int]) -> bool:
    idx = DRUMS.index(drum)
    return any(forward_distance(stop[idx], target[idx]) > 0 for stop in belief)


def _read_useful(belief: Belief, drum: str) -> bool:
    return len(possible_readings(belief, drum)) > 1


def _actions(belief: Belief, target: tuple[int, int]) -> list[Action]:
    """候选动作：读头优先、鼓号有序；只保留可能有用的动作。"""
    ordered = [Action("read", d) for d in DRUMS] + [Action("advance", d) for d in DRUMS]
    actions = []
    for a in ordered:
        if a.kind == "read" and _read_useful(belief, a.drum):
            actions.append(a)
        elif a.kind == "advance" and _advance_useful(belief, a.drum, target):
            actions.append(a)
    return actions


def plan(stops: list[Stop] | tuple[Stop, ...] | ReconcileResult,
         target: Stop, horizon: int = MAX_BEATS) -> Plan:
    """求最坏情况下拍数最少的策略；horizon 拍内无解则 feasible=False。"""
    if isinstance(stops, ReconcileResult):
        belief = initial_belief(stops)
    else:
        belief = belief_from_stops(stops)
    if not (0 <= horizon <= MAX_BEATS):
        raise ValueError(f"遮幕时限限 {MAX_BEATS} 拍")
    memo: dict[tuple, PolicyNode | None] = {}
    root = _solve(belief, target.key(), horizon, memo)
    return Plan(
        feasible=root is not None,
        root=root,
        worst_beats=root.depth() if root else horizon + 1,
        horizon=horizon,
        target=target.key(),
    )


def _solve(belief: Belief, target: tuple[int, int], beats_left: int,
           memo: dict) -> PolicyNode | None:
    if all(stop == target for stop in belief):
        return PolicyNode(action=None, done=True)
    if beats_left == 0:
        return None

    key = (belief, target, beats_left)
    if key in memo:
        return memo[key]

    best: PolicyNode | None = None
    for action in _actions(belief, target):
        node = _expand(action, belief, target, beats_left, memo)
        if node is not None and (best is None or node.depth() < best.depth()):
            best = node
    memo[key] = best
    return best


def _expand(action: Action, belief: Belief, target: tuple[int, int],
            beats_left: int, memo: dict) -> PolicyNode | None:
    if action.kind == "advance":
        child = _solve(apply_advance(belief, action.drum), target,
                       beats_left - 1, memo)
        return PolicyNode(action=action, next=child) if child is not None else None

    branches: dict[int, PolicyNode] = {}
    for digit in possible_readings(belief, action.drum):
        child = _solve(apply_read(belief, action.drum, digit), target,
                       beats_left - 1, memo)
        if child is None:
            return None
        branches[digit] = child
    return PolicyNode(action=action, branches=branches)
