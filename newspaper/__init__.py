"""newspaper：本地纪年报纸的编年校勘与缩微订补。

与 :mod:`archive_handoff` 同构的两个独立服务：

* :mod:`newspaper.collation` 具名历书原档 + 馆藏原件 → 合法排年（穷举勘误）。
* :mod:`newspaper.microfilm` 冻结排年产品 + 订补单 → 一种实际可行的补拍工单。
"""

from .collation import CollationEngine
from .microfilm import MicrofilmOrderService, PreservationHold, SurrogateStock
from .models import (
    Almanac,
    ChronologyProduct,
    CollationError,
    ConflictError,
    Issue,
    Masthead,
    Month,
    Placement,
    ServiceWindow,
    Slot,
    UnfillableOrder,
    WorkOrder,
)

__all__ = [
    "Almanac",
    "ChronologyProduct",
    "CollationEngine",
    "CollationError",
    "ConflictError",
    "Issue",
    "Masthead",
    "MicrofilmOrderService",
    "Month",
    "Placement",
    "PreservationHold",
    "ServiceWindow",
    "Slot",
    "SurrogateStock",
    "UnfillableOrder",
    "WorkOrder",
]
