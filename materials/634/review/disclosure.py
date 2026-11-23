"""两种给裁判的披露方式，支持隔离未经授权的其他球员原始位置。

完整视图：把具名球员的同步场地坐标签出，裁判在离线席自行重排防线。
最小视图：裁判先交待核球员和时间段，校场人只签出能同时成立的
逐拍相对次序与球边界，不泄露其他球员的原始位置。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Sequence, Tuple

from .model import CaseFile, JointInterpretation
from .offside import TruePosition, materialize
from .schedule import Side, team_attack_sign


@dataclass(frozen=True)
class CoordinateRow:
    label: str
    beat: int
    true_position: int
    sources: Tuple[str, ...]


@dataclass(frozen=True)
class FullDisclosure:
    """完整视图：授权球员逐拍同步场地坐标。"""

    authorized: Tuple[str, ...]
    beats: Tuple[int, ...]
    rows: Tuple[CoordinateRow, ...]
    withheld_labels: Tuple[str, ...]
    conflicts: Tuple[str, ...]


@dataclass(frozen=True)
class OrderingStatement:
    """逐拍相对次序：只含授权球员，按靠前到靠后排列。"""

    beat: int
    order: Tuple[str, ...]
    relations: Tuple[str, ...]          # 形如 "R > D1" / "R = ball"
    ball_boundary: str                  # 球边界：授权接应人相对球的位置


@dataclass(frozen=True)
class MinimalDisclosure:
    """最小视图：相对次序 + 球边界，不含任何未授权球员原始坐标。"""

    authorized: Tuple[str, ...]
    beats: Tuple[int, ...]
    statements: Tuple[OrderingStatement, ...]
    withheld_labels: Tuple[str, ...]
    missing: Tuple[str, ...]


def _relation(left: str, right: str, pos_left: int, pos_right: int,
              sign: int) -> str:
    delta = sign * (pos_left - pos_right)
    symbol = ">" if delta > 0 else ("<" if delta < 0 else "=")
    return f"{left} {symbol} {right}"


def full_coordinate_view(case: CaseFile,
                         interpretation: JointInterpretation,
                         beats: Sequence[int],
                         authorized_labels: FrozenSet[str]) -> FullDisclosure:
    """完整视图：仅签出授权具名球员（含球，若授权）的真坐标。"""

    rows: List[CoordinateRow] = []
    conflicts: List[str] = []
    all_labels = set(case.defender_labels) | {case.passer, case.receiver,
                                              case.ball_label}
    for beat in beats:
        positions, cell_conflicts, _ = materialize(case, interpretation, beat)
        conflicts.extend(cell_conflicts)
        per_label: Dict[str, List[TruePosition]] = {}
        for pos in positions.values():
            if pos.label in authorized_labels:
                per_label.setdefault(pos.label, []).append(pos)
        for label in sorted(authorized_labels):
            if label not in per_label:
                continue
            seen = per_label[label]
            rows.append(CoordinateRow(
                label=label, beat=beat,
                true_position=seen[0].true_position,
                sources=tuple(s.camera_id for s in seen)))
    withheld = tuple(sorted(all_labels - authorized_labels))
    return FullDisclosure(
        authorized=tuple(sorted(authorized_labels)), beats=tuple(beats),
        rows=tuple(rows), withheld_labels=withheld,
        conflicts=tuple(dict.fromkeys(conflicts)))


def minimal_ordering_view(case: CaseFile,
                          interpretation: JointInterpretation,
                          beats: Sequence[int],
                          reviewing_side: Side,
                          authorized_labels: FrozenSet[str]
                          ) -> MinimalDisclosure:
    """最小视图：只签授权球员的逐拍相对次序与球边界。

    未授权球员（包括未授权防守人）的原始位置一律不出现；
    若授权集合不足以支撑某拍，该拍记入 missing 而不是补数据。
    """

    sign = team_attack_sign(case.home_attack_end, reviewing_side)
    statements: List[OrderingStatement] = []
    missing: List[str] = []
    all_labels = set(case.defender_labels) | {case.passer, case.receiver,
                                              case.ball_label}
    for beat in beats:
        positions, _, absent = materialize(case, interpretation, beat)
        unavailable = sorted((set(authorized_labels) & all_labels)
                             - set(positions))
        if unavailable:
            missing.append(f"拍 {beat} 缺授权对象：{', '.join(unavailable)}")
            continue
        items = sorted(
            ((label, positions[label].true_position)
             for label in authorized_labels if label in positions),
            key=lambda item: sign * item[1], reverse=True)
        order = tuple(label for label, _ in items)
        relations = tuple(
            _relation(items[i][0], items[j][0],
                      items[i][1], items[j][1], sign)
            for i in range(len(items)) for j in range(i + 1, len(items)))
        if case.receiver in authorized_labels and case.ball_label in positions \
                and case.receiver in positions:
            ball_boundary = _relation(
                case.receiver, case.ball_label,
                positions[case.receiver].true_position,
                positions[case.ball_label].true_position, sign)
        else:
            ball_boundary = "未授权接应人或球，球边界不可签出"
        statements.append(OrderingStatement(
            beat=beat, order=order, relations=relations,
            ball_boundary=ball_boundary))
    withheld = tuple(sorted(all_labels - authorized_labels))
    return MinimalDisclosure(
        authorized=tuple(sorted(authorized_labels)), beats=tuple(beats),
        statements=tuple(statements), withheld_labels=withheld,
        missing=tuple(missing))
