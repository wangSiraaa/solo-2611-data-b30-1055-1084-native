"""缩微中心订补服务。

职责
----
* 消费校勘侧冻结的 :class:`ChronologyProduct`（可多版并存，按摘要校验），
  本层不重算刊头与历书对应。
* 校验订补单：每个空片位声明缺位与单格/双格类型；已有期不得再贴；一件合刊
  必须整体用同一个 double 格、两半顺序固定。
* “在所有仍成立的排年中都能把所缺各期补上一次”——以多版产品的缺位交集为准，
  只有在每版排年里都成立的补法才被接受。
* 评估两种馆际补拍路由并交其中一种实际可行的工单：
  - ``central``  案卷整件借到中心集中拍；原件在途/封存期间原馆查阅单不可摸
    原报；中心拿到原件后可等迟送的新日历再决定拍哪些幅。
  - ``surrogate`` 各持馆签读员直拍有号副片，实耗该馆片材、原案不借出；批次
    出馆后内容不能改，错一期另用有余量的片，错片不记作缺位已补。
* 同案保全只压住同案另一件，不得把整案登记成已到；选副片时须能出示拍摄当时
  确实在馆的依据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .models import (
    ChronologyProduct,
    CollationError,
    ConflictError,
    ServiceWindow,
    Slot,
    UnfillableOrder,
    WorkOrder,
)


@dataclass
class SurrogateStock:
    """持馆有号副片库存。每张有号片只服务一个指定期目。"""

    surrogate_id: str
    library: str
    target_issue_id: str
    capacity: int = 1          # 可拍摄次数（余量）
    in_house_at_commit: bool = True

    def as_dict(self) -> dict:
        return {"surrogate_id": self.surrogate_id, "library": self.library,
                "target_issue_id": self.target_issue_id, "capacity": self.capacity,
                "in_house_at_commit": self.in_house_at_commit}


@dataclass
class PreservationHold:
    """保全：只压住同案另一件（scope=case 时给出同案成员）。"""

    hold_id: str
    scope: str                 # "case" | "issue"
    case_id: str
    blocked_issue_ids: tuple[str, ...]
    start: int
    end: Optional[int] = None  # 半开 [start, end)，None 为未释放

    def active_on(self, day: int) -> bool:
        if day < self.start:
            return False
        return self.end is None or day < self.end


class MicrofilmOrderService:
    def __init__(self):
        self._products: dict[str, ChronologyProduct] = {}
        self._orders: dict[str, dict] = {}
        self._work_orders: dict[str, WorkOrder] = {}

    # ------------------------------------------------------------ 产品输入
    def ingest_product(self, product: ChronologyProduct) -> dict:
        existing = self._products.get(product.product_id)
        if existing is not None:
            if existing.digest() != product.digest():
                raise ConflictError(
                    "product_conflict",
                    f"chronology product {product.product_id} re-ingested with different content",
                )
            return {"product_id": product.product_id, "replayed": True}
        self._products[product.product_id] = product
        return {"product_id": product.product_id, "replayed": False}

    def live_products(self, almanac_id: Optional[str] = None) -> list[ChronologyProduct]:
        """仍成立的排年产品：同 almanac 下保留全部冻结版（历史产品不倒改）。"""
        out = [p for p in self._products.values()
               if almanac_id is None or p.almanac_id == almanac_id]
        out.sort(key=lambda p: (p.as_of_day, p.product_id))
        return out

    # ------------------------------------------------------------ 单子校验
    @staticmethod
    def _normalise_slots(raw_slots: list[dict | Slot]) -> list[Slot]:
        slots = [s if isinstance(s, Slot) else Slot.from_dict(s) for s in raw_slots]
        if not slots:
            raise CollationError("invalid_order", "an order needs at least one slot")
        ids = [s.slot_id for s in slots]
        if len(set(ids)) != len(ids):
            raise CollationError("invalid_order", "duplicate slot_id")
        covered = [d for s in slots for d in s.missing_days]
        if len(set(covered)) != len(covered):
            raise CollationError("invalid_order", "a missing day may appear in only one slot")
        return slots

    def _issue_for_day(self, product: ChronologyProduct, day: int) -> Optional[str]:
        return product.day_to_issue().get(day)

    def _stable_fillings(self, product_ids: list[str], slots: list[Slot]) -> list[dict]:
        """求在每版排年里都成立的 片位 -> 期目 对应；任何一版不成立即拒单。"""
        products = [self._products[pid] for pid in product_ids]
        # 每个片位在每版产品下可选的期目（期目须恰好覆盖片位缺位）。
        options_per_slot: list[list[str]] = []
        for slot in slots:
            common: Optional[set[str]] = None
            for product in products:
                touched = set()
                for day in slot.missing_days:
                    issue_id = self._issue_for_day(product, day)
                    if issue_id is None:
                        raise UnfillableOrder(
                            "day_not_in_chronology",
                            f"day {day} is not covered by product {product.product_id}",
                        )
                    touched.add(issue_id)
                # 片位类型与原件宽度先判：合刊只能整体进一个 double 格。
                if len(touched) == 1:
                    issue_id = next(iter(touched))
                    placement = next(p for p in product.placements
                                     if p.issue_id == issue_id)
                    width = len(placement.days)
                    if slot.kind == "single" and width == 2:
                        raise UnfillableOrder(
                            "combined_requires_double",
                            f"slot {slot.slot_id}: issue {issue_id} is a combined issue and "
                            "must use one double slot; two single slots cannot take it",
                        )
                    if slot.kind == "double" and width == 1:
                        raise UnfillableOrder(
                            "double_requires_combined",
                            f"slot {slot.slot_id}: a double slot accepts one combined issue only",
                        )
                    if tuple(placement.days) != tuple(slot.missing_days):
                        raise UnfillableOrder(
                            "slot_coverage_mismatch",
                            f"slot {slot.slot_id}: issue {issue_id} covers "
                            f"{list(placement.days)} but slot declares {list(slot.missing_days)}",
                        )
                else:
                    # 一个片位跨了多张原件：只可能是把合刊拆格，同样不允许。
                    raise UnfillableOrder(
                        "slot_crosses_sheets",
                        f"slot {slot.slot_id}: its missing days belong to different issues; "
                        "a combined issue must stay in one slot with fixed half order",
                    )
                common = touched if common is None else common & touched
            assert common is not None
            if not common:
                raise UnfillableOrder(
                    "no_stable_issue",
                    f"slot {slot.slot_id}: no single issue fills it in every live chronology",
                )
            options_per_slot.append(sorted(common))

        # 不同片位不得用同一期（不同原件不得占同一应刊日的补拍侧对偶）。
        assignments: list[dict] = []
        if not self._assign(slots, options_per_slot, 0, set(), assignments):
            raise UnfillableOrder(
                "slot_overlap",
                "no assignment gives each slot a distinct issue across every chronology",
            )
        for assignment in assignments:
            slot = next(s for s in slots if s.slot_id == assignment["slot_id"])
            assignment["missing_days"] = list(slot.missing_days)
            assignment["slot_kind"] = slot.kind
        assignments.sort(key=lambda a: a["missing_days"])
        return assignments

    def _assign(self, slots, options, idx, used: set[str], out: list[dict]) -> bool:
        if idx == len(slots):
            return True
        for issue_id in options[idx]:
            if issue_id in used:
                continue
            out.append({"slot_id": slots[idx].slot_id, "issue_id": issue_id})
            if self._assign(slots, options, idx + 1, used | {issue_id}, out):
                return True
            out.pop()
        return False

    # ------------------------------------------------------------ 路由评估
    def plan_order(
        self,
        order_id: str,
        product_ids: list[str],
        slots: list[dict | Slot],
        window: ServiceWindow,
        *,
        case_members: Optional[dict[str, list[str]]] = None,
        holds: Optional[list[PreservationHold]] = None,
        surrogates: Optional[list[SurrogateStock]] = None,
        existing_filled_days: Optional[list[int]] = None,
        preferred_route: Optional[str] = None,
        route_available: Optional[set[str]] = None,
    ) -> WorkOrder:
        """校验订补单并交一种实际可行的工单。"""
        if order_id in self._orders:
            raise ConflictError("order_conflict", f"order {order_id} already planned")
        for pid in product_ids:
            if pid not in self._products:
                raise CollationError("unknown_product", f"product {pid} not ingested")
        normalised = self._normalise_slots(slots)
        already = set(existing_filled_days or [])
        overlap = sorted(d for s in normalised for d in s.missing_days if d in already)
        if overlap:
            raise UnfillableOrder(
                "already_filmed", f"these days already have film and must not be re-shot: {overlap}"
            )
        assignments = self._stable_fillings(product_ids, normalised)
        issue_ids = [a["issue_id"] for a in assignments]

        holds = list(holds or [])
        surrogates = list(surrogates or [])
        case_members = case_members or {}
        available = set(route_available or {"central", "surrogate"})
        central = self._evaluate_central(
            order_id, product_ids, assignments, issue_ids, window,
            case_members, holds, available,
        )
        surrogate = self._evaluate_surrogate(
            order_id, product_ids, assignments, issue_ids, window,
            holds, surrogates, available,
        )
        choices = [w for w in (central, surrogate) if w.feasible]
        if preferred_route:
            choices.sort(key=lambda w: 0 if w.route == preferred_route else 1)
        if not choices:
            reasons = central.reasons + surrogate.reasons
            wo = WorkOrder(order_id=order_id, route="none", product_id=",".join(product_ids),
                           assignments=assignments, feasible=False, reasons=reasons,
                           commit_by_day=window.latest_proof_day)
            self._work_orders[order_id] = wo
            self._orders[order_id] = {"status": "unfillable", "reasons": reasons}
            raise UnfillableOrder("no_feasible_route", "; ".join(reasons))
        chosen = choices[0]
        self._work_orders[order_id] = chosen
        self._orders[order_id] = {"status": "planned", "route": chosen.route,
                                  "assignments": assignments}
        return chosen

    def _evaluate_central(self, order_id, product_ids, assignments, issue_ids,
                          window: ServiceWindow, case_members, holds, available) -> WorkOrder:
        reasons: list[str] = []
        wo = WorkOrder(order_id=order_id, route="central",
                       product_id=",".join(product_ids), assignments=assignments,
                       occupied_originals=list(issue_ids),
                       commit_by_day=window.latest_proof_day)
        if "central" not in available:
            wo.feasible = False
            wo.reasons = ["central route not offered this month"]
            return wo
        # 同案保全：只压住同案另一件——原件借调会把被保全件一并带走，故整案不可借，
        # 但中心绝不能因此把整案登记成已到。
        blocked_cases = set()
        for hold in holds:
            if hold.scope != "case":
                continue
            members = set(case_members.get(hold.case_id, []))
            if hold.active_on(window.latest_proof_day) and members & set(issue_ids):
                blocked_cases.add(hold.case_id)
                reasons.append(
                    f"hold {hold.hold_id} protects another member of case {hold.case_id}; "
                    "whole-case loan blocked (case must not be registered as arrived)"
                )
        # 原馆查阅不可中止窗口 + 在途：取件日 + ship_days 必须落在最迟校读之前，
        # 且借调区间不能与查阅黑窗相交。
        pickup_day = max(0, window.latest_proof_day - window.ship_days)
        arrival_day = pickup_day + window.ship_days
        if arrival_day > window.latest_proof_day:
            reasons.append(
                f"shipment arrives day {arrival_day} after latest proof day "
                f"{window.latest_proof_day}"
            )
        conflict_day = next((d for d in range(pickup_day, arrival_day + 1)
                             if window.original_unavailable(d)), None)
        if conflict_day is not None:
            reasons.append(
                f"original unavailable in reader blackout / transit on day {conflict_day}"
            )
        wo.feasible = not reasons and not blocked_cases
        wo.reasons = reasons
        wo.shoot_by_day = window.latest_proof_day  # 中心可取晚，等迟送日历后再拍
        return wo

    def _evaluate_surrogate(self, order_id, product_ids, assignments, issue_ids,
                            window: ServiceWindow, holds, surrogates, available) -> WorkOrder:
        reasons: list[str] = []
        wo = WorkOrder(order_id=order_id, route="surrogate",
                       product_id=",".join(product_ids), assignments=assignments,
                       commit_by_day=window.latest_proof_day,
                       shoot_by_day=window.latest_proof_day)
        if "surrogate" not in available:
            wo.feasible = False
            wo.reasons = ["surrogate route not offered this month"]
            return wo
        pool: dict[str, list[SurrogateStock]] = {}
        for stock in surrogates:
            pool.setdefault(stock.target_issue_id, []).append(stock)
        occupied: list[str] = []
        evidence: dict[str, dict] = {}
        for issue_id in issue_ids:
            # 同一张有号片不能跨卷复用：只接受指定期目匹配且有余量的片。
            usable = [s for s in pool.get(issue_id, []) if s.capacity > 0]
            if not usable:
                reasons.append(f"no spare numbered surrogate left for issue {issue_id}")
                wo.feasible = False
                continue
            stock = sorted(usable, key=lambda s: s.surrogate_id)[0]
            if not stock.in_house_at_commit:
                reasons.append(
                    f"surrogate {stock.surrogate_id}: cannot prove the original was in the "
                    "holding library at shooting time"
                )
                wo.feasible = False
                continue
            stock.capacity -= 1  # 实耗该馆片材；出馆后内容冻结
            occupied.append(stock.surrogate_id)
            evidence[issue_id] = {
                "surrogate_id": stock.surrogate_id,
                "library": stock.library,
                "in_house_at_commit": True,
                "provenance": "signed reading-room attestation at shooting time",
            }
        wo.occupied_surrogates = occupied
        wo.in_house_evidence = evidence
        wo.reasons = reasons
        if not wo.feasible:
            return wo
        # 副片路线原案不借出：保全与查阅黑窗不构成阻断，只需在最迟校读前签认。
        wo.feasible = True
        return wo

    # ------------------------------------------------------------ 查询/重开
    def get_work_order(self, order_id: str) -> WorkOrder:
        if order_id not in self._work_orders:
            raise CollationError("unknown_order", f"unknown order {order_id}")
        return self._work_orders[order_id]

    def reopen(self, order_id: str, product_ids: list[str], slots, window: ServiceWindow,
               **kwargs) -> WorkOrder:
        """候选自行决定何时承诺补足；按当时签认重开（新排年产品可在此并入）。"""
        previous = self._work_orders.pop(order_id, None)
        self._orders.pop(order_id, None)
        if previous is not None and previous.route == "surrogate":
            # 已出馆批次内容不能改：重开只能新拍，旧片不记作缺位已补。
            kwargs.setdefault("existing_filled_days", [])
        return self.plan_order(order_id, product_ids, slots, window, **kwargs)
