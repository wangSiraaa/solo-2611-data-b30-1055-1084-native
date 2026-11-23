"""份额账本测试：实付/担保各自下取整、转让、取消、逐户核对与互填零。"""

from __future__ import annotations

import unittest

from orchard.errors import (
    IneligibleTransfer,
    ShareAlreadyConsumed,
    VerificationError,
)
from orchard.shares import (
    GUARANTEE,
    PAID,
    Adoption,
    Claim,
    ClaimLine,
    CommitmentSnapshot,
    ShareLedger,
    fen_pools,
)
from orgbudget import OrgService


def _product(tmp_root) -> tuple[OrgService, object]:
    org = OrgService(tmp_root / "org.db")
    org.create_department("HQ", None)
    org.create_department("DIV", "HQ")
    org.create_department("A", "DIV")
    org.create_department("B", "DIV")
    org.create_department("C", "HQ")       # 另一分部
    return org, org.publish("v1")


class ShareIssuanceTest(unittest.TestCase):
    def test_paid_and_guarantee_floor_separately(self) -> None:
        """实付 12、担保 8：各自 floor -> 2 + 1 串，两个零头不合并。"""
        led = ShareLedger()
        shares = led.adopt(
            Adoption("C1", "C1", "A", "R1", "GREEN", "A"),
            CommitmentSnapshot("C1", "A", paid=12, guarantee=8),
        )
        self.assertEqual(len(shares), 3)
        self.assertEqual(sum(1 for s in shares if s.kind == PAID), 2)
        self.assertEqual(sum(1 for s in shares if s.kind == GUARANTEE), 1)
        # 2 分实付零头 + 3 分担保零头合计 5 分也不能凑一串。
        pools = fen_pools(led, {"C1": CommitmentSnapshot("C1", "A", 12, 8)})
        self.assertEqual(pools["C1"], (2, 3))

    def test_guarantee_not_allowed(self) -> None:
        led = ShareLedger()
        shares = led.adopt(
            Adoption("C1", "C1", "A", "R1", "GREEN", "A", guarantee_allowed=False),
            CommitmentSnapshot("C1", "A", paid=5, guarantee=20),
        )
        self.assertEqual([s.kind for s in shares], [PAID])

    def test_adopt_is_idempotent_and_backfills_new_paid(self) -> None:
        led = ShareLedger()
        ad = Adoption("C1", "C1", "A", "R1", "GREEN", "A")
        first = led.adopt(ad, CommitmentSnapshot("C1", "A", paid=5, guarantee=5))
        self.assertEqual(len(first), 2)
        # 重复调用不重复发行。
        again = led.adopt(ad, CommitmentSnapshot("C1", "A", paid=5, guarantee=5))
        self.assertEqual(again, ())
        # 后续又实付 5：仅补 1 条实付份额。
        more = led.adopt(ad, CommitmentSnapshot("C1", "A", paid=10, guarantee=5))
        self.assertEqual([s.kind for s in more], [PAID])
        self.assertEqual(len(led.shares_of("C1")), 3)


class ShareTransferTest(unittest.TestCase):
    def test_same_division_transfer_keeps_row_variety_grade(self) -> None:
        import tempfile, pathlib

        with tempfile.TemporaryDirectory() as d:
            _org, product = _product(pathlib.Path(d))
            led = ShareLedger()
            (share,) = led.adopt(
                Adoption("CX", "CX", "A", "R1", "GREEN", "A"),
                CommitmentSnapshot("CX", "A", paid=5, guarantee=0),
            )
            moved = led.transfer(share.share_id, "B", product=product)
            self.assertEqual(moved.dept_id, "B")
            self.assertEqual((moved.row, moved.variety, moved.grade),
                             ("R1", "GREEN", "A"))
            self.assertEqual(moved.origin_dept, "A")
            self.assertIn(moved.share_id,
                          {s.share_id for s in led.available_shares("B")})

    def test_cross_division_transfer_rejected(self) -> None:
        import tempfile, pathlib

        with tempfile.TemporaryDirectory() as d:
            _org, product = _product(pathlib.Path(d))
            led = ShareLedger()
            (share,) = led.adopt(
                Adoption("CX", "CX", "A", "R1", "GREEN", "A"),
                CommitmentSnapshot("CX", "A", paid=5, guarantee=0),
            )
            with self.assertRaises(IneligibleTransfer):
                led.transfer(share.share_id, "C", product=product)

    def test_consumed_share_cannot_transfer(self) -> None:
        import tempfile, pathlib

        with tempfile.TemporaryDirectory() as d:
            _org, product = _product(pathlib.Path(d))
            led = ShareLedger()
            (share,) = led.adopt(
                Adoption("CX", "CX", "A", "R1", "GREEN", "A"),
                CommitmentSnapshot("CX", "A", paid=5, guarantee=0),
            )
            led.consume(share.share_id, "SG")
            with self.assertRaises(ShareAlreadyConsumed):
                led.transfer(share.share_id, "B", product=product)
            with self.assertRaises(ShareAlreadyConsumed):
                led.consume(share.share_id, "SY")


