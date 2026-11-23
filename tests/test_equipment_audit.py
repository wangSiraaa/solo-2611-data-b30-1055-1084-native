"""设备会计独立审计测试：模糊交叉核对、篡改必被发现。"""

from __future__ import annotations

import random
import sqlite3
import unittest

from orgbudget.equipment import HourlyPart, split_installation_fee
from orgbudget.equipment_audit import _independent_split, audit_equipment
from orgbudget.equipment_service import VoucherLine
from orgbudget.errors import EquipmentAuditError
from orgbudget.local_cabinet import LocalCabinet
from tests.support import EquipmentTestCase


class SplitFuzzTest(unittest.TestCase):
    """生产实现与 Fraction 独立实现在随机参数下完全一致。"""

    def test_fuzz_matches_production(self) -> None:
        rng = random.Random(20261123)
        for _ in range(2_000):
            n = rng.randint(1, 6)
            hours = {f"P{i:02d}": rng.randint(1, 12) for i in range(n)}
            amount = rng.randint(1, 10_000)
            production = split_installation_fee(
                amount, [HourlyPart(p, h) for p, h in hours.items()]
            )
            audit = _independent_split(amount, hours)
            self.assertEqual(production, audit)
            self.assertEqual(sum(production.values()), amount)


class EquipmentAuditTest(EquipmentTestCase):
    def _build_full_scenario(self) -> None:
        """两台设备、三张底单、一次换件、一笔在途拨付。"""
        self.build_workshop()
        self.equipment.register_equipment("M1", "W", 1, {"MOTOR": 3, "BASE": 6})
        for req, sp, part, amount in [
            ("REQ-MOTOR", "SP-MOTOR", "MOTOR", 8),
            ("REQ-BASE", "SP-BASE", "BASE", 4),
        ]:
            self.pay(req, "W", amount, amount, sp)
            self.equipment.post_voucher(
                f"V-{part}", "M1", sp, [VoucherLine.direct(part, amount)]
            )
        self.pay("REQ-INST", "W", 3, 3, "SP-INST")
        self.equipment.post_voucher(
            "V-INST", "M1", "SP-INST",
            [VoucherLine.install(3, {"MOTOR": 1, "BASE": 2})],
        )
        self.pay("REQ-NEW", "W", 7, 7, "SP-NEW")
        self.equipment.post_voucher(
            "V-NEW", "M1", "SP-NEW", [VoucherLine.direct("MOTOR2", 7)]
        )
        self.equipment.seal_original_value("M1")
        self.equipment.advance_period()
        self.equipment.set_loss_limit("W", 2, 8)
        self.equipment.disburse_to_cabinet("D1", "W", ["M1"], "CAB1", 6)
        cab = LocalCabinet(self.tmp / "cab1.db", "CAB1")
        cab.load(self.equipment.export_disbursement("D1"))
        cab.register_order("O1", "M1", {"MOTOR": 0}, {"MOTOR2": 3})
        cab.settle_all()
        self.equipment.import_return(cab.hand_back())
        # 留一笔在途拨付，覆盖 outstanding 校验路径。
        self.equipment.disburse_to_cabinet("D2", "W", ["M1"], "CAB2", 2)

    def test_clean_scenario_passes(self) -> None:
        self._build_full_scenario()
        result = audit_equipment(self.equipment.db_path, self.budget.db_path)
        self.assertEqual(result.equipments, 1)
        self.assertEqual(result.vouchers, 4)
        self.assertEqual(result.replacements, 1)
        self.assertEqual(result.open_disbursements, 1)
        self.assertEqual(result.total_confirmed_loss, 6)
        self.assertEqual(len(result.checks), 7)

    def test_tampered_seal_detected(self) -> None:
        self._build_full_scenario()
        with sqlite3.connect(self.equipment.db_path) as con:
            con.execute(
                "UPDATE seal SET value = value + 1 "
                "WHERE equipment_id = 'M1' AND part_no = 'BASE'"
            )
            con.commit()
        with self.assertRaises(EquipmentAuditError):
            audit_equipment(self.equipment.db_path, self.budget.db_path)

    def test_tampered_replacement_loss_detected(self) -> None:
        self._build_full_scenario()
        with sqlite3.connect(self.equipment.db_path) as con:
            con.execute("UPDATE replacement SET loss = loss + 1")
            con.commit()
        with self.assertRaises(EquipmentAuditError):
            audit_equipment(self.equipment.db_path, self.budget.db_path)

    def test_tampered_voucher_line_detected(self) -> None:
        self._build_full_scenario()
        with sqlite3.connect(self.equipment.db_path) as con:
            con.execute(
                "UPDATE voucher_line SET amount = amount + 1 WHERE voucher_id = 'V-MOTOR'"
            )
            con.commit()
        with self.assertRaises(EquipmentAuditError):
            audit_equipment(self.equipment.db_path, self.budget.db_path)

    def test_tampered_part_state_detected(self) -> None:
        self._build_full_scenario()
        with sqlite3.connect(self.equipment.db_path) as con:
            # 把在库件伪造成退役件：查无拆装交易。
            con.execute(
                "UPDATE part SET state = 'RETIRED' "
                "WHERE equipment_id = 'M1' AND part_no = 'BASE'"
            )
            con.commit()
        with self.assertRaises(EquipmentAuditError):
            audit_equipment(self.equipment.db_path, self.budget.db_path)

    def test_tampered_limit_detected(self) -> None:
        self._build_full_scenario()
        with sqlite3.connect(self.equipment.db_path) as con:
            # 把限额改小到盖不住已确认损失。
            con.execute(
                "UPDATE loss_limit SET limit_amount = 5 "
                "WHERE dept_id = 'W' AND period = 2"
            )
            con.commit()
        with self.assertRaises(EquipmentAuditError):
            audit_equipment(self.equipment.db_path, self.budget.db_path)


if __name__ == "__main__":
    unittest.main()
