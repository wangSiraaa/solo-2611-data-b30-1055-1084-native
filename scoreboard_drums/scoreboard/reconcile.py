"""交班核对：把电工的证据按先后核成联合止位。

核对是构造性的：先给每条“不确定”的证据（失回执脉冲、总闸失电脉冲组）
枚举真/假两种真相，再按 seq 顺序在两只鼓上逐条模拟，并用触片读数过滤。
活下来的真相各自给出一个联合止位：

- 有活真相 -> feasible=True，stops 为全部可能止位，每个止位附可独立回放的
  见证（哪些失回执脉冲实际走了、逐拍时间线）。
- 无活真相 -> feasible=False，stops 为空，conflict 为一个不可同时成立的
  最小记录子集（逐条贪心剔除得到的不可满足核），并说明矛盾点。

同一总闸失电组用同一个布尔变量，保证要么全走、要么全丢。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product

from .core import (
    DRUMS,
    Contact,
    Pulse,
    PulseStatus,
    Record,
    Seen,
    Stop,
    advance,
)


@dataclass(frozen=True)
class TimelineEntry:
    """一次真相模拟中的逐拍现场条目，供独立回放。"""

    seq: int
    record_type: str
    drum: str | None
    detail: str
    digits_after: dict[str, int]


@dataclass(frozen=True)
class Witness:
    """某个止位的完整证据解释：不确定脉冲的取值 + 逐拍时间线。"""

    stop: Stop
    # 记录 seq -> 该失回执脉冲在这条真相里是否真的走了
    pulse_truth: dict[int, bool]
    # 总闸失电组 -> 全走(True)/全丢(False)
    group_truth: dict[str, bool]
    timeline: tuple[TimelineEntry, ...]

    def replay_log(self) -> list[dict]:
        return [
            {
                "seq": e.seq,
                "record_type": e.record_type,
                "drum": e.drum,
                "detail": e.detail,
                "digits_after": dict(e.digits_after),
            }
            for e in self.timeline
        ]


@dataclass(frozen=True)
class ReconcileResult:
    feasible: bool
    stops: tuple[Stop, ...]
    witnesses: tuple[Witness, ...]
    # 矛盾时：不能同时成立的最小记录子集（按 seq 排序）
    conflict: tuple[Record, ...] = field(default_factory=tuple)
    conflict_reason: str | None = None

    def stop_keys(self) -> list[tuple[int, int]]:
        return [s.key() for s in self.stops]

    def witness_for(self, stop: Stop) -> Witness:
        for w in self.witnesses:
            if w.stop == stop:
                return w
        raise KeyError(f"止位 {stop.key()} 不在核对结果内")


def reconcile(records: list[Record]) -> ReconcileResult:
    """核对交班记录，返回全部可能联合止位或一个矛盾核。"""
    records = sorted(records, key=lambda r: r.seq)
    _validate_shape(records)

    # 每个失回执脉冲占一个布尔变量；同组共用一个变量（原子全走/全丢）。
    pulse_records = [r for r in records if isinstance(r, Pulse)]
    group_of: dict[int, str] = {}
    group_variables: dict[str, list[int]] = {}
    solo_variables: list[int] = []
    for p in pulse_records:
        if p.ack is PulseStatus.ACK:
            continue
        if p.group is not None:
            group_of[p.seq] = p.group
            group_variables.setdefault(p.group, []).append(p.seq)
        else:
            solo_variables.append(p.seq)
    variable_names = solo_variables + sorted(group_variables)

    witnesses: list[Witness] = []
    seen_stop_keys: set[tuple[int, int]] = set()

    for assignment in product((False, True), repeat=len(variable_names)):
        truth = dict(zip(variable_names, assignment))
        witness = _simulate(records, truth, group_of)
        if witness is None:
            continue
        key = witness.stop.key()
        if key not in seen_stop_keys:
            seen_stop_keys.add(key)
            witnesses.append(witness)

    if witnesses:
        stops = tuple(sorted((w.stop for w in witnesses), key=lambda s: s.key()))
        ordered = [next(w for w in witnesses if w.stop == s) for s in stops]
        return ReconcileResult(feasible=True, stops=stops, witnesses=tuple(ordered))

    conflict, reason = _unsat_core(records, variable_names, group_of)
    return ReconcileResult(
        feasible=False, stops=(), witnesses=(), conflict=tuple(conflict),
        conflict_reason=reason,
    )


def _simulate(
    records: list[Record],
    truth: dict,
    group_of: dict[int, str],
) -> Witness | None:
    """按一条完整真相赋值逐条模拟；任一触片证伪则返回 None。"""
    digits = {drum: None for drum in DRUMS}
    timeline: list[TimelineEntry] = []
    pulse_truth: dict[int, bool] = {}
    group_truth: dict[str, bool] = {g: bool(v) for g, v in truth.items()
                                    if g in set(group_of.values())}

    for r in records:
        if isinstance(r, Seen):
            digits[r.drum] = r.digit
            timeline.append(TimelineEntry(
                r.seq, "seen", r.drum, f"亲眼见到鼓面={r.digit}", dict(digits)))
        elif isinstance(r, Pulse):
            if r.ack is PulseStatus.ACK:
                moved = True
                detail = "确认脉冲，前进一格"
            else:
                if r.group is not None:
                    moved = bool(truth[r.group])
                    detail = (f"总闸失电组 {r.group} "
                              f"{'全走' if moved else '全丢'}")
                else:
                    moved = bool(truth[r.seq])
                    detail = "失回执脉冲" + ("实际走了一格" if moved else "实际未走")
                pulse_truth[r.seq] = moved
            if moved:
                if digits[r.drum] is None:
                    # 子集核对时可能缺 Seen：该子集本身无法定位，视为不可满足
                    raise ValueError(f"{r.drum} 鼓缺少定位鼓面")
                digits[r.drum] = advance(digits[r.drum])
            timeline.append(TimelineEntry(
                r.seq, "pulse", r.drum, detail, dict(digits)))
        elif isinstance(r, Contact):
            current = digits[r.drum]
            if current is None:
                raise ValueError(f"{r.drum} 鼓在触片读数前缺少定位鼓面")
            inside = current in r.registered
            if inside != r.reading:
                return None
            timeline.append(TimelineEntry(
                r.seq, "contact", r.drum,
                f"触片读数={r.reading}，登记齿位={list(r.registered)}，"
                f"当前={current} 判定{'通过' if inside == r.reading else '证伪'}",
                dict(digits)))

    if any(digits[d] is None for d in DRUMS):
        missing = [d for d in DRUMS if digits[d] is None]
        raise ValueError(f"鼓 {missing} 缺少亲眼见到的鼓面（Seen），无法定位止位")

    stop = Stop(dict(digits))
    return Witness(stop, pulse_truth, group_truth, tuple(timeline))


def _unsat_core(
    records: list[Record],
    variable_names: list,
    group_of: dict[int, str],
) -> tuple[list[Record], str]:
    """贪心逐条剔除：仍矛盾就保持剔除，最终剩下的最小子集本身不可满足。"""

    def satisfiable(subset: list[Record]) -> bool:
        # 缺定位鼓面的子集无法形成任何真相，按“无效”处理，不参与缩核
        for drum in DRUMS:
            if not any(isinstance(r, Seen) and r.drum == drum for r in subset):
                raise ValueError("子集缺少每只鼓的定位 Seen")
        for assignment in product((False, True), repeat=len(variable_names)):
            truth = dict(zip(variable_names, assignment))
            if _simulate(subset, truth, group_of) is not None:
                return True
        return False

    # 定位 Seen 始终作为前提在场，贪心缩核只在脉冲/触片记录上进行
    premises = [r for r in records if isinstance(r, Seen)]
    core = [r for r in records if not isinstance(r, Seen)]
    for r in list(core):
        trial = premises + [x for x in core if x is not r]
        # 拿掉 r 后存在可通过真相 -> r 是矛盾必要成员，保留；否则剔除
        if satisfiable(trial):
            continue
        core = trial
    # 对外只暴露相互打架的非前提记录（Seen 是共同前提，不列入矛盾）
    return core, _conflict_reason(core)


def _conflict_reason(core: list[Record]) -> str:
    reasons = []
    for r in core:
        if isinstance(r, Contact):
            reasons.append(
                f"seq={r.seq} 触片要求 {r.drum} 鼓"
                f"{'在' if r.reading else '不在'} {list(r.registered)}")
        else:
            reasons.append(f"seq={r.seq} {r.drum} 鼓脉冲({r.ack.value})")
    return "下列记录不能同时成立：" + "；".join(reasons)


def _validate_shape(records: list[Record]) -> None:
    seqs = [r.seq for r in records]
    if len(seqs) != len(set(seqs)):
        raise ValueError("交班记录 seq 必须全局唯一，按先后排序")
    seen_drums = {r.drum for r in records if isinstance(r, Seen)}
    missing = [d for d in DRUMS if d not in seen_drums]
    if missing:
        raise ValueError(f"鼓 {missing} 缺少亲眼见到的鼓面（Seen），无法定位止位")
    # 同组失电脉冲必须共享 NAK 状态（ACK 脉冲没有“可能没走”的语义）。
    groups: dict[str, set] = {}
    for r in records:
        if isinstance(r, Pulse) and r.group is not None:
            groups.setdefault(r.group, set()).add(r.ack)
    for group, statuses in groups.items():
        if statuses != {PulseStatus.NAK}:
            raise ValueError(f"总闸失电组 {group} 必须全部为失回执(NAK)脉冲")
