"""订单员签单服务：封价校验与外协名额占用（独立 SQLite 服务）。

在 :mod:`orgbudget.order` 纯函数求解之外，本服务负责两件有状态的事：

1. **本期外协名册与名额**：每家外协每个期间一个共同名额（期产能），
   正式接单才占用；``BEGIN IMMEDIATE`` 事务把“校验 -> 求解 -> 占名额”
   串行化，同时签出的两单不可能许给同一班；
2. **资格撤销留痕**：撤掉某外协对某品种的资格只影响之后的新单，
   已承兑订单原样保留，可随时复核当时所用的逐来源归线报价。

签单必须同时满足：联营会报价仍在用、需求可交足、最低成本不超封价、
外协剩余名额足够。任一不满足都拒签并给出可复核理由。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Optional

from .association import AssociationService
from .costflow import unit_burden
from .errors import OrderError, SupplierCapacityTaken
from .order import (
    LineCost,
    OptimizationResult,
    OrderSpec,
    Plan,
    Supplier,
    optimize_order,
    optimize_without_qualification,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS supplier_period (
    supplier_id     TEXT NOT NULL,
    period          TEXT NOT NULL,
    order_capacity  INTEGER NOT NULL,
    period_capacity INTEGER NOT NULL,
    prices_json     TEXT NOT NULL,
    revoked_json    TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (supplier_id, period)
);
CREATE TABLE IF NOT EXISTS accepted_order (
    order_id    TEXT PRIMARY KEY,
    period      TEXT NOT NULL,
    quote_id    TEXT NOT NULL,
    total       TEXT NOT NULL,
    seal_price  TEXT NOT NULL,
    plan_json   TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_order_period ON accepted_order(period);
CREATE TABLE IF NOT EXISTS capacity_use (
    supplier_id TEXT NOT NULL,
    period      TEXT NOT NULL,
    order_id    TEXT NOT NULL,
    units       INTEGER NOT NULL,
    PRIMARY KEY (supplier_id, period, order_id)
);
"""


def _frac_text(f: Fraction) -> str:
    return f"{f.numerator}/{f.denominator}"


def _parse_frac(text: str) -> Fraction:
    num, den = text.split("/")
    return Fraction(int(num), int(den))


@dataclass(frozen=True)
class SignedOrder:
    order_id: str
    period: str
    quote_id: str
    plan: Plan
    total: Fraction
    seal_price: Fraction


