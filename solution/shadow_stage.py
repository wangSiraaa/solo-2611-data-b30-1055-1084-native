"""Daytime shadow-puppet stage: physical slots, lighting, occlusion and the
beat-by-beat performance scheduler.

This sits on top of the chainage / inspection data in
:mod:`chainage_catalog` / :mod:`inspection_library`: the route's *physical*
axis is what decides whether a slot and its shade board were really walked and
really measured.  Nothing here treats a chainage *label* as a global
coordinate.

Concepts
--------
slots      Performance "shadow positions" (影位) on an integer grid.  A slot
           is only usable once its rebuilt board is bound to a real *physical*
           position on the route and its walk to the source side was
           completed; a gap in that walk makes it ``unresolved`` -- never
           certified lit, never usable as a dark hiding slot.
lights     Per beat a light comes from east or west with a positive rational
           slope.  A slot is lit unless the nearest shade board on the
           up-light side, at grid distance ``d``, has ``height > d * slope``
           (equality leaves light).  Farther boards are irrelevant once a
           nearer one is too short.
grids      Each puppet carries a grid shape; one cell is the *face*.  A lit
           slot projects that grid onto the screen; the front row (OC1)
           covers the back row (OC2) where their cells overlap.  A dark slot
           projects nothing.
actors     A master (操偶师) holds one puppet at a time, may only present a
           role it has learned, and needs one empty beat to rebind to a
           different puppet.  Movement and turning cost the stage's declared
           beats.  A standby grant prebinds a real puppet to a named master
           who may lift it on time; a flip rig preloads two puppets so a face
           can change inside one beat, at the cost of one extra measured slot.

The scheduler validates an explicitly declared block (there is no search),
reports the first beat that cannot be joined, and renders the faces actually
visible on screen after occlusion.
"""

from __future__ import annotations

from fractions import Fraction

from chainage_catalog import as_fraction
from inspection_library import covered_by

EAST = "E"
WEST = "W"


def _f(value) -> Fraction:
    return as_fraction(value)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def grid_distance(a: int, b: int) -> int:
    return abs(int(a) - int(b))


def direction_sign(light_dir: str) -> int:
    """Sign to apply to ``(panel_x - slot_x)`` so that positive means the
    panel is on the **up-light** (source) side.

    Light from the west puts the source at smaller x, so a panel west of the
    slot (smaller x) is the one that can shade it -> sign ``-1``.  Light from
    the east -> sign ``+1``.
    """
    d = str(light_dir).upper()
    if d in (WEST, "WEST", "FROM_W"):
        return -1
    if d in (EAST, "EAST", "FROM_E"):
        return +1
    raise ValueError(f"unknown light direction {light_dir!r}")


# ---------------------------------------------------------------------------
# Lighting (with an explicit, citable board basis)
# ---------------------------------------------------------------------------


