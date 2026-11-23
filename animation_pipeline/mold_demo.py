"""开模接盘演示素材与场景：全部通过真实 GIF -> 目录 -> 播放器读取。

场景一（合同例子，4x4 画布，3 帧各 10ms）：
    帧0 [0,10)  在 (1,0) 画模壁（绿，wall_a）
    帧1 [10,20) 在 (1,0) 画透明像素 —— 画布保持首帧模壁
    帧2 [20,30) 把 (1,0) 画白（free）
    糖 c1 初始 {(1,1)}，顶针方向 (0,-1)，步数 1，扫过 {(1,1),(1,0)}。
    唯一脉冲表 (10,20)：保留首帧壁时 10 拍碰壁、20 拍可脱；
    飞行 1ms -> 21ms 到达，原味盘格在 21ms 正好进站接住。

    变体 A：把帧0处置改为 2（清掉 (1,0) 这处局部）-> 10 拍即可脱，
            11ms 到达时盘格尚未进站，糖落空且不能留到下一脉冲。
    变体 B：把 slot 拨给他味盒 -> 原味糖仍落进该具名格，于是“他味盒”多一颗
            原味、缺一颗他味，“原味盒”没有具名盘格、仍交不了（不能挪糖补盒）。

场景二（两盘 + 共轴耦合，4x2 画布，3 帧各 10ms，无模壁）：
    模腔 a 落点 0、b 落点 10。左盘 tray0 常驻 0 接 a；右盘 tray1 初位 20，
    20ms 才走到 10 接 b。各自早晚都似可接：
      * 拆成两排（a 表 (10,)、b 表 (20,)）：a@10->11 由 tray0 接，b@20->21 由
        tray1 接 —— 2 盒齐。
      * 合成一排（表 (10,20)）：10 拍二者同出、11ms 同时到达；tray1 尚未进站，
        b 落空 —— 只能交 1 盒。共顶排后不能一个早一个晚任选。

场景三（单盘 + 缓冲槽，一条轨道；同轴两腔原拍均能脱，一空一迟）：
    a 落点 0（盘格 A 盒A 在 11ms 正好进站接住），b 落点 4（盒B 的盘格 B 偏 8，
    11ms 还在 8，迟到）。M 盘格（偏 4、无盒）11ms 恰在 4。
      * 无设备：b 落进无盒的 M，盒B 缺 b —— 1 盒。
      * 缓冲槽装 M、目标 B、20ms 送达：b 先接入、再转盒B —— 2 盒。
    缓槽限制：若两颗迟到糖同拍都需要暂存（同落点），同一具名缓槽接不了两颗。

场景四（单盘 + 延时扣，同 a/b）：
    无扣：a@11 被 A 接、b 落进无盒 M —— 1 盒（盒A）。
    按扣(row,10)：两腔一起改到 20 拍、21ms 到达；重排盘位后 b 进盒B，但原来
    能接的 a 在新拍落到无盒格 —— 1 盒（盒B）。即“原来能接的那颗也要重排”。
    若 20 拍某腔新增挡壁：整排（含另一腔）本拍都不走，不能把另糖提前拆出。
"""

from __future__ import annotations

from pathlib import Path

from .gif_io import frame_spec, write_gif
from .catalog import FrameCatalog
from .demold import Cavity, CoaxialRow, DemoldWorld, FrameRoles
from .puzzle import Scenario
from .tray import Box, Tray, TrayCell

# 8 色全局调色板（索引 7 用作透明标记）。
PALETTE = [
    (0, 0, 0),        # 0 背景黑（free）
    (0, 200, 0),      # 1 绿 wall_a
    (0, 0, 200),      # 2 蓝 wall_b
    (255, 255, 255),  # 3 白 free
    (120, 120, 120),  # 4 灰 reflection（不占位）
    (200, 0, 0),      # 5 红
    (200, 200, 0),    # 6 黄
    (0, 0, 0),        # 7 透明标记
]
BG = 0
GREEN = 1
BLUE = 2
WHITE = 3
GRAY = 4
TRANSPARENT = 7

