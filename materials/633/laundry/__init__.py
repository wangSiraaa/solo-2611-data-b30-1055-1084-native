"""联赛后勤洗衣规划子系统。

从旧仓（赛程 edition + 比分事实）派生待洗布件，判定每件/整袋可共容的洗涤程序，
并在实筒、容量、用时与交还截止约束下给出可执行的拆袋、整筒、交回方案——
或给出缺件、时窗不足、错误送洗的可定位理由。

公开入口
--------
* :class:`~laundry.model.EquipmentLedger` / :class:`~laundry.model.Constraint`：
  具名布件与约束→允许程序集合。
* :class:`~laundry.towash.IssuanceBook`：按 (match, side) 登记领用。
* :func:`~laundry.towash.derive_to_wash`：由 played/cancelled 事实派生待洗。
* :func:`~laundry.solver.plan` / :func:`~laundry.solver.replan`：
  求解与“已开机后重开”。
* :func:`~laundry.solver.scheme_first` / :func:`~laundry.solver.drum_first`：
  两法（方案先行 / 实筒先行）。
* :class:`~laundry.service.WashRequest` 与 :func:`~laundry.service.plan_request`：
  直接读取旧仓的端到端入口。
"""

from .model import (PROGRAMS, BAG_DONE, BAG_OPEN, BAG_SEALED, BAG_WASHING,
                    Bag, Constraint, Drum, EquipmentLedger, Item, ItemInfo,
                    machine_conflicts)
from .towash import Issuance, IssuanceBook, ToWashResult, derive_to_wash
from .solver import (Assignment, BagOp, Scheme, Solution, WashProblem,
                     drum_first, invalidated_schemes, item_program_report,
                     plan, replan, scheme_first)
from .service import (WashRequest, build_problem, derive_for_request,
                      plan_request, replan_request)

__all__ = [
    "PROGRAMS", "BAG_OPEN", "BAG_SEALED", "BAG_WASHING", "BAG_DONE",
    "Bag", "Constraint", "Drum", "EquipmentLedger", "Item", "ItemInfo",
    "machine_conflicts",
    "Issuance", "IssuanceBook", "ToWashResult", "derive_to_wash",
    "Assignment", "BagOp", "Scheme", "Solution", "WashProblem",
    "plan", "replan", "scheme_first", "drum_first",
    "item_program_report", "invalidated_schemes",
    "WashRequest", "build_problem", "derive_for_request",
    "plan_request", "replan_request",
]
