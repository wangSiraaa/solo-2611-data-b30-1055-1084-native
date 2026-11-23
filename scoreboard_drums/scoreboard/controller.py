"""馆内硬件与现场控制器：保管交班可能止位，随读头逐拍决策。

与电工席的操作卡方案并列，控制器方案把“可能止位 + 策略树”都放在馆内，
现场与电工席断开连接仍可独立工作；换目标不需要换卡，在馆内按当前剩余
止位重算即可。换班时必须交接已花拍数、真实动作与剩余止位（快照可 JSON
落盘，下一班原样恢复）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from . import MAX_BEATS
from .core import DRUMS, Stop, advance as wheel_advance
from .planner import (
    Belief,
    Plan,
    PolicyNode,
    apply_advance,
    apply_read,
    belief_from_stops,
    plan as build_plan,
)
from .reconcile import ReconcileResult


class DrumHardware:
    """两只只向前转的实物鼓；读头如实报当前数字，幕内回执可靠。"""

    def __init__(self, truth: Stop):
        self._digits = dict(truth.digits)

    def advance(self, drum: str) -> int:
        self._digits[drum] = wheel_advance(self._digits[drum])
        return self._digits[drum]

    def read(self, drum: str) -> int:
        return self._digits[drum]

    def snapshot(self) -> dict[str, int]:
        return dict(self._digits)


@dataclass(frozen=True)
class Decision:
    issuable: bool
    reason: str
    plan: Plan | None

    def to_dict(self) -> dict:
        return {
            "issuable": self.issuable,
            "reason": self.reason,
            "worst_beats": self.plan.worst_beats if self.plan else None,
            "horizon": self.plan.horizon if self.plan else None,
        }


def decide(result: ReconcileResult, target: Stop,
           curtain_beats: int = MAX_BEATS) -> Decision:
    """馆方总判定：遮幕时限内对交班单允许的每种真相都可执行才发，否则留幕。"""
    if not result.feasible:
        return Decision(False, "交班记录自相矛盾，无任何可成立止位，留幕不发", None)
    plan = build_plan(result.stops, target, curtain_beats)
    if plan.issuable(curtain_beats):
        return Decision(
            True,
            f"可发：最坏 {plan.worst_beats} 拍（遮幕 {curtain_beats} 拍）",
            plan,
        )
    return Decision(
        False,
        f"留幕不发：最坏需要 {plan.worst_beats} 拍，超过遮幕 {curtain_beats} 拍",
        plan,
    )


class SiteController:
    """馆内控制器：持有信念（剩余止位）与策略树，逐拍驱动硬件。"""

    def __init__(self, stops: list[Stop] | tuple[Stop, ...], target: Stop,
                 horizon: int = MAX_BEATS):
        self.belief: Belief = belief_from_stops(stops)
        self.target: Stop = target
        self.horizon = horizon
        self.node: PolicyNode | None = None
        self.beats_used = 0
        self.actions_done: list[dict] = []
        self._replan(horizon)

    # ------------------------------------------------------------ 规划/换目标
    def _replan(self, horizon: int) -> None:
        plan = build_plan(tuple(Stop({d: s[i] for i, d in enumerate(DRUMS)})
                                for s in self.belief),
                          self.target, horizon)
        if not plan.feasible:
            raise ValueError(
                f"当前剩余止位在 {horizon} 拍内无法保证到达 {self.target.key()}")
        self.node = plan.root
        self.current_plan = plan

    def retarget(self, new_target: Stop, horizon: int | None = None) -> Plan:
        """换目标：不清零已花拍数与已做动作，按当前剩余止位在馆内重算。"""
        self.target = new_target
        self.horizon = self.horizon if horizon is None else horizon
        self._replan(self.horizon)
        return self.current_plan

    # ------------------------------------------------------------ 逐拍执行
    def finished(self) -> bool:
        return self.node is not None and self.node.done

    def step(self, hardware: DrumHardware) -> dict:
        if self.finished():
            raise RuntimeError("策略已完成，无下一拍")
        action = self.node.action
        beat = self.beats_used + 1
        if action.kind == "advance":
            observed = None
            hardware.advance(action.drum)
            self.belief = apply_advance(self.belief, action.drum)
            self.node = self.node.next
        else:
            observed = hardware.read(action.drum)
            self.belief = apply_read(self.belief, action.drum, observed)
            self.node = self.node.branches[observed]
        record = {"beat": beat, "action": action.kind, "drum": action.drum}
        if observed is not None:
            record["observed"] = observed
        self.actions_done.append(record)
        self.beats_used = beat
        return record

    def run(self, hardware: DrumHardware) -> dict:
        while not self.finished():
            self.step(hardware)
        return self.handover(hardware)

    # ------------------------------------------------------------ 交班快照
    def handover(self, hardware: DrumHardware | None = None) -> dict:
        """换班交接：已花拍数、真实动作、剩余止位、目标与剩余策略深度。

        传入硬件时一并附当前真实鼓面快照，便于下一班当面核对。
        """
        snapshot = {
            "beats_used": self.beats_used,
            "actions_done": [dict(a) for a in self.actions_done],
            "remaining_stops": [list(s) for s in sorted(self.belief)],
            "target": list(self.target.key()),
            "horizon": self.horizon,
            "remaining_beats_worst": (0 if self.finished()
                                      else self.node.depth()),
        }
        if hardware is not None:
            snapshot["hardware"] = hardware.snapshot()
        return snapshot

    def to_json(self, hardware: DrumHardware | None = None) -> str:
        return json.dumps(self.handover(hardware), ensure_ascii=False, indent=2)

    @classmethod
    def from_handover(cls, snapshot: dict) -> "SiteController":
        """下一班凭快照恢复：已花拍数与动作原样延续，只重算剩余策略。"""
        stops = tuple(Stop.of(a, b) for a, b in snapshot["remaining_stops"])
        target = Stop.of(*snapshot["target"])
        controller = cls(stops, target, snapshot["horizon"])
        controller.beats_used = snapshot["beats_used"]
        controller.actions_done = [dict(a) for a in snapshot["actions_done"]]
        return controller