ROLES = FrameRoles(
    wall_a=frozenset({PALETTE[GREEN]}),
    wall_b=frozenset({PALETTE[BLUE]}),
    reflection=frozenset({PALETTE[GRAY]}),
)


# --------------------------------------------------------------------------- #
# GIF 构造
# --------------------------------------------------------------------------- #

def _build_example_gif(path: str, disposal0: int = 1) -> str:
    """场景一的三帧 GIF；disposal0=2 即“清掉 (1,0) 这处局部”的变体。"""
    frames = [
        frame_spec(1, 0, 1, 1, [GREEN], delay_centis=1, disposal=disposal0),
        frame_spec(1, 0, 1, 1, [TRANSPARENT], transparent_index=TRANSPARENT,
                   delay_centis=1, disposal=1),
        frame_spec(1, 0, 1, 1, [WHITE], delay_centis=1, disposal=1),
    ]
    write_gif(path, 4, 4, BG, PALETTE, frames)
    return path


def _build_layered_gif(path: str, width: int, height: int, frames) -> str:
    """每帧给出 {cell: 颜色索引}，其余格子透明（叠加继承）；3 帧各 10ms。"""
    specs = []
    for draw in frames:
        idx = []
        for y in range(height):
            for x in range(width):
                color = draw.get((x, y))
                idx.append(TRANSPARENT if color is None else color)
        specs.append(
            frame_spec(0, 0, width, height, idx, transparent_index=TRANSPARENT,
                       delay_centis=1, disposal=1)
        )
    write_gif(path, width, height, BG, PALETTE, specs)
    return path


def _world(path: str) -> DemoldWorld:
    catalog = FrameCatalog.from_gif(path, None)
    return DemoldWorld(catalog, ROLES)


# --------------------------------------------------------------------------- #
# 场景一
# --------------------------------------------------------------------------- #

def _example_cavity() -> Cavity:
    return Cavity(
        name="c1", cells=frozenset({(1, 1)}), direction=(0, -1), steps=1,
        landing=0, flight_ms=1, flavor="原味",
    )


def example_scenario(tmp_dir: Path, *, disposal0: int = 1,
                     slot_box: str = "原味盒") -> Scenario:
    """场景一。``slot_box`` 为盘格归属盒（默认原味盒）。"""
    path = _build_example_gif(str(Path(tmp_dir) / f"example_{disposal0}.gif"), disposal0)
    world = _world(path)
    boxes = {"原味盒": Box("原味盒", {"原味": 1})}
    if slot_box != "原味盒":
        boxes[slot_box] = Box(slot_box, {"他味": 1})
    tray = Tray(
        name="tray0", initial_pos=-5, move_times_ms=(20,), stride=5,
        cells=(TrayCell("slot", 0, box=slot_box),),
    )
    return Scenario(
        name="example", world=world, cavities={"c1": _example_cavity()},
        rows=(CoaxialRow("row1", ("c1",), ((10, 20),)),),
        trays=(tray,), boxes=boxes,
    )


# --------------------------------------------------------------------------- #
# 场景二：两盘 + 共轴耦合
# --------------------------------------------------------------------------- #

def _rising_cavity(name: str, x: int, landing: int, flavor: str) -> Cavity:
    return Cavity(
        name=name, cells=frozenset({(x, 1)}), direction=(0, -1), steps=1,
        landing=landing, flight_ms=1, flavor=flavor,
    )


def coupling_two_tray_scenario(tmp_dir: Path, *, shared_row: bool) -> Scenario:
    """场景二：tray0 常驻接 a，tray1 20ms 才进站接 b。"""
    path = _build_layered_gif(
        str(Path(tmp_dir) / "coupling.gif"), 4, 2,
        [{}, {}, {}],  # 全空位
    )
    world = _world(path)
    cavities = {
        "a": _rising_cavity("a", 0, landing=0, flavor="A"),
        "b": _rising_cavity("b", 2, landing=10, flavor="B"),
    }
    if shared_row:
        rows = (CoaxialRow("rowS", ("a", "b"), ((10, 20),)),)
    else:
        rows = (
            CoaxialRow("rowA", ("a",), ((10,),)),
            CoaxialRow("rowB", ("b",), ((20,),)),
        )
    tray0 = Tray("tray0", initial_pos=0, move_times_ms=(), stride=0,
                 cells=(TrayCell("A", 0, box="盒A"),))
    tray1 = Tray("tray1", initial_pos=20, move_times_ms=(20,), stride=10,
                 cells=(TrayCell("B", 0, box="盒B"),))
    boxes = {"盒A": Box("盒A", {"A": 1}), "盒B": Box("盒B", {"B": 1})}
    return Scenario(
        name=f"coupling_{'shared' if shared_row else 'split'}",
        world=world, cavities=cavities, rows=rows,
        trays=(tray0, tray1), boxes=boxes,
    )


