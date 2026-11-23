"""校场：独立找出各片段相容的联合坐标/时钟解释。

同一台机整段共用一个横轴方向和平移、一个时钟偏移：
    真位置 true = sign * 机内格号 grid + translation，sign ∈ {-1, +1}
    机内拍   camera_tick = 正式拍 official_beat + offset
标杆卡给“原画格坐标 + 可能的实际标杆位置”，同步灯卡给
“机内格号 + 允许对应的正式节拍”，各台同时看到同一编号闪灯必须落到同一拍。
互相矛盾（无任何联合解释）的素材不准入。
"""

from __future__ import annotations

from itertools import product
from typing import Dict, List, Sequence, Set, Tuple

from .model import (AdmissionCode, AdmissionFailure, AdmissionResult,
                    AxisSolution, CameraSpec, JointInterpretation)


def solve_axis(spec: CameraSpec) -> List[AxisSolution]:
    """枚举满足全部标杆卡的横轴解释（方向 × 整数平移）。

    单条标杆 grid -> actuals：
      sign=+1 时 translation = actual - grid
      sign=-1 时 translation = actual + grid
    多条标杆的候选平移取交集；实际标杆位置由卡册保证落在 0..100。
    """

    solutions: List[AxisSolution] = []
    for sign in (+1, -1):
        translations: Set[int] | None = None
        for pair in spec.benchmarks:
            if sign == +1:
                cand = {a - pair.grid for a in pair.actuals}
            else:
                cand = {a + pair.grid for a in pair.actuals}
            translations = cand if translations is None else translations & cand
            if not translations:
                break
        if translations:
            solutions.extend(AxisSolution(sign, t) for t in sorted(translations))
    return solutions


def solve_offsets(spec: CameraSpec) -> Tuple[Set[int], List[AdmissionFailure]]:
    """由同步灯卡求相容时钟偏移，再与卡册限定（至多五个值）取交。"""

    failures: List[AdmissionFailure] = []
    allowed: Set[int] | None = None
    for sighting in spec.flashes:
        # camera_tick = official + offset  =>  offset = camera_tick - official
        cand = {sighting.camera_tick - b for b in sighting.allowed_beats}
        allowed = cand if allowed is None else allowed & cand
        if not cand:
            failures.append(AdmissionFailure(
                AdmissionCode.SYNC_OUT_OF_RANGE, spec.camera_id,
                f"同步灯 {sighting.flash_id} 的允许拍为空"))
            return set(), failures
    if allowed is not None and not allowed:
        failures.append(AdmissionFailure(
            AdmissionCode.SYNC_OUT_OF_RANGE, spec.camera_id,
            "多张同步灯卡的相容偏移不相交"))
        return set(), failures
    candidates = set(spec.offset_album) if allowed is None else allowed & set(
        spec.offset_album)
    if not candidates:
        failures.append(AdmissionFailure(
            AdmissionCode.NO_OFFSET_IN_ALBUM, spec.camera_id,
            f"同步灯相容偏移 {sorted(allowed or set())} 均不在卡册 "
            f"{sorted(spec.offset_album)} 内"))
    return candidates, failures


def _flash_official_beat(spec: CameraSpec, flash_id: str, offset: int) -> int:
    sighting = next(f for f in spec.flashes if f.flash_id == flash_id)
    return sighting.camera_tick - offset


def admit(specs: Sequence[CameraSpec]) -> AdmissionResult:
    """全机校场：返回全部相容联合解释；为空时附逐条失败原因。"""

    failures: List[AdmissionFailure] = []
    per_camera: List[List[Tuple[AxisSolution, int]]] = []

    for spec in specs:
        axes = solve_axis(spec)
        if not axes:
            failures.append(AdmissionFailure(
                AdmissionCode.NO_AXIS_SOLUTION, spec.camera_id,
                "标杆卡不存在共同的方向与整数平移"))
            per_camera.append([])
            continue
        offsets, offset_failures = solve_offsets(spec)
        failures.extend(offset_failures)
        per_camera.append([(axis, off) for axis in axes for off in offsets])

    if any(not options for options in per_camera):
        return AdmissionResult(False, (), tuple(failures))

    shared_flashes: Dict[str, List[CameraSpec]] = {}
    for spec in specs:
        for sighting in spec.flashes:
            shared_flashes.setdefault(sighting.flash_id, []).append(spec)
    shared_flashes = {k: v for k, v in shared_flashes.items() if len(v) > 1}

    interpretations: List[JointInterpretation] = []
    flash_failed: Set[str] = set()
    for combo in product(*per_camera):
        axes = {spec.camera_id: pair[0]
                for spec, pair in zip(specs, combo)}
        offsets = {spec.camera_id: pair[1]
                   for spec, pair in zip(specs, combo)}
        ok = True
        for flash_id, holders in shared_flashes.items():
            beats = {_flash_official_beat(h, flash_id, offsets[h.camera_id])
                     for h in holders}
            if len(beats) != 1:
                ok = False
                flash_failed.add(flash_id)
        if ok:
            interpretations.append(JointInterpretation(axes, offsets))

    if not interpretations:
        for flash_id in sorted(flash_failed):
            holders = [s.camera_id for s in shared_flashes[flash_id]]
            failures.append(AdmissionFailure(
                AdmissionCode.FLASH_CONFLICT, None,
                f"闪灯 {flash_id} 在 {holders} 上映射不到同一正式拍"))
        return AdmissionResult(False, (), tuple(failures))

    # 去重（不同枚举路径可能给出同一解释）
    unique = {(tuple(sorted(j.axes.items())), tuple(sorted(j.offsets.items())))
              for j in interpretations}
    deduped = tuple(
        JointInterpretation(dict(axis_items), dict(offset_items))
        for axis_items, offset_items in sorted(
            unique, key=lambda x: (str(x[1]), str(x[0]))))
    return AdmissionResult(True, deduped, tuple(failures))
