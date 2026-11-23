"""联营会：集中收各中心完整流向，签发逐来源归线报价。

联营会与实付账、订单员物理分离（独立 SQLite 文件），但只接收各中心
**主动报送**的分送表与实付汇总，不要求外协或外部掌握工坊内部明细。
职责（题面“任选一路”中的集中收数一路）：

1. 集中各中心完整流向，跑精确分数联算，给订单员签**逐来源归线表**；
2. 报价签发时冻结当时的分送表版本与承载边集合；某中心撤回一条服务量
   时，精确找出哪些在用报价的来源走向经过该中心，逐条置失效；
3. 用 SCC 发现跨中心封闭圈，并在一方离线时凭冻结快照复核已承兑旧报价；
4. 提供工坊口径的边界表导出（见 :mod:`orgbudget.boundary`），同址中心
   可作为一个工坊对外交割，内部互供明细不外泄。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Optional

from .costflow import AllocationResult, Distribution, solve_allocation
from .errors import (
    CenterNotFound,
    QuoteInvalid,
    QuoteNotFound,
    TableVersionConflict,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS flow_table (
    center_id   TEXT NOT NULL,
    version     INTEGER NOT NULL,
    target      TEXT NOT NULL,
    num         INTEGER NOT NULL,
    den         INTEGER NOT NULL,
    withdrawn   INTEGER NOT NULL DEFAULT 0 CHECK (withdrawn IN (0, 1)),
    created_at  TEXT NOT NULL,
    PRIMARY KEY (center_id, version, target)
);
CREATE TABLE IF NOT EXISTS withdrawal (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    center_id   TEXT NOT NULL,
    target      TEXT NOT NULL,
    table_version INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS line_registry (
    line_id TEXT PRIMARY KEY,
    num     INTEGER NOT NULL,
    den     INTEGER NOT NULL
);
-- 报价：签发即冻结快照，之后任何重算都不改写本行。
CREATE TABLE IF NOT EXISTS quote (
    quote_id    TEXT PRIMARY KEY,
    status      TEXT NOT NULL CHECK (status IN ('ISSUED', 'ACCEPTED', 'INVALIDATED')),
    period      TEXT NOT NULL,
    payload     TEXT NOT NULL,
    dep_edges   TEXT NOT NULL,
    table_versions TEXT NOT NULL,
    issued_at   TEXT NOT NULL,
    accepted_at TEXT,
    invalidated_at TEXT,
    invalidate_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_quote_status ON quote(status);
"""

_ISSUED = "ISSUED"
_ACCEPTED = "ACCEPTED"
_INVALIDATED = "INVALIDATED"


def _frac_text(f: Fraction) -> str:
    return f"{f.numerator}/{f.denominator}"


def _parse_frac(text: str) -> Fraction:
    num, den = text.split("/")
    return Fraction(int(num), int(den))


@dataclass(frozen=True)
class Quote:
    quote_id: str
    status: str
    period: str
    line_amount: dict[str, Fraction]
    line_by_source: dict[str, dict[str, Fraction]]
    source_edge_flows: dict[str, dict[tuple[str, str], Fraction]]
    dep_edges: tuple[tuple[str, str], ...]
    table_versions: dict[str, int]
    issued_at: str
    accepted_at: Optional[str]
    invalidated_at: Optional[str]
    invalidate_reason: Optional[str]

    @property
    def in_use(self) -> bool:
        """在用 = 已签发且尚未失效（含已被订单承兑的）。"""
        return self.status in (_ISSUED, _ACCEPTED)


