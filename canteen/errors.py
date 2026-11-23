"""餐签（展会食堂）领域的异常。

分层刻意清楚：

* 与钱有关的失败一律抛 :class:`~orgbudget.errors.BudgetError` 的子类，
  因为真实付款、剩余额度、承诺都落在 :mod:`orgbudget` 里；
* 本模块只补“签 / 场 / 工单 / 托管柜”这一层独有的错误，签核失败另立
  :class:`VoucherError` 体系，不与预算异常混为一谈。
"""

from __future__ import annotations


class VoucherError(Exception):
    """餐签与菜单层面的错误基类。"""


class VoucherConflict(VoucherError):
    """一组签无法同时交付（余额不够，或同一点被两桌重复支取）。"""


class VoucherExhausted(VoucherError):
    """签上可扣的点已被扣满。"""


class ReservationOversubscribed(VoucherError):
    """某桌的保留额不足以覆盖其同时有效的菜单联。"""


class ServiceError(Exception):
    """场次 / 桌 / 锅 / 时隙的服务错误。"""


class SlotConflict(ServiceError):
    """同一桌/锅/时隙被重复安排，或菜品工艺不允许。"""


class DishForbidden(ServiceError):
    """某锅的“允许前味”不允许在其当前余味上做该菜。"""


class OrderNotFulfillable(ServiceError):
    """在给定锅、余味、备料与授权下无法排出整场工单。"""


class StockShortfall(ServiceError):
    """实际备料不足以支撑所需菜品。"""


class TicketError(Exception):
    """托管柜与菜单联的审批错误。"""


class TicketExceedsReservation(TicketError):
    """封出的联最高可扣额之和超过本桌保留额。"""


class TicketNotActive(TicketError):
    """对已使用 / 已退回的联继续扣点或退回。"""


class CabinetExhausted(TicketError):
    """本拍（tick）的具名扣点号已经发过，不能再发。"""
