"""后厨排程：把一组已定菜单排成可按格执行的整场工单。

模型（题面）：

* 每口锅每格只做一份菜或洗锅；做完菜余味改成该菜的 ``residual``，
  洗锅把余味清成 ``CLEAN``。
* 一份菜只有在当前余味属于 ``dish.allowed_prev`` 时才能下锅。
* 一个桌的餐从下锅到端出按一格计，须落在该桌可接受的时隙里。
* 原料按整场共用，实际耗用不能超过备料。

搜索按“格 -> 锅 -> 桌/洗/闲”回溯，剪枝为：剩余每桌仍有可接受时隙、
剩余桌数不超过剩余容量。找到的第一份工单即返回；失败抛
:class:`~canteen.errors.OrderNotFulfillable` / :class:`~canteen.errors.StockShortfall`。
"""

from __future__ import annotations

from typing import Iterable, Optional

from .errors import OrderNotFulfillable, StockShortfall
from .model import CLEAN, SessionSpec, TablePlan, WorkAction, WorkOrder

_MAX_NODES = 3_000_000


class _Search:
    def __init__(self, spec: SessionSpec, plans: tuple[TablePlan, ...]) -> None:
        self.spec = spec
        self.plans = {p.table_id: p for p in plans}
        if len(self.plans) != len(plans):
            raise OrderNotFulfillable("同一桌被排了多份菜单")
        for p in plans:
            spec.table_of_plan(p)
        self.all_tables = frozenset(self.plans)
        self.pots = spec.pots
        self.n_pots = len(self.pots)
        self.failed: set[tuple[int, tuple[str, ...], frozenset[str]]] = set()
        self.nodes = 0
        # 提前核原料总量，给更明确的错误。
        need: dict[str, int] = {}
        for p in plans:
            dish = spec.dishes[p.dish]
            if dish.ingredient is not None:
                need[dish.ingredient] = need.get(dish.ingredient, 0) + 1
        for ing, qty in need.items():
            if spec.stock.get(ing, 0) < qty:
                raise StockShortfall(
                    f"原料 {ing} 需要 {qty} 份，备料仅 {spec.stock.get(ing, 0)} 份"
                )

    def _used_ingredient(self, ingredient: str, remaining: frozenset[str]) -> int:
        used = 0
        for t in self.all_tables - remaining:
            dish = self.spec.dishes[self.plans[t].dish]
            if dish.ingredient == ingredient:
                used += 1
        return used

    def _remaining_can_finish(self, remaining: frozenset[str], slot: int) -> bool:
        if not remaining:
            return True
        if len(remaining) > (self.spec.slots - slot + 1) * self.n_pots:
            return False
        for t in remaining:
            table = self.spec.tables[t]
            if not any(s >= slot for s in table.acceptable_slots):
                return False
        return True

    def run(self) -> WorkOrder:
        flavors = tuple(p.initial_residual for p in self.pots)
        actions = self._dfs(1, flavors, self.all_tables, ())
        if actions is None:
            raise OrderNotFulfillable("在给定锅、余味、时隙与备料下排不出整场工单")
        action_list = list(actions)
        meals: dict[str, tuple[int, str, str]] = {}
        for a in action_list:
            if a.kind == "cook":
                meals[a.table_id] = (a.slot, a.pot_id, a.dish)
        ing_used: dict[str, int] = {}
        for p in self.plans.values():
            dish = self.spec.dishes[p.dish]
            if dish.ingredient is not None:
                ing_used[dish.ingredient] = ing_used.get(dish.ingredient, 0) + 1
        return WorkOrder(
            actions=tuple(action_list),
            meals=meals,
            ingredients_used=ing_used,
        )

    def _dfs(
        self,
        slot: int,
        flavors: tuple[str, ...],
        remaining: frozenset[str],
        chosen: tuple[WorkAction, ...],
    ) -> Optional[tuple[WorkAction, ...]]:
        self.nodes += 1
        if self.nodes > _MAX_NODES:
            raise OrderNotFulfillable("排程搜索超过上限，限制过紧或桌数过大")
        if not remaining:
            return chosen
        if slot > self.spec.slots:
            return None
        if not self._remaining_can_finish(remaining, slot):
            return None
        key = (slot, flavors, remaining)
        if key in self.failed:
            return None
        result = self._assign_slot(slot, 0, flavors, remaining, (), chosen)
        if result is None:
            self.failed.add(key)
        return result

    def _assign_slot(
        self,
        slot: int,
        pot_idx: int,
        flavors: tuple[str, ...],
        remaining: frozenset[str],
        slot_actions: tuple[WorkAction, ...],
        chosen: tuple[WorkAction, ...],
    ) -> Optional[tuple[WorkAction, ...]]:
        if pot_idx == self.n_pots:
            # 本格排完，进入下一格。
            return self._dfs(slot + 1, flavors, remaining, chosen + slot_actions)

        pot = self.pots[pot_idx]
        flavor = flavors[pot_idx]

        # 候选动作：先尝试下锅（按截止早的桌优先），再洗锅，最后闲置。
        cooks: list[str] = []
        for t in remaining:
            table = self.spec.tables[t]
            if slot not in table.acceptable_slots:
                continue
            dish = self.spec.dishes[self.plans[t].dish]
            if not dish.can_follow(flavor):
                continue
            if dish.ingredient is not None:
                if self._used_ingredient(dish.ingredient, remaining) >= self.spec.stock.get(
                    dish.ingredient, 0
                ):
                    continue
            cooks.append(t)
        cooks.sort(
            key=lambda t: (
                min(s for s in self.spec.tables[t].acceptable_slots if s >= slot),
                t,
            )
        )

        for t in cooks:
            dish = self.spec.dishes[self.plans[t].dish]
            new_flavors = tuple(
                dish.residual if i == pot_idx else flavors[i] for i in range(self.n_pots)
            )
            action = WorkAction(slot, pot.pot_id, "cook", t, dish.name)
            res = self._assign_slot(
                slot,
                pot_idx + 1,
                new_flavors,
                remaining - {t},
                slot_actions + (action,),
                chosen,
            )
            if res is not None:
                return res

        # 洗锅：清空余味（已经干净则跳过，洗了没有意义）。
        if flavor != CLEAN:
            new_flavors = tuple(
                CLEAN if i == pot_idx else flavors[i] for i in range(self.n_pots)
            )
            action = WorkAction(slot, pot.pot_id, "wash")
            res = self._assign_slot(
                slot, pot_idx + 1, new_flavors, remaining, slot_actions + (action,), chosen
            )
            if res is not None:
                return res

        # 闲置。
        return self._assign_slot(
            slot, pot_idx + 1, flavors, remaining, slot_actions, chosen
        )


def schedule(spec: SessionSpec, plans: Iterable[TablePlan]) -> WorkOrder:
    """为一组菜单排出整场工单，不可行时抛异常。"""
    plans_t = tuple(plans)
    return _Search(spec, plans_t).run()


def can_schedule(spec: SessionSpec, plans: Iterable[TablePlan]) -> bool:
    """只判断是否可行，不返回工单。"""
    try:
        schedule(spec, plans)
    except (OrderNotFulfillable, StockShortfall):
        return False
    return True
