"""谜题编排：把开模、脉冲表/设备选择、接盘台搜索与齐盒报告连起来。

提供：
* ``Scenario``：世界 + 模腔 + 顶排 + 两份盘/盒配方。
* ``solve``：在给定设备下，对全部脉冲表组合（必要时含延时扣）与合法盘轨迹
  搜索“完整盒最多”的方案（并列任取第一）。
* 报告函数：逐拍出糖/未脱原因、每格实得模腔、未齐盒缺项、设备改动前后对比。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Mapping, Sequence

from .demold import (
    MAX_CAVITIES,
    MAX_TRAYS,
    Arrival,
    Cavity,
    CoaxialRow,
    DemoldResult,
    DemoldWorld,
    arrivals_from,
    explain_cavity,
    simulate_demold,
)
from .tray import (
    Box,
    DelayPinDevice,
    Device,
    Plan,
    Tray,
    TrayResult,
    search_best,
)


@dataclass
class Scenario:
    name: str
    world: DemoldWorld
    cavities: Mapping[str, Cavity]
    rows: Sequence[CoaxialRow]
    trays: Sequence[Tray]
    boxes: Mapping[str, Box]

    def __post_init__(self) -> None:
        # 合同上限：最多四腔、两盘；且每个模腔必须恰好属于一支顶排。
        if len(self.cavities) > MAX_CAVITIES:
            raise ValueError(f"合同最多 {MAX_CAVITIES} 腔，本场景有 {len(self.cavities)} 腔")
        if len(self.trays) > MAX_TRAYS:
            raise ValueError(f"合同最多 {MAX_TRAYS} 盘，本场景有 {len(self.trays)} 盘")
        assigned: list[str] = []
        for row in self.rows:
            assigned.extend(row.cavities)
        unknown = set(assigned) - set(self.cavities)
        if unknown:
            raise ValueError(f"顶排引用了未知模腔 {sorted(unknown)}")
        duplicated = {c for c in assigned if assigned.count(c) > 1}
        if duplicated:
            raise ValueError(f"模腔 {sorted(duplicated)} 被多支顶排共用")
        missing = set(self.cavities) - set(assigned)
        if missing:
            raise ValueError(f"模腔 {sorted(missing)} 未归入任何顶排")

    # -- 别名 ------------------------------------------------------------ #
    @property
    def landings(self) -> tuple[int, ...]:
        return tuple(sorted({c.landing for c in self.cavities.values()}))


# --------------------------------------------------------------------------- #
# 脉冲表组合
# --------------------------------------------------------------------------- #

def table_combinations(rows: Sequence[CoaxialRow]) -> list[dict[str, int]]:
    names = [r.name for r in rows]
    ranges = [range(len(r.pulse_tables)) for r in rows]
    return [dict(zip(names, combo)) for combo in itertools.product(*ranges)]


@dataclass(frozen=True)
class DemoldSolution:
    table_choice: Mapping[str, int]
    pin: tuple[str, int] | None
    demold: DemoldResult
    arrivals: tuple[Arrival, ...]


def demold_options(
    scenario: Scenario,
    pins: Sequence[tuple[str, int] | None] | None = None,
) -> list[DemoldSolution]:
    """枚举脉冲表组合（与给定延时扣候选）下的开模结果。

    ``pins`` 为 None 时枚举所有可能的单次延时扣（用于分析）；否则只考虑给定的
    候选（含 None 表示不按扣）。
    """
    out: list[DemoldSolution] = []
    if pins is None:
        pin_list: list[tuple[str, int] | None] = [None]
        for row in scenario.rows:
            for t in sorted({pt for table in row.pulse_tables for pt in table}):
                pin_list.append((row.name, t))
    else:
        pin_list = list(pins)
        if None not in pin_list:
            pin_list.append(None)
    seen: set[tuple] = set()
    for choice in table_combinations(scenario.rows):
        for pin in pin_list:
            demold = simulate_demold(
                scenario.world, scenario.cavities, scenario.rows, choice, pin
            )
            arrivals = tuple(arrivals_from(demold, scenario.cavities))
            # 同一批到达（且同一套脉冲表选择）只搜索一次。
            sig = (
                tuple(sorted(choice.items())),
                tuple((a.cavity, a.time_ms) for a in arrivals),
            )
            if sig in seen:
                continue
            seen.add(sig)
            out.append(
                DemoldSolution(
                    table_choice=dict(choice),
                    pin=pin,
                    demold=demold,
                    arrivals=arrivals,
                )
            )
    return out


# --------------------------------------------------------------------------- #
# 完整求解
# --------------------------------------------------------------------------- #

@dataclass
class Solved:
    device: Device | None
    demold_solution: DemoldSolution
    plan: Plan
    tray_result: TrayResult

    @property
    def complete(self) -> tuple[str, ...]:
        return self.tray_result.complete_boxes


def solve(
    scenario: Scenario,
    *,
    device: Device | None = None,
    pins: Sequence[tuple[str, int] | None] | None = ([None],),
    max_trajectories: int = 200000,
) -> Solved | None:
    """搜索完整盒最多的方案。并列时按（脉冲表组合、到达签名）稳定取首个。

    ``device`` 可为缓冲槽或延时扣；若为 ``DelayPinDevice``，则把它作为唯一延时扣
    候选按下，并自动枚举脉冲表。``pins`` 仅在没有设备延时时用于候选延时扣集合。
    """
    pin_candidates: Sequence[tuple[str, int] | None]
    if isinstance(device, DelayPinDevice):
        pin_candidates = [(device.row, device.time_ms)]
    else:
        pin_candidates = pins  # type: ignore[assignment]
    best: Solved | None = None
    for ds in demold_options(scenario, pins=pin_candidates):
        plan, result = search_best(
            list(ds.arrivals),
            scenario.trays,
            scenario.boxes,
            device=device,
            max_trajectories=max_trajectories,
        )
        if plan is None or result is None:
            continue
        if best is None or len(result.complete_boxes) > len(best.complete):
            best = Solved(
                device=device, demold_solution=ds, plan=plan, tray_result=result
            )
    return best


@dataclass
class DeviceChoice:
    """对比“无设备”与两种设备后的最佳选择。"""

    best: Solved | None
    baseline: Solved | None
    by_device: dict[str, Solved | None]

    @property
    def device_name(self) -> str:
        g = self.best.device if self.best else None
        if g is None:
            return "无设备"
        return type(g).__name__


def choose_device(
    scenario: Scenario,
    devices: Sequence[Device],
    *,
    max_trajectories: int = 200000,
) -> DeviceChoice:
    """分别用无设备与候选设备求解，返回完整盒最多的那个（并列优先无设备）。

    对应工单要求：只选两种装置之一，做一份尽可能多的齐盒单，并给出基本对照。
    """
    baseline = solve(scenario, max_trajectories=max_trajectories)
    by_device: dict[str, Solved | None] = {}
    best = baseline
    best_score = len(baseline.complete) if baseline else -1
    for dev in devices:
        sol = solve(scenario, device=dev, max_trajectories=max_trajectories)
        name = f"{type(dev).__name__}:{repr(dev)}"
        by_device[name] = sol
        if sol is not None and len(sol.complete) > best_score:
            best_score = len(sol.complete)
            best = sol
    return DeviceChoice(best=best, baseline=baseline, by_device=by_device)
    """成功被接盘接住（含缓冲）的糖对应的到达。"""
    caught = {
        rec.cavity
        for rec in solved.tray_result.cells.values()
        if rec.cavity is not None
    }
    return [a for a in solved.demold_solution.arrivals if a.cavity in caught]


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #

def report_demold(ds: DemoldSolution) -> list[str]:
    lines: list[str] = []
    for rep in sorted(ds.demold.reports, key=lambda r: (r.row, r.time_ms)):
        head = f"[{rep.row}] {rep.time_ms}ms -> {rep.status}"
        if rep.status == "pushed":
            lines.append(f"{head}: 脱出 {list(rep.pushed)}")
        elif rep.status == "blocked":
            detail = ", ".join(
                f"{c} 先碰 {cell}{rgb}({role})" for c, cell, rgb, role in rep.blocked
            )
            lines.append(f"{head}: 未脱 — {detail}")
        elif rep.status == "delayed":
            lines.append(f"{head}: 延时扣生效，整排 {list(rep.delayed)} 移到下一许可脉冲")
        else:
            lines.append(f"{head}: 无糖")
    if ds.demold.remaining:
        lines.append(f"仍未脱: {list(ds.demold.remaining)}")
    return lines


def report_teacher(
    scenario: Scenario,
    ds: DemoldSolution,
    row_name: str,
    query_times: Sequence[int],
) -> dict[int, dict]:
    """回答“某组会在第几拍出哪几颗、为何先前未脱”。

    针对查询拍，给出该排每个“尚有糖”腔的真实扫过形状与碰壁坐标。
    """
    row = next(r for r in scenario.rows if r.name == row_name)
    out: dict[int, dict] = {}
    for t in query_times:
        # 只保留“到 t 拍前尚未落”的糖。
        fallen_before = {
            e.cavity for e in ds.demold.events if e.row == row_name and e.time_ms < t
        }
        pending = [c for c in row.cavities if c not in fallen_before]
        checks = {}
        for c in pending:
            chk = explain_cavity(scenario.world, scenario.cavities[c], t)
            checks[c] = {
                "ok": chk.ok,
                "blocked": chk.blocked_by(),
                "swept": sorted(map(list, scenario.cavities[c].swept())),
            }
        out[t] = {
            "pending": pending,
            "checks": checks,
            "pushed_here": [
                e.cavity for e in ds.demold.events
                if e.row == row_name and e.time_ms == t
            ],
        }
    return out


def report_boxes(result: TrayResult) -> list[str]:
    lines = [f"完整盒: {list(result.complete_boxes)}"]
    for name, info in sorted(result.incomplete.items()):
        if name in result.complete_boxes:
            continue
        extra = f"（{info['note']}）" if info.get("note") else ""
        lines.append(
            f"未齐 {name}: 空盘格 {info['empty_cells']}，缺 {info['missing']}，"
            f"多 {info['excess']}，实得 {info['actual']}{extra}"
        )
    if result.lost:
        lines.append(
            "落空糖: " + ", ".join(f"{a.cavity}@{a.time_ms}ms->{a.landing}" for a in result.lost)
        )
    return lines


def report_cells(result: TrayResult) -> list[str]:
    lines: list[str] = []
    for key, rec in sorted(result.cells.items()):
        got = rec.cavity or "空"
        lines.append(f"{key[0]}/{key[1]} (盒={rec.box}) -> {got}"
                     + (f" [{rec.flavor}]" if rec.flavor else "")
                     + (f" via {rec.via}" if rec.via else ""))
    return lines
