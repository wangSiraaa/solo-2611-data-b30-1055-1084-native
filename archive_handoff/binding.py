"""Leaf-assembly support model for the rare-volume photography job.

This module owns the *true support determination* (真支承判定) side of the job:
it attaches a leaf-assembly sheet to an ``archive_handoff`` case member, records
the approved integer opening levels of every adjacent page seam, records the
patch strips (补条) that span one or two seams, and answers whether a given
whole-volume pose is *statically safe*.

Terminology
-----------
* **Seam** -- an adjacent page seam with a small set of approved integer
  opening levels (档).  ``0`` is the closed / handover level.
* **Patch strip** -- a repair strip belonging to some original page.  It may
  span one seam (allowed single levels) or two seams (allowed *pairs* of
  levels).  Strips that share a ``leaf_group`` must all be satisfied by one and
  the same whole-volume pose.
* **Pose** -- one approved level per seam.  A pose is *supported* when every
  patch strip's projection onto its seams is in that strip's allowed set.  The
  set of all supported poses is the whole-volume support table.
* **Reachability** -- an opening operation changes exactly **one** seam's
  level.  Every intermediate pose must be supported; the whole volume may not
  be lifted and re-arranged.

Nothing here knows about camera geometry; that lives in :mod:`photography`.
"""

from __future__ import annotations

import itertools
from collections import deque
from dataclasses import dataclass
from typing import Iterable, Optional

from .models import ConflictError, RejectError

MAX_SEAMS = 6
MAX_LEVELS = 3
HANDOVER_LEVEL = 0


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def pose_str(levels: Iterable[int]) -> str:
    """Compact human key for a pose, e.g. ``(1, 0)`` -> ``"10"``."""
    return "".join(str(v) for v in levels)


@dataclass(frozen=True)
class Seam:
    seam_id: str
    levels: tuple[int, ...]

    def __post_init__(self):
        if not self.levels:
            raise RejectError("invalid_seam", f"seam {self.seam_id} has no levels")
        if len(self.levels) > MAX_LEVELS:
            raise RejectError(
                "invalid_seam",
                f"seam {self.seam_id} has {len(self.levels)} levels > {MAX_LEVELS}",
            )
        if len(set(self.levels)) != len(self.levels):
            raise RejectError("invalid_seam", f"seam {self.seam_id} has duplicate levels")
        for lv in self.levels:
            if not _is_int(lv) or lv < 0:
                raise RejectError(
                    "invalid_seam", f"seam {self.seam_id} level {lv!r} must be int >= 0"
                )
        if HANDOVER_LEVEL not in self.levels:
            raise RejectError(
                "invalid_seam",
                f"seam {self.seam_id} must include the closed level {HANDOVER_LEVEL}",
            )


@dataclass(frozen=True)
class PatchStrip:
    """A repair strip and the level tuples under which it stays safe.

    ``seams`` is the one or two seams the strip spans.  ``allowed`` is the set
    of permitted level tuples on exactly those seams, in order.

    ``hold_at`` is the set of *projections* at which the strip must be actively
    clamped to stay down (e.g. it only needs takeover once the right seam opens
    to ``11``).  ``hold_positions`` lists the photo columns where a clamp can
    support the strip.  A strip with an empty ``hold_at`` only constrains the
    pose and never demands a clamp.
    """

    strip_id: str
    doc_id: str
    leaf_group: str
    seams: tuple[str, ...]
    allowed: frozenset[tuple[int, ...]]
    hold_positions: frozenset[int] = frozenset()
    hold_at: frozenset[tuple[int, ...]] = frozenset()


@dataclass
class LeafAssembly:
    """The sheet attached to a case member (a volume).

    ``members`` are the ``archive_handoff`` case members this sheet is attached
    to (a subset of, or all of, the case's documents).
    """

    assembly_id: str
    case_id: str
    seams: list[Seam]
    strips: list[PatchStrip]
    start_pose: tuple[int, ...]
    members: tuple[str, ...] = ()

    def seam_ids(self) -> list[str]:
        return [s.seam_id for s in self.seams]


