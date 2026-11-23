"""Tests for the leveling profile and static display-cart modules.

Every numeric scenario named in the task is asserted here, including:
  * the two-setup 0/1/2 network giving profile 0, 1, 0 and the signable cart;
  * the altered middle readings giving 0, 2, 0 (cart scrapes);
  * the half-rise cart scraping on the original ground;
  * a loop misclosure that names the conflicting records;
  * a component disconnected from the benchmark (never assumed zero);
  * an un-walked gap that forbids interpolation and is reported separately
    from a confirmed scrape;
  * the two-carts-over-two-humps crest case, single stand shortfall, the
    spanning bridge, and the bridge becoming baseless when a seat is missing.
"""

from __future__ import annotations

import os
import sys
import unittest
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chainage_catalog import Interval  # noqa: E402
from leveling_profile import (  # noqa: E402
    Benchmark,
    GroundProfile,
    LevelSetup,
    LevelingNetwork,
    Reading,
    Stake,
)
from display_carts import (  # noqa: E402
    Bridge,
    BridgeSeat,
    Cart,
    ExhibitionLimits,
    check_cart,
    minimum_clearance_bridge,
    plan_exhibition,
    required_stand_height,
    verify_bridge,
)


F = Fraction


def stakes_012():
    return [Stake("0", 0), Stake("1", 1), Stake("2", 2)]


def cover_012():
    return [Interval(F(0), F(3))]


def network(read_mid_a, read_mid_b):
    """The task's two-setup network with parameterised middle readings.

    S1 reads stakes (0, 1) = (4, read_mid_a); S2 reads stakes (1, 2) =
    (read_mid_b, 8).  Reading differences equal elevation differences, so the
    original pair (5, 8) yields elevations 0, 1, 0.
    """
    s1 = LevelSetup("S1", (Reading("0", 4), Reading("1", read_mid_a)))
    s2 = LevelSetup("S2", (Reading("1", read_mid_b), Reading("2", 8)))
    return LevelingNetwork(
        stakes_012(), [s1, s2], Benchmark("0", 0)
    )


# ---------------------------------------------------------------------------
# Leveling network: the headline 0,1,0 worked example
# ---------------------------------------------------------------------------


class TestHeadlineNetwork(unittest.TestCase):
    def test_original_readings_give_0_1_0(self):
        net = network(5, 9)  # readings 4,5 and 9,8
        self.assertFalse(net.has_conflict())
        self.assertEqual(net.elevation("0"), F(0))
        self.assertEqual(net.elevation("1"), F(1))
        self.assertEqual(net.elevation("2"), F(0))
        prof = GroundProfile(net, cover_012())
        self.assertEqual(prof.stake_elevation("1"), F(1))
        # Linear interpolation only between adjacent measured stakes.
        self.assertEqual(prof.ground_at(F(1, 2)), F(Fraction(1, 2)))

    def test_altered_middle_readings_give_0_2_0(self):
        net = network(6, 10)  # only the two middle readings change (5->6, 9->10)
        self.assertFalse(net.has_conflict())
        self.assertEqual(
            [net.elevation(s) for s in ("0", "1", "2")], [F(0), F(2), F(0)]
        )

    def test_arbitrary_zero_does_not_enter_elevation(self):
        # Shift S2's readings by a large constant but keep the difference
        # r2-r1 = -1; elevations are unchanged.
        s1 = LevelSetup("S1", (Reading("0", 4), Reading("1", 5)))
        s2 = LevelSetup("S2", (Reading("1", 800), Reading("2", 799)))
        net = LevelingNetwork(stakes_012(), [s1, s2], Benchmark("0", 0))
        self.assertEqual(
            [net.elevation(s) for s in ("0", "1", "2")], [F(0), F(1), F(0)]
        )


# ---------------------------------------------------------------------------
# Static cart check on the headline profile
# ---------------------------------------------------------------------------


