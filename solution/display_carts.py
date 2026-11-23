"""Static scale-model display carts: plate clearance, stands, bridges.

A cart rests statically on two point wheels at given horizontal positions.
Its bottom plate is the straight line joining the two wheel contact points,
raised vertically by a fixed ``plate_rise``.  The plate is acceptable only
where it never runs below the measured ground polyline; touching it exactly
(clearance zero) is allowed.

Three outcomes are kept deliberately distinct:

* ``signable``      - every point of the plate over the span has clearance >= 0;
* ``scrapes``       - at least one *measured* point goes below the ground;
* ``unmeasured``    - the span crosses a gap nobody walked/measured, so the
  question cannot be answered.  An unmeasured span is never silently treated
  as safe, and it is never reported as a confirmed scrape.

Ground heights come from :class:`leveling_profile.GroundProfile`, which
already refuses to interpolate across an un-walked gap.

Two remediation devices are modelled, and they are not interchangeable:

* :class:`EqualHeightStand` - a set of jacks locked to the named wheels of
  one cart, lifting both contact points to one common height.  A stand set
  stays under its own cart for the whole exhibition and cannot be moved to
  another cart;
* :class:`Bridge` - a temporary crossing whose two feet land on already
  verified bridge seats.  The deck is the straight line between the seats
  and must clear the measured ground everywhere between them.  A bridge may
  not be founded on an unmeasured point.

Only the static display is checked; no rolling/driving is simulated.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from itertools import combinations, product
from typing import Optional, Sequence

from chainage_catalog import Interval, as_fraction, merge_intervals
from leveling_profile import GroundProfile


class DisplayError(ValueError):
    """Base error for display-cart modelling."""


# ---------------------------------------------------------------------------
# Carts and the static check
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Cart:
    """A two-wheel display cart.

    ``plate_rise`` is how much the bottom plate sits above the straight line
    through the two wheel contact points.  Optional carts carry several
    candidate placements; a mandatory cart has exactly one.
    """

    cart_id: str
    wheel_a: Fraction
    wheel_b: Fraction
    plate_rise: Fraction
    mandatory: bool = False

    def __post_init__(self):
        object.__setattr__(self, "wheel_a", as_fraction(self.wheel_a))
        object.__setattr__(self, "wheel_b", as_fraction(self.wheel_b))
        object.__setattr__(self, "plate_rise", as_fraction(self.plate_rise))
        if self.wheel_a == self.wheel_b:
            raise DisplayError(f"cart {self.cart_id!r} wheels must be distinct")

    @property
    def span(self) -> Interval:
        lo, hi = sorted((self.wheel_a, self.wheel_b))
        return Interval(lo, hi)

    def relocated(self, wheel_a, wheel_b) -> "Cart":
        return Cart(
            self.cart_id,
            as_fraction(wheel_a),
            as_fraction(wheel_b),
            self.plate_rise,
            self.mandatory,
        )


@dataclass(frozen=True)
class ClearancePoint:
    x: Fraction
    ground: Fraction
    plate: Fraction

    @property
    def clearance(self) -> Fraction:
        return self.plate - self.ground


@dataclass(frozen=True)
class CheckResult:
    cart_id: str
    status: str  # "signable" | "scrapes" | "unmeasured"
    wheel_a: Fraction
    wheel_b: Fraction
    plate_rise: Fraction
    min_clearance: Optional[Fraction]
    contact_points: tuple[ClearancePoint, ...]
    unmeasured_gaps: tuple[Interval, ...]
    binding_segment: Optional[tuple[Fraction, Fraction]]

    @property
    def signable(self) -> bool:
        return self.status == "signable"

    def reason(self) -> str:
        if self.status == "signable":
            seg = self.binding_segment
            tail = (
                f", tightest on segment {_f(seg[0])}->{_f(seg[1])}"
                if seg is not None
                else ""
            )
            return (
                f"signable: minimum clearance {_f(self.min_clearance)}{tail}"
            )
        if self.status == "scrapes":
            bad = [p for p in self.contact_points if p.clearance < 0]
            names = ", ".join(f"x={_f(p.x)} gap={_f(p.clearance)}" for p in bad)
            return f"scrapes at measured point(s): {names}"
        gaps = ", ".join(f"[{_f(g.start)}, {_f(g.end)})" for g in self.unmeasured_gaps)
        return f"unmeasured span across gap(s): {gaps} - cannot decide"


def _f(v: Fraction) -> str:
    return str(v.numerator) if v.denominator == 1 else f"{v.numerator}/{v.denominator}"


def check_cart(cart: Cart, profile: GroundProfile) -> CheckResult:
    """Check one cart against the measured ground.

    The plate is a straight chord between the *wheel contact elevations*,
    plus ``plate_rise``.  The ground polyline between two measured stakes is
    linear, so the minimum plate-vs-ground clearance on each measured segment
    occurs at a segment endpoint; measured stakes therefore suffice to prove
    clearance, and no unmeasured point is invented.
    """
    lo, hi = sorted((cart.wheel_a, cart.wheel_b))
    ga = profile.ground_at(cart.wheel_a)
    gb = profile.ground_at(cart.wheel_b)

    # Known wheel contacts seed the proven points; an unmeasured contact is
    # carried separately rather than immediately condemning the whole span, so
    # the reported gap can be located precisely.
    pts: list[tuple[Fraction, Fraction]] = []
    contact_gaps: list[Fraction] = []
    if ga is not None:
        pts.append((cart.wheel_a, ga))
    else:
        contact_gaps.append(cart.wheel_a)
    if gb is not None:
        pts.append((cart.wheel_b, gb))
    else:
        contact_gaps.append(cart.wheel_b)

    missing_vertices: list[Fraction] = []
    for st in profile.network.stakes:
        sx = st.x
        if lo < sx < hi:
            g = profile.ground_at(sx)
            if g is None:
                missing_vertices.append(sx)
                continue
            pts.append((sx, g))

    # An un-walked gap sits between consecutive known x positions whenever
    # they are not inside one walked interval.
    pts.sort(key=lambda p: p[0])
    unmeasured: list[Interval] = []
    for (x1, _), (x2, _) in zip(pts, pts[1:]):
        if not profile._same_cover(x1, x2):
            unmeasured.append(Interval(x1, x2))
    # An unmeasured contact point brackets the unknown stretch at the edge.
    if contact_gaps:
        known_x = [p[0] for p in pts]
        for x in contact_gaps:
            inner = max((k for k in known_x if k < x), default=None)
            if inner is not None:
                unmeasured.append(Interval(inner, x))
            elif pts:
                unmeasured.append(Interval(x, min(known_x)))
            else:
                unmeasured.append(Interval(lo, hi))
    unmeasured = list(merge_intervals(unmeasured))
    if (missing_vertices or contact_gaps) and not unmeasured:
        unmeasured = [Interval(lo, hi)]
    if contact_gaps or not pts:
        # A wheel contact (or every point) is unmeasured: the plate chord is
        # unknown, so clearance cannot be proven. Report the located gap.
        return CheckResult(
            cart.cart_id, "unmeasured", cart.wheel_a, cart.wheel_b,
            cart.plate_rise, None, (), tuple(unmeasured), None,
        )

    ga, gb = profile.ground_at(cart.wheel_a), profile.ground_at(cart.wheel_b)

    def chord(x: Fraction) -> Fraction:
        t = (x - lo) / (hi - lo)
        return ga + t * (gb - ga)

    contacts: list[ClearancePoint] = []
    min_cl: Optional[Fraction] = None
    binding: Optional[tuple[Fraction, Fraction]] = None
    for x, ground in pts:
        plate = chord(x) + cart.plate_rise
        contacts.append(ClearancePoint(x, ground, plate))
        cl = plate - ground
        if min_cl is None or cl < min_cl:
            min_cl = cl
            binding = self_segment(pts, x)

    if unmeasured:
        status = "unmeasured"
    elif min_cl < 0:
        status = "scrapes"
    else:
        status = "signable"
    return CheckResult(
        cart.cart_id,
        status,
        cart.wheel_a,
        cart.wheel_b,
        cart.plate_rise,
        min_cl if not unmeasured else None,
        tuple(contacts),
        tuple(unmeasured),
        binding if status == "signable" else None,
    )


def self_segment(
    pts: Sequence[tuple[Fraction, Fraction]], x: Fraction
) -> Optional[tuple[Fraction, Fraction]]:
    """The ground segment (pair of stakes) containing point x."""
    for (x1, _), (x2, _) in zip(pts, pts[1:]):
        if x1 <= x <= x2:
            return (x1, x2)
    return None


# ---------------------------------------------------------------------------
# Equal-height stands
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StandAssignment:
    """A stand set locked to the two named wheels of one cart."""

    stand_id: str
    cart_id: str
    wheel_a: Fraction
    wheel_b: Fraction
    common_height: Fraction  # the height both contact points are lifted to

    def footprint(self) -> tuple[Fraction, Fraction]:
        return tuple(sorted((self.wheel_a, self.wheel_b)))  # type: ignore[return-value]


def required_stand_height(cart: Cart, profile: GroundProfile) -> dict:
    """Smallest common wheel height that makes the plate clear the ground.

    Both wheels are jacked to one height ``H``; the plate chord is then
    horizontal at ``H + plate_rise``.  It must dominate every measured
    ground point in the span.  Returns a reason if any needed ground point is
    unmeasured (a stand cannot be justified over a gap either).
    """
    lo, hi = sorted((cart.wheel_a, cart.wheel_b))
    heights: list[tuple[Fraction, Fraction]] = []
    for st in profile.network.stakes:
        if lo <= st.x <= hi:
            g = profile.ground_at(st.x)
            if g is None:
                return {
                    "cart_id": cart.cart_id,
                    "feasible": False,
                    "reason": f"unmeasured ground at x={_f(st.x)}",
                }
            heights.append((st.x, g))
    ga = profile.ground_at(cart.wheel_a)
    gb = profile.ground_at(cart.wheel_b)
    if ga is None or gb is None:
        return {"cart_id": cart.cart_id, "feasible": False,
                "reason": "a wheel contact point is unmeasured"}
    ground_max = max(g for _, g in heights)
    common = ground_max - cart.plate_rise
    # Wheels must not be lowered below their own contact ground.
    common = max(common, ga, gb)
    return {
        "cart_id": cart.cart_id,
        "feasible": True,
        "common_height": common,
        "plate_height": common + cart.plate_rise,
        "ground_max": ground_max,
    }


# ---------------------------------------------------------------------------
# Bridges
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BridgeSeat:
    x: Fraction
    deck_height: Fraction  # deck elevation at this seat


@dataclass(frozen=True)
class Bridge:
    bridge_id: str
    seat_a: BridgeSeat
    seat_b: BridgeSeat
    weight: Fraction = Fraction(1)
    roadside_footprint: Fraction = Fraction(1)

    def __post_init__(self):
        object.__setattr__(self, "weight", as_fraction(self.weight))
        object.__setattr__(self, "roadside_footprint", as_fraction(self.roadside_footprint))
        if self.seat_a.x == self.seat_b.x:
            raise DisplayError("bridge needs two distinct seats")

    @property
    def span(self) -> Interval:
        lo, hi = sorted((self.seat_a.x, self.seat_b.x))
        return Interval(lo, hi)

    def deck_at(self, x: Fraction) -> Fraction:
        lo = min(self.seat_a.x, self.seat_b.x)
        hi = max(self.seat_a.x, self.seat_b.x)
        t = (x - lo) / (hi - lo)
        h_lo = self.seat_a.deck_height if self.seat_a.x == lo else self.seat_b.deck_height
        h_hi = self.seat_b.deck_height if self.seat_b.x == hi else self.seat_a.deck_height
        return h_lo + t * (h_hi - h_lo)


@dataclass(frozen=True)
class BridgeVerification:
    bridge_id: str
    feasible: bool
    min_deck_clearance: Optional[Fraction]
    blocking_points: tuple[tuple[Fraction, Fraction, Fraction], ...]
    unmeasured_seats: tuple[Fraction, ...]
    reason: Optional[str]


def verify_bridge(bridge: Bridge, profile: GroundProfile) -> BridgeVerification:
    """Prove the straight deck clears measured ground between its two seats.

    Both seats must sit on measured/verified points.  Every measured stake
    between them is tested against the straight deck; an unmeasured seat or
    an un-walked gap inside the span makes the bridge unjustifiable.
    """
    lo, hi = bridge.span.start, bridge.span.end
    unmeasured_seats: list[Fraction] = []
    for seat in (bridge.seat_a, bridge.seat_b):
        if profile.ground_at(seat.x) is None:
            unmeasured_seats.append(seat.x)
    if unmeasured_seats:
        return BridgeVerification(
            bridge.bridge_id, False, None, (), tuple(unmeasured_seats),
            "bridge seat on an unmeasured point - no basis to sign",
        )

    blocking: list[tuple[Fraction, Fraction, Fraction]] = []
    min_cl: Optional[Fraction] = None
    pts: list[Fraction] = []
    for st in profile.network.stakes:
        if lo <= st.x <= hi:
            g = profile.ground_at(st.x)
            if g is None:
                return BridgeVerification(
                    bridge.bridge_id, False, None, (), (st.x,),
                    f"un-walked gap at x={_f(st.x)} inside bridge span",
                )
            pts.append(st.x)
    pts.sort()
    for x in pts:
        ground = profile.ground_at(x)
        deck = bridge.deck_at(x)
        cl = deck - ground
        if min_cl is None or cl < min_cl:
            min_cl = cl
        if cl < 0:
            blocking.append((x, ground, deck))
    if blocking:
        return BridgeVerification(
            bridge.bridge_id, False, min_cl, tuple(blocking), (),
            "deck runs below measured ground",
        )
    return BridgeVerification(
        bridge.bridge_id, True, min_cl, (), (), None
    )


def minimum_clearance_bridge(
    bridge_id: str, seat_x_a: Fraction, seat_x_b: Fraction, profile: GroundProfile
) -> Bridge:
    """Build the lowest signable straight deck between two measured seats.

    Deck heights are chosen so the chord just dominates the measured ground.
    The simplest provable choice is a horizontal deck at the max ground
    height in the span; the seats are grounded on the measured points and
    raised to that deck level.
    """
    lo, hi = sorted((as_fraction(seat_x_a), as_fraction(seat_x_b)))
    if profile.ground_at(lo) is None or profile.ground_at(hi) is None:
        raise DisplayError("cannot seat a bridge on an unmeasured point")
    ground_max = None
    for st in profile.network.stakes:
        if lo <= st.x <= hi:
            g = profile.ground_at(st.x)
            if g is None:
                raise DisplayError(
                    f"un-walked gap at x={_f(st.x)} inside proposed bridge"
                )
            ground_max = g if ground_max is None else max(ground_max, g)
    return Bridge(
        bridge_id,
        BridgeSeat(lo, ground_max),
        BridgeSeat(hi, ground_max),
    )


# ---------------------------------------------------------------------------
# Exhibition scheduling
# ---------------------------------------------------------------------------


@dataclass
class ExhibitionLimits:
    max_stands: int = 0
    max_bridges: int = 0
    max_bridge_weight: Fraction = None  # type: ignore[assignment]
    max_roadside: Fraction = None  # type: ignore[assignment]
    max_carts: Optional[int] = None

    def __post_init__(self):
        if self.max_bridge_weight is not None:
            self.max_bridge_weight = as_fraction(self.max_bridge_weight)
        if self.max_roadside is not None:
            self.max_roadside = as_fraction(self.max_roadside)


@dataclass
class PlacedCart:
    cart_id: str
    mandatory: bool
    wheel_a: Fraction
    wheel_b: Fraction
    status: str
    min_clearance: Optional[Fraction]
    binding_segment: Optional[tuple[Fraction, Fraction]]
    stand: Optional[StandAssignment]
    on_bridge: Optional[str]

    @property
    def span(self) -> Interval:
        lo, hi = sorted((self.wheel_a, self.wheel_b))
        return Interval(lo, hi)


@dataclass
class ExhibitionPlan:
    feasible: bool
    reason: Optional[str]
    placed: tuple[PlacedCart, ...]
    cancelled: tuple[str, ...]
    stands_used: tuple[StandAssignment, ...]
    bridges_used: tuple[Bridge, ...]
    min_clearance_overall: Optional[Fraction]
    min_clearance_source: Optional[str]

    def summary(self) -> str:
        if not self.feasible:
            return f"infeasible: {self.reason}"
        lines = [
            f"feasible: kept {len(self.placed)} cart(s), cancelled "
            f"{len(self.cancelled)} ({', '.join(self.cancelled) or 'none'})",
            f"  stands used: {len(self.stands_used)}, bridges used: "
            f"{len(self.bridges_used)}",
        ]
        for p in self.placed:
            tag = "MANDATORY" if p.mandatory else "optional"
            dev = (
                f" stand={p.stand.stand_id}" if p.stand else
                (f" bridge={p.on_bridge}" if p.on_bridge else " plain")
            )
            lines.append(
                f"  {p.cart_id} [{tag}] wheels {_f(p.wheel_a)},{_f(p.wheel_b)}"
                f" status={p.status} min_clearance="
                f"{_f(p.min_clearance) if p.min_clearance is not None else 'n/a'}"
                f"{dev}"
            )
        if self.min_clearance_source:
            lines.append(
                f"  tightest clearance {_f(self.min_clearance_overall)} from "
                f"{self.min_clearance_source}"
            )
        return "\n".join(lines)


def _no_overlap(intervals: Sequence[Interval]) -> bool:
    """Occupied intervals may share endpoints but never overlap internally."""
    ordered = sorted(intervals, key=lambda iv: iv.start)
    for a, b in zip(ordered, ordered[1:]):
        if a.start < b.end and b.start < a.end:
            return False
    return True


def plan_exhibition(
    carts: Sequence[Cart],
    profile: GroundProfile,
    limits: ExhibitionLimits,
    candidate_placements: Optional[dict[str, Sequence[tuple[Fraction, Fraction]]]] = None,
    bridges: Sequence[Bridge] = (),
) -> ExhibitionPlan:
    """Find the feasible plan that keeps the most carts (fewest cancellations).

    Mandatory carts must be placed or the whole exhibition is infeasible.
    Optional carts may use any of their candidate placements.  Occupied road
    intervals may share endpoints but never overlap internally.  Stands and
    bridges are consumed resources limited by ``limits``; a stand is bound to
    one cart and never reused.
    """
    candidate_placements = candidate_placements or {}
    verified_bridges: list[Bridge] = []
    for br in bridges:
        ver = verify_bridge(br, profile)
        if ver.feasible:
            verified_bridges.append(br)

    mandatory = [c for c in carts if c.mandatory]
    optional = [c for c in carts if not c.mandatory]

    # Pre-verify each offered bridge once; its proven deck clearance travels
    # with the options that use it.
    bridge_clearance: dict[str, Fraction] = {}
    for br in verified_bridges:
        ver = verify_bridge(br, profile)
        bridge_clearance[br.bridge_id] = ver.min_deck_clearance

    # Build the option list per cart. Each option is a concrete placement plus
    # the device (if any) needed to make it signable.
    def options_for(cart: Cart) -> list[dict]:
        placements = (
            candidate_placements.get(cart.cart_id)
            or [(cart.wheel_a, cart.wheel_b)]
        )
        opts: list[dict] = []
        for wa, wb in placements:
            placed_cart = cart.relocated(wa, wb)
            res = check_cart(placed_cart, profile)
            if res.status == "signable":
                opts.append({
                    "cart": placed_cart, "result": res,
                    "stand": None, "bridge": None,
                })
            elif res.status == "scrapes":
                need = required_stand_height(placed_cart, profile)
                if need["feasible"]:
                    opts.append({
                        "cart": placed_cart, "result": res,
                        "stand": need, "bridge": None,
                    })
            # unmeasured placements yield no signable option at all
        # Bridge-supported options: a cart whose span sits inside a verified
        # bridge may stand on the deck (deck is its ground).
        for br in verified_bridges:
            for wa, wb in placements:
                if br.span.start <= min(wa, wb) and max(wa, wb) <= br.span.end:
                    opts.append({
                        "cart": cart.relocated(wa, wb), "result": None,
                        "stand": None, "bridge": br,
                        "bridge_clearance": bridge_clearance[br.bridge_id],
                    })
        return opts

    mandatory_options = {c.cart_id: options_for(c) for c in mandatory}
    for c in mandatory:
        if not mandatory_options[c.cart_id]:
            return ExhibitionPlan(
                False,
                f"mandatory cart {c.cart_id} has no signable placement "
                "(scrape or unmeasured span that no available device fixes)",
                (), tuple(x.cart_id for x in carts), (), (), None, None,
            )

    optional_options = {c.cart_id: options_for(c) for c in optional}

    best: Optional[ExhibitionPlan] = None
    mand_keys = [c.cart_id for c in mandatory]
    opt_keys = [c.cart_id for c in optional]

    # Enumerate mandatory placements, then greedily/exhaustively add optionals.
    for mand_choice in product(*(mandatory_options[k] for k in mand_keys)):
        chosen = {k: mand_choice[i] for i, k in enumerate(mand_keys)}
        feasible_mand, reason = _assemble(
            chosen, mand_keys, limits, verified_bridges
        )
        if not feasible_mand:
            continue
        # Exhaustively choose a subset of optional carts (max kept).
        opt_lists = [optional_options[k] for k in opt_keys]
        for keep_n in range(len(opt_keys), -1, -1):
            found = _best_optional(
                opt_keys, opt_lists, chosen, mand_keys, limits,
                verified_bridges, keep_n,
            )
            if found is not None:
                plan = _build_plan(found, carts, limits, verified_bridges)
                if best is None or len(plan.placed) > len(best.placed):
                    best = plan
                break
    if best is None:
        return ExhibitionPlan(
            False,
            "mandatory carts cannot be co-located within the device/road limits",
            (), tuple(c.cart_id for c in carts), (), (), None, None,
        )
    return best


def _assemble(chosen: dict, keys: Sequence[str], limits: ExhibitionLimits,
              verified_bridges: Sequence[Bridge]) -> tuple[bool, Optional[str]]:
    intervals = []
    stands = 0
    bridges: set[str] = set()
    for k in keys:
        opt = chosen[k]
        cart = opt["cart"]
        intervals.append(
            Interval(min(cart.wheel_a, cart.wheel_b), max(cart.wheel_a, cart.wheel_b))
        )
        if opt["stand"] is not None:
            stands += 1
        if opt["bridge"] is not None:
            bridges.add(opt["bridge"].bridge_id)
    if not _no_overlap(intervals):
        return False, "internal road overlap"
    if stands > limits.max_stands:
        return False, "not enough stands"
    if len(bridges) > limits.max_bridges:
        return False, "not enough bridges"
    for br in verified_bridges:
        if br.bridge_id in bridges:
            if limits.max_bridge_weight is not None and br.weight > limits.max_bridge_weight:
                return False, "bridge over weight limit"
            if limits.max_roadside is not None and br.roadside_footprint > limits.max_roadside:
                return False, "bridge over roadside footprint limit"
    return True, None


def _best_optional(opt_keys, opt_lists, chosen, mand_keys, limits,
                   verified_bridges, keep_n):
    if keep_n == 0:
        return dict(chosen)
    # Try every subset of size keep_n and every placement of each member.
    for subset in combinations(range(len(opt_keys)), keep_n):
        sub_keys = [opt_keys[i] for i in subset]
        sub_lists = [opt_lists[i] for i in subset]
        if any(not opts for opts in sub_lists):
            continue
        for combo in product(*sub_lists):
            trial = dict(chosen)
            for i, k in enumerate(sub_keys):
                trial[k] = combo[i]
            ok, _ = _assemble(trial, mand_keys + sub_keys, limits, verified_bridges)
            if ok:
                return trial
    return None


def _build_plan(chosen: dict, all_carts: Sequence[Cart],
                limits: ExhibitionLimits,
                verified_bridges: Sequence[Bridge]) -> ExhibitionPlan:
    placed: list[PlacedCart] = []
    stands: list[StandAssignment] = []
    bridges_used: list[Bridge] = []
    stand_index = 0
    kept_ids = set(chosen)
    for cart in all_carts:
        if cart.cart_id not in kept_ids:
            continue
        opt = chosen[cart.cart_id]
        pc = opt["cart"]
        if opt["bridge"] is not None:
            br = opt["bridge"]
            if br not in bridges_used:
                bridges_used.append(br)
            # On a verified deck the plate is clear; report the proven deck
            # clearance rather than re-reading a ground profile.
            placed.append(PlacedCart(
                pc.cart_id, pc.mandatory, pc.wheel_a, pc.wheel_b,
                "signable", opt["bridge_clearance"],
                (br.span.start, br.span.end), None, br.bridge_id,
            ))
            continue
        res = opt["result"]
        stand = None
        if opt["stand"] is not None:
            stand_index += 1
            need = opt["stand"]
            stand = StandAssignment(
                f"ST{stand_index}", pc.cart_id, pc.wheel_a, pc.wheel_b,
                need["common_height"],
            )
            stands.append(stand)
        placed.append(PlacedCart(
            pc.cart_id, pc.mandatory, pc.wheel_a, pc.wheel_b,
            res.status, res.min_clearance, res.binding_segment,
            stand, None,
        ))
    # Overall tightest clearance and its source segment.
    cl_sources = [
        (p.min_clearance, f"{p.cart_id} on {p.binding_segment}")
        for p in placed if p.min_clearance is not None
    ]
    cl_sources.sort(key=lambda t: t[0])
    overall = cl_sources[0][0] if cl_sources else None
    source = cl_sources[0][1] if cl_sources else None
    cancelled = tuple(
        c.cart_id for c in all_carts if c.cart_id not in kept_ids
    )
    return ExhibitionPlan(
        True, None, tuple(placed), cancelled,
        tuple(stands), tuple(bridges_used), overall, source,
    )
