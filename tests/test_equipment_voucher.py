"""购建底单与原值封存测试：分摊、三条红线、重封、迁编归属、部分实付。"""

from __future__ import annotations

import sqlite3
import unittest

from orgbudget.equipment import HourlyPart, split_installation_fee
from orgbudget.equipment_service import VoucherLine
from orgbudget.errors import (
    AllocationMismatch,
    CrossDivisionClaim,
    DuplicateClaim,
    EquipmentIdempotencyConflict,
    PartAlreadyRetired,
    SealStale,
    SpendEventNotFound,
)
from orgbudget.local_cabinet import LocalCabinet
from tests.support import EquipmentTestCase


class InstallFeeSplitTest(unittest.TestCase):
    """共用安装费按工时比例分，余数从大到小、同余数按部件号补分。"""

    def test_split_by_hours(self) -> None:
        r = split_installation_fee(3, [HourlyPart("MOTOR", 1), HourlyPart("BASE", 2)])
        self.assertEqual(r, {"MOTOR": 1, "BASE": 2})

    def test_remainder_tie_broken_by_part_no(self) -> None:
        r = split_installation_fee(
            5, [HourlyPart("C", 1), HourlyPart("A", 1), HourlyPart("B", 1)]
        )
        self.assertEqual(r, {"A": 2, "B": 2, "C": 1})
        self.assertEqual(sum(r.values()), 5)

    def test_larger_remainder_first(self) -> None:
        r = split_installation_fee(
            11, [HourlyPart("x", 6), HourlyPart("y", 3), HourlyPart("z", 1)]
        )
        self.assertEqual(r, {"x": 7, "y": 3, "z": 1})


