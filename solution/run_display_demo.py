"""End-to-end demonstration of the static display survey and cart checks.

Runs every scenario named in the task and prints a signable / not-signable
verdict with the evidence behind it.  Pure in-memory, exact Fraction
arithmetic, standard library only.

Scenarios
---------
1. Headline leveling: two setups over stakes 0,1,2 -> profile 0,1,0; the
   rise-1 cart touches exactly at stake 1 and is signable.
2. Altered middle readings -> 0,2,0; the same cart scrapes.
3. Original ground with a half-rise cart -> scrapes.
4. A loop misclosure: the conflicting record group is named, no profile is
   published.
5. A component disconnected from the benchmark: no absolute elevation, never
   assumed zero.
6. An un-walked gap: interpolation is refused and the cart is "unmeasured",
   kept separate from a confirmed scrape.
7. Broken-chainage label: one paper label points at two physical segments.
8. Crest: two mandatory carts over two humps; one stand set is short, two
   stands work, and one verified spanning bridge works; a missing bridge seat
   removes the basis to sign.

A final guard asserts the static model never exceeds 12 stakes / 8 setups /
5 carts.

Run:  python3 solution/run_display_demo.py
"""

from __future__ import annotations

import os
import sys
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from chainage_catalog import Interval, RouteCatalog, Section  # noqa: E402
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
LINE = "=" * 76


def f(v) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, Fraction):
        return str(v.numerator) if v.denominator == 1 else f"{v.numerator}/{v.denominator}"
    return str(v)


def headline_network(mid_a: int, mid_b: int) -> LevelingNetwork:
    stakes = [Stake("0", 0), Stake("1", 1), Stake("2", 2)]
    s1 = LevelSetup("S1", (Reading("0", 4), Reading("1", mid_a)))
    s2 = LevelSetup("S2", (Reading("1", mid_b), Reading("2", 8)))
    return LevelingNetwork(stakes, [s1, s2], Benchmark("0", 0))


def crest_network() -> LevelingNetwork:
    stakes = [Stake(str(i), i) for i in range(5)]
    setups = [
        LevelSetup("A", (Reading("0", 0), Reading("1", 2))),
        LevelSetup("B", (Reading("1", 2), Reading("2", 0))),
        LevelSetup("C", (Reading("2", 0), Reading("3", 2))),
        LevelSetup("D", (Reading("3", 2), Reading("4", 0))),
    ]
    return LevelingNetwork(stakes, setups, Benchmark("0", 0))


def section(title: str) -> None:
    print("\n" + LINE)
    print(title)
    print(LINE)


