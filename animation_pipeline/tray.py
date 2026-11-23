"""接盘台：同轨两盘、具名盘格、硬配方盒、缓冲槽与齐盒报告。

轨道是一维整数坐标。每支托盘有初位、自己的一组“可动时点”（毫秒）、
相邻时点最多可走格数与若干具名盘格；盘格坐标 = 盘位 + 格偏移。
两盘共用同一轨道，不得穿过彼此（左盘所有盘格始终严格在右盘所有盘格左侧）。

糖在“出模拍 + 飞行毫秒”到达具名落点 ``landing``，只能由**当时正位于该点**
的一个空格接住；被接住后就钉在该格（不挪糖补盒）。
盒有“品种 + 数目”硬配方，盘格归属哪一盒已知；盒齐当且仅当该盒全部盘格
含糖且实际品种计数与配方完全相等。

时间一律整数毫秒；盘位在两可动时点之间保持不变（分段常量）。
"""

from __future__ import annotations

import itertools
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from .demold import Arrival

TrayKey = tuple[str, str]  # (tray_name, cell_name)


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class TrayCell:
    name: str
    offset: int              # 相对盘位的格偏移
    box: str | None = None   # 归属盒（None 表示非盒盘格）


@dataclass(frozen=True)
class Tray:
    name: str
    initial_pos: int
    move_times_ms: tuple[int, ...]
    stride: int              # 相邻时点最多可走格数
    cells: tuple[TrayCell, ...]

    def __post_init__(self) -> None:
        if self.stride < 0:
            raise ValueError("stride 必须非负")
        if any(b <= a for a, b in zip(self.move_times_ms, self.move_times_ms[1:])):
            raise ValueError("可动时点必须严格升序")

    def offsets(self) -> tuple[int, ...]:
        return tuple(c.offset for c in self.cells)


@dataclass(frozen=True)
class Box:
    name: str
    recipe: Mapping[str, int]   # 品种 -> 数目

    def recipe_counter(self) -> Counter:
        return Counter({k: v for k, v in self.recipe.items() if v > 0})


# 轨迹：tray 名 -> {可动时点毫秒: 盘位}；未列出的时点表示“不动”。
Trajectory = Mapping[str, Mapping[int, int]]


@dataclass(frozen=True)
class BufferDevice:
    """单次缓冲槽：装在某一盘格上的暂存口。"""

    tray: str
    cell: str                  # 缓冲槽所在盘格（口）
    target_cell: str           # 同盘另一个盒格
    deliver_time_ms: int
    only_cavity: str | None = None  # 只接这颗指定（未分盒）糖；None 表示接首颗到点糖


@dataclass(frozen=True)
class DelayPinDevice:
    """整轴延时扣：某排某拍本可脱出时，把整排改到下一许可脉冲。"""

    row: str
    time_ms: int


Device = BufferDevice | DelayPinDevice


# --------------------------------------------------------------------------- #
# 轨迹几何
# --------------------------------------------------------------------------- #

def position_at(tray: Tray, trajectory: Trajectory, time_ms: int) -> int:
    pos = tray.initial_pos
    moves = trajectory.get(tray.name, {})
    for mt in tray.move_times_ms:
        if mt > time_ms:
            break
        if mt in moves:
            pos = moves[mt]
    return pos


def cell_coord_at(tray: Tray, cell: TrayCell, trajectory: Trajectory, time_ms: int) -> int:
    return position_at(tray, trajectory, time_ms) + cell.offset


def _extent_at(tray: Tray, trajectory: Trajectory, time_ms: int) -> tuple[int, int]:
    pos = position_at(tray, trajectory, time_ms)
    offs = tray.offsets()
    return pos + min(offs), pos + max(offs)


def validate_trajectory(trays: Sequence[Tray], trajectory: Trajectory) -> None:
    """校验步长约束与两盘不穿越。"""
    by_name = {t.name: t for t in trays}
    for tray in trays:
        moves = trajectory.get(tray.name, {})
        prev_pos = tray.initial_pos
        for mt in tray.move_times_ms:
            if mt in moves:
                new_pos = moves[mt]
                if abs(new_pos - prev_pos) > tray.stride:
                    raise ValueError(
                        f"盘 {tray.name} 在 {mt}ms 越过步长上限 {tray.stride}"
                    )
                prev_pos = new_pos
    if len(trays) == 2:
        ordered = sorted(
            trays, key=lambda t: (_extent_at(t, trajectory, 0)[0], t.name)
        )
        left, right = ordered
        # 检查所有盘位可能变化的时刻（两盘可动时点并集 + 起点）。
        times = {0}
        for tray in trays:
            times.update(tray.move_times_ms)
        for tm in sorted(times):
            lmax = _extent_at(left, trajectory, tm)[1]
            rmin = _extent_at(right, trajectory, tm)[0]
            if lmax >= rmin:
                raise ValueError(
                    f"两盘在 {tm}ms 穿越/重叠（左到 {lmax}，右起 {rmin}）"
                )


