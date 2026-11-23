"""机身折旧、换件、报废限额与独占拨付离线柜测试。"""

from __future__ import annotations

import threading
import unittest

from orgbudget.equipment import (
    accumulated_depreciation,
    book_value,
    disposal_loss,
)
from orgbudget.equipment_service import EquipmentService, VoucherLine
from orgbudget.errors import (
    EquipmentIdempotencyConflict,
    EquipmentLocked,
    InsufficientLossAllowance,
    LimitBelowObligations,
    OrderBlocked,
    PartNotOnMachine,
    SealStale,
)
from orgbudget.local_cabinet import LocalCabinet
from tests.support import EquipmentTestCase


class DepreciationTest(unittest.TestCase):
    """累计损耗 = 原值 × 已用期数 ÷ 总期数，向下取整，最多到原值。"""

    def test_floor_and_cap(self) -> None:
        self.assertEqual(accumulated_depreciation(9, 1, 3), 3)
        self.assertEqual(accumulated_depreciation(10, 1, 3), 3)
        self.assertEqual(accumulated_depreciation(9, 3, 3), 9)
        # 超龄也最多到原值。
        self.assertEqual(accumulated_depreciation(9, 5, 3), 9)
        self.assertEqual(accumulated_depreciation(9, 0, 3), 0)
        self.assertEqual(book_value(10, 1, 3), 7)

    def test_loss_floor_zero(self) -> None:
        self.assertEqual(disposal_loss(6, 0), 6)
        self.assertEqual(disposal_loss(6, 6), 0)
        # 残件价款高于账面，损失最低为零。
        self.assertEqual(disposal_loss(6, 9), 0)


class ReplaceTestCase(EquipmentTestCase):
    """共用布景：M1 装电机(三期归零)/机座(六期归零)，原值 9 / 6。"""

    def _build_m1(self) -> None:
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
        self.equipment.seal_original_value("M1")

    def _stock_new_motor(self, value: int = 7) -> None:
        """购入新电机：自带购建底单，封存后落在库。"""
        self.pay("REQ-NEW", "W", value, value, "SP-NEW")
        self.equipment.post_voucher(
            "V-NEW", "M1", "SP-NEW", [VoucherLine.direct("MOTOR2", value)]
        )
        self.equipment.seal_original_value("M1")

    def _cabinet(self, name: str = "CAB1") -> LocalCabinet:
        return LocalCabinet(self.tmp / f"{name.lower()}.db", name)


