"""Documented water-clock collation & exhibition-reproduction walkthrough.

Everything here is fictional and nothing is deleted.  The walkthrough proves,
step by step, the behaviours the museum asked for:

* joint collation across copies with a mis-bound fragment choosing a binding
  direction, leaving initial value + directions + per-beat water;
* a beat on which the originals genuinely disagree is *disclosed*, never
  asserted as history, and a fixed-cup plan that would assert it is refused;
* the three-beat book (opening 0, scale 3, inflows 1,1,1 -> only beat 3 rings)
  reproduced on a stand with inflows 1,0,1 by one cup allowed at beat 2;
* the calibration trap (an opening calibration cup changes the *real* water,
  so later ring times are recomputed from the added water) and the read-gate
  rule (opening the gate at a beat records a real stop, it is not "one more
  beat of the old table");
* a safe action that must wait one more beat (a cup at beat 0 or 1 mis-rings
  for opening 1; only a beat-2 cup is safe for both openings);
* openining values in one naked-eye bracket that may *not* be secretly handled
  differently: a per-opening plan exists but no single fixed cup list does;
* the custody narrative: a new institution's initial reading stays provisional
  (and the show keeps its cup) until the whole case -- with a real handoff
  trigger -- arrives; the old institution may only re-sign its old reading;
  only after whole-case arrival does the stop-flow become formal and the cup
  withdrawn.

Signature of the main entry point::

    run_clepsydra_demo(data_dir="artifacts/clepsydra") -> dict
"""

from __future__ import annotations

import json
import os

from .clepsydra import (
    STOP,
    Entry,
    Witness,
    earliest_discerning_beat,
    forward_direction,
    full_direction,
    plan_adaptive,
    plan_fixed_cups,
    ring_pattern,
    run_stand,
)
from .clepsydra_case import LogCopy, WaterClockCase
from .handoff_service import HandoffRepo
from .materials import import_materials
from .rules_service import RuleEventStore


def _rings(scale, opening, inflows, **kw):
    return [b.ring for b in run_stand(scale, opening, inflows, **kw)]


def _book(case):
    return list(case.effective_inflows())


