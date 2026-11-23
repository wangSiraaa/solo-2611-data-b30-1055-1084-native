"""层级组织编制服务、预算执行仓与设备会计。

相互独立、仅通过公开查询衔接的三类服务：

* :mod:`orgbudget.org_service` —— 组织编制服务，维护唯一根的父子树；
* :mod:`orgbudget.budget_service` —— 预算执行仓，绑定某个组织产品版本做
  权重分解、承诺与实际支出；
* :mod:`orgbudget.equipment_service` —— 设备会计中心：购建底单认领真实
  实付事件、封存原值、登记机身、按期限额独占拨付；
* :mod:`orgbudget.local_cabinet` —— 离线柜：凭拨付单在本地核算拆装并
  交还余款回执。

所有金额均为整数分。
"""

from .budget import allocate_children
from .budget_service import (
    BudgetService,
    Commitment,
    NodeSummary,
    SchemeInfo,
    SpendEvent,
)
from .equipment import (
    HourlyPart,
    accumulated_depreciation,
    book_value,
    disposal_loss,
    split_installation_fee,
)
from .equipment_service import (
    DisbursementInfo,
    EquipmentService,
    LossAllowance,
    MoneyTrailEntry,
    PartStatus,
    ReplacementInfo,
    VoucherInfo,
    VoucherLine,
)
from .errors import (
    AllocationMismatch,
    BudgetError,
    CabinetBusy,
    CommitmentClosedError,
    CommitmentNotFound,
    CrossDivisionClaim,
    CycleError,
    DisbursementClosed,
    DisbursementNotFound,
    DuplicateClaim,
    DuplicateDepartment,
    DuplicateEquipment,
    EquipmentError,
    EquipmentIdempotencyConflict,
    EquipmentLocked,
    EquipmentNotFound,
    IdempotencyConflict,
    InactiveDepartment,
    InsufficientFunds,
    InsufficientLossAllowance,
    InvalidTree,
    LimitBelowObligations,
    ManifestInvalid,
    MissingCommitment,
    NoActiveSlip,
    NoLossLimit,
    NotALeaf,
    NotSealed,
    OrderBlocked,
    OrderNotFound,
    OrderStateError,
    OrganizationVersionNotFound,
    PartAlreadyRetired,
    PartNotFound,
    PartNotOnMachine,
    PartNotStocked,
    RootError,
    SealStale,
    SpendEventNotFound,
    WeightError,
)
from .local_cabinet import LocalCabinet
from .org_service import OrgProduct, OrgService

__all__ = [
    "OrgService",
    "OrgProduct",
    "BudgetService",
    "Commitment",
    "NodeSummary",
    "SchemeInfo",
    "SpendEvent",
    "EquipmentService",
    "LocalCabinet",
    "VoucherLine",
    "VoucherInfo",
    "MoneyTrailEntry",
    "PartStatus",
    "LossAllowance",
    "DisbursementInfo",
    "ReplacementInfo",
    "HourlyPart",
    "allocate_children",
    "split_installation_fee",
    "accumulated_depreciation",
    "book_value",
    "disposal_loss",
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
    "EquipmentError",
    "EquipmentNotFound",
    "DuplicateEquipment",
    "PartNotFound",
    "PartNotOnMachine",
    "PartNotStocked",
    "PartAlreadyRetired",
    "SpendEventNotFound",
    "MissingCommitment",
    "DuplicateClaim",
    "AllocationMismatch",
    "CrossDivisionClaim",
    "NotSealed",
    "SealStale",
    "EquipmentLocked",
    "NoLossLimit",
    "InsufficientLossAllowance",
    "LimitBelowObligations",
    "DisbursementNotFound",
    "DisbursementClosed",
    "CabinetBusy",
    "NoActiveSlip",
    "OrderNotFound",
    "OrderBlocked",
    "OrderStateError",
    "ManifestInvalid",
    "EquipmentIdempotencyConflict",
]