def slot_lighting(slot: dict, beat_light: dict, panels: list[dict]) -> dict:
    """Resolve one slot's light for one beat from measured boards only.

    Only boards on the up-light side can shade the slot; the nearest blocking
    board is the one cited as the reason.  A board that was never really
    measured, sitting on the up-light side, makes the slot ``unresolved``: we
    cannot certify a light state behind an unmeasured board.
    """
    light_dir = beat_light["dir"]
    slope = _f(beat_light["slope"])
    if slope <= 0:
        raise ValueError("light slope must be positive")

    step = direction_sign(light_dir)  # +1 -> larger x is up-light side
    slot_x = int(slot["grid_x"])

    # A slot on an un-walked survey leg can never be certified, whatever the
    # boards say: it might not even be where the plan claims.
    if slot.get("survey_status") != "walked":
        return {
            "status": "unresolved",
            "reason": "slot's walk is incomplete (missing measurement)",
            "blocker": None, "candidates": [],
        }

    # Every board on the up-light side, nearest first.  We walk outward from
    # the slot: a measured board tall enough shades it (dark) and hides any
    # board beyond it; a measured board too short lets the beam pass; an
    # unmeasured board is a measurement gap we cannot certify past.
    up_light = []
    for panel in panels:
        px = int(panel["grid_x"])
        signed = (px - slot_x) * step
        if signed <= 0:
            continue  # not on the up-light side (or level with the slot)
        up_light.append((grid_distance(px, slot_x), panel))
    up_light.sort(key=lambda t: t[0])

    for d, panel in up_light:
        if panel.get("surveyed", True) is not True:
            return {
                "status": "unresolved",
                "reason": f"unmeasured board {panel['panel_id']!r} on the "
                          f"up-light side: the walk to the source is incomplete",
                "blocker": None, "candidates": [],
            }
        height = _f(panel["height"])
        threshold = Fraction(d) * slope
        if height > threshold:
            return {
                "status": "dark",
                "reason": "shaded by nearest up-light board",
                "blocker": {
                    "panel_id": panel["panel_id"],
                    "physical_position": str(_f(panel["position"])),
                    "grid_distance": d,
                    "height": str(height),
                    "slope": str(slope),
                    "comparison": f"{height} > {d}*{slope} = {threshold}",
                },
                "candidates": [
                    {"panel_id": p["panel_id"], "grid_distance": dd,
                     "height": str(_f(p["height"])),
                     "would_block": _f(p["height"]) > Fraction(dd) * slope}
                    for dd, p in up_light
                ],
            }
        # Measured and too short: the beam passes this board; look farther.
    return {
        "status": "lit",
        "reason": "no up-light board tall enough above the beam",
        "blocker": None,
        "candidates": [
            {"panel_id": p["panel_id"], "grid_distance": dd,
             "height": str(_f(p["height"])),
             "would_block": _f(p["height"]) > Fraction(dd) * slope}
            for dd, p in up_light
        ],
    }


def all_slot_lights(env: dict) -> dict:
    """Light status for every slot at every declared beat."""
    lights = {sid: {} for sid in env["slots"]}
    for beat in env["beats"]:
        for sid, slot in env["slots"].items():
            lights[sid][beat["beat"]] = slot_lighting(
                slot, beat["light"], env.get("panels", [])
            )
    return lights


# ---------------------------------------------------------------------------
# Physical certification of slots against the route + inspections
# ---------------------------------------------------------------------------


def certify_slots(env: dict, assets, coverage_intervals=None, catalog=None) -> dict:
    """Bind each slot's locator plaque to a *physical* position on the route.

    ``env['slots'][sid]['locator_asset']`` names a rebuilt locator plaque.  We
    resolve it to the asset's physical position (never a label point, so a
    repeated label can never swap the physical section) and check it lies
    inside the assignment coverage (when supplied).  When a ``catalog`` is
    given, each slot also records the *physical* section its plaque sits in and
    the label it would carry under the current revision -- so the same label
    number naming two different slots is exposed rather than collapsed.

    Returns a per-slot record with any certification issues.
    """
    asset_by_id = {a.asset_id: a for a in assets}
    coverage = list(coverage_intervals) if coverage_intervals else []

    def section_of(point):
        if catalog is None:
            return None, None
        for s in catalog.sections:
            if s.contains_physical_point(point):
                return s.section_id, s.label_at(point)
        return None, None

    out = {}
    for sid, slot in env["slots"].items():
        rec = {
            "slot_id": sid,
            "locator_asset": slot.get("locator_asset"),
            "position": slot.get("position"),
            "survey_status": slot.get("survey_status"),
            "issues": [],
        }

        def unresolved(reason):
            rec["issues"].append(reason)
            rec["light_certifiable"] = False
            rec["usable_as_dark"] = False

        if slot.get("survey_status") != "walked":
            unresolved("walk not completed: missing measurement")
        asset = asset_by_id.get(slot.get("locator_asset"))
        if asset is None:
            unresolved("no rebuilt locator plaque bound to this slot")
            rec["physical_position"] = None
        else:
            rec["physical_position"] = str(asset.physical_position)
            sec, label = section_of(asset.physical_position)
            rec["physical_section"] = sec
            rec["revision_label"] = str(label) if label is not None else None
            if coverage and not covered_by(coverage, asset.physical_position):
                unresolved("locator's physical position is outside inspected coverage")
        rec["certified"] = not rec["issues"]
        # A missing measurement can never be signed as lit, and never as a
        # usable dark hiding slot either.
        rec.setdefault("light_certifiable", rec["certified"])
        rec.setdefault("usable_as_dark", rec["certified"])
        out[sid] = rec
    return out


