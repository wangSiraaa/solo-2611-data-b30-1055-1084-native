"""Custody-gated collation for a water-clock case (水尺簿 / 值更簿).

The schematic clock model lives in :mod:`archive_handoff.clepsydra`.  This
module adds the *provenance* rules the museum insists on:

* Only the institution that actually holds the case may collate-read the
  original page and turn a reading into a *formal* record.
* A new institution that has merely received one volume, or whose whole-case
  handoff trigger is still missing, may record only an *initial* (provisional)
  reading.  It cannot use that to delete another copy's stop-flow marker, and
  the exhibition keeps playing the old reading (and therefore still needs its
  calibration cup).
* Re-verification at the new institution waits for the *whole* case to arrive;
  only then does the provisional reading become formal and the cup withdrawn.
* The original institution, asked to re-check, can only re-sign its old reading
  (a new revision number, same content) -- it does not manufacture the new
  stop-flow fact.

Nothing here deletes museum data; readings are append-only revisions with a
status.  Custody itself is owned by :class:`archive_handoff.handoff_service.HandoffRepo`
and is only *read* here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .clepsydra import (
    STOP,
    Entry,
    Solution,
    Witness,
    determined_beats,
    explain,
    full_direction,
    possible_ring_patterns,
)
from .models import RejectError

INITIAL_CUSTODIAN = "LOCAL"
FORMAL = "formal"
PROVISIONAL = "provisional"


@dataclass(frozen=True)
class Revision:
    """One appended reading revision for a single beat."""

    revision_id: str
    beat: int
    value: int              # STOP (0) or a natural inflow 1..scale-1
    institution: str
    status: str             # FORMAL | PROVISIONAL
    revision: int
    claims_original_page: bool
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "revision_id": self.revision_id,
            "beat": self.beat,
            "value": self.value,
            "kind": "stop" if self.value == STOP else "flow",
            "institution": self.institution,
            "status": self.status,
            "revision": self.revision,
            "claims_original_page": self.claims_original_page,
            "note": self.note,
        }


@dataclass
class LogCopy:
    """A copy (抄本) or mis-bound fragment (残页) bound to a case."""

    copy_id: str
    institution: str
    witness: Witness
    authoritative: bool = True

    def as_dict(self) -> dict:
        return {
            "copy_id": self.copy_id,
            "institution": self.institution,
            "authoritative": self.authoritative,
            "beats": [
                {
                    "inflow": e.inflow,
                    "ring": e.ring,
                    "obs": list(e.obs),
                }
                for e in self.witness.entries
            ],
            "directions": [
                {str(k): v for k, v in sorted(d.items())}
                for d in self.witness.directions
            ],
        }


class WaterClockCase:
    """A case's water-scale book plus its copies and reading revisions."""

    def __init__(self, case_id: str, scale: int, beats: int, original_inflows: list[int]):
        if not (1 <= beats <= 12):
            raise ValueError("beats must be 1..12 (local integer table)")
        if len(original_inflows) != beats:
            raise ValueError("original_inflows length must equal beats")
        for inf in original_inflows:
            if not (0 <= inf < scale):
                raise ValueError(f"original inflow {inf} outside [0,{scale})")
        self.case_id = case_id
        self.scale = scale
        self.beats = beats
        self.original_inflows = tuple(original_inflows)
        self.copies: list[LogCopy] = []
        self.revisions: list[Revision] = []

    # ------------------------------------------------------------ copies
    def add_copy(self, copy: LogCopy) -> None:
        if copy.authoritative:
            # A provisional copy's claims are not silently authoritative.
            pass
        self.copies.append(copy)

    # --------------------------------------------------------- revisions
    def _revision_count(self) -> int:
        return len(self.revisions)

    def submit_revision(
        self,
        revision_id: str,
        beat: int,
        value: int,
        institution: str,
        handoff,
        *,
        claims_original_page: bool = True,
        note: str = "",
    ) -> Revision:
        """Append a reading revision.

        Formal only when the submitting institution both holds the whole case
        *now* and claims to have read the original page.  Otherwise the
        revision is provisional.
        """
        if not (0 <= beat < self.beats):
            raise RejectError("invalid_beat", f"beat {beat} outside [0,{self.beats})")
        if not (0 <= value < self.scale):
            raise RejectError("invalid_value", f"value {value} outside [0,{self.scale})")
        formal = claims_original_page and self.holds_whole_case(institution, handoff)
        status = FORMAL if formal else PROVISIONAL
        rev = Revision(
            revision_id=revision_id,
            beat=beat,
            value=value,
            institution=institution,
            status=status,
            revision=self._revision_count() + 1,
            claims_original_page=claims_original_page,
            note=note,
        )
        self.revisions.append(rev)
        return rev

    def resign(self, revision_id: str, institution: str, handoff) -> Revision:
        """Re-sign the *old* reading: new revision number, same old content.

        Only the current holder may re-sign, and it cannot change the fact.
        Crucially "原馆只能重签旧读": the old institution re-affirms the value
        that was already formal on that beat (or the original-book value if no
        formal revision exists) -- it does *not* adopt the newer provisional
        stop-flow proposal.  Adopting that fact is exactly what it is not
        allowed to manufacture.
        """
        target = next((r for r in self.revisions if r.revision_id == revision_id), None)
        if target is None:
            raise RejectError("unknown_revision", f"revision {revision_id} not found")
        if not self.holds_whole_case(institution, handoff):
            raise RejectError(
                "not_holder",
                f"{institution} does not hold the whole case; cannot re-sign",
            )
        old_value = self._old_formal_value(target.beat)
        rev = Revision(
            revision_id=f"{revision_id}~resign{self._revision_count() + 1}",
            beat=target.beat,
            value=old_value,
            institution=institution,
            status=FORMAL,
            revision=self._revision_count() + 1,
            claims_original_page=True,
            note=f"re-signed old formal reading on beat {target.beat} "
                 f"(value {old_value}); provisional {revision_id} not adopted",
        )
        self.revisions.append(rev)
        return rev

    def _old_formal_value(self, beat: int) -> int:
        """Value already formal on ``beat`` (latest formal rev), else original."""
        formal_on_beat = [r for r in self.formal_revisions() if r.beat == beat]
        if formal_on_beat:
            return max(formal_on_beat, key=lambda r: r.revision).value
        return self.original_inflows[beat]


    def promote_pending(self, handoff) -> list[Revision]:
        """Turn provisional readings into formal ones once the case truly holds.

        Called after a whole-case handoff commits.  Re-verification at the new
        institution must wait for the whole case; this is that gate.
        """
        promoted: list[Revision] = []
        for r in list(self.revisions):
            if r.status != PROVISIONAL:
                continue
            if r.claims_original_page and self.holds_whole_case(r.institution, handoff):
                promoted.append(r)
                # replace with a formal revision (append-only: keep old record)
                self.revisions.append(Revision(
                    revision_id=f"{r.revision_id}~formal",
                    beat=r.beat,
                    value=r.value,
                    institution=r.institution,
                    status=FORMAL,
                    revision=self._revision_count() + 1,
                    claims_original_page=True,
                    note=f"promoted provisional {r.revision_id} after whole-case arrival",
                ))
        return promoted

    # ------------------------------------------------------------ custody
    def holds_whole_case(self, institution: str, handoff) -> bool:
        """True iff ``institution`` holds the *whole* case with a real trigger.

        Two conditions, both required (this is the "同案水尺簿仍缺一个移交
        触发" gate):

        1. ``institution`` is the current whole-case custodian; and
        2. a committed whole-case handoff actually delivered the case to it --
           either it is the untouched initial custodian, or the custody ledger
           records a transfer *to* it.

        Merely having received a single physical volume does not satisfy (2),
        so the new institution's initial reading stays provisional until the
        whole case is actually handed over.
        """
        try:
            custodian = handoff.current_custodian(self.case_id)
        except Exception:
            return False
        if custodian != institution:
            return False
        return self._handoff_triggered(institution, handoff)

    def _handoff_triggered(self, institution: str, handoff) -> bool:
        try:
            history = handoff.custody_history(self.case_id)
        except Exception:
            history = []
        if not history:
            # No transfer recorded: only the initial custodian holds it by
            # default; anyone else has merely some physical volume.
            return institution == INITIAL_CUSTODIAN
        return history[-1]["to"] == institution


    # ------------------------------------------------------- effective book
    def formal_revisions(self) -> list[Revision]:
        return [r for r in self.revisions if r.status == FORMAL]

    def provisional_revisions(self) -> list[Revision]:
        return [r for r in self.revisions if r.status == PROVISIONAL]

    def effective_inflows(self) -> tuple[int, ...]:
        """Original book with formal revisions applied (latest revision wins)."""
        values = list(self.original_inflows)
        for r in sorted(self.formal_revisions(), key=lambda r: r.revision):
            values[r.beat] = r.value
        return tuple(values)

    def pending_changes(self) -> list[dict]:
        """Provisional readings that differ from the effective book."""
        eff = self.effective_inflows()
        out = []
        for r in self.provisional_revisions():
            if r.value != eff[r.beat]:
                out.append({
                    "revision_id": r.revision_id,
                    "beat": r.beat,
                    "effective": eff[r.beat],
                    "proposed": r.value,
                    "institution": r.institution,
                })
        return out

    # --------------------------------------------------------- collation
    def book_witness(self) -> Witness:
        eff = self.effective_inflows()
        entries = [Entry(inflow=eff[i], ring=None) for i in range(self.beats)]
        return Witness(
            name=f"{self.case_id}:book",
            entries=entries,
            directions=[full_direction(self.beats)],
        )

    def collate(self) -> Optional[Solution]:
        """Jointly explain effective book plus authoritative copies."""
        witnesses = [self.book_witness()]
        witnesses += [c.witness for c in self.copies if c.authoritative]
        return explain(self.scale, self.beats, witnesses)

    def ring_analysis(self) -> dict:
        """Determine which beats ring unambiguously and which stay disputed."""
        witnesses = [self.book_witness()]
        witnesses += [c.witness for c in self.copies if c.authoritative]
        patterns = possible_ring_patterns(self.scale, self.beats, witnesses)
        determined, disputed = determined_beats(patterns, self.beats)
        return {
            "patterns": sorted(patterns),
            "determined": dict(sorted(determined.items())),
            "disputed": sorted(disputed),
        }

    def show_target(self) -> tuple[dict[int, bool], set[int]]:
        analysis = self.ring_analysis()
        return analysis["determined"], set(analysis["disputed"])
