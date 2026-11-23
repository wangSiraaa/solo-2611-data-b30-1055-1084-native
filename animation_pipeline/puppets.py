"""场记读法：把“合成完的显示画布”读成可核对的纸样姿势。

本模块只消费 :class:`animation_pipeline.compositor.DisplayFrame` 已经合成好的
``canvas``，不重新解释 GIF 处置，也不看原始局部正文。因此“短暂遮住后露出的
造型仍按那一刻的显示画面读取”是天然满足的：某一时刻被别的东西挡住，就读不到；
下一时刻重新露出，就按那一时刻画布上真实显示的身形读取，绝不回填被遮住前的
记忆。

读取步骤（与题面逐条对应）：

1. **种子定色**：场记在某一播放毫秒处，给每只偶一个种子像素与色样。
2. **同色四邻身形**：从种子出发，沿 4 邻域只收同色且不透明的像素，得到该偶的
   身形连通块。
3. **合并色块不给两张纸样**：若两只偶的种子落进同一个连通块（同色相接），
   视为“连为同一个色块”，两块都不发纸样，只记一条合并告警。
4. **对齐具名定位孔**：把身形像素减去定位孔坐标，得到相对定位孔的局部坐标。
5. **比对纸样**：纸样是一组相对定位孔的局部格，带朝向（左右面孔）与正反两面
   服饰，并用一个凹口格打破对称。整形（格集）、朝向（镜像与否）、定位（相对
   孔）三者能同时解释，才算一条确定读法；能同时解释的纸样多于一条时，留成
   “成组替选”并给出复查依据；一条都解释不了则不给纸样。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np

Cell = tuple[int, int]

ORIENTATION_LEFT = "left"
ORIENTATION_RIGHT = "right"
_OPPOSITE = {ORIENTATION_LEFT: ORIENTATION_RIGHT, ORIENTATION_RIGHT: ORIENTATION_LEFT}


@dataclass(frozen=True)
class Hole:
    """具名定位孔：画布上的绝对坐标，也是纸样局部坐标的原点。"""

    name: str
    x: int
    y: int


@dataclass(frozen=True)
class Pattern:
    """一张姿势纸样。

    * ``cells``：相对定位孔的局部格集合，决定整形与定位；
    * ``notch``：凹口格（必须属于 ``cells``），用于在镜像下区分左右面孔；
    * ``front_costume`` / ``back_costume``：正反两面各自的服饰；某面为 ``None``
      表示该面无服饰，任何要拍那一面的机位都收不来；
    * ``orientation``：纸样正面朝向（左或右），镜像读取时取反。
    """

    name: str
    cells: frozenset[Cell]
    notch: Cell
    front_costume: str | None
    back_costume: str | None
    orientation: str = ORIENTATION_LEFT

    def __post_init__(self) -> None:
        if self.notch not in self.cells:
            raise ValueError(f"纸样 {self.name} 的凹口 {self.notch} 不在整形格集内")
        if self.orientation not in _OPPOSITE:
            raise ValueError(f"纸样 {self.name} 朝向非法：{self.orientation}")

    def costume_for(self, side: str) -> str | None:
        if side == "front":
            return self.front_costume
        if side == "back":
            return self.back_costume
        raise ValueError(f"未知的机位面 {side!r}")

    def mirrored(self) -> "Pattern":
        """沿定位孔竖轴镜像：格集 x 取反，朝向取反，凹口随格一起镜像。"""
        return Pattern(
            name=self.name,
            cells=frozenset((-x, y) for x, y in self.cells),
            notch=(-self.notch[0], self.notch[1]),
            front_costume=self.front_costume,
            back_costume=self.back_costume,
            orientation=_OPPOSITE[self.orientation],
        )


@dataclass(frozen=True)
class Seed:
    """场记在某一毫秒处标定的种子：它属于哪只偶、在画布哪里、认什么色。"""

    puppet: str
    x: int
    y: int
    color: tuple[int, int, int]


@dataclass(frozen=True)
class Mark:
    """一只偶在这一毫秒的完整标定：种子、具名定位孔、允许的左右面孔。"""

    seed: Seed
    hole: Hole
    allowed_orientations: frozenset[str] = frozenset(
        (ORIENTATION_LEFT, ORIENTATION_RIGHT)
    )


@dataclass(frozen=True)
class Interpretation:
    """一条能解释身形的纸样读法（一个候选）。"""

    pattern: Pattern
    orientation: str
    mirrored: bool

    @property
    def review_key(self) -> str:
        side = "镜像" if self.mirrored else "原样"
        return f"{self.pattern.name}/{self.orientation}/{side}"


@dataclass
class PuppetReading:
    """单只偶在某一毫秒的读取结果。"""

    puppet: str
    hole: str
    visible: bool
    merged: bool
    body_cells: frozenset[Cell] = frozenset()  # 相对定位孔的局部坐标
    alternatives: tuple[Interpretation, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def determinate(self) -> bool:
        return (
            self.visible
            and not self.merged
            and len(self.alternatives) == 1
        )

    @property
    def pose(self) -> Pattern | None:
        """确定的姿势纸样；不确定或不可读时返回 ``None``。"""
        return self.alternatives[0].pattern if self.determinate else None

    @property
    def review_basis(self) -> str:
        """复查依据：确定/替选/不可读分别给一句话。"""
        if not self.visible:
            return "种子处同色像素不可见（被遮住或不在画面），无法读取"
        if self.merged:
            return "两只偶连为同一个色块，按规则不给任一偶发纸样"
        if not self.alternatives:
            return "没有任何纸样的整形/朝向/定位能同时解释画格"
        if len(self.alternatives) > 1:
            keys = "；".join(a.review_key for a in self.alternatives)
            return f"多条纸样同时可解释，留作成组替选：{keys}"
        return f"唯一可解释读法：{self.alternatives[0].review_key}"


@dataclass
class SceneReading:
    """某一毫秒、某一帧的整场读法。"""

    frame_id: int
    time_ms: int
    puppets: dict[str, PuppetReading] = field(default_factory=dict)

    def determinate_poses(self) -> dict[str, Pattern]:
        return {
            name: r.pose  # type: ignore[dict-item]
            for name, r in self.puppets.items()
            if r.determinate
        }


# --------------------------------------------------------------------------- #
# 分割与读取
# --------------------------------------------------------------------------- #

def _component(canvas: np.ndarray, seed: Seed, width: int, height: int) -> frozenset[Cell]:
    """从种子出发的同色四邻连通块（不透明像素）。空集表示种子不可见。"""
    x0, y0 = seed.x, seed.y
    if not (0 <= x0 < width and 0 <= y0 < height):
        return frozenset()
    r, g, b = seed.color
    at = canvas[y0, x0]
    if at[3] == 0 or (int(at[0]), int(at[1]), int(at[2])) != (r, g, b):
        return frozenset()

    seen: set[Cell] = {(x0, y0)}
    queue: deque[Cell] = deque([(x0, y0)])
    color = np.array([r, g, b], dtype=np.uint8)
    while queue:
        x, y = queue.popleft()
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if not (0 <= nx < width and 0 <= ny < height) or (nx, ny) in seen:
                continue
            px = canvas[ny, nx]
            if px[3] == 0:
                continue
            if np.array_equal(px[:3], color):
                seen.add((nx, ny))
                queue.append((nx, ny))
    return frozenset(seen)


def _match(
    body: frozenset[Cell],
    marks: Mark,
    patterns: tuple[Pattern, ...],
) -> tuple[Interpretation, ...]:
    """把身形局部坐标与每张纸样的原样/镜像做整形+朝向+定位比对。"""
    hits: list[Interpretation] = []
    for pat in patterns:
        if body == pat.cells and pat.orientation in marks.allowed_orientations:
            hits.append(Interpretation(pat, pat.orientation, mirrored=False))
        mirror = pat.mirrored()
        if body == mirror.cells and mirror.orientation in marks.allowed_orientations:
            hits.append(Interpretation(pat, mirror.orientation, mirrored=True))
    # 同一张纸样若原样与镜像都成立（对称且两向都允许），两条都保留为替选。
    return tuple(hits)


def read_scene(
    canvas: np.ndarray,
    marks: tuple[Mark, ...],
    patterns: tuple[Pattern, ...],
    *,
    frame_id: int,
    time_ms: int,
) -> SceneReading:
    """按一组标定读取一张显示画布，返回整场读法。"""
    height, width = canvas.shape[:2]
    comps = {m.seed.puppet: _component(canvas, m.seed, width, height) for m in marks}

    # 同一连通块被多只偶认领 → 连为同一色块，都不发纸样。
    owners: dict[frozenset[Cell], list[str]] = {}
    for name, comp in comps.items():
        if comp:
            owners.setdefault(comp, []).append(name)

    reading = SceneReading(frame_id=frame_id, time_ms=time_ms)
    for m in marks:
        name = m.seed.puppet
        comp = comps[name]
        visible = bool(comp)
        merged = visible and len(owners[comp]) > 1
        local = frozenset((x - m.hole.x, y - m.hole.y) for x, y in comp)
        notes: list[str] = []
        if merged:
            others = [o for o in owners[comp] if o != name]
            notes.append(f"与 {', '.join(sorted(others))} 连成同一色块")
            alternatives: tuple[Interpretation, ...] = ()
        elif not visible:
            alternatives = ()
        else:
            alternatives = _match(local, m, patterns)
        reading.puppets[name] = PuppetReading(
            puppet=name,
            hole=m.hole.name,
            visible=visible,
            merged=merged,
            body_cells=local,
            alternatives=alternatives,
            notes=tuple(notes),
        )
    return reading