class TestHeadlineCart(unittest.TestCase):
    def profile(self, mid_a, mid_b):
        return GroundProfile(network(mid_a, mid_b), cover_012())

    def test_rise_one_cart_signable_and_touches_at_stake_1(self):
        prof = self.profile(5, 9)  # ground 0,1,0
        cart = Cart("M", 0, 2, F(1), mandatory=True)
        res = check_cart(cart, prof)
        self.assertEqual(res.status, "signable")
        # Plate chord is the wheel line (both wheels at ground 0), rise 1.
        # At stake 1 plate=1, ground=1 -> exact touch, clearance 0.
        at1 = next(p for p in res.contact_points if p.x == 1)
        self.assertEqual(at1.clearance, F(0))
        self.assertEqual(res.min_clearance, F(0))

    def test_rise_one_cart_scrapes_on_0_2_0(self):
        prof = self.profile(6, 10)  # ground 0,2,0
        cart = Cart("M", 0, 2, F(1), mandatory=True)
        res = check_cart(cart, prof)
        self.assertEqual(res.status, "scrapes")
        at1 = next(p for p in res.contact_points if p.x == 1)
        self.assertEqual(at1.clearance, F(-1))

    def test_half_rise_cart_scrapes_on_original_ground(self):
        prof = self.profile(5, 9)  # ground 0,1,0
        cart = Cart("M", 0, 2, F(1, 2))
        res = check_cart(cart, prof)
        self.assertEqual(res.status, "scrapes")
        at1 = next(p for p in res.contact_points if p.x == 1)
        self.assertEqual(at1.clearance, F(-Fraction(1, 2)))


# ---------------------------------------------------------------------------
# Misclosure: conflicting records are named
# ---------------------------------------------------------------------------


class TestMisclosure(unittest.TestCase):
    def test_loop_conflict_is_named(self):
        # S1: 0->1 +1; S2: 1->2 -1 (so stake 2 closes back at elevation 0).
        # S3 re-observes 2->0 but with readings forcing +1 rather than 0: a
        # misclosure of one unit whose conflicting records include S3.
        s1 = LevelSetup("S1", (Reading("0", 4), Reading("1", 5)))
        s2 = LevelSetup("S2", (Reading("1", 9), Reading("2", 8)))
        s3 = LevelSetup("S3", (Reading("2", 2), Reading("0", 3)))
        net = LevelingNetwork(stakes_012(), [s1, s2, s3], Benchmark("0", 0))
        self.assertTrue(net.has_conflict())
        # The misclosure magnitude is exactly one unit. Depending on BFS order
        # the contradiction is reported on S2 or S3; both are members of the
        # single conflicting record group, so assert on the group's identity.
        self.assertEqual({abs(m.misclosure) for m in net.misclosures}, {F(1)})
        named = set()
        for m in net.misclosures:
            named.add(m.setup_id)
        self.assertTrue(named & {"S2", "S3"})
        # A profile must not be published over a conflicted network.
        with self.assertRaises(ValueError):
            GroundProfile(net, cover_012())


# ---------------------------------------------------------------------------
# Disconnected component is never assumed zero
# ---------------------------------------------------------------------------


class TestDisconnectedComponent(unittest.TestCase):
    def test_disconnected_stakes_have_no_absolute_elevation(self):
        # Component A: stakes 0,1 anchored. Component B: stakes 3,4 on their
        # own setup, never linked to the benchmark.
        stakes = stakes_012() + [Stake("3", 3), Stake("4", 4)]
        s1 = LevelSetup("S1", (Reading("0", 4), Reading("1", 5)))
        s2 = LevelSetup("S2", (Reading("1", 9), Reading("2", 8)))
        sb = LevelSetup("SB", (Reading("3", 1), Reading("4", 3)))
        net = LevelingNetwork(stakes, [s1, s2, sb], Benchmark("0", 0))
        self.assertIsNone(net.elevation("3"))
        self.assertIsNone(net.elevation("4"))
        # Relative difference inside the disconnected component is still a
        # fact: readings 1 (stake 3) and 3 (stake 4) give r4-r3 = +2.
        self.assertEqual(net.relative_elevation("3", "4"), F(2))
        prof = GroundProfile(net, [Interval(F(0), F(5))])
        # The profile simply has no height for the disconnected stakes.
        self.assertNotIn("3", prof.measured_stakes())


