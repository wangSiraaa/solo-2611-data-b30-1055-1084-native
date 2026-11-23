"""Documented binding-photography walkthrough for the rare-volume job.

This is the executable form of the job brief.  It builds one volume, attaches
its leaf-assembly sheet to case members, and produces **two separate orders**:

* a *true-support* order (whole-volume support witness, seam/combination queries,
  reachability), and
* an *imaging* order (per-target shot plan with a chosen jig).

It then runs the six worked cases the brief calls out:

A. support table ``{00,10,11}``: a one-seam-at-a-time route ``00 -> 10 -> 11``;
   a new signed measurement that leaves only ``{00,11}`` keeps ``11`` statically
   safe but makes it unreachable.
B. a double-clamp handover: the old clamp may be withdrawn only after the new
   clamp has taken over; the two clamp states are each safe and the engage step
   cannot be skipped.  If the only available new clamp position also hides the
   right column, the frame still cannot be shot.
C. a standby station whose right column is *permanently* barred: no route can
   shoot that frame.  The stop frame reaches the angle but its own bars hide the
   must-see column.
D. custody: while a member is under hold the volume may not be split out to an
   outside station; equipment already at the *origin* may still work under the
   origin's own measurements.  A merely-prepared transfer slip does not make an
   out-of-house frame countable.

Everything is fictional; nothing is deleted or sent anywhere real.
"""

from __future__ import annotations

import json
import os

from .binding import PatchMeasurement, make_assembly, pose_str
from .binding_service import BindingPhotoService
from .handoff_service import HandoffRepo
from .materials import import_materials
from .models import RejectError
from .photography import ClampJigPlanner, PhotoStation, TargetShot
from .rules_service import RuleEventStore


# --------------------------------------------------------------------------
# The demo volume and its leaf-assembly sheet
# --------------------------------------------------------------------------
def demo_assembly():
    """Three seams F1..F3 (levels 0/1/2) and two patch strips on members B1/B2.

    Strip ``PA`` (page B1) spans F1/F2 and only stays flat while ``F1 >= F2``;
    strip ``PB`` (page B1) spans F2/F3 and only stays flat while ``F2 >= F3``.
    Together they admit exactly the monotone poses ``F1 >= F2 >= F3``, a
    connected staircase the ordinary table can walk one seam at a time.
    """
    monotone = [[0, 0], [1, 0], [1, 1], [2, 0], [2, 1], [2, 2]]
    return make_assembly(
        "VOL-B1", "V1",
        seams=[
            {"seam_id": "F1", "levels": [0, 1, 2]},
            {"seam_id": "F2", "levels": [0, 1, 2]},
            {"seam_id": "F3", "levels": [0, 1, 2]},
        ],
        strips=[
            {"strip_id": "PA", "doc_id": "B1", "leaf_group": "G1",
             "seams": ["F1", "F2"], "allowed": monotone,
             "hold_positions": [2, 3, 6], "hold_at": [[1, 1], [2, 1], [2, 2]]},
            {"strip_id": "PB", "doc_id": "B1", "leaf_group": "G2",
             "seams": ["F2", "F3"], "allowed": monotone,
             "hold_positions": [4, 5, 6], "hold_at": [[1, 1], [2, 2]]},
        ],
        start_pose=[0, 0, 0],
        members=("B1", "B2"),
    )


def local_station() -> PhotoStation:
    """The origin table: text columns 2..5, margin 6, fixed bars 1 and 7, one clamp."""
    return PhotoStation(
        "LOCAL-TABLE", "LOCAL", columns=(1, 2, 3, 4, 5, 6, 7),
        left_bar=frozenset({1}), right_bar=frozenset({7}), clamp_slots=1,
    )


def double_clamp_station() -> PhotoStation:
    """The same table fitted with the double-clamp bracket (clamp_slots=2)."""
    return PhotoStation(
        "LOCAL-TABLE-2C", "LOCAL", columns=(1, 2, 3, 4, 5, 6, 7),
        left_bar=frozenset({1}), right_bar=frozenset({7}), clamp_slots=2,
    )


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def _two_seam_assembly(hold_positions, hold_at):
    return make_assembly(
        "VOL-EX", "V1",
        seams=[
            {"seam_id": "L", "levels": [0, 1]},
            {"seam_id": "R", "levels": [0, 1]},
        ],
        strips=[
            {"strip_id": "RT", "doc_id": "B1", "leaf_group": "G2",
             "seams": ["L", "R"], "allowed": [[0, 0], [1, 0], [1, 1]],
             "hold_positions": list(hold_positions),
             "hold_at": [list(t) for t in hold_at]},
        ],
        start_pose=[0, 0],
        members=("B1",),
    )