class AssociationService:
    """联营会集中流向服务，每个实例持有独立 SQLite 文件。"""

    def __init__(self, db_path: str | Path, *, busy_timeout_ms: int = 10_000) -> None:
        self.db_path = str(db_path)
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

    # ---------------------------------------------------------------- 流向收集

    def submit_flow(
        self,
        center_id: str,
        shares: dict[str, Fraction],
        *,
        expected_version: Optional[int] = None,
    ) -> int:
        """接收一个中心报送的完整分送表（比例合计必须 100%）。"""
        Distribution(center_id, dict(shares))  # 复用校验
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            current = con.execute(
                "SELECT COALESCE(MAX(version), 0) FROM flow_table WHERE center_id = ?",
                (center_id,),
            ).fetchone()[0]
            if expected_version is not None and expected_version != current:
                raise TableVersionConflict(
                    f"中心 {center_id} 当前版本 {current}，报送基于 {expected_version}"
                )
            next_version = current + 1
            now = datetime.now(timezone.utc).isoformat()
            con.executemany(
                "INSERT INTO flow_table(center_id, version, target, num, den, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(center_id, next_version, t, f.numerator, f.denominator, now)
                 for t, f in sorted(shares.items())],
            )
            con.commit()
            return next_version

    def withdraw_flow(self, center_id: str, target: str) -> tuple[int, ...]:
        """中心撤回一条服务量（边）。返回因此失效的在用报价号。

        旧版分送行标记 withdrawn 并留痕；所有来源走向经过该中心出边的
        在用报价（含已承兑的）立即失效，订单员不得再据以签新单。
        """
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            version = con.execute(
                "SELECT COALESCE(MAX(version), 0) FROM flow_table WHERE center_id = ?",
                (center_id,),
            ).fetchone()[0]
            if version == 0:
                raise CenterNotFound(f"中心 {center_id} 从未报送分送表")
            exists = con.execute(
                "SELECT 1 FROM flow_table WHERE center_id = ? AND version = ? AND target = ?",
                (center_id, version, target),
            ).fetchone()
            if exists is None:
                raise CenterNotFound(
                    f"中心 {center_id} 当前版本 {version} 没有送向 {target} 的服务量"
                )
            con.execute(
                "UPDATE flow_table SET withdrawn = 1 "
                "WHERE center_id = ? AND version = ? AND target = ?",
                (center_id, version, target),
            )
            now = datetime.now(timezone.utc).isoformat()
            con.execute(
                "INSERT INTO withdrawal(center_id, target, table_version, created_at) "
                "VALUES (?, ?, ?, ?)",
                (center_id, target, version, now),
            )
            rows = con.execute(
                "SELECT quote_id, dep_edges FROM quote WHERE status IN (?, ?)",
                (_ISSUED, _ACCEPTED),
            ).fetchall()
            invalidated: list[str] = []
            reason = f"中心 {center_id} 撤回送向 {target} 的服务量"
            for quote_id, dep_edges_json in rows:
                edges = [tuple(e) for e in json.loads(dep_edges_json)]
                if any(src == center_id for src, _dst in edges):
                    con.execute(
                        "UPDATE quote SET status = ?, invalidated_at = ?, "
                        "invalidate_reason = ? WHERE quote_id = ?",
                        (_INVALIDATED, now, reason, quote_id),
                    )
                    invalidated.append(quote_id)
            con.commit()
            return tuple(sorted(invalidated))

    def withdrawals(self) -> tuple[tuple[str, str, int, str], ...]:
        with self._connect() as con:
            return tuple(
                con.execute(
                    "SELECT center_id, target, table_version, created_at "
                    "FROM withdrawal ORDER BY id"
                ).fetchall()
            )

    def register_line(self, line_id: str, standard_output: Fraction = Fraction(1)) -> None:
        if standard_output <= 0:
            raise ValueError("标准产量必须为正数")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "INSERT OR REPLACE INTO line_registry(line_id, num, den) VALUES (?, ?, ?)",
                (line_id, standard_output.numerator, standard_output.denominator),
            )
            con.commit()

    def _current_tables_locked(
        self, con: sqlite3.Connection
    ) -> tuple[list[Distribution], dict[str, int]]:
        versions = {
            r[0]: r[1]
            for r in con.execute(
                "SELECT center_id, MAX(version) FROM flow_table GROUP BY center_id"
            ).fetchall()
        }
        distributions: list[Distribution] = []
        for center_id, version in sorted(versions.items()):
            rows = con.execute(
                "SELECT target, num, den FROM flow_table "
                "WHERE center_id = ? AND version = ? AND withdrawn = 0 ORDER BY target",
                (center_id, version),
            ).fetchall()
            if not rows:
                raise CenterNotFound(
                    f"中心 {center_id} 当前分送表已全部撤回，无法联算"
                )
            distributions.append(Distribution(
                center_id, {t: Fraction(n, d) for t, n, d in rows}
            ))
        return distributions, versions

    def current_distributions(self) -> list[Distribution]:
        with self._connect() as con:
            return self._current_tables_locked(con)[0]

    def table_versions(self) -> dict[str, int]:
        with self._connect() as con:
            return {
                r[0]: r[1]
                for r in con.execute(
                    "SELECT center_id, MAX(version) FROM flow_table GROUP BY center_id"
                ).fetchall()
            }

    # ---------------------------------------------------------------- 联算与报价

    def solve(self, direct_cost: dict[str, Fraction]) -> AllocationResult:
        """集中收数后跑一次完整精确分数联算（不落报价）。"""
        with self._connect() as con:
            distributions, _ = self._current_tables_locked(con)
            lines = [r[0] for r in con.execute(
                "SELECT line_id FROM line_registry ORDER BY line_id").fetchall()]
        return solve_allocation(distributions, dict(direct_cost), lines)

    def issue_quote(
        self,
        quote_id: str,
        period: str,
        direct_cost: dict[str, Fraction],
    ) -> Quote:
        """联算并签发逐来源归线报价，冻结快照与依赖边，供订单员使用。"""
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if con.execute("SELECT 1 FROM quote WHERE quote_id = ?",
                           (quote_id,)).fetchone():
                raise QuoteNotFound(f"报价号 {quote_id} 已存在")
            distributions, versions = self._current_tables_locked(con)
            lines = [r[0] for r in con.execute(
                "SELECT line_id FROM line_registry ORDER BY line_id").fetchall()]
            result = solve_allocation(distributions, dict(direct_cost), lines)
            dep_edges = sorted({
                edge
                for trace in result.source_traces.values()
                for edge in trace.edge_flow_amount
            })
            payload = {
                "line_amount": {L: _frac_text(f) for L, f in result.line_amount.items()},
                "line_by_source": {
                    s: {L: _frac_text(f) for L, f in by.items()}
                    for s, by in result.line_by_source.items()
                },
                "source_edge_flows": {
                    s: {f"{u}>{v}": _frac_text(f)
                        for (u, v), f in trace.edge_flow_amount.items()}
                    for s, trace in result.source_traces.items()
                },
                "traps": [
                    {
                        "amount": _frac_text(t.amount),
                        "sources": {s: _frac_text(f) for s, f in t.sources.items()},
                        "component": list(t.component),
                    }
                    for t in result.traps
                ],
                "closed_components": [list(c) for c in result.closed_components],
                "direct_cost": {c: _frac_text(f) for c, f in direct_cost.items()},
            }
            now = datetime.now(timezone.utc).isoformat()
            con.execute(
                "INSERT INTO quote(quote_id, status, period, payload, dep_edges, "
                "table_versions, issued_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (quote_id, _ISSUED, period, json.dumps(payload, ensure_ascii=False),
                 json.dumps(dep_edges), json.dumps(sorted(versions.items())), now),
            )
            con.commit()
        return self.get_quote(quote_id)

    def mark_accepted(self, quote_id: str, order_id: str) -> None:
        """订单正式签出后承兑报价（订单号记入 period 备注旁证）。"""
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT status FROM quote WHERE quote_id = ?", (quote_id,)
            ).fetchone()
            if row is None:
                raise QuoteNotFound(f"报价 {quote_id} 不存在")
            if row[0] == _INVALIDATED:
                raise QuoteInvalid(f"报价 {quote_id} 已失效，不能承兑")
            con.execute(
                "UPDATE quote SET status = ?, accepted_at = ? WHERE quote_id = ?",
                (_ACCEPTED, datetime.now(timezone.utc).isoformat(), quote_id),
            )
            con.commit()

    def get_quote(self, quote_id: str) -> Quote:
        """读报价。一方离线时仍可凭冻结快照完整复核，不实时呼叫任何中心。"""
        with self._connect() as con:
            row = con.execute(
                "SELECT quote_id, status, period, payload, dep_edges, table_versions, "
                "issued_at, accepted_at, invalidated_at, invalidate_reason "
                "FROM quote WHERE quote_id = ?",
                (quote_id,),
            ).fetchone()
        if row is None:
            raise QuoteNotFound(f"报价 {quote_id} 不存在")
        payload = json.loads(row[3])
        return Quote(
            quote_id=row[0], status=row[1], period=row[2],
            line_amount={L: _parse_frac(t) for L, t in payload["line_amount"].items()},
            line_by_source={
                s: {L: _parse_frac(t) for L, t in by.items()}
                for s, by in payload["line_by_source"].items()
            },
            source_edge_flows={
                s: {tuple(edge.split(">")): _parse_frac(t)
                    for edge, t in flows.items()}
                for s, flows in payload["source_edge_flows"].items()
            },
            dep_edges=tuple(tuple(e) for e in json.loads(row[4])),
            table_versions=dict(json.loads(row[5])),
            issued_at=row[6], accepted_at=row[7],
            invalidated_at=row[8], invalidate_reason=row[9],
        )

    def quotes(self, status: Optional[str] = None) -> tuple[str, ...]:
        sql = "SELECT quote_id FROM quote"
        params: tuple = ()
        if status is not None:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY quote_id"
        with self._connect() as con:
            return tuple(r[0] for r in con.execute(sql, params).fetchall())

    def require_live(self, quote_id: str) -> Quote:
        """订单员签新单前调用：失效报价直接拒绝。"""
        quote = self.get_quote(quote_id)
        if not quote.in_use:
            raise QuoteInvalid(
                f"报价 {quote_id} 已于 {quote.invalidated_at} 失效："
                f"{quote.invalidate_reason}"
            )
        return quote

    def cross_center_loops(self) -> tuple[tuple[str, ...], ...]:
        """对当前整张网络跑 SCC，报出所有跨中心封闭圈（含单点自环）。"""
        with self._connect() as con:
            distributions, _ = self._current_tables_locked(con)
        center_succ: dict[str, list[str]] = {d.source: [] for d in distributions}
        for d in distributions:
            for target in d.shares:
                if target in center_succ:
                    center_succ[d.source].append(target)
        from .costflow import _tarjan_scc
        comps = _tarjan_scc(sorted(center_succ), center_succ)
        loops: list[tuple[str, ...]] = []
        for comp in comps:
            member = set(comp)
            has_outlet = any(
                t not in member
                for d in distributions if d.source in member
                for t in d.shares
            )
            if not has_outlet:
                loops.append(tuple(sorted(comp)))
        return tuple(sorted(loops))
