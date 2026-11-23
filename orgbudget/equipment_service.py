"""设备会计中心：购建底单、原值封存、机身登记、报废限额与独占拨付。

设备会计仓与预算执行仓物理分离（独立 SQLite 文件），只通过
:class:`~orgbudget.budget_service.BudgetService` 的公开查询衔接：

* 每张购建底单认领一笔真实实付事件（及其所属承诺），一笔实付可拆给
  若干具名部件，也可作为共用安装费按工时比例分摊；各来源之和须等于
  实付原件，归入哪台设备、钱落在哪个叶都逐分可查；
* 底单落账时快照所属叶与预算方案采用的组织产品版本，后续迁编不改写
  早期入账（含安装费）的归属；
* 会计可在工坊尚未要求换件时独立封存一台设备的原值；封存重验
  “重领 / 漏分 / 跨分部拿错实付原件”三条红线，违反即拒封；
* 现场拆装只实现**独占拨付**一种方式：中心把本期报废损失限额中的一笔
  实际额度独占拨给某台离线柜并锁定受益设备（这笔钱在交还前不得再给
  另一台设备），柜上据实际见到的旧件与残件先核算再扣额，余款交还后
  才回到可用限额；拆件交易按退役件原号可查，已拆旧件不可能按猜重拆。

写操作统一在 ``BEGIN IMMEDIATE`` 事务内完成“读取校验 → 写入”。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional

from .budget_service import BudgetService
from .equipment import accumulated_depreciation, split_installation_fee, HourlyPart
from .errors import (
    AllocationMismatch,
    CommitmentNotFound,
    CrossDivisionClaim,
    DisbursementClosed,
    DisbursementNotFound,
    DuplicateClaim,
    DuplicateEquipment,
    EquipmentIdempotencyConflict,
    EquipmentLocked,
    EquipmentNotFound,
    InsufficientLossAllowance,
    LimitBelowObligations,
    MissingCommitment,
    NoLossLimit,
    NotSealed,
    PartAlreadyRetired,
    PartNotFound,
    SealStale,
    SpendEventNotFound,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- 机身：按实际投用期登记；voucher_generation 随底单变动递增，
-- sealed_generation 是最近一次封存覆盖的底单代次，二者不等即封存过期。
CREATE TABLE IF NOT EXISTS equipment (
    equipment_id       TEXT PRIMARY KEY,
    dept_id            TEXT NOT NULL,
    commissioned_period INTEGER NOT NULL CHECK (commissioned_period >= 1),
    voucher_generation INTEGER NOT NULL DEFAULT 0,
    sealed_generation  INTEGER,
    created_at         TEXT NOT NULL
);
-- 部件状态机：INSTALLED 在机上 / STOCKED 在库备件 / RETIRED 已退役。
CREATE TABLE IF NOT EXISTS part (
    equipment_id   TEXT NOT NULL REFERENCES equipment(equipment_id),
    part_no        TEXT NOT NULL,
    state          TEXT NOT NULL CHECK (state IN ('INSTALLED', 'STOCKED', 'RETIRED')),
    install_period INTEGER,
    zero_periods   INTEGER,
    PRIMARY KEY (equipment_id, part_no)
);
-- 购建底单：每张认领一笔真实实付事件；落账即快照所属叶与组织产品版本。
CREATE TABLE IF NOT EXISTS voucher (
    voucher_id     TEXT PRIMARY KEY,
    equipment_id   TEXT NOT NULL REFERENCES equipment(equipment_id),
    dept_id        TEXT NOT NULL,
    org_version    INTEGER NOT NULL,
    spend_idem_key TEXT NOT NULL,
    request_no     TEXT NOT NULL,
    spend_amount   INTEGER NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_voucher_eq ON voucher(equipment_id);
CREATE INDEX IF NOT EXISTS idx_voucher_spend ON voucher(spend_idem_key);
CREATE TABLE IF NOT EXISTS voucher_line (
    voucher_id TEXT NOT NULL REFERENCES voucher(voucher_id),
    line_no    INTEGER NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('DIRECT', 'INSTALL')),
    part_no    TEXT,
    amount     INTEGER NOT NULL CHECK (amount > 0),
    hours_json TEXT,
    PRIMARY KEY (voucher_id, line_no)
);
-- 原值封存快照：每部件一行，generation 是封存时覆盖的底单代次。
CREATE TABLE IF NOT EXISTS seal (
    equipment_id TEXT NOT NULL REFERENCES equipment(equipment_id),
    part_no      TEXT NOT NULL,
    value        INTEGER NOT NULL CHECK (value >= 0),
    generation   INTEGER NOT NULL,
    PRIMARY KEY (equipment_id, part_no)
);
-- 每个工坊（叶）每期一次报废损失总限额。
CREATE TABLE IF NOT EXISTS loss_limit (
    dept_id      TEXT NOT NULL,
    period       INTEGER NOT NULL,
    limit_amount INTEGER NOT NULL CHECK (limit_amount >= 0),
    PRIMARY KEY (dept_id, period)
);
-- 独占拨付单：OPEN 期间金额独占、受益设备锁定，交还结案后才释放。
CREATE TABLE IF NOT EXISTS disbursement (
    disbursement_id TEXT PRIMARY KEY,
    dept_id         TEXT NOT NULL,
    period          INTEGER NOT NULL,
    cabinet_id      TEXT NOT NULL,
    amount          INTEGER NOT NULL CHECK (amount > 0),
    state           TEXT NOT NULL CHECK (state IN ('OPEN', 'CLOSED')),
    manifest_json   TEXT,
    created_at      TEXT NOT NULL,
    closed_at       TEXT
);
CREATE TABLE IF NOT EXISTS disbursement_device (
    disbursement_id TEXT NOT NULL REFERENCES disbursement(disbursement_id),
    equipment_id    TEXT NOT NULL REFERENCES equipment(equipment_id),
    PRIMARY KEY (disbursement_id, equipment_id)
);
-- 拆装交易：回单丢失也能按退役件原号查到此次装了什么。
CREATE TABLE IF NOT EXISTS replacement (
    tx_id           TEXT PRIMARY KEY,
    equipment_id    TEXT NOT NULL REFERENCES equipment(equipment_id),
    dept_id         TEXT NOT NULL,
    period          INTEGER NOT NULL,
    disbursement_id TEXT NOT NULL,
    order_id        TEXT NOT NULL,
    loss            INTEGER NOT NULL CHECK (loss >= 0),
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_replacement_eq ON replacement(equipment_id);
CREATE TABLE IF NOT EXISTS replacement_part (
    tx_id        TEXT NOT NULL REFERENCES replacement(tx_id),
    role         TEXT NOT NULL CHECK (role IN ('RETIRED', 'INSTALLED')),
    part_no      TEXT NOT NULL,
    book_value   INTEGER,
    salvage      INTEGER,
    loss         INTEGER,
    zero_periods INTEGER,
    value        INTEGER,
    PRIMARY KEY (tx_id, role, part_no)
);
"""