# --------------------------------------------------------------------------- #
# 场景三：单盘 + 缓冲槽
# --------------------------------------------------------------------------- #

def _buffer_tray() -> Tray:
    # A 偏 0 盒A；M 偏 4 缓冲口；B 偏 8 盒B。
    return Tray(
        name="tray0", initial_pos=0, move_times_ms=(20,), stride=10,
        cells=(
            TrayCell("A", 0, box="盒A"),
            TrayCell("M", 4, box=None),
            TrayCell("B", 8, box="盒B"),
        ),
    )


def buffer_scenario(tmp_dir: Path) -> Scenario:
    """场景三：a@0 立即被 A 接；b@4 迟到需要缓冲口 M 接入后转盒B。"""
    path = _build_layered_gif(str(Path(tmp_dir) / "buffer.gif"), 4, 2, [{}, {}, {}])
    world = _world(path)
    cavities = {
        "a": _rising_cavity("a", 0, landing=0, flavor="A"),
        "b": _rising_cavity("b", 2, landing=4, flavor="B"),
    }
    boxes = {"盒A": Box("盒A", {"A": 1}), "盒B": Box("盒B", {"B": 1})}
    return Scenario(
        name="buffer", world=world, cavities=cavities,
        rows=(CoaxialRow("rowS", ("a", "b"), ((10,),)),),
        trays=(_buffer_tray(),), boxes=boxes,
    )


def buffer_two_late_scenario(tmp_dir: Path) -> Scenario:
    """场景三变体：b、c 两颗迟到糖同拍都需暂存，同一缓槽接不了两颗。

    b、c 都落在 4；11ms 时坐标 4 上唯一的空格是缓槽口 M。缓槽只先接一颗，
    另一颗落空——即便盒C 另有具名盘格 C，也补不上。
    """
    path = _build_layered_gif(str(Path(tmp_dir) / "buffer2.gif"), 4, 2, [{}, {}, {}])
    world = _world(path)
    cavities = {
        "a": _rising_cavity("a", 0, landing=0, flavor="A"),
        "b": _rising_cavity("b", 2, landing=4, flavor="B"),
        "c": _rising_cavity("c", 1, landing=4, flavor="C"),
    }
    boxes = {
        "盒A": Box("盒A", {"A": 1}),
        "盒B": Box("盒B", {"B": 1}),
        "盒C": Box("盒C", {"C": 1}),
    }
    tray = Tray(
        "tray0", initial_pos=0, move_times_ms=(20,), stride=10,
        cells=(
            TrayCell("A", 0, box="盒A"),
            TrayCell("M", 4, box=None),
            TrayCell("B", 8, box="盒B"),
            TrayCell("C", 12, box="盒C"),
        ),
    )
    return Scenario(
        name="buffer_two_late", world=world, cavities=cavities,
        rows=(CoaxialRow("rowS", ("a", "b", "c"), ((10,),)),),
        trays=(tray,), boxes=boxes,
    )


# --------------------------------------------------------------------------- #
# 场景四：单盘 + 延时扣
# --------------------------------------------------------------------------- #

def _pin_tray() -> Tray:
    # A 偏 0 盒A；B 偏 8 盒B（无缓冲口）。
    return Tray(
        name="tray0", initial_pos=0, move_times_ms=(20,), stride=10,
        cells=(
            TrayCell("A", 0, box="盒A"),
            TrayCell("B", 8, box="盒B"),
        ),
    )


