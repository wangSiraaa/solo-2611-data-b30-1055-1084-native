"""题面情形的统一构造：固定证据、版次与真实赛果，供演示与测试复用。

情形清单（每鼓一位、六拍遮幕）：
- A 交班认定 (1,1)：两拍可发到 (2,2)
- B 客鼓最后一下失回执：可能 (1,1) 或 (1,2)，两拍不能保证、三拍先认客鼓
- C 同 A 的交班，遮幕只给一拍：发不了
- D 9 转 0：只向前转的回绕，止位 (9,9) 到 (0,0)
- E 总闸失电脉冲组：同组要么全走要么全丢，产生成对止位
- F 主客换版与待赛拒绝播报
"""

from __future__ import annotations

from dataclasses import dataclass

from .broadcast import BroadcastOffice, Edition, MatchResult
from .core import DRUM_A, DRUM_B, Contact, Pulse, PulseStatus, Record, Seen, Stop

MATCH_ID = "m-22"
EDITION_V1 = "v1"
EDITION_V2 = "v2"


@dataclass(frozen=True)
class Scenario:
    name: str
    records: tuple[Record, ...]
    target: Stop
    curtain_beats: int


def scenario_a_certain_11() -> Scenario:
    """认定 (1,1)：全部脉冲有回执，止位唯一。"""
    records = (
        Seen(seq=0, drum=DRUM_A, digit=0),
        Seen(seq=1, drum=DRUM_B, digit=0),
        Pulse(seq=2, drum=DRUM_A, ack=PulseStatus.ACK),
        Pulse(seq=3, drum=DRUM_B, ack=PulseStatus.ACK),
    )
    return Scenario("A 认定(1,1) 遮两拍", records, Stop.of(2, 2), 2)


def scenario_b_uncertain_away() -> Scenario:
    """客鼓最后一下失回执：真相 (1,1) 或 (1,2)。"""
    records = (
        Seen(seq=0, drum=DRUM_A, digit=0),
        Seen(seq=1, drum=DRUM_B, digit=0),
        Pulse(seq=2, drum=DRUM_A, ack=PulseStatus.ACK),
        Pulse(seq=3, drum=DRUM_B, ack=PulseStatus.ACK),
        Pulse(seq=4, drum=DRUM_B, ack=PulseStatus.NAK),
    )
    return Scenario("B 客鼓失回执 两拍", records, Stop.of(2, 2), 2)


def scenario_c_one_beat_curtain() -> Scenario:
    """沿用 A 的认定 (1,1)，只把遮幕时限改成一拍。"""
    base = scenario_a_certain_11()
    return Scenario("C 认定(1,1) 遮一拍", base.records, Stop.of(2, 2), 1)


def scenario_d_wraparound() -> Scenario:
    """9 再走一格到 0：两只鼓都验证回绕。"""
    records = (
        Seen(seq=0, drum=DRUM_A, digit=9),
        Seen(seq=1, drum=DRUM_B, digit=9),
    )
    return Scenario("D 9转0 遮两拍", records, Stop.of(0, 0), 2)


def scenario_e_breaker_group() -> Scenario:
    """总闸失电组 g1 同时打向两只鼓：全丢 ->(1,1)，全走 ->(2,2)。

    组内一条发给 A、一条发给 B，禁止出现 (1,2)/(2,1) 这类交叉止位。
    """
    records = (
        Seen(seq=0, drum=DRUM_A, digit=0),
        Seen(seq=1, drum=DRUM_B, digit=0),
        Pulse(seq=2, drum=DRUM_A, ack=PulseStatus.ACK),
        Pulse(seq=3, drum=DRUM_B, ack=PulseStatus.ACK),
        Pulse(seq=4, drum=DRUM_A, ack=PulseStatus.NAK, group="g1"),
        Pulse(seq=5, drum=DRUM_B, ack=PulseStatus.NAK, group="g1"),
    )
    return Scenario("E 总闸失电组 遮六拍", records, Stop.of(3, 3), 6)


def conflicting_records() -> list[Record]:
    """自相矛盾交班：触片只登记 {0} 却要求在档，而脉冲已把鼓顶到 1。"""
    return [
        Seen(seq=0, drum=DRUM_A, digit=0),
        Seen(seq=1, drum=DRUM_B, digit=0),
        Pulse(seq=2, drum=DRUM_A, ack=PulseStatus.ACK),
        Contact(seq=3, drum=DRUM_A, registered=(0,), reading=True),
    ]


def build_office() -> BroadcastOffice:
    """播报员席：录入一场真实 2:2，登记两版主客映射，另挂一场待赛。"""
    office = BroadcastOffice()
    office.add_edition(Edition(EDITION_V1, home_drum=DRUM_A, away_drum=DRUM_B))
    office.add_edition(Edition(EDITION_V2, home_drum=DRUM_B, away_drum=DRUM_A))
    office.record_result(MatchResult(MATCH_ID, "played",
                                     home_goals=2, away_goals=2))
    office.record_result(MatchResult("m-pending", "pending"))
    return office


ALL_SCENARIOS = (
    scenario_a_certain_11,
    scenario_b_uncertain_away,
    scenario_c_one_beat_curtain,
    scenario_d_wraparound,
    scenario_e_breaker_group,
)