_INSTALLED = "INSTALLED"
_STOCKED = "STOCKED"
_RETIRED = "RETIRED"
_OPEN = "OPEN"
_CLOSED = "CLOSED"


@dataclass(frozen=True)
class VoucherLine:
    """底单分录：``DIRECT`` 直接拆给具名部件，``INSTALL`` 共用安装费按工时摊。"""

    kind: str
    amount: int
    part_no: Optional[str] = None
    hours: Optional[Mapping[str, int]] = None

    @staticmethod
    def direct(part_no: str, amount: int) -> "VoucherLine":
        return VoucherLine(kind="DIRECT", amount=amount, part_no=part_no)

    @staticmethod
    def install(amount: int, hours: Mapping[str, int]) -> "VoucherLine":
        return VoucherLine(kind="INSTALL", amount=amount, hours=dict(hours))


@dataclass(frozen=True)
class VoucherInfo:
    voucher_id: str
    equipment_id: str
    dept_id: str
    org_version: int
    spend_idem_key: str
    request_no: str
    spend_amount: int
    lines: tuple[VoucherLine, ...]


@dataclass(frozen=True)
class MoneyTrailEntry:
    """一笔分到某部件的钱：可逐分追查回实付事件、承诺与归属叶。"""

    voucher_id: str
    spend_idem_key: str
    request_no: str
    dept_id: str
    org_version: int
    kind: str
    amount: int


@dataclass(frozen=True)
class PartStatus:
    part_no: str
    state: str
    install_period: Optional[int]
    zero_periods: Optional[int]
    sealed_value: Optional[int]
    seal_fresh: bool
    book_value: Optional[int]
    retired_tx: Optional[str]


@dataclass(frozen=True)
class LossAllowance:
    """某工坊某期的报废损失限额账。"""

    dept_id: str
    period: int
    limit: Optional[int]
    confirmed: int
    outstanding: int
    available: Optional[int]


@dataclass(frozen=True)
class DisbursementInfo:
    disbursement_id: str
    dept_id: str
    period: int
    cabinet_id: str
    amount: int
    state: str
    equipment_ids: tuple[str, ...]


@dataclass(frozen=True)
class RetiredPartInfo:
    part_no: str
    book_value: int
    salvage: int
    loss: int


@dataclass(frozen=True)
class InstalledPartInfo:
    part_no: str
    zero_periods: int
    value: int


@dataclass(frozen=True)
class ReplacementInfo:
    """一次拆装交易：整套旧件一次拆下，新件同期装上、单独计龄。"""

    tx_id: str
    equipment_id: str
    dept_id: str
    period: int
    disbursement_id: str
    order_id: str
    loss: int
    retired: tuple[RetiredPartInfo, ...]
    installed: tuple[InstalledPartInfo, ...]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class _EquipmentRow:
    equipment_id: str
    dept_id: str
    commissioned_period: int
    voucher_generation: int
    sealed_generation: Optional[int]