# --------------------------------------------------------------------------
# A: support table {00,10,11} and the unreachable goal
# --------------------------------------------------------------------------
def case_a_support_and_reach():
    from .binding import SupportModel

    assembly = make_assembly(
        "VOL-A", "V1",
        seams=[
            {"seam_id": "S0", "levels": [0, 1]},
            {"seam_id": "S1", "levels": [0, 1]},
        ],
        strips=[
            {"strip_id": "R", "doc_id": "B1", "leaf_group": "G1",
             "seams": ["S0", "S1"], "allowed": [[0, 0], [1, 0], [1, 1]]},
        ],
        start_pose=[0, 0],
        members=("B1",),
    )
    model = SupportModel(assembly, custodian_of=lambda c: "LOCAL")
    table_before = [pose_str(p) for p in model.support_table()]
    route = model.shortest_route([0, 0], [1, 1])
    # Re-measure strip R to allow only 00 or 11.
    model.submit_measurement(
        PatchMeasurement("M-A", 1, "V1", 0, "LOCAL", {"R": ((0, 0), (1, 1))})
    )
    table_after = [pose_str(p) for p in model.support_table()]
    return {
        "case": "A_support_and_reachability",
        "table_before": table_before,
        "route_00_to_11": route,
        "shoot_second_seam_at_1": [
            {"step": "open S0", "pose": "10"},
            {"step": "open S1", "pose": "11"},
        ],
        "after_measurement": {
            "measurement": "M-A",
            "table": table_after,
            "goal_11_statically_safe": model.is_supported((1, 1)),
            "goal_11_reachable_from_00": model.target_reachable([0, 0], [1, 1]),
            "note": "target stays statically safe, but 00 and 11 are not adjacent; "
                    "a one-seam move 00->11 jumps two seams and is not allowed",
        },
    }


# --------------------------------------------------------------------------
# B: double-clamp handover; and the new-clamp-still-occludes case
# --------------------------------------------------------------------------
def case_b_handover():
    # RT stays active at both 10 and 11; can be held at col 5 (hides right) or
    # col 4 (clear).  Start at pose 10 with the clamp at col 5.
    hold_at = ((1, 0), (1, 1))
    assembly = _two_seam_assembly([4, 5], hold_at)
    from .binding import SupportModel

    model = SupportModel(assembly, custodian_of=lambda c: "LOCAL")
    target = TargetShot("sh-right", "R", 1, must_see=frozenset({2, 3, 5}))

    single = PhotoStation("T1", "LOCAL", columns=(1, 2, 3, 4, 5),
                          left_bar=frozenset({1}), right_bar=frozenset(),
                          clamp_slots=1)
    double = PhotoStation("T2", "LOCAL", columns=(1, 2, 3, 4, 5),
                          left_bar=frozenset({1}), right_bar=frozenset(),
                          clamp_slots=2)
    plan_single = ClampJigPlanner(single, model, initial_clamps=[5]).plan(
        [target], initial_pose=[1, 0]
    )
    plan_double = ClampJigPlanner(double, model, initial_clamps=[5]).plan(
        [target], initial_pose=[1, 0]
    )
    handover = ClampJigPlanner(double, model, initial_clamps=[5]).handover_states(
        (1, 0), frozenset({5}), new_column=4, old_column=5
    )

    # Variant: the *only* new clamp position also hides the right column.
    narrow = _two_seam_assembly([5], hold_at)  # only col 5 can hold, and it hides
    model_narrow = SupportModel(narrow, custodian_of=lambda c: "LOCAL")
    plan_narrow = ClampJigPlanner(double, model_narrow, initial_clamps=[5]).plan(
        [target], initial_pose=[1, 0]
    )

    return {
        "case": "B_handover",
        "single_clamp": {
            "feasible": plan_single["feasible"],
            "actions": plan_single["actions"],
            "note": "single clamp cannot take over without retracting every seam "
                    "to the handover pose first; it then reopens",
        },
        "double_clamp": {
            "feasible": plan_double["feasible"],
            "actions": plan_double["actions"],
            "handover_states": handover,
            "note": "engage the new clamp (state safe) -> withdraw the old clamp "
                    "(state safe); the engage cannot be skipped",
        },
        "new_clamp_also_occludes_right": {
            "feasible": plan_narrow["feasible"],
            "reason": plan_narrow["targets"][0].get("reason"),
            "note": "if the only takeover position also hides the right column, "
                    "the frame still cannot be shot",
        },
    }


