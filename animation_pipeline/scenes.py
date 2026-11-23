"""袖珍彩格场景：本地生成“原目录”小动图，供场记读法与补拍单端到端演示。

场景全部走本仓库的 :class:`animation_pipeline.catalog.FrameCatalog`，由
:class:`animation_pipeline.compositor.FramePlayer` 合成为真实显示画布；场记读法
只读合成后的画布。规模约束（题面）：最多 5 只偶、12 个画格、3 台相机。

示例场景精确对上题面：

* **首帧**只画红左格（frame 0）；
* **次帧**局部只添红右格（frame 1），两格相接；合成后读到“双格红姿势”；
* **随后一帧全透明**（frame 2）。按合成合同，透明像素不覆盖旧像素，所以
  frame 2 的**显示画布仍是双格红**——后两帧时点原本均读到双格红姿势；
* 唯一的实偶已封在双格姿势、不能再换，但前后两面能各拍一张（同拍）。

两个对照变体（用于演示“补不齐/拒收”）：

* ``variant_clear``：把**次帧**处置改成 2（清该局部=右格）。次帧显示仍是双格，
  但末帧起始画布只剩左格、又全透明不再覆盖，故末帧只读到左格——片序不变，
  却补不齐（末帧读法与工单双格不符）。
* ``variant_no_back``：恢复处置，仅把第二个机位换成必须看偶背面、而该姿势无
  背饰的机位——即便读法正确，也因无背饰拒收。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .catalog import FrameCatalog
from .compositor import DisplayFrame, FramePlayer
from .puppets import Hole, Mark, ORIENTATION_LEFT, ORIENTATION_RIGHT, Pattern, Seed
from .shoot import Camera, FrameJob, RealPuppet

RED = (220, 40, 40)

# 双格红姿势：相对定位孔 (0,0) 的 (0,0) 与 (1,0)，凹口取右格；正背各有服饰。
RED_DOUBLE = Pattern(
    name="red_double",
    cells=frozenset({(0, 0), (1, 0)}),
    notch=(1, 0),
    front_costume="red-front",
    back_costume="red-back",
    orientation=ORIENTATION_LEFT,
)
# 单格红（只剩左格），无背面服饰。
RED_LEFT_ONLY = Pattern(
    name="red_left_only",
    cells=frozenset({(0, 0)}),
    notch=(0, 0),
    front_costume="red-front",
    back_costume=None,
    orientation=ORIENTATION_LEFT,
)
# 双格红但**无背饰**（variant_no_back 用）：同一姿势名、同一整形，仅缺背饰。
RED_DOUBLE_NO_BACK = Pattern(
    name="red_double",
    cells=frozenset({(0, 0), (1, 0)}),
    notch=(1, 0),
    front_costume="red-front",
    back_costume=None,
    orientation=ORIENTATION_LEFT,
)

HOLE = Hole("holeA", 0, 1)


@dataclass
class Scene:
    catalog: FrameCatalog
    frames: list[DisplayFrame]
    marks_by_frame: dict[int, tuple[Mark, ...]]
    patterns: dict[str, Pattern]
    puppets: tuple[RealPuppet, ...]
    binding: dict[str, str]
    cameras: dict[str, Camera]


def _solid(cat: FrameCatalog, rect, rgb, delay=10, disposal=1) -> int:
    left, top, width, height = rect
    body = np.zeros((height, width, 4), dtype=np.uint8)
    body[:, :, :3] = rgb
    body[:, :, 3] = 255
    return cat.add_frame(rect, body, delay, disposal)


def _transparent(cat: FrameCatalog, rect, delay=10, disposal=2) -> int:
    left, top, width, height = rect
    body = np.zeros((height, width, 4), dtype=np.uint8)  # 全透明
    return cat.add_frame(rect, body, delay, disposal)


def _build(second_frame_disposal: int, double_pattern: Pattern = RED_DOUBLE) -> Scene:
    """红左格 → 添红右格 → 全透明。``second_frame_disposal`` 可改次帧处置。"""
    cat = FrameCatalog(6, 4, background_rgb=(0, 0, 0))
    _solid(cat, (0, 1, 1, 1), RED, delay=10, disposal=1)         # frame 0 红左格
    _solid(cat, (1, 1, 1, 1), RED, delay=10, disposal=second_frame_disposal)  # frame 1 添红右格
    _transparent(cat, (0, 1, 2, 1), delay=10, disposal=2)        # frame 2 全透明
    frames = list(FramePlayer(cat).replay())

    seed = Seed(puppet="redpup", x=0, y=1, color=RED)
    marks = (Mark(seed, HOLE, frozenset({ORIENTATION_LEFT, ORIENTATION_RIGHT})),)
    marks_by_frame = {1: marks, 2: marks}

    # 唯一实偶已封在双格姿势，不能再换。
    puppet = RealPuppet(
        name="realRed", model="redpup", pose="red_double", sealed=True, transitions=(),
    )
    cameras = {
        "camFront": Camera("camFront", "front"),
        "camBack": Camera("camBack", "back"),
        "camBack2": Camera("camBack2", "back"),
    }
    patterns = {double_pattern.name: double_pattern, "red_left_only": RED_LEFT_ONLY}
    return Scene(
        catalog=cat,
        frames=frames,
        marks_by_frame=marks_by_frame,
        patterns=patterns,
        puppets=(puppet,),
        binding={"redpup": "realRed"},
        cameras=cameras,
    )


def build_example_scene() -> Scene:
    """基线：次帧处置 1（保留），后两帧时点均读到双格红。"""
    return _build(second_frame_disposal=1)


def build_variant_clear_scene() -> Scene:
    """对照：次帧处置改为 2（清右格），末帧只剩左格。"""
    return _build(second_frame_disposal=2)


def build_variant_no_back_scene() -> Scene:
    """对照：恢复处置，但双格纸样无背饰（第二机位须看背面）。"""
    return _build(second_frame_disposal=1, double_pattern=RED_DOUBLE_NO_BACK)


def base_jobs() -> tuple[FrameJob, ...]:
    """基线工单：后两帧各补一次正背同拍（双格红）。"""
    return (
        FrameJob(1, 10, "redpup", "red_double", ("camFront", "camBack"), simultaneous=True),
        FrameJob(2, 20, "redpup", "red_double", ("camFront", "camBack"), simultaneous=True),
    )


def no_back_jobs() -> tuple[FrameJob, ...]:
    """对照工单：恢复处置，仅把第二个机位换成必须看背面（该姿势无背饰）。"""
    return (
        FrameJob(1, 10, "redpup", "red_double", ("camFront", "camBack2"),
                 simultaneous=True),
        FrameJob(2, 20, "redpup", "red_double", ("camFront", "camBack2"),
                 simultaneous=True),
    )


# --------------------------------------------------------------------------- #
# 第二张工单：首幕正背同拍、次幕只要正面新姿势
# --------------------------------------------------------------------------- #

# 首幕姿势：双格红，正背都有服饰。次幕姿势：再添左下格，只有正面服饰。
ACT1_POSE = Pattern(
    name="act1", cells=frozenset({(0, 0), (1, 0)}), notch=(1, 0),
    front_costume="act1-front", back_costume="act1-back", orientation=ORIENTATION_LEFT,
)
ACT2_POSE = Pattern(
    name="act2", cells=frozenset({(0, 0), (1, 0), (0, 1)}), notch=(0, 1),
    front_costume="act2-front", back_costume=None, orientation=ORIENTATION_LEFT,
)


@dataclass
class TwoActScene:
    catalog: FrameCatalog
    frames: list[DisplayFrame]
    marks_by_frame: dict[int, tuple[Mark, ...]]
    patterns: dict[str, Pattern]
    puppets: tuple[RealPuppet, ...]
    binding: dict[str, str]
    cameras: dict[str, Camera]


def build_two_act_scene() -> TwoActScene:
    """首幕（frame 0）双格红=act1；次幕（frame 1）添左下格=act2 新姿势。

    唯一实偶的转换：``None -> act1`` 与 ``act1 -> act2`` 都需拍封。于是：

    * 首幕把它封在 act1，正背同拍首幕可出；
    * 次幕要 act2，但实偶已封在 act1、换不过去，又只有这一只、退框就没人——
      只能拒收次幕，并追到首幕那一次纸封。
    """
    cat = FrameCatalog(6, 4, background_rgb=(0, 0, 0))
    # 首幕一帧画齐 act1 双格（左 (0,1) + 右 (1,1)）。
    _solid(cat, (0, 1, 2, 1), RED, delay=10, disposal=1)                # act1 双格
    # 次幕添左下格 (0,2)，合成后成 act2 三格。
    _solid(cat, (0, 2, 1, 1), RED, delay=10, disposal=1)                # act2 新姿势
    frames = list(FramePlayer(cat).replay())

    seed = Seed(puppet="redpup", x=0, y=1, color=RED)
    marks = (Mark(seed, HOLE, frozenset({ORIENTATION_LEFT, ORIENTATION_RIGHT})),)
    marks_by_frame = {0: marks, 1: marks}

    puppet = RealPuppet(
        name="realRed", model="redpup", pose=None, sealed=False,
        transitions=((None, "act1", True), ("act1", "act2", True)),
    )
    cameras = {
        "camFront": Camera("camFront", "front"),
        "camBack": Camera("camBack", "back"),
    }
    patterns = {"act1": ACT1_POSE, "act2": ACT2_POSE}
    return TwoActScene(
        catalog=cat, frames=frames, marks_by_frame=marks_by_frame,
        patterns=patterns, puppets=(puppet,), binding={"redpup": "realRed"},
        cameras=cameras,
    )


def two_act_jobs() -> tuple[FrameJob, ...]:
    """首幕正背同拍 act1，次幕只要正面 act2。"""
    return (
        FrameJob(0, 0, "redpup", "act1", ("camFront", "camBack"), simultaneous=True),
        FrameJob(1, 10, "redpup", "act2", ("camFront",), simultaneous=False),
    )


# --------------------------------------------------------------------------- #
# 多偶场景：4 只偶、3 台相机、多画格；含“两偶连为同一色块”
# --------------------------------------------------------------------------- #

GREEN = (40, 180, 60)
BLUE = (50, 80, 220)


@dataclass
class CrowdScene:
    catalog: FrameCatalog
    frames: list[DisplayFrame]
    marks_by_frame: dict[int, tuple[Mark, ...]]
    patterns: dict[str, Pattern]
    puppets: tuple[RealPuppet, ...]
    binding: dict[str, str]
    cameras: dict[str, Camera]


# 形状各不相同，避免形状互串：红=横双格、绿=竖双格、蓝=L 三格；均左右不对称。
CROWD_RED = Pattern("crowd_red", frozenset({(0, 0), (1, 0)}), notch=(1, 0),
                    front_costume="r-f", back_costume="r-b", orientation=ORIENTATION_LEFT)
CROWD_GREEN = Pattern("crowd_green", frozenset({(0, 0), (0, 1), (-1, 1)}), notch=(-1, 1),
                      front_costume="g-f", back_costume=None,
                      orientation=ORIENTATION_LEFT)
CROWD_BLUE = Pattern("crowd_blue", frozenset({(0, 0), (1, 0), (0, 1)}), notch=(1, 0),
                     front_costume="b-f", back_costume="b-b", orientation=ORIENTATION_LEFT)


def build_crowd_scene() -> CrowdScene:
    """3 只偶 + 1 只同款备份、3 台相机、2 个画格；两只红偶在 frame 1 合并。

    * frame 0：红横双格、绿竖双格、蓝 L 三格，各可读、互不相接；
    * frame 1：把 row0 连成一条红，两只红偶落入同一色块 → 合并，都不发纸样。
    规模在题面上限内（≤5 偶、≤12 格、≤3 相机）。
    """
    cat = FrameCatalog(8, 5, background_rgb=(0, 0, 0))

    def paint(cells, delay=10, disposal=1):
        body = np.zeros((5, 8, 4), dtype=np.uint8)
        for (x, y), rgb in cells:
            body[y, x, :3] = rgb
            body[y, x, 3] = 255
        cat.add_frame((0, 0, 8, 5), body, delay, disposal)

    # frame 0：红横双格 (0,0)(1,0)；绿 L (2,2)(2,3)(1,3)；蓝 L (4,2)(5,2)(4,3)。
    paint([((0, 0), RED), ((1, 0), RED),
           ((2, 2), GREEN), ((2, 3), GREEN), ((1, 3), GREEN),
           ((4, 2), BLUE), ((5, 2), BLUE), ((4, 3), BLUE)])
    # frame 1：row0 (2,0)..(6,0) 连成红条，两只红偶（孔 (0,0) 与 (6,0)）并入同一块。
    paint([((2, 0), RED), ((3, 0), RED), ((4, 0), RED), ((5, 0), RED), ((6, 0), RED)])
    frames = list(FramePlayer(cat).replay())

    both = frozenset({ORIENTATION_LEFT, ORIENTATION_RIGHT})
    marks = (
        Mark(Seed("redpup", 0, 0, RED), Hole("hr", 0, 0), both),
        Mark(Seed("redpup2", 6, 0, RED), Hole("hr2", 6, 0), both),
        Mark(Seed("greenpup", 2, 2, GREEN), Hole("hg", 2, 2), both),
        Mark(Seed("bluepup", 4, 2, BLUE), Hole("hb", 4, 2), both),
    )
    marks_by_frame = {0: marks, 1: marks}

    puppets = (
        RealPuppet("r-real", "redpup", pose="crowd_red", sealed=True),
        RealPuppet("g-real", "greenpup", pose="crowd_green", sealed=True),
        RealPuppet("b-real", "bluepup", pose="crowd_blue", sealed=True),
        RealPuppet("b-real2", "bluepup", pose="crowd_blue", sealed=True),  # 同款备份
    )
    cameras = {
        "cf": Camera("cf", "front"),
        "cb": Camera("cb", "back"),
        "cf2": Camera("cf2", "front"),
    }
    patterns = {
        "crowd_red": CROWD_RED,
        "crowd_green": CROWD_GREEN,
        "crowd_blue": CROWD_BLUE,
    }
    return CrowdScene(
        catalog=cat, frames=frames, marks_by_frame=marks_by_frame,
        patterns=patterns, puppets=puppets,
        binding={"redpup": "r-real", "redpup2": "r-real",
                 "greenpup": "g-real", "bluepup": "b-real"},
        cameras=cameras,
    )
