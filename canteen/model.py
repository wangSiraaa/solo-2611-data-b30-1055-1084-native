"""餐签、桌、锅与菜色的领域模型（纯数据，无 I/O）。

题面用“余味 / 前味”描述一锅连做的约束，这里用一个字符串表示余味，
``CLEAN`` 表示洗净后的空锅。做完一道菜后锅的余味改成该菜的 ``residual``；
洗锅把余味清成 ``CLEAN``。

“点”即赞助点，全部为整数；面额取**实际付款**的额度，见
:mod:`canteen.vouchers`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional

# 洗净后的空锅余味。任何允许“干净起锅”的菜都要把 CLEAN 放进 allowed_prev。
CLEAN = "clean"


@dataclass(frozen=True)
class Dish:
    """一道菜：做完成什么余味、允许在什么前味上下锅、消耗哪种原料。"""

    name: str
    residual: str
    allowed_prev: frozenset[str]
    ingredient: Optional[str] = None

    def can_follow(self, prev_flavor: str) -> bool:
        return prev_flavor in self.allowed_prev


@dataclass(frozen=True)
class Pot:
    """一口具名锅及其初始余味（``CLEAN`` 表示开局干净）。"""

    pot_id: str
    initial_residual: str = CLEAN


@dataclass(frozen=True)
class DishUpgrade:
    """把基础餐换成某替换菜，多花 ``extra_points`` 点。"""

    dish: str
    extra_points: int

    def __post_init__(self) -> None:
        if self.extra_points < 0:
            raise ValueError("替换菜的加点不能为负")


@dataclass(frozen=True)
class Table:
    """一桌：开餐班、必点的基础餐、可接受时隙、可选替换菜。"""

    table_id: str
    shift_id: str
    base_dish: str
    base_points: int
    acceptable_slots: tuple[int, ...]
    upgrade: Optional[DishUpgrade] = None

    def __post_init__(self) -> None:
        if self.base_points <= 0:
            raise ValueError(f"桌 {self.table_id} 的基础餐点数必须为正")
        if not self.acceptable_slots:
            raise ValueError(f"桌 {self.table_id} 至少要有一个可接受时隙")

    def choices(self) -> tuple[tuple[str, int], ...]:
        """本桌所有可点的 (菜名, 点数)，基础餐在前。"""
        result = [(self.base_dish, self.base_points)]
        if self.upgrade is not None:
            result.append((self.upgrade.dish, self.base_points + self.upgrade.extra_points))
        return tuple(result)

    def plan(self, *, upgraded: bool = False) -> "TablePlan":
        if not upgraded or self.upgrade is None:
            return TablePlan(self.table_id, self.base_dish, self.base_points)
        return TablePlan(
            self.table_id,
            self.upgrade.dish,
            self.base_points + self.upgrade.extra_points,
        )


@dataclass(frozen=True)
class TablePlan:
    """审批与排程用的已定菜单：某桌这一场吃什么、共扣几点。"""

    table_id: str
    dish: str
    points: int

    def __post_init__(self) -> None:
        if self.points < 0:
            raise ValueError("菜单点数不能为负")


@dataclass(frozen=True)
class SessionSpec:
    """一个场次的全部静态限制。"""

    division: str
    slots: int
    pots: tuple[Pot, ...]
    dishes: Mapping[str, Dish]
    tables: Mapping[str, Table]
    stock: Mapping[str, int] = field(default_factory=dict)
    free_trial: bool = False

    def __post_init__(self) -> None:
        if self.slots <= 0:
            raise ValueError("场次时隙数必须为正")
        if not self.pots:
            raise ValueError("场次至少要有一口锅")
        if len({p.pot_id for p in self.pots}) != len(self.pots):
            raise ValueError("锅名重复")
        for t in self.tables.values():
            if t.base_dish not in self.dishes:
                raise ValueError(f"桌 {t.table_id} 的基础菜 {t.base_dish} 不在菜谱中")
            if t.upgrade is not None and t.upgrade.dish not in self.dishes:
                raise ValueError(f"桌 {t.table_id} 的替换菜 {t.upgrade.dish} 不在菜谱中")
            for slot in t.acceptable_slots:
                if not 1 <= slot <= self.slots:
                    raise ValueError(f"桌 {t.table_id} 的时隙 {slot} 超出 1..{self.slots}")

    def table_of_plan(self, plan: TablePlan) -> Table:
        try:
            return self.tables[plan.table_id]
        except KeyError:
            raise ValueError(f"场次没有桌 {plan.table_id}") from None


@dataclass(frozen=True)
class WorkAction:
    """工单上的一格：某锅在这一格做一份菜，或者洗锅。"""

    slot: int
    pot_id: str
    kind: str  # 'cook' | 'wash'
    table_id: Optional[str] = None
    dish: Optional[str] = None

    def __post_init__(self) -> None:
        if self.kind not in ("cook", "wash"):
            raise ValueError(f"未知动作 {self.kind}")
        if self.kind == "cook" and (self.table_id is None or self.dish is None):
            raise ValueError("下锅动作必须带桌与菜")
        if self.kind == "wash" and (self.table_id is not None or self.dish is not None):
            raise ValueError("洗锅动作不能带菜")


@dataclass(frozen=True)
class WorkOrder:
    """整场工单：按格排好的动作、每桌实际出餐位置、实际耗用的原料。"""

    actions: tuple[WorkAction, ...]
    meals: Mapping[str, tuple[int, str, str]]  # table_id -> (slot, pot_id, dish)
    ingredients_used: Mapping[str, int]

    def cooks(self) -> tuple[WorkAction, ...]:
        return tuple(a for a in self.actions if a.kind == "cook")

    def washes(self) -> tuple[WorkAction, ...]:
        return tuple(a for a in self.actions if a.kind == "wash")

    def actions_at(self, slot: int) -> tuple[WorkAction, ...]:
        return tuple(a for a in self.actions if a.slot == slot)