# --------------------------------------------------------------------------
# C: standby station with a permanently-barred right column; stop-frame probe
# --------------------------------------------------------------------------
def case_c_standby_barred():
    from .binding import SupportModel

    hold_at = ((1, 0), (1, 1))
    assembly = _two_seam_assembly([4, 5], hold_at)
    model = SupportModel(assembly, custodian_of=lambda c: "LOCAL")
    target = TargetShot("sh-right", "R", 1, must_see=frozenset({2, 3, 5}))

    # Standby table: right column 5 permanently hidden by a fixed bar.
    standby = PhotoStation("STANDBY", "OUTPOST", columns=(1, 2, 3, 4, 5),
                          left_bar=frozenset({1}), right_bar=frozenset({5}),
                          clamp_slots=2)
    plan = ClampJigPlanner(standby, model, initial_clamps=[5]).plan(
        [target], initial_pose=[1, 0]
    )
    from .photography import stop_frame_probe

    frame = stop_frame_probe(
        standby, target, supported=True, hold_ok=True,
        pad_swap={"pad": "P3", "stop": "S2", "qualified_initial_pose": True},
    )
    return {
        "case": "C_standby_permanently_barred",
        "feasible": plan["feasible"],
        "reason": plan["targets"][0].get("reason"),
        "stop_frame_probe": frame,
        "note": "any route leaves the right column hidden, so this frame is "
                "unshootable on the standby table; a numbered pad swap is "
                "possible only between qualified poses, but the fixed bar still "
                "hides the must-see column",
    }


def _resolve_materials(materials: str) -> str:
    if os.path.isdir(materials):
        return materials
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidate = os.path.join(here, materials)
    if os.path.isdir(candidate):
        return candidate
    return materials