# ---------------------------------------------------------------------------
# Un-walked gap: no interpolation, kept separate from scraping
# ---------------------------------------------------------------------------


class TestUnwalkedGap(unittest.TestCase):
    def test_no_interpolation_across_gap(self):
        net = network(5, 9)
        # Coverage stops at stake 1 and resumes only at stake 2: the open
        # interval (1,2) was never walked.
        prof = GroundProfile(net, [Interval(F(0), F(2))])
        self.assertEqual(prof.ground_at(F(1)), F(1))
        # A point strictly inside the un-walked gap cannot be answered.
        self.assertIsNone(prof.ground_at(F(3, 2)))

    def test_cart_spanning_gap_is_unmeasured_not_scrape(self):
        net = network(5, 9)
        prof = GroundProfile(net, [Interval(F(0), F(2))])  # gap (1,2)
        # Add a stake beyond coverage so the span genuinely crosses unknown ground.
        cart = Cart("X", 0, 2, F(10))  # generous rise, but gap dominates
        res = check_cart(cart, prof)
        self.assertEqual(res.status, "unmeasured")
        self.assertNotEqual(res.status, "scrapes")
        self.assertTrue(res.unmeasured_gaps)


# ---------------------------------------------------------------------------
# Crest: two mandatory carts over two humps
# ---------------------------------------------------------------------------


def crest_stakes():
    # ground 0,2,0,2,0 at x=0..4
    return [Stake(str(i), i) for i in range(5)]


def crest_network():
    # Reading differences track elevation differences: +2 up, -2 down.
    setups = [
        LevelSetup("A", (Reading("0", 0), Reading("1", 2))),    # +2
        LevelSetup("B", (Reading("1", 2), Reading("2", 0))),    # -2
        LevelSetup("C", (Reading("2", 0), Reading("3", 2))),    # +2
        LevelSetup("D", (Reading("3", 2), Reading("4", 0))),    # -2
    ]
    return LevelingNetwork(crest_stakes(), setups, Benchmark("0", 0))


def crest_profile():
    return GroundProfile(crest_network(), [Interval(F(0), F(5))])


