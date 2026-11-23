"""领域模型与输入合法性约束。

约束（题面硬性上限）：
- 场地为一维整数坐标，范围 0..100；
- 同一场最多三台机、正式节拍 1..10；
- 每台机卡册给出的候选时钟偏移至多五个；
- 每帧具名防守人至多六名；
- 标杆的原画格坐标与其实际位置均为整数，平移量为整数。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import FrozenSet, List, Mapping, Optional, Sequence, Tuple

FIELD_MIN = 0
FIELD_MAX = 100
MAX_CAMERAS = 3
MAX_BEATS = 10
MAX_OFFSETS_PER_CAMERA = 5
MAX_DEFENDERS = 6


class Side(str, Enum):
    """赛程槽位：主队 / 客队。"""

    HOME = "home"
    AWAY = "away"


class AttackEnd(str, Enum):
    """半场开场单记录的主队进攻端（0 端或 100 端）。"""

    LOW = "low"    # 主队进攻 0 端，进攻方向为数值减小
    HIGH = "high"  # 主队进攻 100 端，进攻方向为数值增大


class Verdict(str, Enum):
    """越线裁决结论。"""

    CONFIRMED = "confirmed"  # 所有合法出球拍 × 所有校场解释均越线
    REJECTED = "rejected"    # 所有单元均未越线（含平齐）
    PENDING = "pending"      # 其余情形：保留相反的两场景
    NOT_ADMITTED = "not_admitted"  # 素材互相矛盾，无联合解释，不能准入


class AdmissionCode(str, Enum):
    """校场失败的可定位原因。"""

    NO_AXIS_SOLUTION = "no_axis_solution"        # 标杆无相容方向/平移
    NO_OFFSET_IN_ALBUM = "no_offset_in_album"    # 同步灯要求的偏移不在卡册
    FLASH_CONFLICT = "flash_conflict"            # 同编号闪灯跨机对不上同一拍
    SYNC_OUT_OF_RANGE = "sync_out_of_range"      # 同步卡允许拍与映射拍不相交
    OFFSET_OVER_LIMIT = "offset_over_limit"      # 卡册候选偏移超过五个
    BAD_LIMITS = "bad_limits"                    # 违反机位/节拍/防守人等硬上限


@dataclass(frozen=True)
class BenchmarkPair:
    """标杆卡：原画格坐标 grid 与可能的实际标杆位置 actuals（整数集）。"""

    grid: int
    actuals: FrozenSet[int]


@dataclass(frozen=True)
class FlashSighting:
    """同步灯卡：机内格号 camera_tick 与允许对应的正式节拍 allowed_beats。"""

    flash_id: str
    camera_tick: int
    allowed_beats: FrozenSet[int]


@dataclass(frozen=True)
class CameraSpec:
    """一台机的卡册：标杆、同步灯、候选时钟偏移（至多五个）。"""

    camera_id: str
    benchmarks: Tuple[BenchmarkPair, ...]
    flashes: Tuple[FlashSighting, ...]
    offset_album: FrozenSet[int]


@dataclass(frozen=True)
class AxisSolution:
    """横轴解释：true = sign * grid + translation。"""

    sign: int
    translation: int

    def to_field(self, grid: int) -> int:
        return self.sign * grid + self.translation


@dataclass(frozen=True)
class JointInterpretation:
    """一个全机相容的联合解释：每台机一个横轴解释与一个时钟偏移。"""

    axes: Mapping[str, AxisSolution]
    offsets: Mapping[str, int]

    def camera_tick(self, camera_id: str, official_beat: int) -> int:
        """正式拍 -> 该机机内拍：camera_tick = official_beat + offset。"""

        return official_beat + self.offsets[camera_id]


@dataclass(frozen=True)
class AdmissionFailure:
    code: AdmissionCode
    camera_id: Optional[str]
    detail: str


@dataclass(frozen=True)
class AdmissionResult:
    admitted: bool
    interpretations: Tuple[JointInterpretation, ...]
    failures: Tuple[AdmissionFailure, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class FrameObservation:
    """机内一帧对某具名对象的一维格号采样。"""

    camera_id: str
    camera_tick: int
    label: str
    grid: int


@dataclass(frozen=True)
class CaseFile:
    """一场一个半场的复盘素材包。"""

    match_id: str
    half: int
    home_team: str
    away_team: str
    home_attack_end: AttackEnd
    cameras: Tuple[CameraSpec, ...]
    observations: Tuple[FrameObservation, ...]
    release_beats: FrozenSet[int]          # 可能出球的正式拍段
    passer: str
    receiver: str
    defender_labels: Tuple[str, ...]       # 具名防守人（<=6）
    ball_label: str = "ball"


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def validate_limits(specs: Sequence[CameraSpec],
                    beats: Sequence[int],
                    defender_labels: Sequence[str]) -> List[AdmissionFailure]:
    """硬上限校验，失败以 AdmissionFailure(BAD_LIMITS) 逐条返回。"""

    failures: List[AdmissionFailure] = []
    if len(specs) > MAX_CAMERAS:
        failures.append(AdmissionFailure(
            AdmissionCode.BAD_LIMITS, None,
            f"机位 {len(specs)} 超过上限 {MAX_CAMERAS}"))
    for spec in specs:
        if len(spec.offset_album) > MAX_OFFSETS_PER_CAMERA:
            failures.append(AdmissionFailure(
                AdmissionCode.OFFSET_OVER_LIMIT, spec.camera_id,
                f"候选偏移 {len(spec.offset_album)} 个，超过上限 "
                f"{MAX_OFFSETS_PER_CAMERA}"))
    for beat in beats:
        if not (1 <= beat <= MAX_BEATS):
            failures.append(AdmissionFailure(
                AdmissionCode.BAD_LIMITS, None,
                f"正式节拍 {beat} 超出 1..{MAX_BEATS}"))
    if len(defender_labels) > MAX_DEFENDERS:
        failures.append(AdmissionFailure(
            AdmissionCode.BAD_LIMITS, None,
            f"具名防守人 {len(defender_labels)} 超过上限 {MAX_DEFENDERS}"))
    if len({d for d in defender_labels}) != len(defender_labels):
        failures.append(AdmissionFailure(
            AdmissionCode.BAD_LIMITS, None, "防守人标签重复"))
    ids = [s.camera_id for s in specs]
    if len(set(ids)) != len(ids):
        failures.append(AdmissionFailure(
            AdmissionCode.BAD_LIMITS, None, "机位 ID 重复"))
    return failures


def validate_case(case: CaseFile) -> None:
    """CaseFile 结构级自检；非法直接抛 ValueError。"""

    _assert(case.home_team != case.away_team, "主客必须是不同球队")
    _assert(1 <= case.half <= 2, "半场编号只允许 1 或 2")
    failures = validate_limits(case.cameras, sorted(case.release_beats),
                               case.defender_labels)
    _assert(not failures, "; ".join(f.detail for f in failures))
    known = {s.camera_id for s in case.cameras}
    for obs in case.observations:
        _assert(obs.camera_id in known,
                f"采样引用未知机位 {obs.camera_id}")
        _assert(isinstance(obs.grid, int)
                and -MAX_BEATS <= obs.grid <= FIELD_MAX + MAX_BEATS,
                f"格号 {obs.grid} 越界或非整数")
    labels = set(case.defender_labels) | {case.passer, case.receiver,
                                          case.ball_label}
    for obs in case.observations:
        _assert(obs.label in labels,
                f"采样对象 {obs.label} 不在名单内")
    for beat in case.release_beats:
        _assert(1 <= beat <= MAX_BEATS,
                f"出球拍 {beat} 超出 1..{MAX_BEATS}")
    _assert(case.release_beats, "出球拍段不能为空")