def main() -> int:
    # ---- 1. headline profile and the signable cart -----------------
    section("1. Headline network (readings 4,5 / 9,8) -> profile 0,1,0")
    net = headline_network(5, 9)
    prof = GroundProfile(net, [Interval(F(0), F(3))])
    print("  elevations:",
          {s: f(net.elevation(s)) for s in ("0", "1", "2")})
    print("  ground at x=1/2 (linear interp. only inside walked cover):",
          f(prof.ground_at(F(1, 2))))
    cart = Cart("M", 0, 2, F(1), mandatory=True)
    res = check_cart(cart, prof)
    print("  rise-1 cart:", res.reason())

    # ---- 2. altered readings -> 0,2,0 scrapes ----------------------
    section("2. Middle readings changed to 6,10 -> profile 0,2,0")
    net2 = headline_network(6, 10)
    prof2 = GroundProfile(net2, [Interval(F(0), F(3))])
    print("  elevations:",
          {s: f(net2.elevation(s)) for s in ("0", "1", "2")})
    print("  rise-1 cart:", check_cart(cart, prof2).reason())

    # ---- 3. half-rise cart on the original ground ------------------
    section("3. Original ground 0,1,0 with a half-rise cart")
    half = Cart("Mhalf", 0, 2, F(1, 2))
    print(" ", check_cart(half, prof).reason())

    # ---- 4. loop misclosure ----------------------------------------
    section("4. Loop misclosure: conflicting records are named, no profile")
    stakes = [Stake("0", 0), Stake("1", 1), Stake("2", 2)]
    bad = LevelingNetwork(
        stakes,
        [
            LevelSetup("S1", (Reading("0", 4), Reading("1", 5))),
            LevelSetup("S2", (Reading("1", 9), Reading("2", 8))),
            LevelSetup("S3", (Reading("2", 2), Reading("0", 3))),
        ],
        Benchmark("0", 0),
    )
    for m in bad.misclosures:
        print("  ", m.describe())
    try:
        GroundProfile(bad, [Interval(F(0), F(3))])
    except ValueError as exc:
        print("   profile refused:", str(exc).split(":")[0])

    # ---- 5. disconnected component never assumed zero --------------
    section("5. Component disconnected from the benchmark")
    stakes5 = stakes + [Stake("3", 3), Stake("4", 4)]
    net5 = LevelingNetwork(
        stakes5,
        [
            LevelSetup("S1", (Reading("0", 4), Reading("1", 5))),
            LevelSetup("S2", (Reading("1", 9), Reading("2", 8))),
            LevelSetup("SB", (Reading("3", 1), Reading("4", 3))),
        ],
        Benchmark("0", 0),
    )
    print("  anchored 0..2:", {s: f(net5.elevation(s)) for s in ("0", "1", "2")})
    print("  disconnected 3,4 absolute elevation:",
          net5.elevation("3"), net5.elevation("4"),
          "(relative 3->4 still known:", f(net5.relative_elevation("3", "4")), ")")

    # ---- 6. un-walked gap is not a confirmed scrape ----------------
    section("6. Un-walked gap: interpolation refused, status unmeasured")
    gapped = GroundProfile(headline_network(5, 9), [Interval(F(0), F(2))])
    print("  ground at x=3/2 across gap (1,2):", gapped.ground_at(F(3, 2)))
    gap_cart = Cart("X", 0, 2, F(10))
    print("  generous-rise cart over the gap:", check_cart(gap_cart, gapped).reason())

    # ---- 7. broken-chainage label -> two physical segments ---------
    section("7. Broken chainage: one paper label, two physical segments")
    cat = RouteCatalog(
        "r1", "L", 10,
        [
            Section("P1", F(0), F(5), F(0)),
            Section("P2", F(5), F(10), F(0)),
        ],
    )
    r = cat.resolve_label_point(2)
    print("  label 2 status:", r["status"],
          "candidates:", [(c["section_id"], f(c["physical"]))
                          for c in r["candidates"]])
    print("  explicit P2 disambiguation -> physical",
          f(cat.resolve_label_point(2, section_id="P2")["candidates"][0]["physical"]))

    # ---- 8. crest: stands shortfall, two stands, spanning bridge ---
    section("8. Crest 0..4 (two humps): two mandatory carts")
    cprof = GroundProfile(crest_network(), [Interval(F(0), F(5))])
    m1 = Cart("M1", 0, 2, F(1), mandatory=True)
    m2 = Cart("M2", 2, 4, F(1), mandatory=True)
    print("  plain:", check_cart(m1, cprof).status, check_cart(m2, cprof).status)
    h1 = required_stand_height(m1, cprof)
    h2 = required_stand_height(m2, cprof)
    print("  required common wheel height per cart:",
          f(h1["common_height"]), f(h2["common_height"]))
    one_set = plan_exhibition([m1, m2], cprof, ExhibitionLimits(max_stands=1))
    print("  stock = 1 stand set ->", one_set.summary().splitlines()[0])
    two_set = plan_exhibition([m1, m2], cprof, ExhibitionLimits(max_stands=2))
    print("  stock = 2 stand sets ->", two_set.summary().splitlines()[0])

    bridge = minimum_clearance_bridge("BR", 0, 4, cprof)
    ver = verify_bridge(bridge, cprof)
    print("  spanning bridge feasible:", ver.feasible,
          "min deck clearance:", f(ver.min_deck_clearance))
    bridged = plan_exhibition(
        [m1, m2], cprof,
        ExhibitionLimits(max_stands=0, max_bridges=1),
        bridges=[bridge],
    )
    print("  one bridge, no stands ->", bridged.summary().splitlines()[0])

    missing = GroundProfile(crest_network(), [Interval(F(0), F(4))])
    bad_bridge = Bridge("BRbad", BridgeSeat(F(0), F(2)), BridgeSeat(F(4), F(2)))
    bad_ver = verify_bridge(bad_bridge, missing)
    print("  seat x=4 turned into a missing point -> feasible:",
          bad_ver.feasible, "|", bad_ver.reason)

    # ---- scale guard ------------------------------------------------
    section("Scale guard (static local model: <=12 stakes, <=8 setups, <=5 carts)")
    n_stakes = len(crest_network().stakes)
    n_setups = len(crest_network().setups)
    n_carts = 2
    print(f"  largest single model uses {n_stakes} stakes, {n_setups} setups, "
          f"{n_carts} carts")
    assert n_stakes <= 12 and n_setups <= 8 and n_carts <= 5
    print("  within limits.")
    print("\nAll scenarios executed. Only static display is checked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
