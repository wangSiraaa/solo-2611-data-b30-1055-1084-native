"""Recycling-bin turnover history built on the offline delivery store.

A *named bin* is dispatched to a household through the existing
:class:`~address_delivery.service.AddressService`.  The send order must commit;
the bin's lending then records the street / house number / station that the
committed job actually froze -- the ledger never re-resolves a lent bin.

Everything after dispatch is an append-only event log.  Each event carries a
logical ``seq`` (plus insertion order as tie-breaker), so a late supplement or
a mis-scan is re-checked in the *original* order rather than the order it was
typed in.

Recycling-scan rules
--------------------
A scan names the lending tag it belongs to and the station it was taken at.  It
is applied only when both match the bin's **current** lending:

* a tag from a previous round -> ``stale_tag`` (an old sticker cannot overwrite
  the new lending);
* a scan at a different station -> ``station_mismatch`` (the scan must happen
  at the current lent address);
* a second fill without an intervening return -> ``duplicate_fill``;
* a return before the bin was filled -> ``early_return``.

Cleaning rules
--------------
Cleaning has two *ordered* steps, ``empty`` then ``dry``.  ``empty`` removes the
material but keeps wet residue (a bin emptied while it held wet-mixed material
stays wet); only an ``empty`` followed by ``dry`` (or a drying certificate that
follows an ``empty``) clears it.  Glass is recyclable but does *not* clear old
residue.  Loading dry paper while wet residue remains makes the whole bin
wet-mixed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from .db import transaction

# -- event kinds -------------------------------------------------------------
INITIAL = "initial"
LEND = "lend"
FILL = "fill"
RETURN = "return"
EMPTY = "empty"
DRY = "dry"
CERTIFICATE = "certificate"

# -- event outcomes ----------------------------------------------------------
APPLIED = "applied"
STALE_TAG = "stale_tag"
DUPLICATE_FILL = "duplicate_fill"
EARLY_RETURN = "early_return"
DUPLICATE_RETURN = "duplicate_return"
STATION_MISMATCH = "station_mismatch"
OUT_OF_ORDER = "out_of_order"
CERTIFICATE_UNBACKED = "certificate_without_empty"

# -- material / content grades ----------------------------------------------
PAPER = "paper"
GLASS = "glass"
WET_MIXED = "wet_mixed"
NONE = "none"

MATERIALS = (PAPER, GLASS)

# Why wet residue survived into this round.
REASON_PREVIOUS_ROUND = "previous_round"
REASON_EMPTIED_WET_MIXED = "emptied_while_wet_mixed"


class LendError(RuntimeError):
    """A bin's send order did not commit, so no lending was opened."""

    def __init__(self, bin_id: str, outcome: str, message: str) -> None:
        super().__init__(f"bin {bin_id} dispatch failed: {outcome} ({message})")
        self.bin_id = bin_id
        self.outcome = outcome
        self.message = message


