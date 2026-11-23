"""Truck compartments: grading, both planning methods, mandatory bins, proof."""
from __future__ import annotations

import unittest

from support import ROOT

from address_delivery.scenarios import load_scenario, run
from address_delivery.truck import (
    Compartment,
    MIXED,
    PartitionLockedError,
    Truck,
    arrival_grade,
    make_bin,
    plan_late,
    plan_with_partition,
    prove_early_capacity,
)

SCEN = ROOT / "scenarios"


def scenario(name):
    return run(load_scenario(SCEN / f"{name}.json"))


# -- grading unit tests ------------------------------------------------------


class ArrivalGradeTests(unittest.TestCase):
    paper = Compartment("P", 5, ("paper",))
    glass = Compartment("G", 5, ("glass",))
    mixing = Compartment("X", 5, ("paper", "glass"), mix_permitted=True)

    def test_single_grade_kept_when_line_accepts(self):
        self.assertEqual(arrival_grade(self.paper, ["paper"]), "paper")
        self.assertEqual(arrival_grade(self.glass, ["glass"]), "glass")

    def test_paper_and_glass_together_become_mixed(self):
        self.assertEqual(arrival_grade(self.mixing, ["paper", "glass"]), MIXED)

    def test_wet_mixed_makes_whole_compartment_mixed(self):
        self.assertEqual(arrival_grade(self.mixing, ["paper", "wet_mixed"]), MIXED)
        self.assertEqual(arrival_grade(self.mixing, ["glass", "wet_mixed"]), MIXED)

    def test_grade_outside_registered_line_downgrades(self):
        # A paper bin forced into the glass line is downgraded to mixed.
        self.assertEqual(arrival_grade(self.glass, ["paper"]), MIXED)


# -- canonical and residue scenarios ----------------------------------------


class CanonicalScenarioTests(unittest.TestCase):
    def test_both_stations_collect_qualified_paper_and_glass(self):
        r = scenario("canonical")
        for method in ("method_a", "method_b"):
            with self.subTest(method=method):
                report = r[method]
                self.assertTrue(report["feasible"])
                self.assertEqual(report["completed_committed_stations"], 2)
                slip = report["weighing_slip"]
                self.assertEqual(slip["qualified_paper_weight"], 2)
                self.assertEqual(slip["qualified_glass_weight"], 1)
                self.assertEqual(slip["mixed_weight"], 0)
                self.assertEqual(slip["recyclable_weight"], 3)

    def test_arrival_grades_named(self):
        r = scenario("canonical")
        lines = {c["compartment_id"]: c for c in r["method_b"]["compartments"]}
        self.assertEqual(lines["P"]["arrival_grade"], "paper")
        self.assertEqual(lines["G"]["arrival_grade"], "glass")


class CleanProvenTests(unittest.TestCase):
    def test_clean_bin_collects_both_stations(self):
        r = scenario("clean_proven")
        self.assertTrue(r["method_b"]["feasible"])
        self.assertEqual(r["method_b"]["completed_committed_stations"], 2)
        b1 = next(b for b in r["bins"] if b["bin_id"] == "B1")
        self.assertEqual(b1["grade"], "paper")
        self.assertEqual(r["method_b"]["weighing_slip"]["qualified_paper_weight"], 2)

    def test_explain_shows_previous_round_residue_cleared(self):
        r = scenario("clean_proven")
        explain = next(e for e in r["explain"] if e["bin_id"] == "B1")
        self.assertEqual(explain["last_round_residue"], {"wet": True, "reason": "previous_round"})
        self.assertFalse(explain["wet_residue"])