class EquipmentService:
    """设备会计中心仓。每个实例持有独立 SQLite 文件。"""

    def __init__(
        self,
        db_path: str | Path,
        budget: BudgetService,
        *,
        busy_timeout_ms: int = 10_000,
    ) -> None:
        self.db_path = str(db_path)
        self.budget = budget
        self._busy_timeout = busy_timeout_ms
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(_SCHEMA)
            con.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('current_period', '1')"
            )
            con.commit()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=self._busy_timeout / 1000)
        con.execute(f"PRAGMA busy_timeout = {self._busy_timeout}")
        con.execute("PRAGMA foreign_keys = ON")
        return con

    # ---------------------------------------------------------------- 期间时钟

    def current_period(self) -> int:
        with self._connect() as con:
            return int(
                con.execute(
                    "SELECT value FROM meta WHERE key = 'current_period'"
                ).fetchone()[0]
            )

    def advance_period(self) -> int:
        """结束本期：期间号加一。已用期数 = 当前期间 − 投用期间。"""
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            period = int(
                con.execute(
                    "SELECT value FROM meta WHERE key = 'current_period'"
                ).fetchone()[0]
            ) + 1
            con.execute(
                "UPDATE meta SET value = ? WHERE key = 'current_period'", (str(period),)
            )
            con.commit()
        return period

    # ---------------------------------------------------------------- 机身登记

    def register_equipment(
        self,
        equipment_id: str,
        dept_id: str,
        commissioned_period: int,
        parts: Mapping[str, int],
    ) -> None:
        """按实际投用期、归零期数与部件号登记机身（初始部件全部在机上）。"""
        if not parts:
            raise ValueError("机身至少需要一个部件")
        for part_no, zero in parts.items():
            self._check_zero_periods(zero)
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if con.execute(
                "SELECT 1 FROM equipment WHERE equipment_id = ?", (equipment_id,)
            ).fetchone():
                raise DuplicateEquipment(f"设备 {equipment_id} 已登记")
            current = int(
                con.execute(
                    "SELECT value FROM meta WHERE key = 'current_period'"
                ).fetchone()[0]
            )
            if not isinstance(commissioned_period, int) or isinstance(
                commissioned_period, bool
            ) or not (1 <= commissioned_period <= current):
                raise ValueError(
                    f"投用期必须在 1..{current} 之间: {commissioned_period!r}"
                )
            con.execute(
                "INSERT INTO equipment(equipment_id, dept_id, commissioned_period, "
                "voucher_generation, sealed_generation, created_at) "
                "VALUES (?, ?, ?, 0, NULL, ?)",
                (equipment_id, dept_id, commissioned_period, _now()),
            )
            con.executemany(
                "INSERT INTO part(equipment_id, part_no, state, install_period, "
                "zero_periods) VALUES (?, ?, ?, ?, ?)",
                [
                    (equipment_id, p, _INSTALLED, commissioned_period, z)
                    for p, z in sorted(parts.items())
                ],
            )
            con.commit()

    # ---------------------------------------------------------------- 购建底单

    def post_voucher(
        self,
        voucher_id: str,
        equipment_id: str,
        spend_idem_key: str,
        lines: list[VoucherLine],
    ) -> VoucherInfo:
        """登记购建底单：认领一笔真实实付事件并给出部件拆分 / 安装费分摊。

        ``voucher_id`` 是幂等键：同键同内容重试返回既有底单，同键不同
        内容报 :class:`EquipmentIdempotencyConflict`。落账即快照所属叶
        与当前预算方案采用的组织产品版本，后续迁编不改写归属。
        """
        self._validate_lines(lines)
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute(
                "SELECT equipment_id, spend_idem_key FROM voucher WHERE voucher_id = ?",
                (voucher_id,),
            ).fetchone()
            if existing is not None:
                info = self._load_voucher_locked(con, voucher_id)
                if (
                    info.equipment_id != equipment_id
                    or info.spend_idem_key != spend_idem_key
                    or info.lines != tuple(lines)
                ):
                    raise EquipmentIdempotencyConflict(
                        f"底单号 {voucher_id} 已用于另一笔入账"
                    )
                con.commit()
                return info
            eq = self._equipment_locked(con, equipment_id)
            self._assert_unlocked_locked(con, equipment_id)
            spend = self.budget.spend_event(spend_idem_key)
            if spend is None:
                raise SpendEventNotFound(f"实付事件 {spend_idem_key} 不存在")
            try:
                self.budget.get_commitment(spend.request_no)
            except CommitmentNotFound:
                raise MissingCommitment(
                    f"实付事件 {spend_idem_key} 所属承诺 {spend.request_no} 不存在"
                ) from None
            # 重领：一笔实付事件全库只允许被认领一次。
            clash = con.execute(
                "SELECT voucher_id FROM voucher WHERE spend_idem_key = ?",
                (spend_idem_key,),
            ).fetchone()
            if clash is not None:
                raise DuplicateClaim(
                    f"实付事件 {spend_idem_key} 已被底单 {clash[0]} 认领，不得重领"
                )
            for line in lines:
                for part_no in self._line_parts(line):
                    self._assert_part_not_retired_locked(con, equipment_id, part_no)
            org_version = self.budget.active_scheme().org_version
            con.execute(
                "INSERT INTO voucher(voucher_id, equipment_id, dept_id, org_version, "
                "spend_idem_key, request_no, spend_amount, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    voucher_id,
                    equipment_id,
                    eq.dept_id,
                    org_version,
                    spend_idem_key,
                    spend.request_no,
                    spend.amount,
                    _now(),
                ),
            )
            con.executemany(
                "INSERT INTO voucher_line(voucher_id, line_no, kind, part_no, amount, "
                "hours_json) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        voucher_id,
                        no,
                        line.kind,
                        line.part_no,
                        line.amount,
                        _canonical(line.hours) if line.hours is not None else None,
                    )
                    for no, line in enumerate(lines, start=1)
                ],
            )
            self._bump_generation_locked(con, equipment_id)
            con.commit()
        return self.get_voucher(voucher_id)

    def correct_install_hours(
        self, voucher_id: str, line_no: int, hours: Mapping[str, int]
    ) -> None:
        """更正一条安装费分录的工时；退役件涉及的底单不得再改动。"""
        self._check_hours(hours)
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT equipment_id FROM voucher WHERE voucher_id = ?", (voucher_id,)
            ).fetchone()
            if row is None:
                raise EquipmentNotFound(f"底单 {voucher_id} 不存在")
            equipment_id = row[0]
            self._assert_unlocked_locked(con, equipment_id)
            line = con.execute(
                "SELECT kind FROM voucher_line WHERE voucher_id = ? AND line_no = ?",
                (voucher_id, line_no),
            ).fetchone()
            if line is None:
                raise EquipmentNotFound(f"底单 {voucher_id} 没有第 {line_no} 行")
            if line[0] != "INSTALL":
                raise ValueError(f"底单 {voucher_id} 第 {line_no} 行不是安装费分录")
            # 退役件原值是历史事实：本底单涉及的新旧部件都不得已退役。
            affected = set(hours)
            for r in con.execute(
                "SELECT kind, part_no, hours_json FROM voucher_line WHERE voucher_id = ?",
                (voucher_id,),
            ).fetchall():
                affected.update(self._parts_of_row(r[0], r[1], r[2]))
            for part_no in affected:
                self._assert_part_not_retired_locked(con, equipment_id, part_no)
            con.execute(
                "UPDATE voucher_line SET hours_json = ? "
                "WHERE voucher_id = ? AND line_no = ?",
                (_canonical(dict(hours)), voucher_id, line_no),
            )
            self._bump_generation_locked(con, equipment_id)
            con.commit()

    def get_voucher(self, voucher_id: str) -> VoucherInfo:
        with self._connect() as con:
            return self._load_voucher_locked(con, voucher_id)

    def vouchers_of(self, equipment_id: str) -> tuple[VoucherInfo, ...]:
        with self._connect() as con:
            self._equipment_locked(con, equipment_id)
            rows = con.execute(
                "SELECT voucher_id FROM voucher WHERE equipment_id = ? "
                "ORDER BY voucher_id",
                (equipment_id,),
            ).fetchall()
            return tuple(self._load_voucher_locked(con, r[0]) for r in rows)

    # ---------------------------------------------------------------- 原值封存

    def seal_original_value(self, equipment_id: str) -> dict[str, int]:
        """封存设备原值：重验三条红线并快照每部件原值。

        红线：实付事件与所属承诺必须真实；一笔实付全库只领一次（重领
        拒封）；每张底单各来源之和等于实付原件（漏分 / 超分拒封）；
        实付所属叶须与设备归属叶一致（跨分部拿错拒封）。会计无需等待
        工坊提出换件即可独立封存。
        """
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            eq = self._equipment_locked(con, equipment_id)
            self._assert_unlocked_locked(con, equipment_id)
            vouchers = con.execute(
                "SELECT voucher_id, dept_id, spend_idem_key, request_no "
                "FROM voucher WHERE equipment_id = ? ORDER BY voucher_id",
                (equipment_id,),
            ).fetchall()
            values: dict[str, int] = {}
            seen_spends: set[str] = set()
            for voucher_id, dept_id, idem_key, request_no in vouchers:
                spend = self.budget.spend_event(idem_key)
                if spend is None:
                    raise SpendEventNotFound(f"实付事件 {idem_key} 不存在")
                try:
                    self.budget.get_commitment(spend.request_no)
                except CommitmentNotFound:
                    raise MissingCommitment(
                        f"实付事件 {idem_key} 所属承诺 {spend.request_no} 不存在"
                    ) from None
                if spend.request_no != request_no:
                    raise MissingCommitment(
                        f"底单 {voucher_id} 记录的承诺 {request_no} 与实付不符"
                    )
                if idem_key in seen_spends:
                    raise DuplicateClaim(f"实付事件 {idem_key} 被本设备重复认领")
                seen_spends.add(idem_key)
                # 跨分部拿错实付原件：实付所属叶必须就是设备归属叶。
                if spend.dept_id != dept_id or spend.dept_id != eq.dept_id:
                    raise CrossDivisionClaim(
                        f"底单 {voucher_id} 认领的实付属于叶 {spend.dept_id}，"
                        f"设备归属叶是 {eq.dept_id}"
                    )
                lines = con.execute(
                    "SELECT kind, part_no, amount, hours_json FROM voucher_line "
                    "WHERE voucher_id = ? ORDER BY line_no",
                    (voucher_id,),
                ).fetchall()
                total = 0
                for kind, part_no, amount, hours_json in lines:
                    total += amount
                    if kind == "DIRECT":
                        values[part_no] = values.get(part_no, 0) + amount
                    else:
                        shares = split_installation_fee(
                            amount,
                            [HourlyPart(p, h) for p, h in json.loads(hours_json).items()],
                        )
                        for part, share in shares.items():
                            values[part] = values.get(part, 0) + share
                # 各来源之和须等于实付原件：漏分与超分都拒封。
                if total != spend.amount:
                    raise AllocationMismatch(
                        f"底单 {voucher_id} 各来源之和 {total} 分 ≠ "
                        f"实付原件 {spend.amount} 分"
                    )
            # 重领的全库复核：同一实付不得出现在别的设备的底单上。
            for idem_key in seen_spends:
                others = con.execute(
                    "SELECT COUNT(*) FROM voucher WHERE spend_idem_key = ?",
                    (idem_key,),
                ).fetchone()[0]
                if others > 1:
                    raise DuplicateClaim(f"实付事件 {idem_key} 被多张底单重复认领")
            # 未知部件号落为在库备件；退役件号永远不得再领用。
            for part_no in values:
                row = con.execute(
                    "SELECT state FROM part WHERE equipment_id = ? AND part_no = ?",
                    (equipment_id, part_no),
                ).fetchone()
                if row is None:
                    con.execute(
                        "INSERT INTO part(equipment_id, part_no, state) "
                        "VALUES (?, ?, ?)",
                        (equipment_id, part_no, _STOCKED),
                    )
                elif row[0] == _RETIRED:
                    raise PartAlreadyRetired(
                        f"部件 {part_no} 已退役，底单不得再向其分摊"
                    )
            generation = eq.voucher_generation
            con.execute("DELETE FROM seal WHERE equipment_id = ?", (equipment_id,))
            con.executemany(
                "INSERT INTO seal(equipment_id, part_no, value, generation) "
                "VALUES (?, ?, ?, ?)",
                [
                    (equipment_id, p, v, generation)
                    for p, v in sorted(values.items())
                ],
            )
            con.execute(
                "UPDATE equipment SET sealed_generation = ? WHERE equipment_id = ?",
                (generation, equipment_id),
            )
            con.commit()
        return dict(sorted(values.items()))

    def original_values(self, equipment_id: str) -> dict[str, int]:
        """核原值：读取封存原值；底单在封存后又有变动则报封存过期。"""
        with self._connect() as con:
            eq = self._equipment_locked(con, equipment_id)
            self._assert_seal_fresh_locked(con, eq)
            rows = con.execute(
                "SELECT part_no, value FROM seal WHERE equipment_id = ? "
                "ORDER BY part_no",
                (equipment_id,),
            ).fetchall()
            return {p: v for p, v in rows}

    def part_money_trail(
        self, equipment_id: str, part_no: str
    ) -> tuple[MoneyTrailEntry, ...]:
        """查一项部件分到了哪些钱：逐分追查回实付事件、承诺与归属叶。"""
        with self._connect() as con:
            self._equipment_locked(con, equipment_id)
            entries: list[MoneyTrailEntry] = []
            vouchers = con.execute(
                "SELECT voucher_id, dept_id, org_version, spend_idem_key, request_no "
                "FROM voucher WHERE equipment_id = ? ORDER BY rowid",
                (equipment_id,),
            ).fetchall()
            for voucher_id, dept_id, org_version, idem_key, request_no in vouchers:
                lines = con.execute(
                    "SELECT kind, part_no, amount, hours_json FROM voucher_line "
                    "WHERE voucher_id = ? ORDER BY line_no",
                    (voucher_id,),
                ).fetchall()
                for kind, p, amount, hours_json in lines:
                    if kind == "DIRECT" and p == part_no:
                        entries.append(
                            MoneyTrailEntry(
                                voucher_id, idem_key, request_no, dept_id,
                                org_version, kind, amount,
                            )
                        )
                    elif kind == "INSTALL":
                        shares = split_installation_fee(
                            amount,
                            [HourlyPart(x, h) for x, h in json.loads(hours_json).items()],
                        )
                        if part_no in shares:
                            entries.append(
                                MoneyTrailEntry(
                                    voucher_id, idem_key, request_no, dept_id,
                                    org_version, kind, shares[part_no],
                                )
                            )
            return tuple(entries)

    # ---------------------------------------------------------------- 部件账龄

    def part_status(self, equipment_id: str, part_no: str) -> PartStatus:
        """部件现状：在机 / 在库 / 退役，以及按当前期间算的账面值。"""
        with self._connect() as con:
            eq = self._equipment_locked(con, equipment_id)
            row = con.execute(
                "SELECT state, install_period, zero_periods FROM part "
                "WHERE equipment_id = ? AND part_no = ?",
                (equipment_id, part_no),
            ).fetchone()
            if row is None:
                raise PartNotFound(f"设备 {equipment_id} 没有部件 {part_no}")
            state, install_period, zero_periods = row
            seal_row = con.execute(
                "SELECT value FROM seal WHERE equipment_id = ? AND part_no = ?",
                (equipment_id, part_no),
            ).fetchone()
            value = int(seal_row[0]) if seal_row is not None else None
            fresh = (
                eq.sealed_generation is not None
                and eq.sealed_generation == eq.voucher_generation
            )
            current = int(
                con.execute(
                    "SELECT value FROM meta WHERE key = 'current_period'"
                ).fetchone()[0]
            )
            book: Optional[int] = None
            retired_tx: Optional[str] = None
            if state == _INSTALLED and value is not None:
                book = value - accumulated_depreciation(
                    value, current - install_period, zero_periods
                )
            elif state == _STOCKED:
                book = value
            elif state == _RETIRED:
                book = 0
                tx = con.execute(
                    "SELECT rp.tx_id FROM replacement_part rp "
                    "JOIN replacement r ON r.tx_id = rp.tx_id "
                    "WHERE r.equipment_id = ? AND rp.role = 'RETIRED' "
                    "AND rp.part_no = ?",
                    (equipment_id, part_no),
                ).fetchone()
                retired_tx = tx[0] if tx else None
            return PartStatus(
                part_no=part_no,
                state=state,
                install_period=install_period,
                zero_periods=zero_periods,
                sealed_value=value,
                seal_fresh=fresh,
                book_value=book,
                retired_tx=retired_tx,
            )

    def book_value(self, equipment_id: str, part_no: str) -> int:
        """部件当前账面值（在机件按已用期数折旧，在库件不折旧）。"""
        status = self.part_status(equipment_id, part_no)
        if status.book_value is None:
            raise NotSealed(f"部件 {part_no} 没有封存原值")
        return status.book_value

    # ---------------------------------------------------------------- 限额与拨付

    def set_loss_limit(self, dept_id: str, period: int, limit: int) -> None:
        """登记某工坊某期的报废损失总限额（不得低于已确认 + 在途）。"""
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValueError("限额必须是非负整数分")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            confirmed = self._confirmed_loss_locked(con, dept_id, period)
            outstanding = self._outstanding_locked(con, dept_id, period)
            if limit < confirmed + outstanding:
                raise LimitBelowObligations(
                    f"限额 {limit} 分低于已确认损失 {confirmed} 分 + "
                    f"在途拨付 {outstanding} 分"
                )
            con.execute(
                "INSERT INTO loss_limit(dept_id, period, limit_amount) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(dept_id, period) DO UPDATE SET limit_amount = ?",
                (dept_id, period, limit, limit),
            )
            con.commit()

    def loss_allowance(self, dept_id: str, period: int) -> LossAllowance:
        with self._connect() as con:
            row = con.execute(
                "SELECT limit_amount FROM loss_limit WHERE dept_id = ? AND period = ?",
                (dept_id, period),
            ).fetchone()
            limit = int(row[0]) if row else None
            confirmed = self._confirmed_loss_locked(con, dept_id, period)
            outstanding = self._outstanding_locked(con, dept_id, period)
            return LossAllowance(
                dept_id=dept_id,
                period=period,
                limit=limit,
                confirmed=confirmed,
                outstanding=outstanding,
                available=None if limit is None else limit - confirmed - outstanding,
            )

    def disburse_to_cabinet(
        self,
        disbursement_id: str,
        dept_id: str,
        equipment_ids: list[str] | tuple[str, ...],
        cabinet_id: str,
        amount: int,
    ) -> DisbursementInfo:
        """把本期限额中的一笔实际额度独占拨给一台离线柜。

        拨付成功即独占：这笔钱在柜上交还前不得再拨给另一台设备；
        受益设备在拨付期间账务锁定。``disbursement_id`` 是幂等键。
        """
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise ValueError("拨付金额必须是正整数分")
        equipment_ids = tuple(sorted(set(equipment_ids)))
        if not equipment_ids:
            raise ValueError("拨付至少要受益一台设备")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute(
                "SELECT dept_id, cabinet_id, amount FROM disbursement "
                "WHERE disbursement_id = ?",
                (disbursement_id,),
            ).fetchone()
            if existing is not None:
                info = self._load_disbursement_locked(con, disbursement_id)
                if (
                    info.dept_id != dept_id
                    or info.cabinet_id != cabinet_id
                    or info.amount != amount
                    or info.equipment_ids != equipment_ids
                ):
                    raise EquipmentIdempotencyConflict(
                        f"拨付单号 {disbursement_id} 已用于另一笔拨付"
                    )
                con.commit()
                return info
            period = int(
                con.execute(
                    "SELECT value FROM meta WHERE key = 'current_period'"
                ).fetchone()[0]
            )
            limit_row = con.execute(
                "SELECT limit_amount FROM loss_limit WHERE dept_id = ? AND period = ?",
                (dept_id, period),
            ).fetchone()
            if limit_row is None:
                raise NoLossLimit(f"工坊 {dept_id} 第 {period} 期尚未登记报废损失限额")
            available = (
                limit_row[0]
                - self._confirmed_loss_locked(con, dept_id, period)
                - self._outstanding_locked(con, dept_id, period)
            )
            if amount > available:
                raise InsufficientLossAllowance(
                    f"工坊 {dept_id} 第 {period} 期限额可用 {available} 分，"
                    f"申请拨付 {amount} 分"
                )
            for equipment_id in equipment_ids:
                self._equipment_locked(con, equipment_id)
                self._assert_unlocked_locked(con, equipment_id)
            con.execute(
                "INSERT INTO disbursement(disbursement_id, dept_id, period, "
                "cabinet_id, amount, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (disbursement_id, dept_id, period, cabinet_id, amount, _OPEN, _now()),
            )
            con.executemany(
                "INSERT INTO disbursement_device(disbursement_id, equipment_id) "
                "VALUES (?, ?)",
                [(disbursement_id, e) for e in equipment_ids],
            )
            con.commit()
        return self.get_disbursement(disbursement_id)

    def get_disbursement(self, disbursement_id: str) -> DisbursementInfo:
        with self._connect() as con:
            return self._load_disbursement_locked(con, disbursement_id)

    def export_disbursement(self, disbursement_id: str) -> dict:
        """导出拨付单与受益设备快照（柜上领单的一刻是在线的）。"""
        with self._connect() as con:
            info = self._load_disbursement_locked(con, disbursement_id)
            if info.state == _CLOSED:
                raise DisbursementClosed(f"拨付单 {disbursement_id} 已交还结案")
            devices: dict[str, dict] = {}
            for equipment_id in info.equipment_ids:
                eq = self._equipment_locked(con, equipment_id)
                self._assert_seal_fresh_locked(con, eq)
                seal_rows = dict(
                    con.execute(
                        "SELECT part_no, value FROM seal WHERE equipment_id = ?",
                        (equipment_id,),
                    ).fetchall()
                )
                parts = {}
                for p, state, install_period, zero in con.execute(
                    "SELECT part_no, state, install_period, zero_periods FROM part "
                    "WHERE equipment_id = ? AND state IN (?, ?) ORDER BY part_no",
                    (equipment_id, _INSTALLED, _STOCKED),
                ).fetchall():
                    parts[p] = {
                        "state": state,
                        "install_period": install_period,
                        "zero_periods": zero,
                        "value": int(seal_rows.get(p, 0)),
                    }
                devices[equipment_id] = {"dept_id": eq.dept_id, "parts": parts}
            return {
                "disbursement_id": info.disbursement_id,
                "cabinet_id": info.cabinet_id,
                "dept_id": info.dept_id,
                "period": info.period,
                "amount": info.amount,
                "devices": devices,
            }

    def import_return(self, manifest: Mapping) -> tuple[str, ...]:
        """导入柜上交还的回执：逐笔核对后落拆装交易并释放余款。

        中心按自己的封存账重算每件旧件的账面值与损失，与回执不符即
        整单拒绝；同一回执重复导入是幂等的（内容不一致报冲突）。
        """
        disbursement_id = manifest.get("disbursement_id")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT dept_id, period, cabinet_id, amount, state, manifest_json "
                "FROM disbursement WHERE disbursement_id = ?",
                (disbursement_id,),
            ).fetchone()
            if row is None:
                raise DisbursementNotFound(f"拨付单 {disbursement_id} 不存在")
            dept_id, period, cabinet_id, amount, state, stored = row
            if state == _CLOSED:
                if stored != _canonical(manifest):
                    raise EquipmentIdempotencyConflict(
                        f"拨付单 {disbursement_id} 已结案，回执内容与留档不符"
                    )
                tx_ids = tuple(
                    r[0]
                    for r in con.execute(
                        "SELECT tx_id FROM replacement WHERE disbursement_id = ? "
                        "ORDER BY rowid",
                        (disbursement_id,),
                    ).fetchall()
                )
                con.commit()
                return tx_ids
            if manifest.get("cabinet_id") != cabinet_id:
                raise ManifestInvalid(
                    f"回执来自柜 {manifest.get('cabinet_id')}，"
                    f"拨付单属于柜 {cabinet_id}"
                )
            earmarked = {
                r[0]
                for r in con.execute(
                    "SELECT equipment_id FROM disbursement_device "
                    "WHERE disbursement_id = ?",
                    (disbursement_id,),
                ).fetchall()
            }
            settlements = manifest.get("settlements", [])
            remainder = manifest.get("remainder")
            if not isinstance(remainder, int) or remainder < 0:
                raise ManifestInvalid("回执余款必须是非负整数分")
            total_loss = 0
            for s in settlements:
                if s.get("equipment_id") not in earmarked:
                    raise ManifestInvalid(
                        f"回执中设备 {s.get('equipment_id')} 不在拨付单受益范围内"
                    )
                total_loss += self._check_int(s.get("loss"), "结算损失")
            if total_loss + remainder != amount:
                raise ManifestInvalid(
                    f"回执损失合计 {total_loss} 分 + 余款 {remainder} 分 ≠ "
                    f"拨付 {amount} 分"
                )
            tx_ids: list[str] = []
            for s in settlements:
                tx_ids.append(
                    self._apply_settlement_locked(
                        con, disbursement_id, dept_id, period, s
                    )
                )
            con.execute(
                "UPDATE disbursement SET state = ?, manifest_json = ?, closed_at = ? "
                "WHERE disbursement_id = ?",
                (_CLOSED, _canonical(manifest), _now(), disbursement_id),
            )
            con.commit()
        return tuple(tx_ids)

    # ---------------------------------------------------------------- 退役查询

    def lookup_installation(
        self, equipment_id: str, retired_part_no: str
    ) -> ReplacementInfo:
        """回单丢失时用退役件的原号查此次到底装了什么。"""
        with self._connect() as con:
            row = con.execute(
                "SELECT r.tx_id FROM replacement r "
                "JOIN replacement_part rp ON rp.tx_id = r.tx_id "
                "WHERE r.equipment_id = ? AND rp.role = 'RETIRED' "
                "AND rp.part_no = ?",
                (equipment_id, retired_part_no),
            ).fetchone()
            if row is None:
                raise PartNotFound(
                    f"设备 {equipment_id} 没有退役件 {retired_part_no} 的拆装交易"
                )
            return self._load_replacement_locked(con, row[0])

    def replacements(self, equipment_id: str) -> tuple[ReplacementInfo, ...]:
        with self._connect() as con:
            self._equipment_locked(con, equipment_id)
            rows = con.execute(
                "SELECT tx_id FROM replacement WHERE equipment_id = ? ORDER BY tx_id",
                (equipment_id,),
            ).fetchall()
            return tuple(self._load_replacement_locked(con, r[0]) for r in rows)

    # ---------------------------------------------------------------- 内部

    @staticmethod
    def _check_zero_periods(zero: object) -> None:
        if not isinstance(zero, int) or isinstance(zero, bool) or zero <= 0:
            raise ValueError(f"归零期数必须是正整数: {zero!r}")

    @staticmethod
    def _check_hours(hours: Mapping[str, int]) -> None:
        if not hours:
            raise ValueError("工时表不能为空")
        for part_no, h in hours.items():
            if not isinstance(h, int) or isinstance(h, bool) or h <= 0:
                raise ValueError(f"部件 {part_no} 的工时必须是正整数: {h!r}")

    @staticmethod
    def _check_int(value: object, name: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ManifestInvalid(f"{name}必须是非负整数分: {value!r}")
        return value

    @staticmethod
    def _line_parts(line: VoucherLine) -> tuple[str, ...]:
        if line.kind == "DIRECT":
            return (line.part_no,)
        return tuple(line.hours or ())

    @staticmethod
    def _parts_of_row(kind: str, part_no: Optional[str], hours_json: Optional[str]):
        if kind == "DIRECT":
            return (part_no,)
        return tuple(json.loads(hours_json))

    def _validate_lines(self, lines: list[VoucherLine]) -> None:
        if not lines:
            raise ValueError("底单至少需要一条分录")
        for line in lines:
            if not isinstance(line.amount, int) or isinstance(line.amount, bool) \
                    or line.amount <= 0:
                raise ValueError(f"分录金额必须是正整数分: {line.amount!r}")
            if line.kind == "DIRECT":
                if not line.part_no:
                    raise ValueError("直接拆分分录必须给出部件号")
            elif line.kind == "INSTALL":
                self._check_hours(line.hours or {})
            else:
                raise ValueError(f"未知分录类型: {line.kind!r}")

    def _equipment_locked(self, con: sqlite3.Connection, equipment_id: str) -> _EquipmentRow:
        row = con.execute(
            "SELECT equipment_id, dept_id, commissioned_period, voucher_generation, "
            "sealed_generation FROM equipment WHERE equipment_id = ?",
            (equipment_id,),
        ).fetchone()
        if row is None:
            raise EquipmentNotFound(f"设备 {equipment_id} 不存在")
        return _EquipmentRow(*row)

    def _assert_unlocked_locked(self, con: sqlite3.Connection, equipment_id: str) -> None:
        row = con.execute(
            "SELECT d.disbursement_id FROM disbursement d "
            "JOIN disbursement_device dd ON dd.disbursement_id = d.disbursement_id "
            "WHERE dd.equipment_id = ? AND d.state = ?",
            (equipment_id, _OPEN),
        ).fetchone()
        if row is not None:
            raise EquipmentLocked(
                f"设备 {equipment_id} 有未交还的独占拨付 {row[0]}，账务被锁定"
            )

    def _assert_part_not_retired_locked(
        self, con: sqlite3.Connection, equipment_id: str, part_no: str
    ) -> None:
        row = con.execute(
            "SELECT state FROM part WHERE equipment_id = ? AND part_no = ?",
            (equipment_id, part_no),
        ).fetchone()
        if row is not None and row[0] == _RETIRED:
            raise PartAlreadyRetired(
                f"部件 {part_no} 已退役，退役件原值是历史事实，不得再改动"
            )

    def _assert_seal_fresh_locked(
        self, con: sqlite3.Connection, eq: _EquipmentRow
    ) -> None:
        if eq.sealed_generation is None:
            raise NotSealed(f"设备 {eq.equipment_id} 尚未封存原值")
        if eq.sealed_generation != eq.voucher_generation:
            raise SealStale(
                f"设备 {eq.equipment_id} 的底单在封存后又有变动，须重新封存"
            )

    def _bump_generation_locked(self, con: sqlite3.Connection, equipment_id: str) -> None:
        con.execute(
            "UPDATE equipment SET voucher_generation = voucher_generation + 1 "
            "WHERE equipment_id = ?",
            (equipment_id,),
        )

    def _load_voucher_locked(self, con: sqlite3.Connection, voucher_id: str) -> VoucherInfo:
        row = con.execute(
            "SELECT equipment_id, dept_id, org_version, spend_idem_key, request_no, "
            "spend_amount FROM voucher WHERE voucher_id = ?",
            (voucher_id,),
        ).fetchone()
        if row is None:
            raise EquipmentNotFound(f"底单 {voucher_id} 不存在")
        lines = tuple(
            VoucherLine(
                kind=kind,
                amount=amount,
                part_no=part_no,
                hours=json.loads(hours_json) if hours_json is not None else None,
            )
            for kind, part_no, amount, hours_json in con.execute(
                "SELECT kind, part_no, amount, hours_json FROM voucher_line "
                "WHERE voucher_id = ? ORDER BY line_no",
                (voucher_id,),
            ).fetchall()
        )
        return VoucherInfo(
            voucher_id=voucher_id,
            equipment_id=row[0],
            dept_id=row[1],
            org_version=row[2],
            spend_idem_key=row[3],
            request_no=row[4],
            spend_amount=row[5],
            lines=lines,
        )

    def _confirmed_loss_locked(self, con: sqlite3.Connection, dept_id: str, period: int) -> int:
        return int(
            con.execute(
                "SELECT COALESCE(SUM(loss), 0) FROM replacement "
                "WHERE dept_id = ? AND period = ?",
                (dept_id, period),
            ).fetchone()[0]
        )

    def _outstanding_locked(self, con: sqlite3.Connection, dept_id: str, period: int) -> int:
        return int(
            con.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM disbursement "
                "WHERE dept_id = ? AND period = ? AND state = ?",
                (dept_id, period, _OPEN),
            ).fetchone()[0]
        )

    def _load_disbursement_locked(
        self, con: sqlite3.Connection, disbursement_id: str
    ) -> DisbursementInfo:
        row = con.execute(
            "SELECT dept_id, period, cabinet_id, amount, state FROM disbursement "
            "WHERE disbursement_id = ?",
            (disbursement_id,),
        ).fetchone()
        if row is None:
            raise DisbursementNotFound(f"拨付单 {disbursement_id} 不存在")
        devices = tuple(
            r[0]
            for r in con.execute(
                "SELECT equipment_id FROM disbursement_device "
                "WHERE disbursement_id = ? ORDER BY equipment_id",
                (disbursement_id,),
            ).fetchall()
        )
        return DisbursementInfo(
            disbursement_id=disbursement_id,
            dept_id=row[0],
            period=row[1],
            cabinet_id=row[2],
            amount=row[3],
            state=row[4],
            equipment_ids=devices,
        )

    def _apply_settlement_locked(
        self,
        con: sqlite3.Connection,
        disbursement_id: str,
        dept_id: str,
        period: int,
        settlement: Mapping,
    ) -> str:
        equipment_id = settlement["equipment_id"]
        order_id = settlement.get("order_id")
        if not order_id:
            raise ManifestInvalid("结算记录缺少拆单号")
        if not settlement.get("retired"):
            raise ManifestInvalid(f"结算 {order_id} 没有退役件，不是有效拆装")
        tx_id = f"{disbursement_id}#{order_id}"
        eq = self._equipment_locked(con, equipment_id)
        if eq.dept_id != dept_id:
            raise ManifestInvalid(
                f"设备 {equipment_id} 归属叶 {eq.dept_id} 与拨付工坊 {dept_id} 不符"
            )
        seal_rows = dict(
            con.execute(
                "SELECT part_no, value FROM seal WHERE equipment_id = ?",
                (equipment_id,),
            ).fetchall()
        )
        retired: list[RetiredPartInfo] = []
        for r in settlement.get("retired", []):
            part_no = r.get("part_no")
            part = con.execute(
                "SELECT state, install_period, zero_periods FROM part "
                "WHERE equipment_id = ? AND part_no = ?",
                (equipment_id, part_no),
            ).fetchone()
            if part is None or part[0] != _INSTALLED:
                raise ManifestInvalid(f"旧件 {part_no} 不在设备 {equipment_id} 机上")
            value = int(seal_rows.get(part_no, 0))
            book = value - accumulated_depreciation(
                value, period - part[1], part[2]
            )
            salvage = self._check_int(r.get("salvage"), "残件价款")
            loss = max(0, book - salvage)
            if book != r.get("book_value") or loss != r.get("loss"):
                raise ManifestInvalid(
                    f"旧件 {part_no} 回执账面 {r.get('book_value')} / 损失 "
                    f"{r.get('loss')} 与中心重算 {book} / {loss} 不符"
                )
            retired.append(RetiredPartInfo(part_no, book, salvage, loss))
        if sum(r.loss for r in retired) != settlement.get("loss"):
            raise ManifestInvalid(f"结算 {order_id} 的损失合计与分件明细不符")
        installed: list[InstalledPartInfo] = []
        for i in settlement.get("installed", []):
            part_no = i.get("part_no")
            part = con.execute(
                "SELECT state FROM part WHERE equipment_id = ? AND part_no = ?",
                (equipment_id, part_no),
            ).fetchone()
            if part is None or part[0] != _STOCKED:
                raise ManifestInvalid(f"新件 {part_no} 不是设备 {equipment_id} 的在库备件")
            value = int(seal_rows.get(part_no, 0))
            if value != i.get("value"):
                raise ManifestInvalid(
                    f"新件 {part_no} 回执原值 {i.get('value')} 与中心封存 {value} 不符"
                )
            zero = i.get("zero_periods")
            try:
                self._check_zero_periods(zero)
            except ValueError as exc:
                raise ManifestInvalid(str(exc)) from None
            installed.append(InstalledPartInfo(part_no, zero, value))
        for r in retired:
            con.execute(
                "UPDATE part SET state = ? WHERE equipment_id = ? AND part_no = ?",
                (_RETIRED, equipment_id, r.part_no),
            )
        for i in installed:
            con.execute(
                "UPDATE part SET state = ?, install_period = ?, zero_periods = ? "
                "WHERE equipment_id = ? AND part_no = ?",
                (_INSTALLED, period, i.zero_periods, equipment_id, i.part_no),
            )
        con.execute(
            "INSERT INTO replacement(tx_id, equipment_id, dept_id, period, "
            "disbursement_id, order_id, loss, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tx_id,
                equipment_id,
                dept_id,
                period,
                disbursement_id,
                order_id,
                settlement["loss"],
                _now(),
            ),
        )
        for r in retired:
            con.execute(
                "INSERT INTO replacement_part(tx_id, role, part_no, book_value, "
                "salvage, loss) VALUES (?, 'RETIRED', ?, ?, ?, ?)",
                (tx_id, r.part_no, r.book_value, r.salvage, r.loss),
            )
        for i in installed:
            con.execute(
                "INSERT INTO replacement_part(tx_id, role, part_no, zero_periods, "
                "value) VALUES (?, 'INSTALLED', ?, ?, ?)",
                (tx_id, i.part_no, i.zero_periods, i.value),
            )
        return tx_id

    def _load_replacement_locked(
        self, con: sqlite3.Connection, tx_id: str
    ) -> ReplacementInfo:
        row = con.execute(
            "SELECT equipment_id, dept_id, period, disbursement_id, order_id, loss "
            "FROM replacement WHERE tx_id = ?",
            (tx_id,),
        ).fetchone()
        if row is None:
            raise PartNotFound(f"拆装交易 {tx_id} 不存在")
        retired: list[RetiredPartInfo] = []
        installed: list[InstalledPartInfo] = []
        for role, part_no, book, salvage, loss, zero, value in con.execute(
            "SELECT role, part_no, book_value, salvage, loss, zero_periods, value "
            "FROM replacement_part WHERE tx_id = ? ORDER BY part_no",
            (tx_id,),
        ).fetchall():
            if role == "RETIRED":
                retired.append(RetiredPartInfo(part_no, book, salvage, loss))
            else:
                installed.append(InstalledPartInfo(part_no, zero, value))
        return ReplacementInfo(
            tx_id=tx_id,
            equipment_id=row[0],
            dept_id=row[1],
            period=row[2],
            disbursement_id=row[3],
            order_id=row[4],
            loss=row[5],
            retired=tuple(retired),
            installed=tuple(installed),
        )
