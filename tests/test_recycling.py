"""Recycling turnovers: lending freeze, scan rules, ordered cleaning/residue."""
from __future__ import annotations

import unittest

from support import MATERIALS

from address_delivery.recycling import (
    CERTIFICATE_UNBACKED,
    DUPLICATE_FILL,
    DUPLICATE_RETURN,
    EARLY_RETURN,
    GLASS,
    OUT_OF_ORDER,
    PAPER,
    STALE_TAG,
    STATION_MISMATCH,
    WET_MIXED,
    LendError,
    RecyclingLedger,
)
from address_delivery.service import AddressService


class LedgerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = AddressService.from_materials(MATERIALS)
        self.addCleanup(self.svc.close)
        self.led = RecyclingLedger(self.svc)

    def lend_east(self, bin_id, house=5, street="东路"):
        return self.led.lend(bin_id, "C1", "东", street, house)


class LendingTests(LedgerCase):
    def test_lend_requires_successful_dispatch(self):
        # Unknown street -> dispatch fails, no lending is opened.
        with self.assertRaises(LendError) as ctx:
            self.led.lend("BX", "C1", "东", "不存在路", 5)
        self.assertEqual(ctx.exception.outcome, "failed_street_unknown")
        self.assertIsNone(self.led.current_lending("BX"))

    def test_lending_freezes_street_house_station(self):
        lend = self.lend_east("B1", house=5)
        self.assertEqual(
            (lend.street_id, lend.house_no, lend.station_id), ("S1", 5, "A")
        )
        # The lending's parcel is the committed job with the frozen fact.
        job = self.svc.delivery.get_job(lend.parcel_id)
        self.assertEqual((job["street_id"], job["station_id"]), ("S1", "A"))

    def test_lending_uses_frozen_station_and_house(self):
        lend = self.lend_east("B1", house=5)
        self.assertEqual(lend.station_id, "A")
        self.assertEqual(lend.street_id, "S1")
        self.assertEqual(lend.house_no, 5)
        job = self.svc.delivery.get_job(lend.parcel_id)
        self.assertEqual(job["station_id"], "A")

    def test_ambiguous_dispatch_is_refused(self):
        with self.assertRaises(LendError) as ctx:
            self.lend_east("B1", house=9)
        self.assertEqual(ctx.exception.outcome, "failed_station_ambiguous")

    def test_epochs_advance(self):
        first = self.lend_east("B1", house=5)
        self.led.note_fill("B1", PAPER, 2)
        self.led.note_return("B1")
        second = self.lend_east("B1", house=5)
        self.assertEqual((first.epoch, second.epoch), (1, 2))
        self.assertNotEqual(first.lending_id, second.lending_id)


class ScanRuleTests(LedgerCase):
    def test_fill_at_current_lent_address_applies(self):
        self.lend_east("B1", house=5)
        outcome = self.led.note_fill("B1", PAPER, 2)
        self.assertEqual(outcome["outcome"], "applied")
        self.assertEqual(self.led.material("B1")["grade"], PAPER)

    def test_early_return_is_rejected(self):
        self.lend_east("B1", house=5)
        outcome = self.led.note_return("B1")
        self.assertEqual(outcome["outcome"], EARLY_RETURN)
        self.assertIsNone(self.led.material("B1"))

    def test_duplicate_fill_is_rejected(self):
        self.lend_east("B1", house=5)
        self.led.note_fill("B1", PAPER, 2)
        second = self.led.note_fill("B1", GLASS, 1)
        self.assertEqual(second["outcome"], DUPLICATE_FILL)
        # The first fill still stands.
        self.assertEqual(self.led.material("B1")["grade"], PAPER)

    def test_stale_tag_from_previous_round_cannot_override(self):
        first = self.lend_east("B1", house=5)
        self.led.note_fill("B1", PAPER, 2)
        self.led.note_return("B1")
        second = self.lend_east("B1", house=5)
        # A scan still carrying the previous round's tag.
        stale = self.led.note_fill("B1", GLASS, 1, lending_id=first.lending_id)
        self.assertEqual(stale["outcome"], STALE_TAG)
        self.assertIsNone(self.led.material("B1"))
        # The current tag works.
        ok = self.led.note_fill("B1", GLASS, 1, lending_id=second.lending_id)
        self.assertEqual(ok["outcome"], "applied")

    def test_scan_at_wrong_station_is_rejected(self):
        self.lend_east("B1", house=5)  # current station A
        wrong = self.led.note_fill("B1", PAPER, 2, station_id="B")
        self.assertEqual(wrong["outcome"], STATION_MISMATCH)
        self.assertIsNone(self.led.material("B1"))

    def test_duplicate_return_is_rejected(self):
        self.lend_east("B1", house=5)
        self.led.note_fill("B1", PAPER, 2)
        self.assertEqual(self.led.note_return("B1")["outcome"], "applied")
        self.assertEqual(self.led.note_return("B1")["outcome"], DUPLICATE_RETURN)
        # Exactly one load was produced.
        self.assertEqual(len(self.led.reconcile("B1")["loads"]), 1)


