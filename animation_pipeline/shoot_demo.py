"""端到端演示：从袖珍彩格“原目录”到可装回的补拍单。

运行：.venv/bin/python -m animation_pipeline.shoot_demo
产物落在 var/shoot/ 下：每套场景的读法报告、两套工位的补拍单 JSON，以及
题面要求的三类结论（正拍交付、补不齐、拒收）。

只走本仓库管线：原目录 → FramePlayer 合成 → 场记读法 → 补拍单 → 核对。
"""

from __future__ import annotations

import json
from pathlib import Path

from .puppets import read_scene
from .scenes import (
    base_jobs,
    build_crowd_scene,
    build_example_scene,
    build_two_act_scene,
    build_variant_clear_scene,
    build_variant_no_back_scene,
    no_back_jobs,
    two_act_jobs,
)
from .shoot import (
    STATION_DOUBLE_FRAME,
    STATION_SINGLE_TURNTABLE,
    Worksheet,
    plan_shoot,
    verify_plan,
)

ROOT = Path(__file__).resolve().parent.parent / "var" / "shoot"


def _readings(scene) -> dict:
    readings = {}
    for fid, marks in scene.marks_by_frame.items():
        r = read_scene(
            scene.frames[fid].canvas, marks, tuple(scene.patterns.values()),
            frame_id=fid, time_ms=fid * 10,
        )
        for name, pr in r.puppets.items():
            readings[(fid, name)] = pr
    return readings


def _readings_report(scene) -> list[dict]:
    out = []
    for (fid, name), pr in sorted(_readings(scene).items()):
        out.append({
            "frame_id": fid,
            "puppet": name,
            "visible": pr.visible,
            "merged": pr.merged,
            "pose": pr.pose.name if pr.pose else None,
            "determinate": pr.determinate,
            "alternatives": [a.review_key for a in pr.alternatives],
            "review_basis": pr.review_basis,
        })
    return out


def _worksheet(scene, jobs) -> Worksheet:
    return Worksheet(
        jobs=jobs, cameras=scene.cameras, puppets=scene.puppets,
        patterns=scene.patterns, binding=scene.binding, readings=_readings(scene),
    )


def _dump(name: str, payload) -> Path:
    path = ROOT / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _plan_both(scene, jobs):
    ws = _worksheet(scene, jobs)
    return {
        station: plan_shoot(ws, station)
        for station in (STATION_DOUBLE_FRAME, STATION_SINGLE_TURNTABLE)
    }, ws


def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)

    # 1) 题面例子：两帧正背同拍。
    example = build_example_scene()
    example_readings = _readings_report(example)
    _dump("example_readings.json", example_readings)
    plans, ws = _plan_both(example, base_jobs())
    double = plans[STATION_DOUBLE_FRAME]
    _dump("example_plan_double_frame.json", double.to_report())
    pose_by_frame = {r["frame_id"]: r["pose"] for r in example_readings}
    print("1) 题面例子（后两帧均读到双格红）")
    print(f"   读法：frame1={pose_by_frame.get(1)!r} frame2={pose_by_frame.get(2)!r}")
    print(f"   双面框：交付 {double.delivery_order}，占 {double.slot_count} 格，"
          f"未交 {len(double.undelivered)}，核对 {verify_plan(double, ws) or '通过'}")
    tt = plans[STATION_SINGLE_TURNTABLE]
    print(f"   单面转盘：交付 {tt.delivery_order or '无'}，"
          f"未交 {len(tt.undelivered)}（须同拍，转盘做不了）")

    # 2) 对照：次帧处置改成清右格，末帧只剩左格 → 补不齐。
    clear = build_variant_clear_scene()
    _dump("clear_readings.json", _readings_report(clear))
    plans, ws = _plan_both(clear, base_jobs())
    d = plans[STATION_DOUBLE_FRAME]
    _dump("clear_plan_double_frame.json", d.to_report())
    print("2) 对照 variant_clear（末帧只剩左格）")
    print(f"   双面框：交付帧 {sorted({e.frame_id for e in d.exposures})}，"
          f"未交帧 {sorted({u.frame_id for u in d.undelivered})}（片序不变却补不齐）")

    # 3) 对照：仅把第二机位换成必须看背面、而该偶无背饰 → 拒收。
    nback = build_variant_no_back_scene()
    plans, ws = _plan_both(nback, no_back_jobs())
    d = plans[STATION_DOUBLE_FRAME]
    _dump("no_back_plan_double_frame.json", d.to_report())
    print("3) 对照 variant_no_back（第二机位须看背面，无背饰）")
    print(f"   双面框：交付 {d.delivery_order or '无'}，"
          f"未交 {[u.reason for u in d.undelivered]}")

    # 4) 第二张工单：首幕正背同拍、次幕只要正面新姿势。
    two = build_two_act_scene()
    _dump("two_act_readings.json", _readings_report(two))
    plans, ws = _plan_both(two, two_act_jobs())
    d = plans[STATION_DOUBLE_FRAME]
    _dump("two_act_plan_double_frame.json", d.to_report())
    print("4) 首/次幕工单")
    print(f"   双面框：交付帧 {sorted({e.frame_id for e in d.exposures})}，"
          f"次幕未交：{[u.trace for u in d.undelivered]}")

    # 5) 多偶场景：4 偶/3 相机/2 格，含合并。
    crowd = build_crowd_scene()
    _dump("crowd_readings.json", _readings_report(crowd))
    print("5) 多偶场景：读法已输出，含 frame1 两只红偶合并（不给纸样）")

    print(f"产物已写入 {ROOT}")


if __name__ == "__main__":
    main()
