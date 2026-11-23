"""Photography-station planning for the rare-volume shot list.

Only **one** jig family is implemented: the movable clamp bracket
(可移夹手).  The ordinary machine that ships with a *single* clamp is the
default configuration (``clamp_slots=1``); the double-clamp upgrade is the same
model with ``clamp_slots=2``.  The fixed-stop frame (定档框) is *not* implemented
as an operating jig; :func:`stop_frame_probe` only answers the conceptual
fixed-angle / bar-occlusion question so the report can say what the stop frame
would (or would not) allow.

Model
-----
* A station has a row of photo columns and fixed left/right occlusion bars.
* A clamp sits on a column and *really* occludes that column (``手里的夹会真实遮字``).
  A fixed bar column is always occluded.
* Each target names a seam, the level that must be readable, and the columns
  that must be visible (必见栏).
* A pose is supported when every patch strip admits its projection (see
  :mod:`binding`).  In addition a *strip that needs holding* at a pose needs a
  clamp on one of its permitted hold positions.
* Operations (each changes exactly one opening angle or one clamp):
    - ``open_seam``  : change one seam level, clamped positions unchanged -- the
      resulting pose must still be supported *and* still clamped-safe.  Cost 1.
    - ``add_clamp``  : seat a clamp on a free column (engage).  Cost 1.
    - ``remove_clamp``: lift a clamp (withdraw) -- only when the remaining clamps
      still hold every active strip.  Cost 1.
  Because a clamp may only be *removed* once coverage is kept, relocating a hold
  forces **engage-new-before-withdraw-old**; the planner cannot "skip" the
  engage step.
* ``shoot`` : capture -- requires the target seam at its level, a supported pose,
  clamped coverage, and every must-see column visible.  Cost = 1 frame.
* Changing the page (a target on a different seam) first retracts every opened
  seam to the handover pose; the path therefore visits the start pose.

Every intermediate (pose, clamps) state is validated as a whole; the volume is
never lifted and re-laid.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Iterable, Optional

from .binding import PatchStrip, SupportModel, pose_str
from .models import RejectError

MAX_TARGETS = 4


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class PhotoStation:
    station_id: str
    institution: str                      # custodian party that owns the table
    columns: tuple[int, ...]              # photo columns (left..right)
    left_bar: frozenset[int] = frozenset()
    right_bar: frozenset[int] = frozenset()
    clamp_slots: int = 1                  # ordinary default machine = 1 clamp

    def __post_init__(self):
        if self.clamp_slots < 1:
            raise RejectError("invalid_station", "clamp_slots must be >= 1")
        for c in (*self.columns, *self.left_bar, *self.right_bar):
            if not _is_int(c):
                raise RejectError("invalid_station", f"column {c!r} must be an int")
        all_cols = set(self.columns)
        if not set(self.left_bar) <= all_cols or not set(self.right_bar) <= all_cols:
            raise RejectError("invalid_station", "bar columns must be photo columns")

    def fixed_hidden(self) -> frozenset[int]:
        return frozenset(self.left_bar | self.right_bar)

    def hidden(self, clamps: frozenset[int]) -> frozenset[int]:
        return self.fixed_hidden() | frozenset(clamps)


@dataclass(frozen=True)
class TargetShot:
    shot_id: str
    seam_id: str
    required_level: int
    must_see: frozenset[int] = frozenset()


@dataclass
class Action:
    kind: str                 # "open_seam" | "add_clamp" | "remove_clamp" | "shoot"
    detail: dict
    pose: str
    clamps: tuple[int, ...]
    cost: int = 1
    pre_pose: str = ""
    pre_clamps: tuple[int, ...] = ()

    def as_dict(self) -> dict:
        return {
            "action": self.kind,
            "detail": self.detail,
            "pre": {"pose": self.pre_pose, "clamps": list(self.pre_clamps)},
            "post": {"pose": self.pose, "clamps": list(self.clamps)},
            "cost": self.cost,
        }


# --------------------------------------------------------------------------
# Static analysis of the *other* jig (not the implemented operating path)
# --------------------------------------------------------------------------
def stop_frame_probe(station: PhotoStation, target: TargetShot, supported: bool,
                     hold_ok: bool, pad_swap: Optional[dict] = None) -> dict:
    """Conceptual fixed-stop-frame verdict for one target.

    The stop frame does not reshape clamps; it only holds *fixed* inter-part
    angles.  We therefore judge (a) from a qualified starting pose, whether the
    target's level is an allowed *fixed* angle, and (b) whether the frame's own
    bars occlude a must-see column before any pad swap.  This is an analysis
    aid only -- the executable planner is the clamp jig.

    ``pad_swap`` (optional) models the brief's numbered short-pad substitution:
    between shots with a qualified initial pose one stop may be replaced by a
    numbered pad, but *until the swap happens the old frame's limits still
    apply*.  We only report the semantics; nothing is executed.
    """
    bar_hidden = target.must_see & station.fixed_hidden()
    frame_ok = supported and hold_ok and not bar_hidden
    out = {
        "jig": "stop_frame (analysis only)",
        "target": target.shot_id,
        "angle_allowed": supported,
        "hold_ok": hold_ok,
        "bar_occludes_must_see": sorted(bar_hidden),
        "shootable_before_pad_swap": frame_ok,
        "note": (
            "fixed frame may reach the level but its bars permanently hide a "
            "must-see column" if bar_hidden else "no fixed-bar occlusion"
        ),
    }
    if pad_swap is not None:
        initial_ok = bool(pad_swap.get("qualified_initial_pose", False))
        out["pad_swap"] = {
            "numbered_pad": pad_swap.get("pad"),
            "replaces_stop": pad_swap.get("stop"),
            "qualified_initial_pose": initial_ok,
            "old_limits_valid_until_swap": True,
            "shootable_only_after_swap": frame_ok and initial_ok,
            "note": "a numbered short pad may replace one stop between qualified "
                    "poses; before the swap the old frame's stop still limits the angle",
        }
    return out


# --------------------------------------------------------------------------
# Combined-state planner for the clamp jig
# --------------------------------------------------------------------------
class ClampJigPlanner:
    def __init__(self, station: PhotoStation, support: SupportModel,
                 initial_clamps: Iterable[int] = ()):
        self.station = station
        self.support = support
        self.initial_clamps = frozenset(int(c) for c in initial_clamps)
        for c in self.initial_clamps:
            if c not in self.station.columns:
                raise RejectError(
                    "invalid_station", f"initial clamp column {c} not on station"
                )
        if len(self.initial_clamps) > self.station.clamp_slots:
            raise RejectError(
                "invalid_station",
                f"{len(self.initial_clamps)} initial clamps exceed "
                f"{self.station.clamp_slots} slots",
            )

    # ------------------------------------------------------------ predicates
    def _active_strips(self, pose: tuple[int, ...]) -> list[PatchStrip]:
        """Strips that must be actively clamped at this pose.

        A strip is active when its projection is one of its ``hold_at`` tuples
        (e.g. the right patch only needs takeover once the right seam is at
        ``11``).  A strip with no ``hold_at`` never demands a clamp.
        """
        return [
            st for st in self.support.strips
            if self.support.hold_positions[st.strip_id]
            and self.support._project(pose, st) in self.support.hold_at[st.strip_id]
        ]

    def coverage_ok(self, pose: tuple[int, ...], clamps: frozenset[int]) -> bool:
        for st in self._active_strips(pose):
            if not (clamps & self.support.hold_positions[st.strip_id]):
                return False
        return True

    def pose_supported(self, pose: tuple[int, ...]) -> bool:
        return self.support.is_supported(pose)

    def state_safe(self, pose: tuple[int, ...], clamps: frozenset[int]) -> bool:
        return (
            self.pose_supported(pose)
            and len(clamps) <= self.station.clamp_slots
            and self.coverage_ok(pose, clamps)
        )

    def shootable(self, pose: tuple[int, ...], clamps: frozenset[int],
                  target: TargetShot) -> bool:
        if not self.state_safe(pose, clamps):
            return False
        if pose[self.support.index[target.seam_id]] != target.required_level:
            return False
        visible = frozenset(self.station.columns) - self.station.hidden(clamps)
        return target.must_see <= visible

    # -------------------------------------------------------------- movement
    def _at_handover(self, pose: tuple[int, ...]) -> bool:
        return all(level == 0 for level in pose)

    def _clamp_changes_need_handover(self) -> bool:
        """A single-clamp machine must retract before it relocates its clamp.

        The ordinary machine has only one clamp, so moving it is a *clamp
        change* and the opened seams must first return to the handover pose.
        The double-clamp machine keeps support while it engages the new clamp
        before withdrawing the old one, so it may relocate at the current pose.
        """
        return self.station.clamp_slots == 1

    def _successors(self, pose, clamps):
        out = []
        # open/close one seam (clamp positions unchanged)
        for i, sid in enumerate(self.support.seam_order):
            for level in self.support.levels_by_seam[sid]:
                if level == pose[i]:
                    continue
                new = pose[:i] + (level,) + pose[i + 1:]
                if self.state_safe(new, clamps):
                    out.append(("open_seam", {"seam_id": sid, "from": pose[i], "to": level},
                                new, clamps))
        needs_handover = self._clamp_changes_need_handover()
        may_change_clamp = (not needs_handover) or self._at_handover(pose)
        # engage a clamp on a free column
        if may_change_clamp and len(clamps) < self.station.clamp_slots:
            for c in self.station.columns:
                if c in clamps:
                    continue
                nc = clamps | {c}
                if self.state_safe(pose, nc):
                    out.append(("add_clamp", {"column": c}, pose, nc))
        # withdraw a clamp only if the remainder still holds every active strip
        if may_change_clamp:
            for c in clamps:
                nc = clamps - {c}
                if self.state_safe(pose, nc):
                    out.append(("remove_clamp", {"column": c}, pose, nc))
        return out

    def _bfs(self, start_pose, start_clamps, goal_test, limit=200000):
        start = (start_pose, frozenset(start_clamps))
        if not self.state_safe(*start):
            # An unsafe start may still be repairable only by seal-level moves
            # that are themselves safe; if the start itself is unsafe there is
            # no legal first action.
            if not self.pose_supported(start_pose):
                return None
        prev = {start: None}
        move = {start: None}
        q = deque([start])
        explored = 0
        while q:
            cur = q.popleft()
            explored += 1
            if explored > limit:
                return None
            if goal_test(*cur):
                # reconstruct
                chain = []
                node = cur
                while prev[node] is not None:
                    kind, detail, pose, clamps = move[node]
                    chain.append((kind, detail, pose, clamps))
                    node = prev[node]
                chain.reverse()
                return chain
            for kind, detail, npose, nclamps in self._successors(*cur):
                nxt = (npose, frozenset(nclamps))
                if nxt not in prev:
                    prev[nxt] = cur
                    move[nxt] = (kind, detail, npose, frozenset(nclamps))
                    q.append(nxt)
        return None

    # ---------------------------------------------------------------- target
    def _retract(self, pose, clamps):
        start_pose = self.support.assembly.start_pose
        if pose == start_pose:
            return []
        chain = self._bfs(
            pose, clamps,
            lambda p, c: p == start_pose,
        )
        return chain

    def _plan_one(self, pose, clamps, target: TargetShot, retract_first: bool):
        # Validate the target against the model before searching.
        if target.seam_id not in self.support.index:
            raise RejectError("unknown_seam", f"target {target.shot_id} unknown seam")
        if target.required_level not in self.support.levels_by_seam[target.seam_id]:
            raise RejectError(
                "invalid_target",
                f"target {target.shot_id} level {target.required_level} not approved",
            )
        if target.must_see & self.station.fixed_hidden():
            return None, {
                "reason": "column_occluded",
                "detail": f"must-see {sorted(target.must_see & self.station.fixed_hidden())} "
                          "permanently hidden by a fixed bar on this station",
            }

        chain: list[tuple] = []
        cur_pose, cur_clamps = pose, frozenset(clamps)
        if retract_first:
            seg = self._retract(cur_pose, cur_clamps)
            if seg is None:
                return None, {"reason": "no_route",
                              "detail": "cannot retract to handover pose safely"}
            chain.extend(seg)
            if seg:
                cur_pose, cur_clamps = seg[-1][2], seg[-1][3]

        # Pre-flight: classify why a target may be impossible.
        poses_at_level = [
            p for p in self.support.support_table()
            if p[self.support.index[target.seam_id]] == target.required_level
        ]
        if not poses_at_level:
            return None, {
                "reason": "pose_unsupported",
                "detail": "no supported whole-volume pose has this seam at the "
                          "required level",
            }
        if not any(self._coverage_possible(p, target, ignore_must_see=True)
                   for p in poses_at_level):
            return None, {
                "reason": "no_coverage",
                "detail": "no supported pose at the required level admits a clamp "
                          "coverage of every active patch strip",
            }
        if not any(self._coverage_possible(p, target, ignore_must_see=False)
                   for p in poses_at_level):
            return None, {
                "reason": "hold_occludes_must_see",
                "detail": "every clamp coverage that holds the active strips covers "
                          "a must-see column (the only shared hold position hides it)",
            }

        seg = self._bfs(
            cur_pose, cur_clamps,
            lambda p, c: self.shootable(p, c, target),
        )
        if seg is None:
            return None, {
                "reason": "no_route",
                "detail": "no legal one-seam / one-clamp path to a shootable state "
                          "(the goal may be disconnected in the support table)",
            }
        chain.extend(seg)
        if seg:
            cur_pose, cur_clamps = seg[-1][2], seg[-1][3]
        return (chain, cur_pose, cur_clamps), None

    def _coverage_possible(self, pose: tuple[int, ...], target: TargetShot,
                           ignore_must_see: bool) -> bool:
        from itertools import combinations

        positions = sorted(self.station.columns)
        for size in range(0, min(self.station.clamp_slots, len(positions)) + 1):
            for combo in combinations(positions, size):
                cs = frozenset(combo)
                if not self.coverage_ok(pose, cs):
                    continue
                if ignore_must_see:
                    return True
                visible = frozenset(self.station.columns) - self.station.hidden(cs)
                if target.must_see <= visible:
                    return True
        return False

    # ---------------------------------------------------------------- plan
    def plan(self, targets: Iterable[TargetShot], *,
             initial_pose: Optional[Iterable[int]] = None) -> dict:
        targets = list(targets)
        if len(targets) > MAX_TARGETS:
            raise RejectError(
                "invalid_target", f"at most {MAX_TARGETS} targets, got {len(targets)}"
            )
        if initial_pose is None:
            pose = self.support.assembly.start_pose
        else:
            pose = tuple(int(v) for v in initial_pose)
            self.support._check_pose_shape(pose)
        clamps = self.initial_clamps

        actions: list[Action] = []
        frames = 0
        seam_moves = 0
        clamp_moves = 0
        per_target = []
        last_seam = None
        feasible = True

        for target in targets:
            # The ordinary single-clamp machine must retract every opened seam
            # to the handover pose when it changes page OR clamp.  The movable
            # double-clamp bracket keeps support as it engages a new clamp before
            # withdrawing the old one, so it "passes through more angles" and is
            # not forced to retract between frames.
            page_changed = last_seam is not None and target.seam_id != last_seam
            retract_first = page_changed and self._clamp_changes_need_handover()
            planned, err = self._plan_one(pose, clamps, target, retract_first)
            if err is not None:
                feasible = False
                per_target.append(
                    {"shot_id": target.shot_id, "seam_id": target.seam_id,
                     "required_level": target.required_level,
                     "must_see": sorted(target.must_see),
                     "feasible": False, **err}
                )
                # Cannot proceed past an infeasible target; remaining stay unplanned.
                continue
            chain, new_pose, new_clamps = planned
            cur_pose, cur_clamps = pose, clamps
            for kind, detail, p, c in chain:
                actions.append(Action(kind, detail, pose_str(p), tuple(sorted(c)),
                                      cost=1, pre_pose=pose_str(cur_pose),
                                      pre_clamps=tuple(sorted(cur_clamps))))
                if kind == "open_seam":
                    seam_moves += 1
                else:
                    clamp_moves += 1
                cur_pose, cur_clamps = p, c
            # shoot
            frames += 1
            actions.append(
                Action("shoot",
                       {"shot_id": target.shot_id, "seam_id": target.seam_id,
                        "level": target.required_level,
                        "visible": sorted(set(self.station.columns)
                                          - self.station.hidden(new_clamps)),
                        "clamps": sorted(new_clamps)},
                       pose_str(new_pose), tuple(sorted(new_clamps)),
                       cost=1, pre_pose=pose_str(cur_pose),
                       pre_clamps=tuple(sorted(cur_clamps)))
            )
            per_target.append(
                {"shot_id": target.shot_id, "seam_id": target.seam_id,
                 "required_level": target.required_level,
                 "must_see": sorted(target.must_see),
                 "feasible": True,
                 "pose": pose_str(new_pose),
                 "clamps": sorted(new_clamps),
                 "visible": sorted(set(self.station.columns)
                                   - self.station.hidden(new_clamps)),
                 "moves": len(chain)}
            )
            pose, clamps = new_pose, new_clamps
            last_seam = target.seam_id

        return {
            "jig": f"clamp_bracket(clamp_slots={self.station.clamp_slots})",
            "station": self.station.station_id,
            "institution": self.station.institution,
            "feasible": feasible,
            "targets": per_target,
            "actions": [a.as_dict() for a in actions],
            "budget": {
                "frames": frames,
                "seam_moves": seam_moves,
                "clamp_moves": clamp_moves,
                "total_ops": seam_moves + clamp_moves + frames,
            },
        }

    # ------------------------------------------------- guiding / handover
    def handover_states(self, pose: tuple[int, ...], clamps: frozenset[int],
                        new_column: int, old_column: int) -> list[dict]:
        """Explain the engage-before-withdraw handover for one clamp move.

        Returns the two safe clamp states (before / after) and the ordered
        actions; raises if the direct single move would drop support.
        """
        if old_column not in clamps:
            raise RejectError("invalid_clamp", f"clamp not at column {old_column}")
        if len(clamps) >= self.station.clamp_slots:
            raise RejectError(
                "no_free_slot",
                "no free clamp slot: engage-before-withdraw needs a second clamp",
            )
        engaged = clamps | {new_column}
        withdrawn = engaged - {old_column}
        if not self.state_safe(pose, engaged):
            raise RejectError("unsafe_clamp", "engaging the new clamp is unsafe")
        if not self.state_safe(pose, withdrawn):
            raise RejectError("unsafe_clamp", "withdrawing the old clamp is unsafe")
        return [
            {"step": "engage_new", "clamps": sorted(engaged),
             "safe": True, "action": {"add_clamp": new_column}},
            {"step": "withdraw_old", "clamps": sorted(withdrawn),
             "safe": True, "action": {"remove_clamp": old_column}},
            {"note": "the two states are each safe; the engage cannot be skipped"},
        ]
