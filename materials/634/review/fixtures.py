"""题面场景的虚构素材夹具。

主线（示例 1，进攻向右增大）：
- 两名具名防守人 D1=90、D2=60，球 ball=50；
- 接应人 R 在机内第 4 拍为 61、第 5 拍为 60；
- 两台机都已按标杆定出横轴（sign=+1, translation=0）；
- 正式第 5 拍出球，机钟原可差零或 -1 拍（机内拍=正式拍+offset）
  -> 结论待核；
- 追加真实同步卡把正式 5 锁到机内 4 -> 立刻确认越线；
- 保持校场产品仅把审查拍改成正式 6（对应机内 5）-> 平齐 -> 驳回。
另给闪灯对不上的矛盾素材，以及双方换端后的镜像例（反向横轴）。
"""

from __future__ import annotations

from typing import Dict, FrozenSet, List, Sequence, Tuple

from .model import (AttackEnd, BenchmarkPair, CameraSpec, CaseFile,
                    FlashSighting, FrameObservation)

HOME = "HOME-FC"
AWAY = "AWAY-XI"
DEFENDERS = ("D1", "D2")
RECEIVER = "R"
PASSER = "P"
BALL = "ball"

# 正式拍 -> 真位置（示例 1；格号==真位置，因 sign=+1/t=0）
TRUE_TRACKS_EX1: Dict[str, Dict[int, int]] = {
    "D1": {4: 90, 5: 90, 6: 90},
    "D2": {4: 60, 5: 60, 6: 60},
    "ball": {4: 50, 5: 50, 6: 50},
    "P": {4: 40, 5: 40, 6: 40},
    "R": {4: 61, 5: 60, 6: 60},
}


def _benchmarks_identity() -> Tuple[BenchmarkPair, ...]:
    # 格 10 必在真 10、格 90 必在真 90：唯一解 sign=+1/t=0
    return (BenchmarkPair(10, frozenset({10})),
            BenchmarkPair(90, frozenset({90})))


def _benchmarks_mirrored() -> Tuple[BenchmarkPair, ...]:
    # 同一批物理标杆，反向机：格 90 对应真 10、格 10 对应真 90 -> sign=-1/t=100
    return (BenchmarkPair(90, frozenset({10})),
            BenchmarkPair(10, frozenset({90})))


def _observations(camera_id: str, tracks: Dict[str, Dict[int, int]],
                  ticks: Sequence[int], grid_fn) -> List[FrameObservation]:
    obs: List[FrameObservation] = []
    for label, by_beat in tracks.items():
        for beat in ticks:
            if beat in by_beat:
                obs.append(FrameObservation(
                    camera_id, beat, label, grid_fn(by_beat[beat])))
    return obs


def cameras_ex1(with_sync_card: bool) -> Tuple[CameraSpec, CameraSpec]:
    """两台正向机。with_sync_card=False 时只给卡册 {0,1}，不给闪灯。"""

    flashes: Tuple[FlashSighting, ...] = ()
    if with_sync_card:
        # 真实同步卡：正式 5 锁到机内 4（两台同见闪灯 F1）
        flashes = (FlashSighting("F1", 4, frozenset({5})),)
    specs = []
    for cid in ("CAM-A", "CAM-B"):
        specs.append(CameraSpec(
            camera_id=cid, benchmarks=_benchmarks_identity(),
            flashes=flashes, offset_album=frozenset({0, -1})))
    return tuple(specs)  # type: ignore[return-value]


def _case(case_id: str, half: int, end: AttackEnd,
          cameras: Sequence[CameraSpec],
          observations: Sequence[FrameObservation],
          release_beats: Sequence[int]) -> CaseFile:
    return CaseFile(
        match_id=case_id, half=half, home_team=HOME, away_team=AWAY,
        home_attack_end=end, cameras=tuple(cameras),
        observations=tuple(observations),
        release_beats=frozenset(release_beats), passer=PASSER,
        receiver=RECEIVER, defender_labels=DEFENDERS, ball_label=BALL)