# ---------------------------------------------------------------------------
# Puppets & grid shapes / occlusion
# ---------------------------------------------------------------------------


def grid_cells(puppet: dict) -> set[tuple[int, int]]:
    cells = set()
    for r, row in enumerate(puppet.get("shape", [])):
        for c, ch in enumerate(row):
            if ch and ch != ".":
                cells.add((r, c))
    return cells


def face_cell(puppet: dict) -> tuple[int, int]:
    r, c = puppet["face"]
    return int(r), int(c)


def visible_faces(env: dict, on_stage: dict, lit: dict) -> dict:
    """Which ``slot -> face`` are actually seen on the screen for one beat.

    The screen has a front row (OC1, nearest the light) and a back row (OC2).
    A lit, occupied slot projects its puppet's shape.  The front row covers
    the back row where the front puppet's shape includes the back puppet's
    *face cell* (puppet-local coordinates), so the back face is hidden.  A
    dark slot projects nothing.
    """
    oc1 = env["screen"]["oc1"]["slot"]
    oc2 = (env["screen"].get("oc2") or {}).get("slot")
    puppets = env["puppets"]

    def lit_at(sid):
        return sid is not None and lit.get(sid, {}).get("status") == "lit"

    front = on_stage.get(oc1) if oc1 is not None else None
    front_pup = puppets.get(front) if front is not None and lit_at(oc1) else None

    result = {}
    for sid in (oc1, oc2):
        if sid is None:
            continue
        pid = on_stage.get(sid)
        if pid is None or not lit_at(sid):
            continue
        puppet = puppets[pid]
        fr, fc = face_cell(puppet)
        if sid == oc2 and front_pup is not None:
            if (fr, fc) in grid_cells(front_pup):
                continue  # front row covers the back face cell
        result[sid] = puppet["face_label"]
    return result


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


def move_duration(env: dict, a: str, b: str) -> int:
    xa = env["slots"][a]["grid_x"]
    xb = env["slots"][b]["grid_x"]
    dist = grid_distance(xa, xb)
    if dist == 0:
        return 0
    rules = env["stage"].get("adjacent_move", [])
    for rule in rules:
        if int(rule["distance"]) == dist:
            return int(rule["beats"])
    return int(env["stage"].get("move_beats_per_cell", 1)) * dist


def turn_duration(env: dict, actor: str) -> int:
    turns = env["stage"].get("turn_beats", 0)
    if isinstance(turns, dict):
        return int(turns.get(actor, turns.get("*", 1)))
    return int(turns)


def _learned(machine: dict, actor: str) -> bool:
    return actor in set(machine.get("learned", []))


def _puppet_actor(env: dict, pid: str) -> str:
    return env["puppets"][pid]["actor"]


