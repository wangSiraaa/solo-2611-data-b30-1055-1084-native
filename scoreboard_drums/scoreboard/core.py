"""核心域模型：两只鼓的实物牌号、证据记录与只向前转的十进制规则。

鼓只向前转（forward wheel）：每收到一格脉冲，显示数字 +1 模 10，9 再走一格到 0。
所有记录都按实物牌号（serial）挂在鼓上，不随主客版次改变；换主队不清零。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from . import DIGITS

# 固定两只鼓的实物牌号。
DRUM_A = "A"
DRUM_B = "B"
DRUMS = (DRUM_A, DRUM_B)


class PulseStatus(str, Enum):
    """脉冲回执状态。ACK 一定走了一格；NAK 可能走也可能没走。"""

    ACK = "ack"
    NAK = "nak"


def advance(digit: int, steps: int = 1) -> int:
    """鼓只向前转：steps 格后的数字（模 10）。steps 非负。"""
    if steps < 0:
        raise ValueError("鼓只会向前转，不能回退")
    return (digit + steps) % DIGITS


def forward_distance(start: int, target: int) -> int:
    """从 start 只向前转到 target 需要的格数，范围 0..9（9->0 为 1 格）。"""
    return (target - start) % DIGITS


@dataclass(frozen=True)
class Seen:
    """电工上次亲眼见到的鼓面：该鼓在这条记录时显示 digit，是硬事实。"""

    seq: int
    drum: str
    digit: int

    def __post_init__(self) -> None:
        _validate_seq(self.seq)
        _validate_drum(self.drum)
        _validate_digit(self.digit)


@dataclass(frozen=True)
class Pulse:
    """发给指定鼓的一格脉冲。

    ack=ACK：确认前进一格；ack=NAK：失去回执，可能走了也可能没走。
    group 非 None 时为一次总闸失电脉冲组：同组要么全走、要么全丢，
    组内允许发给不同的鼓。
    """

    seq: int
    drum: str
    ack: PulseStatus
    group: str | None = None

    def __post_init__(self) -> None:
        _validate_seq(self.seq)
        _validate_drum(self.drum)
        if not isinstance(self.ack, PulseStatus):
            raise TypeError("ack 必须是 PulseStatus")


@dataclass(frozen=True)
class Contact:
    """独立齿位触片读数：该鼓当时是否停在 registered 登记齿位集合内。

    触片只回答在/不在这几个登记齿位，不直接报数字。
    """

    seq: int
    drum: str
    registered: tuple[int, ...]
    reading: bool

    def __post_init__(self) -> None:
        _validate_seq(self.seq)
        _validate_drum(self.drum)
        if not self.registered:
            raise ValueError("触片至少登记一个齿位")
        for d in self.registered:
            _validate_digit(d)
        if not isinstance(self.reading, bool):
            raise TypeError("触片读数必须为布尔值")


Record = Seen | Pulse | Contact


@dataclass(frozen=True)
class Stop:
    """联合止位：两只鼓各自可能停住的数字集合（按实物牌号）。"""

    digits: dict[str, int]

    def __post_init__(self) -> None:
        if set(self.digits) != set(DRUMS):
            raise ValueError("联合止位必须且只能包含两只鼓")
        for drum, digit in self.digits.items():
            _validate_drum(drum)
            _validate_digit(digit)

    @classmethod
    def of(cls, a: int, b: int) -> "Stop":
        return cls({DRUM_A: a, DRUM_B: b})

    def get(self, drum: str) -> int:
        _validate_drum(drum)
        return self.digits[drum]

    def key(self) -> tuple[int, int]:
        return (self.digits[DRUM_A], self.digits[DRUM_B])


def _validate_drum(drum: str) -> None:
    if drum not in DRUMS:
        raise ValueError(f"未知实物牌号 {drum!r}，合法值 {DRUMS}")


def _validate_digit(digit: int) -> None:
    if not isinstance(digit, bool) and isinstance(digit, int) and 0 <= digit < DIGITS:
        return
    raise ValueError(f"每鼓一位，数字必须是 0..9 的整数，得到 {digit!r}")


def _validate_seq(seq: int) -> None:
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise ValueError(f"seq 必须是非负整数，得到 {seq!r}")
