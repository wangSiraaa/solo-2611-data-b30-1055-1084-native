"""Worked reproductions of every case named in the workshop ticket.

Each function returns ``{"ok": bool, "steps": [...], "expected": {...}}`` so the
tests and the artifact writer can check positive booleans, mirroring
``archive_handoff.scenario.run_scenario`` style.

The archive store is used for the *custody / whole-case* half only: the engine
never recomputes dates and never decides custody, it consumes ``custody_ok`` and
the custodian name from :class:`archive_handoff.HandoffRepo`.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Optional

from archive_handoff import HandoffRepo, RuleEventStore
from archive_handoff.materials import import_materials

from .engine import (
    CRAFT_CATCH_FRAME,
    CRAFT_FILM,
    CoordinateTable,
    PlateWorkOrder,
    ProofingSession,
    SheetRequest,
)
from .model import (
    AUTHORITY_CUSTODIAN,
    AUTHORITY_PLAN_ONLY,
    COMPLETE,
    MISSING,
    TORN,
    Patch,
    Reading,
    Regrind,
)


def load_coordinate_table(path: str) -> CoordinateTable:
    return CoordinateTable.load(path)


def _fresh_archive(tmpdir: str, materials: str):
    rules = RuleEventStore(os.path.join(tmpdir, "rules.db"))
    handoff = HandoffRepo(os.path.join(tmpdir, "handoff.db"))
    import_materials(rules, handoff, materials)
    return rules, handoff


# ---------------------------------------------------------------------------
# Case 1 + 2: old proof says L missing / R complete, then R notched too.
# One patch can restore exactly R; both are must-check -> can sign.
# A new reading of the same old sample that also says L completed makes the
# signed proof fail to reproduce the old reading.
# ---------------------------------------------------------------------------
def case_right_patch_signable(coord: CoordinateTable) -> dict:
    steps = []
    wo = PlateWorkOrder(coord)
    wo.set_plate_state({"R": MISSING})  # plate now misses R; L already missing
    wo.register_run(10)
    # Old (original) reading of the old sample at run 10.
    old = Reading(
        reading_id="RD-old", sample_id="SAMP-old", run=10,
        states={"L": MISSING, "R": COMPLETE},
        custodian="HOLD-A",
    )
    wo.add_reading(old)
    wo.set_must_check(["L", "R"])
    wo.add_patch(Patch(
        patch_id="P-R", physical_id="PHY-R", covers=frozenset({"R"}),
        emits={"R": COMPLETE}, slot="S-R", pins=frozenset({"pin-R"}),
    ))
    wo.validate()

    sess = ProofingSession(wo, CRAFT_CATCH_FRAME)
    j = sess.process(SheetRequest("SH1", "SAMP-old", 10, mounts=("P-R",),
                                  signer="HOLD-A"))
    steps.append({"step": "sign_with_R_patch", **j.as_dict()})
    expected = {
        "prints_L_missing": j.printed.get("L") == MISSING,
        "prints_R_complete": j.printed.get("R") == COMPLETE,
        "signable": j.signable,
    }

    # New reading of the SAME old sample says L was complete too.  Now no proof
    # with only the R patch can reproduce the old reading at L.
    newo = Reading(
        reading_id="RD-old-reread", sample_id="SAMP-old", run=10,
        states={"L": COMPLETE, "R": COMPLETE},
        custodian="HOLD-A", supersedes=("RD-old",),
    )
    wo.add_reading(newo)
    sess2 = ProofingSession(wo, CRAFT_CATCH_FRAME)
    j2 = sess2.process(SheetRequest("SH2", "SAMP-old", 10, mounts=("P-R",),
                                    signer="HOLD-A"))
    steps.append({"step": "sign_after_reread_L_complete", **j2.as_dict()})
    expected["reread_makes_R_only_insufficient"] = (
        not j2.signable and any(r["kind"] == "mismatch" and r.get("point") == "L"
                                for r in j2.refusals)
    )
    ok = all(expected.values())
    return {"ok": ok, "expected": expected, "steps": steps}


# ---------------------------------------------------------------------------
# Case 2: restore the old appraisal after a re-read deposed it, so the R-only
# proof fits the old sample again.  Then convert the locating pin into a slot
# locked by the machine: the *appraisal* stays valid but the *proof* is refused.
# ---------------------------------------------------------------------------
def case_restore_and_locked_slot(coord: CoordinateTable) -> dict:
    steps = []
    wo = PlateWorkOrder(coord)
    wo.set_plate_state({"L": MISSING, "R": MISSING})
    wo.register_run(20)
    old = Reading(
        reading_id="RD-old", sample_id="SAMP-old", run=20,
        states={"L": MISSING, "R": COMPLETE}, custodian="HOLD-B",
    )
    wo.add_reading(old)
    # A re-read deposed it, claiming L was complete after all.
    reread = Reading(
        reading_id="RD-reread", sample_id="SAMP-old", run=20,
        states={"L": COMPLETE, "R": COMPLETE}, custodian="HOLD-B",
        supersedes=("RD-old",),
    )
    wo.add_reading(reread)
    # Restore the old appraisal: a sufficient-authority reading re-instates it.
    restore = Reading(
        reading_id="RD-restore", sample_id="SAMP-old", run=20,
        states={"L": MISSING, "R": COMPLETE}, custodian="HOLD-B",
        supersedes=("RD-reread",),
    )
    wo.add_reading(restore)
    wo.set_must_check(["L", "R"])
    wo.add_patch(Patch(
        patch_id="P-LL", physical_id="PHY-LL", covers=frozenset({"R"}),
        emits={"R": COMPLETE}, slot="S-R", pins=frozenset({"pin-R"}),
    ))
    wo.validate()

    # After restore, only the old reading is live; R-only proof is signable.
    sess = ProofingSession(wo, CRAFT_CATCH_FRAME)
    j = sess.process(SheetRequest("SH3", "SAMP-old", 20, mounts=("P-LL",),
                                  signer="HOLD-B"))
    steps.append({"step": "after_restore_signable", **j.as_dict()})
    expected = {
        "appraisal_valid": bool(j.matched_readings),
        "restore_reinstate_signs": j.signable,
        "prints_L_missing": j.printed.get("L") == MISSING,
    }

    # Now the locating pin becomes a slot locked by the machine: appraisal valid
    # but the proof is refused.
    sess2 = ProofingSession(wo, CRAFT_CATCH_FRAME, locked_slots={"S-R"},
                            locked_pins={"pin-R"})
    j2 = sess2.process(SheetRequest("SH3b", "SAMP-old", 20, mounts=("P-LL",),
                                    signer="HOLD-B"))
    steps.append({"step": "pin_as_locked_slot", **j2.as_dict()})
    expected["locked_slot_refuses_proof"] = (
        not j2.signable and any(r["kind"] == "slot_locked" for r in j2.refusals)
    )
    expected["appraisal_still_valid_when_locked"] = bool(j2.matched_readings)
    ok = all(expected.values())
    return {"ok": ok, "expected": expected, "steps": steps}


# ---------------------------------------------------------------------------
# Case 4: a torn-corner impression spans the two return days of a proven
# regrind.  The torn point is registered as TORN (not complete, not missing).
# ---------------------------------------------------------------------------
def case_torn_spanning_regrind(coord: CoordinateTable) -> dict:
    steps = []
    wo = PlateWorkOrder(coord)
    wo.set_plate_state({"L": COMPLETE, "R": COMPLETE})
    wo.register_run(30)
    wo.register_run(31)
    # Regrind proof: the same physical state returns at day 30 and day 35.
    wo.add_reading(Reading(
        reading_id="TORN-30", sample_id="SAMP-torn", run=30,
        states={"L": COMPLETE, "R": TORN},
        custodian="HOLD-C", regrind_return_day=30,
    ))
    wo.add_reading(Reading(
        reading_id="TORN-35", sample_id="SAMP-torn", run=31,
        states={"L": COMPLETE, "R": TORN},
        custodian="HOLD-C", regrind_return_day=35,
    ))
    wo.set_must_check(["L", "R"])
    wo.validate()
    sess = ProofingSession(wo, CRAFT_CATCH_FRAME)
    j30 = sess.process(SheetRequest("SH30", "SAMP-torn", 30, signer="HOLD-C"))
    j35 = sess.process(SheetRequest("SH35", "SAMP-torn", 31, signer="HOLD-C"))
    steps.append({"step": "torn_at_30", **j30.as_dict(), "return_day": 30})
    steps.append({"step": "torn_at_31", **j35.as_dict(), "return_day": 35})
    expected = {
        "torn_not_complete": wo.readings["TORN-30"].state_of("R") == TORN,
        "torn_not_missing": wo.readings["TORN-30"].state_of("R") != MISSING,
        "torn_R_unverifiable_both_days": all(
            not j.signable and any(r["kind"] == "unverifiable" and r.get("point") == "R"
                                   for r in j.refusals)
            for j in (j30, j35)
        ),
        "L_still_signed_off": all(j.printed.get("L") == COMPLETE for j in (j30, j35)),
    }
    ok = all(expected.values())
    return {"ok": ok, "expected": expected, "steps": steps}


# ---------------------------------------------------------------------------
# Case 4b: an old notch disappears only after an evidenced whole-face regrind.
# Without evidence the two readings cannot both stand; with evidence they can,
# and the "two return days" of the regrind are both explained.
# ---------------------------------------------------------------------------
def case_regrind_explains_notch(coord: CoordinateTable) -> dict:
    steps = []
    wo = PlateWorkOrder(coord)
    wo.set_plate_state({"L": COMPLETE, "R": COMPLETE})
    wo.register_run(33)
    wo.register_run(35)
    # Run 33: R notched.  Run 35: R complete again.
    wo.add_reading(Reading("RG33", "SAMP-RG", 33,
                           {"L": COMPLETE, "R": MISSING}, "HOLD-R"))
    wo.add_reading(Reading("RG35", "SAMP-RG", 35,
                           {"L": COMPLETE, "R": COMPLETE}, "HOLD-R"))
    wo.set_must_check(["L", "R"])
    wo.validate()

    # No regrind yet -> the disappearing notch is unexplainable.
    sess0 = ProofingSession(wo, CRAFT_CATCH_FRAME)
    j0 = sess0.process(SheetRequest("SH35a", "SAMP-RG", 35, signer="HOLD-R"))
    steps.append({"step": "no_regrind_evidence", **j0.as_dict()})
    expected = {
        "unexplained_without_evidence": (not j0.signable and any(
            r["kind"] == "unexplained_transition" and r.get("point") == "R"
            for r in j0.refusals)),
    }
    # Add an evidenced regrind whose return day sits between the observations.
    wo.add_regrind(Regrind("RG-1", day=34, evidence_id="EV-REG-1",
                           note="whole face ground and re-proofed"))
    sess1 = ProofingSession(wo, CRAFT_CATCH_FRAME)
    j1 = sess1.process(SheetRequest("SH35b", "SAMP-RG", 35, signer="HOLD-R"))
    steps.append({"step": "with_regrind_evidence", **j1.as_dict()})
    expected["regrind_explains_signs"] = j1.signable
    # An unevidenced regrind is rejected at registration time.
    try:
        Regrind("RG-BAD", day=34, evidence_id="")
        regrind_rejected = False
    except Exception as exc:
        regrind_rejected = getattr(exc, "code", None) == "unevidenced_regrind"
    expected["unevidenced_regrind_rejected"] = regrind_rejected
    ok = all(expected.values())
    return {"ok": ok, "expected": expected, "steps": steps}


# ---------------------------------------------------------------------------
# Case 5b: tie the proofing layer to real archive custody.  A case with a held
# member (H1 active) or a missing due trigger is not wholly held, so no proof
# may be signed -- even though the pattern itself would be correct.
# ---------------------------------------------------------------------------
def _custody_ok_for_case(rules, handoff, case_id: str, day: int) -> bool:
    """A case is wholly held only if it is validly held and every member is
    printable on the day (no active hold, no missing due trigger)."""
    members = handoff.current_members(case_id)
    product = rules.build_product(day, members)
    return all(product.documents[m].releasable for m in members)


def case_held_member_blocks_proof(tmpdir: str, materials: str,
                                  coord: CoordinateTable) -> dict:
    steps = []
    rules, handoff = _fresh_archive(tmpdir, materials)
    try:
        # Day 40 with H1 [25,50): C1 (D1,D2) is held -> not wholly held.
        ok_c1 = _custody_ok_for_case(rules, handoff, "C1", 40)
        # Day 40 C3 (D4) has no CLOSED trigger -> pending -> not wholly held.
        ok_c3 = _custody_ok_for_case(rules, handoff, "C3", 40)
        # Day 40 C2 (D3) is free -> wholly held.
        ok_c2 = _custody_ok_for_case(rules, handoff, "C2", 40)
    finally:
        rules.close()
        handoff.close()
    steps.append({"step": "custody_basis",
                  "C1_held": ok_c1, "C3_missing_trigger": ok_c3, "C2_free": ok_c2})

    wo = PlateWorkOrder(coord)
    wo.set_plate_state({"L": MISSING, "R": MISSING})
    wo.register_run(40)
    wo.add_reading(Reading("R40", "S40", 40, {"L": MISSING, "R": COMPLETE}, "HOLD-A"))
    wo.set_must_check(["L", "R"])
    wo.add_patch(Patch("P-R", "PHY-R", frozenset({"R"}), {"R": COMPLETE},
                       "S-R", frozenset({"pin-R"})))
    wo.validate()

    # Pattern would be correct, but custody for C1 is incomplete -> refused.
    sess = ProofingSession(wo, CRAFT_CATCH_FRAME)
    j_held = sess.process(SheetRequest("SH-held", "S40", 40, mounts=("P-R",),
                                       signer="HOLD-A", custody_ok=ok_c1))
    # Same pattern on the wholly-held C2 basis -> signable.
    sess2 = ProofingSession(wo, CRAFT_CATCH_FRAME)
    j_free = sess2.process(SheetRequest("SH-free", "S40", 40, mounts=("P-R",),
                                        signer="HOLD-A", custody_ok=ok_c2))
    steps.append({"step": "held_case", **j_held.as_dict()})
    steps.append({"step": "free_case", **j_free.as_dict()})
    expected = {
        "C1_not_wholly_held": not ok_c1,
        "C3_not_wholly_held": not ok_c3,
        "C2_wholly_held": ok_c2,
        "held_case_refused": (not j_held.signable and any(
            r["kind"] == "custody_incomplete" for r in j_held.refusals)),
        "free_case_signs": j_free.signable,
    }
    ok = all(expected.values())
    return {"ok": ok, "expected": expected, "steps": steps}


# ---------------------------------------------------------------------------
# Case 5: a new institution holds only a plan (移交预案).  Its re-reading of the
# torn corner cannot replace the custodian's old reading.  Its sibling member
# also lacks a due trigger, so one cannot fake a completed handoff to sign.
# ---------------------------------------------------------------------------
def case_plan_only_cannot_depose(tmpdir: str, materials: str,
                                 coord: CoordinateTable) -> dict:
    steps = []
    wo = PlateWorkOrder(coord)
    wo.set_plate_state({"L": COMPLETE, "R": COMPLETE})
    wo.register_run(40)
    wo.add_reading(Reading(
        reading_id="OLD-40", sample_id="SAMP40", run=40,
        states={"L": COMPLETE, "R": COMPLETE}, custodian="HOLD-OLD",
    ))
    # New institution's re-read: corner is actually fine; only plan-backed.
    wo.add_reading(Reading(
        reading_id="NEW-40", sample_id="SAMP40", run=40,
        states={"L": COMPLETE, "R": COMPLETE}, custodian="HOLD-NEW",
        authority=AUTHORITY_PLAN_ONLY, plan_only=True, supersedes=("OLD-40",),
    ))
    wo.set_must_check(["L", "R"])
    wo.validate()

    sess = ProofingSession(wo, CRAFT_CATCH_FRAME)
    j = sess.process(SheetRequest("SH40", "SAMP40", 40, signer="HOLD-NEW",
                                  custody_ok=True))
    steps.append({"step": "plan_only_reread_tried", **j.as_dict()})
    expected = {
        "old_reading_survives": any(
            r.reading_id == "OLD-40" for r in wo.authoritative_readings(40)),
        "plan_only_ignored": any(
            r.reading_id == "NEW-40" for r in wo.plan_only_readings(40)),
        "new_signer_rejected": (not j.signable and any(
            r["kind"] == "signer_not_custodian" for r in j.refusals)),
    }

    # Now the archive half: a sibling member (D4 in C3) lacks a due trigger, so
    # a fake whole-case handoff to the new institution must be refused.
    rules, handoff = _fresh_archive(tmpdir, materials)
    try:
        product = rules.build_product(40)
        rules.save_product("Q40", product)
        handoff.ingest_product("Q40", product)
        try:
            handoff.submit({
                "handoff_id": "FAKE", "case_id": "C3", "doc_ids": ["D4"],
                "from": "LOCAL", "to": "HOLD-NEW", "as_of_day": 40,
            })
            fake_code = "accepted"
        except Exception as exc:  # RejectError
            fake_code = getattr(exc, "code", type(exc).__name__)
        keep = {c: handoff.current_custodian(c) for c in handoff.known_cases()}
    finally:
        rules.close()
        handoff.close()
    steps.append({"step": "fake_sibling_handoff", "code": fake_code,
                  "custody": keep})
    expected["fake_handoff_refused"] = fake_code == "ineligible_members"
    expected["custody_unchanged"] = keep.get("C3") == "LOCAL"
    ok = all(expected.values())
    return {"ok": ok, "expected": expected, "steps": steps}


# ---------------------------------------------------------------------------
# Case 6: two consecutive proofs both need R complete; one also needs L missing,
# the other L complete.  Single physical piece for R; with a dismantle window it
# can swap frames.  But if the repair film is already on sheet 1 and there is no
# second piece, sheet 2 cannot borrow it via sheet 1's signed proof.
# ---------------------------------------------------------------------------
def case_two_runs_single_piece(coord: CoordinateTable) -> dict:
    steps = []
    wo = PlateWorkOrder(coord)
    wo.set_plate_state({"L": MISSING, "R": MISSING})
    wo.register_run(50)
    wo.register_run(51)
    # Between run 50 and 51 the old L notch disappeared, so the full reading
    # needs an evidenced whole-face regrind to explain it (new notches may
    # appear freely; old ones only vanish after a proven regrind).
    wo.add_regrind(Regrind("RG-50", day=50, evidence_id="EV-RG-50",
                           note="L notch ground away before run 51"))
    # Old reading says L missing, R complete (run 50).
    wo.add_reading(Reading(
        reading_id="RD50", sample_id="SAMP50", run=50,
        states={"L": MISSING, "R": COMPLETE}, custodian="HOLD-D",
    ))
    # Reading for run 51 says L complete, R complete.
    wo.add_reading(Reading(
        reading_id="RD51", sample_id="SAMP51", run=51,
        states={"L": COMPLETE, "R": COMPLETE}, custodian="HOLD-D",
    ))
    wo.set_must_check(["L", "R"])
    # Only piece for R.
    wo.add_patch(Patch(
        patch_id="P-R", physical_id="PHY-R", covers=frozenset({"R"}),
        emits={"R": COMPLETE}, slot="S-R", pins=frozenset({"pin-R"}),
    ))
    # A piece that restores L for the second proof.
    wo.add_patch(Patch(
        patch_id="P-L", physical_id="PHY-L", covers=frozenset({"L"}),
        emits={"L": COMPLETE}, slot="S-L", pins=frozenset({"pin-L"}),
    ))
    wo.validate()

    sess = ProofingSession(wo, CRAFT_CATCH_FRAME)
    j1 = sess.process(SheetRequest("SH-A", "SAMP50", 50, mounts=("P-R",),
                                   signer="HOLD-D"))
    steps.append({"step": "run50_R", **j1.as_dict(), "ok": j1.signable})

    # Without dismantling, the single R piece is still pinned on SH-A.
    j2_blocked = sess.process(SheetRequest("SH-B", "SAMP51", 51,
                                           mounts=("P-R", "P-L"), signer="HOLD-D"))
    steps.append({"step": "run51_no_swap", **j2_blocked.as_dict()})
    # With a scan-confirmed dismantle of SH-A, the same piece can be lent.
    j2_swap = sess.process(SheetRequest(
        "SH-B", "SAMP51", 51, mounts=("P-R", "P-L"), release=("SH-A",),
        scan_release_confirmed=True, signer="HOLD-D"))
    steps.append({"step": "run51_with_swap", **j2_swap.as_dict()})

    expected = {
        "run50_signs": j1.signable,
        "run51_blocked_without_swap": (not j2_blocked.signable and any(
            r["kind"] == "piece_in_use" for r in j2_blocked.refusals)),
        "run51_signs_after_swap": j2_swap.signable,
    }

    # Film variant: repair film already on sheet 1, no second physical piece.
    # Sheet 2 cannot borrow it, and the origin right for R is already spent.
    wof = PlateWorkOrder(coord)
    wof.set_plate_state({"L": MISSING, "R": MISSING})
    wof.register_run(60)
    wof.register_run(61)
    wof.add_reading(Reading(
        reading_id="RDF60", sample_id="SAMP60", run=60,
        states={"L": MISSING, "R": COMPLETE}, custodian="HOLD-E"))
    wof.add_reading(Reading(
        reading_id="RDF61", sample_id="SAMP61", run=61,
        states={"L": MISSING, "R": COMPLETE}, custodian="HOLD-E"))
    wof.set_must_check(["L", "R"])
    wof.add_patch(Patch(
        patch_id="FILM-R", physical_id="FILM-R-PHY", covers=frozenset({"R"}),
        emits={"R": COMPLETE}, slot="S-R", film=True))
    wof.validate()
    sessf = ProofingSession(wof, CRAFT_FILM)
    f1 = sessf.process(SheetRequest("SH-1", "SAMP60", 60, mounts=("FILM-R",),
                                    signer="HOLD-E"))
    f2 = sessf.process(SheetRequest("SH-2", "SAMP61", 61, mounts=("FILM-R",),
                                    signer="HOLD-E"))
    steps.append({"step": "film_sheet1", **f1.as_dict()})
    steps.append({"step": "film_sheet2_no_second_piece", **f2.as_dict()})
    expected["film_sheet1_signs"] = f1.signable
    expected["film_sheet2_blocked_no_second_piece"] = (not f2.signable and any(
        r["kind"] == "film_spent" for r in f2.refusals))
    ok = all(expected.values())
    return {"ok": ok, "expected": expected, "steps": steps}


# ---------------------------------------------------------------------------
# Case 7: pattern correct but no clamp slot free; a film may be approved
# independently (a promise may be given before actually applying film).
# ---------------------------------------------------------------------------
def case_pattern_ok_no_slot_film_approval(coord: CoordinateTable) -> dict:
    steps = []
    wo = PlateWorkOrder(coord)
    wo.set_plate_state({"L": MISSING, "R": MISSING})
    wo.register_run(70)
    wo.add_reading(Reading(
        reading_id="RD70", sample_id="SAMP70", run=70,
        states={"L": MISSING, "R": COMPLETE}, custodian="HOLD-F"))
    wo.set_must_check(["L", "R"])
    wo.add_patch(Patch(
        patch_id="P-R", physical_id="PHY-R", covers=frozenset({"R"}),
        emits={"R": COMPLETE}, slot="S-R", pins=frozenset({"pin-R"})))
    # A film twin covering the same region.
    wo.add_patch(Patch(
        patch_id="FILM-R", physical_id="FILM-R-PHY", covers=frozenset({"R"}),
        emits={"R": COMPLETE}, slot="S-R", film=True))
    wo.validate()

    # Catch-frame: slot is locked -> proof refused, but pattern is correct and an
    # independent film approval is available.
    sess = ProofingSession(wo, CRAFT_CATCH_FRAME, locked_slots={"S-R"},
                           locked_pins={"pin-R"})
    j = sess.process(SheetRequest("SH70", "SAMP70", 70, mounts=("P-R",),
                                  signer="HOLD-F", approve_film=True))
    steps.append({"step": "catch_frame_no_slot", **j.as_dict()})
    expected = {
        "pattern_correct": all(
            j.printed.get(p) == req for p, req in wo.point_requirements(70)[0].items()),
        "catch_frame_refused": (not j.signable and any(
            r["kind"] == "slot_locked" for r in j.refusals)),
        "film_approval_available": any("independent film approval" in n for n in j.notes),
    }
    # Applying the film independently does satisfy the proof.
    sessf = ProofingSession(wo, CRAFT_FILM)
    jf = sessf.process(SheetRequest("SH70f", "SAMP70", 70, mounts=("FILM-R",),
                                    signer="HOLD-F"))
    steps.append({"step": "film_applied", **jf.as_dict()})
    expected["film_signs"] = jf.signable
    ok = all(expected.values())
    return {"ok": ok, "expected": expected, "steps": steps}


# ---------------------------------------------------------------------------
# Case 8: the work order must list, per judgment: signable samples, their slots,
# spent film and the exact refusal positions.
# ---------------------------------------------------------------------------
def case_full_work_order(coord: CoordinateTable) -> dict:
    steps = []
    wo = PlateWorkOrder(coord)
    wo.set_plate_state({"L": MISSING, "R": MISSING})
    for run in (80, 81, 82):
        wo.register_run(run)
    # L goes missing (run 80) -> complete (run 81); explain it with an
    # evidenced whole-face regrind between the two runs.
    wo.add_regrind(Regrind("RG-80", day=80, evidence_id="EV-RG-80",
                           note="L notch ground away before run 81"))
    wo.add_reading(Reading("R80", "S80", 80,
                           {"L": MISSING, "R": COMPLETE}, "HOLD-G"))
    wo.add_reading(Reading("R81", "S81", 81,
                           {"L": COMPLETE, "R": COMPLETE}, "HOLD-G"))
    wo.add_reading(Reading("R82", "S82", 82,
                           {"L": MISSING, "R": TORN}, "HOLD-G"))
    wo.set_must_check(["L", "R"])
    wo.add_patch(Patch("P-R", "PHY-R", frozenset({"R"}), {"R": COMPLETE},
                       "S-R", frozenset({"pin-R"})))
    wo.add_patch(Patch("P-L", "PHY-L", frozenset({"L"}), {"L": COMPLETE},
                       "S-L", frozenset({"pin-L"})))
    wo.add_patch(Patch("FILM-R", "FILM-R-PHY", frozenset({"R"}), {"R": COMPLETE},
                       "S-R", film=True))
    wo.validate()

    # One work order listing: each proof is judged independently against its own
    # reading.  R is a single piece, so signable sheets are evaluated in fresh
    # sessions (the inventory swap across sheets is Case 6's concern).
    sess80 = ProofingSession(wo, CRAFT_CATCH_FRAME)
    sess81 = ProofingSession(wo, CRAFT_CATCH_FRAME)
    sess82 = ProofingSession(wo, CRAFT_CATCH_FRAME)
    j80 = sess80.process(SheetRequest("S80", "S80", 80, mounts=("P-R",), signer="HOLD-G"))
    j81 = sess81.process(SheetRequest("S81", "S81", 81, mounts=("P-L", "P-R"),
                                      signer="HOLD-G"))
    j82 = sess82.process(SheetRequest("S82", "S82", 82, mounts=("P-R",), signer="HOLD-G"))
    results = [
        {"sheet": "S80", "sample": "S80", "run": 80, **j80.as_dict()},
        {"sheet": "S81", "sample": "S81", "run": 81, **j81.as_dict()},
        {"sheet": "S82", "sample": "S82", "run": 82, **j82.as_dict()},
    ]
    batch = {
        "craft": CRAFT_CATCH_FRAME,
        "signed": [r["sheet"] for r in results if r["signable"]],
        "film_consumed": [],
        "origin_consumed": [],
        "results": results,
    }
    steps.append({"step": "batch", **batch})
    expected = {
        "S80_signs": "S80" in batch["signed"],
        "S81_signs": "S81" in batch["signed"],
        "S82_refused_torn": any(
            r["sheet"] == "S82" and any(x["kind"] == "unverifiable"
                                        and x.get("point") == "R"
                                        for x in r["refusals"])
            for r in batch["results"]),
        "S80_slot_listed": any(
            r["sheet"] == "S80" and r["used_mounts"] == ["P-R"]
            for r in batch["results"]),
    }
    ok = all(expected.values())
    return {"ok": ok, "expected": expected, "steps": steps}


# ---------------------------------------------------------------------------
# Case 9: single-sheet comparison only mounts the ticket's clamp-pin piece and
# keeps the original judgment (no extra pieces smuggled in).
# ---------------------------------------------------------------------------
def case_single_sheet_original_judgment(coord: CoordinateTable) -> dict:
    wo = PlateWorkOrder(coord)
    wo.set_plate_state({"L": MISSING, "R": MISSING})
    wo.register_run(90)
    wo.add_reading(Reading("R90", "S90", 90,
                           {"L": MISSING, "R": COMPLETE}, "HOLD-H"))
    wo.set_must_check(["L", "R"])
    wo.add_patch(Patch("P-R", "PHY-R", frozenset({"R"}), {"R": COMPLETE},
                       "S-R", frozenset({"pin-R"})))
    # A tempting extra piece that is NOT on the ticket for this comparison.
    wo.add_patch(Patch("P-LX", "PHY-LX", frozenset({"L"}), {"L": COMPLETE},
                       "S-L", frozenset({"pin-L"})))
    wo.validate()
    sess = ProofingSession(wo, CRAFT_CATCH_FRAME)
    allowed = {"P-R"}
    req = SheetRequest("S90", "S90", 90, mounts=("P-R",), signer="HOLD-H")
    # Guard: only ticket pieces may be mounted for a single-sheet comparison.
    offending = [p for p in req.mounts if p not in allowed]
    if offending:
        return {"ok": False, "expected": {"only_ticket_pieces": False},
                "steps": [{"offending": offending}]}
    j = sess.process(req)
    expected = {
        "only_ticket_pieces": not offending,
        "signs": j.signable,
        "printed_keeps_L_missing": j.printed.get("L") == MISSING,
    }
    return {"ok": all(expected.values()), "expected": expected,
            "steps": [{"step": "single_sheet", **j.as_dict()}]}


# ---------------------------------------------------------------------------
# Aggregate: run everything and write artifacts (JSON + sqlite-free).
# ---------------------------------------------------------------------------
def run_all(materials_dir: str = "materials/751",
            out_dir: str = "artifacts/workorder") -> dict:
    coord = load_coordinate_table(os.path.join(materials_dir, "coordinate_table.json"))
    with tempfile.TemporaryDirectory() as tmp:
        results = {
            "case_right_patch_signable": case_right_patch_signable(coord),
            "case_restore_and_locked_slot": case_restore_and_locked_slot(coord),
            "case_torn_spanning_regrind": case_torn_spanning_regrind(coord),
            "case_regrind_explains_notch": case_regrind_explains_notch(coord),
            "case_held_member_blocks_proof": case_held_member_blocks_proof(
                tmp, materials_dir, coord),
            "case_plan_only_cannot_depose": case_plan_only_cannot_depose(
                tmp, materials_dir, coord),
            "case_two_runs_single_piece": case_two_runs_single_piece(coord),
            "case_pattern_ok_no_slot_film_approval": case_pattern_ok_no_slot_film_approval(coord),
            "case_full_work_order": case_full_work_order(coord),
            "case_single_sheet_original_judgment": case_single_sheet_original_judgment(coord),
        }
    ok = all(r["ok"] for r in results.values())
    payload = {"ok": ok, "coordinate_table": coord.plate,
               "points": list(coord.points), "cases": results}
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "workorder.json"), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    with open(os.path.join(out_dir, "workorder_summary.json"), "w", encoding="utf-8") as fh:
        json.dump(work_order_summary(payload), fh, ensure_ascii=False, indent=2,
                  sort_keys=True)
        fh.write("\n")
    return payload


def work_order_summary(payload: dict) -> dict:
    """The operator-facing work order: what to mount, what signs, what refuses.

    Derived purely from the recorded case steps, so it cannot drift from the
    executed behavior.
    """
    cases = payload["cases"]

    def sheets_of(case: str) -> list[dict]:
        out = []
        for st in cases[case]["steps"]:
            if "results" not in st:
                continue
            for r in st["results"]:
                out.append({
                    "sheet": r["sheet"],
                    "run": r["run"],
                    "signable": r["signable"],
                    "mounts": r["used_mounts"],
                    "refusals": [{"kind": x["kind"], "point": x.get("point"),
                                  "detail": x["detail"]} for x in r["refusals"]],
                })
        return out

    all_sheets = []
    for st in cases["case_full_work_order"]["steps"]:
        if "results" in st:
            for r in st["results"]:
                all_sheets.append({
                    "sheet": r["sheet"], "run": r["run"],
                    "signable": r["signable"], "mounts": r["used_mounts"],
                    "refusals": [{"kind": x["kind"], "point": x.get("point")}
                                 for x in r["refusals"]],
                })

    return {
        "ok": payload["ok"],
        "coordinate_table": payload["coordinate_table"],
        "points": payload["points"],
        "chosen_craft": "catch_frame (mountable, reusable piece; film only via "
                        "independent approval)",
        "signable_samples": [s["sheet"] for s in all_sheets if s["signable"]],
        "refused_samples": [s for s in all_sheets if not s["signable"]],
        "full_work_order_sheets": all_sheets,
        "cases": {
            name: {"ok": c["ok"], "expected": c["expected"]}
            for name, c in cases.items()
        },
    }