def compile_show(env: dict, schedule: dict, resume: dict | None = None) -> dict:
    """Validate the declared beat block and render what is on screen.

    ``resume`` optionally continues from an already-performed state: it may
    carry ``last`` (machine -> last performance record), ``performed_on``
    (machine -> beats performed) and ``prev_assign`` (machine -> last slot &
    puppet), so a shift handover starts from the real on-hand puppets and
    remaining beats instead of replaying from the top.

    Returns ``ok``, per-beat ``frames`` (lit/dark status, on-stage puppet and
    the visible face per slot) plus each frame's operator/handoff, the list of
    ``failures`` (each naming the beat it could not be joined at) and
    ``earliest_failure``.
    """
    failures: list[dict] = []

    def fail(beat, msg, **extra):
        rec = {"beat": beat, "error": msg}
        rec.update(extra)
        failures.append(rec)

    machine_ids = list(env["machines"])
    start = {mid: dict(m.get("start_holding") or {}) for mid, m in env["machines"].items()}
    standby = schedule.get("standby", {})     # puppet -> {machine, beat, slot?, hold?}
    rigs = schedule.get("rigs", {})           # machine -> rig record
    end_turn = schedule.get("end_turn")
    notices = schedule.get("notices", [])     # [{role, beat, available:[masters]}]
    resume = resume or {}

    lights = all_slot_lights(env)
    declared = {int(b["beat"]): b for b in schedule.get("beats", [])}
    beats = sorted(set(int(b["beat"]) for b in env["beats"]) | set(declared))
    if not beats:
        return {"ok": True, "frames": [], "failures": [],
                "earliest_failure": None, "final": {}}

    # Which master is the *originally bound* performer of each role/puppet, so
    # an absence notice can forbid them from appearing.  A puppet that has a
    # ``home_machine`` is bound to it; a standby can only be used while that
    # home master has not already performed (no switching in after they might
    # have been known to make it).
    home = {pid: p.get("home_machine") for pid, p in env["puppets"].items()}

    # Absent role -> beat: the bound master cannot appear at that beat.  A
    # switching decision for an absence notice may only be acted on once the
    # notice is known, so we also track the earliest beat a substitute may
    # legally first take over (the notice's own beat).
    absent = {}
    available_at = {}
    notice_known = {}      # role -> earliest beat substitutions are allowed
    for n in notices:
        b = int(n["beat"])
        absent[(n["role"], b)] = True
        available_at[b] = set(n.get("available", []))
        notice_known[n["role"]] = min(notice_known.get(n["role"], b), b)

    # Standby exclusivity windows: while a standby occupies, the home venue
    # may not borrow that substitute or that puppet on the immediately
    # following beat, so reserve them over [beat, beat+hold).
    standby_busy = {}  # (kind, id) -> set(beats)
    for pid, sb in standby.items():
        m = sb["machine"]
        b0 = int(sb["beat"])
        hold = int(sb.get("hold", 2))
        for u in range(b0, b0 + hold):
            standby_busy.setdefault(("machine", m), set()).add(u)
            standby_busy.setdefault(("puppet", pid), set()).add(u)

    last: dict[str, dict] = {k: dict(v) for k, v in (resume.get("last") or {}).items()}
    prev_assign: dict[str, dict] = {
        k: dict(v) for k, v in (resume.get("prev_assign") or {}).items()
    }
    frames = []
    performed_on: dict[str, set[int]] = {mid: set() for mid in machine_ids}
    for mid, bs in (resume.get("performed_on") or {}).items():
        performed_on[mid] = set(bs)
    rigs_used: dict[str, int] = {}

    for t in beats:
        decl = declared.get(t)
        perform = list(decl.get("perform", [])) if decl else []

        on_stage: dict[str, str] = {}
        used_slots: dict[str, str] = {}
        used_puppets: dict[str, str] = {}
        machine_seen: dict[str, int] = {}
        operator_of: dict[str, str] = {}
        rig_flips: list[dict] = []

        for item in perform:
            mid, pid, sid = item.get("machine"), item.get("puppet"), item.get("slot")
            if mid not in env["machines"]:
                fail(t, f"unknown master {mid!r}"); continue
            if pid not in env["puppets"]:
                fail(t, f"unknown puppet {pid!r}"); continue
            if sid not in env["slots"]:
                fail(t, f"unknown slot {sid!r}"); continue
            machine_seen[mid] = machine_seen.get(mid, 0) + 1
            if machine_seen[mid] > 1:
                fail(t, f"master {mid!r} holds two puppets in one beat")
            if sid in used_slots:
                fail(t, f"two masters share slot {sid!r}",
                     machines=[used_slots[sid], mid])
            used_slots[sid] = mid
            if pid in used_puppets:
                fail(t, f"puppet {pid!r} is used twice in one beat",
                     machines=[used_puppets[pid], mid])
            used_puppets[pid] = mid
            operator_of[sid] = mid
            on_stage[sid] = pid

        lit = {sid: lights[sid].get(t, {"status": "unresolved"}) for sid in env["slots"]}

        # -- per-machine continuity ----------------------------------
        for item in perform:
            mid, pid, sid = item["machine"], item["puppet"], item["slot"]
            if (mid not in env["machines"] or pid not in env["puppets"]
                    or sid not in env["slots"]):
                continue  # already reported in the membership pass above
            action = item.get("action", "hold")
            machine = env["machines"][mid]
            actor = _puppet_actor(env, pid)
            prev = last.get(mid)

            # Absence notice: the originally bound master of this role is
            # known gone at this beat; a declared plan using them is invalid.
            if absent.get((actor, t)) and home.get(pid) == mid:
                fail(t, f"role {actor!r}'s bound master {mid!r} is absent at beat {t}")
            # A notice that is not yet known may not be acted on early: a
            # substitute for this role cannot *appear* before the notice beat
            # (prebinding the puppet on a standby is fine -- lifting it early
            # is not, because the absence was not yet known).
            if (actor in notice_known and t < notice_known[actor]
                    and home.get(pid) != mid):
                fail(t, f"cannot switch to a substitute for role {actor!r} at beat "
                        f"{t}: the absence notice only arrives at beat "
                        f"{notice_known[actor]}")
            # Substitution may only use masters on hand for the notice.
            if (actor, t) in absent and available_at.get(t):
                if mid not in available_at[t]:
                    fail(t, f"master {mid!r} is not among the on-hand masters for "
                            f"the absent role {actor!r} at beat {t}")

            # Standby exclusivity: the home venue cannot borrow a substitute
            # or puppet that a standby is holding through this beat.  The
            # standby's *own* use inside its window is of course allowed.
            sb = standby.get(pid)
            is_own_standby = (
                sb is not None and sb.get("machine") == mid
                and int(sb.get("beat", 0)) <= t < int(sb.get("beat", 0)) + int(sb.get("hold", 2))
            )
            if not is_own_standby:
                if t in standby_busy.get(("machine", mid), ()):
                    fail(t, f"master {mid!r} is committed to a standby at beat {t}")
                if t in standby_busy.get(("puppet", pid), ()):
                    fail(t, f"puppet {pid!r} is committed to a standby at beat {t}")

            if prev is None:
                holding = pid in start[mid]
                grant = (pid in standby
                         and standby[pid].get("machine") == mid
                         and int(standby[pid]["beat"]) <= t)
                if not holding and not grant:
                    fail(t, f"master {mid!r} cannot be holding {pid!r} at beat {t}")
                if not _learned(machine, actor):
                    fail(t, f"master {mid!r} never learned role {actor!r}")
                if grant and not holding:
                    hm = home.get(pid)
                    sb = int(standby[pid]["beat"])
                    for u in range(sb, t):
                        if hm is not None and u in performed_on.get(hm, ()):
                            fail(t, f"standby for {pid!r} at beat {t} is too late: "
                                    f"home master {hm!r} already performed at beat {u}")
                            break
            else:
                pb, pp, ps, pf = prev["beat"], prev["puppet"], prev["slot"], prev["face"]
                gap = t - pb - 1  # empty beats for this master strictly between

                if action == "swap":
                    pass  # validated as a pair below
                elif action == "rigflip":
                    rig = rigs.get(mid)
                    if rig is None:
                        fail(t, f"master {mid!r} used a flip rig that was not reserved")
                    else:
                        if pid not in set(rig.get("puppets", [])):
                            fail(t, f"puppet {pid!r} was not preloaded on {mid!r}'s rig")
                        need = int(rig.get("load_beats", 1))
                        since = int(rig.get("since", pb))
                        if since + need > t:
                            fail(t, f"rig on {mid!r} cannot be loaded {need} beat(s) "
                                    f"before beat {t}")
                        occ = rig.get("occupy_slot")
                        if occ is not None:
                            if occ not in env["slots"]:
                                fail(t, f"rig occupies unknown slot {occ!r}")
                            elif env["slots"][occ].get("survey_status") != "walked":
                                fail(t, f"rig's extra slot {occ!r} was not measured")
                            elif occ in used_slots and used_slots[occ] != mid:
                                fail(t, f"rig's extra slot {occ!r} is occupied by "
                                        f"{used_slots[occ]!r}")
                        # The rig flips within one beat, so the outgoing puppet
                        # (``pp``) swings through the extra slot; its face is the
                        # "passing face" that must stay hidden along the seam.
                        rig_flips.append({
                            "machine": mid,
                            "old_face": env["puppets"][pp]["face_label"],
                            "occupy_slot": occ,
                            "perform_slot": sid,
                            "beat": t,
                            "passing_hidden": bool(rig.get("passing_hidden", False)),
                        })
                else:
                    if pp != pid:
                        if gap < 1:
                            fail(t, f"master {mid!r} rebinds {pp!r}->{pid!r} at beat "
                                    f"{t} without an empty beat")
                        if not _learned(machine, actor):
                            fail(t, f"master {mid!r} never learned role {actor!r}")
                    if sid != ps:
                        need = move_duration(env, ps, sid)
                        if gap < need:
                            fail(t, f"master {mid!r} cannot move {ps!r}->{sid!r} in "
                                    f"{gap} empty beat(s); needs {need}")
                    want_face = item.get("face", pf)
                    if pf != want_face:
                        need_t = turn_duration(env, actor)
                        if gap < need_t:
                            fail(t, f"master {mid!r} cannot turn to {want_face!r} in "
                                    f"{gap} empty beat(s); needs {need_t}")

            last[mid] = {
                "beat": t, "puppet": pid, "slot": sid,
                "face": item.get("face", env["puppets"][pid]["face_label"]),
            }

        # -- swap pairs ----------------------------------------------
        for item in (it for it in perform if it.get("action") == "swap"):
            mid = item["machine"]
            partner = item.get("swap_with")
            pitem = next((x for x in perform if x["machine"] == partner), None)
            a_prev = prev_assign.get(mid)
            b_prev = prev_assign.get(partner)
            if partner is None or pitem is None or pitem.get("action") != "swap":
                fail(t, f"swap by {mid!r} has no swapping partner at beat {t}")
                continue
            if a_prev is None or b_prev is None:
                fail(t, f"swap at beat {t} requires both masters to have performed "
                        f"the previous beat")
                continue
            if (item["slot"] != b_prev["slot"]
                    or pitem["slot"] != a_prev["slot"]
                    or item["puppet"] != b_prev["puppet"]
                    or pitem["puppet"] != a_prev["puppet"]):
                fail(t, f"swap at beat {t} did not exchange both slot+puppet as a pair")
            if grid_distance(env["slots"][item["slot"]]["grid_x"],
                             env["slots"][pitem["slot"]]["grid_x"]) != 1:
                fail(t, f"swap at beat {t} is only instantaneous between adjacent slots")
            for s in (item["slot"], pitem["slot"]):
                if lit.get(s, {}).get("status") != "lit":
                    fail(t, f"swap slot {s!r} is not lit at beat {t}")

        # -- scene requirements --------------------------------------
        scene = decl.get("scene") if decl else None
        vis = visible_faces(env, on_stage, lit)
        handoff = {}
        if scene:
            for req in scene.get("required", []):
                sid, want = req["slot"], req["face"]
                if lit.get(sid, {}).get("status") != "lit":
                    fail(t, f"required face {want!r} at {sid!r} cannot show: slot is "
                            f"{lit.get(sid, {}).get('status')}", light=lit.get(sid))
                    continue
                if sid not in on_stage:
                    fail(t, f"required face {want!r} at {sid!r}: no puppet staged")
                    continue
                show_pid = on_stage[sid]
                show_face = env["puppets"][show_pid]["face_label"]
                if show_face != want:
                    fail(t, f"required face {want!r} at {sid!r} but {show_face!r} is there")
                # A required *role* must be shown by its own real puppet, not
                # by some other puppet that happens to project the same face.
                if req.get("role") is not None:
                    if env["puppets"][show_pid]["actor"] != req["role"]:
                        fail(t, f"required role {req['role']!r} at {sid!r} is being "
                                f"shown by puppet {show_pid!r} of another role")
                if vis.get(sid) != want:
                    fail(t, f"required face {want!r} at {sid!r} is occluded/hidden")
            for hid in scene.get("hidden", []):
                if isinstance(hid, str):
                    if hid in vis:
                        fail(t, f"slot {hid!r} must show nothing but shows {vis[hid]!r}")
                    continue
                sid, face = hid["slot"], hid.get("face")
                if face is None:
                    if sid in vis:
                        fail(t, f"slot {sid!r} must be hidden but shows {vis[sid]!r}")
                elif vis.get(sid) == face:
                    fail(t, f"face {face!r} must stay hidden at {sid!r} but is seen")
            for req in scene.get("forbid_passing", []):
                bad = req["face"]
                for flip in rig_flips:
                    if flip["old_face"] != bad:
                        continue
                    occ = flip["occupy_slot"]
                    if occ is None:
                        continue
                    # The swing-through is only exposed if the extra slot is lit
                    # *and* the operator did not keep the old face turned away
                    # through the seam.  Keeping it hidden means the flip is
                    # signable even though the slot can be lit.
                    if (lit.get(occ, {}).get("status") == "lit"
                            and not flip["passing_hidden"]):
                        fail(t, f"flip at beat {t} leaks forbidden passing face "
                                f"{bad!r} through lit slot {occ!r}",
                             passing_slot=occ)
            # A handoff: a role takes over the previous beat's other role's
            # face-on-screen grid cell.
            for h in scene.get("handoff", []):
                from_slot, to_slot = h["from_slot"], h["to_slot"]
                prev_frame = frames[-1] if frames else None
                if prev_frame is None:
                    fail(t, f"handoff at beat {t} has no previous beat")
                    continue
                prev_vis = prev_frame["visible"]
                if prev_vis.get(from_slot) != h["face"]:
                    fail(t, f"handoff at beat {t}: previous face at {from_slot!r} "
                            f"was {prev_vis.get(from_slot)!r}, not {h['face']!r}")
                handoff[to_slot] = h["face"]

        frames.append({
            "beat": t,
            "on_stage": dict(on_stage),
            "operator": dict(operator_of),
            "lit": {s: lit[s]["status"] for s in env["slots"]},
            "visible": vis,
            "handoff": handoff,
        })

        for item in perform:
            performed_on[item["machine"]].add(t)
            if item.get("action") == "rigflip":
                rigs_used[item["machine"]] = rigs_used.get(item["machine"], 0) + 1
        prev_assign = {
            it["machine"]: {"slot": it["slot"], "puppet": it["puppet"]}
            for it in perform
        }

    # -- end-of-show turn (after the final beat) ------------------------
    final = {}
    if end_turn:
        final = {
            "machine": end_turn["machine"],
            "slot": end_turn.get("slot"),
            "ends_on_face": end_turn.get("face"),
            "note": "turned after the final beat; no earlier scene is affected",
        }

    failures.sort(key=lambda r: r["beat"])
    return {
        "ok": not failures,
        "frames": frames,
        "failures": failures,
        "earliest_failure": failures[0]["beat"] if failures else None,
        "final": final,
        "rigs_used": rigs_used,
    }


