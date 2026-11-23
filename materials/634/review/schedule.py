"""旧赛程查询与每半场进攻方向。

每场在旧赛程中查真实主客；每半场只由开场单记录哪一端是主队进攻端，
换边后整段反向。进攻位置统一按“朝攻门方向越大越靠前”的真位置比较，
因此越线判定只依赖场地真坐标 + 进攻符号 attack_sign。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Tuple

from .model import AttackEnd, Side


@dataclass(frozen=True)
class MatchSlot:
    match_id: str
    home: str
    away: str

    def team_on(self, side: Side) -> str:
        return self.home if side == Side.HOME else self.away


class ScheduleBook:
    """只读旧赛程：按 match_id 查真实主客槽位。"""

    def __init__(self, fixtures: Tuple[Mapping[str, str], ...]):
        self._by_id = {
            f["match_id"]: MatchSlot(f["match_id"], f["home"], f["away"])
            for f in fixtures
        }

    def lookup(self, match_id: str) -> MatchSlot:
        if match_id not in self._by_id:
            raise KeyError(f"旧赛程中没有比赛 {match_id}")
        return self._by_id[match_id]

    def side_of(self, match_id: str, team: str) -> Side:
        slot = self.lookup(match_id)
        if team == slot.home:
            return Side.HOME
        if team == slot.away:
            return Side.AWAY
        raise KeyError(f"球队 {team} 不在比赛 {match_id} 名单内")


def attack_sign(home_attack_end: AttackEnd) -> int:
    """主队进攻符号：进攻 HIGH(100) 端为 +1，进攻 LOW(0) 端为 -1。

    客队符号恒为其相反数。换边半场传入另一 AttackEnd 即整段反向。
    """

    return +1 if home_attack_end == AttackEnd.HIGH else -1


def team_attack_sign(home_attack_end: AttackEnd, side: Side) -> int:
    sign = attack_sign(home_attack_end)
    return sign if side == Side.HOME else -sign


def ahead_value(true_position: int, sign: int) -> int:
    """把真位置投影为“靠前值”：朝攻门方向越大越靠前。"""

    return sign * true_position