# --------------------------------------------------------------------------- #
# 仿真结果
# --------------------------------------------------------------------------- #

@dataclass
class CellRecord:
    tray: str
    cell: str
    box: str | None
    cavity: str | None = None
    flavor: str | None = None
    via: str | None = None  # direct / buffer


@dataclass
class TrayResult:
    cells: dict[TrayKey, CellRecord]
    lost: list[Arrival]
    complete_boxes: tuple[str, ...]
    incomplete: dict[str, dict]
    buffer_trace: list[dict] = field(default_factory=list)

    def cell_trace(self) -> dict[str, dict]:
        return {
            f"{tray}/{cell}": {
                "box": rec.box,
                "cavity": rec.cavity,
                "flavor": rec.flavor,
                "via": rec.via,
            }
            for (tray, cell), rec in sorted(self.cells.items())
        }


def _box_status(
    boxes: Mapping[str, Box],
    trays: Sequence[Tray],
    cells: dict[TrayKey, CellRecord],
) -> tuple[tuple[str, ...], dict[str, dict]]:
    complete: list[str] = []
    incomplete: dict[str, dict] = {}
    # 每盒收集其全部盘格的实得品种。
    box_cells: dict[str, list[CellRecord]] = {name: [] for name in boxes}
    designated: Counter = Counter()
    for tray in trays:
        for cell in tray.cells:
            if cell.box is not None and cell.box in box_cells:
                box_cells[cell.box].append(cells[(tray.name, cell.name)])
                designated[cell.box] += 1
    for name, box in boxes.items():
        recs = box_cells.get(name, [])
        actual = Counter(r.flavor for r in recs if r.cavity is not None)
        recipe = box.recipe_counter()
        empty = sum(1 for r in recs if r.cavity is None)
        if actual == recipe and empty == 0 and recs:
            complete.append(name)
        else:
            missing = recipe - actual
            excess = actual - recipe
            note = None
            if designated[name] == 0:
                note = "该盒在盘面上没有具名盘格"
            elif designated[name] < sum(recipe.values()):
                note = f"具名盘格 {designated[name]} 少于配方总数 {sum(recipe.values())}"
            incomplete[name] = {
                "designated_cells": designated[name],
                "empty_cells": empty,
                "missing": dict(missing),
                "excess": dict(excess),
                "actual": dict(actual),
                "recipe": dict(recipe),
                "note": note,
            }
    return tuple(complete), incomplete


# --------------------------------------------------------------------------- #
# 仿真
# --------------------------------------------------------------------------- #

def _validate_device(
    trays: Sequence[Tray], boxes: Mapping[str, Box], device: Device | None
) -> None:
    if not isinstance(device, BufferDevice):
        return
    by_name = {t.name: t for t in trays}
    if device.tray not in by_name:
        raise ValueError(f"缓冲槽所在盘 {device.tray} 不存在")
    tray = by_name[device.tray]
    cells = {c.name: c for c in tray.cells}
    if device.cell not in cells:
        raise ValueError(f"缓冲槽口 {device.cell} 不是盘 {device.tray} 的盘格")
    if device.target_cell not in cells:
        raise ValueError(f"缓冲目标 {device.target_cell} 不是同盘的盘格")
    if device.target_cell == device.cell:
        raise ValueError("缓冲目标必须是另一个盘格")
    if cells[device.target_cell].box is None:
        raise ValueError("缓冲目标必须是盒格（归属某盒）")