class CleaningTests(LedgerCase):
    def test_empty_then_dry_clears_wet_residue(self):
        self.lend_east("B1", house=5)
        self.led.note_empty("B1")
        self.led.note_dry("B1")
        self.led.note_fill("B1", PAPER, 2)
        self.assertEqual(self.led.material("B1")["grade"], PAPER)

    def test_dry_before_empty_is_out_of_order(self):
        self.lend_east("B1", house=5)
        dry = self.led.note_dry("B1")
        self.assertEqual(dry["outcome"], OUT_OF_ORDER)

    def test_paper_without_cleaning_leaves_whole_bin_wet_mixed(self):
        self.led.ensure_bin("B1", initial_wet_residue=True)
        self.lend_east("B1", house=5)
        self.led.note_fill("B1", PAPER, 2)  # no empty+dry first
        self.assertEqual(self.led.material("B1")["grade"], WET_MIXED)

    def test_glass_recyclable_but_does_not_clear_residue(self):
        self.led.ensure_bin("B1", initial_wet_residue=True)
        self.lend_east("B1", house=5)
        self.led.note_fill("B1", GLASS, 1)
        self.assertEqual(self.led.material("B1")["grade"], GLASS)
        self.led.note_return("B1")
        # Residue survived the glass round.
        state = self.led.reconcile("B1")["state"]
        self.assertTrue(state["wet_residue"])

    def test_emptied_wet_mixed_bin_retains_residue(self):
        self.led.ensure_bin("B1", initial_wet_residue=True)
        self.lend_east("B1", house=5)
        self.led.note_fill("B1", PAPER, 2)  # becomes wet_mixed
        self.led.note_return("B1")           # truck takes it, bin stays wet
        state = self.led.reconcile("B1")["state"]
        self.assertTrue(state["wet_residue"])
        self.assertEqual(state["wet_reason"], "previous_round")

    def test_explicit_empty_of_wet_mixed_retains_residue_until_dry(self):
        self.led.ensure_bin("B1", initial_wet_residue=True)
        self.lend_east("B1", house=5)
        self.led.note_fill("B1", PAPER, 2)   # wet_mixed
        empty = self.led.note_empty("B1")
        self.assertEqual(empty["outcome"], "applied")
        self.assertTrue(self.led.reconcile("B1")["state"]["wet_residue"])
        self.led.note_dry("B1")
        self.assertFalse(self.led.reconcile("B1")["state"]["wet_residue"])

    def test_certificate_without_empty_is_unbacked(self):
        self.led.ensure_bin("B1", initial_wet_residue=True)
        self.lend_east("B1", house=5)
        cert = self.led.note_certificate("B1")
        self.assertEqual(cert["outcome"], CERTIFICATE_UNBACKED)
        self.assertTrue(self.led.reconcile("B1")["state"]["wet_residue"])

    def test_certificate_after_empty_clears(self):
        self.led.ensure_bin("B1", initial_wet_residue=True)
        self.lend_east("B1", house=5)
        self.led.note_empty("B1")
        cert = self.led.note_certificate("B1")
        self.assertEqual(cert["outcome"], "applied")
        self.assertFalse(self.led.reconcile("B1")["state"]["wet_residue"])


class ExplainTests(LedgerCase):
    def test_explain_reports_weight_grade_and_last_round_reason(self):
        self.led.ensure_bin("B1", initial_wet_residue=True, reason="previous_round")
        self.lend_east("B1", house=5)
        self.led.note_fill("B1", PAPER, 2)
        report = self.led.explain("B1")
        self.assertEqual(report["round_weight"], 2)
        self.assertEqual(report["round_grade"], WET_MIXED)
        self.assertEqual(report["last_round_residue"], {"wet": True, "reason": "previous_round"})

    def test_explain_lists_rejected_scans(self):
        self.lend_east("B1", house=5)
        self.led.note_fill("B1", PAPER, 2)
        self.led.note_fill("B1", GLASS, 1)  # duplicate
        self.led.note_return("B1", station_id="B")  # wrong station
        report = self.led.explain("B1")
        rejected = {e["outcome"] for e in report["rejected_events"]}
        self.assertEqual(rejected, {DUPLICATE_FILL, STATION_MISMATCH})

    def test_supplement_is_rechecked_in_original_order(self):
        self.led.lend("B1", "C1", "东", "东路", 5, seq=1)
        # Enter the fill first, but give it seq 3; the return gets seq 2.
        # Replaying by seq shows a return (seq 2) before any fill -> early.
        self.led.note_fill("B1", PAPER, 2, seq=3)
        self.led.note_return("B1", seq=2)
        report = self.led.reconcile("B1")
        ordered = [(e["seq"], e["kind"], e["outcome"]) for e in report["events"]]
        # initial(0) then lend(1) then return(2) then fill(3)
        self.assertEqual(ordered[2], (2, "return", EARLY_RETURN))
        self.assertEqual(ordered[3], (3, "fill", "applied"))
        self.assertEqual(report["loads"], [])


if __name__ == "__main__":
    unittest.main()
