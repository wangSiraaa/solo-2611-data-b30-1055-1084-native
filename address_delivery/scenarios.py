"""Run synthetic recycling scenarios end to end.

A scenario document is a self-contained ``.json`` file (it may also name a
shared ``catalog_file`` / ``coverage_file`` resolved next to it):

* ``catalog`` / ``coverage`` - the synthetic catalog to load (or the file names);
* ``bins`` - named bins, each optionally seeded with last-round wet residue and
  a lending address, plus an ordered event script (``empty`` / ``dry`` /
  ``certificate`` / ``fill`` / ``return``).  A bin marked
  ``lend_after_revision`` is lent only after the revision is applied;
* optional ``revision`` - applied after the pre-revision lendings, so a zoning
  change leaves the original lending at the old station;
* optional ``probes`` - extra new-parcel submissions (e.g. to show the new
  station after a zoning change);
* ``route``, ``compartments``, ``station_rules`` - the truck for planning;
* ``committed_stations`` - stations the operator has promised to serve;
* ``method_a_terms`` - depot-issued per-bin compartment terms;
* ``prove_split`` - a station at which to split early/later for the capacity
  reservation proof.

``build_scenario`` returns the live service, ledger and lendings; ``derive_bins``
turns every bin currently holding a load into a truck
:class:`~address_delivery.truck.Bin`; ``run`` returns the planning report.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .recycling import RecyclingLedger
from .service import AddressService
from .truck import (
    Compartment,
    plan_late,
    plan_with_partition,
    prove_early_capacity,
)


def load_scenario(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    scenario = json.loads(path.read_text(encoding="utf-8"))
    if "catalog_file" in scenario and "catalog" not in scenario:
        scenario["catalog"] = json.loads(
            (path.parent / scenario["catalog_file"]).read_text(encoding="utf-8")
        )
    if "coverage_file" in scenario and "coverage" not in scenario:
        scenario["coverage"] = json.loads(
            (path.parent / scenario["coverage_file"]).read_text(encoding="utf-8")
        )
    return scenario


def _apply_event(led: RecyclingLedger, bin_id: str, ev: dict[str, Any]) -> dict[str, Any]:
    kind = ev["kind"]
    kwargs = {}
    if "seq" in ev:
        kwargs["seq"] = ev["seq"]
    if kind == "fill":
        for key in ("lending_id", "station_id"):
            if key in ev:
                kwargs[key] = ev[key]
        return led.note_fill(bin_id, ev["material"], int(ev["weight"]), **kwargs)
    if kind == "return":
        for key in ("lending_id", "station_id"):
            if key in ev:
                kwargs[key] = ev[key]
        return led.note_return(bin_id, **kwargs)
    if kind == "empty":
        return led.note_empty(bin_id, **kwargs)
    if kind == "dry":
        return led.note_dry(bin_id, **kwargs)
    if kind == "certificate":
        for key in ("lending_id", "station_id"):
            if key in ev:
                kwargs[key] = ev[key]
        return led.note_certificate(bin_id, **kwargs)
    raise ValueError(f"unknown scenario event {kind!r}")


def _do_lend(led: RecyclingLedger, spec: dict[str, Any]):
    lend = spec["lend"]
    return led.lend(
        spec["bin_id"],
        lend["city_id"],
        lend["district_text"],
        lend["street_text"],
        int(lend["house_no"]),
        parcel_id=lend.get("parcel_id"),
        seq=lend.get("seq"),
    )


def build_scenario(scenario: dict[str, Any]):
    """Build the catalog, seed bins, open lendings (then optional revision)."""
    svc = AddressService(":memory:")
    svc.catalog.load_initial(scenario["catalog"], scenario["coverage"])
    led = RecyclingLedger(svc)

    for spec in scenario["bins"]:
        led.ensure_bin(
            spec["bin_id"],
            initial_wet_residue=spec.get("initial_wet_residue", False),
            reason=spec.get("residue_reason"),
        )

    lends: dict[str, Any] = {}
    for spec in scenario["bins"]:
        if not spec.get("lend_after_revision"):
            lends[spec["bin_id"]] = _do_lend(led, spec)

    revision = scenario.get("revision")
    if revision:
        svc.apply_revision(revision)

    for spec in scenario["bins"]:
        if spec.get("lend_after_revision"):
            lends[spec["bin_id"]] = _do_lend(led, spec)

    for spec in scenario["bins"]:
        for ev in spec.get("events", []):
            _apply_event(led, spec["bin_id"], ev)

    probes = {}
    for probe in scenario.get("probes", []):
        result = svc.submit_new(
            probe["parcel_id"],
            probe["city_id"],
            probe["district_text"],
            probe["street_text"],
            int(probe["house_no"]),
        )
        probes[probe["parcel_id"]] = {
            "outcome": result.outcome,
            "station_id": result.station_id,
        }
    return svc, led, lends, probes


def derive_bins(led: RecyclingLedger, bin_ids):
    """Truck bins for every named bin currently holding a load."""
    from .truck import make_bin

    out = []
    for bin_id in bin_ids:
        material = led.material(bin_id)
        if material is not None:
            out.append(
                make_bin(
                    bin_id,
                    material["station_id"],
                    material["grade"],
                    material["weight"],
                )
            )
    return out


def _compartments(scenario: dict[str, Any]) -> list[Compartment]:
    return [
        Compartment(
            c["compartment_id"],
            int(c["capacity"]),
            tuple(c.get("accepts", [])),
            bool(c.get("mix_permitted", False)),
        )
        for c in scenario.get("compartments", [])
    ]


def run(scenario: dict[str, Any]) -> dict[str, Any]:
    """Build and plan; returns ledger explanations plus both method reports."""
    svc, led, lends, probes = build_scenario(scenario)
    try:
        bins = derive_bins(led, [s["bin_id"] for s in scenario["bins"]])
        route = list(scenario["route"])
        compartments = _compartments(scenario)
        rules = scenario.get("station_rules", {})
        committed = scenario.get("committed_stations")
        result: dict[str, Any] = {
            "bins": [b.as_dict() for b in bins],
            "route": route,
            "compartments": [c.as_dict() for c in compartments],
            "lendings": {k: v.as_dict() for k, v in lends.items()},
            "probes": probes,
            "explain": [led.explain(s["bin_id"]) for s in scenario["bins"]],
        }
        if "method_a_terms" in scenario:
            result["method_a"] = plan_with_partition(
                bins,
                route,
                compartments,
                scenario["method_a_terms"],
                station_rules=rules,
                committed_stations=committed,
            )
        result["method_b"] = plan_late(
            bins,
            route,
            compartments,
            station_rules=rules,
            committed_stations=committed,
        )
        if scenario.get("prove_split"):
            result["reservation_proof"] = prove_early_capacity(
                bins,
                route,
                compartments,
                split_station=scenario["prove_split"],
                station_rules=rules,
                committed_stations=committed,
            )
        return result
    finally:
        svc.close()
