"""Domain records for the plate proofing work order.

The vocabulary deliberately mirrors a lithographic workshop, but the semantics
are the same temporal / whole-case ones the archive store already enforces:

===============  ============================================================
石版 / 原版       the plate (a case)
点 / 缺口          a point on the plate (a document); "missing" == not printable
印次              a print run index (an ordered ``as_of_day``)
鉴定 / 读法        a *reading* of a sample impression at one run (a frozen
                  qualification product); lists per-point state + the custodian
                  who signed the original then
补版 / 补片        a corrective patch: covers points, restores a final printed
                  state, occupies a clamp slot and some locating pins
夹位 / 定位脚      clamp slot / locating pin
补版膜            a one-time film: no pins, permanently consumes the patch and
                  the local reprint right (the origin point can no longer be
                  re-read afterwards)
必核点            must-check points named by the contract
===============  ============================================================

Two point states are *not* interchangeable and must be registered apart:

* ``COMPLETE`` (此处印全)  -- the point printed fully.
* ``MISSING``  (缺口)       -- the point is notched / not printed.
* ``TORN``     (纸角缺失)   -- the sample's corner is gone, so the point cannot
  be read at all.  This is *absence of evidence*, never "complete" and never
  "missing".  A proof itself always prints COMPLETE or MISSING; TORN exists
  only on the reading side.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional

# Point states.
COMPLETE = "complete"   # 印全 / 完好
MISSING = "missing"     # 缺
TORN = "torn"           # 纸角缺失 —— unreadable, registered separately

POINT_STATES = frozenset({COMPLETE, MISSING})
READING_STATES = frozenset({COMPLETE, MISSING, TORN})


class ProofError(Exception):
    """A batch / request that must be rejected as a whole."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


# Authority ranks.  A *plan only* (未确认的移交预案) carries no authority to
# depose the custodian's signed reading of an original.
AUTHORITY_PLAN_ONLY = 1
AUTHORITY_CUSTODIAN = 2


@dataclass(frozen=True)
class Reading:
    """One appraisal of a sample impression at one print run.

    ``states`` records every point the appraiser could read; a point whose
    sample corner is missing is recorded as :data:`TORN` and must never be
    silently folded into COMPLETE/MISSING.
    """

    reading_id: str
    sample_id: str
    run: int
    states: Mapping[str, str]
    custodian: str                       # 持馆 that signed the original then
    authority: int = AUTHORITY_CUSTODIAN
    plan_only: bool = False              # 只凭移交预案得出的重读
    supersedes: tuple[str, ...] = ()     # reading ids this one claims to replace
    regrind_return_day: Optional[int] = None  # 已证重磨的归期 (if applicable)
    notes: tuple[str, ...] = ()

    def state_of(self, point: str) -> Optional[str]:
        return self.states.get(point)

    def __post_init__(self):
        for p, s in self.states.items():
            if s not in READING_STATES:
                raise ProofError("invalid_reading", f"{self.reading_id}: bad state {s!r} at {p}")


@dataclass(frozen=True)
class Patch:
    """A physical local patch (补版 / 补片).

    ``emits`` is the *final printed state* the patch produces at each covered
    point.  ``physical_id`` identifies the actual piece: the same piece can be
    lent to another sheet only after an explicit release.
    """

    patch_id: str
    physical_id: str
    covers: frozenset[str]
    emits: Mapping[str, str]
    slot: str
    pins: frozenset[str] = frozenset()
    film: bool = False  # 补版膜: no pins, permanent, single use

    def __post_init__(self):
        if not self.covers:
            raise ProofError("invalid_patch", f"{self.patch_id}: covers nothing")
        for p in self.covers:
            s = self.emits.get(p)
            if s not in POINT_STATES:
                raise ProofError(
                    "invalid_patch",
                    f"{self.patch_id}: covered point {p} must emit COMPLETE or MISSING, got {s!r}",
                )
        if self.film and self.pins:
            raise ProofError("invalid_patch", f"{self.patch_id}: film patches take no pins")


@dataclass(frozen=True)
class Regrind:
    """An evidenced whole-face regrind (整面重磨).

    A regrind is the *only* way an old notch (MISSING) can disappear and become
    COMPLETE.  ``evidence_id`` must be non-empty: an unevidenced regrind cannot
    explain any transition.  ``day`` is the run / return day after which the
    freshly ground face prints again.
    """

    regrind_id: str
    day: int
    evidence_id: str
    note: str = ""

    def __post_init__(self):
        if not self.evidence_id:
            raise ProofError(
                "unevidenced_regrind",
                f"{self.regrind_id}: whole-face regrind requires evidence",
            )
        if not isinstance(self.day, int) or isinstance(self.day, bool) or self.day < 0:
            raise ProofError("invalid_regrind", f"{self.regrind_id}: bad day {self.day!r}")


@dataclass
class Judgment:
    """Outcome of evaluating one proof / mounting against the readings."""

    signable: bool
    printed: dict[str, str] = field(default_factory=dict)
    refusals: list[dict] = field(default_factory=list)
    matched_readings: list[str] = field(default_factory=list)
    used_mounts: list[str] = field(default_factory=list)
    disputed_points: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "signable": self.signable,
            "printed": dict(sorted(self.printed.items())),
            "refusals": self.refusals,
            "matched_readings": self.matched_readings,
            "used_mounts": self.used_mounts,
            "disputed_points": self.disputed_points,
            "notes": self.notes,
        }