_SCHEMA = """
CREATE TABLE IF NOT EXISTS rec_events (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    bin_id       TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rec_events_bin ON rec_events(bin_id, seq, event_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Pure replay
# ---------------------------------------------------------------------------


@dataclass
class _State:
    wet_residue: bool = False
    wet_reason: str | None = None
    content: str = NONE
    weight: int = 0
    filled: bool = False
    returned: bool = False
    pending_empty: bool = False
    lending: dict[str, Any] | None = None
    wet_at_lend: bool = False
    wet_reason_at_lend: str | None = None


def replay_events(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Replay an ordered event stream and explain every event.

    ``events`` must already be sorted by ``(seq, event_id)``.  The return value
    is a plain dict so it can be serialised or asserted on directly.
    """
    st = _State()
    outcomes: list[dict[str, Any]] = []
    loads: list[dict[str, Any]] = []

    for ev in events:
        kind = ev["kind"]
        p = ev["payload"]
        outcome = APPLIED
        reason: str | None = None

        if kind == INITIAL:
            st.wet_residue = bool(p.get("wet_residue", False))
            st.wet_reason = p.get("reason") if st.wet_residue else None

        elif kind == LEND:
            st.lending = p
            st.filled = False
            st.returned = False
            st.content = NONE
            st.weight = 0
            st.pending_empty = False
            st.wet_at_lend = st.wet_residue
            st.wet_reason_at_lend = st.wet_reason

        elif kind == FILL:
            if st.lending is None or p.get("lending_id") != st.lending["lending_id"]:
                outcome = STALE_TAG
            elif p.get("station_id") != st.lending["station_id"]:
                outcome = STATION_MISMATCH
            elif st.filled:
                outcome = DUPLICATE_FILL
            elif st.returned:
                # The current lending already returned; a fill without a new
                # lending cannot take over it.
                outcome = STALE_TAG
            else:
                material = p["material"]
                if material == PAPER:
                    if st.wet_residue:
                        st.content = WET_MIXED
                        if st.wet_reason is None:
                            st.wet_reason = REASON_PREVIOUS_ROUND
                        reason = st.wet_reason
                    else:
                        st.content = PAPER
                elif material == GLASS:
                    # Glass is recyclable but does not clear old residue; the
                    # residue (and its reason) simply survives.
                    st.content = GLASS
                else:
                    raise ValueError(f"unknown material {material!r}")
                st.weight = int(p["weight"])
                st.filled = True
                st.pending_empty = False

        elif kind == RETURN:
            if st.lending is None or p.get("lending_id") != st.lending["lending_id"]:
                outcome = STALE_TAG
            elif p.get("station_id") != st.lending["station_id"]:
                outcome = STATION_MISMATCH
            elif st.returned:
                outcome = DUPLICATE_RETURN
            elif not st.filled:
                outcome = EARLY_RETURN
            else:
                loads.append(
                    {
                        "lending_id": st.lending["lending_id"],
                        "station_id": st.lending["station_id"],
                        "bin_id": st.lending["bin_id"],
                        "grade": st.content,
                        "weight": st.weight,
                    }
                )
                if st.content == WET_MIXED:
                    # The truck takes the material; the bin keeps wet residue.
                    st.wet_residue = True
                    if st.wet_reason is None:
                        st.wet_reason = REASON_EMPTIED_WET_MIXED
                st.content = NONE
                st.weight = 0
                st.filled = False
                st.returned = True

        elif kind == EMPTY:
            if st.content == WET_MIXED:
                st.wet_residue = True
                st.wet_reason = st.wet_reason or REASON_EMPTIED_WET_MIXED
            st.content = NONE
            st.weight = 0
            st.filled = False
            st.returned = False
            st.pending_empty = True

        elif kind == DRY:
            if not st.pending_empty:
                outcome = OUT_OF_ORDER
            else:
                st.wet_residue = False
                st.wet_reason = None
                st.pending_empty = False

        elif kind == CERTIFICATE:
            # A drying certificate is a *proof* of the dry step.  It only
            # clears residue when an empty step was already recorded.
            if not st.pending_empty:
                outcome = CERTIFICATE_UNBACKED
            else:
                st.wet_residue = False
                st.wet_reason = None
                st.pending_empty = False

        else:
            raise ValueError(f"unknown event kind {kind!r}")

        outcomes.append(
            {
                "event_id": ev["event_id"],
                "seq": ev["seq"],
                "kind": kind,
                "outcome": outcome,
                "reason": reason,
            }
        )

    lending = st.lending
    return {
        "events": outcomes,
        "state": {
            "wet_residue": st.wet_residue,
            "wet_reason": st.wet_reason,
            "content": st.content,
            "weight": st.weight,
            "filled": st.filled,
        },
        "round": None
        if lending is None
        else {
            "lending_id": lending["lending_id"],
            "epoch": lending["epoch"],
            "station_id": lending["station_id"],
            "street_id": lending["street_id"],
            "house_no": lending["house_no"],
            "grade": st.content,
            "weight": st.weight,
            "filled": st.filled,
        },
        "last_round_residue": {
            "wet": st.wet_at_lend,
            "reason": st.wet_reason_at_lend,
        },
        "loads": loads,
    }


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


