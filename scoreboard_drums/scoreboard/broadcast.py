"""播报员换牌单：依指定版次的主客映射取真实已完赛比分。

比分事实只挂在 match 的主/客槽位上；版次（edition）决定哪个实物鼓显示
主槽位、哪个显示客槽位。换主客版次不会把鼓面清零，只是换牌单的取数映射变。
待赛（pending）或取消的比赛没有真实完赛比分，播报必须拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass

from .core import DRUMS, Stop, _validate_digit


class BroadcastError(ValueError):
    """换牌单无法签发（待赛、未知比赛/版次、越界分数等）。"""


@dataclass(frozen=True)
class MatchResult:
    match_id: str
    state: str  # played / pending / cancelled
    home_goals: int | None = None
    away_goals: int | None = None


@dataclass(frozen=True)
class Edition:
    """一版主客映射：哪个实物牌号坐主槽、哪个坐客槽。"""

    edition_id: str
    home_drum: str
    away_drum: str

    def __post_init__(self) -> None:
        if set((self.home_drum, self.away_drum)) != set(DRUMS):
            raise ValueError("版次必须把两只实物鼓分别映射到主/客槽")


@dataclass(frozen=True)
class ChangeOrder:
    """换牌单：目标联合止位按实物牌号表达，附取数来源以便审计。"""

    match_id: str
    edition_id: str
    target: Stop
    source_home_goals: int
    source_away_goals: int


class BroadcastOffice:
    """录入真实赛果、登记版次，并按指定版次签发换牌单。"""

    def __init__(self) -> None:
        self._results: dict[str, MatchResult] = {}
        self._editions: dict[str, Edition] = {}

    # ------------------------------------------------------------ 录入/版次
    def record_result(self, result: MatchResult) -> None:
        if result.state == "played":
            for name, value in (("home_goals", result.home_goals),
                                ("away_goals", result.away_goals)):
                _validate_digit(value)
        elif result.home_goals is not None or result.away_goals is not None:
            raise BroadcastError(f"{result.match_id} 未完赛，不得携带比分")
        self._results[result.match_id] = result

    def add_edition(self, edition: Edition) -> None:
        if edition.edition_id in self._editions:
            raise BroadcastError(f"版次 {edition.edition_id} 已存在且不可变")
        self._editions[edition.edition_id] = edition

    # ------------------------------------------------------------ 换牌单
    def issue_order(self, match_id: str, edition_id: str) -> ChangeOrder:
        result = self._results.get(match_id)
        if result is None:
            raise BroadcastError(f"未知比赛 {match_id}")
        if result.state != "played":
            raise BroadcastError(
                f"比赛 {match_id} 状态为 {result.state}，没有真实已完赛比分，拒绝播报")
        edition = self._editions.get(edition_id)
        if edition is None:
            raise BroadcastError(f"未知版次 {edition_id}")

        target = Stop({
            edition.home_drum: result.home_goals,
            edition.away_drum: result.away_goals,
        })
        return ChangeOrder(
            match_id=match_id,
            edition_id=edition_id,
            target=target,
            source_home_goals=result.home_goals,
            source_away_goals=result.away_goals,
        )
