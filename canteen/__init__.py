"""展会食堂：餐签核验、后厨排程与高峰经营方式。

按题面拆成几块，互不越界：

* :mod:`canteen.model`    纯数据：菜、锅、桌、场次、工单；
* :mod:`canteen.vouchers` 建立在 ``orgbudget`` 实付事实上的餐签与联合审批
  （含两桌共抢一张限班点的最小见证）；
* :mod:`canteen.kitchen`  余味 / 时隙 / 备料约束下的整场排程；
* :mod:`canteen.runtime`  依次执行、实扣、交班保留（仅追加）；
* :mod:`canteen.ticketing` 高峰两种经营方式：具名菜单联 / 共享托管柜；
* :mod:`canteen.session`  把审批、排程、执行串成可批准的整体。
"""

from .errors import (
    CabinetExhausted,
    DishForbidden,
    OrderNotFulfillable,
    ReservationOversubscribed,
    ServiceError,
    SlotConflict,
    StockShortfall,
    TicketError,
    TicketExceedsReservation,
    TicketNotActive,
    VoucherConflict,
    VoucherError,
    VoucherExhausted,
)
from .kitchen import can_schedule, schedule
from .model import (
    CLEAN,
    Dish,
    DishUpgrade,
    Pot,
    SessionSpec,
    Table,
    TablePlan,
    WorkAction,
    WorkOrder,
)
from .runtime import (
    Charge,
    Handover,
    IngredientDraw,
    PointDraw,
    ServedMeal,
    SessionRuntime,
    ShiftBook,
    charges_from_approval,
)
from .session import (
    OperatingModel,
    PeakPlan,
    ServicePlan,
    plan_and_serve,
    plan_peak_named_tickets,
    plan_peak_shared_cabinet,
    trial_serve,
)
from .ticketing import (
    ACTIVE,
    RETURNED,
    USED,
    Cabinet,
    DeductionToken,
    MenuTicket,
    ReservationLedger,
    TicketBook,
    tickets_reservation_feasible,
)
from .vouchers import (
    GENERAL,
    SHIFT,
    TABLE,
    Allocation,
    Approval,
    Voucher,
    VoucherBook,
    issue_voucher,
)

__all__ = [
    # model
    "CLEAN",
    "Dish",
    "DishUpgrade",
    "Pot",
    "SessionSpec",
    "Table",
    "TablePlan",
    "WorkAction",
    "WorkOrder",
    # vouchers
    "GENERAL",
    "TABLE",
    "SHIFT",
    "Voucher",
    "VoucherBook",
    "Allocation",
    "Approval",
    "issue_voucher",
    # kitchen
    "schedule",
    "can_schedule",
    # runtime
    "Charge",
    "Handover",
    "IngredientDraw",
    "PointDraw",
    "ServedMeal",
    "SessionRuntime",
    "ShiftBook",
    "charges_from_approval",
    # ticketing
    "ACTIVE",
    "USED",
    "RETURNED",
    "MenuTicket",
    "ReservationLedger",
    "TicketBook",
    "Cabinet",
    "DeductionToken",
    "tickets_reservation_feasible",
    # session
    "OperatingModel",
    "PeakPlan",
    "ServicePlan",
    "plan_and_serve",
    "plan_peak_named_tickets",
    "plan_peak_shared_cabinet",
    "trial_serve",
    # errors
    "VoucherError",
    "VoucherConflict",
    "VoucherExhausted",
    "ReservationOversubscribed",
    "ServiceError",
    "SlotConflict",
    "DishForbidden",
    "OrderNotFulfillable",
    "StockShortfall",
    "TicketError",
    "TicketExceedsReservation",
    "TicketNotActive",
    "CabinetExhausted",
]
