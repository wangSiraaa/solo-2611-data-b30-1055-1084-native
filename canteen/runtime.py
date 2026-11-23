"""工单执行与交班：按格依次做完，实菜 / 原料 / 点数全部实扣且仅追加。

原则（题面）：

* 一份得到资助席同一批准的整场工单可以依次做完；
* 已经送上的菜、耗掉的原料、扣掉的点数都变成**仅追加事件**，不能用新菜单
  覆盖；
* 做过的实菜与已耗点数**随交班保留**，下一个班仍能看到、不能回滚；
* 不收费的普通对照试做（``free_trial=True``）照常执行，但不落任何扣点事件。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

from .errors import (
    DishForbidden,
    SlotConflict,
    StockShortfall,
    VoucherConflict,
    VoucherError,
)
from .model import CLEAN, SessionSpec, TablePlan, WorkAction, WorkOrder
from .vouchers import Approval


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Charge:
    """预定的扣点来路：某桌从某来源取几点。"""

    table_id: str
    source: str
    points: int

    def __post_init__(self) -> None:
        if self.points <= 0:
            raise ValueError("扣点必须为正")


def charges_from_approval(approval: Approval) -> tuple[Charge, ...]:
    return tuple(
        Charge(a.table_id, vid, pts)
        for a in approval.allocations
        for vid, pts in a.parts
        if pts > 0
    )


@dataclass(frozen=True)
class ServedMeal:
    table_id: str
    dish: str
    slot: int
    pot_id: str
    points: int


@dataclass(frozen=True)
class PointDraw:
    table_id: str
    source: str
    points: int
    slot: int


@dataclass(frozen=True)
class IngredientDraw:
    ingredient: str
    amount: int
    slot: int


@dataclass(frozen=True)
class Handover:
    """一次交班的不可变快照：保留已做的实菜、已耗原料与已扣点数。"""

    sequence: int
    served: tuple[ServedMeal, ...]
    ingredients: tuple[IngredientDraw, ...]
    points: tuple[PointDraw, ...]
    created_at: str = field(default_factory=_now)

    @property
    def served_count(self) -> int:
        return len(self.served)

    @property
    def points_used(self) -> int:
        return sum(p.points for p in self.points)

    def tables_served(self) -> tuple[str, ...]:
        return tuple(sorted({m.table_id for m in self.served}))


class SessionRuntime:
    """按工单依次执行，并维护仅追加的执行事实。"""

    def __init__(
        self,
        spec: SessionSpec,
        plans: Iterable[TablePlan],
        order: WorkOrder,
        charges: Optional[Iterable[Charge]] = None,
        *,
        free_trial: bool = False,
        prior_served: Iterable[str] = (),
        base_covered: Optional[dict[str, int]] = None,
    ) -> None:
        self.spec = spec
        self.plans = {p.table_id: p for p in plans}
        self.order = order
        self.free_trial = free_trial
        self._served: list[ServedMeal] = []
        self._points: list[PointDraw] = []
        self._ingredients: list[IngredientDraw] = []
        self._washes: list[tuple[int, str]] = []
        self._flavors = {p.pot_id: p.initial_residual for p in spec.pots}
        self._done = False
        self._handovers: list[Handover] = []
        # 上一班已端出的桌：本班不得以新菜单覆盖。
        self._prior_served = set(prior_served)
        # 部分点数由别处覆盖（基础餐赞助）：这些桌的 charges 只需补差额。
        self._base_covered = dict(base_covered or {})

        self._charges: dict[str, list[list]] = {t: [] for t in self.plans}
        if free_trial:
            if charges:
                raise VoucherError("普通对照试做不收费，不能带扣点计划")
        else:
            charge_list = list(charges or ())
            for c in charge_list:
                if c.table_id not in self.plans:
                    raise VoucherError(f"扣点计划含未知桌 {c.table_id}")
                self._charges[c.table_id].append([c.source, c.points])
            for tid, plan in self.plans.items():
                total = sum(pts for _, pts in self._charges[tid])
                if total + self._base_covered.get(tid, 0) != plan.points:
                    raise VoucherConflict(
                        f"桌 {tid} 扣点计划 {total} + 已覆盖 {self._base_covered.get(tid, 0)}"
                        f" ≠ 菜单 {plan.points} 点"
                    )

    # ---------------------------------------------------------------- 执行
    def execute(self) -> "SessionRuntime":
        if self._done:
            return self
        for action in self.order.actions:
            if action.kind == "cook":
                self._cook(action)
            else:
                self._wash(action)
        self._done = True
        self.assert_invariants()
        return self

    def _cook(self, action: WorkAction) -> None:
        table = self.spec.table_of_plan(self.plans[action.table_id])
        dish = self.spec.dishes[action.dish]
        if action.slot not in table.acceptable_slots:
            raise SlotConflict(
                f"桌 {action.table_id} 不接受时隙 {action.slot}（可接受 {table.acceptable_slots}）"
            )
        if action.table_id in self._prior_served:
            raise SlotConflict(f"桌 {action.table_id} 上一班已端出，不能以新菜单覆盖")
        if any(m.table_id == action.table_id for m in self._served):
            raise SlotConflict(f"桌 {action.table_id} 已经端出，不能以新菜单覆盖")
        flavor = self._flavors[action.pot_id]
        if not dish.can_follow(flavor):
            raise DishForbidden(
                f"锅 {action.pot_id} 当前余味 {flavor!r} 不允许做 {action.dish}"
            )
        if dish.ingredient is not None:
            used = sum(
                d.amount for d in self._ingredients if d.ingredient == dish.ingredient
            )
            if used + 1 > self.spec.stock.get(dish.ingredient, 0):
                raise StockShortfall(
                    f"原料 {dish.ingredient} 已不足以再做一份 {action.dish}"
                )
        self._flavors[action.pot_id] = dish.residual
        self._served.append(
            ServedMeal(
                action.table_id,
                action.dish,
                action.slot,
                action.pot_id,
                self.plans[action.table_id].points,
            )
        )
        if dish.ingredient is not None:
            self._ingredients.append(IngredientDraw(dish.ingredient, 1, action.slot))
        if not self.free_trial:
            for source, points in self._charges[action.table_id]:
                self._points.append(PointDraw(action.table_id, source, points, action.slot))

    def _wash(self, action: WorkAction) -> None:
        if self._flavors[action.pot_id] == CLEAN:
            raise SlotConflict(f"锅 {action.pot_id} 已干净，无需再洗")
        self._flavors[action.pot_id] = CLEAN
        self._washes.append((action.slot, action.pot_id))

    # ---------------------------------------------------------------- 查询
    @property
    def served(self) -> tuple[ServedMeal, ...]:
        return tuple(self._served)

    @property
    def point_draws(self) -> tuple[PointDraw, ...]:
        return tuple(self._points)

    @property
    def ingredient_draws(self) -> tuple[IngredientDraw, ...]:
        return tuple(self._ingredients)

    @property
    def washes(self) -> tuple[tuple[int, str], ...]:
        return tuple(self._washes)

    @property
    def points_used(self) -> int:
        return sum(p.points for p in self._points)

    def ingredients_consumed(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for d in self._ingredients:
            result[d.ingredient] = result.get(d.ingredient, 0) + d.amount
        return result

    def point_provenance(self) -> str:
        lines = [
            f"  {p.table_id} @槽{p.slot} <- {p.source} 扣 {p.points} 点" for p in self._points
        ]
        return "\n".join(lines) if lines else "  （试做，不扣点）"

    # ---------------------------------------------------------------- 交班
    def handover(self) -> Handover:
        """生成一份不可回滚的交班快照，已做事实随班保留。"""
        h = Handover(
            sequence=len(self._handovers) + 1,
            served=tuple(self._served),
            ingredients=tuple(self._ingredients),
            points=tuple(self._points),
        )
        self._handovers.append(h)
        return h

    # ---------------------------------------------------------------- 不变量
    def assert_invariants(self) -> None:
        seen: set[str] = set()
        for m in self._served:
            if m.table_id in seen:
                raise SlotConflict(f"桌 {m.table_id} 重复出餐")
            seen.add(m.table_id)
        missing = set(self.plans) - seen
        if missing:
            raise SlotConflict(f"桌 {sorted(missing)} 未出餐")
        if not self.free_trial:
            for tid, plan in self.plans.items():
                got = sum(p.points for p in self._points if p.table_id == tid)
                if got + self._base_covered.get(tid, 0) != plan.points:
                    raise VoucherConflict(
                        f"桌 {tid} 实扣 {got} + 已覆盖 {self._base_covered.get(tid, 0)}"
                        f" 与菜单 {plan.points} 不一致"
                    )
        # 原料不超备料。
        for ing, qty in self.ingredients_consumed().items():
            if qty > self.spec.stock.get(ing, 0):
                raise StockShortfall(
                    f"原料 {ing} 超用：{qty} > {self.spec.stock.get(ing, 0)}"
                )


@dataclass(frozen=True)
class ShiftBook:
    """跨班账：汇总已交班事实，并阻止下一班覆盖已端出的桌。"""

    _served: tuple[ServedMeal, ...] = ()
    _points: tuple[PointDraw, ...] = ()
    _ingredients: tuple[IngredientDraw, ...] = ()
    _handovers: int = 0

    def commit(self, handover: Handover) -> "ShiftBook":
        return ShiftBook(
            _served=self._served + handover.served,
            _points=self._points + handover.points,
            _ingredients=self._ingredients + handover.ingredients,
            _handovers=self._handovers + 1,
        )

    @property
    def served(self) -> tuple[ServedMeal, ...]:
        return self._served

    @property
    def points(self) -> tuple[PointDraw, ...]:
        return self._points

    @property
    def ingredients(self) -> tuple[IngredientDraw, ...]:
        return self._ingredients

    @property
    def handovers(self) -> int:
        return self._handovers

    def tables_served(self) -> frozenset[str]:
        return frozenset(m.table_id for m in self._served)

    def points_used(self) -> int:
        return sum(p.points for p in self._points)

    def ingredient_consumed(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for d in self._ingredients:
            result[d.ingredient] = result.get(d.ingredient, 0) + d.amount
        return result
