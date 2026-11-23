"""Binding-photography coordination on top of ``archive_handoff``.

This ties the support model (:mod:`binding`) and the clamp-jig planner
(:mod:`photography`) to the handoff store's custody truth.  It exists so the
report can show *both* orders of business as separate outputs:

* the **true-support** order -- patches measured and signed, poses validated,
  whole-volume witness, seam/combination queries, reachability; and
* the **imaging** order -- per-target shot plans against a chosen jig.

The key custody rule for measurements: a new patch measurement must be signed
by the case's **current custodian**.  While the volume is *in transit* (a
handoff proposal/plan exists but is not confirmed) the last **confirmed** holder
still governs -- an unconfirmed transfer slip does not move that authority, and
an existing device at the origin institution may still start work under the
origin's own measurements.

The module persists only our own coordination notes (measurement ledger records
are already held by the ``SupportModel``); it reads custody from the
``HandoffRepo`` so it never re-derives custody itself.
"""

from __future__ import annotations

from typing import Optional

from .binding import (
    LeafAssembly,
    PatchMeasurement,
    SupportModel,
)
from .models import RejectError
from .photography import (
    ClampJigPlanner,
    PhotoStation,
    TargetShot,
    stop_frame_probe,
)


class BindingPhotoService:
    """Coordinate one volume's support determination and imaging plan."""

    def __init__(self, handoff_repo, assembly: LeafAssembly,
                 station: Optional[PhotoStation] = None):
        self.handoff = handoff_repo
        self.assembly = assembly
        self.support = SupportModel(assembly, custodian_of=self._custodian)
        self.station = station
        self.initial_clamps: frozenset[int] = frozenset()
        # work orders / shot lists / re-shoot records (in-memory job journal)
        self.work_orders: list[dict] = []
        self.reshoot_log: list[dict] = []

    # ---------------------------------------------------------------- custody
    def _custodian(self, case_id: str) -> str:
        return self.handoff.current_custodian(case_id)

    def custody_view(self) -> dict:
        """Current (confirmed) custodian and any in-transit proposal note."""
        case_id = self.assembly.case_id
        current = self.handoff.current_custodian(case_id)
        transit = []
        for row in self.handoff.conn.execute(
            "SELECT proposal_id, from_party, to_party, status FROM proposals "
            "WHERE case_id=? AND status='prepared'", (case_id,)
        ):
            transit.append(
                {
                    "proposal_id": row["proposal_id"],
                    "from": row["from_party"],
                    "to": row["to_party"],
                    "status": row["status"],
                    "note": "prepared only; custody does NOT move until confirmed",
                }
            )
        return {
            "case_id": case_id,
            "confirmed_custodian": current,
            "measurement_authority": current,
            "in_transit": transit,
        }

    # ------------------------------------------------------------ measurements
    def sign_measurement(self, measurement: PatchMeasurement) -> dict:
        """Submit a patch measurement, authorised by the current custodian."""
        return self.support.submit_measurement(measurement)

    # ------------------------------------------------------ support order out
    def support_order(self, queries: Optional[list[dict]] = None) -> dict:
        """Produce the *true-support* order: witness + any queries."""
        queries = queries or []
        query_results = []
        for q in queries:
            if q.get("kind") == "seam":
                query_results.append(
                    {"kind": "seam", "result": self.support.query_seam(q["seam_id"])}
                )
            elif q.get("kind") == "combination":
                query_results.append(
                    {"kind": "combination",
                     "result": self.support.query_combination(q["partial"])}
                )
            else:
                raise RejectError("invalid_query", f"unknown query kind {q.get('kind')}")
        return {
            "order": "true_support",
            "assembly_id": self.assembly.assembly_id,
            "case_id": self.assembly.case_id,
            "custody": self.custody_view(),
            "witness": self.support.support_witness(),
            "queries": query_results,
        }

    # -------------------------------------------------------- imaging order
    def plan_targets(self, targets: list[TargetShot], *,
                     station: Optional[PhotoStation] = None,
                     initial_pose=None,
                     initial_clamps=None) -> dict:
        station = station or self.station
        if station is None:
            raise RejectError("invalid_station", "no photo station configured")
        clamps = self.initial_clamps if initial_clamps is None else frozenset(initial_clamps)
        planner = ClampJigPlanner(station, self.support, initial_clamps=clamps)
        plan = planner.plan(targets, initial_pose=initial_pose)
        plan["custody"] = self.custody_view()
        return plan

    def imaging_order(self, targets: list[TargetShot], **kwargs) -> dict:
        plan = self.plan_targets(targets, **kwargs)
        return {
            "order": "imaging",
            "assembly_id": self.assembly.assembly_id,
            "produced_by": plan["institution"],
            "jig": plan["jig"],
            "feasible": plan["feasible"],
            "targets": plan["targets"],
            "actions": plan["actions"],
            "budget": plan["budget"],
            "custody": plan["custody"],
        }

    def stop_frame_analysis(self, targets: list[TargetShot], *,
                            station: Optional[PhotoStation] = None) -> list[dict]:
        """Report what the (unimplemented) stop frame would allow per target."""
        station = station or self.station
        if station is None:
            raise RejectError("invalid_station", "no photo station configured")
        out = []
        for t in targets:
            # Best case: some supported pose has the required level and a clamp
            # coverage is possible.
            supported = any(
                p[self.support.index[t.seam_id]] == t.required_level
                for p in self.support.support_table()
            ) if t.seam_id in self.support.index else False
            out.append(stop_frame_probe(station, t, supported, supported))
        return out

    # ------------------------------------------------------------- work order
    def make_work_order(self, targets: list[TargetShot], *,
                        station: Optional[PhotoStation] = None,
                        initial_pose=None,
                        initial_clamps=None,
                        queries: Optional[list[dict]] = None) -> dict:
        """Combine the two orders into one local work order + re-shoot record."""
        station = station or self.station
        support = self.support_order(queries)
        imaging = self.imaging_order(
            targets, station=station,
            initial_pose=initial_pose, initial_clamps=initial_clamps,
        )
        order = {
            "work_order": self.assembly.assembly_id,
            "case_id": self.assembly.case_id,
            "station": station.station_id if station else None,
            "institution": station.institution if station else None,
            "custody": self.custody_view(),
            "support_order": support,
            "imaging_order": imaging,
            "stop_frame_analysis": (
                self.stop_frame_analysis(targets, station=station) if station else []
            ),
        }
        self.work_orders.append(order)
        return order

    def record_reshoot(self, shot_id: str, reason: str, *,
                       original_page: str, pose: str,
                       visible_columns: list[int], feasible: bool) -> dict:
        """Log a re-shoot entry tying each frame to page/pose/visible columns."""
        entry = {
            "shot_id": shot_id,
            "reason": reason,
            "original_page": original_page,
            "pose": pose,
            "visible_columns": sorted(visible_columns),
            "feasible": feasible,
        }
        self.reshoot_log.append(entry)
        return entry