class CertificateWithdrawnTests(unittest.TestCase):
    def test_paper_bin_is_wet_mixed_and_not_boarded(self):
        r = scenario("certificate_withdrawn")
        b1 = next(b for b in r["bins"] if b["bin_id"] == "B1")
        self.assertEqual(b1["grade"], "wet_mixed")
        # No mixing compartment exists, so B1 is left; only glass collected.
        self.assertIsNone(r["method_b"]["assignment"]["B1"])
        self.assertEqual(r["method_b"]["completed_committed_stations"], 1)
        self.assertEqual(r["method_b"]["weighing_slip"]["qualified_glass_weight"], 1)
        self.assertEqual(r["method_b"]["weighing_slip"]["mixed_weight"], 0)

    def test_method_a_flags_reissue_of_paper_term(self):
        r = scenario("certificate_withdrawn")
        reissues = r["method_a"]["reissue_needed"]
        self.assertEqual(len(reissues), 1)
        self.assertEqual(reissues[0]["bin_id"], "B1")
        self.assertEqual(reissues[0]["current_grade"], "wet_mixed")


class LockedOneTwoTests(unittest.TestCase):
    def test_paper_bin_exceeds_locked_paper_capacity(self):
        r = scenario("locked_paper1_glass2")
        # Paper compartment capacity 1 < paper bin weight 2, and the bin may not
        # be split, so it cannot ride; only the glass is collected.
        self.assertIsNone(r["method_b"]["assignment"]["B1"])
        self.assertEqual(r["method_b"]["assignment"]["B2"], "G")
        self.assertEqual(r["method_b"]["weighing_slip"]["qualified_paper_weight"], 0)
        self.assertEqual(r["method_b"]["weighing_slip"]["qualified_glass_weight"], 1)
        self.assertEqual(r["method_b"]["completed_committed_stations"], 1)


class MixDowngradeTests(unittest.TestCase):
    def test_sharing_one_mixing_compartment_downgrades_everything(self):
        r = scenario("mix_downgrade")
        report = r["method_b"]
        self.assertTrue(report["feasible"])
        # Both bins ride, both stations complete, but all weight is mixed.
        self.assertEqual(report["completed_committed_stations"], 2)
        slip = report["weighing_slip"]
        self.assertEqual(slip["qualified_paper_weight"], 0)
        self.assertEqual(slip["qualified_glass_weight"], 0)
        self.assertEqual(slip["mixed_weight"], 3)


class ZoningChangeTests(unittest.TestCase):
    def test_original_lending_stays_at_old_station(self):
        r = scenario("zoning_change")
        lending = r["lendings"]["B1"]
        self.assertEqual(lending["station_id"], "ST1")
        # A fresh submission after the revision resolves to the new station.
        self.assertEqual(r["probes"]["PZ"]["station_id"], "ST2")
        # But the scan naming the new station is a mismatch vs the frozen lending.
        explain = r["explain"][0]
        outcomes = {e["kind"]: e["outcome"] for e in explain["rejected_events"]}
        self.assertEqual(outcomes.get("return"), "station_mismatch")
        # The bin's load is still at the old station.
        self.assertEqual(r["bins"][0]["station_id"], "ST1")


# -- method-specific planning and proof -------------------------------------


class ReissueCertificateTests(unittest.TestCase):
    def test_late_drying_forces_reissue_under_method_a(self):
        r = scenario("reissue_certificate")
        reissues = r["method_a"]["reissue_needed"]
        self.assertEqual(len(reissues), 1)
        self.assertEqual(reissues[0]["issued_grade"], "wet_mixed")
        self.assertEqual(reissues[0]["current_grade"], "paper")
        # Method B just loads it as clean paper alongside glass.
        self.assertEqual(r["method_b"]["assignment"]["B1"], "X")
        self.assertEqual(r["method_b"]["weighing_slip"]["qualified_paper_weight"], 2)
        # Late decision still proves the early station's freedom is safe.
        self.assertTrue(r["reservation_proof"]["proved"])


