"""Proofing engine: readings, patches, clamp slots and one-time film.

This is the workshop side.  It consumes *readings* (appraisals of sample
impressions) and answers: given the physical plate plus a set of local patches,
which proof sheets can be signed, which must-check points block, and why.

Design notes
------------
* The engine is deliberately independent of the archive store.  Custody and
  whole-case eligibility enter only as a ``custody_ok`` boolean computed by the
  caller from :class:`archive_handoff.HandoffRepo`; the engine never recomputes
  dates.
* Readings can conflict.  Only *live* readings count; a reading is deposed by a
  later (or restored) reading that names it in ``supersedes`` **and** has at
  least as much authority.  A plan-only reading therefore can never depose a
  custodian's signed reading.
* ``TORN`` (纸角缺失) is absence of evidence and is never folded into COMPLETE or
  MISSING.  A must-check point whose only witnesses are torn cannot be verified.
* Two patching crafts are modelled:

  - ``catch_frame`` -- the piece occupies a clamp slot and pins while mounted.
    Reusing the same physical piece on another sheet requires dismantling the
    old frame and a scan-confirmed release; otherwise it still occupies its
    pins ("不扫码确认拆掉时仍占脚").
  - ``film`` -- the piece takes no pins, but each application permanently spends
    the piece and the local reprint right of the covered origin points.

Limits (enforced when a work order is validated): at most 8 points, 12 print
runs and 4 patches.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional

from .model import (
    AUTHORITY_CUSTODIAN,
    AUTHORITY_PLAN_ONLY,
    COMPLETE,
    MISSING,
    POINT_STATES,
    TORN,
    Judgment,
    Patch,
    ProofError,
    Reading,
    Regrind,
)

MAX_POINTS = 8
MAX_RUNS = 12
MAX_PATCHES = 4

CRAFT_CATCH_FRAME = "catch_frame"
CRAFT_FILM = "film"


# ---------------------------------------------------------------- coordinate table
@dataclass(frozen=True)
class CoordinateTable:
    """The local coordinate grid the workshop rules the plate into."""

    plate: str
    points: Mapping[str, dict]   # point -> {"x","y","slot","pins"}
    locked_slots: frozenset[str] = frozenset()
    locked_pins: frozenset[str] = frozenset()

    @classmethod
    def load(cls, path: str) -> "CoordinateTable":
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict) -> "CoordinateTable":
        points = raw["points"]
        if len(points) > MAX_POINTS:
            raise ProofError(
                "too_many_points", f"{len(points)} points > limit {MAX_POINTS}"
            )
        return cls(
            plate=raw.get("plate", "PLATE"),
            points={k: dict(v) for k, v in points.items()},
            locked_slots=frozenset(raw.get("locked_slots", [])),
            locked_pins=frozenset(raw.get("locked_pins", [])),
        )

    def slot_of(self, point: str) -> Optional[str]:
        return self.points.get(point, {}).get("slot")

    def pins_of(self, point: str) -> tuple[str, ...]:
        return tuple(self.points.get(point, {}).get("pins", ()))

    def coords(self) -> dict[str, tuple[int, int]]:
        return {p: (v["x"], v["y"]) for p, v in self.points.items()}


# ---------------------------------------------------------------- work order
class PlateWorkOrder:
    """The plate, its readings, its candidate patches and the must-check set."""

    def __init__(self, coordinate_table: CoordinateTable):
        self.coord = coordinate_table
        self.readings: dict[str, Reading] = {}
        self.readings_by_sample: dict[str, list[str]] = {}
        self.patches: dict[str, Patch] = {}
        self.plate_state: dict[str, str] = {
            p: v.get("state", MISSING) for p, v in coordinate_table.points.items()
        }
        self.must_check: set[str] = set()
        self.runs: set[int] = set()
        self.regrinds: dict[str, Regrind] = {}

    # ---- registration -------------------------------------------------------
    def set_plate_state(self, states: Mapping[str, str]) -> None:
        for p, s in states.items():
            self._require_point(p)
            if s not in POINT_STATES:
                raise ProofError("invalid_state", f"plate point {p} state {s!r}")
            self.plate_state[p] = s

    def add_regrind(self, regrind: Regrind) -> None:
        self._require_run(regrind.day)
        if regrind.regrind_id in self.regrinds:
            raise ProofError("duplicate_regrind", regrind.regrind_id)
        self.regrinds[regrind.regrind_id] = regrind

    def evidenced_regrind_at_or_before(self, day: int) -> Optional[Regrind]:
        """The latest evidenced regrind whose return day is <= ``day``."""
        hits = [r for r in self.regrinds.values() if r.day <= day and r.evidence_id]
        return max(hits, key=lambda r: (r.day, r.regrind_id)) if hits else None

    def explain_transitions(self) -> list[dict]:
        """Verify one coherent reading covering every sample / regrind / run.

        For every point, walk the chronological sequence of *definite* states
        across live authoritative readings.  The only legal way an old notch
        (MISSING) becomes COMPLETE is an evidenced whole-face regrind falling
        between the two observations.  Any such transition without a regrind is
        reported as an unexplainable gap.
        """
        violations: list[dict] = []
        # Gather, per point, (run, state) from live authoritative readings.
        obs: dict[str, list[tuple[int, str, str]]] = {}
        for sample_id, ids in self.readings_by_sample.items():
            live_ids = {r.reading_id for r in self.live_readings(sample_id)}
            for rid in ids:
                if rid not in live_ids:
                    continue
                r = self.readings[rid]
                if r.plan_only or r.authority < AUTHORITY_CUSTODIAN:
                    continue
                for p, s in r.states.items():
                    if s in POINT_STATES:
                        obs.setdefault(p, []).append((r.run, s, rid))
        for p, seq in obs.items():
            seq.sort(key=lambda t: (t[0], t[2]))
            prev_missing_run: Optional[int] = None
            for run, state, rid in seq:
                if state == MISSING:
                    prev_missing_run = run
                elif state == COMPLETE and prev_missing_run is not None:
                    reg = self.evidenced_regrind_at_or_before(run)
                    if reg is None or reg.day < prev_missing_run:
                        violations.append({
                            "point": p,
                            "from_missing_run": prev_missing_run,
                            "to_complete_run": run,
                            "detail": f"point {p}: notch at run {prev_missing_run} becomes "
                                      f"complete at run {run} without an evidenced regrind",
                        })
                    prev_missing_run = None
        return violations

    def add_reading(self, reading: Reading) -> None:
        self._require_run(reading.run)
        for p in reading.states:
            self._require_point(p)
        if reading.reading_id in self.readings:
            raise ProofError("duplicate_reading", reading.reading_id)
        self.readings[reading.reading_id] = reading
        self.readings_by_sample.setdefault(reading.sample_id, []).append(reading.reading_id)

    def add_patch(self, patch: Patch) -> None:
        if len(self.patches) >= MAX_PATCHES and patch.patch_id not in self.patches:
            raise ProofError(
                "too_many_patches", f"would exceed patch limit {MAX_PATCHES}"
            )
        for p in patch.covers:
            self._require_point(p)
        self.patches[patch.patch_id] = patch

    def set_must_check(self, points: Iterable[str]) -> None:
        for p in points:
            self._require_point(p)
        self.must_check = set(points)

    def register_run(self, run: int) -> None:
        self._require_run(run)

    # ---- helpers ------------------------------------------------------------
    def _require_point(self, point: str) -> None:
        if point not in self.coord.points:
            raise ProofError("unknown_point", f"point {point!r} not on coordinate table")

    def _require_run(self, run: int) -> None:
        if not isinstance(run, int) or isinstance(run, bool) or run < 0:
            raise ProofError("invalid_run", f"run must be int >= 0, got {run!r}")
        if run not in self.runs and len(self.runs) >= MAX_RUNS:
            raise ProofError("too_many_runs", f"would exceed run limit {MAX_RUNS}")
        self.runs.add(run)

    def validate(self) -> None:
        if len(self.coord.points) > MAX_POINTS:
            raise ProofError("too_many_points", "point limit exceeded")
        if len(self.runs) > MAX_RUNS:
            raise ProofError("too_many_runs", "run limit exceeded")
        if len(self.patches) > MAX_PATCHES:
            raise ProofError("too_many_patches", "patch limit exceeded")

    # ---- readings resolution ------------------------------------------------
    def live_readings(self, sample_id: str) -> list[Reading]:
        """Readings of ``sample_id`` not deposed by a sufficient-authority peer."""
        ids = list(self.readings_by_sample.get(sample_id, []))
        live = {rid: self.readings[rid] for rid in ids}
        changed = True
        while changed:
            changed = False
            for r in list(live.values()):
                for victim in r.supersedes:
                    v = live.get(victim)
                    if v is not None and r.authority >= v.authority:
                        del live[victim]
                        changed = True
        return [live[rid] for rid in ids if rid in live]

    def readings_for_run(self, run: int) -> list[Reading]:
        """All live readings (across impressions) recorded for one print run."""
        out: list[Reading] = []
        for sample_id, ids in self.readings_by_sample.items():
            live_ids = {r.reading_id for r in self.live_readings(sample_id)}
            for rid in ids:
                if rid in live_ids and self.readings[rid].run == run:
                    out.append(self.readings[rid])
        return out

    def authoritative_readings(self, run: int) -> list[Reading]:
        return [
            r
            for r in self.readings_for_run(run)
            if not r.plan_only and r.authority >= AUTHORITY_CUSTODIAN
        ]

    def plan_only_readings(self, run: int) -> list[Reading]:
        return [
            r
            for r in self.readings_for_run(run)
            if r.plan_only or r.authority < AUTHORITY_CUSTODIAN
        ]

    def point_requirements(self, run: int):
        """Resolve per-point required states from live authoritative readings.

        Every still-valid original reading for the run constrains the proof at
        its must-check points.  Returns ``(required, disputed, torn_only,
        torn_witness)``.
        """
        auth = self.authoritative_readings(run)
        required: dict[str, str] = {}
        disputed: set[str] = set()
        torn_only: set[str] = set()
        torn_witness: set[str] = set()
        all_points = set()
        for r in auth:
            all_points.update(r.states)
        for p in all_points:
            states = [r.state_of(p) for r in auth if p in r.states]
            definite = {s for s in states if s in POINT_STATES}
            if TORN in states:
                torn_witness.add(p)
            if not definite:
                torn_only.add(p)
            elif len(definite) > 1:
                disputed.add(p)
            else:
                required[p] = next(iter(definite))
        return required, disputed, torn_only, torn_witness


# ---------------------------------------------------------------- sheet request
@dataclass
class SheetRequest:
    sheet_id: str
    sample_id: str
    run: int
    mounts: tuple[str, ...] = ()
    # frames to dismantle before mounting; only effective with scan confirmation
    release: tuple[str, ...] = ()
    scan_release_confirmed: bool = False
    # custody basis supplied by the caller (from HandoffRepo); not recomputed here
    custody_ok: bool = True
    signer: Optional[str] = None
    # may an independent film approval be granted for a pin-blocked region?
    approve_film: bool = False


# ---------------------------------------------------------------- session
@dataclass
class _Mount:
    patch_id: str
    physical_id: str
    sheet: str
    slot: str
    pins: frozenset[str]
    status: str = "held"  # "held" | "released"

    @classmethod
    def of(cls, patch: Patch, sheet: str) -> "_Mount":
        return cls(patch.patch_id, patch.physical_id, sheet, patch.slot, patch.pins)


class ProofingSession:
    """Stateful batch proofing for one craft across several sheets."""

    def __init__(
        self,
        workorder: PlateWorkOrder,
        craft: str,
        *,
        locked_slots: Optional[Iterable[str]] = None,
        locked_pins: Optional[Iterable[str]] = None,
    ):
        if craft not in (CRAFT_CATCH_FRAME, CRAFT_FILM):
            raise ProofError("invalid_craft", craft)
        self.wo = workorder
        self.craft = craft
        self.locked_slots = set(locked_slots if locked_slots is not None
                                else workorder.coord.locked_slots)
        self.locked_pins = set(locked_pins if locked_pins is not None
                               else workorder.coord.locked_pins)
        self._mounts: list[_Mount] = []
        self.film_consumed: set[str] = set()
        self.origin_consumed: set[str] = set()
        self.film_commitments: set[str] = set()
        self.signed: list[str] = []

    # ---- occupancy views ----------------------------------------------------
    def _slot_holder(self, slot: str) -> Optional[_Mount]:
        for m in self._mounts:
            if m.status == "held" and m.slot == slot:
                return m
        return None

    def _pin_holder(self, pin: str) -> Optional[_Mount]:
        for m in self._mounts:
            if m.status == "held" and pin in m.pins:
                return m
        return None

    def _piece_holder(self, physical_id: str) -> Optional[_Mount]:
        for m in self._mounts:
            if m.status == "held" and m.physical_id == physical_id:
                return m
        return None

    # ---- main entry ---------------------------------------------------------
    def process(self, req: SheetRequest) -> Judgment:
        j = Judgment(signable=False)

        # 1) release requested frames (only with a scan-confirmed dismantle).
        for sid in req.release:
            held = [m for m in self._mounts if m.sheet == sid and m.status == "held"]
            if not held:
                continue
            if not req.scan_release_confirmed:
                # The piece is NOT freed: it still occupies its pins/slots.
                j.notes.append(
                    f"release of {sid} not scan-confirmed; pins still occupied"
                )
                continue
            for m in held:
                m.status = "released"
            j.notes.append(f"frame {sid} dismantled (scan-confirmed)")

        # 2) resolve requested patches.
        try:
            mounts = [self.wo.patches[pid] for pid in req.mounts]
        except KeyError as exc:
            j.refusals.append({"kind": "unknown_patch", "detail": str(exc)})
            return j

        # 3) validate mountability.  A failed mount does NOT invalidate the
        #    appraisal: we still resolve readings and the (hypothetical) printed
        #    pattern, then refuse the proof itself.
        mount_errors = self._validate_mounts(req, mounts)
        j.refusals.extend(mount_errors)

        # 4) hypothetical printed state = plate base, overridden by requested
        #    patches' emits (pattern evaluation is independent of mountability).
        printed = dict(self.wo.plate_state)
        for patch in mounts:
            for p, s in patch.emits.items():
                printed[p] = s
        j.printed = printed

        covered = set()
        for patch in mounts:
            covered.update(patch.covers)

        # 5) custody / authority basis.
        if not req.custody_ok:
            j.refusals.append({
                "kind": "custody_incomplete",
                "detail": "whole case not validly held (member held or trigger missing); "
                          "cannot sign this proof",
            })

        auth = self.wo.authoritative_readings(req.run)
        if not auth:
            j.refusals.append({
                "kind": "no_authoritative_reading",
                "detail": "only plan-only / unconfirmed readings exist; the custodian's "
                          "original reading cannot be replaced",
            })
        else:
            # The signer must be the custodian who signed a reading we rely on.
            if req.signer is not None:
                signers = {r.custodian for r in auth}
                if req.signer not in signers:
                    j.refusals.append({
                        "kind": "signer_not_custodian",
                        "detail": f"signer {req.signer!r} did not sign any authoritative "
                                  f"reading (custodians: {sorted(signers)})",
                    })

        required, disputed, torn_only, torn_witness = self.wo.point_requirements(
            req.run
        )
        j.disputed_points = sorted(disputed & self.wo.must_check)

        # 6) per must-check point verdict.
        for p in sorted(self.wo.must_check):
            # origin reprint right spent by an earlier film we did not re-cover
            if p in self.origin_consumed and p not in covered:
                j.refusals.append({
                    "kind": "origin_consumed",
                    "point": p,
                    "detail": f"point {p}: film already spent its local reprint right; "
                              f"original state cannot be taken again",
                })
                continue
            if p in disputed:
                j.refusals.append({
                    "kind": "disputed",
                    "point": p,
                    "detail": f"point {p}: live authoritative readings disagree",
                })
                continue
            if p not in required:
                reason = "torn witness only" if p in torn_only else "no reading covers it"
                j.refusals.append({
                    "kind": "unverifiable",
                    "point": p,
                    "detail": f"point {p}: cannot be verified ({reason})",
                    "torn": p in torn_witness,
                })
                continue
            if printed.get(p) != required[p]:
                j.refusals.append({
                    "kind": "mismatch",
                    "point": p,
                    "detail": f"point {p}: proof prints {printed.get(p)!r}, "
                              f"reading requires {required[p]!r}",
                })

        pattern_correct = not any(
            r["kind"] in ("mismatch", "disputed", "unverifiable", "origin_consumed")
            for r in j.refusals
        )

        # 7) independent film approval for a pin-blocked but pattern-correct case.
        if mount_errors and pattern_correct and req.approve_film \
                and self.craft == CRAFT_CATCH_FRAME:
            film_twin = self._film_twin(mounts)
            if film_twin is not None:
                j.notes.append(
                    f"independent film approval available for pinned region via "
                    f"{film_twin.patch_id}"
                )

        j.matched_readings = [r.reading_id for r in auth]
        j.notes.extend(
            f"plan-only reading ignored (no authority over original): {r.reading_id}"
            for r in self.wo.plan_only_readings(req.run)
        )

        # 7) the assembly of readings must be explainable across samples /
        #    regrinds / run order for every must-check point.
        for v in self.wo.explain_transitions():
            if v["point"] in self.wo.must_check:
                j.refusals.append({
                    "kind": "unexplained_transition",
                    "point": v["point"],
                    "detail": v["detail"],
                })

        j.signable = not j.refusals
        if j.signable:
            # 8) commit mounts / consumptions only when the sheet is signable;
            #    a refused sheet leaves no partial occupancy behind.
            for patch in mounts:
                if patch.film:
                    self.film_consumed.add(patch.patch_id)
                    self.origin_consumed.update(patch.covers)
                else:
                    self._mounts.append(_Mount.of(patch, req.sheet_id))
            j.used_mounts = [p.patch_id for p in mounts]
            self.signed.append(req.sheet_id)
        else:
            j.used_mounts = []
        return j

    # ---- helpers ------------------------------------------------------------
    def _validate_mounts(self, req: SheetRequest, mounts: list[Patch]) -> list[dict]:
        errors: list[dict] = []
        seen_slots: dict[str, str] = {}
        seen_pins: set[str] = set()
        seen_covers: set[str] = set()

        for patch in mounts:
            if self.craft == CRAFT_FILM and not patch.film:
                errors.append({
                    "kind": "pinned_patch_in_film_craft",
                    "patch": patch.patch_id,
                    "detail": f"{patch.patch_id}: film craft cannot use a pinned piece",
                })
                continue
            if self.craft == CRAFT_CATCH_FRAME and patch.film:
                errors.append({
                    "kind": "film_patch_in_catch_frame",
                    "patch": patch.patch_id,
                    "detail": f"{patch.patch_id}: catch-frame craft cannot use film",
                })
                continue

            if patch.film:
                if patch.patch_id in self.film_consumed:
                    errors.append({
                        "kind": "film_spent",
                        "patch": patch.patch_id,
                        "detail": f"{patch.patch_id}: film already applied elsewhere",
                    })
                continue

            # pinned: slot / pin / piece availability
            if patch.slot in seen_slots:
                errors.append({
                    "kind": "co_mount_conflict",
                    "patch": patch.patch_id,
                    "detail": f"slot {patch.slot} already taken by {seen_slots[patch.slot]} on this sheet",
                })
            if patch.slot in self.locked_slots:
                errors.append({
                    "kind": "slot_locked",
                    "patch": patch.patch_id,
                    "detail": f"slot {patch.slot} is locked by the machine",
                })
            else:
                holder = self._slot_holder(patch.slot)
                if holder is not None:
                    errors.append({
                        "kind": "slot_unavailable",
                        "patch": patch.patch_id,
                        "detail": f"slot {patch.slot} held by {holder.patch_id} on {holder.sheet}",
                    })
            for pin in patch.pins:
                if pin in seen_pins:
                    errors.append({
                        "kind": "co_mount_conflict",
                        "patch": patch.patch_id,
                        "detail": f"pin {pin} already used by another piece on this sheet",
                    })
                if pin in self.locked_pins:
                    errors.append({
                        "kind": "pin_locked",
                        "patch": patch.patch_id,
                        "detail": f"pin {pin} is locked by the machine",
                    })
                else:
                    ph = self._pin_holder(pin)
                    if ph is not None:
                        errors.append({
                            "kind": "pin_unavailable",
                            "patch": patch.patch_id,
                            "detail": f"pin {pin} held by {ph.patch_id} on {ph.sheet}",
                        })
            if patch.covers & seen_covers:
                errors.append({
                    "kind": "co_mount_conflict",
                    "patch": patch.patch_id,
                    "detail": f"{patch.patch_id} overlaps another piece's covered points",
                })
            piece = self._piece_holder(patch.physical_id)
            if piece is not None and piece.sheet != req.sheet_id:
                errors.append({
                    "kind": "piece_in_use",
                    "patch": patch.patch_id,
                    "detail": f"physical piece {patch.physical_id} is mounted on {piece.sheet}",
                })
            seen_slots[patch.slot] = patch.patch_id
            seen_pins.update(patch.pins)
            seen_covers.update(patch.covers)
        return errors

    def _film_twin(self, mounts: list[Patch]) -> Optional[Patch]:
        """A film piece with identical coverage/emission, if the order has one."""
        wanted = {(p.covers, tuple(sorted(p.emits.items()))) for p in mounts}
        for patch in self.wo.patches.values():
            key = (patch.covers, tuple(sorted(patch.emits.items())))
            if patch.film and key in wanted:
                return patch
        return None

    # ---- batch convenience --------------------------------------------------
    def process_batch(self, requests: Iterable[SheetRequest]) -> dict:
        results = []
        for req in requests:
            j = self.process(req)
            results.append({"sheet": req.sheet_id, "sample": req.sample_id,
                            "run": req.run, **j.as_dict()})
        return {
            "craft": self.craft,
            "signed": list(self.signed),
            "film_consumed": sorted(self.film_consumed),
            "origin_consumed": sorted(self.origin_consumed),
            "results": results,
        }
