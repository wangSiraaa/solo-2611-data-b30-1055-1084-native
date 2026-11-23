"""层级组织编制服务、预算执行仓与虚构工坊成本联算。

四个相互独立、仅通过已发布的不可变组织产品版本或冻结报价快照衔接的服务：

* :mod:`orgbudget.org_service` —— 组织编制服务，维护唯一根的父子树；
* :mod:`orgbudget.budget_service` —— 预算执行仓，绑定组织产品版本做
  权重分解、承诺与实际支出；
* :mod:`orgbudget.ledger` —— 实付账，按预算树子树就近归集实付，跑
  互供网络上的精确分数成本联算（:mod:`orgbudget.costflow`）；
* :mod:`orgbudget.association` / :mod:`orgbudget.order_desk` —— 联营会
  集中流向、签逐来源归线报价，订单员按封价与外协名额签最低成本分工。

所有内部金额均为整数分；成本联算与单件负担全程使用
:class:`fractions.Fraction` 精确分数。
"""

from .association import AssociationService, Quote
from .boundary import UnitRoute, WorkshopBoundary, build_boundary, combine_and_find_closed_loops
from .budget import allocate_children
from .budget_service import BudgetService, Commitment, NodeSummary, SchemeInfo
from .costflow import (
    AllocationResult,
    Distribution,
    SourceTrace,
    TrapReport,
    solve_allocation,
    unit_burden,
)
from .errors import (
    AssociationError,
    BudgetError,
    CenterNotFound,
    CommitmentClosedError,
    CommitmentNotFound,
    CostflowError,
    CycleError,
    DistributionError,
    DuplicateDepartment,
    IdempotencyConflict,
    InactiveDepartment,
    InfeasibleOrder,
    InsufficientFunds,
    InvalidTree,
    LedgerError,
    NetworkError,
    NotALeaf,
    OrderError,
    OrganizationVersionNotFound,
    PaymentConflict,
    QuoteInvalid,
    QuoteNotFound,
    RootError,
    SealPriceRejected,
    SingularNetwork,
    SupplierCapacityTaken,
    TableVersionConflict,
    UnpostablePayment,
    WeightError,
)
from .ledger import DirectCost, LedgerService, Payment
from .order import (
    BlockingConstraint,
    LineCost,
    OptimizationResult,
    OrderSpec,
    Plan,
    Supplier,
    optimize_order,
    optimize_without_qualification,
)
from .order_desk import OrderDesk, SignedOrder
from .org_service import OrgProduct, OrgService

__all__ = [
    # 组织与预算
    "OrgService",
    "OrgProduct",
    "BudgetService",
    "Commitment",
    "NodeSummary",
    "SchemeInfo",
    "allocate_children",
    # 成本联算
    "Distribution",
    "AllocationResult",
    "SourceTrace",
    "TrapReport",
    "solve_allocation",
    "unit_burden",
    "LedgerService",
    "Payment",
    "DirectCost",
    # 联营会与工坊边界
    "AssociationService",
    "Quote",
    "WorkshopBoundary",
    "UnitRoute",
    "build_boundary",
    "combine_and_find_closed_loops",
    # 订单与外协
    "LineCost",
    "Supplier",
    "OrderSpec",
    "Plan",
    "OptimizationResult",
    "BlockingConstraint",
    "optimize_order",
    "optimize_without_qualification",
    "OrderDesk",
    "SignedOrder",
    # 异常
    "BudgetError",
    "CommitmentClosedError",
    "CommitmentNotFound",
    "CycleError",
    "DuplicateDepartment",
    "IdempotencyConflict",
    "InactiveDepartment",
    "InsufficientFunds",
    "InvalidTree",
    "NotALeaf",
    "OrganizationVersionNotFound",
    "RootError",
    "WeightError",
    "CostflowError",
    "DistributionError",
    "NetworkError",
    "SingularNetwork",
    "LedgerError",
    "CenterNotFound",
    "UnpostablePayment",
    "PaymentConflict",
    "AssociationError",
    "QuoteNotFound",
    "QuoteInvalid",
    "TableVersionConflict",
    "OrderError",
    "InfeasibleOrder",
    "SealPriceRejected",
    "SupplierCapacityTaken",
]