class TestCrestCarts(unittest.TestCase):
    def test_ground_profile_is_two_humps(self):
        prof = crest_profile()
        self.assertEqual(
            [prof.stake_elevation(str(i)) for i in range(5)],
            [F(0), F(2), F(0), F(2), F(0)],
        )

    def test_both_carts_scrape_plain(self):
        prof = crest_profile()
        m1 = Cart("M1", 0, 2, F(1), mandatory=True)
        m2 = Cart("M2", 2, 4, F(1), mandatory=True)
        self.assertEqual(check_cart(m1, prof).status, "scrapes")
        self.assertEqual(check_cart(m2, prof).status, "scrapes")

    def test_each_cart_needs_a_stand_one_set_short(self):
        prof = crest_profile()
        m1 = Cart("M1", 0, 2, F(1), mandatory=True)
        m2 = Cart("M2", 2, 4, F(1), mandatory=True)
        h1 = required_stand_height(m1, prof)
        h2 = required_stand_height(m2, prof)
        self.assertTrue(h1["feasible"] and h2["feasible"])
        # Both need common wheel height 1 (plate at 2 clears the hump top).
        self.assertEqual(h1["common_height"], F(1))
        self.assertEqual(h2["common_height"], F(1))
        limits = ExhibitionLimits(max_stands=1)
        plan = plan_exhibition([m1, m2], prof, limits)
        self.assertFalse(plan.feasible)

    def test_two_stands_place_both_carts_sharing_endpoint(self):
        prof = crest_profile()
        m1 = Cart("M1", 0, 2, F(1), mandatory=True)
        m2 = Cart("M2", 2, 4, F(1), mandatory=True)
        limits = ExhibitionLimits(max_stands=2)
        plan = plan_exhibition([m1, m2], prof, limits)
        self.assertTrue(plan.feasible, plan.reason)
        self.assertEqual(len(plan.stands_used), 2)
        # Endpoint at x=2 is shared, not an overlap.
        spans = sorted((p.span for p in plan.placed), key=lambda iv: iv.start)
        self.assertEqual(spans[0].end, spans[1].start)

    def test_spanning_bridge_clears_both_humps(self):
        prof = crest_profile()
        bridge = minimum_clearance_bridge("BR", 0, 4, prof)
        ver = verify_bridge(bridge, prof)
        self.assertTrue(ver.feasible, ver.reason)
        # Horizontal deck at ground max 2; clearance 0 at both hump crests.
        self.assertEqual(ver.min_deck_clearance, F(0))
        m1 = Cart("M1", 0, 2, F(1), mandatory=True)
        m2 = Cart("M2", 2, 4, F(1), mandatory=True)
        plan = plan_exhibition(
            [m1, m2], prof,
            ExhibitionLimits(max_stands=0, max_bridges=1),
            bridges=[bridge],
        )
        self.assertTrue(plan.feasible, plan.reason)
        self.assertEqual(len(plan.bridges_used), 1)
        self.assertEqual(plan.stands_used, ())

    def test_bridge_with_missing_seat_has_no_basis(self):
        # Recreate the crest profile but drop stake 4 from coverage: the right
        # seat is unmeasured, so the 0..4 bridge cannot be signed.
        net = crest_network()
        prof = GroundProfile(net, [Interval(F(0), F(4))])  # x=4 excluded
        bridge = Bridge(
            "BR",
            BridgeSeat(F(0), F(2)),
            BridgeSeat(F(4), F(2)),
        )
        ver = verify_bridge(bridge, prof)
        self.assertFalse(ver.feasible)
        self.assertIn(F(4), ver.unmeasured_seats)


# ---------------------------------------------------------------------------
# Optional-cart scheduling: minimum cancellations and binding segment
# ---------------------------------------------------------------------------


class TestScheduling(unittest.TestCase):
    def _profile(self):
        # stakes 0..4, ground 0,1,0,1,0: room for non-overlapping carts.
        stakes = [Stake(str(i), i) for i in range(5)]
        setups = [
            LevelSetup("A", (Reading("0", 0), Reading("1", 1))),   # +1
            LevelSetup("B", (Reading("1", 1), Reading("2", 0))),   # -1
            LevelSetup("C", (Reading("2", 0), Reading("3", 1))),   # +1
            LevelSetup("D", (Reading("3", 1), Reading("4", 0))),   # -1
        ]
        net = LevelingNetwork(stakes, setups, Benchmark("0", 0))
        return GroundProfile(net, [Interval(F(0), F(5))])

    def test_minimum_cancellations_and_binding_segment(self):
        prof = self._profile()
        mandatory = Cart("M", 0, 2, F(1), mandatory=True)  # touches at x=1
        # Two optional carts that can each use the free right half; they
        # compete with each other, so exactly one is cancelled.
        o1 = Cart("O1", 2, 4, F(1))
        o2 = Cart("O2", 2, 4, F(1))
        plan = plan_exhibition(
            [mandatory, o1, o2], prof, ExhibitionLimits(max_stands=0)
        )
        self.assertTrue(plan.feasible, plan.reason)
        self.assertEqual(len(plan.cancelled), 1)
        self.assertEqual(plan.min_clearance_overall, F(0))
        self.assertIn("M", plan.min_clearance_source)
        # Endpoint x=2 shared between the mandatory and the kept optional cart.
        spans = sorted((p.span for p in plan.placed), key=lambda iv: iv.start)
        self.assertEqual(spans[0].end, spans[1].start)

    def test_mandatory_unmeasured_is_reported_not_assumed(self):
        # A mandatory cart whose only span crosses an un-walked gap has no
        # signable placement; the reason names the gap rather than a scrape.
        stakes = [Stake("0", 0), Stake("1", 1), Stake("2", 2)]
        s1 = LevelSetup("S1", (Reading("0", 4), Reading("1", 5)))
        s2 = LevelSetup("S2", (Reading("1", 9), Reading("2", 8)))
        net = LevelingNetwork(stakes, [s1, s2], Benchmark("0", 0))
        prof = GroundProfile(net, [Interval(F(0), F(2))])  # gap (1,2)
        m = Cart("M", 0, 2, F(10), mandatory=True)
        plan = plan_exhibition([m], prof, ExhibitionLimits())
        self.assertFalse(plan.feasible)
        self.assertIn("no signable placement", plan.reason)