class ReplacementHappyPathTest(ReplaceTestCase):
    """限额 6，换电机可办：旧件账面 6 − 残件 0 = 损失 6，恰好够。"""

    def test_replace_motor(self) -> None:
        self._build_m1()
        self._stock_new_motor()
        self.equipment.advance_period()  # 第 2 期期初，电机已过一期
        self.assertEqual(self.equipment.book_value("M1", "MOTOR"), 6)
        self.assertEqual(self.equipment.book_value("M1", "BASE"), 5)

        self.equipment.set_loss_limit("W", 2, 6)
        self.equipment.disburse_to_cabinet("D1", "W", ["M1"], "CAB1", 6)
        cab = self._cabinet()
        cab.load(self.equipment.export_disbursement("D1"))
        cab.register_order("O1", "M1", {"MOTOR": 0}, {"MOTOR2": 3})
        (settlement,) = cab.settle_all()
        self.assertEqual(settlement.loss, 6)
        self.assertEqual(settlement.remaining_after, 0)
        manifest = cab.hand_back()
        self.assertEqual(manifest["remainder"], 0)
        (tx_id,) = self.equipment.import_return(manifest)

        # 旧电机退役、新电机上机单独计龄、机座照原日龄走。
        motor = self.equipment.part_status("M1", "MOTOR")
        self.assertEqual((motor.state, motor.book_value), ("RETIRED", 0))
        self.assertEqual(motor.retired_tx, tx_id)
        motor2 = self.equipment.part_status("M1", "MOTOR2")
        self.assertEqual(
            (motor2.state, motor2.install_period, motor2.zero_periods),
            ("INSTALLED", 2, 3),
        )
        base = self.equipment.part_status("M1", "BASE")
        self.assertEqual((base.state, base.install_period), ("INSTALLED", 1))
        allowance = self.equipment.loss_allowance("W", 2)
        self.assertEqual(
            (allowance.limit, allowance.confirmed, allowance.outstanding,
             allowance.available),
            (6, 6, 0, 0),
        )

    def test_receipt_lost_lookup_by_retired_part_no(self) -> None:
        self._build_m1()
        self._stock_new_motor()
        self.equipment.advance_period()
        self.equipment.set_loss_limit("W", 2, 6)
        self.equipment.disburse_to_cabinet("D1", "W", ["M1"], "CAB1", 6)
        cab = self._cabinet()
        cab.load(self.equipment.export_disbursement("D1"))
        cab.register_order("O1", "M1", {"MOTOR": 0}, {"MOTOR2": 3})
        cab.settle_all()
        self.equipment.import_return(cab.hand_back())

        # 回单丢失：用退役件原号查此次到底装了什么。
        info = self.equipment.lookup_installation("M1", "MOTOR")
        self.assertEqual([p.part_no for p in info.installed], ["MOTOR2"])
        self.assertEqual(
            [(p.part_no, p.book_value, p.salvage, p.loss) for p in info.retired],
            [("MOTOR", 6, 0, 6)],
        )
        # 不能按猜的重拆：旧电机已不在机上。
        self.equipment.set_loss_limit("W", 2, 7)
        self.equipment.disburse_to_cabinet("D2", "W", ["M1"], "CAB2", 1)
        cab2 = self._cabinet("CAB2")
        cab2.load(self.equipment.export_disbursement("D2"))
        cab2.register_order("O2", "M1", {"MOTOR": 0}, {})
        with self.assertRaises(PartNotOnMachine):
            cab2.settle_all()

    def test_new_parts_age_separately(self) -> None:
        self._build_m1()
        self._stock_new_motor(9)
        self.equipment.advance_period()
        self.equipment.set_loss_limit("W", 2, 6)
        self.equipment.disburse_to_cabinet("D1", "W", ["M1"], "CAB1", 6)
        cab = self._cabinet()
        cab.load(self.equipment.export_disbursement("D1"))
        cab.register_order("O1", "M1", {"MOTOR": 0}, {"MOTOR2": 3})
        cab.settle_all()
        self.equipment.import_return(cab.hand_back())

        self.equipment.advance_period()  # 第 3 期
        # 新电机第 2 期上机：已过 1 期；机座第 1 期上机：已过 2 期。
        self.assertEqual(self.equipment.book_value("M1", "MOTOR2"), 9 - 3)
        self.assertEqual(self.equipment.book_value("M1", "BASE"), 6 - 2)


class WorkOrderRejectionTest(ReplaceTestCase):
    """题面两个“须拒”分支：重封后账面变高、限额被别的退役占去。"""

    def test_resealed_higher_book_breaks_work_order(self) -> None:
        self._build_m1()
        self._stock_new_motor()
        # 只更正工时为 2 和 1，重新封同一组实付：电机变 10，账面 7。
        self.equipment.correct_install_hours("V-INST", 1, {"MOTOR": 2, "BASE": 1})
        self.assertEqual(self.equipment.seal_original_value("M1")["MOTOR"], 10)
        self.equipment.advance_period()
        self.assertEqual(self.equipment.book_value("M1", "MOTOR"), 7)
        self.equipment.set_loss_limit("W", 2, 6)
        # 损失 7 超限额 6：中心拨不出 7。
        with self.assertRaises(InsufficientLossAllowance):
            self.equipment.disburse_to_cabinet("D1", "W", ["M1"], "CAB1", 7)
        # 拨 6 到柜上，核算出实际损失 7 一样办不成，原工单被拒。
        self.equipment.disburse_to_cabinet("D1", "W", ["M1"], "CAB1", 6)
        cab = self._cabinet()
        cab.load(self.equipment.export_disbursement("D1"))
        cab.register_order("O1", "M1", {"MOTOR": 0}, {"MOTOR2": 3})
        with self.assertRaises(InsufficientLossAllowance):
            cab.settle_all()
        # 没动旧件：电机仍在机上，机座照常计龄（重封后原值 5，已过 1 期）。
        self.assertEqual(self.equipment.part_status("M1", "MOTOR").state, "INSTALLED")
        self.assertEqual(self.equipment.book_value("M1", "BASE"), 5)

    def test_limit_consumed_by_other_retirement(self) -> None:
        self._build_m1()
        self._stock_new_motor()
        # 另一台设备本期已办退役，损失占去限额 1。
        self.pay("REQ-X", "W", 2, 2, "SP-X")
        self.equipment.register_equipment("M2", "W", 1, {"X": 2})
        self.equipment.post_voucher(
            "V-X", "M2", "SP-X", [VoucherLine.direct("X", 2)]
        )
        self.equipment.seal_original_value("M2")
        self.equipment.advance_period()
        self.equipment.set_loss_limit("W", 2, 6)
        self.equipment.disburse_to_cabinet("D-X", "W", ["M2"], "CABX", 1)
        cabx = self._cabinet("CABX")
        cabx.load(self.equipment.export_disbursement("D-X"))
        cabx.register_order("O-X", "M2", {"X": 0}, {})
        cabx.settle_all()
        self.equipment.import_return(cabx.hand_back())
        allowance = self.equipment.loss_allowance("W", 2)
        self.assertEqual((allowance.confirmed, allowance.available), (1, 5))

        # 电机账面 6，限额只剩 5：同样须拒。
        self.assertEqual(self.equipment.book_value("M1", "MOTOR"), 6)
        with self.assertRaises(InsufficientLossAllowance):
            self.equipment.disburse_to_cabinet("D1", "W", ["M1"], "CAB1", 6)
        # 机座照常计龄，不受拒单影响。
        self.assertEqual(self.equipment.book_value("M1", "BASE"), 5)


