"""离线柜：凭独占拨付单在本地核算拆装，交还余款回执。

柜与中心物理分离（独立 SQLite 文件）：领单时一次性拉取受益设备
快照，此后据实际见到的旧件与残件在本地**先核算再扣额**，全程离线；
交还时把已办拆装清单与余款一并带回中心。拆单严格按登记先后结算，
前序未了结（核算失败待更正）后续拆单不能先动。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional

from .equipment import accumulated_depreciation
from .errors import (
    CabinetBusy,
    EquipmentIdempotencyConflict,
    InsufficientLossAllowance,
    ManifestInvalid,
    NoActiveSlip,
    OrderBlocked,
    OrderNotFound,
    OrderStateError,
    PartNotOnMachine,
    PartNotStocked,
)

_SCHEMA = """
-- 在持拨付单：一柜一单，交还后才能再领。
CREATE TABLE IF NOT EXISTS slip (
    disbursement_id TEXT PRIMARY KEY,
    cabinet_id      TEXT NOT NULL,
    dept_id         TEXT NOT NULL,
    period          INTEGER NOT NULL,
    amount          INTEGER NOT NULL,
    remaining       INTEGER NOT NULL,
    state           TEXT NOT NULL CHECK (state IN ('HELD', 'RETURNED')),
    received_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snap_equipment (
    disbursement_id TEXT NOT NULL,
    equipment_id    TEXT NOT NULL,
    dept_id         TEXT NOT NULL,
    PRIMARY KEY (disbursement_id, equipment_id)
);
CREATE TABLE IF NOT EXISTS snap_part (
    disbursement_id TEXT NOT NULL,
    equipment_id    TEXT NOT NULL,
    part_no         TEXT NOT NULL,
    state           TEXT NOT NULL,
    install_period  INTEGER,
    zero_periods    INTEGER,
    value           INTEGER NOT NULL,
    PRIMARY KEY (disbursement_id, equipment_id, part_no)
);
-- 拆单：登记先后即结算先后（rowid 即队序）。
CREATE TABLE IF NOT EXISTS removal_order (
    order_id        TEXT PRIMARY KEY,
    disbursement_id TEXT NOT NULL,
    equipment_id    TEXT NOT NULL,
    estimated_loss  INTEGER,
    state           TEXT NOT NULL CHECK (state IN ('PENDING', 'SETTLED', 'CANCELLED')),
    last_error      TEXT,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS order_retire (
    order_id TEXT NOT NULL,
    part_no  TEXT NOT NULL,
    salvage  INTEGER NOT NULL CHECK (salvage >= 0),
    PRIMARY KEY (order_id, part_no)
);
CREATE TABLE IF NOT EXISTS order_install (
    order_id     TEXT NOT NULL,
    part_no      TEXT NOT NULL,
    zero_periods INTEGER NOT NULL CHECK (zero_periods > 0),
    PRIMARY KEY (order_id, part_no)
);
-- 已办结算：交还时据此生成回执。
CREATE TABLE IF NOT EXISTS settlement (
    order_id        TEXT PRIMARY KEY,
    disbursement_id TEXT NOT NULL,
    equipment_id    TEXT NOT NULL,
    loss            INTEGER NOT NULL CHECK (loss >= 0),
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settlement_retired (
    order_id   TEXT NOT NULL,
    part_no    TEXT NOT NULL,
    book_value INTEGER NOT NULL,
    salvage    INTEGER NOT NULL,
    loss       INTEGER NOT NULL,
    PRIMARY KEY (order_id, part_no)
);
CREATE TABLE IF NOT EXISTS settlement_installed (
    order_id     TEXT NOT NULL,
    part_no      TEXT NOT NULL,
    zero_periods INTEGER NOT NULL,
    value        INTEGER NOT NULL,
    PRIMARY KEY (order_id, part_no)
);
"""

_HELD = "HELD"
_RETURNED = "RETURNED"
_PENDING = "PENDING"
_SETTLED = "SETTLED"
_CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class RetiredFact:
    part_no: str
    book_value: int
    salvage: int
    loss: int


@dataclass(frozen=True)
class InstalledFact:
    part_no: str
    zero_periods: int
    value: int


@dataclass(frozen=True)
class SettlementInfo:
    """一笔已办结算：实际见到的旧件、残件与装上的新件。"""

    order_id: str
    equipment_id: str
    loss: int
    retired: tuple[RetiredFact, ...]
    installed: tuple[InstalledFact, ...]
    remaining_after: int


@dataclass(frozen=True)
class OrderInfo:
    order_id: str
    equipment_id: str
    estimated_loss: Optional[int]
    state: str
    last_error: Optional[str]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LocalCabinet:
    """一台离线柜。每个实例持有独立 SQLite 文件。"""

    def __init__(
        self, db_path: str | Path, cabinet_id: str, *, busy_timeout_ms: int = 10_000
    ) -> None:
        self.db_path = str(db_path)
        self.cabinet_id = cabinet_id
        self._busy_timeout = busy_timeout_ms
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=self._busy_timeout / 1000)
        con.execute(f"PRAGMA busy_timeout = {self._busy_timeout}")
        con.execute("PRAGMA foreign_keys = ON")
        return con

    # ---------------------------------------------------------------- 领单

    def load(self, snapshot: Mapping) -> None:
        """领取独占拨付单与受益设备快照；一柜同时只持一单。"""
        if snapshot.get("cabinet_id") != self.cabinet_id:
            raise ManifestInvalid(
                f"拨付单属于柜 {snapshot.get('cabinet_id')}，本柜是 {self.cabinet_id}"
            )
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            held = con.execute(
                "SELECT disbursement_id FROM slip WHERE state = ?", (_HELD,)
            ).fetchone()
            if held is not None:
                raise CabinetBusy(f"柜上还有未交还的拨付 {held[0]}，不能再领新单")
            disbursement_id = snapshot["disbursement_id"]
            if con.execute(
                "SELECT 1 FROM slip WHERE disbursement_id = ?", (disbursement_id,)
            ).fetchone():
                raise ManifestInvalid(f"拨付单 {disbursement_id} 已在柜上领过")
            con.execute(
                "INSERT INTO slip(disbursement_id, cabinet_id, dept_id, period, "
                "amount, remaining, state, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    disbursement_id,
                    self.cabinet_id,
                    snapshot["dept_id"],
                    snapshot["period"],
                    snapshot["amount"],
                    snapshot["amount"],
                    _HELD,
                    _now(),
                ),
            )
            for equipment_id, device in snapshot["devices"].items():
                con.execute(
                    "INSERT INTO snap_equipment(disbursement_id, equipment_id, "
                    "dept_id) VALUES (?, ?, ?)",
                    (disbursement_id, equipment_id, device["dept_id"]),
                )
                for part_no, part in device["parts"].items():
                    con.execute(
                        "INSERT INTO snap_part(disbursement_id, equipment_id, "
                        "part_no, state, install_period, zero_periods, value) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            disbursement_id,
                            equipment_id,
                            part_no,
                            part["state"],
                            part["install_period"],
                            part["zero_periods"],
                            part["value"],
                        ),
                    )
            con.commit()

    def held_remaining(self) -> int:
        """柜上留存可用额度。"""
        with self._connect() as con:
            slip = self._active_slip_locked(con)
            return int(slip[1])

    # ---------------------------------------------------------------- 拆单登记

    def register_order(
        self,
        order_id: str,
        equipment_id: str,
        retired: Mapping[str, int],
        installed: Mapping[str, int],
        estimated_loss: Optional[int] = None,
    ) -> None:
        """登记一张拆单：要拆的整套旧件（含验回残件价款）与要装的新件。

        ``order_id`` 是幂等键；登记先后即结算先后。
        """
        if not retired:
            raise ValueError("拆单至少要拆一件旧件")
        overlap = set(retired) & set(installed)
        if overlap:
            raise ValueError(f"新件部件号与退役件冲突: {sorted(overlap)}")
        for part_no, salvage in retired.items():
            if not isinstance(salvage, int) or isinstance(salvage, bool) or salvage < 0:
                raise ValueError(f"旧件 {part_no} 的残件价款必须是非负整数分")
        for part_no, zero in installed.items():
            if not isinstance(zero, int) or isinstance(zero, bool) or zero <= 0:
                raise ValueError(f"新件 {part_no} 的归零期数必须是正整数")
        if estimated_loss is not None and estimated_loss < 0:
            raise ValueError("预估损失不能为负")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            slip = self._active_slip_locked(con)
            disbursement_id = slip[0]
            existing = con.execute(
                "SELECT equipment_id, estimated_loss FROM removal_order "
                "WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if existing is not None:
                same = (
                    existing[0] == equipment_id
                    and existing[1] == estimated_loss
                    and self._order_parts_locked(con, order_id) == (dict(retired), dict(installed))
                )
                if not same:
                    raise EquipmentIdempotencyConflict(
                        f"拆单号 {order_id} 已用于另一张拆单"
                    )
                con.commit()
                return
            device = con.execute(
                "SELECT 1 FROM snap_equipment WHERE disbursement_id = ? "
                "AND equipment_id = ?",
                (disbursement_id, equipment_id),
            ).fetchone()
            if device is None:
                raise ManifestInvalid(
                    f"设备 {equipment_id} 不在拨付单 {disbursement_id} 受益范围内"
                )
            con.execute(
                "INSERT INTO removal_order(order_id, disbursement_id, equipment_id, "
                "estimated_loss, state, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (order_id, disbursement_id, equipment_id, estimated_loss, _PENDING, _now()),
            )
            con.executemany(
                "INSERT INTO order_retire(order_id, part_no, salvage) VALUES (?, ?, ?)",
                [(order_id, p, s) for p, s in sorted(retired.items())],
            )
            con.executemany(
                "INSERT INTO order_install(order_id, part_no, zero_periods) "
                "VALUES (?, ?, ?)",
                [(order_id, p, z) for p, z in sorted(installed.items())],
            )
            con.commit()

    def update_order_salvage(self, order_id: str, part_no: str, salvage: int) -> None:
        """更正待办拆单的残件记录（开柜验件后、结算前）。"""
        if not isinstance(salvage, int) or isinstance(salvage, bool) or salvage < 0:
            raise ValueError("残件价款必须是非负整数分")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT state FROM removal_order WHERE order_id = ?", (order_id,)
            ).fetchone()
            if row is None:
                raise OrderNotFound(f"拆单 {order_id} 不存在")
            if row[0] != _PENDING:
                raise OrderStateError(f"拆单 {order_id} 已了结，不能再改残件记录")
            cur = con.execute(
                "UPDATE order_retire SET salvage = ? WHERE order_id = ? AND part_no = ?",
                (salvage, order_id, part_no),
            )
            if cur.rowcount == 0:
                raise OrderNotFound(f"拆单 {order_id} 没有旧件 {part_no}")
            con.execute(
                "UPDATE removal_order SET last_error = NULL WHERE order_id = ?",
                (order_id,),
            )
            con.commit()

    def cancel_order(self, order_id: str) -> None:
        """撤销一张待办拆单（不拆不占，让出队首）。"""
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT state FROM removal_order WHERE order_id = ?", (order_id,)
            ).fetchone()
            if row is None:
                raise OrderNotFound(f"拆单 {order_id} 不存在")
            if row[0] == _PENDING:
                con.execute(
                    "UPDATE removal_order SET state = ? WHERE order_id = ?",
                    (_CANCELLED, order_id),
                )
            con.commit()

    def orders(self) -> tuple[OrderInfo, ...]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT order_id, equipment_id, estimated_loss, state, last_error "
                "FROM removal_order ORDER BY rowid"
            ).fetchall()
            return tuple(OrderInfo(*r) for r in rows)

    # ---------------------------------------------------------------- 结算

    def settle_next(self) -> Optional[SettlementInfo]:
        """结算队首待办拆单；没有待办拆单返回 ``None``。"""
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            head = self._head_locked(con)
            if head is None:
                con.commit()
                return None
            return self._settle_locked(con, head)

    def settle_order(self, order_id: str) -> SettlementInfo:
        """结算指定拆单；不是队首则报“不能先动”。"""
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT state FROM removal_order WHERE order_id = ?", (order_id,)
            ).fetchone()
            if row is None:
                raise OrderNotFound(f"拆单 {order_id} 不存在")
            if row[0] != _PENDING:
                raise OrderStateError(f"拆单 {order_id} 状态 {row[0]} 不允许结算")
            head = self._head_locked(con)
            if head != order_id:
                raise OrderBlocked(
                    f"前序拆单 {head} 未了结，拆单 {order_id} 不能先动"
                )
            return self._settle_locked(con, order_id)

    def settle_all(self) -> tuple[SettlementInfo, ...]:
        """按登记先后依次结算，直到没有待办拆单或队首卡住。"""
        results: list[SettlementInfo] = []
        while True:
            info = self.settle_next()
            if info is None:
                return tuple(results)
            results.append(info)

    # ---------------------------------------------------------------- 交还

    def hand_back(self) -> dict:
        """交还回执：已办结算清单 + 余款；未了拆单按未使用作废。"""
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            slip = con.execute(
                "SELECT disbursement_id, remaining FROM slip WHERE state = ?",
                (_HELD,),
            ).fetchone()
            if slip is None:
                raise NoActiveSlip("柜上没有在持拨付单")
            disbursement_id, remaining = slip
            settlements = []
            for order_id, equipment_id, loss in con.execute(
                "SELECT order_id, equipment_id, loss FROM settlement "
                "WHERE disbursement_id = ? ORDER BY rowid",
                (disbursement_id,),
            ).fetchall():
                retired = [
                    {"part_no": p, "book_value": b, "salvage": s, "loss": l}
                    for p, b, s, l in con.execute(
                        "SELECT part_no, book_value, salvage, loss "
                        "FROM settlement_retired WHERE order_id = ? ORDER BY part_no",
                        (order_id,),
                    ).fetchall()
                ]
                installed = [
                    {"part_no": p, "zero_periods": z, "value": v}
                    for p, z, v in con.execute(
                        "SELECT part_no, zero_periods, value "
                        "FROM settlement_installed WHERE order_id = ? ORDER BY part_no",
                        (order_id,),
                    ).fetchall()
                ]
                settlements.append(
                    {
                        "order_id": order_id,
                        "equipment_id": equipment_id,
                        "loss": loss,
                        "retired": retired,
                        "installed": installed,
                    }
                )
            con.execute(
                "UPDATE removal_order SET state = ? "
                "WHERE disbursement_id = ? AND state = ?",
                (_CANCELLED, disbursement_id, _PENDING),
            )
            con.execute(
                "UPDATE slip SET state = ? WHERE disbursement_id = ?",
                (_RETURNED, disbursement_id),
            )
            con.commit()
        return {
            "disbursement_id": disbursement_id,
            "cabinet_id": self.cabinet_id,
            "settlements": settlements,
            "remainder": remaining,
        }

    # ---------------------------------------------------------------- 内部

    def _active_slip_locked(self, con: sqlite3.Connection) -> tuple:
        row = con.execute(
            "SELECT disbursement_id, remaining, period FROM slip WHERE state = ?",
            (_HELD,),
        ).fetchone()
        if row is None:
            raise NoActiveSlip("柜上没有在持拨付单")
        return row

    def _head_locked(self, con: sqlite3.Connection) -> Optional[str]:
        row = con.execute(
            "SELECT order_id FROM removal_order WHERE state = ? ORDER BY rowid LIMIT 1",
            (_PENDING,),
        ).fetchone()
        return row[0] if row else None

    def _order_parts_locked(
        self, con: sqlite3.Connection, order_id: str
    ) -> tuple[dict, dict]:
        retired = dict(
            con.execute(
                "SELECT part_no, salvage FROM order_retire WHERE order_id = ?",
                (order_id,),
            ).fetchall()
        )
        installed = dict(
            con.execute(
                "SELECT part_no, zero_periods FROM order_install WHERE order_id = ?",
                (order_id,),
            ).fetchall()
        )
        return retired, installed

    def _settle_locked(self, con: sqlite3.Connection, order_id: str) -> SettlementInfo:
        """先核算再扣额：任何一项不符都不动旧件、不扣额度。"""
        disbursement_id, remaining, period = self._active_slip_locked(con)
        try:
            info = self._compute_settlement_locked(
                con, disbursement_id, order_id, period, remaining
            )
        except Exception as exc:
            con.execute(
                "UPDATE removal_order SET last_error = ? WHERE order_id = ?",
                (str(exc), order_id),
            )
            con.commit()
            raise
        # 落本地事实：旧件退役、新件上机（单独计龄自拨付所属期起）。
        for fact in info.retired:
            con.execute(
                "UPDATE snap_part SET state = 'RETIRED' WHERE disbursement_id = ? "
                "AND equipment_id = ? AND part_no = ?",
                (disbursement_id, info.equipment_id, fact.part_no),
            )
        for fact in info.installed:
            con.execute(
                "UPDATE snap_part SET state = 'INSTALLED', install_period = ?, "
                "zero_periods = ? WHERE disbursement_id = ? AND equipment_id = ? "
                "AND part_no = ?",
                (period, fact.zero_periods, disbursement_id, info.equipment_id,
                 fact.part_no),
            )
        con.execute(
            "UPDATE slip SET remaining = ? WHERE disbursement_id = ?",
            (remaining - info.loss, disbursement_id),
        )
        con.execute(
            "INSERT INTO settlement(order_id, disbursement_id, equipment_id, loss, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (order_id, disbursement_id, info.equipment_id, info.loss, _now()),
        )
        con.executemany(
            "INSERT INTO settlement_retired(order_id, part_no, book_value, salvage, "
            "loss) VALUES (?, ?, ?, ?, ?)",
            [
                (order_id, f.part_no, f.book_value, f.salvage, f.loss)
                for f in info.retired
            ],
        )
        con.executemany(
            "INSERT INTO settlement_installed(order_id, part_no, zero_periods, value) "
            "VALUES (?, ?, ?, ?)",
            [
                (order_id, f.part_no, f.zero_periods, f.value)
                for f in info.installed
            ],
        )
        con.execute(
            "UPDATE removal_order SET state = ?, last_error = NULL WHERE order_id = ?",
            (_SETTLED, order_id),
        )
        con.commit()
        return info

    def _compute_settlement_locked(
        self,
        con: sqlite3.Connection,
        disbursement_id: str,
        order_id: str,
        period: int,
        remaining: int,
    ) -> SettlementInfo:
        row = con.execute(
            "SELECT equipment_id FROM removal_order WHERE order_id = ?", (order_id,)
        ).fetchone()
        equipment_id = row[0]
        retired, installed = self._order_parts_locked(con, order_id)
        facts: list[RetiredFact] = []
        total = 0
        for part_no, salvage in sorted(retired.items()):
            snap = con.execute(
                "SELECT state, install_period, zero_periods, value FROM snap_part "
                "WHERE disbursement_id = ? AND equipment_id = ? AND part_no = ?",
                (disbursement_id, equipment_id, part_no),
            ).fetchone()
            # 先查一整套旧件是否仍在机上。
            if snap is None or snap[0] != "INSTALLED":
                raise PartNotOnMachine(
                    f"旧件 {part_no} 不在设备 {equipment_id} 机上，整套不拆"
                )
            book = snap[3] - accumulated_depreciation(
                snap[3], period - snap[1], snap[2]
            )
            loss = max(0, book - salvage)
            facts.append(RetiredFact(part_no, book, salvage, loss))
            total += loss
        if total > remaining:
            raise InsufficientLossAllowance(
                f"柜上留存 {remaining} 分，本单损失 {total} 分，限额不够"
            )
        new_facts: list[InstalledFact] = []
        for part_no, zero in sorted(installed.items()):
            snap = con.execute(
                "SELECT state, value FROM snap_part WHERE disbursement_id = ? "
                "AND equipment_id = ? AND part_no = ?",
                (disbursement_id, equipment_id, part_no),
            ).fetchone()
            if snap is None or snap[0] != "STOCKED":
                raise PartNotStocked(
                    f"新件 {part_no} 不是设备 {equipment_id} 的在库备件"
                )
            new_facts.append(InstalledFact(part_no, zero, int(snap[1])))
        return SettlementInfo(
            order_id=order_id,
            equipment_id=equipment_id,
            loss=total,
            retired=tuple(facts),
            installed=tuple(new_facts),
            remaining_after=remaining - total,
        )