class TestBrokenChainageLabel(unittest.TestCase):
    """One paper label points at two physical segments; both must be carried."""

    def test_same_label_two_physical_segments(self):
        from chainage_catalog import RouteCatalog, Section
        cat = RouteCatalog(
            "r1", "L", 10,
            [
                Section("P1", F(0), F(5), F(0)),
                Section("P2", F(5), F(10), F(0)),  # labels restart -> repeats
            ],
        )
        r = cat.resolve_label_point(2)
        self.assertEqual(r["status"], "ambiguous")
        phys = sorted(c["physical"] for c in r["candidates"])
        self.assertEqual(phys, [F(2), F(7)])
        # Explicit section disambiguates; the survey must keep both identities.
        self.assertEqual(
            cat.resolve_label_point(2, section_id="P2")["candidates"][0]["physical"],
            F(7),
        )


class TestStandAndBridgeResourceLimits(unittest.TestCase):
    def test_stand_is_locked_to_one_cart_and_cannot_be_reused(self):
        prof = crest_profile()
        m1 = Cart("M1", 0, 2, F(1), mandatory=True)
        m2 = Cart("M2", 2, 4, F(1), mandatory=True)
        # One physical stand set cannot be shared: the second cart has none.
        plan = plan_exhibition(
            [m1, m2], prof, ExhibitionLimits(max_stands=1, max_bridges=0)
        )
        self.assertFalse(plan.feasible)
        # And a stand, when allocated, names the exact wheels it is locked to.
        ok = plan_exhibition(
            [m1, m2], prof, ExhibitionLimits(max_stands=2)
        )
        self.assertTrue(ok.feasible)
        owners = {s.cart_id for s in ok.stands_used}
        self.assertEqual(owners, {"M1", "M2"})
        for s in ok.stands_used:
            cart = next(p for p in ok.placed if p.cart_id == s.cart_id)
            self.assertEqual(
                s.footprint(),
                tuple(sorted((cart.wheel_a, cart.wheel_b))),
            )

    def test_bridge_weight_and_roadside_limits(self):
        prof = crest_profile()
        bridge = minimum_clearance_bridge("BR", 0, 4, prof)
        heavy = Bridge(
            "BR", bridge.seat_a, bridge.seat_b,
            weight=F(5), roadside_footprint=F(3),
        )
        m1 = Cart("M1", 0, 2, F(1), mandatory=True)
        m2 = Cart("M2", 2, 4, F(1), mandatory=True)
        # Weight cap below the bridge's weight rejects the bridge option.
        plan = plan_exhibition(
            [m1, m2], prof,
            ExhibitionLimits(max_bridges=1, max_bridge_weight=F(2)),
            bridges=[heavy],
        )
        self.assertFalse(plan.feasible)
        # A permissive cap admits it.
        plan2 = plan_exhibition(
            [m1, m2], prof,
            ExhibitionLimits(max_bridges=1, max_bridge_weight=F(5),
                             max_roadside=F(3)),
            bridges=[heavy],
        )
        self.assertTrue(plan2.feasible, plan2.reason)


if __name__ == "__main__":
    unittest.main()
