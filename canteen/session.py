"""场次编排：把餐签审批、后厨排程与工单执行串成一个可批准的整体。

对外主入口：

* :func:`plan_and_serve` —— 给定一桌桌菜单，先做资助席联合审批（给点数
  来路），再排整场工单，最后依次执行并交班；任何一步不成立都不会"半做"。
* :class:`OperatingModel` —— 高峰时两种经营方式的统一入口，便于题面里
  的“二选一”对照。

不收费的普通对照试做也能排程执行（``free_trial=True``），只是不落扣点。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional

from .errors import OrderNotFulfillable, VoucherConflict
from .kitchen import schedule
from .model import SessionSpec, TablePlan, WorkOrder
from .runtime import Charge, SessionRuntime, charges_from_approval
from .ticketing import Cabinet, TicketBook, TicketExceedsReservation
from .vouchers import Approval, VoucherBook


@dataclass(frozen=True)
class ServicePlan:
    """一份可批准、可执行的整体方案。"""

    approval: Approval
    order: WorkOrder
    charges: tuple[Charge, ...]

    def describe(self) -> str:
        lines = ["点数来路（联合批准）："]
        lines.append(self.approval.describe())
        lines.append("整场工单：")
        for a in self.order.actions:
            if a.kind == "cook":
                lines.append(f"  槽{a.slot} 锅{a.pot_id} 做 {a.dish} -> 桌{a.table_id}")
            else:
                lines.append(f"  槽{a.slot} 锅{a.pot_id} 洗锅")
        return "\n".join(lines)


def plan_and_serve(
    spec: SessionSpec,
    plans: Iterable[TablePlan],
    book: VoucherBook,
) -> tuple[ServicePlan, SessionRuntime]:
    """联合审批 + 排程；成功才返回可执行的方案。

    先审批（点数来路），再排程；两者都过不了就不会进入执行，避免"先做
    了菜才发现钱不够"或"钱批了却做不出来"。
    """
    plans_t = tuple(plans)
    approval = book.approve_together(spec, plans_t)
    order = schedule(spec, plans_t)
    charges = charges_from_approval(approval)
    plan = ServicePlan(approval, order, charges)
    runtime = SessionRuntime(spec, plans_t, order, charges).execute()
    return plan, runtime


def trial_serve(
    spec: SessionSpec, plans: Iterable[TablePlan]
) -> tuple[ServicePlan, SessionRuntime]:
    """不收费的普通对照试做：只排程、执行，不审批也不扣点。"""
    plans_t = tuple(plans)
    order = schedule(spec, plans_t)
    approval = Approval(())
    runtime = SessionRuntime(spec, plans_t, order, free_trial=True).execute()
    return ServicePlan(approval, order, ()), runtime


class OperatingModel(Enum):
    """高峰时的经营方式。"""

    NAMED_TICKETS = "named_tickets"  # 具名菜单联
    SHARED_CABINET = "shared_cabinet"  # 共享托管柜


@dataclass
class PeakPlan:
    """高峰安排的统一结果，附带解释字段。"""

    model: OperatingModel
    feasible: bool
    reason: str = ""
    plan: Optional[ServicePlan] = None
    runtime: Optional[SessionRuntime] = None


def plan_peak_named_tickets(
    spec: SessionSpec,
    plans: tuple[TablePlan, ...],
    book: VoucherBook,
    ticket_book: TicketBook,
    tickets: dict[str, str],
) -> PeakPlan:
    """按具名菜单联安排：每桌照已封的联办，联的保留额必须守得住。

    ``tickets`` 给出 ``table_id -> ticket_id``。只有真实点数能覆盖全部保留额
    时才算守得住来源；否则说明超保留额。扣点来源落成**联号**本身。
    """
    try:
        approval = book.approve_together(spec, plans)
    except VoucherConflict as exc:
        return PeakPlan(OperatingModel.NAMED_TICKETS, False, f"联合审批不足：{exc}")

    # 真实可付点数（该场次可用的签面额之和），用来核“保留额守得住”。
    usable_points = sum(v.amount for v in book.vouchers() if v.usable_in(spec))

    charges: list[Charge] = []
    try:
        for p in plans:
            tid = tickets.get(p.table_id)
            if tid is None:
                return PeakPlan(OperatingModel.NAMED_TICKETS, False, f"桌 {p.table_id} 未封联")
            tk = ticket_book.ticket(tid)
            if tk.table_id != p.table_id:
                return PeakPlan(
                    OperatingModel.NAMED_TICKETS, False, f"联 {tid} 不属桌 {p.table_id}"
                )
            if tk.status != "ACTIVE":
                return PeakPlan(
                    OperatingModel.NAMED_TICKETS, False, f"联 {tid} 状态 {tk.status}，不能照办"
                )
            points = tk.option(p.dish)  # 菜品不在联上会抛
            charges.append(Charge(p.table_id, tid, points))
    except TicketExceedsReservation as exc:  # pragma: no cover - 封联阶段更早拦截
        return PeakPlan(OperatingModel.NAMED_TICKETS, False, f"超保留额：{exc}")
    except Exception as exc:  # noqa: BLE001 - 转成不可行结论
        return PeakPlan(OperatingModel.NAMED_TICKETS, False, f"联核验失败：{exc}")

    # 保留额守住实付来源：被引用的联保留额总和不得超过真实可付点数。
    referenced = {tickets[p.table_id] for p in plans}
    reserved = sum(ticket_book.ticket(tid).reserved for tid in referenced)
    if reserved > usable_points:
        return PeakPlan(
            OperatingModel.NAMED_TICKETS,
            False,
            f"联保留额 {reserved} 超过本场真实可付 {usable_points} 点",
        )

    try:
        order = schedule(spec, plans)
    except OrderNotFulfillable as exc:
        return PeakPlan(OperatingModel.NAMED_TICKETS, False, f"排程不可行：{exc}")
    plan = ServicePlan(approval, order, tuple(charges))
    runtime = SessionRuntime(spec, plans, order, tuple(charges)).execute()
    return PeakPlan(OperatingModel.NAMED_TICKETS, True, "照联分锅可办", plan, runtime)


def plan_peak_shared_cabinet(
    spec: SessionSpec,
    plans: tuple[TablePlan, ...],
    order: WorkOrder,
    cabinet: Cabinet,
    *,
    arrival_tick: dict[str, int],
    base_covered: Optional[dict[str, int]] = None,
) -> PeakPlan:
    """按共享托管柜安排：在下锅时凭“原料到货消息”请求具名扣点号。

    ``arrival_tick`` 给出每桌知道本班原料到货/缺货的那一拍；扣点号必须
    在“知道缺货”之后、“下锅”之前发出。每拍只能发一个号。

    ``base_covered`` 给出每桌已经被别处（基础餐赞助 / 签）覆盖的点数；
    托管柜只需补足差额。典型高峰反例：两桌基础各 2 点已覆盖，全班只有
    1 个加价点，恰好一桌临缺货要换白汤（+1），柜子把唯一的号发给它。
    """
    covered = base_covered or {}
    draws = {p.table_id: p.points - covered.get(p.table_id, 0) for p in plans}
    total_draw = sum(d for d in draws.values() if d > 0)
    if cabinet.total < total_draw:
        return PeakPlan(
            OperatingModel.SHARED_CABINET,
            False,
            f"托管点数 {cabinet.total} 不足以覆盖加价 {total_draw} 点",
        )
    charges: list[Charge] = []
    try:
        for p in plans:
            points = draws[p.table_id]
            if points <= 0:
                continue
            cook_slot = order.meals[p.table_id][0]
            know = arrival_tick.get(p.table_id, 0)
            # 知道到货/缺货后、下锅前必须有一拍能发号。
            issue_tick = know + 1
            if issue_tick >= cook_slot:
                return PeakPlan(
                    OperatingModel.SHARED_CABINET,
                    False,
                    f"桌 {p.table_id} 知道缺货(拍{know})与下锅(槽{cook_slot})之间没有可发号的拍",
                )
            token = cabinet.issue(issue_tick, p.table_id, p.dish, points)
            charges.append(Charge(p.table_id, token.token_id, token.points))
    except Exception as exc:  # noqa: BLE001
        return PeakPlan(OperatingModel.SHARED_CABINET, False, f"发号失败：{exc}")

    # 托管柜只结算加价部分，基础餐的扣点来路由调用方的批准负责；
    # 这里用一个“补差”计划整体执行（基础部分记为空来源）。
    approval = Approval(())
    plan = ServicePlan(approval, order, tuple(charges))
    try:
        runtime = SessionRuntime(
            spec, plans, order, tuple(charges), base_covered=covered
        ).execute()
    except Exception as exc:  # noqa: BLE001
        return PeakPlan(OperatingModel.SHARED_CABINET, False, f"执行失败：{exc}")
    return PeakPlan(OperatingModel.SHARED_CABINET, True, "托管柜按拍发号可办", plan, runtime)
