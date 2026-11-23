"""越线裁决：同拍真位置、倒数第二防线、严格领先与跨解释聚合。

规则（题面）：
- 每个合法解释下取同一正式拍的真位置，朝攻门方向越大越靠前；
- 接球队员必须严格领先球，且严格领先对手倒数第二名防守人，平齐不算；
- 防线按各具名防守人的真位置排序；
- 位于“所有合法出球拍 × 所有校场解释”都越线才可确认，
  全部未越则驳回，其余保留相反的两场景；
- 输入承诺各帧含所需球员，但仍须能报缺帧：任一必需对象缺帧的单元不可裁决。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .model import (AdmissionResult, CaseFile, FrameObservation,
                    JointInterpretation, Verdict)
from .schedule import Side, team_attack_sign


@dataclass(frozen=True)
class TruePosition:
    camera_id: str
    label: str
    beat: int
    camera_tick: int
    grid: int
    true_position: int


@dataclass(frozen=True)
class EvidenceCell:
    """一个 (出球拍, 联合解释) 单元的可审计裁决依据。"""

    beat: int
    interpretation_index: int
    decidable: bool
    offside: Optional[bool]
    receiver_position: Optional[int]
    ball_position: Optional[int]
    second_last_defender: Optional[Tuple[str, int]]
    missing: Tuple[str, ...] = field(default_factory=tuple)
    conflicts: Tuple[str, ...] = field(default_factory=tuple)
    reason: str = ""


@dataclass(frozen=True)
class ReviewDecision:
    verdict: Verdict
    cells: Tuple[EvidenceCell, ...]
    missing_frames: Tuple[str, ...]
    retained_scenarios: Tuple[EvidenceCell, ...]  # PENDING 时相反两场景
    conflicts: Tuple[str, ...] = field(default_factory=tuple)


class _FrameIndex:
    """按机位索引采样：(camera_tick, label) -> grid。"""

    def __init__(self, observations: Sequence[FrameObservation]):
        self._map: Dict[Tuple[str, int, str], int] = {}
        self._duplicate_conflicts: List[str] = []
        for obs in observations:
            key = (obs.camera_id, obs.camera_tick, obs.label)
            if key in self._map and self._map[key] != obs.grid:
                self._duplicate_conflicts.append(
                    f"{obs.camera_id} 机内拍 {obs.camera_tick} 的 {obs.label} "
                    f"出现冲突格号 {self._map[key]} 与 {obs.grid}")
            self._map[key] = obs.grid

    def grid(self, camera_id: str, camera_tick: int, label: str) -> Optional[int]:
        return self._map.get((camera_id, camera_tick, label))

    @property
    def conflicts(self) -> Tuple[str, ...]:
        return tuple(self._duplicate_conflicts)


def _required_labels(case: CaseFile) -> Tuple[str, ...]:
    return (case.receiver, case.ball_label, *case.defender_labels)


def materialize(case: CaseFile, interpretation: JointInterpretation,
                beat: int) -> Tuple[Dict[str, TruePosition], Tuple[str, ...],
                                    Tuple[str, ...]]:
    """把一个解释下某正式拍的全部机位采样投影为真位置并合并。

    返回 (合并真位置, 冲突说明, 缺帧说明)。多台机对同一对象给出不同真位置
    属于证据冲突；任何机位缺该帧只是该帧不可用，不伪造数据。
    """

    index = _FrameIndex(case.observations)
    merged: Dict[str, TruePosition] = {}
    conflicts: List[str] = []
    missing: List[str] = []
    # 记录每个对象各采样来自哪一“同步簇”：同一同步簇意味着同一次物理采样。
    # 没有同步灯把两台机的该帧绑定时，不同机只是各拍各的瞬间，不得互判冲突。
    for label in _required_labels(case):
        seen: List[TruePosition] = []
        for camera_id in interpretation.axes:
            tick = interpretation.camera_tick(camera_id, beat)
            grid = index.grid(camera_id, tick, label)
            if grid is None:
                continue
            axis = interpretation.axes[camera_id]
            seen.append(TruePosition(
                camera_id, label, beat, tick, grid, axis.to_field(grid)))
        if not seen:
            missing.append(label)
            continue
        # 仅当两台机该帧机内拍相同（同源采样的代理判据）时才核对一致性
        same_source = len({s.camera_tick for s in seen}) == 1
        positions = {s.true_position for s in seen}
        if same_source and len(positions) > 1:
            conflicts.append(
                f"正式拍 {beat} 的 {label} 同源帧多机真位置冲突："
                + ", ".join(f"{s.camera_id}->{s.true_position}" for s in seen))
        merged[label] = seen[0]
    return merged, tuple(conflicts), tuple(missing)


def _evaluate_cell(case: CaseFile, interpretation: JointInterpretation,
                   interpretation_index: int, beat: int,
                   sign: int) -> EvidenceCell:
    positions, conflicts, missing = materialize(case, interpretation, beat)
    required = _required_labels(case)
    absent = tuple(sorted(label for label in required if label not in positions))
    if absent or conflicts:
        return EvidenceCell(
            beat=beat, interpretation_index=interpretation_index,
            decidable=False, offside=None,
            receiver_position=None, ball_position=None,
            second_last_defender=None, missing=absent, conflicts=conflicts,
            reason="; ".join(tuple(conflicts)
                             + tuple(f"缺帧：{m}@拍{beat}" for m in missing)))

    receiver = positions[case.receiver].true_position
    ball = positions[case.ball_label].true_position
    # 按“靠前值”升序：越靠后（贴近防守方门线）的防守人越排在后面，
    # 最后一名 [-1] 是门线前最后防守人，[-2] 即倒数第二名防守人。
    defenders = sorted(
        ((label, positions[label].true_position) for label in case.defender_labels),
        key=lambda item: sign * item[1])
    if len(defenders) < 2:
        raise ValueError("越线裁决至少需要两名具名防守人")
    second_last = defenders[-2]

    # 越线比较统一走靠前值差：sign*(接应位置 - 参照物位置) > 0
    ahead_ball = sign * (receiver - ball) > 0
    ahead_line = sign * (receiver - second_last[1]) > 0  # 严格领先，平齐不算
    offside = ahead_ball and ahead_line
    reason = (f"接应 {receiver}；球 {ball}（严格领先球：{ahead_ball}）；"
              f"倒数第二防线 {second_last[0]}={second_last[1]}"
              f"（严格领先防线：{ahead_line}）")
    return EvidenceCell(
        beat=beat, interpretation_index=interpretation_index,
        decidable=True, offside=offside,
        receiver_position=receiver, ball_position=ball,
        second_last_defender=second_last, reason=reason)


def review(case: CaseFile, admission: AdmissionResult,
           reviewing_side: Side) -> ReviewDecision:
    """对已校场素材做越线裁决。reviewing_side 为接球队员所属方。"""

    if not admission.admitted:
        return ReviewDecision(Verdict.NOT_ADMITTED, (), (), ())

    sign = team_attack_sign(case.home_attack_end, reviewing_side)
    cells: List[EvidenceCell] = []
    for idx, interpretation in enumerate(admission.interpretations):
        for beat in sorted(case.release_beats):
            cells.append(_evaluate_cell(
                case, interpretation, idx, beat, sign))

    decidable = [c for c in cells if c.decidable]
    missing_frames = tuple(sorted({
        f"正式拍 {c.beat} 解释#{c.interpretation_index} 缺 {m}"
        for c in cells for m in c.missing}))
    conflicts = tuple(dict.fromkeys(
        item for c in cells for item in c.conflicts))

    if not decidable:
        return ReviewDecision(Verdict.PENDING, tuple(cells), missing_frames,
                              (), conflicts)

    flags = {c.offside for c in decidable}
    if flags == {True}:
        verdict = Verdict.CONFIRMED
    elif flags == {False}:
        verdict = Verdict.REJECTED
    else:
        verdict = Verdict.PENDING

    retained: Tuple[EvidenceCell, ...] = ()
    if verdict == Verdict.PENDING:
        offside_cell = next(c for c in decidable if c.offside)
        onside_cell = next(c for c in decidable if not c.offside)
        retained = (offside_cell, onside_cell)
    return ReviewDecision(verdict, tuple(cells), missing_frames,
                          retained, conflicts)
