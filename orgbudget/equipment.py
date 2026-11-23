"""设备会计的纯函数：共用安装费分摊、累计损耗、报废损失。

规则（题面）：

* 共用安装费按给定的受益部件工时比例分到整数分：先下取整，丢掉的
  分按小数余数从大到小补，同余数按部件号升序 —— 与预算分解同一套
  大数余数法，直接复用 :func:`orgbudget.budget.allocate_children`；
* 每期期末累计损耗 = 原值 × 已用期数 ÷ 总期数，向下取整，最多到原值；
* 报废损失 = 旧件账面值 − 验回残件价款，最低为零。

本模块不含任何 I/O，便于独立测试与重算。
"""

from __future__ import annotations

from dataclasses import dataclass

from .budget import WeightedChild, allocate_children


@dataclass(frozen=True)
class HourlyPart:
    """一名受益部件及其工时。"""

    part_no: str
    hours: int


def split_installation_fee(amount: int, parts: list[HourlyPart]) -> dict[str, int]:
    """把共用安装费按工时比例分给受益部件，结果守恒到整数分。

    返回 ``{部件号: 分得分}``，总和严格等于 ``amount``；工时必须是
    正整数，部件号不允许重复。
    """
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        raise ValueError("安装费必须是正整数分")
    if not parts:
        raise ValueError("共用安装费至少需要一个受益部件")
    return allocate_children(
        amount, [WeightedChild(p.part_no, p.hours) for p in parts]
    )


def accumulated_depreciation(
    original: int, periods_used: int, total_periods: int
) -> int:
    """累计损耗：``原值 × 已用期数 ÷ 总期数`` 向下取整，最多到原值。"""
    if not isinstance(total_periods, int) or isinstance(total_periods, bool) \
            or total_periods <= 0:
        raise ValueError(f"归零期数必须是正整数: {total_periods!r}")
    if original < 0:
        raise ValueError("原值不能为负")
    if periods_used <= 0:
        return 0
    return min(original, original * periods_used // total_periods)


def book_value(original: int, periods_used: int, total_periods: int) -> int:
    """账面值 = 原值 − 累计损耗。"""
    return original - accumulated_depreciation(original, periods_used, total_periods)


def disposal_loss(book: int, salvage: int) -> int:
    """报废损失 = 旧件账面值 − 验回残件价款，最低为零。"""
    if salvage < 0:
        raise ValueError("残件价款不能为负")
    return max(0, book - salvage)