def delay_pin_scenario(tmp_dir: Path, *, wall_on_b: bool = False) -> Scenario:
    """场景四：同轴两腔 a、b，表 (10,20)。

    ``wall_on_b=True`` 时帧2（>=20ms）在 b 的扫过格 (2,0) 新增绿壁，
    用于核“晚帧一腔新增挡壁时整排不走、不能提前拆出另糖”。
    """
    frame2 = {(2, 0): GREEN} if wall_on_b else {}
    path = _build_layered_gif(
        str(Path(tmp_dir) / f"pin_{int(wall_on_b)}.gif"), 4, 2,
        [{}, {}, frame2],
    )
    world = _world(path)
    cavities = {
        "a": _rising_cavity("a", 0, landing=0, flavor="A"),
        "b": _rising_cavity("b", 2, landing=4, flavor="B"),
    }
    boxes = {"盒A": Box("盒A", {"A": 1}), "盒B": Box("盒B", {"B": 1})}
    return Scenario(
        name=f"pin_{'wall' if wall_on_b else 'plain'}", world=world,
        cavities=cavities, rows=(CoaxialRow("rowS", ("a", "b"), ((10, 20),)),),
        trays=(_pin_tray(),), boxes=boxes,
    )


# --------------------------------------------------------------------------- #
# 演示主程序
# --------------------------------------------------------------------------- #

def _arrival_line(arrivals) -> str:
    return ", ".join(
        f"{a.cavity}({a.flavor})@{a.time_ms}ms->{a.landing}" for a in arrivals
    )