class ExclusiveDisbursementTest(EquipmentTestCase):
    """独占拨付：在途未交还的钱不得再给另一台设备；一柜可先后办两台。"""

    def _build_two_devices(self) -> None:
        """A 旧件账面 7（残件记 3 → 损失 4）；B 旧件账面 2（残件 0 → 损失 2）。"""
        self.build_workshop()
        self.pay("REQ-PA", "W", 14, 14, "SP-PA")
        self.pay("REQ-PA2", "W", 5, 5, "SP-PA2")
        self.equipment.register_equipment("MA", "W", 1, {"PA": 2})
        self.equipment.post_voucher("V-PA", "MA", "SP-PA",
                                    [VoucherLine.direct("PA", 14)])
        self.equipment.post_voucher("V-PA2", "MA", "SP-PA2",
                                    [VoucherLine.direct("PA2", 5)])
        self.equipment.seal_original_value("MA")
        self.pay("REQ-PB", "W", 4, 4, "SP-PB")
        self.pay("REQ-PB2", "W", 3, 3, "SP-PB2")
        self.equipment.register_equipment("MB", "W", 1, {"PB": 2})
        self.equipment.post_voucher("V-PB", "MB", "SP-PB",
                                    [VoucherLine.direct("PB", 4)])
        self.equipment.post_voucher("V-PB2", "MB", "SP-PB2",
                                    [VoucherLine.direct("PB2", 3)])
        self.equipment.seal_original_value("MB")
        self.equipment.advance_period()
        self.equipment.set_loss_limit("W", 2, 6)

    def _cabinet(self, name: str) -> LocalCabinet:
        return LocalCabinet(self.tmp / f"{name.lower()}.db", name)

    def test_in_flight_disbursement_blocks_second_device(self) -> None:
        self._build_two_devices()
        # A 占 5 的拨付在途、中心没收到余款回执：B 此时没足 2。
        self.equipment.disburse_to_cabinet("D-A", "W", ["MA"], "CAB1", 5)
        allowance = self.equipment.loss_allowance("W", 2)
        self.assertEqual((allowance.outstanding, allowance.available), (5, 1))
        with self.assertRaises(InsufficientLossAllowance):
            self.equipment.disburse_to_cabinet("D-B", "W", ["MB"], "CAB2", 2)
        # 柜上交还余款后，这笔钱才回到可用限额。
        cab1 = self._cabinet("CAB1")
        cab1.load(self.equipment.export_disbursement("D-A"))
        self.equipment.import_return(cab1.hand_back())
        allowance = self.equipment.loss_allowance("W", 2)
        self.assertEqual((allowance.outstanding, allowance.available), (0, 6))
        self.equipment.disburse_to_cabinet("D-B", "W", ["MB"], "CAB2", 2)

    def test_one_cabinet_settles_two_devices_in_order(self) -> None:
        self._build_two_devices()
        # 柜上收到一笔独占余额 6 并能看见两套实件：按 4 及 2 先后办完。
        self.equipment.disburse_to_cabinet("D1", "W", ["MA", "MB"], "CAB1", 6)
        cab = self._cabinet("CAB1")
        snapshot = self.equipment.export_disbursement("D1")
        self.assertEqual(set(snapshot["devices"]), {"MA", "MB"})
        cab.load(snapshot)
        cab.register_order("OA", "MA", {"PA": 3}, {"PA2": 2}, estimated_loss=5)
        cab.register_order("OB", "MB", {"PB": 0}, {"PB2": 2})
        first, second = cab.settle_all()
        self.assertEqual((first.loss, first.remaining_after), (4, 2))
        self.assertEqual((second.loss, second.remaining_after), (2, 0))
        manifest = cab.hand_back()
        self.assertEqual(manifest["remainder"], 0)
        tx_ids = self.equipment.import_return(manifest)
        self.assertEqual(len(tx_ids), 2)
        allowance = self.equipment.loss_allowance("W", 2)
        self.assertEqual((allowance.confirmed, allowance.available), (6, 0))
        self.assertEqual(self.equipment.part_status("MA", "PA").state, "RETIRED")
        self.assertEqual(self.part_book("MB", "PB2"), 3)

    def part_book(self, equipment_id: str, part_no: str) -> int:
        return self.equipment.book_value(equipment_id, part_no)

    def test_salvage_change_over_limit_blocks_everyone(self) -> None:
        self._build_two_devices()
        self.equipment.disburse_to_cabinet("D1", "W", ["MA", "MB"], "CAB1", 6)
        cab = self._cabinet("CAB1")
        cab.load(self.equipment.export_disbursement("D1"))
        # 残件记录先记 0：A 实际损失 7 超 6，谁都动不了。
        cab.register_order("OA", "MA", {"PA": 0}, {"PA2": 2}, estimated_loss=5)
        cab.register_order("OB", "MB", {"PB": 0}, {"PB2": 2})
        with self.assertRaises(InsufficientLossAllowance):
            cab.settle_next()
        # 前序未了结，B 不能先动。
        with self.assertRaises(OrderBlocked):
            cab.settle_order("OB")
        with self.assertRaises(InsufficientLossAllowance):
            cab.settle_all()
        # 更正残件记录为 3：A 损失 4，随后 B 损失 2，先后办完。
        cab.update_order_salvage("OA", "PA", 3)
        first, second = cab.settle_all()
        self.assertEqual((first.loss, second.loss), (4, 2))
        self.equipment.import_return(cab.hand_back())
        self.assertEqual(self.equipment.loss_allowance("W", 2).confirmed, 6)

    def test_locked_equipment_rejects_accounting_changes(self) -> None:
        self._build_two_devices()
        self.equipment.disburse_to_cabinet("D-A", "W", ["MA"], "CAB1", 5)
        # 收到独占拨付后，受益设备账务锁定：不能加底单、不能重封。
        self.pay("REQ-Z", "W", 1, 1, "SP-Z")
        with self.assertRaises(EquipmentLocked):
            self.equipment.post_voucher(
                "V-Z", "MA", "SP-Z", [VoucherLine.direct("PZ", 1)]
            )
        with self.assertRaises(EquipmentLocked):
            self.equipment.seal_original_value("MA")
        with self.assertRaises(EquipmentLocked):
            self.equipment.correct_install_hours("V-PA", 1, {"PA": 1})
        # 另一台设备不受影响；同一设备不得再被别的拨付拿走。
        self.equipment.post_voucher(
            "V-Z", "MB", "SP-Z", [VoucherLine.direct("PZ", 1)]
        )
        with self.assertRaises(EquipmentLocked):
            self.equipment.disburse_to_cabinet("D-A2", "W", ["MA"], "CAB2", 1)
        # 交还后解锁。
        cab = self._cabinet("CAB1")
        cab.load(self.equipment.export_disbursement("D-A"))
        self.equipment.import_return(cab.hand_back())
        self.pay("REQ-Z2", "W", 1, 1, "SP-Z2")
        self.equipment.post_voucher(
            "V-Z2", "MA", "SP-Z2", [VoucherLine.direct("PZ", 1)])
        with self.assertRaises(SealStale):
            self.equipment.export_disbursement(
                self.equipment.disburse_to_cabinet(
                    "D-A3", "W", ["MA"], "CAB1", 1).disbursement_id)

    def test_remainder_usable_only_after_handback(self) -> None:
        self._build_two_devices()
        self.equipment.disburse_to_cabinet("D1", "W", ["MA"], "CAB1", 6)
        cab = self._cabinet("CAB1")
        cab.load(self.equipment.export_disbursement("D1"))
        cab.register_order("OA", "MA", {"PA": 3}, {"PA2": 2})
        cab.settle_all()
        # 柜上已核算损失 4，但中心没收到回执前，6 分全部仍在途。
        allowance = self.equipment.loss_allowance("W", 2)
        self.assertEqual((allowance.confirmed, allowance.outstanding,
                          allowance.available), (0, 6, 0))
        with self.assertRaises(InsufficientLossAllowance):
            self.equipment.disburse_to_cabinet("D2", "W", ["MB"], "CAB2", 1)
        self.equipment.import_return(cab.hand_back())
        allowance = self.equipment.loss_allowance("W", 2)
        self.assertEqual((allowance.confirmed, allowance.outstanding,
                          allowance.available), (4, 0, 2))
        self.equipment.disburse_to_cabinet("D2", "W", ["MB"], "CAB2", 2)

    def test_import_idempotent_and_conflict_detected(self) -> None:
        self._build_two_devices()
        self.equipment.disburse_to_cabinet("D1", "W", ["MA"], "CAB1", 6)
        cab = self._cabinet("CAB1")
        cab.load(self.equipment.export_disbursement("D1"))
        cab.register_order("OA", "MA", {"PA": 3}, {"PA2": 2})
        cab.settle_all()
        manifest = cab.hand_back()
        first = self.equipment.import_return(manifest)
        second = self.equipment.import_return(manifest)
        self.assertEqual(first, second)
        self.assertEqual(self.equipment.loss_allowance("W", 2).confirmed, 4)
        tampered = dict(manifest, remainder=manifest["remainder"] + 1)
        with self.assertRaises(EquipmentIdempotencyConflict):
            self.equipment.import_return(tampered)

    def test_limit_cannot_drop_below_obligations(self) -> None:
        self._build_two_devices()
        self.equipment.disburse_to_cabinet("D1", "W", ["MA"], "CAB1", 5)
        with self.assertRaises(LimitBelowObligations):
            self.equipment.set_loss_limit("W", 2, 4)
        self.equipment.set_loss_limit("W", 2, 5)

    def test_concurrent_disburse_race_only_one_wins(self) -> None:
        self._build_two_devices()
        barrier = threading.Barrier(2)
        results: list[str] = []

        def race(tag: str, device: str) -> None:
            svc = EquipmentService(
                self.equipment.db_path, self.budget, busy_timeout_ms=30_000
            )
            barrier.wait()
            try:
                svc.disburse_to_cabinet(f"D-{tag}", "W", [device], f"CAB-{tag}", 4)
                results.append(f"ok-{tag}")
            except InsufficientLossAllowance:
                results.append(f"reject-{tag}")

        threads = [
            threading.Thread(target=race, args=("A", "MA")),
            threading.Thread(target=race, args=("B", "MB")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(r.split("-")[0] for r in results), ["ok", "reject"])
        self.assertEqual(self.equipment.loss_allowance("W", 2).outstanding, 4)

    def test_state_survives_restart(self) -> None:
        self._build_two_devices()
        self.equipment.disburse_to_cabinet("D1", "W", ["MA"], "CAB1", 6)
        cab = self._cabinet("CAB1")
        cab.load(self.equipment.export_disbursement("D1"))
        cab.register_order("OA", "MA", {"PA": 3}, {"PA2": 2})
        cab.settle_all()
        self.equipment.import_return(cab.hand_back())

        reopened = EquipmentService(self.equipment.db_path, self.budget)
        info = reopened.lookup_installation("MA", "PA")
        self.assertEqual([p.part_no for p in info.installed], ["PA2"])
        self.assertEqual(reopened.loss_allowance("W", 2).confirmed, 4)
        self.assertEqual(reopened.original_values("MA")["PA"], 14)


if __name__ == "__main__":
    unittest.main()