class LatePlanningTests(unittest.TestCase):
    def test_all_five_stations_complete_and_recyclable_maximised(self):
        r = scenario("late_planning")
        report = r["method_b"]
        self.assertTrue(report["feasible"])
        self.assertEqual(report["completed_committed_stations"], 5)
        self.assertEqual(report["completed_stations"], 5)
        # Dedicated paper and glass lines keep their qualified weight; only
        # overflow paper/glass that shares the mixing compartment downgrades.
        slip = report["weighing_slip"]
        self.assertEqual(slip["qualified_paper_weight"], 3)
        self.assertEqual(slip["qualified_glass_weight"], 3)
        self.assertEqual(slip["mixed_weight"], 4)

    def test_reservation_proof_holds(self):
        r = scenario("late_planning")
        proof = r["reservation_proof"]
        self.assertTrue(proof["proved"])
        # Early stations reach their best possible count within residual space.
        self.assertEqual(proof["early_completed_committed"], proof["early_best_possible"])
        for line in proof["capacity_ledger"]:
            self.assertGreaterEqual(line["free_for_early"], 0)
            self.assertLessEqual(line["early_used"], line["free_for_early"])


class MethodComparisonTests(unittest.TestCase):
    def test_late_never_worse_than_depot_on_recyclable(self):
        for name in ("canonical", "clean_proven", "certificate_withdrawn",
                     "locked_paper1_glass2", "mix_downgrade", "reissue_certificate"):
            with self.subTest(scenario=name):
                r = scenario(name)
                a, b = r["method_a"], r["method_b"]
                # Late planning can always reproduce a depot plan that is
                # feasible, so it is at least as good on both objectives.
                self.assertGreaterEqual(
                    b["completed_committed_stations"], a["completed_committed_stations"]
                )
                self.assertGreaterEqual(b["recyclable_weight"], a["recyclable_weight"])


class MandatoryBinTests(unittest.TestCase):
    comps = [
        Compartment("P", 2, ("paper",)),
        Compartment("G", 1, ("glass",)),
    ]

    def test_mandatory_bin_cannot_be_left(self):
        bins = [
            make_bin("B1", "S1", "paper", 2, mandatory=True),
            make_bin("B2", "S2", "glass", 1),
        ]
        report = plan_late(bins, ["S1", "S2"], self.comps)
        self.assertTrue(report["feasible"])
        self.assertEqual(report["assignment"]["B1"], "P")

    def test_unsplittable_bin_over_capacity_is_infeasible(self):
        # Paper capacity 1, mandatory paper bin 2: no feasible loading.
        comps = [Compartment("P", 1, ("paper",)), Compartment("G", 1, ("glass",))]
        bins = [make_bin("B1", "S1", "paper", 2, mandatory=True)]
        report = plan_late(bins, ["S1"], comps)
        self.assertFalse(report["feasible"])


class TruckPartitionTests(unittest.TestCase):
    def test_partition_fitted_once_and_locked_after_departure(self):
        truck = Truck()
        truck.fit_partition([Compartment("P", 2, ("paper",))])
        with self.assertRaises(PartitionLockedError):
            truck.fit_partition([Compartment("G", 1, ("glass",))])
        truck.depart()
        self.assertTrue(truck.locked)
        with self.assertRaises(PartitionLockedError):
            truck.fit_partition([Compartment("G", 1, ("glass",))])
        self.assertEqual([c.compartment_id for c in truck.compartments], ["P"])

    def test_fit_from_dicts(self):
        truck = Truck()
        truck.fit_partition([{"compartment_id": "P", "capacity": 2, "accepts": ["paper"]}])
        self.assertEqual(truck.compartments[0].capacity, 2)

    def test_depart_without_partition_rejected(self):
        with self.assertRaises(PartitionLockedError):
            Truck().depart()


class EarlyCapacityProofTests(unittest.TestCase):
    def test_proof_detects_capacity_shortfall(self):
        # Reserve the whole paper line for later stations, leaving early with
        # nothing; early cannot complete -> proof fails.
        comps = [Compartment("P", 2, ("paper",))]
        route = ["S1", "S2"]
        bins = [
            make_bin("e1", "S1", "paper", 2),
            make_bin("l1", "S2", "paper", 2),
        ]
        proof = prove_early_capacity(bins, route, comps, split_station="S2")
        self.assertFalse(proof["proved"])
        self.assertEqual(proof["early_completed_committed"], 0)
        self.assertEqual(proof["early_best_possible"], 1)


if __name__ == "__main__":
    unittest.main()