def make_assembly(
    assembly_id: str,
    case_id: str,
    seams: Iterable[dict | Seam],
    strips: Iterable[dict | PatchStrip],
    start_pose: Iterable[int],
    members: Iterable[str] = (),
) -> LeafAssembly:
    """Validate and build an assembly from plain records.

    Enforces the job limits (<= 6 seams, <= 3 approved levels each) and that
    every strip's allowed tuples only mention approved levels.
    """
    seam_objs = [
        s if isinstance(s, Seam) else Seam(str(s["seam_id"]), tuple(s["levels"]))
        for s in seams
    ]
    if not seam_objs:
        raise RejectError("invalid_assembly", "assembly needs at least one seam")
    if len(seam_objs) > MAX_SEAMS:
        raise RejectError(
            "invalid_assembly", f"at most {MAX_SEAMS} seams, got {len(seam_objs)}"
        )
    ids = [s.seam_id for s in seam_objs]
    if len(set(ids)) != len(ids):
        raise RejectError("invalid_assembly", "duplicate seam ids")
    by_id = {s.seam_id: s for s in seam_objs}

    strip_objs: list[PatchStrip] = []
    for raw in strips:
        if isinstance(raw, PatchStrip):
            st = raw
        else:
            st = PatchStrip(
                strip_id=str(raw["strip_id"]),
                doc_id=str(raw["doc_id"]),
                leaf_group=str(raw.get("leaf_group", raw["strip_id"])),
                seams=tuple(str(x) for x in raw["seams"]),
                allowed=frozenset(tuple(int(v) for v in tup) for tup in raw["allowed"]),
                hold_positions=frozenset(int(p) for p in raw.get("hold_positions", ())),
                hold_at=frozenset(tuple(int(v) for v in tup)
                                  for tup in raw.get("hold_at", ())),
            )
        if not st.seams:
            raise RejectError("invalid_strip", f"strip {st.strip_id} spans no seam")
        if len(st.seams) > 2:
            raise RejectError(
                "invalid_strip", f"strip {st.strip_id} spans {len(st.seams)} seams (max 2)"
            )
        for sid in st.seams:
            if sid not in by_id:
                raise RejectError(
                    "invalid_strip", f"strip {st.strip_id} references unknown seam {sid}"
                )
        if not st.allowed:
            raise RejectError("invalid_strip", f"strip {st.strip_id} has no allowed tuples")
        for tup in st.allowed:
            if len(tup) != len(st.seams):
                raise RejectError(
                    "invalid_strip",
                    f"strip {st.strip_id} allowed tuple {tup} has wrong arity",
                )
            for level, sid in zip(tup, st.seams):
                if level not in by_id[sid].levels:
                    raise RejectError(
                        "invalid_strip",
                        f"strip {st.strip_id} uses unapproved level {level} on seam {sid}",
                    )
        strip_objs.append(st)

    start = tuple(int(v) for v in start_pose)
    if len(start) != len(seam_objs):
        raise RejectError("invalid_pose", "start pose length must equal seam count")
    for level, seam in zip(start, seam_objs):
        if level not in seam.levels:
            raise RejectError(
                "invalid_pose",
                f"start pose level {level} not approved on seam {seam.seam_id}",
            )

    return LeafAssembly(
        assembly_id=assembly_id,
        case_id=case_id,
        seams=seam_objs,
        strips=strip_objs,
        start_pose=start,
        members=tuple(str(m) for m in members),
    )


@dataclass
class PatchMeasurement:
    """A new patch measurement (补条新测) that re-specifies a strip.

    ``updates`` maps ``strip_id`` to a spec.  A spec is either a bare sequence
    of allowed level tuples, or a mapping with any of:

    * ``allowed``        -- the new permitted level tuples; and
    * ``hold_positions`` -- the columns where a clamp may support the strip; and
    * ``hold_at``        -- the projections at which a clamp is *required*.

    The measurement is only accepted when ``signer`` is the case's *current
    custodian* -- a prepared (unconfirmed) transfer does not shift that
    authority.
    """

    measurement_id: str
    revision: int
    case_id: str
    as_of_day: int
    signer: str
    updates: dict[str, object]


