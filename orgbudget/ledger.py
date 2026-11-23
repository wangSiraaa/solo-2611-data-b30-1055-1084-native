"""实付账：虚构工坊的成本联算账（独立 SQLite 服务）。

账与组织编制服务、预算执行仓物理分离，只通过已发布的不可变组织产品
（预算所用的那棵树）衔接，落实题面三条入账规则：

1. **子树口径**：每个服务中心对应预算采用版中的一棵子树；
2. **就近归集一次**：入账叶的实付按预算树向上找最近的登记中心，
   全网络只归集这一次，中心自身或其子树内的支付都进该中心；
3. **只保留实付**：支付先登记全额，取消只撤销未付余款，已支付部分
   永远保留；联算只认真正付出去的钱。

分送表按中心版本化：重报必须基于当前版本，旧版快照留痕。分部迁移后
若预算换版被拒绝，预算仓仍绑旧树，本账取预算仓**当前生效方案**的
组织产品版本——即“接的仍是预算所用树”。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Optional

from .costflow import Distribution, solve_allocation
from .errors import (
    CenterNotFound,
    PaymentConflict,
    TableVersionConflict,
    UnpostablePayment,
)
from .org_service import OrgProduct, OrgService

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- 服务中心：绑定登记时的组织产品版本与预算树子树根。
CREATE TABLE IF NOT EXISTS center (
    center_id    TEXT PRIMARY KEY,
    org_version  INTEGER NOT NULL,
    subtree_root TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
-- 分送表版本链：每次重报追加一行，version 严格 +1，旧版永不覆盖。
CREATE TABLE IF NOT EXISTS distribution_table (
    center_id   TEXT NOT NULL REFERENCES center(center_id),
    version     INTEGER NOT NULL,
    target      TEXT NOT NULL,
    numerator   INTEGER NOT NULL,
    denominator INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (center_id, version, target)
);
-- 支付事实：取消只把 cancelled 置 1，实际支付额永久保留。
CREATE TABLE IF NOT EXISTS payment (
    payment_id  TEXT PRIMARY KEY,
    dept_id     TEXT NOT NULL,
    amount      INTEGER NOT NULL CHECK (amount > 0),
    paid        INTEGER NOT NULL CHECK (paid >= 0 AND paid <= amount),
    cancelled   INTEGER NOT NULL CHECK (cancelled IN (0, 1)),
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payment_dept ON payment(dept_id);
-- 可选：直接声明生产线集合（联算时也可显式传入）。
CREATE TABLE IF NOT EXISTS production_line (
    line_id    TEXT PRIMARY KEY,
    standard_output_num INTEGER NOT NULL,
    standard_output_den INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Payment:
    payment_id: str
    dept_id: str
    amount: int
    paid: int
    cancelled: bool

    @property
    def actual_paid(self) -> int:
        """取消后只保留实付；未取消则全额视为已支付。"""
        return self.paid if self.cancelled else self.amount


@dataclass(frozen=True)
class DirectCost:
    center_id: str
    amount: Fraction
    payment_count: int


class LedgerService:
    """实付账服务，每个实例持有独立 SQLite 文件。"""

    def __init__(
        self,
        db_path: str | Path,
        org_service: OrgService,
        *,
        busy_timeout_ms: int = 10_000,
    ) -> None:
        self.db_path = str(db_path)
        self.org = org_service
        self._busy_timeout = busy_timeout_ms
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(_SCHEMA)
            con.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1')"
            )
            con.commit()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=self._busy_timeout / 1000)
        con.execute(f"PRAGMA busy_timeout = {self._busy_timeout}")
        con.execute("PRAGMA foreign_keys = ON")
        return con

    # ---------------------------------------------------------------- 中心与线

    def register_center(
        self, center_id: str, org_version: int, subtree_root: str
    ) -> None:
        """登记服务中心：它对应 ``org_version`` 产品中以 ``subtree_root``
        为根的一棵子树。"""
        product = self.org.get_product(org_version)
        if not product.contains(subtree_root):
            raise CenterNotFound(
                f"子树根 {subtree_root} 不在组织产品 v{org_version} 中"
            )
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if con.execute(
                "SELECT 1 FROM center WHERE center_id = ?", (center_id,)
            ).fetchone():
                raise CenterNotFound(f"中心 {center_id} 已登记，标识不可复用")
            con.execute(
                "INSERT INTO center(center_id, org_version, subtree_root, created_at) "
                "VALUES (?, ?, ?, ?)",
                (center_id, org_version, subtree_root,
                 datetime.now(timezone.utc).isoformat()),
            )
            con.commit()

    def centers(self) -> tuple[tuple[str, int, str], ...]:
        with self._connect() as con:
            return tuple(
                con.execute(
                    "SELECT center_id, org_version, subtree_root FROM center "
                    "ORDER BY center_id"
                ).fetchall()
            )

    def register_line(
        self, line_id: str, standard_output: Fraction = Fraction(1)
    ) -> None:
        if standard_output <= 0:
            raise ValueError("标准产量必须为正数")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "INSERT OR REPLACE INTO production_line"
                "(line_id, standard_output_num, standard_output_den, created_at) "
                "VALUES (?, ?, ?, ?)",
                (line_id, standard_output.numerator, standard_output.denominator,
                 datetime.now(timezone.utc).isoformat()),
            )
            con.commit()

    def lines(self) -> dict[str, Fraction]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT line_id, standard_output_num, standard_output_den "
                "FROM production_line ORDER BY line_id"
            ).fetchall()
        return {lid: Fraction(n, d) for lid, n, d in rows}

    # ---------------------------------------------------------------- 支付

    def register_payment(self, payment_id: str, dept_id: str, amount: int) -> Payment:
        """登记一笔支付（视为已全额支付）。同号重试做幂等校验。"""
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise ValueError("支付金额必须是正整数分")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT dept_id, amount, paid, cancelled FROM payment WHERE payment_id = ?",
                (payment_id,),
            ).fetchone()
            if row is not None:
                if row[0] != dept_id or row[1] != amount:
                    raise PaymentConflict(
                        f"支付号 {payment_id} 已用于 ({row[0]}, {row[1]})，"
                        f"与重试 ({dept_id}, {amount}) 不一致"
                    )
                con.commit()
                return Payment(payment_id, row[0], row[1], row[2], bool(row[3]))
            con.execute(
                "INSERT INTO payment(payment_id, dept_id, amount, paid, cancelled, created_at) "
                "VALUES (?, ?, ?, ?, 0, ?)",
                (payment_id, dept_id, amount, amount,
                 datetime.now(timezone.utc).isoformat()),
            )
            con.commit()
        return self.get_payment(payment_id)

    def register_partial_payment(
        self, payment_id: str, dept_id: str, amount: int, paid: int
    ) -> Payment:
        """登记一笔只实付了 ``paid``、余款尚未支付的支付。"""
        if not isinstance(amount, int) or amount <= 0:
            raise ValueError("支付金额必须是正整数分")
        if not isinstance(paid, int) or paid < 0 or paid > amount:
            raise ValueError("实付额必须满足 0 <= paid <= amount")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if con.execute(
                "SELECT 1 FROM payment WHERE payment_id = ?", (payment_id,)
            ).fetchone():
                raise PaymentConflict(f"支付号 {payment_id} 已存在")
            con.execute(
                "INSERT INTO payment(payment_id, dept_id, amount, paid, cancelled, created_at) "
                "VALUES (?, ?, ?, ?, 0, ?)",
                (payment_id, dept_id, amount, paid,
                 datetime.now(timezone.utc).isoformat()),
            )
            con.commit()
        return self.get_payment(payment_id)

    def cancel_payment(self, payment_id: str) -> Payment:
        """取消支付：只撤销未付余款，已付部分永久保留为实付。"""
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT dept_id, amount, paid, cancelled FROM payment WHERE payment_id = ?",
                (payment_id,),
            ).fetchone()
            if row is None:
                raise CenterNotFound(f"支付 {payment_id} 不存在")
            if not row[3]:
                con.execute(
                    "UPDATE payment SET cancelled = 1 WHERE payment_id = ?",
                    (payment_id,),
                )
            con.commit()
        return self.get_payment(payment_id)

    def get_payment(self, payment_id: str) -> Payment:
        with self._connect() as con:
            row = con.execute(
                "SELECT payment_id, dept_id, amount, paid, cancelled "
                "FROM payment WHERE payment_id = ?",
                (payment_id,),
            ).fetchone()
        if row is None:
            raise CenterNotFound(f"支付 {payment_id} 不存在")
        return Payment(row[0], row[1], row[2], row[3], bool(row[4]))

    def payments(self) -> tuple[Payment, ...]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT payment_id, dept_id, amount, paid, cancelled "
                "FROM payment ORDER BY payment_id"
            ).fetchall()
        return tuple(Payment(*r[:3], r[3], bool(r[4])) for r in rows)

    # ---------------------------------------------------------------- 分送表

    def submit_distribution(
        self,
        center_id: str,
        shares: dict[str, Fraction],
        *,
        expected_version: Optional[int] = None,
    ) -> int:
        """提交（或重报）中心的分送表，返回新版本号。

        重报必须带上 ``expected_version`` 且等于当前版本，防止两个联营
        成员基于旧版互相覆盖；构造 :class:`Distribution` 时即校验合计 100%。
        """
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if not con.execute(
                "SELECT 1 FROM center WHERE center_id = ?", (center_id,)
            ).fetchone():
                raise CenterNotFound(f"中心 {center_id} 未登记")
            current = con.execute(
                "SELECT COALESCE(MAX(version), 0) FROM distribution_table "
                "WHERE center_id = ?",
                (center_id,),
            ).fetchone()[0]
            if expected_version is not None and expected_version != current:
                raise TableVersionConflict(
                    f"中心 {center_id} 当前版本 {current}，重报基于 {expected_version}"
                )
            # 先构造以复用比例校验（合计 1、正数、目标非空）。
            Distribution(center_id, dict(shares))
            next_version = current + 1
            now = datetime.now(timezone.utc).isoformat()
            con.executemany(
                "INSERT INTO distribution_table(center_id, version, target, "
                "numerator, denominator, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                [(center_id, next_version, t, f.numerator, f.denominator, now)
                 for t, f in sorted(shares.items())],
            )
            con.commit()
            return next_version

    def current_distribution(self, center_id: str) -> Distribution:
        with self._connect() as con:
            version = con.execute(
                "SELECT COALESCE(MAX(version), 0) FROM distribution_table "
                "WHERE center_id = ?",
                (center_id,),
            ).fetchone()[0]
            if version == 0:
                raise CenterNotFound(f"中心 {center_id} 尚未提交分送表")
            rows = con.execute(
                "SELECT target, numerator, denominator FROM distribution_table "
                "WHERE center_id = ? AND version = ? ORDER BY target",
                (center_id, version),
            ).fetchall()
        return Distribution(
            center_id, {t: Fraction(n, d) for t, n, d in rows}
        )

    def distribution_versions(self, center_id: str) -> tuple[int, ...]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT DISTINCT version FROM distribution_table WHERE center_id = ? "
                "ORDER BY version",
                (center_id,),
            ).fetchall()
        return tuple(r[0] for r in rows)

    # ---------------------------------------------------------------- 归集与联算

    def _budget_product(self, con: sqlite3.Connection) -> OrgProduct:
        """取预算所用树：读预算仓当前生效方案绑定的组织产品版本。

        分部迁移但换版被拒绝时，预算仓仍绑旧版，本账因此也沿旧树归集，
        不按组织服务的当前树解释。
        """
        row = con.execute(
            "SELECT value FROM meta WHERE key = 'budget_scheme_ref'"
        ).fetchone()
        if row is None:
            raise CenterNotFound(
                "尚未用 bind_budget_scheme 指明预算所用的组织产品版本"
            )
        return self.org.get_product(int(row[0]))

    def bind_budget_version(self, org_version: int) -> None:
        """显式绑定预算采用版（预算仓生效方案对应的组织产品版本）。"""
        self.org.get_product(org_version)  # 版本必须存在
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "INSERT INTO meta(key, value) VALUES ('budget_scheme_ref', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(org_version),),
            )
            con.commit()

    def bound_version(self) -> Optional[int]:
        with self._connect() as con:
            row = con.execute(
                "SELECT value FROM meta WHERE key = 'budget_scheme_ref'"
            ).fetchone()
        return int(row[0]) if row else None

    def _nearest_center(self, product: OrgProduct, dept_id: str) -> Optional[str]:
        """入账部门沿预算树向上找最近的登记中心（含自身）。"""
        centers = {c[0]: c[2] for c in self.centers()}
        cur: Optional[str] = dept_id
        while cur is not None:
            if cur in centers:
                return cur
            cur = product.parent_of[cur]
        return None

    def collect_direct_costs(
        self, product: Optional[OrgProduct] = None
    ) -> dict[str, DirectCost]:
        """把全部实付按“最近登记中心”在预算树上归集一次，只计实付。"""
        with self._connect() as con:
            if product is None:
                product = self._budget_product(con)
            pay_rows = con.execute(
                "SELECT dept_id, amount, paid, cancelled FROM payment"
            ).fetchall()
            center_rows = con.execute(
                "SELECT center_id FROM center ORDER BY center_id"
            ).fetchall()
        totals = {c[0]: [Fraction(0), 0] for c in center_rows}
        for dept_id, amount, paid, cancelled in pay_rows:
            if not product.contains(dept_id):
                raise UnpostablePayment(
                    f"支付部门 {dept_id} 不在预算所用树 v{product.version} 中"
                )
            center = self._nearest_center(product, dept_id)
            if center is None:
                raise UnpostablePayment(
                    f"部门 {dept_id} 沿预算树向上没有任何登记中心，无法归集"
                )
            actual = paid if cancelled else amount
            totals[center][0] += Fraction(actual)
            totals[center][1] += 1
        return {
            cid: DirectCost(cid, vals[0], vals[1])
            for cid, vals in totals.items()
        }

    def run_allocation(self, lines: Optional[list[str]] = None):
        """用当前分送表与归集后的直接成本跑一次完整精确分数联算。"""
        with self._connect() as con:
            product = self._budget_product(con)
        direct = self.collect_direct_costs(product)
        distributions = [
            self.current_distribution(cid) for cid in sorted(direct)
        ]
        # 零成本中心若已登记且有分送表，也必须参与网络（可能承担中转/空圈）。
        known = {d.source for d in distributions}
        for cid, _org_version, _root in self.centers():
            if cid not in known:
                try:
                    distributions.append(self.current_distribution(cid))
                except CenterNotFound:
                    raise CenterNotFound(f"中心 {cid} 已登记但未提交分送表")
        line_list = sorted(lines) if lines is not None else sorted(self.lines())
        return solve_allocation(
            distributions,
            {dc.center_id: dc.amount for dc in direct.values()},
            line_list,
        )