# --------------------------------------------------------------------------
# D: custody-scoped measurement signing + prepared-but-unconfirmed transit
# --------------------------------------------------------------------------
def case_d_custody(workdir: str, materials: str = "materials/binding751") -> dict:
    """Demonstrate measurement authority and the prepared-transit boundary.

    V1 = {B1, B2, B4}; the sheet is attached to B1/B2, but member **B4 is under
    an open hold**, so V1 cannot be handed off as a whole -- the volume cannot
    be split out to an outside table.  Equipment already at the origin (LOCAL)
    may still start work under LOCAL's own signed measurements.

    Separately, for a releasable case V2 we show that a merely *prepared*
    transfer slip does not move custody: LOCAL remains the measurement authority
    and an outside frame is not countable.
    """
    rules = RuleEventStore(os.path.join(workdir, "rules_events.sqlite"))
    handoff = HandoffRepo(os.path.join(workdir, "handoff.sqlite"))
    import_materials(rules, handoff, _resolve_materials(materials))
    service = BindingPhotoService(handoff, demo_assembly(), local_station())

    steps = []
    # 1) LOCAL is the confirmed custodian: its measurement is accepted.
    ok_local = service.sign_measurement(
        PatchMeasurement("M-D1", 1, "V1", 0, "LOCAL",
                         {"PA": {"hold_positions": [2, 3, 4]}})
    )
    steps.append({"step": "local_signs_measurement", "accepted": True,
                  "signer": ok_local["signer"]})

    # 2) An out-of-house signer cannot sign while LOCAL is custodian.
    rej = None
    try:
        service.sign_measurement(
            PatchMeasurement("M-D2", 1, "V1", 0, "OUTPOST",
                             {"PA": {"hold_positions": [2, 3, 4]}})
        )
    except RejectError as exc:
        rej = exc
    steps.append({"step": "outside_signer_rejected", "accepted": rej is None,
                  "code": rej.code if rej else None})

    # 3) Whole-case handoff of V1 is refused because member B4 is on hold: the
    #    volume cannot be split out to an outside station.
    product = rules.build_product(0)
    rules.save_product("QD", product)
    handoff.ingest_product("QD", product)
    split_err = None
    try:
        handoff.submit({"handoff_id": "H-D", "case_id": "V1",
                        "doc_ids": sorted(handoff.current_members("V1")),
                        "from": "LOCAL", "to": "OUTPOST", "as_of_day": 0,
                        "qualification_product_id": "QD"})
    except RejectError as exc:
        split_err = exc
    steps.append({"step": "cannot_split_held_volume_out",
                  "members": sorted(handoff.current_members("V1")),
                  "rejected": split_err is not None,
                  "code": split_err.code if split_err else None,
                  "custodian_after": handoff.current_custodian("V1"),
                  "note": "B4 on hold -> whole case not releasable -> no outside handoff"})

    # 4) Even so, LOCAL equipment may start: a LOCAL-signed measurement plus a
    #    LOCAL-station plan are usable right now.
    plan_at_origin = service.plan_targets(
        [TargetShot("sh-f1", "F1", 1, frozenset({2, 3, 4}))],
        station=local_station(),
    )
    steps.append({"step": "origin_equipment_may_work",
                  "feasible": plan_at_origin["feasible"],
                  "produced_by": plan_at_origin["institution"],
                  "note": "already-at-origin device works under LOCAL measurements"})

    # 5) Prepared-but-unconfirmed: use the releasable case V2 (B3).  A prepared
    #    transfer to OUTPOST does NOT move custody; LOCAL stays the authority and
    #    an outside frame is not countable yet.
    handoff.prepare({"handoff_id": "P-D", "proposal_id": "P-D", "case_id": "V2",
                     "doc_ids": sorted(handoff.current_members("V2")),
                     "from": "LOCAL", "to": "OUTPOST", "as_of_day": 0,
                     "qualification_product_id": "QD"})
    v2_service = BindingPhotoService(
        handoff,
        make_assembly("VOL-B3", "V2",
                      seams=[{"seam_id": "Q1", "levels": [0, 1]}],
                      strips=[],
                      start_pose=[0], members=("B3",)),
        local_station(),
    )
    v2_custody = v2_service.custody_view()
    authority = v2_custody["measurement_authority"]
    outside_frame_countable = v2_custody["confirmed_custodian"] == "OUTPOST"
    steps.append({"step": "prepared_not_confirmed", "case": "V2",
                  "custody": v2_custody,
                  "authority_still": authority,
                  "outside_frame_countable": outside_frame_countable,
                  "note": "prepared slip keeps LOCAL as measurement authority; "
                          "an outside frame is not countable until confirmed"})

    rules.close()
    handoff.close()
    return {
        "case": "D_custody_signing_and_transit",
        "steps": steps,
        "ok": (
            steps[0]["accepted"]
            and steps[1]["accepted"] is False
            and steps[1]["code"] == "unsigned_measurement"
            and steps[2]["rejected"] is True
            and steps[2]["code"] == "ineligible_members"
            and steps[2]["custodian_after"] == "LOCAL"
            and steps[3]["feasible"] is True
            and authority == "LOCAL"
            and outside_frame_countable is False
        ),
    }


