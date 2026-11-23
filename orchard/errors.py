"""果园采果与剪枝领域的异常体系。

与 :mod:`orgbudget.errors` 一样，本模块的异常自成一体：份额错误、剪枝
错误、预订（挂签）错误互不混淆，便于调用方精确区分。
"""

from __future__ import annotations


class OrchardError(Exception):
    """果园领域所有错误的基类。"""


# ---------------------------------------------------------------- 树与剪枝


class InvalidOrchard(OrchardError):
    """树结构不合法（多根、成环、孤儿枝、芽数为负等）。"""


class UnknownBranch(OrchardError):
    """引用了不存在的枝节。"""


class UnknownTree(OrchardError):
    """引用了不存在的树。"""


class BudLimitError(OrchardError):
    """剪枝后某主枝留芽低于下限。"""


class Undeliverable(OrchardError):
    """在已核准的份额与留芽约束下，无法交出全部要求的整串。"""


class SolveTooLarge(OrchardError):
    """剪枝求解规模超出小园上限（枝节太多，无法穷举）。"""


# ---------------------------------------------------------------- 份额


class ShareError(OrchardError):
    """采果份额相关错误的基类。"""


class UnknownShare(ShareError):
    """引用了不存在的采果份额。"""


class UnknownCommitment(ShareError):
    """引用的承诺号没有对应的预算事实。"""


class IneligibleTransfer(ShareError):
    """份额转让不合法（跨分部、同一叶、已剪走、已失效等）。"""


class ShareAlreadyConsumed(ShareError):
    """份额已被剪走或已作废，不能再用。"""


class VerificationError(ShareError):
    """逐户领果要求核对失败（来源不明、重复占用、承诺间互相填零等）。"""


# ---------------------------------------------------------------- 预订 / 挂签


class ReservationError(OrchardError):
    """预订与挂签相关错误的基类。"""


class ReservationConflict(ReservationError):
    """果串或替枝已被另一户挂签占用，真来单不得使用。"""


class ReservationNotFound(ReservationError):
    """请求的挂签不存在。"""


class AmendmentNotHonorable(ReservationError):
    """改约无法从尚实存且满足留芽线的枝上兑现。"""
