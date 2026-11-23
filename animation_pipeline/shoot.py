"""补拍单：把场记读法兑换成“指定机位的曝光”。

摄影棚提供两套**实拍工位**，任选其一补拍；两套都遵守同一批硬性规矩：

* **纸封（只能摆一次）**：某些姿势转换 ``breaks_seal=True``，一执行就把纸封
  拍掉，该实偶自此锁死在这一姿势，**不能倒回、不能换**。
* **同拍**：两台相机从不同面同时曝光，必须照到**同一只实偶、当时的同一个
  姿势**。这只有双面同拍框做得到；单面转盘先天做不到。
* **正反各有服饰 = 两次曝光**：同一纸样 ``front_costume`` 与 ``back_costume``
  都非空时，正、背两面各记一条曝光。
* **无可换约束**：``allow_substitute=False`` 的格只认绑定实偶；为真时才可另借
  同款实偶分别拍不同画格。
* **拒收要追根**：未交的曝光必须追到**具体纸封**或**被占的相机/实偶**。

工位一 ``double_frame``（双面同拍框）：
同一只实偶一次挂进双面框，前后两台固定机位在相同时点同拍、共用已消耗的纸封；
框内装牢后不许中途换同款偶，**换框占一格**。机位是固定朝向的：
``Camera.side`` 表明它拍的偶面（前机位/背机位）。

工位二 ``single_turntable``（单面转盘）：
每台相机自带单面转盘，可在现场挑面（前或背），但**转一次占一格**；
正背两面只能先后出片，**绝不允许**把不同拍的两张接成“同拍对”。
机位``Camera.side``可为 ``None``（纯转盘机位）。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .puppets import Pattern, PuppetReading

SIDE_FRONT = "front"
SIDE_BACK = "back"
SIDES = (SIDE_FRONT, SIDE_BACK)

STATION_SINGLE_TURNTABLE = "single_turntable"
STATION_DOUBLE_FRAME = "double_frame"
STATIONS = (STATION_SINGLE_TURNTABLE, STATION_DOUBLE_FRAME)


# --------------------------------------------------------------------------- #
# 输入：机位、实偶、工单
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Camera:
    """一台相机（机位）。

    ``side`` 为固定朝向（前机位/背机位，双面框用）；为 ``None`` 表示该机位
    只带单面转盘，现场挑面（单面转盘用）。
    """

    name: str
    side: str | None = None

    def __post_init__(self) -> None:
        if self.side is not None and self.side not in SIDES:
            raise ValueError(f"相机 {self.name} 的机位面非法：{self.side}")


@dataclass(frozen=True)
class RealPuppet:
    """一只实偶。

    * ``pose``：当前姿势（纸样名；``None`` 表示空白）；
    * ``sealed``：纸封是否已被拍掉（已锁死，只剩当前姿势可拍）；
    * ``transitions``：``(src, dst, breaks_seal)`` —— 可执行的姿势转换，
      ``breaks_seal=True`` 表示这一转换要拍掉纸封、不可倒回。
    """

    name: str
    model: str
    pose: str | None = None
    sealed: bool = False
    transitions: tuple[tuple[str | None, str | None, bool], ...] = ()

    def reachable_poses(self) -> dict[str | None, bool]:
        """可达姿势 -> 是否必须拍掉纸封。锁死时只剩当前姿势。"""
        return _State((self,)).reachable(self.name)


@dataclass(frozen=True)
class FrameJob:
    """工单里的一格。

    ``character`` 是场记读法里的偶名（也用来挑同款实偶）；``pose`` 是要穿的
    纸样名；``cameras`` 是本格要补的机位；``simultaneous`` 要求这些机位在同一
    时点取前后两面（只有双面框做得到）。
    """

    frame_id: int
    time_ms: int
    character: str
    pose: str
    cameras: tuple[str, ...]
    simultaneous: bool = False
    allow_substitute: bool = False


@dataclass(frozen=True)
class Worksheet:
    jobs: tuple[FrameJob, ...]
    cameras: dict[str, Camera]
    puppets: tuple[RealPuppet, ...]
    patterns: dict[str, Pattern]
    # 偶名 -> 绑定的实偶名；无替代时只认这一只。
    binding: dict[str, str] = field(default_factory=dict)
    # (frame_id, 偶名) -> 场记读法；提供后，非确定/合并/姿势不符的格一律拒收。
    readings: dict[tuple[int, str], PuppetReading] = field(default_factory=dict)

    def camera(self, name: str) -> Camera:
        if name not in self.cameras:
            raise KeyError(f"工单引用了未登记的机位 {name!r}")
        return self.cameras[name]

    def pattern(self, name: str) -> Pattern:
        if name not in self.patterns:
            raise KeyError(f"工单引用了未登记的纸样 {name!r}")
        return self.patterns[name]


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Slot:
    """一格实拍动作（换框 / 转盘 / 拍封 / 曝光）。顺序即工作顺序。"""

    index: int
    kind: str  # remount / rotate / seal / take / expose
    detail: str
    puppet: str | None = None
    camera: str | None = None


@dataclass(frozen=True)
class Exposure:
    exposure_id: str
    frame_id: int
    camera: str
    side: str
    costume: str
    pose: str
    puppet: str
    slot_index: int
    group: str | None  # 同拍组号：同格、同偶、同姿势的双面曝光共用一个组号


@dataclass(frozen=True)
class Undelivered:
    frame_id: int
    cameras: tuple[str, ...]
    reason: str
    trace: str


@dataclass
class ShootPlan:
    station: str
    slots: list[Slot] = field(default_factory=list)
    exposures: list[Exposure] = field(default_factory=list)
    undelivered: list[Undelivered] = field(default_factory=list)

    @property
    def slot_count(self) -> int:
        return len(self.slots)

    @property
    def delivery_order(self) -> list[str]:
        """依原片顺序可装回的曝光编号。"""
        return [e.exposure_id for e in self.exposures]

    def exposures_for(self, frame_id: int) -> list[Exposure]:
        return [e for e in self.exposures if e.frame_id == frame_id]

    def to_report(self) -> dict:
        return {
            "station": self.station,
            "slot_count": self.slot_count,
            "delivery_order": self.delivery_order,
            "slots": [
                {"index": s.index, "kind": s.kind, "detail": s.detail,
                 "puppet": s.puppet, "camera": s.camera}
                for s in self.slots
            ],
            "exposures": [
                {"exposure_id": e.exposure_id, "frame_id": e.frame_id,
                 "camera": e.camera, "side": e.side, "costume": e.costume,
                 "pose": e.pose, "puppet": e.puppet, "slot_index": e.slot_index,
                 "group": e.group}
                for e in self.exposures
            ],
            "undelivered": [
                {"frame_id": u.frame_id, "cameras": list(u.cameras),
                 "reason": u.reason, "trace": u.trace}
                for u in self.undelivered
            ],
        }


# --------------------------------------------------------------------------- #
# 实偶状态机（调度期可变状态 + 纸封溯源）
# --------------------------------------------------------------------------- #

class _State:
    def __init__(self, puppets: tuple[RealPuppet, ...]) -> None:
        self.pose: dict[str, str | None] = {p.name: p.pose for p in puppets}
        self.sealed: dict[str, bool] = {p.name: p.sealed for p in puppets}
        self.trans = {p.name: p.transitions for p in puppets}
        self.model = {p.name: p.model for p in puppets}
        self.order = [p.name for p in puppets]
        self.seal_origin: dict[str, str] = {}  # 实偶 -> 在哪一格拍的封

    def reachable(self, name: str) -> dict[str | None, bool]:
        """可达姿势 -> 是否必须拍掉纸封。"""
        if self.sealed[name]:
            return {self.pose[name]: False} if self.pose[name] is not None else {}
        rev: dict[str | None, list[str | None]] = defaultdict(list)
        seal: dict[str | None, list[str | None]] = defaultdict(list)
        for src, dst, breaks in self.trans[name]:
            (seal if breaks else rev)[src].append(dst)
        seen: set[str | None] = {self.pose[name]}
        stack: list[str | None] = [self.pose[name]]
        while stack:
            cur = stack.pop()
            for nxt in rev[cur]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        result: dict[str | None, bool] = {p: False for p in seen if p is not None}
        for src in seen:
            for dst in seal[src]:
                if dst is not None:
                    result.setdefault(dst, True)
        return result

    def apply_pose(self, name: str, target: str, origin: str) -> bool:
        """把实偶转到目标姿势；返回是否因此拍掉纸封（拍掉即锁死）。"""
        if self.pose[name] == target:
            return False
        need_seal = self.reachable(name).get(target)
        if need_seal is None:
            raise KeyError(f"实偶 {name} 不能从 {self.pose[name]!r} 转到 {target!r}")
        self.pose[name] = target
        if need_seal:
            self.sealed[name] = True
            self.seal_origin.setdefault(name, origin)
        return bool(need_seal)


# --------------------------------------------------------------------------- #
# 规划器
# --------------------------------------------------------------------------- #

class _Planner:
    def __init__(self, ws: Worksheet, station: str) -> None:
        if station not in STATIONS:
            raise ValueError(f"未知工位 {station!r}")
        self.ws = ws
        self.station = station
        self.state = _State(ws.puppets)
        self.plan = ShootPlan(station=station)
        self._slot_i = 0
        self._exp_i = 0
        self._mounted: str | None = None
        self._presented: dict[str, str | None] = {c: ws.cameras[c].side
                                                  for c in ws.cameras}
        # 同一播放时点被占用的相机与实偶。
        self._busy_cams: dict[int, dict[str, int]] = defaultdict(dict)
        self._busy_puppets: dict[int, dict[str, int]] = defaultdict(dict)

    # -- 小工具 ---------------------------------------------------------- #
    def _slot(self, kind: str, detail: str, puppet=None, camera=None) -> int:
        idx = self._slot_i
        self._slot_i += 1
        self.plan.slots.append(Slot(idx, kind, detail, puppet, camera))
        return idx

    def _exp_id(self) -> str:
        self._exp_i += 1
        return f"E{self._exp_i:03d}"

    def _candidates(self, job: FrameJob) -> list[str]:
        bound = self.ws.binding.get(job.character)
        same_model = [n for n in self.state.order if self.state.model[n] == job.character]
        if job.allow_substitute:
            return same_model
        if bound is None or bound not in same_model:
            return []
        return [bound]

    def _rank(self, name: str, target: str, character: str) -> tuple:
        needs_seal = self.state.reachable(name).get(target)
        at_target = self.state.pose[name] == target
        bounded = self.ws.binding.get(character) == name
        # 少拍封 > 已在位 > 有绑定 > 名字稳定
        return (0 if needs_seal is False else 1,
                0 if at_target else 1,
                0 if bounded else 1,
                self.state.order.index(name))

    def _pick(self, job: FrameJob) -> str | None:
        cands = [n for n in self._candidates(job) if job.pose in self.state.reachable(n)]
        if not cands:
            return None
        return min(cands, key=lambda n: self._rank(n, job.pose, job.character))

    def _no_puppet_reason(self, job: FrameJob) -> tuple[str, str]:
        bound = self.ws.binding.get(job.character)
        cands = self._candidates(job)
        if not cands:
            return (
                "同款实偶不足",
                f"格（frame {job.frame_id}）不允许换偶，但绑定实偶 {bound!r} 不存在或不同款；"
                f"只有可换一只同款偶才可能补拍",
            )
        details = []
        for n in cands:
            if self.state.sealed[n]:
                origin = self.state.seal_origin.get(n, "更早的格")
                details.append(
                    f"实偶 {n} 已在 {origin} 被纸封锁死在姿势 {self.state.pose[n]!r}，"
                    f"不能再转到 {job.pose!r}"
                )
            else:
                details.append(
                    f"实偶 {n} 当前 {self.state.pose[n]!r}，可执行转换里没有到 {job.pose!r} 的路"
                )
        return ("无实偶可达目标姿势", "；".join(details))

    def _reject(self, job: FrameJob, reason: str, trace: str) -> None:
        self.plan.undelivered.append(
            Undelivered(job.frame_id, job.cameras, reason, trace)
        )

    def _reading_gate(self, job: FrameJob) -> bool:
        """若提供了场记读法，只有确定且吻合的读法才能被签成曝光。"""
        key = (job.frame_id, job.character)
        if key not in self.ws.readings:
            return True
        reading = self.ws.readings[key]
        if reading.pose is not None and reading.pose.name == job.pose:
            return True
        self._reject(
            job,
            "场记读法不确定或与工单不符",
            f"格（frame {job.frame_id}）偶 {job.character}：{reading.review_basis}；"
            f"工单要求纸样 {job.pose!r}，不能据此签曝光",
        )
        return False

    def _emit(self, job: FrameJob, puppet: str, camera: str, side: str,
              costume: str, slot_index: int, group: str | None) -> None:
        self.plan.exposures.append(
            Exposure(
                exposure_id=self._exp_id(),
                frame_id=job.frame_id,
                camera=camera,
                side=side,
                costume=costume,
                pose=job.pose,
                puppet=puppet,
                slot_index=slot_index,
                group=group,
            )
        )

    # -- 主循环 ---------------------------------------------------------- #
    def build(self) -> ShootPlan:
        for job in self.ws.jobs:
            if not self._reading_gate(job):
                continue
            if self.station == STATION_DOUBLE_FRAME:
                self._job_double_frame(job)
            else:
                self._job_turntable(job)
        return self.plan

    # -- 工位一：双面同拍框 --------------------------------------------- #
    def _job_double_frame(self, job: FrameJob) -> None:
        clash = [c for c in job.cameras if c in self._busy_cams.get(job.time_ms, {})]
        if clash:
            occupant = self._busy_cams[job.time_ms][clash[0]]
            self._reject(
                job,
                "相机在同时点被占",
                f"格（frame {job.frame_id}）需机位 {clash[0]}，但同一播放时点 "
                f"{job.time_ms}ms 已被 frame {occupant} 的同拍占住，无法两台同时用同一机位",
            )
            return

        puppet = self._pick(job)
        if puppet is None:
            reason, trace = self._no_puppet_reason(job)
            self._reject(job, reason, trace)
            return

        if puppet in self._busy_puppets.get(job.time_ms, {}):
            occupant = self._busy_puppets[job.time_ms][puppet]
            self._reject(
                job,
                "同拍两机抢实偶",
                f"格（frame {job.frame_id}）只能借到实偶 {puppet}，但同一播放时点 "
                f"{job.time_ms}ms 它已被 frame {occupant} 的双面同拍占用；"
                f"一只实偶无法同时挂进两个框",
            )
            return

        pat = self.ws.pattern(job.pose)
        # 固定机位必须各有对应面的服饰，否则整格拒收。
        for cam_name in job.cameras:
            side = self.ws.camera(cam_name).side
            if side is None:
                self._reject(
                    job,
                    "双面框机位未定面",
                    f"机位 {cam_name} 未声明固定朝向，无法作双面同拍",
                )
                return
            if pat.costume_for(side) is None:
                self._reject(
                    job,
                    "该面无所求服饰",
                    f"格（frame {job.frame_id}）机位 {cam_name} 必须看偶的 {side} 面，"
                    f"但纸样 {job.pose!r} 的 {side}_costume 为空；两面并非都有服饰，整格拒收",
                )
                return

        # 换框占一格；同拍把前后两机一次打齐、共用纸封。
        if self._mounted != puppet:
            self._slot("remount", f"换框挂上实偶 {puppet}", puppet=puppet)
            self._mounted = puppet
        if self.state.apply_pose(puppet, job.pose, f"frame {job.frame_id}"):
            self._slot("seal", f"实偶 {puppet} 拍掉纸封、锁死在 {job.pose}", puppet=puppet)
        take = self._slot(
            "take",
            f"双面框同拍 frame {job.frame_id}：机位 {','.join(job.cameras)}",
            puppet=puppet,
        )
        sides = [self.ws.camera(c).side for c in job.cameras]
        group = (
            f"G{job.frame_id}"
            if (job.simultaneous and len(job.cameras) >= 2
                and set(sides) == {SIDE_FRONT, SIDE_BACK})
            else None
        )
        for cam_name in job.cameras:
            side = self.ws.camera(cam_name).side
            self._emit(job, puppet, cam_name, side, pat.costume_for(side), take, group)
        for cam in job.cameras:
            self._busy_cams[job.time_ms][cam] = job.frame_id
        self._busy_puppets[job.time_ms][puppet] = job.frame_id

    # -- 工位二：单面转盘 ----------------------------------------------- #
    def _required_sides(self, job: FrameJob, pat: Pattern) -> list[str]:
        """本格要出几面：工单若点名固定机位就按机位；否则按纸样两面服饰。"""
        fixed = [(self.ws.camera(c).side) for c in job.cameras
                 if self.ws.camera(c).side is not None]
        if fixed:
            sides = [s for s in SIDES if s in fixed]
        else:
            sides = [s for s in SIDES if pat.costume_for(s) is not None]
        return sides

    def _job_turntable(self, job: FrameJob) -> None:
        if job.simultaneous and len(job.cameras) >= 2:
            self._reject(
                job,
                "转盘无法同拍",
                f"格（frame {job.frame_id}）要求机位 {','.join(job.cameras)} 同时取得前后两面；"
                f"单面转盘只能先后出片，转一次还占一格，规则禁止把不同拍的两张接成同拍对",
            )
            return

        candidates = self._candidates(job)
        if not candidates:
            reason, trace = self._no_puppet_reason(job)
            self._reject(job, reason, trace)
            return

        pat = self.ws.pattern(job.pose)
        given_sides = self._required_sides(job, pat)
        if not given_sides:
            self._reject(
                job,
                "该面无所求服饰",
                f"纸样 {job.pose!r} 正背两面均无服饰，或工单点名的机位面在该纸样上无服饰",
            )
            return
        for side in given_sides:
            if pat.costume_for(side) is None:
                self._reject(
                    job,
                    "该面无所求服饰",
                    f"格（frame {job.frame_id}）须出 {side} 面，但纸样 {job.pose!r} 的 "
                    f"{side}_costume 为空，整格拒收",
                )
                return

        fixed_puppet = None if job.allow_substitute else candidates[0]
        for side in given_sides:
            pool = [fixed_puppet] if fixed_puppet is not None else candidates
            usable = [n for n in pool if job.pose in self.state.reachable(n)]
            if not usable:
                reason, trace = self._no_puppet_reason(job)
                self._reject(job, reason, trace)
                return
            puppet = min(usable, key=lambda n: self._rank(n, job.pose, job.character))
            if self.state.apply_pose(puppet, job.pose, f"frame {job.frame_id}"):
                self._slot("seal", f"实偶 {puppet} 拍掉纸封、锁死在 {job.pose}",
                           puppet=puppet)
            cam_name = self._turntable_camera(job, side)
            if self._presented.get(cam_name) not in (None, side):
                self._slot("rotate", f"转盘把机位 {cam_name} 转向 {side} 面",
                           camera=cam_name)
            self._presented[cam_name] = side
            slot = self._slot(
                "expose",
                f"单面曝光 frame {job.frame_id}：{cam_name}/{side}",
                puppet=puppet,
                camera=cam_name,
            )
            # 单面转盘即使一格要两面，也各拍各的、绝不发同拍组号。
            self._emit(job, puppet, cam_name, side, pat.costume_for(side), slot, None)

    def _turntable_camera(self, job: FrameJob, side: str) -> str:
        for c in job.cameras:
            if self.ws.camera(c).side == side:
                return c
        for c in job.cameras:
            if self.ws.camera(c).side is None:
                return c
        return job.cameras[0]


def plan_shoot(ws: Worksheet, station: str) -> ShootPlan:
    """在指定工位上把工单排成补拍单。"""
    return _Planner(ws, station).build()


# --------------------------------------------------------------------------- #
# 核对：补拍单必须自洽
# --------------------------------------------------------------------------- #

def verify_plan(plan: ShootPlan, ws: Worksheet) -> list[str]:
    """重放补拍单，返回违规描述列表（空表示自洽）。"""
    problems: list[str] = []

    last_frame = -1
    last_id = 0
    for e in plan.exposures:
        num = int(e.exposure_id[1:])
        if num <= last_id:
            problems.append(f"曝光编号 {e.exposure_id} 未按片序递增")
        last_id = num
        if e.frame_id < last_frame:
            problems.append(f"曝光 {e.exposure_id} 的 frame {e.frame_id} 早于前一张，片序错乱")
        last_frame = max(last_frame, e.frame_id)

    for e in plan.exposures:
        pat = ws.patterns[e.pose]
        if pat.costume_for(e.side) != e.costume:
            problems.append(f"曝光 {e.exposure_id} 服饰与纸样 {e.pose} 的 {e.side} 面不符")
        if e.costume is None:
            problems.append(f"曝光 {e.exposure_id} 在无服饰的 {e.side} 面交付")

    groups: dict[str, list[Exposure]] = defaultdict(list)
    for e in plan.exposures:
        if e.group is not None:
            groups[e.group].append(e)
    for gid, members in groups.items():
        if len(members) < 2:
            problems.append(f"同拍组 {gid} 少于两张曝光")
        if len({m.puppet for m in members}) != 1:
            problems.append(f"同拍组 {gid} 的两面不是同一只实偶")
        if len({m.pose for m in members}) != 1:
            problems.append(f"同拍组 {gid} 的两面不是同一个姿势")
        if len({m.slot_index for m in members}) != 1:
            problems.append(f"同拍组 {gid} 的两面不在同一拍")
        if len(members) == 2 and {m.side for m in members} != {SIDE_FRONT, SIDE_BACK}:
            problems.append(f"同拍组 {gid} 的两面不是正+背各一张")

    # 单面转盘不得出现同拍组（那是跨拍拼接）。
    if plan.station == STATION_SINGLE_TURNTABLE and groups:
        problems.append("单面转盘出现同拍组：把不同拍的两张当成了同拍对")

    # 纸封不可倒回：按拍序重放实偶姿势，封后不得再变。
    by_slot = sorted(plan.exposures, key=lambda e: (e.slot_index, e.exposure_id))
    state = _State(ws.puppets)
    sealed_at: dict[str, str] = {}
    for e in by_slot:
        if e.puppet in sealed_at:
            if state.pose[e.puppet] != e.pose:
                problems.append(
                    f"实偶 {e.puppet} 在 {sealed_at[e.puppet]} 后已封，"
                    f"却又要换到 {e.pose}（纸封倒回）"
                )
            continue
        try:
            state.apply_pose(e.puppet, e.pose, e.exposure_id)
        except KeyError as exc:
            problems.append(str(exc))
            continue
        if state.sealed[e.puppet]:
            sealed_at[e.puppet] = e.exposure_id
    return problems
