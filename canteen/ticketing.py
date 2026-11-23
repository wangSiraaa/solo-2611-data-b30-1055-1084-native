"""高峰时的两种经营方式：具名菜单联（封联）与共享托管柜。

题面要求二选一，本模块把两种都实现成可对照的对象：

**具名菜单联（:class:`MenuTicket` / :class:`TicketBook`）**
  开场时给每桌封一张具名联。联上可列有限个互斥选项（同一份餐只能取其中
  一个），**真点数按该联最高可扣额保留**（``reserved = max(option.points)``）。
  未下锅的联可退回，退回后保留额才释放、才能改给另一桌。好处是各桌可照
  联分锅办。限制是每桌保留额有限：若给每桌都封“能提满额”的联，保留额
  之和会超过全场实付点数。

**共享托管柜（:class:`Cabinet`）**
  把共享点数托管到后厨同一个柜。多个掌厨在下锅时拿到本班虚构原料到货
  消息后，请求一个**具名扣点号**；同一台柜**每拍只能发一个**加价号。

两者都只决定“点如何落到具体一桌上”，最终仍要回到
:mod:`canteen.vouchers` 的联合审批与 :mod:`canteen.kitchen` 的排程。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from .errors import (
    CabinetExhausted,
    TicketExceedsReservation,
    TicketNotActive,
    TicketError,
)

ACTIVE = "ACTIVE"
USED = "USED"
RETURNED = "RETURNED"


@dataclass
class MenuTicket:
    """一张具名菜单联：限定某桌、列互斥选项、按最高可扣额保留。"""

    ticket_id: str
    table_id: str
    options: tuple[tuple[str, int], ...]
    status: str = ACTIVE
    chosen_dish: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.options:
            raise TicketError("菜单联至少要列一个选项")
        if len({d for d, _ in self.options}) != len(self.options):
            raise TicketError("菜单联的选项菜品重复")
        if any(pts < 0 for _, pts in self.options):
            raise TicketError("选项点数不能为负")

    @property
    def reserved(self) -> int:
        return max(pts for _, pts in self.options)

    def option(self, dish: str) -> int:
        for d, pts in self.options:
            if d == dish:
                return pts
        raise TicketError(f"联 {self.ticket_id} 未列菜品 {dish}")


class ReservationLedger:
    """保留额账：每桌有上限，全场还另有一个总上限（实付加价点数）。

    联的保留额之和既不能超过该桌上限，也不能超过全场总上限。题目里的
    “两张各可提 1 点的联会超保留额”即指：全场只有 1 点可保留，两张联
    却要保留 2 点。
    """

    def __init__(
        self, limits: dict[str, int], total_limit: Optional[int] = None
    ) -> None:
        self._limits = dict(limits)
        self._reserved = {t: 0 for t in limits}
        self._total_limit = (
            sum(self._limits.values()) if total_limit is None else total_limit
        )

    def limit(self, table_id: str) -> int:
        return self._limits.get(table_id, 0)

    def reserved(self, table_id: str) -> int:
        return self._reserved.get(table_id, 0)

    def free(self, table_id: str) -> int:
        return self.limit(table_id) - self.reserved(table_id)

    def total_reserved(self) -> int:
        return sum(self._reserved.values())

    def total_limit(self) -> int:
        return self._total_limit

    def total_free(self) -> int:
        return self._total_limit - self.total_reserved()

    def reserve(self, table_id: str, amount: int) -> None:
        if amount > self.total_free():
            raise TicketExceedsReservation(
                f"全场剩余可保留 {self.total_free()} 点，无法再保留 {amount} 点"
            )
        if self.free(table_id) < amount:
            raise TicketExceedsReservation(
                f"桌 {table_id} 剩余保留额 {self.free(table_id)}，无法再保留 {amount}"
            )
        self._reserved[table_id] += amount

    def release(self, table_id: str, amount: int) -> None:
        self._reserved[table_id] = self.reserved(table_id) - amount


class TicketBook:
    """封联 / 退回 / 使用菜单联，并维护每桌保留额。"""

    def __init__(self, ledger: ReservationLedger) -> None:
        self.ledger = ledger
        self._tickets: dict[str, MenuTicket] = {}

    def open_ticket(
        self,
        ticket_id: str,
        table_id: str,
        options: Iterable[tuple[str, int]],
    ) -> MenuTicket:
        if ticket_id in self._tickets:
            raise TicketError(f"菜单联 {ticket_id} 已存在")
        ticket = MenuTicket(ticket_id, table_id, tuple(options))
        self.ledger.reserve(table_id, ticket.reserved)
        self._tickets[ticket_id] = ticket
        return ticket

    def return_ticket(self, ticket_id: str) -> MenuTicket:
        ticket = self._get(ticket_id)
        if ticket.status != ACTIVE:
            raise TicketNotActive(f"联 {ticket_id} 状态为 {ticket.status}，不能退回")
        ticket.status = RETURNED
        self.ledger.release(ticket.table_id, ticket.reserved)
        return ticket

    def use_ticket(
        self, ticket_id: str, dish: str, table_id: str
    ) -> tuple[int, str]:
        """按联下锅：返回 (扣点额, 菜名)，并把保留额结算为实际扣点。"""
        ticket = self._get(ticket_id)
        if ticket.status != ACTIVE:
            raise TicketNotActive(f"联 {ticket_id} 状态为 {ticket.status}，不能使用")
        if ticket.table_id != table_id:
            raise TicketError(f"联 {ticket_id} 属桌 {ticket.table_id}，不是 {table_id}")
        points = ticket.option(dish)
        ticket.status = USED
        ticket.chosen_dish = dish
        # 保留额结算：释放最高额，记账为实际点数。
        self.ledger.release(ticket.table_id, ticket.reserved)
        return points, dish

    def _get(self, ticket_id: str) -> MenuTicket:
        try:
            return self._tickets[ticket_id]
        except KeyError:
            raise TicketError(f"菜单联 {ticket_id} 不存在") from None

    def ticket(self, ticket_id: str) -> MenuTicket:
        return self._get(ticket_id)

    def tickets(self) -> tuple[MenuTicket, ...]:
        return tuple(self._tickets[k] for k in sorted(self._tickets))

    def active_reserved(self) -> int:
        return sum(t.reserved for t in self._tickets.values() if t.status == ACTIVE)


@dataclass(frozen=True)
class DeductionToken:
    """托管柜发出的一个具名扣点号。"""

    token_id: str
    tick: int
    table_id: str
    dish: str
    points: int


class Cabinet:
    """共享托管柜：每拍最多发一个具名加价号。"""

    def __init__(self, total_points: int) -> None:
        if total_points < 0:
            raise ValueError("托管点数不能为负")
        self._remaining = total_points
        self._total = total_points
        self._issued_by_tick: dict[int, DeductionToken] = {}
        self._tokens: list[DeductionToken] = []
        self._seq = 0

    @property
    def remaining(self) -> int:
        return self._remaining

    @property
    def total(self) -> int:
        return self._total

    def tick_has_token(self, tick: int) -> bool:
        return tick in self._issued_by_tick

    def issue(
        self, tick: int, table_id: str, dish: str, points: int
    ) -> DeductionToken:
        """请求一个具名扣点号；同一拍只能成功一次。"""
        if points <= 0:
            raise TicketError("扣点号点数必须为正")
        if tick in self._issued_by_tick:
            raise CabinetExhausted(
                f"第 {tick} 拍已经发过号（{self._issued_by_tick[tick].token_id}），每拍只能发一个"
            )
        if points > self._remaining:
            raise CabinetExhausted(
                f"托管柜剩余 {self._remaining} 点，无法发出 {points} 点的号"
            )
        self._seq += 1
        token = DeductionToken(f"CAB-{self._seq:03d}", tick, table_id, dish, points)
        self._remaining -= points
        self._issued_by_tick[tick] = token
        self._tokens.append(token)
        return token

    def tokens(self) -> tuple[DeductionToken, ...]:
        return tuple(self._tokens)

    def consumed(self) -> int:
        return self._total - self._remaining


def tickets_reservation_feasible(ledger: ReservationLedger, cabinet: Cabinet) -> bool:
    """两张“各能提满额”的联需要保留的总数是否超过实付点数。

    用于题面反例：两桌各封一张“可提 1 点”的联，保留额 2 点 > 全场 1 点。
    """
    return ledger.total_reserved() <= cabinet.total