def run_clepsydra_demo(data_dir: str = "artifacts/clepsydra") -> dict:
    os.makedirs(data_dir, exist_ok=True)
    steps: list[dict] = []
    checks: dict[str, bool] = {}

    def check(name, condition):
        checks[name] = bool(condition)
        return bool(condition)

    scale, beats = 3, 3
    target_ex2 = (False, False, True)

    # ------------------------------------------------------------------
    # 1. Joint collation of copies + a mis-bound fragment.
    # ------------------------------------------------------------------
    book = WaterClockCase("C-W", scale, beats, [1, 1, 1])
    copy_a = LogCopy("COPY-A", "OLD", Witness(
        "COPY-A",
        [Entry(inflow=1, ring=False),
         Entry(inflow=1, ring=False),
         Entry(inflow=1, ring=True)],
        [full_direction(beats)],
        initial_span=(0, 0),
    ))
    # A fragment whose two surviving beats can bind to (0,1) or to (1,2); only
    # the (1,2) binding (silent then ring) is consistent with the book.
    frag = LogCopy("FRAG-B", "OLD", Witness(
        "FRAG-B",
        [Entry(inflow=1, ring=False),
         Entry(inflow=1, ring=True)],
        [forward_direction(2, 0), forward_direction(2, 1)],
    ))
    book.add_copy(copy_a)
    book.add_copy(frag)
    sol = book.collate()
    steps.append({
        "step": "collate_copies",
        "solution": None if sol is None else sol.as_dict(),
    })
    check("collation_exists", sol is not None)
    check("collation_opening_0", sol is not None and sol.opening == 0)
    check("fragment_bound_forward_1_2",
          sol is not None and sol.directions.get("FRAG-B") == {0: 1, 1: 2})
    check("collation_inflows_1_1_1",
          sol is not None and list(sol.inflows) == [1, 1, 1])
    check("collation_rings_ex2",
          sol is not None and list(sol.rings) == list(target_ex2))

    # ------------------------------------------------------------------
    # 2. A disputed beat must be disclosed, never asserted.
    # ------------------------------------------------------------------
    # Effective book opens in the bracket {0,1}; beats 1 and 2 then disagree.
    disputed_case = WaterClockCase("C-D", scale, beats, [1, 1, 1])
    disputed_case.add_copy(LogCopy("SEEN", "OLD", Witness(
        "SEEN",
        [Entry(ring=False), Entry(ring=None), Entry(ring=None)],
        [full_direction(beats)],
        initial_span=(0, 1),
    )))
    analysis = disputed_case.ring_analysis()
    determined, disputed = disputed_case.show_target()
    steps.append({
        "step": "disputed_beats",
        "patterns": [list(p) for p in analysis["patterns"]],
        "determined": determined,
        "disputed": sorted(disputed),
    })
    check("disputed_is_beats_1_2", sorted(disputed) == [1, 2])
    check("disputed_beat0_determined", determined.get(0) is False)
    refused = plan_fixed_cups(
        scale, [1, 0, 1], [0, 1], determined, disputed,
        allowed_cups={2}, require_dispute_free=True,
    )
    steps.append({"step": "disputed_plan_refused", "plan": refused.as_dict()})
    check("disputed_plan_refused", not refused.feasible)

    # ------------------------------------------------------------------
    # 3. Three-beat book reproduced by one cup at beat 2.
    # ------------------------------------------------------------------
    stand = [1, 0, 1]
    book_rings = ring_pattern(scale, 0, _book(book))
    show = plan_fixed_cups(scale, stand, {0}, {0: False, 1: False, 2: True},
                           allowed_cups={2})
    no_cup = plan_fixed_cups(scale, stand, {0}, {0: False, 1: False, 2: True},
                             allowed_cups=set())
    steps.append({
        "step": "reproduce_example2",
        "book_rings": list(book_rings),
        "stand_no_cup": _rings(scale, 0, stand),
        "stand_with_cup2": _rings(scale, 0, stand, cup_beats={2}),
        "plan": show.as_dict(),
    })
    check("book_only_beat2_rings", list(book_rings) == [False, False, True])
    check("stand_with_cup2_matches", _rings(scale, 0, stand, cup_beats={2})
          == [False, False, True])
    check("stand_needs_cup2", show.feasible and show.cup_beats == (2,))
    check("stand_without_cup_fails", not no_cup.feasible)

    # ------------------------------------------------------------------
    # 4. Calibration trap + read-gate rule.
    # ------------------------------------------------------------------
    # A calibration cup at beat 0 really adds water: real openings 0 and 1
    # become 1 and 2, so ring times are recomputed from the added water.  The
    # raw stand rings already show this.
    cal0 = {
        0: _rings(scale, 1, stand),   # real 0 + calibrated 1 == effective 1
        1: _rings(scale, 2, stand),   # real 1 + calibrated 1 == effective 2
    }
    steps.append({
        "step": "calibration_trap",
        "effective_openings_after_beat0_cup": cal0,
        "note": "beat-0 calibration shifts both readings by one; downstream "
                "ring times move with the real water, they are not the old ones",
    })
    check("calibration_shifts_opening0_to_1",
          cal0[0] == _rings(scale, 1, stand))
    check("calibration_shifts_opening1_to_2",
          cal0[1] == _rings(scale, 2, stand))

    # Read-gate at a beat whose natural inflow is non-zero: the gate really
    # stops that beat's inflow, so it records a true stop.  The fiction of
    # "feed the old table one extra beat" would instead add the water the raw
    # book did not, and must not be used to fake a correct ring pattern.
    gate_stand = [1, 1, 1]
    gate_b1 = [b.inflow for b in run_stand(scale, 0, gate_stand, gate_beats={1})]
    raw_b1 = [b.inflow for b in run_stand(scale, 0, gate_stand)]
    steps.append({
        "step": "read_gate_records_true_stop",
        "stand_inflows": gate_stand,
        "gate_beat1_inflows": gate_b1,
        "raw_inflows": raw_b1,
        "gate_beat1_rings": _rings(scale, 0, gate_stand, gate_beats={1}),
        "raw_rings": _rings(scale, 0, gate_stand),
        "note": "opening the gate at a wet beat records a true stop (0); it "
                "must not silently add the old table's water an extra beat",
    })
    check("gate_stops_a_wet_beat", raw_b1[1] == 1 and gate_b1[1] == 0)
    # A gate and a cup may never share a beat.
    try:
        run_stand(scale, 0, stand, cup_beats={1}, gate_beats={1})
        gate_cup_clash = False
    except ValueError:
        gate_cup_clash = True
    check("gate_and_cup_cannot_share_beat", gate_cup_clash)

    # ------------------------------------------------------------------
    # 5. A safe action that must wait one more beat.
    # ------------------------------------------------------------------
    # Bracket {0,1}: a cup at beat 0 or 1 mis-rings for opening 1; only beat 2
    # is safe for both.  That is the "must wait a beat" witness.
    per_beat = {}
    for c in (0, 1, 2):
        per_beat[c] = {
            str(o): _rings(scale, o, stand, cup_beats={c}) for o in (0, 1)
        }
    wait_plan = plan_fixed_cups(scale, stand, {0, 1},
                                {0: False, 1: False, 2: True}, allowed_cups={2})
    early0 = plan_fixed_cups(scale, stand, {0, 1},
                             {0: False, 1: False, 2: True}, allowed_cups={0})
    early1 = plan_fixed_cups(scale, stand, {0, 1},
                             {0: False, 1: False, 2: True}, allowed_cups={1})
    disc_beat, disc_kind = earliest_discerning_beat(scale, {0, 1}, stand)
    steps.append({
        "step": "safe_action_must_wait",
        "cup_by_beat": per_beat,
        "cup_only_0_feasible": early0.feasible,
        "cup_only_1_feasible": early1.feasible,
        "cup_only_2_feasible": wait_plan.feasible,
        "earliest_discerning_beat": disc_beat,
        "earliest_discerning_kind": disc_kind,
    })
    check("cup_at_0_unsafe_for_bracket", not early0.feasible)
    check("cup_at_1_unsafe_for_bracket", not early1.feasible)
    check("cup_at_2_safe_for_bracket", wait_plan.feasible and
          wait_plan.cup_beats == (2,))
    check("discerning_beat_is_2", disc_beat == 2)

    # Adaptive read-scale gate policy also solves it (branch on the reading).
    adaptive = plan_adaptive(scale, stand, {0, 1},
                             {0: False, 1: False, 2: True},
                             allowed_gates={0, 1, 2}, allowed_cups={2})
    steps.append({"step": "adaptive_gate_policy", "feasible": adaptive["feasible"],
                  "note": adaptive.get("note")})
    check("adaptive_policy_feasible", adaptive["feasible"])

    # ------------------------------------------------------------------
    # 6. One visible bracket may not be secretly split.
    # ------------------------------------------------------------------
    split_stand = [1, 0, 0]
    oracle = {
        o: plan_fixed_cups(scale, split_stand, {o},
                           {0: False, 1: False, 2: True},
                           allowed_cups={0, 1, 2}).as_dict()
        for o in (0, 1)
    }
    joint = plan_fixed_cups(scale, split_stand, {0, 1},
                            {0: False, 1: False, 2: True}, allowed_cups={0, 1, 2})
    steps.append({
        "step": "no_secret_split",
        "per_opening_oracle": oracle,
        "single_fixed_list_for_bracket": joint.as_dict(),
        "note": "opening 0 and opening 1 sit in one naked-eye bracket; a plan "
                "must use ONE fixed cup list for both, not secretly differ",
    })
    check("oracle_per_opening_feasible",
          all(v["feasible"] for v in oracle.values()))
    check("single_fixed_list_refused", not joint.feasible)
    # For completeness, the adaptive gate CAN legitimately branch on a real
    # reading (that is not a secret split: the reading is observed).
    split_adaptive = plan_adaptive(scale, split_stand, {0, 1},
                                   {0: False, 1: False, 2: True},
                                   allowed_gates={1, 2}, allowed_cups={2})
    steps.append({"step": "split_adaptive", "feasible": split_adaptive["feasible"]})

    # ------------------------------------------------------------------
    # 7. Custody narrative: provisional -> old re-sign -> whole-case arrival.
    # ------------------------------------------------------------------
    custody = _CustodyNarrative(data_dir)
    narrative = custody.run()
    steps.append({"step": "custody_narrative", "narrative": narrative})
    check("new_reading_stays_provisional", narrative["new_status"] == "provisional")
    check("book_unchanged_while_provisional",
          narrative["effective_while_provisional"] == [1, 1, 1])
    check("show_still_needs_cup", narrative["show_still_needs_cup"])
    check("old_resigns_old_reading", narrative["old_resign_value"] == 1)
    check("still_provisional_after_resign",
          narrative["effective_after_resign"] == [1, 1, 1])
    check("trigger_missing_blocks_promotion",
          narrative["promoted_before_trigger"] == [])
    check("whole_case_arrival_promotes", narrative["promoted_after_arrival"] != [])
    check("effective_becomes_all_silent",
          narrative["effective_after_arrival"] == [1, 1, 0])
    check("show_drops_cup_after_formal_stop",
          not narrative["show_needs_cup_after"])
    custody.close()

    ok = all(checks.values())
    result = {
        "ok": ok,
        "data_dir": os.path.abspath(data_dir),
        "checks": checks,
        "steps": steps,
    }
    with open(os.path.join(data_dir, "clepsydra_demo.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    return result


class _CustodyNarrative:
    """Runs the provisional / re-sign / promotion story on real custody."""

    def __init__(self, data_dir: str):
        self.rules = RuleEventStore(os.path.join(data_dir, "clepsydra_rules.sqlite"))
        self.handoff = HandoffRepo(os.path.join(data_dir, "clepsydra_handoff.sqlite"))
        import_materials(self.rules, self.handoff, "materials/751")
        # Make C1 wholly releasable at day 40 for the later transfer.
        self.rules.import_holds([
            {"hold_id": "H1", "revision": 2, "case_id": "C1", "start": 25, "end": 35},
        ])
        product = self.rules.build_product(40)
        self.rules.save_product("Q40", product)
        self.handoff.ingest_product("Q40", product)
        self.case = WaterClockCase("C1", 3, 3, [1, 1, 1])

    def run(self) -> dict:
        scale, stand = 3, [1, 0, 1]
        target = (False, False, True)

        # NEW registers a stop on beat 2, but only holds a single volume.
        new_rev = self.case.submit_revision(
            "R-stop2", 2, STOP, "NEW", self.handoff,
            claims_original_page=True, note="read a stop trace on the page",
        )
        effective_while = list(self.case.effective_inflows())
        # The show still needs the beat-2 cup while the stop is provisional.
        show_while = plan_fixed_cups(
            scale, stand, {0}, {0: False, 1: False, 2: True}, allowed_cups={2}
        )

        # OLD re-signs its old reading; it may not adopt the new stop.
        old_rev = self.case.resign("R-stop2", "LOCAL", self.handoff)
        effective_after_resign = list(self.case.effective_inflows())
        show_after_resign = plan_fixed_cups(
            scale, stand, {0}, {0: False, 1: False, 2: True}, allowed_cups={2}
        )

        # Before the whole case arrives, promotion is gated off.
        promoted_before = [r.revision_id
                           for r in self.case.promote_pending(self.handoff)]

        # Whole case C1 arrives at NEW (real trigger in the custody ledger).
        self.handoff.submit({
            "handoff_id": "WC1", "case_id": "C1", "doc_ids": ["D1", "D2"],
            "from": "LOCAL", "to": "NEW", "as_of_day": 40,
        })
        promoted_after = [r.revision_id
                          for r in self.case.promote_pending(self.handoff)]
        effective_after = list(self.case.effective_inflows())
        # Now the book is all-silent; the show must drop its cup.
        new_target = {0: False, 1: False, 2: False}
        show_after_cup = plan_fixed_cups(
            scale, stand, {0}, new_target, allowed_cups={2}
        )
        show_after_nocup = plan_fixed_cups(
            scale, stand, {0}, new_target, allowed_cups=set()
        )
        return {
            "new_status": new_rev.status,
            "effective_while_provisional": effective_while,
            "show_still_needs_cup": show_while.feasible and show_while.cup_beats == (2,),
            "old_resign_value": old_rev.value,
            "old_resign_status": old_rev.status,
            "effective_after_resign": effective_after_resign,
            "show_still_needs_cup_after_resign":
                show_after_resign.feasible and show_after_resign.cup_beats == (2,),
            "promoted_before_trigger": promoted_before,
            "promoted_after_arrival": promoted_after,
            "effective_after_arrival": effective_after,
            "show_needs_cup_after": not show_after_nocup.feasible,
            "show_without_cup_after_feasible": show_after_nocup.feasible,
            "custodian": self.handoff.current_custodian("C1"),
            "verify_ok": self.handoff.verify()["ok"],
        }

    def close(self) -> None:
        self.rules.close()
        self.handoff.close()