# ---------------------------------------------------------------------------
# Compact plan -> schedule helpers used by the demo / scenarios
# ---------------------------------------------------------------------------


def build_schedule(plan: dict) -> dict:
    """Turn a compact per-beat plan into the ``schedule`` compile_show wants.

    ``plan['beats']`` maps beat -> list of entries ``[machine, puppet, slot]``
    optionally followed by an action string and an extras dict.  Other keys
    (``standby``, ``rigs``, ``end_turn``, ``scenes``) pass through.
    """
    beats = []
    for t in sorted(plan["beats"]):
        items = []
        for entry in plan["beats"][t]:
            item = {"machine": entry[0], "puppet": entry[1], "slot": entry[2]}
            if len(entry) > 3 and entry[3]:
                item["action"] = entry[3]
            if len(entry) > 4 and entry[4]:
                item.update(entry[4])
            items.append(item)
        beat = {"beat": t, "perform": items}
        if plan.get("scenes", {}).get(t) is not None:
            beat["scene"] = plan["scenes"][t]
        beats.append(beat)
    return {
        "beats": beats,
        "standby": plan.get("standby", {}),
        "rigs": plan.get("rigs", {}),
        "end_turn": plan.get("end_turn"),
        "notices": plan.get("notices", []),
    }


def run_plan(env: dict, plan: dict, resume: dict | None = None) -> dict:
    return compile_show(env, build_schedule(plan), resume=resume)