class SupportModel:
    """Whole-volume support table, witnesses, queries and measurement ledger."""

    def __init__(self, assembly: LeafAssembly, custodian_of=None):
        self.assembly = assembly
        self.custodian_of = custodian_of or (lambda case_id: None)
        self.seam_order: list[str] = assembly.seam_ids()
        self.index: dict[str, int] = {sid: i for i, sid in enumerate(self.seam_order)}
        self.levels_by_seam: dict[str, tuple[int, ...]] = {
            s.seam_id: tuple(sorted(s.levels)) for s in assembly.seams
        }
        self.strips: list[PatchStrip] = list(assembly.strips)
        self._strip_by_id = {st.strip_id: st for st in self.strips}
        self._table_cache: Optional[list[tuple[int, ...]]] = None
        # Mutable support/hold maps (may be replaced by a signed measurement).
        self.allowed_map: dict[str, frozenset[tuple[int, ...]]] = {
            st.strip_id: frozenset(st.allowed) for st in self.strips
        }
        self.hold_positions: dict[str, frozenset[int]] = {
            st.strip_id: frozenset(st.hold_positions) for st in self.strips
        }
        self.hold_at: dict[str, frozenset[tuple[int, ...]]] = {
            st.strip_id: frozenset(st.hold_at) for st in self.strips
        }
        # measurement ledger: id -> stored record
        self.measurements: dict[str, dict] = {}
        # provenance: strip_id -> signer of the last measurement that set it
        self.strip_signer: dict[str, Optional[str]] = {
            st.strip_id: None for st in self.strips
        }

    # ------------------------------------------------------------- geometry
    def _project(self, pose: tuple[int, ...], strip: PatchStrip) -> tuple[int, ...]:
        return tuple(pose[self.index[sid]] for sid in strip.seams)

    def violations(self, pose: tuple[int, ...]) -> list[tuple[PatchStrip, tuple[int, ...]]]:
        """All strips whose allowed set does not contain the pose's projection."""
        bad = []
        for st in self.strips:
            proj = self._project(pose, st)
            if proj not in self.allowed_map[st.strip_id]:
                bad.append((st, proj))
        return bad

    def is_supported(self, pose: tuple[int, ...]) -> bool:
        return not self.violations(pose)

    def support_table(self) -> list[tuple[int, ...]]:
        if self._table_cache is None:
            axes = [self.levels_by_seam[sid] for sid in self.seam_order]
            table = [p for p in itertools.product(*axes) if self.is_supported(p)]
            self._table_cache = sorted(table)
        return self._table_cache

    def _invalidate(self) -> None:
        self._table_cache = None

    # ------------------------------------------------------------- witnesses
    def support_witness(self) -> dict:
        """The whole-volume support witness: every statically safe pose."""
        table = self.support_table()
        return {
            "assembly_id": self.assembly.assembly_id,
            "case_id": self.assembly.case_id,
            "seams": [
                {"seam_id": s.seam_id, "levels": list(s.levels)} for s in self.assembly.seams
            ],
            "support_count": len(table),
            "support_poses": [pose_str(p) for p in table],
            "strips": [
                {
                    "strip_id": st.strip_id,
                    "doc_id": st.doc_id,
                    "leaf_group": st.leaf_group,
                    "seams": list(st.seams),
                    "allowed": [list(t) for t in sorted(self.allowed_map[st.strip_id])],
                    "hold_positions": sorted(self.hold_positions[st.strip_id]),
                    "hold_at": [list(t) for t in sorted(self.hold_at[st.strip_id])],
                    "signed_by": self.strip_signer.get(st.strip_id),
                }
                for st in self.strips
            ],
        }

    def pose_witness(self, pose: Iterable[int]) -> dict:
        """Explain one pose: which strips match, or which strips conflict."""
        pose = tuple(int(v) for v in pose)
        self._check_pose_shape(pose)
        bad = self.violations(pose)
        detail = []
        for st in self.strips:
            proj = self._project(pose, st)
            detail.append(
                {
                    "strip_id": st.strip_id,
                    "doc_id": st.doc_id,
                    "leaf_group": st.leaf_group,
                    "seams": list(st.seams),
                    "projection": list(proj),
                    "allowed": [list(t) for t in sorted(self.allowed_map[st.strip_id])],
                    "ok": proj in self.allowed_map[st.strip_id],
                }
            )
        return {
            "pose": pose_str(pose),
            "supported": not bad,
            "conflicts": [
                {"strip_id": st.strip_id, "doc_id": st.doc_id, "projection": list(proj)}
                for st, proj in bad
            ],
            "strips": detail,
        }

    def _check_pose_shape(self, pose: tuple[int, ...]) -> None:
        if len(pose) != len(self.seam_order):
            raise RejectError(
                "invalid_pose", f"pose must have {len(self.seam_order)} levels"
            )
        for level, sid in zip(pose, self.seam_order):
            if level not in self.levels_by_seam[sid]:
                raise RejectError(
                    "invalid_pose", f"level {level} not approved on seam {sid}"
                )

    # ---------------------------------------------------------------- queries
    def query_seam(self, seam_id: str) -> dict:
        """Independently inspect one seam.

        For each approved level we report:
          * ``extendable`` -- can it appear in *some* supported whole-volume pose?
          * ``marginal``   -- is it admitted by every single-seam strip on this
            seam, ignoring the rest of the volume?
          * ``constraining_strips`` -- strips that rule the level out.
        """
        if seam_id not in self.index:
            raise RejectError("unknown_seam", f"unknown seam {seam_id}")
        idx = self.index[seam_id]
        table = self.support_table()
        extendable_levels = {p[idx] for p in table}
        touching = [st for st in self.strips if seam_id in st.seams]
        out_levels = []
        for level in self.levels_by_seam[seam_id]:
            constraining = []
            for st in touching:
                # For a single-seam strip, check the level directly.  For a
                # two-seam strip, this level is marginally allowed if some
                # allowed tuple has it in the right position.
                pos = st.seams.index(seam_id)
                if not any(tup[pos] == level for tup in self.allowed_map[st.strip_id]):
                    constraining.append({"strip_id": st.strip_id, "doc_id": st.doc_id})
            out_levels.append(
                {
                    "level": level,
                    "extendable": level in extendable_levels,
                    "marginal": not constraining,
                    "constraining_strips": constraining,
                }
            )
        return {
            "seam_id": seam_id,
            "levels": out_levels,
            "strips": [
                {"strip_id": st.strip_id, "doc_id": st.doc_id, "seams": list(st.seams)}
                for st in touching
            ],
        }

    def query_combination(self, partial: dict[str, int]) -> dict:
        """Can this level combination at several seams be held safely?

        Returns a witness full pose when it extends to one; otherwise a conflict
        listing the patch strips and original pages that make it impossible.
        """
        for sid, level in partial.items():
            if sid not in self.index:
                raise RejectError("unknown_seam", f"unknown seam {sid}")
            if level not in self.levels_by_seam[sid]:
                raise RejectError(
                    "invalid_pose", f"level {level} not approved on seam {sid}"
                )
        table = self.support_table()
        for pose in table:
            if all(pose[self.index[sid]] == lv for sid, lv in partial.items()):
                return {
                    "partial": dict(sorted(partial.items())),
                    "holdable": True,
                    "witness_pose": pose_str(pose),
                    "conflicts": [],
                }
        # No supported pose matches: find the strips that individually conflict.
        conflicts = []
        for st in self.strips:
            # Restrict this strip's allowed tuples to those consistent with the
            # partial on the seams the strip actually touches.
            positions = [
                (pos, partial[sid])
                for pos, sid in enumerate(st.seams)
                if sid in partial
            ]
            if not positions:
                continue
            ok = any(
                all(tup[pos] == lv for pos, lv in positions)
                for tup in self.allowed_map[st.strip_id]
            )
            if not ok:
                conflicts.append(
                    {
                        "strip_id": st.strip_id,
                        "doc_id": st.doc_id,
                        "leaf_group": st.leaf_group,
                        "seams": list(st.seams),
                        "requested": {
                            st.seams[pos]: lv for pos, lv in positions
                        },
                        "allowed": [list(t) for t in sorted(self.allowed_map[st.strip_id])],
                    }
                )
        return {
            "partial": dict(sorted(partial.items())),
            "holdable": False,
            "witness_pose": None,
            "conflicts": conflicts,
        }

    # ---------------------------------------------------------- reachability
    def _neighbors(self, pose: tuple[int, ...]) -> list[tuple[tuple[int, ...], tuple[str, int, int]]]:
        """All one-seam level changes that stay inside the support table."""
        out = []
        for i, sid in enumerate(self.seam_order):
            for level in self.levels_by_seam[sid]:
                if level == pose[i]:
                    continue
                new = pose[:i] + (level,) + pose[i + 1:]
                if self.is_supported(new):
                    out.append((new, (sid, pose[i], level)))
        return out

    def reachable_from(self, start: Iterable[int]) -> dict[tuple[int, ...], Optional[tuple]]:
        start = tuple(int(v) for v in start)
        self._check_pose_shape(start)
        seen: dict[tuple[int, ...], Optional[tuple]] = {start: None}
        q = deque([start])
        while q:
            cur = q.popleft()
            for nxt, _ in self._neighbors(cur):
                if nxt not in seen:
                    seen[nxt] = cur
                    q.append(nxt)
        return seen

    def shortest_route(self, start: Iterable[int], goal: Iterable[int]) -> Optional[list[dict]]:
        """Shortest one-seam-at-a-time route, or ``None`` if unreachable.

        Every pose on the route (including ``goal``) must be supported.
        """
        start = tuple(int(v) for v in start)
        goal = tuple(int(v) for v in goal)
        self._check_pose_shape(start)
        self._check_pose_shape(goal)
        if not self.is_supported(start):
            return None
        if not self.is_supported(goal):
            return None
        prev: dict[tuple[int, ...], Optional[tuple]] = {start: None}
        move: dict[tuple[int, ...], Optional[tuple]] = {start: None}
        q = deque([start])
        while q:
            cur = q.popleft()
            if cur == goal:
                break
            for nxt, desc in self._neighbors(cur):
                if nxt not in prev:
                    prev[nxt] = cur
                    move[nxt] = desc
                    q.append(nxt)
        if goal not in prev:
            return None
        chain = []
        node = goal
        while prev[node] is not None:
            sid, old, new = move[node]
            chain.append({"seam_id": sid, "from": old, "to": new})
            node = prev[node]
        chain.reverse()
        return chain

    def target_reachable(self, start: Iterable[int], goal: Iterable[int]) -> bool:
        return self.shortest_route(start, goal) is not None

    # ---------------------------------------------------- measurement ledger
    @staticmethod
    def _normalise_spec(spec) -> dict:
        """Normalise a measurement spec into ``{allowed, hold_positions, hold_at}``.

        ``spec`` is either a bare sequence of level tuples (allowed only) or a
        mapping with any of ``allowed`` / ``hold_positions`` / ``hold_at``.
        Missing keys are ``None`` (meaning "leave unchanged").
        """
        if isinstance(spec, dict):
            allowed = spec.get("allowed")
            hold_pos = spec.get("hold_positions")
            hold_at = spec.get("hold_at")
        else:
            allowed = spec
            hold_pos = None
            hold_at = None
        out: dict = {"allowed": None, "hold_positions": None, "hold_at": None}
        if allowed is not None:
            out["allowed"] = tuple(tuple(int(v) for v in tup) for tup in allowed)
        if hold_pos is not None:
            out["hold_positions"] = tuple(sorted(int(p) for p in hold_pos))
        if hold_at is not None:
            out["hold_at"] = tuple(tuple(int(v) for v in tup) for tup in hold_at)
        return out

    def _measurement_canon(self, m: PatchMeasurement, clean: dict) -> str:
        import json

        payload = {
            "measurement_id": m.measurement_id,
            "revision": m.revision,
            "case_id": m.case_id,
            "as_of_day": m.as_of_day,
            "signer": m.signer,
            "updates": {},
        }
        for strip_id in sorted(clean):
            spec = clean[strip_id]
            payload["updates"][strip_id] = {
                "allowed": [list(t) for t in spec["allowed"]] if spec["allowed"] is not None else None,
                "hold_positions": list(spec["hold_positions"]) if spec["hold_positions"] is not None else None,
                "hold_at": [list(t) for t in spec["hold_at"]] if spec["hold_at"] is not None else None,
            }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def _validate_tuple(self, strip_id: str, tup, seams) -> tuple[int, ...]:
        tup = tuple(int(v) for v in tup)
        if len(tup) != len(seams):
            raise RejectError(
                "invalid_measurement",
                f"strip {strip_id} tuple {tup} has wrong arity",
            )
        for level, sid in zip(tup, seams):
            if level not in self.levels_by_seam[sid]:
                raise RejectError(
                    "invalid_measurement",
                    f"strip {strip_id} uses unapproved level {level} on {sid}",
                )
        return tup

    def submit_measurement(self, m: PatchMeasurement) -> dict:
        """Accept a signed patch measurement, or reject it.

        * Must be signed by the case's *current custodian* (last committed
          holder -- a prepared/planned transfer does not count).
        * Same id + revision + content replays as a no-op; different content or
          an older revision is a conflict.
        * Updates replace the allowed tuples / hold positions / hold points of
          the named strips (whichever the spec supplies).
        """
        if not m.measurement_id:
            raise RejectError("invalid_measurement", "measurement_id required")
        if not _is_int(m.revision) or m.revision < 1:
            raise RejectError("invalid_measurement", "revision must be int >= 1")
        if not _is_int(m.as_of_day) or m.as_of_day < 0:
            raise RejectError("invalid_measurement", "as_of_day must be int >= 0")
        if m.case_id != self.assembly.case_id:
            raise RejectError(
                "measurement_case_mismatch",
                f"measurement case {m.case_id} != assembly case {self.assembly.case_id}",
            )
        custodian = self.custodian_of(m.case_id)
        if m.signer != custodian:
            raise RejectError(
                "unsigned_measurement",
                f"measurement {m.measurement_id} signed by {m.signer} "
                f"but current custodian is {custodian}",
            )

        clean: dict[str, dict] = {}
        for strip_id, raw_spec in m.updates.items():
            st = self._strip_by_id.get(strip_id)
            if st is None:
                raise RejectError("unknown_strip", f"unknown strip {strip_id}")
            spec = self._normalise_spec(raw_spec)
            if spec["allowed"] is not None:
                if not spec["allowed"]:
                    raise RejectError(
                        "invalid_measurement", f"strip {strip_id} allowed set is empty"
                    )
                spec["allowed"] = tuple(
                    self._validate_tuple(strip_id, tup, st.seams)
                    for tup in spec["allowed"]
                )
            if spec["hold_at"] is not None:
                spec["hold_at"] = tuple(
                    self._validate_tuple(strip_id, tup, st.seams)
                    for tup in spec["hold_at"]
                )
            if all(spec[k] is None for k in ("allowed", "hold_positions", "hold_at")):
                raise RejectError(
                    "invalid_measurement", f"strip {strip_id} update is empty"
                )
            clean[strip_id] = spec

        canon = self._measurement_canon(m, clean)
        existing = self.measurements.get(m.measurement_id)
        if existing is not None:
            if m.revision == existing["revision"]:
                if existing["canon"] != canon:
                    raise ConflictError(
                        "measurement_conflict",
                        f"measurement {m.measurement_id} rev {m.revision} "
                        "replayed with different content",
                    )
                return {"measurement_id": m.measurement_id, "replayed": True,
                        "changed": False,
                        "support_poses": [pose_str(p) for p in self.support_table()]}
            if m.revision < existing["revision"]:
                raise ConflictError(
                    "stale_revision",
                    f"measurement {m.measurement_id} rev {m.revision} older than "
                    f"stored rev {existing['revision']}",
                )

        for strip_id, spec in clean.items():
            if spec["allowed"] is not None:
                self.allowed_map[strip_id] = frozenset(spec["allowed"])
            if spec["hold_positions"] is not None:
                self.hold_positions[strip_id] = frozenset(spec["hold_positions"])
            if spec["hold_at"] is not None:
                self.hold_at[strip_id] = frozenset(spec["hold_at"])
            self.strip_signer[strip_id] = m.signer
        self._invalidate()
        self.measurements[m.measurement_id] = {
            "revision": m.revision,
            "canon": canon,
            "signer": m.signer,
            "as_of_day": m.as_of_day,
            "updates": clean,
        }
        return {
            "measurement_id": m.measurement_id,
            "replayed": False,
            "changed": True,
            "signer": m.signer,
            "support_poses": [pose_str(p) for p in self.support_table()],
        }

    def signed_strip_ids(self, custodian: str) -> set[str]:
        """Strips whose current allowed set was signed by ``custodian``."""
        return {
            sid for sid, signer in self.strip_signer.items() if signer == custodian
        }