# --------------------------------------------------------------------------
# Full work order (two orders) for the demo volume
# --------------------------------------------------------------------------
def demo_work_order() -> dict:
    from .binding import SupportModel

    assembly = demo_assembly()
    service = BindingPhotoService(HandoffRepo(":memory:"), assembly, local_station())
    # HandoffRepo(":memory:") starts every case at LOCAL; mirror the members so
    # custody lookups resolve.
    service.handoff.sync_catalog_membership(
        [{"doc_id": "B1", "case_id": "V1"}, {"doc_id": "B2", "case_id": "V1"}]
    )
    targets = [
        TargetShot("T1", "F1", 1, frozenset({2, 3, 4, 5})),
        TargetShot("T2", "F2", 1, frozenset({2, 3, 4, 5})),
        TargetShot("T3", "F3", 2, frozenset({2, 3, 4, 5})),
        # T4 must also read the margin column 6, but the only clamp that can hold
        # both patches at pose 222 sits on column 6 -> occlusion conflict.
        TargetShot("T4", "F2", 2, frozenset({2, 3, 4, 5, 6})),
    ]
    queries = [
        {"kind": "seam", "seam_id": "F1"},
        {"kind": "combination", "partial": {"F1": 1, "F2": 1}},
        {"kind": "combination", "partial": {"F1": 1, "F3": 1}},
    ]
    order = service.make_work_order(
        targets, station=local_station(), initial_clamps=(6,), queries=queries
    )
    order["refresh_status"] = [
        service.record_reshoot(
            t["shot_id"],
            reason="target infeasible on this station" if not t["feasible"] else "planned",
            original_page=next(
                (st.doc_id for st in assembly.strips if t["seam_id"] in st.seams), "B1"
            ),
            pose=t.get("pose", "?"),
            visible_columns=t.get("visible", []),
            feasible=t["feasible"],
        )
        for t in order["imaging_order"]["targets"]
    ]
    service.handoff.close()
    return order


# --------------------------------------------------------------------------
# Entry point: write artifacts and return the consolidated result
# --------------------------------------------------------------------------
def run_binding_demo(data_dir: str, materials: str = "materials/binding751") -> dict:
    os.makedirs(data_dir, exist_ok=True)
    artifacts = os.path.join(data_dir, "artifacts")
    os.makedirs(artifacts, exist_ok=True)

    # The custody case needs a real (file-backed) handoff store.
    case_d_dir = os.path.join(data_dir, "case_d")
    os.makedirs(case_d_dir, exist_ok=True)
    for name in ("rules_events.sqlite", "handoff.sqlite"):
        p = os.path.join(case_d_dir, name)
        if os.path.exists(p):
            os.remove(p)

    warnings = []
    import_materials_check = None
    _mat = _resolve_materials(materials)
    try:
        # sanity: the sample materials import cleanly
        r = RuleEventStore(os.path.join(case_d_dir, "_check_rules.sqlite"))
        h = HandoffRepo(os.path.join(case_d_dir, "_check_handoff.sqlite"))
        import_materials_check = import_materials(r, h, _mat)
        r.close()
        h.close()
    except Exception as exc:  # pragma: no cover - diagnostic only
        warnings.append(f"materials import check failed: {exc}")

    a = case_a_support_and_reach()
    b = case_b_handover()
    c = case_c_standby_barred()
    d = case_d_custody(case_d_dir, materials=materials)
    work_order = demo_work_order()

    expected = {
        "A_table_00_10_11": a["table_before"] == ["00", "10", "11"],
        "A_route_is_two_steps": len(a["route_00_to_11"] or []) == 2,
        "A_after_00_and_11_only": a["after_measurement"]["table"] == ["00", "11"],
        "A_goal_still_statically_safe": a["after_measurement"]["goal_11_statically_safe"],
        "A_goal_now_unreachable": a["after_measurement"]["goal_11_reachable_from_00"] is False,
        "B_single_clamp_handover_via_retract": b["single_clamp"]["feasible"] is True,
        "B_double_clamp_engage_then_withdraw": b["double_clamp"]["feasible"] is True,
        "B_new_clamp_occluding_is_unshootable":
            b["new_clamp_also_occludes_right"]["feasible"] is False,
        "C_standby_unshootable": c["feasible"] is False,
        "C_stop_frame_bar_hides_column":
            c["stop_frame_probe"]["bar_occludes_must_see"] == [5],
        "D_custody_ok": d["ok"],
        "imaging_infeasible_targets_present": (
            sum(1 for t in work_order["imaging_order"]["targets"] if not t["feasible"]) >= 1
        ),
    }
    ok = all(expected.values())

    result = {
        "ok": ok,
        "data_dir": os.path.abspath(data_dir),
        "materials_import_check": import_materials_check,
        "warnings": warnings,
        "expected": expected,
        "cases": {"A": a, "B": b, "C": c, "D": d},
        "work_order": work_order,
    }

    with open(os.path.join(artifacts, "binding_demo.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    with open(os.path.join(artifacts, "work_order.json"), "w", encoding="utf-8") as fh:
        json.dump(work_order, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    return result