class VoucherSealTest(EquipmentTestCase):
    """题面主例：电机 8、机座 4、安装费 3（工时 1:2）→ 原值 9 和 6。"""

    def _post_main_vouchers(self, equipment_id: str = "M1") -> None:
        self.pay("REQ-MOTOR", "W", 8, 8, "SP-MOTOR")
        self.pay("REQ-BASE", "W", 4, 4, "SP-BASE")
        self.pay("REQ-INST", "W", 3, 3, "SP-INST")
        self.equipment.post_voucher(
            "V-MOTOR", equipment_id, "SP-MOTOR", [VoucherLine.direct("MOTOR", 8)]
        )
        self.equipment.post_voucher(
            "V-BASE", equipment_id, "SP-BASE", [VoucherLine.direct("BASE", 4)]
        )
        self.equipment.post_voucher(
            "V-INST",
            equipment_id,
            "SP-INST",
            [VoucherLine.install(3, {"MOTOR": 1, "BASE": 2})],
        )

    def _main_setup(self) -> None:
        self.build_workshop()
        self.equipment.register_equipment("M1", "W", 1, {"MOTOR": 3, "BASE": 6})
        self._post_main_vouchers()

    def test_seal_computes_original_values(self) -> None:
        self._main_setup()
        # 会计在工坊尚未要求换件时即可独立封存。
        values = self.equipment.seal_original_value("M1")
        self.assertEqual(values, {"MOTOR": 9, "BASE": 6})
        self.assertEqual(self.equipment.original_values("M1"), {"MOTOR": 9, "BASE": 6})

    def test_part_money_trail(self) -> None:
        self._main_setup()
        self.equipment.seal_original_value("M1")
        trail = self.equipment.part_money_trail("M1", "MOTOR")
        self.assertEqual(
            [(t.voucher_id, t.spend_idem_key, t.request_no, t.dept_id, t.kind, t.amount)
             for t in trail],
            [
                ("V-MOTOR", "SP-MOTOR", "REQ-MOTOR", "W", "DIRECT", 8),
                ("V-INST", "SP-INST", "REQ-INST", "W", "INSTALL", 1),
            ],
        )
        self.assertTrue(all(t.org_version == 1 for t in trail))
        base = self.equipment.part_money_trail("M1", "BASE")
        self.assertEqual(sum(t.amount for t in base), 6)

    def test_voucher_idempotent_repost(self) -> None:
        self.build_workshop()
        self.equipment.register_equipment("M1", "W", 1, {"MOTOR": 3})
        self.pay("REQ-MOTOR", "W", 8, 8, "SP-MOTOR")
        v1 = self.equipment.post_voucher(
            "V1", "M1", "SP-MOTOR", [VoucherLine.direct("MOTOR", 8)]
        )
        v2 = self.equipment.post_voucher(
            "V1", "M1", "SP-MOTOR", [VoucherLine.direct("MOTOR", 8)]
        )
        self.assertEqual(v1, v2)
        with self.assertRaises(EquipmentIdempotencyConflict):
            self.equipment.post_voucher(
                "V1", "M1", "SP-MOTOR", [VoucherLine.direct("MOTOR", 7)]
            )

    def test_unknown_spend_event_rejected(self) -> None:
        self.build_workshop()
        self.equipment.register_equipment("M1", "W", 1, {"MOTOR": 3})
        with self.assertRaises(SpendEventNotFound):
            self.equipment.post_voucher(
                "V-X", "M1", "SP-GHOST", [VoucherLine.direct("MOTOR", 1)]
            )

    def test_duplicate_claim_rejected(self) -> None:
        self.build_workshop()
        self.equipment.register_equipment("M1", "W", 1, {"MOTOR": 3})
        self.equipment.register_equipment("M2", "W", 1, {"MOTOR": 3})
        self.pay("REQ-MOTOR", "W", 8, 8, "SP-MOTOR")
        self.equipment.post_voucher(
            "V1", "M1", "SP-MOTOR", [VoucherLine.direct("MOTOR", 8)]
        )
        # 同一笔实付被另一台设备重领。
        with self.assertRaises(DuplicateClaim):
            self.equipment.post_voucher(
                "V2", "M2", "SP-MOTOR", [VoucherLine.direct("MOTOR", 8)]
            )

    def test_duplicate_claim_detected_at_seal_after_tamper(self) -> None:
        self.build_workshop()
        self.equipment.register_equipment("M1", "W", 1, {"MOTOR": 3})
        self.equipment.register_equipment("M2", "W", 1, {"MOTOR": 3})
        self.pay("REQ-MOTOR", "W", 8, 8, "SP-MOTOR")
        self.equipment.post_voucher(
            "V1", "M1", "SP-MOTOR", [VoucherLine.direct("MOTOR", 8)]
        )
        # 绕过服务直接篡改：把同一实付塞进另一台设备的底单。
        with sqlite3.connect(self.equipment.db_path) as con:
            con.execute(
                "INSERT INTO voucher(voucher_id, equipment_id, dept_id, org_version, "
                "spend_idem_key, request_no, spend_amount, created_at) "
                "VALUES ('V2', 'M2', 'W', 1, 'SP-MOTOR', 'REQ-MOTOR', 8, 'now')"
            )
            con.execute(
                "INSERT INTO voucher_line(voucher_id, line_no, kind, part_no, amount) "
                "VALUES ('V2', 1, 'DIRECT', 'MOTOR', 8)"
            )
            con.commit()
        with self.assertRaises(DuplicateClaim):
            self.equipment.seal_original_value("M2")

    def test_missing_allocation_rejected_at_seal(self) -> None:
        self.build_workshop()
        self.equipment.register_equipment("M1", "W", 1, {"MOTOR": 3})
        self.pay("REQ-MOTOR", "W", 8, 8, "SP-MOTOR")
        # 漏分：只拆 7，实付是 8。
        self.equipment.post_voucher(
            "V1", "M1", "SP-MOTOR", [VoucherLine.direct("MOTOR", 7)]
        )
        with self.assertRaises(AllocationMismatch):
            self.equipment.seal_original_value("M1")

    def test_over_allocation_rejected_at_seal(self) -> None:
        self.build_workshop()
        self.equipment.register_equipment("M1", "W", 1, {"MOTOR": 3, "BASE": 6})
        self.pay("REQ-MOTOR", "W", 8, 8, "SP-MOTOR")
        self.equipment.post_voucher(
            "V1",
            "M1",
            "SP-MOTOR",
            [VoucherLine.direct("MOTOR", 5), VoucherLine.direct("BASE", 4)],
        )
        with self.assertRaises(AllocationMismatch):
            self.equipment.seal_original_value("M1")

    def test_cross_division_claim_rejected(self) -> None:
        version = self.build_tree([("HQ", None), ("W1", "HQ"), ("W2", "HQ")])
        product = self.org.get_product(version)
        self.budget.register_root_budget(version, 100_000, self.weights_for(product))
        self.pay("REQ-1", "W1", 8, 8, "SP-1")
        self.equipment.register_equipment("M1", "W2", 1, {"MOTOR": 3})
        # 设备在 W2，却拿了 W1 的实付原件。
        self.equipment.post_voucher(
            "V1", "M1", "SP-1", [VoucherLine.direct("MOTOR", 8)]
        )
        with self.assertRaises(CrossDivisionClaim):
            self.equipment.seal_original_value("M1")

    def test_reseal_after_hours_correction(self) -> None:
        self._main_setup()
        self.assertEqual(self.equipment.seal_original_value("M1"),
                         {"MOTOR": 9, "BASE": 6})
        # 只更正工时为 2 和 1，重新封同一组实付。
        self.equipment.correct_install_hours("V-INST", 1, {"MOTOR": 2, "BASE": 1})
        with self.assertRaises(SealStale):
            self.equipment.original_values("M1")
        values = self.equipment.seal_original_value("M1")
        self.assertEqual(values, {"MOTOR": 10, "BASE": 5})
        self.assertEqual(self.equipment.original_values("M1"), values)

    def test_partial_spend_then_cancel_still_visible(self) -> None:
        """旧库办部分实付后取消余款：核原值与查退役仍看得到这笔钱。"""
        self.build_workshop()
        self.equipment.register_equipment("M1", "W", 1, {"MOTOR": 3})
        self.pay("REQ-P", "W", 10, 4, "SP-PARTIAL", cancel_rest=True)
        commitment = self.budget.get_commitment("REQ-P")
        self.assertEqual((commitment.status, commitment.spent), ("CANCELLED", 4))
        self.equipment.post_voucher(
            "V-P", "M1", "SP-PARTIAL", [VoucherLine.direct("MOTOR", 4)]
        )
        values = self.equipment.seal_original_value("M1")
        self.assertEqual(values["MOTOR"], 4)
        trail = self.equipment.part_money_trail("M1", "MOTOR")
        self.assertEqual(
            [(t.spend_idem_key, t.request_no, t.amount) for t in trail],
            [("SP-PARTIAL", "REQ-P", 4)],
        )

    def test_org_migration_keeps_voucher_attribution(self) -> None:
        self._main_setup()
        self.equipment.seal_original_value("M1")
        voucher = self.equipment.get_voucher("V-INST")
        self.assertEqual((voucher.dept_id, voucher.org_version), ("W", 1))
        # 迁编：W 移到新部门 D 下并换版，早期入账归属不改写。
        self.org.create_department("D", "HQ")
        self.org.move_subtree("W", "D")
        v2 = self.org.publish("W 迁到 D 下").version
        product = self.org.get_product(v2)
        self.budget.revise_budget(v2, 100_000, self.weights_for(product))
        voucher = self.equipment.get_voucher("V-INST")
        self.assertEqual((voucher.dept_id, voucher.org_version), ("W", 1))
        # 封存与分钱查询不随迁编改变。
        self.assertEqual(self.equipment.original_values("M1"), {"MOTOR": 9, "BASE": 6})
        trail = self.equipment.part_money_trail("M1", "MOTOR")
        self.assertEqual(sum(t.amount for t in trail), 9)
        self.assertTrue(all(t.dept_id == "W" for t in trail))

    def test_correction_blocked_after_retirement(self) -> None:
        self._main_setup()
        self.pay("REQ-NEW", "W", 7, 7, "SP-NEW")
        self.equipment.post_voucher(
            "V-NEW", "M1", "SP-NEW", [VoucherLine.direct("MOTOR2", 7)]
        )
        self.equipment.seal_original_value("M1")
        self.equipment.advance_period()
        self.equipment.set_loss_limit("W", 2, 6)
        self.equipment.disburse_to_cabinet("D1", "W", ["M1"], "CAB1", 6)
        cab = LocalCabinet(self.tmp / "cab1.db", "CAB1")
        cab.load(self.equipment.export_disbursement("D1"))
        cab.register_order("O1", "M1", {"MOTOR": 0}, {"MOTOR2": 3})
        cab.settle_all()
        self.equipment.import_return(cab.hand_back())
        # 退役件原值是历史事实，涉及它的底单不得再更正。
        with self.assertRaises(PartAlreadyRetired):
            self.equipment.correct_install_hours("V-INST", 1, {"MOTOR": 2, "BASE": 1})


if __name__ == "__main__":
    unittest.main()