def pending_case() -> CaseFile:
    """场景一：无同步灯，偏移 0/1 都可能，正式拍 5 审查 -> 待核。"""

    cams = cameras_ex1(with_sync_card=False)
    obs = [o for cid in ("CAM-A", "CAM-B")
           for o in _observations(cid, TRUE_TRACKS_EX1, (4, 5, 6), lambda x: x)]
    return _case("m1-h1", 1, AttackEnd.HIGH, cams, obs, (5,))


def confirmed_case() -> CaseFile:
    """场景二：追加真实同步卡（正式5->机内4），正式拍 5 -> 确认越线。"""

    cams = cameras_ex1(with_sync_card=True)
    obs = [o for cid in ("CAM-A", "CAM-B")
           for o in _observations(cid, TRUE_TRACKS_EX1, (4, 5, 6), lambda x: x)]
    return _case("m1-h1", 1, AttackEnd.HIGH, cams, obs, (5,))


def rejected_case() -> CaseFile:
    """场景三：同一校场产品，裁判改审正式 6（机内 5），平齐 -> 驳回。"""

    cams = cameras_ex1(with_sync_card=True)
    obs = [o for cid in ("CAM-A", "CAM-B")
           for o in _observations(cid, TRUE_TRACKS_EX1, (4, 5, 6), lambda x: x)]
    return _case("m1-h1", 1, AttackEnd.HIGH, cams, obs, (6,))


def conflicting_flash_case() -> CaseFile:
    """场景四：两台机各自说得通，但同一闪灯对不上同一正式拍 -> 不准入。"""

    # A：F1 机内 4 只允许正式 4 -> 锁定 offset=0（在册 {0,-1}）
    cam_a = CameraSpec(
        camera_id="CAM-A", benchmarks=_benchmarks_identity(),
        flashes=(FlashSighting("F1", 4, frozenset({4})),),
        offset_album=frozenset({0, -1}))
    # B：F1 机内 4 只允许正式 5 -> 锁定 offset=-1（在册 {-1,0}）
    cam_b = CameraSpec(
        camera_id="CAM-B", benchmarks=_benchmarks_identity(),
        flashes=(FlashSighting("F1", 4, frozenset({5})),),
        offset_album=frozenset({-1, 0}))
    obs = [o for cid in ("CAM-A", "CAM-B")
           for o in _observations(cid, TRUE_TRACKS_EX1, (4, 5, 6), lambda x: x)]
    # A 锁定 offset=0 -> F1 正式拍 4；B 锁定 offset=-1 -> F1 正式拍 5：冲突
    return _case("m2-h1", 1, AttackEnd.HIGH, (cam_a, cam_b), obs, (5,))


def mirrored_case() -> CaseFile:
    """场景五：换边镜像例。主队改攻 LOW 端，机位横轴反向。

    反向机 grid = 100 - true；接应真机内 4/5 拍格号 39/40（真 61/60）。
    """

    tracks_grid: Dict[str, Dict[int, int]] = {
        label: {beat: 100 - pos for beat, pos in by_beat.items()}
        for label, by_beat in TRUE_TRACKS_EX1.items()}
    cams = tuple(
        CameraSpec(camera_id=cid, benchmarks=_benchmarks_mirrored(),
                   flashes=(FlashSighting("F1", 4, frozenset({5})),),
                   offset_album=frozenset({0, -1}))
        for cid in ("CAM-A", "CAM-B"))
    obs = [o for cid in ("CAM-A", "CAM-B")
           for o in _observations(cid, tracks_grid, (4, 5, 6), lambda x: x)]
    # 客队进攻方向（sign=+1）审查；出球拍 5 确认、拍 6 平齐驳回
    return _case("m1-h2", 2, AttackEnd.LOW, cams, obs, (5,))