class OrderDesk:
    """订单员服务，数据落在独立 SQLite 文件。"""

    def __init__(
        self,
        db_path: str | Path,
        association: AssociationService,
        *,
        busy_timeout_ms: int = 10_000,
    ) -> None:
        self.db_path = str(db_path)
        self.assoc = association
        self._busy_timeout = busy_timeout_ms
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(_SCHEMA)
            con.commit()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=self._busy_timeout / 1000)
        con.execute(f"PRAGMA busy_timeout = {self._busy_timeout}")
        con.execute("PRAGMA foreign_keys = ON")
        return con

    # ---------------------------------------------------------------- 名册

    def register_supplier(
        self,
        supplier_id: str,
        period: str,
        prices: dict[str, Fraction],
        order_capacity: int,
        period_capacity: Optional[int] = None,
    ) -> None:
        """登记某期间的外协：逐件价、该单共同产能、本期共同名额。"""
        if order_capacity < 0:
            raise OrderError("单订单共同产能不能为负")
        cap = period_capacity if period_capacity is not None else order_capacity
        if cap < order_capacity:
            raise OrderError("本期共同名额不能小于该单共同产能")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "INSERT OR REPLACE INTO supplier_period(supplier_id, period, "
                "order_capacity, period_capacity, prices_json, revoked_json) "
                "VALUES (?, ?, ?, ?, ?, "
                "(SELECT revoked_json FROM supplier_period WHERE supplier_id = ? "
                "AND period = ?))",
                (supplier_id, period, order_capacity, cap,
                 json.dumps({p: _frac_text(f) for p, f in prices.items()},
                            ensure_ascii=False),
                 supplier_id, period),
            )
            # 首次登记时 revoked_json 子查询为 NULL，补成空列表。
            con.execute(
                "UPDATE supplier_period SET revoked_json = '[]' "
                "WHERE revoked_json IS NULL"
            )
            con.commit()

    def revoke_qualification(
        self, supplier_id: str, period: str, product: str
    ) -> None:
        """撤掉某外协在某期间对某品种的资格；已签订单不受影响。"""
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT revoked_json FROM supplier_period WHERE supplier_id = ? AND period = ?",
                (supplier_id, period),
            ).fetchone()
            if row is None:
                raise OrderError(f"外协 {supplier_id} 在期间 {period} 未登记")
            revoked = set(json.loads(row[0]))
            revoked.add(product)
            con.execute(
                "UPDATE supplier_period SET revoked_json = ? WHERE supplier_id = ? AND period = ?",
                (json.dumps(sorted(revoked)), supplier_id, period),
            )
            con.commit()

    def _load_suppliers_locked(
        self, con: sqlite3.Connection, period: str
    ) -> list[Supplier]:
        rows = con.execute(
            "SELECT supplier_id, order_capacity, period_capacity, prices_json, revoked_json "
            "FROM supplier_period WHERE period = ? ORDER BY supplier_id",
            (period,),
        ).fetchall()
        suppliers: list[Supplier] = []
        for sid, order_cap, period_cap, prices_json, revoked_json in rows:
            revoked = set(json.loads(revoked_json))
            prices = {
                p: _parse_frac(t) for p, t in json.loads(prices_json).items()
                if p not in revoked
            }
            suppliers.append(Supplier(sid, prices, order_cap, period_cap))
        return suppliers

    def remaining_capacity(self, supplier_id: str, period: str) -> int:
        """该外协本期剩余共同名额 = 期产能 - 已承兑订单占用。"""
        with self._connect() as con:
            row = con.execute(
                "SELECT period_capacity FROM supplier_period "
                "WHERE supplier_id = ? AND period = ?",
                (supplier_id, period),
            ).fetchone()
            if row is None:
                raise OrderError(f"外协 {supplier_id} 在期间 {period} 未登记")
            used = con.execute(
                "SELECT COALESCE(SUM(units), 0) FROM capacity_use "
                "WHERE supplier_id = ? AND period = ?",
                (supplier_id, period),
            ).fetchone()[0]
        return int(row[0]) - int(used)

    # ---------------------------------------------------------------- 计价与签单

    def _line_costs(
        self,
        quote_id: str,
        standard_output: dict[str, Fraction],
        opening_fees: dict[str, Fraction],
        line_product: dict[str, str],
    ) -> list[LineCost]:
        quote = self.assoc.get_quote(quote_id)
        lines: list[LineCost] = []
        for line_id, amount in sorted(quote.line_amount.items()):
            output = standard_output.get(line_id, Fraction(1))
            lines.append(LineCost(
                line_id=line_id,
                product=line_product[line_id],
                burden=unit_burden(amount, output),
                opening_fee=opening_fees.get(line_id, Fraction(0)),
            ))
        return lines

    def evaluate(
        self,
        order: OrderSpec,
        quote_id: str,
        line_product: dict[str, str],
        period: str,
        *,
        standard_output: Optional[dict[str, Fraction]] = None,
        opening_fees: Optional[dict[str, Fraction]] = None,
    ) -> OptimizationResult:
        """只求解不占名额，供买方复核最低成本与阻止降价的约束。"""
        self.assoc.require_live(quote_id)
        standard_output = standard_output or {}
        opening_fees = opening_fees or {}
        with self._connect() as con:
            suppliers = self._load_suppliers_locked(con, period)
        lines = self._line_costs(
            quote_id, standard_output, opening_fees, line_product
        )
        return optimize_order(order, lines, suppliers)

    def sign(
        self,
        order: OrderSpec,
        quote_id: str,
        line_product: dict[str, str],
        period: str,
        *,
        standard_output: Optional[dict[str, Fraction]] = None,
        opening_fees: Optional[dict[str, Fraction]] = None,
    ) -> SignedOrder:
        """正式签单：校验报价、求解、封价、占名额，整步事务化。"""
        standard_output = standard_output or {}
        opening_fees = opening_fees or {}
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            quote = self.assoc.require_live(quote_id)
            if con.execute(
                "SELECT 1 FROM accepted_order WHERE order_id = ?", (order.order_id,)
            ).fetchone():
                raise OrderError(f"订单 {order.order_id} 已签出，单号不可复用")
            suppliers = self._load_suppliers_locked(con, period)
            lines = self._line_costs(
                quote_id, standard_output, opening_fees, line_product
            )
            result = optimize_order(order, lines, suppliers)
            plan = result.assert_signable()  # 不可行或超封价在此拒签

            # 名额校验：本期剩余共同名额必须容纳本单实际占用。
            for sup in suppliers:
                wanted = sum(plan.buy.get(sup.supplier_id, {}).values())
                used = con.execute(
                    "SELECT COALESCE(SUM(units), 0) FROM capacity_use "
                    "WHERE supplier_id = ? AND period = ?",
                    (sup.supplier_id, period),
                ).fetchone()[0]
                if int(used) + wanted > sup.effective_period_capacity:
                    raise SupplierCapacityTaken(
                        f"外协 {sup.supplier_id} 本期名额 {sup.effective_period_capacity} "
                        f"已占用 {used}，本单再要 {wanted} 会超额"
                    )

            now = datetime.now(timezone.utc).isoformat()
            con.execute(
                "INSERT INTO accepted_order(order_id, period, quote_id, total, "
                "seal_price, plan_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (order.order_id, period, quote_id, _frac_text(plan.total),
                 _frac_text(order.seal_price),
                 json.dumps(self._plan_to_json(plan), ensure_ascii=False), now),
            )
            con.executemany(
                "INSERT INTO capacity_use(supplier_id, period, order_id, units) "
                "VALUES (?, ?, ?, ?)",
                [(sid, period, order.order_id, sum(items.values()))
                 for sid, items in plan.buy.items() if items],
            )
            con.commit()

        self.assoc.mark_accepted(quote_id, order.order_id)
        return SignedOrder(
            order_id=order.order_id, period=period, quote_id=quote_id,
            plan=plan, total=plan.total, seal_price=order.seal_price,
        )

    def accepted_orders(self, period: Optional[str] = None) -> tuple[SignedOrder, ...]:
        sql = ("SELECT order_id, period, quote_id, total, seal_price, plan_json "
               "FROM accepted_order")
        params: tuple = ()
        if period is not None:
            sql += " WHERE period = ?"
            params = (period,)
        sql += " ORDER BY order_id"
        with self._connect() as con:
            rows = con.execute(sql, params).fetchall()
        return tuple(
            SignedOrder(
                order_id=r[0], period=r[1], quote_id=r[2],
                plan=self._plan_from_json(json.loads(r[5])),
                total=_parse_frac(r[3]), seal_price=_parse_frac(r[4]),
            )
            for r in rows
        )

    def evaluate_without_qualification(
        self,
        order: OrderSpec,
        quote_id: str,
        line_product: dict[str, str],
        period: str,
        supplier_id: str,
        product: str,
        *,
        standard_output: Optional[dict[str, Fraction]] = None,
        opening_fees: Optional[dict[str, Fraction]] = None,
    ) -> OptimizationResult:
        """题面反算：不改动名册，临时撤掉某外协资格后重新求最优。"""
        self.assoc.require_live(quote_id)
        with self._connect() as con:
            suppliers = self._load_suppliers_locked(con, period)
        lines = self._line_costs(
            quote_id, standard_output or {}, opening_fees or {}, line_product
        )
        return optimize_without_qualification(
            order, lines, suppliers, supplier_id, product
        )

    # ---------------------------------------------------------------- 序列化

    @staticmethod
    def _plan_to_json(plan: Plan) -> dict:
        return {
            "make": plan.make,
            "buy": plan.buy,
            "self_cost": _frac_text(plan.self_cost),
            "opening_cost": _frac_text(plan.opening_cost),
            "subcontract_cost": _frac_text(plan.subcontract_cost),
            "opened_lines": list(plan.opened_lines),
        }

    @staticmethod
    def _plan_from_json(data: dict) -> Plan:
        return Plan(
            make={p: int(q) for p, q in data["make"].items()},
            buy={s: {p: int(q) for p, q in items.items()}
                 for s, items in data["buy"].items()},
            self_cost=_parse_frac(data["self_cost"]),
            opening_cost=_parse_frac(data["opening_cost"]),
            subcontract_cost=_parse_frac(data["subcontract_cost"]),
            opened_lines=tuple(data["opened_lines"]),
        )
