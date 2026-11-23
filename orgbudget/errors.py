"""领域异常。组织错误与预算错误各自成体系，二者不互相抛出。"""

from __future__ import annotations


class OrgError(Exception):
    """组织编制服务的所有错误基类。"""


class DuplicateDepartment(OrgError):
    """department_id 已存在。"""


class DepartmentNotFound(OrgError):
    """部门不存在。"""


class RootError(OrgError):
    """违反单根约束（如停用根、把根当作普通节点移动）。"""


class CycleError(OrgError):
    """移动会产生环（把节点移入自身子孙）。"""


class NotALeaf(OrgError):
    """操作只允许叶部门（如停用），但该部门仍有孩子。"""


class InactiveDepartment(OrgError):
    """引用了已停用部门，或在已停用节点上继续建孩子。"""


class InvalidTree(OrgError):
    """发布前校验发现树不变量被破坏。"""


class OrganizationVersionNotFound(OrgError):
    """请求的组织产品版本不存在。"""


class BudgetError(Exception):
    """预算执行仓的所有错误基类。"""


class WeightError(BudgetError):
    """权重不是正整数，或直接孩子权重没有补齐/有多余项。"""


class SchemeError(BudgetError):
    """预算方案状态非法（如未绑定组织版本、重复换版到同一版本）。"""


class InactiveLeaf(BudgetError):
    """在已停用叶部门上开立承诺。"""


class InsufficientFunds(BudgetError):
    """可用额不足以开立承诺，或承诺剩余额不足以登记实际支出。"""


class CommitmentNotFound(BudgetError):
    """请求号对应的承诺不存在。"""


class CommitmentClosedError(BudgetError):
    """承诺已取消，不能再登记支出。"""


class IdempotencyConflict(BudgetError):
    """同一幂等键被用不同的请求内容重试。"""


# ---------------------------------------------------------------- 成本联算

class CostflowError(Exception):
    """成本联算（分送网络、互供环、归线）的所有错误基类。"""


class DistributionError(CostflowError):
    """分送表不合法：目标缺失、比例非正或合计不为 1。"""


class NetworkError(CostflowError):
    """分送网络不合法（如孤立节点、目标既非中心也非生产线）。"""


class SingularNetwork(CostflowError):
    """开网络内出现无法唯一求解的方程（防御性，正常结构不应触发）。"""


# ---------------------------------------------------------------- 实付账与工坊

class LedgerError(Exception):
    """实付账（虚构工坊成本联算账）的所有错误基类。"""


class CenterNotFound(LedgerError):
    """登记中心不存在。"""


class UnpostablePayment(LedgerError):
    """入账部门在预算树上找不到任何登记中心祖先，无法归集。"""


class PaymentConflict(LedgerError):
    """同一支付号重试但金额或部门内容不一致。"""


# ---------------------------------------------------------------- 联营会

class AssociationError(Exception):
    """联营会（集中流向、归线报价、撤回失效）的所有错误基类。"""


class QuoteNotFound(AssociationError):
    """报价号不存在。"""


class QuoteInvalid(AssociationError):
    """报价已因服务量撤回而失效，不能再据以签单。"""


class TableVersionConflict(AssociationError):
    """重报的分送表版本不是当前版本的直接后继。"""


# ---------------------------------------------------------------- 订单与外协

class OrderError(Exception):
    """订单员（最低成本分工、封价、外协名额）的所有错误基类。"""


class InfeasibleOrder(OrderError):
    """需求无法交足（自制与外协产能之和不够，或某品种无人可做）。"""


class SealPriceRejected(OrderError):
    """最低成本仍超过买方封价，必须拒签。"""


class SupplierCapacityTaken(OrderError):
    """外协名额已被先签出的订单占用，不能重复许诺给同一班。"""
