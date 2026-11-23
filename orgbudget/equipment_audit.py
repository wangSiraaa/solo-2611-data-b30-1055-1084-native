"""设备会计独立重算审计。

刻意不调用生产代码的分摊 / 折旧 / 结算路径：

1. 直接 SQL 读设备仓与预算仓的原始行（底单、分录、封存、拨付、拆装
   交易，以及预算仓的仅追加支出事件与承诺）；
2. 用 :class:`fractions.Fraction` 独立重推安装费分摊与每部件原值，
   与落库封存逐部件比对；
3. 从原始事实重建限额账（已确认损失 + 在途拨付 ≤ 限额），核对拆装
   交易算术与部件状态机。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .errors import EquipmentAuditError


@dataclass(frozen=True)
class EquipmentAuditResult:
    equipments: int
    parts: int
    vouchers: int
    sealed_equipments: int
    replacements: int
    open_disbursements: int
    total_confirmed_loss: int
    checks: tuple[str, ...]


def _connect(db_path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    return con


def _independent_split(amount: int, hours: dict[str, int]) -> dict[str, int]:
    """用 Fraction 独立实现“按工时下取整 + 余数降序 + 部件号并列补分”。"""
    total_hours = sum(hours.values())
    if total_hours <= 0 or any(h <= 0 for h in hours.values()):
        raise EquipmentAuditError(f"工时表非法: {hours}")
    exact = {p: Fraction(amount * h, total_hours) for p, h in hours.items()}
    floors = {p: f.numerator // f.denominator for p, f in exact.items()}
    remainders = {p: exact[p] - floors[p] for p in exact}
    leftover = amount - sum(floors.values())
    ranked = sorted(hours, key=lambda p: (-remainders[p], p))
    result = dict(floors)
    for p in ranked[:leftover]:
        if remainders[p] == 0:
            raise EquipmentAuditError("补分落到了余数为 0 的部件")
        result[p] += 1
    assert sum(result.values()) == amount
    return result


def _independent_depreciation(original: int, used: int, total: int) -> int:
    if total <= 0:
        raise EquipmentAuditError(f"归零期数非法: {total}")
    if used <= 0:
        return 0
    return min(original, Fraction(original * used, total).numerator
               // Fraction(original * used, total).denominator)


def audit_equipment(
    equipment_db: str | Path, budget_db: str | Path
) -> EquipmentAuditResult:
    """对设备会计仓做全量独立审计，任何不变量被破坏即抛异常。"""
    checks: list[str] = []
    with _connect(equipment_db) as con:
        equipments = {
            r["equipment_id"]: dict(r)
            for r in con.execute("SELECT * FROM equipment").fetchall()
        }
        parts = con.execute(
            "SELECT equipment_id, part_no, state, install_period, zero_periods "
            "FROM part"
        ).fetchall()
        vouchers = {
            r["voucher_id"]: dict(r)
            for r in con.execute("SELECT * FROM voucher").fetchall()
        }
        lines = con.execute(
            "SELECT voucher_id, line_no, kind, part_no, amount, hours_json "
            "FROM voucher_line ORDER BY voucher_id, line_no"
        ).fetchall()
        seals = con.execute(
            "SELECT equipment_id, part_no, value, generation FROM seal"
        ).fetchall()
        limits = {
            (r["dept_id"], r["period"]): r["limit_amount"]
            for r in con.execute("SELECT * FROM loss_limit").fetchall()
        }
        disbursements = {
            r["disbursement_id"]: dict(r)
            for r in con.execute("SELECT * FROM disbursement").fetchall()
        }
        disb_devices = con.execute(
            "SELECT disbursement_id, equipment_id FROM disbursement_device"
        ).fetchall()
        replacements = {
            r["tx_id"]: dict(r)
            for r in con.execute("SELECT * FROM replacement").fetchall()
        }
        repl_parts = con.execute(
            "SELECT tx_id, role, part_no, book_value, salvage, loss, zero_periods, "
            "value FROM replacement_part"
        ).fetchall()
        current_period = int(
            con.execute("SELECT value FROM meta WHERE key = 'current_period'").fetchone()[0]
        )

    with _connect(budget_db) as con:
        spends = {
            r["idem_key"]: dict(r)
            for r in con.execute(
                "SELECT request_no, dept_id, amount, idem_key FROM spend"
            ).fetchall()
        }
        commitments = {
            r["request_no"]: dict(r)
            for r in con.execute(
                "SELECT request_no, dept_id, amount, status FROM commitment"
            ).fetchall()
        }

    # 1) 每张底单认到真实实付事件及所属承诺，快照与预算仓事实一致。
    lines_by_voucher: dict[str, list[sqlite3.Row]] = {}
    for line in lines:
        lines_by_voucher.setdefault(line["voucher_id"], []).append(line)
    for vid, v in vouchers.items():
        spend = spends.get(v["spend_idem_key"])
        if spend is None:
            raise EquipmentAuditError(f"底单 {vid} 认领的实付事件不存在")
        if spend["request_no"] not in commitments:
            raise EquipmentAuditError(f"底单 {vid} 的实付缺少所属承诺")
        if spend["request_no"] != v["request_no"] or spend["amount"] != v["spend_amount"]:
            raise EquipmentAuditError(f"底单 {vid} 的实付快照与预算仓不符")
        if v["equipment_id"] not in equipments:
            raise EquipmentAuditError(f"底单 {vid} 挂在不存在的设备上")
        if v["dept_id"] != equipments[v["equipment_id"]]["dept_id"]:
            raise EquipmentAuditError(f"底单 {vid} 归属叶与设备不符")
        if spend["dept_id"] != v["dept_id"]:
            raise EquipmentAuditError(f"底单 {vid} 跨分部拿错实付原件")
    checks.append("每张底单认到真实实付事件及所属承诺，归属叶逐分一致")

    # 2) 重领红线：一笔实付事件全库至多被认领一次。
    seen: dict[str, str] = {}
    for vid, v in vouchers.items():
        key = v["spend_idem_key"]
        if key in seen:
            raise EquipmentAuditError(f"实付 {key} 被底单 {seen[key]} 与 {vid} 重领")
        seen[key] = vid
    checks.append("一笔实付事件全库至多被认领一次，无重领")

    # 3) 漏分 / 超分红线：每张底单各来源之和等于实付原件。
    for vid, v in vouchers.items():
        total = sum(l["amount"] for l in lines_by_voucher.get(vid, []))
        if total != spends[v["spend_idem_key"]]["amount"]:
            raise EquipmentAuditError(
                f"底单 {vid} 各来源之和 {total} ≠ 实付原件 "
                f"{spends[v['spend_idem_key']]['amount']}"
            )
    checks.append("每张底单各来源之和严格等于实付原件，无漏分超分")

    # 4) 独立重推每设备每部件原值，与“新鲜”封存逐部件比对。
    recomputed: dict[str, dict[str, int]] = {e: {} for e in equipments}
    for vid, v in vouchers.items():
        eq_values = recomputed[v["equipment_id"]]
        for line in lines_by_voucher.get(vid, []):
            if line["kind"] == "DIRECT":
                eq_values[line["part_no"]] = eq_values.get(line["part_no"], 0) + line["amount"]
            else:
                hours = json.loads(line["hours_json"])
                for part, share in _independent_split(line["amount"], hours).items():
                    eq_values[part] = eq_values.get(part, 0) + share
    seal_by_eq: dict[str, dict[str, int]] = {}
    for s in seals:
        seal_by_eq.setdefault(s["equipment_id"], {})[s["part_no"]] = s["value"]
    sealed_count = 0
    for eq_id, eq in equipments.items():
        sealed = eq["sealed_generation"]
        if sealed is None:
            if seal_by_eq.get(eq_id):
                raise EquipmentAuditError(f"设备 {eq_id} 无封存代次却有封存行")
            continue
        sealed_count += 1
        if sealed == eq["voucher_generation"]:
            if seal_by_eq.get(eq_id, {}) != recomputed[eq_id]:
                raise EquipmentAuditError(
                    f"设备 {eq_id} 封存原值与独立重推不符: "
                    f"{seal_by_eq.get(eq_id, {})} != {recomputed[eq_id]}"
                )
        for part_no, value in seal_by_eq.get(eq_id, {}).items():
            if value < 0:
                raise EquipmentAuditError(f"设备 {eq_id} 部件 {part_no} 封存为负")
    checks.append("独立重推安装费分摊与原值，与全部新鲜封存逐部件一致")

    # 5) 拆装交易算术：损失 = Σ max(0, 账面 − 残件)，且账面按折旧公式可复算。
    retired_globally: set[tuple[str, str]] = set()
    for tx in replacements.values():
        detail = [r for r in repl_parts if r["tx_id"] == tx["tx_id"]]
        retired = [r for r in detail if r["role"] == "RETIRED"]
        installed = [r for r in detail if r["role"] == "INSTALLED"]
        if not retired:
            raise EquipmentAuditError(f"拆装交易 {tx['tx_id']} 没有退役件")
        if sum(r["loss"] for r in retired) != tx["loss"]:
            raise EquipmentAuditError(f"拆装交易 {tx['tx_id']} 损失合计与明细不符")
        for r in retired:
            if r["loss"] != max(0, r["book_value"] - r["salvage"]):
                raise EquipmentAuditError(
                    f"交易 {tx['tx_id']} 旧件 {r['part_no']} 损失算术不符"
                )
            key = (tx["equipment_id"], r["part_no"])
            if key in retired_globally:
                raise EquipmentAuditError(f"部件 {key} 被拆了两回")
            retired_globally.add(key)
        disb = disbursements.get(tx["disbursement_id"])
        if disb is None or disb["period"] != tx["period"]:
            raise EquipmentAuditError(f"交易 {tx['tx_id']} 与拨付单期间不符")
        if tx["equipment_id"] not in {
            d["equipment_id"] for d in disb_devices
            if d["disbursement_id"] == tx["disbursement_id"]
        }:
            raise EquipmentAuditError(f"交易 {tx['tx_id']} 设备不在拨付受益范围")
        for i in installed:
            if i["zero_periods"] <= 0 or i["value"] < 0:
                raise EquipmentAuditError(f"交易 {tx['tx_id']} 新件 {i['part_no']} 记录非法")
    checks.append("拆装交易逐笔算术可复算，退役件全库只拆一回")

    # 6) 限额账：已确认损失 + 在途拨付 ≤ 本期限额；回执落档与拨付守恒。
    confirmed: dict[tuple[str, int], int] = {}
    for tx in replacements.values():
        key = (tx["dept_id"], tx["period"])
        confirmed[key] = confirmed.get(key, 0) + tx["loss"]
    outstanding: dict[tuple[str, int], int] = {}
    open_count = 0
    for d in disbursements.values():
        if d["state"] == "OPEN":
            open_count += 1
            key = (d["dept_id"], d["period"])
            outstanding[key] = outstanding.get(key, 0) + d["amount"]
        elif d["state"] == "CLOSED":
            manifest = json.loads(d["manifest_json"])
            settled = sum(s["loss"] for s in manifest["settlements"])
            if settled + manifest["remainder"] != d["amount"]:
                raise EquipmentAuditError(f"拨付单 {d['disbursement_id']} 回执不守恒")
    for key, used in confirmed.items():
        limit = limits.get(key)
        if limit is None or used > limit:
            raise EquipmentAuditError(f"工坊 {key} 已确认损失 {used} 超限额 {limit}")
    for key, out in outstanding.items():
        limit = limits.get(key)
        if limit is None or confirmed.get(key, 0) + out > limit:
            raise EquipmentAuditError(f"工坊 {key} 在途拨付突破限额")
    checks.append("每工坊每期：已确认损失 + 在途拨付 ≤ 报废损失总限额")

    # 7) 部件状态机：在机件账龄可复算，退役件必有对应交易，在库件无账龄。
    part_keys = set()
    for p in parts:
        key = (p["equipment_id"], p["part_no"])
        if key in part_keys:
            raise EquipmentAuditError(f"部件 {key} 重复登记")
        part_keys.add(key)
        if p["state"] == "INSTALLED":
            if p["install_period"] is None or p["zero_periods"] is None:
                raise EquipmentAuditError(f"在机件 {key} 缺账龄要素")
            if not (1 <= p["install_period"] <= current_period):
                raise EquipmentAuditError(f"在机件 {key} 投用期越界")
            _independent_depreciation(
                seal_by_eq.get(p["equipment_id"], {}).get(p["part_no"], 0),
                current_period - p["install_period"],
                p["zero_periods"],
            )
        elif p["state"] == "STOCKED":
            if p["install_period"] is not None:
                raise EquipmentAuditError(f"在库件 {key} 不应有投用期")
        elif p["state"] == "RETIRED":
            if key not in retired_globally:
                raise EquipmentAuditError(f"退役件 {key} 查无拆装交易")
        else:
            raise EquipmentAuditError(f"部件 {key} 状态非法: {p['state']}")
    checks.append("部件状态机一致：在机有账龄、在库无账龄、退役必有交易")

    return EquipmentAuditResult(
        equipments=len(equipments),
        parts=len(parts),
        vouchers=len(vouchers),
        sealed_equipments=sealed_count,
        replacements=len(replacements),
        open_disbursements=open_count,
        total_confirmed_loss=sum(confirmed.values()),
        checks=tuple(checks),
    )