def simulate_trays(
    arrivals: Sequence[Arrival],
    trays: Sequence[Tray],
    boxes: Mapping[str, Box],
    trajectory: Trajectory,
    device: Device | None = None,
) -> TrayResult:
    """给定到达时刻、盘轨迹与设备，计算接糖、落空与齐盒。"""
    validate_trajectory(trays, trajectory)
    _validate_device(trays, boxes, device)
    by_name = {t.name: t for t in trays}
    cells: dict[TrayKey, CellRecord] = {}
    for tray in trays:
        for cell in tray.cells:
            cells[(tray.name, cell.name)] = CellRecord(tray.name, cell.name, cell.box)
    occupied: dict[TrayKey, str] = {}

    buffer = device if isinstance(device, BufferDevice) else None
    buffer_hold: Arrival | None = None      # 缓冲槽中的糖（完整到达信息）
    buffer_used = False
    buffer_trace: list[dict] = []

    def coord_cell_at(t: int, coord: int) -> TrayKey | None:
        """返回时刻 t 恰位于坐标 coord、且为空格的盘格。"""
        for tray in trays:
            pos = position_at(tray, trajectory, t)
            for cell in tray.cells:
                if pos + cell.offset == coord and (tray.name, cell.name) not in occupied:
                    return (tray.name, cell.name)
        return None

    events: list[tuple[int, int, object]] = []
    for arr in arrivals:
        events.append((arr.time_ms, 1, arr))  # 到达=1
    if buffer is not None:
        events.append((buffer.deliver_time_ms, 0, ("deliver",)))  # 送达=0，先处理
    events.sort(key=lambda e: (e[0], e[1]))

    lost: list[Arrival] = []
    for t, _, payload in events:
        if payload == ("deliver",):
            assert buffer is not None
            target_key = (buffer.tray, buffer.target_cell)
            if buffer_hold is not None and target_key not in occupied:
                occupied[target_key] = buffer_hold.cavity
                rec = cells[target_key]
                rec.cavity = buffer_hold.cavity
                rec.flavor = buffer_hold.flavor
                rec.via = "buffer"
                buffer_trace.append(
                    {"t": t, "action": "deliver", "cell": list(target_key),
                     "cavity": buffer_hold.cavity, "flavor": buffer_hold.flavor}
                )
                buffer_hold = None
            else:
                buffer_trace.append(
                    {"t": t, "action": "deliver_failed", "cell": list(target_key)}
                )
            continue

        arr = payload  # type: ignore[assignment]
        assert isinstance(arr, Arrival)
        # 缓冲槽：只在其固有落点、空槽且未用过时接糖。
        if buffer is not None and not buffer_used and buffer_hold is None:
            btray = by_name[buffer.tray]
            bcell = next(c for c in btray.cells if c.name == buffer.cell)
            bcoord = cell_coord_at(btray, bcell, trajectory, t)
            want = buffer.only_cavity in (None, arr.cavity)
            if bcoord == arr.landing and want:
                buffer_hold = arr
                buffer_used = True
                buffer_trace.append(
                    {"t": t, "action": "catch", "cell": [btray.name, bcell.name], "cavity": arr.cavity}
                )
                continue
        key = coord_cell_at(t, arr.landing)
        if key is None:
            lost.append(arr)
            continue
        # 缓冲槽所在盘格只接指定糖，其它糖不能占用它。
        if buffer is not None and key == (buffer.tray, buffer.cell):
            lost.append(arr)
            continue
        occupied[key] = arr.cavity
        rec = cells[key]
        rec.cavity = arr.cavity
        rec.flavor = arr.flavor
        rec.via = "direct"

    complete, incomplete = _box_status(boxes, trays, cells)
    return TrayResult(
        cells=cells,
        lost=lost,
        complete_boxes=complete,
        incomplete=incomplete,
        buffer_trace=buffer_trace,
    )


# --------------------------------------------------------------------------- #
# 计划与搜索
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Plan:
    table_choice: Mapping[str, int]
    trajectory: Trajectory
    device: Device | None = None

    def key(self) -> tuple:
        return (
            tuple(sorted(self.table_choice.items())),
            tuple(
                (name, tuple(sorted(moves.items())))
                for name, moves in sorted(self.trajectory.items())
            ),
            repr(self.device),
        )


def candidate_positions(tray: Tray, landings: Iterable[int]) -> tuple[int, ...]:
    """能让某盘格正好落在某落点的盘位候选值。"""
    coords = {tray.initial_pos}
    for landing in landings:
        for off in tray.offsets():
            coords.add(landing - off)
    return tuple(sorted(coords))


def enumerate_trajectories(
    trays: Sequence[Tray], landings: Iterable[int], limit: int = 200000
) -> list[dict[str, dict[int, int]]]:
    """枚举合法盘位轨迹（只取能让盘格对准落点的候选盘位）。"""
    landings = tuple(landings)
    per_tray_choices: list[list[dict[int, int]]] = []
    for tray in trays:
        cand = candidate_positions(tray, landings)
        times = tray.move_times_ms
        opts: list[dict[int, int]] = [{}]
        if times and cand:
            # 每个可动时点取一个候选盘位；空轨迹（不动）也保留。
            combos = itertools.product(cand, repeat=len(times))
            opts = [dict(zip(times, combo)) for combo in combos]
            opts.append({})
        per_tray_choices.append(opts)

    out: list[dict[str, dict[int, int]]] = []
    for combo in itertools.product(*per_tray_choices):
        traj = {tray.name: choice for tray, choice in zip(trays, combo)}
        try:
            validate_trajectory(trays, traj)
        except ValueError:
            continue
        out.append(traj)
        if len(out) >= limit:
            break
    return out


def search_best(
    arrivals: Sequence[Arrival],
    trays: Sequence[Tray],
    boxes: Mapping[str, Box],
    device: Device | None = None,
    *,
    max_trajectories: int = 200000,
) -> tuple[Plan | None, TrayResult | None]:
    """在给定设备下，搜索使完整盒最多的轨迹（并列任取首个）。"""
    landings = sorted({a.landing for a in arrivals})
    trajectories = enumerate_trajectories(trays, landings, limit=max_trajectories)
    best_plan: Plan | None = None
    best_result: TrayResult | None = None
    best_score = -1
    for traj in trajectories:
        result = simulate_trays(arrivals, trays, boxes, traj, device)
        score = len(result.complete_boxes)
        if score > best_score:
            best_score = score
            best_plan = Plan(table_choice={}, trajectory=traj, device=device)
            best_result = result
    return best_plan, best_result