@dataclass
class Lending:
    bin_id: str
    lending_id: str
    epoch: int
    parcel_id: str
    city_id: str
    district_id: str
    street_id: str
    station_id: str
    house_no: int
    identity_key: str

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class RecyclingLedger:
    """Append-only turnover history for named bins."""

    def __init__(self, service) -> None:
        self.service = service
        self.conn = service.conn
        self.conn.executescript(_SCHEMA)

    # -- event plumbing ------------------------------------------------------

    def _events(self, bin_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM rec_events WHERE bin_id=? ORDER BY seq, event_id",
            (bin_id,),
        ).fetchall()
        return [
            {
                "event_id": r["event_id"],
                "seq": r["seq"],
                "kind": r["kind"],
                "payload": json.loads(r["payload_json"]),
                "at": r["at"],
            }
            for r in rows
        ]

    def next_seq(self, bin_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM rec_events WHERE bin_id=?",
            (bin_id,),
        ).fetchone()
        return int(row["m"]) + 1

    def _append(
        self, bin_id: str, kind: str, payload: dict, seq: int | None = None
    ) -> int:
        if seq is None:
            seq = self.next_seq(bin_id)
        with transaction(self.conn):
            cur = self.conn.execute(
                "INSERT INTO rec_events(bin_id,seq,kind,payload_json,at) VALUES(?,?,?,?,?)",
                (bin_id, seq, kind, json.dumps(payload, ensure_ascii=False), _now()),
            )
            return int(cur.lastrowid)

    def event_outcome(self, bin_id: str, event_id: int) -> dict[str, Any]:
        report = self.reconcile(bin_id)
        for item in report["events"]:
            if item["event_id"] == event_id:
                return item
        raise KeyError(event_id)

    # -- bin lifecycle -------------------------------------------------------

    def ensure_bin(
        self,
        bin_id: str,
        *,
        initial_wet_residue: bool = False,
        reason: str | None = None,
        seq: int | None = None,
    ) -> None:
        """Seed a bin.  Only the first call has an effect (seq defaults to 0)."""
        if self._events(bin_id):
            return
        self._append(
            bin_id,
            INITIAL,
            {
                "wet_residue": bool(initial_wet_residue),
                "reason": (reason or REASON_PREVIOUS_ROUND)
                if initial_wet_residue
                else None,
            },
            seq=0 if seq is None else seq,
        )

    def current_lending(self, bin_id: str) -> Lending | None:
        lendings = [e for e in self._events(bin_id) if e["kind"] == LEND]
        if not lendings:
            return None
        return Lending(**lendings[-1]["payload"])

    def _next_epoch(self, bin_id: str) -> int:
        epochs = [
            int(e["payload"]["epoch"])
            for e in self._events(bin_id)
            if e["kind"] == LEND
        ]
        return (max(epochs) if epochs else 0) + 1

    def lend(
        self,
        bin_id: str,
        city_id: str,
        district_text: str,
        street_text: str,
        house_no: int,
        *,
        parcel_id: str | None = None,
        seq: int | None = None,
    ) -> Lending:
        """Dispatch *bin_id*; requires the send order to commit.

        The lending stores the frozen street / house number / station; the
        delivery service is the only thing that resolves the address.
        """
        self.ensure_bin(bin_id)
        epoch = self._next_epoch(bin_id)
        parcel_id = parcel_id or f"{bin_id}#{epoch}"
        result = self.service.submit_new(
            parcel_id, city_id, district_text, street_text, house_no
        )
        if not result.ok:
            raise LendError(bin_id, result.outcome, result.message)
        job = result.job
        lending_id = f"{bin_id}:L{epoch}"
        payload = {
            "bin_id": bin_id,
            "lending_id": lending_id,
            "epoch": epoch,
            "parcel_id": parcel_id,
            "city_id": job["city_id"],
            "district_id": job["district_id"],
            "street_id": job["street_id"],
            "station_id": job["station_id"],
            "house_no": job["house_no"],
            "identity_key": json.dumps(job["identity_key"], ensure_ascii=False),
        }
        self._append(bin_id, LEND, payload, seq=seq)
        return Lending(**payload)

    # -- scans and cleaning --------------------------------------------------

    def _default_tag(self, bin_id: str) -> tuple[str, str]:
        lending = self.current_lending(bin_id)
        if lending is None:
            raise ValueError(f"bin {bin_id} has no lending")
        return lending.lending_id, lending.station_id

    def note_fill(
        self,
        bin_id: str,
        material: str,
        weight: int,
        *,
        lending_id: str | None = None,
        station_id: str | None = None,
        seq: int | None = None,
    ) -> dict[str, Any]:
        if material not in MATERIALS:
            raise ValueError(f"material must be one of {MATERIALS}")
        if int(weight) <= 0:
            raise ValueError("weight must be positive")
        default_tag, default_station = self._default_tag(bin_id)
        event_id = self._append(
            bin_id,
            FILL,
            {
                "lending_id": lending_id or default_tag,
                "station_id": station_id or default_station,
                "material": material,
                "weight": int(weight),
            },
            seq=seq,
        )
        return self.event_outcome(bin_id, event_id)

    def note_return(
        self,
        bin_id: str,
        *,
        lending_id: str | None = None,
        station_id: str | None = None,
        seq: int | None = None,
    ) -> dict[str, Any]:
        default_tag, default_station = self._default_tag(bin_id)
        event_id = self._append(
            bin_id,
            RETURN,
            {
                "lending_id": lending_id or default_tag,
                "station_id": station_id or default_station,
            },
            seq=seq,
        )
        return self.event_outcome(bin_id, event_id)

    def note_empty(self, bin_id: str, *, seq: int | None = None) -> dict[str, Any]:
        self.ensure_bin(bin_id)
        event_id = self._append(bin_id, EMPTY, {}, seq=seq)
        return self.event_outcome(bin_id, event_id)

    def note_dry(self, bin_id: str, *, seq: int | None = None) -> dict[str, Any]:
        self.ensure_bin(bin_id)
        event_id = self._append(bin_id, DRY, {}, seq=seq)
        return self.event_outcome(bin_id, event_id)

    def note_certificate(
        self,
        bin_id: str,
        *,
        lending_id: str | None = None,
        station_id: str | None = None,
        seq: int | None = None,
    ) -> dict[str, Any]:
        self.ensure_bin(bin_id)
        payload: dict[str, Any] = {}
        if lending_id is not None:
            payload["lending_id"] = lending_id
        if station_id is not None:
            payload["station_id"] = station_id
        event_id = self._append(bin_id, CERTIFICATE, payload, seq=seq)
        return self.event_outcome(bin_id, event_id)

    # -- queries -------------------------------------------------------------

    def history(self, bin_id: str) -> list[dict[str, Any]]:
        return self._events(bin_id)

    def reconcile(self, bin_id: str) -> dict[str, Any]:
        report = replay_events(self._events(bin_id))
        report["bin_id"] = bin_id
        return report

    def material(self, bin_id: str) -> dict[str, Any] | None:
        """The load the truck would receive from *bin_id* right now."""
        report = self.reconcile(bin_id)
        round_ = report["round"]
        if round_ is None or not round_["filled"]:
            return None
        return {
            "bin_id": bin_id,
            "station_id": round_["station_id"],
            "grade": round_["grade"],
            "weight": round_["weight"],
        }

    def explain(self, bin_id: str) -> dict[str, Any]:
        """Independent explanation of weight, grade and last-round leftover."""
        report = self.reconcile(bin_id)
        round_ = report["round"]
        state = report["state"]
        applied = [e for e in report["events"] if e["outcome"] == APPLIED]
        rejected = [e for e in report["events"] if e["outcome"] != APPLIED]
        return {
            "bin_id": bin_id,
            "round_weight": round_["weight"] if round_ else 0,
            "round_grade": round_["grade"] if round_ else NONE,
            "last_round_residue": report["last_round_residue"],
            "wet_residue": state["wet_residue"],
            "wet_reason": state["wet_reason"],
            "applied_events": applied,
            "rejected_events": rejected,
            "loads": report["loads"],
        }