class CancelTest(unittest.TestCase):
    def test_cancel_revokes_unclipped_guarantee_only(self) -> None:
        led = ShareLedger()
        ad = Adoption("C1", "C1", "A", "R1", "GREEN", "A")
        led.adopt(ad, CommitmentSnapshot("C1", "A", paid=5, guarantee=5))
        paid = [s for s in led.shares_of("C1") if s.kind == PAID][0]
        guarantee = [s for s in led.shares_of("C1") if s.kind == GUARANTEE][0]
        # 先把担保份额用掉（已剪），再取消：已剪担保不被作废。
        led.consume(guarantee.share_id, "SG")
        revoked = led.revoke_guarantees("C1")
        self.assertEqual(revoked, ())                 # 无未剪担保可作废
        self.assertTrue(paid.is_available)
        # 换一张新账：未剪担保在取消后作废，实付仍可用。
        led2 = ShareLedger()
        ad2 = Adoption("C2", "C2", "A", "R1", "GREEN", "A")
        led2.adopt(ad2, CommitmentSnapshot("C2", "A", paid=5, guarantee=5))
        revoked2 = led2.revoke_guarantees("C2")
        self.assertEqual(len(revoked2), 1)
        avail = [s for s in led2.available_shares() ]
        self.assertEqual([s.kind for s in avail], [PAID])


class VerificationTest(unittest.TestCase):
    def _ledger(self):
        led = ShareLedger()
        led.adopt(Adoption("CJ", "CJ", "A", "R1", "ANY", "A"),
                  CommitmentSnapshot("CJ", "A", paid=7, guarantee=0))
        led.adopt(Adoption("CY", "CY", "A", "R1", "ANY", "A"),
                  CommitmentSnapshot("CY", "A", paid=8, guarantee=0))
        return led

    def test_ok_claim_lists_provenance(self) -> None:
        led = self._ledger()
        claim = Claim("H", (ClaimLine("S1", ("CJ:PAID:0",)),
                            ClaimLine("S2", ("CY:PAID:0",))))
        v = led.verify_claim(claim)
        self.assertEqual(v.source_commitments, frozenset({"CJ", "CY"}))
        self.assertEqual({p.string_id for p in v.provenances}, {"S1", "S2"})

    def test_cross_commitment_fill_rejected(self) -> None:
        led = self._ledger()
        # 2 分（CJ 零头）+ 3 分（CY 零头）= 5：不能互相填零凑一串。
        claim = Claim("H", (ClaimLine(
            "S3", fen_sources=(("CJ", "PAID", 2), ("CY", "PAID", 3))),))
        with self.assertRaises(VerificationError):
            led.verify_claim(claim)

    def test_partial_fen_rejected(self) -> None:
        led = self._ledger()
        claim = Claim("H", (ClaimLine("S3", fen_sources=(("CJ", "PAID", 2),)),))
        with self.assertRaises(VerificationError):
            led.verify_claim(claim)

    def test_duplicate_share_rejected(self) -> None:
        led = self._ledger()
        claim = Claim("H", (ClaimLine("S1", ("CJ:PAID:0",)),
                            ClaimLine("S2", ("CJ:PAID:0",))))
        with self.assertRaises(VerificationError):
            led.verify_claim(claim)

    def test_revoked_guarantee_cannot_back_claim(self) -> None:
        led = ShareLedger()
        led.adopt(Adoption("C1", "C1", "A", "R1", "ANY", "A"),
                  CommitmentSnapshot("C1", "A", paid=0, guarantee=5))
        led.revoke_guarantees("C1")
        claim = Claim("H", (ClaimLine("S1", ("C1:GUARANTEE:0",)),))
        with self.assertRaises(VerificationError):
            led.verify_claim(claim)


if __name__ == "__main__":
    unittest.main()
