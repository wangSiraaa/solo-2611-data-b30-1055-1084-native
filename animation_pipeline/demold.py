"""透明教具开模 / 共轴顶排 / 真实扫过形状核验。

本模块建立在既有动画管线上：它**不**自己合成画面，而是用
``FrameCatalog`` + ``FramePlayer`` 读取“实际播放毫秒对应的完整显示画布”，
再把画布上的 RGB 按颜色角色解释成模壁 / 倒影 / 空位。

角色口径
--------
* 两组模壁各占一组 RGB（``wall_a`` / ``wall_b``），任一组格子都挡糖。
* 倒影色 ``reflection`` 不占位，永不挡糖。
* 其余颜色（含背景）一律视为空位。

模腔与顶排
----------
* ``Cavity``：一颗糖的初始小形状（绝对格集合）、顶针平移方向与步数。
  整趟平移 ``k = 0..steps`` 扫过的格子并集是 **swept**；只要 swept 与任一
  模壁相交，本拍就不能脱。
* ``CoaxialRow``：多腔共用一支不可分离的顶针。任一“尚有糖”的腔碰壁，
  本拍整排都不走；能顶就必须顶；已落的糖不会二次顶出。
* 脉冲表：每排给若干可选拍（毫秒）表，选一张；只在表定拍读该刻画面并尝试顶出。

时间一律用整数毫秒。帧时间轴由目录每帧延时（百分之一秒，0 按 1 处理）
换算得到。
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .catalog import FrameCatalog
from .compositor import FramePlayer, effective_delay_centis

RGB = tuple[int, int, int]
Cell = tuple[int, int]

# 合同上限：最多四腔、六帧、两盘。
MAX_CAVITIES = 4
MAX_FRAMES = 6
MAX_TRAYS = 2


# --------------------------------------------------------------------------- #
# 颜色角色
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class FrameRoles:
    """把显示画布的 RGB 解释成角色。其余颜色一律为空位。"""

    wall_a: frozenset[RGB] = frozenset()
    wall_b: frozenset[RGB] = frozenset()
    reflection: frozenset[RGB] = frozenset()

    def role(self, rgb: RGB) -> str:
        if rgb in self.wall_a:
            return "wall_a"
        if rgb in self.wall_b:
            return "wall_b"
        if rgb in self.reflection:
            return "reflection"
        return "free"

    def is_wall(self, rgb: RGB) -> bool:
        return rgb in self.wall_a or rgb in self.wall_b


# --------------------------------------------------------------------------- #
# 世界：按毫秒读真实色格
# --------------------------------------------------------------------------- #

class DemoldWorld:
    """把目录 + 播放器包装成“按毫秒读真实色格”的查询对象。"""

    def __init__(
        self,
        catalog: FrameCatalog,
        roles: FrameRoles,
        player: FramePlayer | None = None,
    ) -> None:
        if catalog.frame_count() > MAX_FRAMES:
            raise ValueError(f"合同最多 {MAX_FRAMES} 帧，本目录有 {catalog.frame_count()} 帧")
        self.catalog = catalog
        self.roles = roles
        self.player = player or FramePlayer(catalog)
        self._starts_ms: list[int] = []
        self._ends_ms: list[int] = []
        t = 0
        for meta in catalog.list_metadata():
            self._starts_ms.append(t)
            t += effective_delay_centis(meta["delay_centis"]) * 10
            self._ends_ms.append(t)
        self._end_ms = t
        self._canvas_cache: dict[int, object] = {}

    # -- 时间轴 ---------------------------------------------------------- #
    @property
    def end_ms(self) -> int:
        return self._end_ms

    @property
    def frame_starts_ms(self) -> tuple[int, ...]:
        return tuple(self._starts_ms)

    def frame_index_at_ms(self, time_ms: int) -> int | None:
        """半开区间选帧：恰在终点落到下一帧；末尾之后返回 None（不循环）。"""
        if not self._starts_ms or time_ms < 0 or time_ms >= self._end_ms:
            return None
        return max(bisect.bisect_right(self._starts_ms, time_ms) - 1, 0)

    def _canvas_at(self, time_ms: int):
        idx = self.frame_index_at_ms(time_ms)
        if idx is None:
            return None
        if idx not in self._canvas_cache:
            self._canvas_cache[idx] = self.player.display_at(idx).canvas
        return self._canvas_cache[idx]

    def rgb_at(self, time_ms: int, cell: Cell) -> RGB | None:
        canvas = self._canvas_at(time_ms)
        if canvas is None:
            return None
        x, y = cell
        if not (0 <= x < self.catalog.width and 0 <= y < self.catalog.height):
            return None
        arr = canvas
        return (int(arr[y, x, 0]), int(arr[y, x, 1]), int(arr[y, x, 2]))

    def role_at(self, time_ms: int, cell: Cell) -> str | None:
        rgb = self.rgb_at(time_ms, cell)
        return None if rgb is None else self.roles.role(rgb)

    def wall_cells_at(self, time_ms: int) -> set[Cell]:
        canvas = self._canvas_at(time_ms)
        if canvas is None:
            return set()
        walls: set[Cell] = set()
        roles = self.roles
        for y in range(self.catalog.height):
            row = canvas[y]
            for x in range(self.catalog.width):
                rgb = (int(row[x, 0]), int(row[x, 1]), int(row[x, 2]))
                if roles.is_wall(rgb):
                    walls.add((x, y))
        return walls


# --------------------------------------------------------------------------- #
# 模腔与顶排
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Cavity:
    """一颗糖的初始小形状 + 顶针方向 / 步数 + 落点与飞行时间。"""

    name: str
    cells: frozenset[Cell]          # 初始糖格（绝对坐标）
    direction: Cell                 # 逐格平移方向（整数格）
    steps: int                      # 顶针步数
    landing: int                    # 接盘轨道上的具名落点（整数坐标）
    flight_ms: int                  # 出模拍 + 飞行毫秒到达落点
    flavor: str                     # 品种

    def __post_init__(self) -> None:
        if self.steps < 0:
            raise ValueError("steps 必须为非负整数")

    def swept(self) -> frozenset[Cell]:
        """整趟平移扫过的所有格子（含起点与终点）。"""
        dx, dy = self.direction
        out: set[Cell] = set()
        for x, y in self.cells:
            for k in range(self.steps + 1):
                out.add((x + k * dx, y + k * dy))
        return frozenset(out)


@dataclass(frozen=True)
class CoaxialRow:
    """共用不可分离顶针的一组模腔 + 若干可选脉冲表（毫秒，升序）。"""

    name: str
    cavities: tuple[str, ...]
    pulse_tables: tuple[tuple[int, ...], ...]

    def __post_init__(self) -> None:
        if not self.pulse_tables:
            raise ValueError("顶排至少需要一张脉冲表")
        for table in self.pulse_tables:
            if list(table) != sorted(table):
                raise ValueError("脉冲表必须按时间升序")


# --------------------------------------------------------------------------- #
# 单腔核验（真实扫过形状 × 实际模壁坐标）
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class CellCheck:
    cell: Cell
    rgb: RGB | None
    role: str | None       # wall_a / wall_b / reflection / free / None(画面外)

    @property
    def is_wall(self) -> bool:
        return self.role in ("wall_a", "wall_b")


@dataclass(frozen=True)
class CavityCheck:
    cavity: str
    time_ms: int
    cells: tuple[CellCheck, ...]
    ok: bool
    first_block: CellCheck | None

    def blocked_by(self) -> tuple[Cell, RGB, str] | None:
        if self.first_block is None:
            return None
        return self.first_block.cell, self.first_block.rgb, self.first_block.role  # type: ignore[return-value]


def explain_cavity(world: DemoldWorld, cavity: Cavity, time_ms: int) -> CavityCheck:
    """按真实画面核对整趟扫过的每个格子是否碰壁。"""
    checks: list[CellCheck] = []
    first: CellCheck | None = None
    ok = True
    for cell in sorted(cavity.swept()):
        rgb = world.rgb_at(time_ms, cell)
        role = None if rgb is None else world.roles.role(rgb)
        cc = CellCheck(cell=cell, rgb=rgb, role=role)
        checks.append(cc)
        # 画面外的格子按“不可脱”处理（边界外视为挡）。
        if cc.is_wall or role is None:
            ok = False
            if first is None:
                first = cc
    return CavityCheck(cavity=cavity.name, time_ms=time_ms, cells=tuple(checks), ok=ok, first_block=first)


def cavity_can_demold(world: DemoldWorld, cavity: Cavity, time_ms: int) -> bool:
    return explain_cavity(world, cavity, time_ms).ok


# --------------------------------------------------------------------------- #
# 开模模拟
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class DemoldEvent:
    cavity: str
    row: str
    time_ms: int


@dataclass(frozen=True)
class PulseReport:
    row: str
    time_ms: int
    status: str              # no_sugar / blocked / pushed / delayed
    pushed: tuple[str, ...] = ()
    blocked: tuple[tuple[str, Cell, RGB | None, str | None], ...] = ()
    delayed: tuple[str, ...] = ()


@dataclass
class DemoldResult:
    events: list[DemoldEvent] = field(default_factory=list)
    reports: list[PulseReport] = field(default_factory=list)
    remaining: tuple[str, ...] = ()

    def event_map(self) -> dict[str, DemoldEvent]:
        return {e.cavity: e for e in self.events}


def simulate_demold(
    world: DemoldWorld,
    cavities: Mapping[str, Cavity],
    rows: Sequence[CoaxialRow],
    table_choice: Mapping[str, int],
    pin: tuple[str, int] | None = None,
) -> DemoldResult:
    """按选定脉冲表与可选延时扣，模拟真实开模过程。

    ``table_choice``：row 名 -> 该排 ``pulse_tables`` 的下标。
    ``pin``：可选 ``(row_name, time_ms)``。一次整轴延时扣，把该拍所有本能脱出的
    整排糖移到下一个许可脉冲；仍按晚帧真实画面复核，不能只保一腔。
    """
    by_row = {r.name: r for r in rows}
    holding: dict[str, set[str]] = {r.name: set(r.cavities) for r in rows}
    result = DemoldResult()
    pin_used = False

    # 所有顶排按名字稳定排序后推演；不同排互相独立，顺序不影响结果。
    for row in sorted(rows, key=lambda r: r.name):
        table = row.pulse_tables[table_choice[row.name]]
        for t in table:
            still = [c for c in by_row[row.name].cavities if c in holding[row.name]]
            if not still:
                result.reports.append(PulseReport(row.name, t, "no_sugar"))
                continue
            checks = {c: explain_cavity(world, cavities[c], t) for c in still}
            blocked = [c for c in still if not checks[c].ok]
            if blocked:
                detail = tuple(
                    (
                        c,
                        checks[c].first_block.cell,  # type: ignore[union-attr]
                        checks[c].first_block.rgb,   # type: ignore[union-attr]
                        checks[c].first_block.role,  # type: ignore[union-attr]
                    )
                    for c in blocked
                )
                result.reports.append(
                    PulseReport(row.name, t, "blocked", blocked=detail)
                )
                continue
            # 能顶。
            if pin is not None and not pin_used and pin == (row.name, t):
                pin_used = True
                result.reports.append(
                    PulseReport(row.name, t, "delayed", delayed=tuple(still))
                )
                continue
            for c in still:
                result.events.append(DemoldEvent(cavity=c, row=row.name, time_ms=t))
                holding[row.name].discard(c)
            result.reports.append(
                PulseReport(row.name, t, "pushed", pushed=tuple(still))
            )

    result.remaining = tuple(
        c for r in rows for c in r.cavities if c in holding[r.name]
    )
    return result


@dataclass(frozen=True)
class Arrival:
    cavity: str
    flavor: str
    time_ms: int      # 到达落点的毫秒
    landing: int      # 具名落点坐标


def arrivals_from(result: DemoldResult, cavities: Mapping[str, Cavity]) -> list[Arrival]:
    out: list[Arrival] = []
    for event in result.events:
        cav = cavities[event.cavity]
        out.append(
            Arrival(
                cavity=cav.name,
                flavor=cav.flavor,
                time_ms=event.time_ms + cav.flight_ms,
                landing=cav.landing,
            )
        )
    out.sort(key=lambda a: (a.time_ms, a.cavity))
    return out