def run_demo(root: Path) -> dict:
    """跑完整组演示，打印报告并返回结构化摘要。"""
    from .puzzle import (
        demold_options,
        report_boxes,
        report_cells,
        report_demold,
        report_teacher,
        solve,
    )
    from .tray import BufferDevice, DelayPinDevice

    root.mkdir(parents=True, exist_ok=True)
    out: dict = {}

    def show(title: str, lines: list[str]) -> None:
        print(f"\n=== {title} ===")
        for line in lines:
            print("  " + line)

    # ---- 场景一：合同例子 + 两个变体 ---------------------------------- #
    s = example_scenario(root)
    ds = demold_options(s)[0]
    show("场景一 合同例子：逐拍出糖 / 未脱原因", report_demold(ds))
    teacher_lines = []
    for t, v in sorted(report_teacher(s, ds, "row1", [10, 20]).items()):
        parts = []
        for c, x in v["checks"].items():
            if x["ok"]:
                parts.append(f"{c}:ok")
            else:
                parts.append(f"{c}:blocked@{x['blocked'][0]}")
        teacher_lines.append(
            f"{t}ms pending={v['pending']} pushed={v['pushed_here']} "
            f"checks={{ {', '.join(parts)} }}"
        )
    show("场景一 教师问 row1 在 10/20 拍", teacher_lines)
    base = solve(s)
    show("场景一 基本对照（无设备）", [f"完整盒 {list(base.complete)}"] + report_cells(base.tray_result))
    out["example"] = {
        "arrivals": _arrival_line(ds.arrivals),
        "complete": list(base.complete),
        "cells": base.tray_result.cell_trace(),
    }

    va = solve(example_scenario(root, disposal0=2))
    show("场景一 变体A：清掉 (1,0) 局部后 10 拍就脱、11ms 落空", report_boxes(va.tray_result))
    out["exampleA"] = {"complete": list(va.complete),
                       "lost": [(a.cavity, a.time_ms) for a in va.tray_result.lost]}

    vb = solve(example_scenario(root, slot_box="他味盒"))
    show("场景一 变体B：slot 拨给他味盒，原味盒仍交不了", report_boxes(vb.tray_result))
    out["exampleB"] = {"complete": list(vb.complete)}

    # ---- 场景二：两盘 + 共轴耦合 -------------------------------------- #
    shared = solve(coupling_two_tray_scenario(root, shared_row=True))
    split = solve(coupling_two_tray_scenario(root, shared_row=False))
    show("场景二 共顶排（只能同拍）", [f"完整盒 {list(shared.complete)}"]
         + report_cells(shared.tray_result) + report_boxes(shared.tray_result))
    show("场景二 拆两排（可早/晚分别调度）", [f"完整盒 {list(split.complete)}"]
         + report_cells(split.tray_result))
    out["coupling"] = {
        "shared_complete": list(shared.complete),
        "split_complete": list(split.complete),
        "shared_arrivals": _arrival_line(shared.demold_solution.arrivals),
        "split_arrivals": _arrival_line(split.demold_solution.arrivals),
    }

    # ---- 场景三：缓冲槽 ---------------------------------------------- #
    sb = buffer_scenario(root)
    nb = solve(sb)
    dev = BufferDevice("tray0", "M", "B", deliver_time_ms=20)
    wb = solve(sb, device=dev)
    show("场景三 基本对照（无设备）：b 落进无盒 M", [f"完整盒 {list(nb.complete)}"]
         + report_cells(nb.tray_result))
    show("场景三 装缓冲槽 M->B@20ms", [f"完整盒 {list(wb.complete)}"]
         + report_cells(wb.tray_result)
         + [f"缓槽轨迹 {wb.tray_result.buffer_trace}"])
    tl = solve(buffer_two_late_scenario(root), device=dev)
    show("场景三 变体：两颗迟到糖同拍、同一缓槽接不了两颗", report_boxes(tl.tray_result))
    out["buffer"] = {
        "baseline_complete": list(nb.complete),
        "device_complete": list(wb.complete),
        "device_cells": wb.tray_result.cell_trace(),
        "trace": wb.tray_result.buffer_trace,
        "two_late_complete": list(tl.complete),
    }

    # ---- 场景四：延时扣 ---------------------------------------------- #
    sp = delay_pin_scenario(root)
    pb = solve(sp)
    pp = solve(sp, device=DelayPinDevice("rowS", 10))
    show("场景四 基本对照（无扣）", [f"完整盒 {list(pb.complete)}"]
         + report_cells(pb.tray_result))
    show("场景四 按整轴延时扣(rowS,10)", report_demold(pp.demold_solution)
         + [f"完整盒 {list(pp.complete)}"] + report_cells(pp.tray_result))
    # 晚帧新增挡壁：整排不走。
    sw = delay_pin_scenario(root, wall_on_b=True)
    from .demold import simulate_demold
    forced = simulate_demold(sw.world, sw.cavities, sw.rows, {"rowS": 0}, pin=("rowS", 10))
    class _Wrap:
        demold = forced
    show("场景四 变体：晚帧 b 新增挡壁 -> 整排(含 a)不走", report_demold(_Wrap()))
    out["pin"] = {
        "baseline_complete": list(pb.complete),
        "pinned_complete": list(pp.complete),
        "pinned_arrivals": _arrival_line(pp.demold_solution.arrivals),
        "pinned_cells": pp.tray_result.cell_trace(),
        "wall_remaining": list(forced.remaining),
    }

    # ---- 工单：每种场景只选一种设备，做尽可能多的齐盒单 --------------- #
    from .puzzle import choose_device
    wb_choice = choose_device(sb, [BufferDevice("tray0", "M", "B", 20)])
    pb_choice = choose_device(sp, [DelayPinDevice("rowS", 10)])
    show("工单 场景三选设备", [
        f"选用 {wb_choice.device_name}；基本对照 {list(wb_choice.baseline.complete)} "
        f"-> 齐盒单 {list(wb_choice.best.complete)}"
    ] + report_cells(wb_choice.best.tray_result))
    show("工单 场景四选设备", [
        f"选用 {pb_choice.device_name}；基本对照 {list(pb_choice.baseline.complete)} "
        f"-> 齐盒单 {list(pb_choice.best.complete)}"
    ] + report_cells(pb_choice.best.tray_result))
    out["workorder"] = {
        "buffer_choice": {
            "device": wb_choice.device_name,
            "baseline": list(wb_choice.baseline.complete),
            "sheets": list(wb_choice.best.complete),
        },
        "pin_choice": {
            "device": pb_choice.device_name,
            "baseline": list(pb_choice.baseline.complete),
            "sheets": list(pb_choice.best.complete),
        },
    }
    return out



if __name__ == "__main__":
    run_demo(Path(__file__).resolve().parent.parent / "var" / "mold")