# ---------------------------------------------------------------------------
# Stage limits
# ---------------------------------------------------------------------------


LIMITS = {
    "machines": 4,
    "puppets": 5,
    "beats": 6,
    "slots": 4,
    "grid_rows": 3,
    "grid_cols": 3,
}


def check_limits(env: dict) -> list[str]:
    """Return a list of violated stage-size limits (empty when within bounds)."""
    problems = []
    if len(env.get("machines", {})) > LIMITS["machines"]:
        problems.append(f"more than {LIMITS['machines']} masters")
    if len(env.get("puppets", {})) > LIMITS["puppets"]:
        problems.append(f"more than {LIMITS['puppets']} puppets")
    if len(env.get("beats", [])) > LIMITS["beats"]:
        problems.append(f"more than {LIMITS['beats']} beats")
    if len(env.get("slots", {})) > LIMITS["slots"]:
        problems.append(f"more than {LIMITS['slots']} slots")
    for pid, pup in env.get("puppets", {}).items():
        shape = pup.get("shape", [])
        if len(shape) > LIMITS["grid_rows"]:
            problems.append(f"puppet {pid!r} shape taller than {LIMITS['grid_rows']}")
        for row in shape:
            if len(row) > LIMITS["grid_cols"]:
                problems.append(f"puppet {pid!r} shape wider than {LIMITS['grid_cols']}")
                break
    return problems


# ---------------------------------------------------------------------------
# Public light query: "which slots are lit each beat, and why"
# ---------------------------------------------------------------------------


def light_report(env: dict) -> dict:
    """Per-beat, per-slot light status with the blocking-board basis.

    This is the query a stage manager needs *before* committing a script: it
    names, for every dark slot, the exact nearest board and the inequality
    that proves it, and marks any slot that cannot be certified because a walk
    or a board measurement is missing.
    """
    cert = env.get("certification") or {}
    out = {"beats": {}}
    for beat in env["beats"]:
        t = int(beat["beat"])
        row = {"light": beat["light"], "slots": {}}
        for sid, slot in env["slots"].items():
            info = slot_lighting(slot, beat["light"], env.get("panels", []))
            c = cert.get(sid, {})
            if info["status"] == "unresolved":
                info["certifiable"] = False
            elif c and not c.get("certified", True):
                # slot identified by an un-walked / out-of-coverage locator
                info = {
                    "status": "unresolved",
                    "reason": "slot locator not certified: "
                              + "; ".join(c.get("issues", [])),
                    "blocker": None, "candidates": [], "certifiable": False,
                }
            row["slots"][sid] = info
        out["beats"][t] = row
    return out

