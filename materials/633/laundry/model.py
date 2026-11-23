"""布件、约束、网袋与洗衣筒的数据模型。

领域边界
--------
* **具名布件**：可重用的球衣、护套等，按实体身份（``item_id``）登记，
  同队主/客场领用的装备各自单独登记，不按“同尺码”互相顶替。
* **程序**：本域只区分温洗 ``warm`` 与快洗 ``quick``，不模拟物理洗涤。
* **约束 → 允许程序集合**：每项约束（磨耗、历次修补材料、内衬、本场用法）
  各自给出一个允许程序集合，一件布件最终允许的程序是其全部约束集合的交集。
  交集为空即这件布件不可洗——即使没有下一场，这份鉴定也能驳回错误送洗。
* **网袋**：实体袋。开袋（open）可在封车前任意拆袋/重装/合袋；封袋（sealed）
  与送洗（washing/done）后袋内布件必须整袋同程序同筒，且不能再凭同一回条在
  更衣室拆开。
* **洗衣筒**：某一整点启动、只跑一种程序的一次装载，有容量与用时；
  封车/开局后容量与内容不可再改。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

# 本域全部可用程序；不含任何物理参数，只作为程序标识。
PROGRAMS = frozenset({"warm", "quick"})

BAG_OPEN = "open"
BAG_SEALED = "sealed"
BAG_WASHING = "washing"
BAG_DONE = "done"
BAG_STATES = (BAG_OPEN, BAG_SEALED, BAG_WASHING, BAG_DONE)


# --------------------------------------------------------------------- 约束
@dataclass(frozen=True)
class Constraint:
    """一条约束及其允许的程序集合（``source`` 记录来源便于解释与审计）。"""

    source: str
    allowed: frozenset[str]
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed", frozenset(self.allowed))

    def to_dict(self) -> dict:
        return {"source": self.source, "allowed": sorted(self.allowed), "note": self.note}


# --------------------------------------------------------------------- 布件
@dataclass(frozen=True)
class ItemInfo:
    """布件的稳定身份与归属；主/客场单独登记的装备各有自己的条目。"""

    item_id: str
    team: str
    side: str          # 'home' / 'away'
    kind: str          # 'jersey' / 'set' / 'sleeve' ...
    size: str = ""


@dataclass
class Item:
    """一件待洗布件：稳定身份 + 该次洗涤累积的全部约束。"""

    info: ItemInfo
    constraints: list[Constraint] = field(default_factory=list)

    @property
    def item_id(self) -> str:
        return self.info.item_id

    @property
    def team(self) -> str:
        return self.info.team

    @property
    def size(self) -> str:
        return self.info.size

    @property
    def allowed(self) -> frozenset[str]:
        """全部约束允许程序的交集；空集表示这件布件当前不可洗。"""
        allowed = set(PROGRAMS)
        for c in self.constraints:
            allowed &= c.allowed
        return frozenset(allowed)

    @property
    def feasible(self) -> bool:
        return bool(self.allowed)

    def permits(self, program: str) -> bool:
        return program in self.allowed

    def add_constraints(self, extra: Iterable[Constraint]) -> None:
        self.constraints.extend(extra)


class EquipmentLedger:
    """装备账：登记布件身份与历次磨耗、修补材料、内衬等**与场次无关**的约束。

    本场用法（usage）在 :mod:`laundry.towash` 派生待洗件时才叠加进来，
    因此同一实体在同一账里可被多场比赛反复引用而逐场累积约束。
    """

    def __init__(self) -> None:
        self._info: dict[str, ItemInfo] = {}
        self._constraints: dict[str, list[Constraint]] = {}

    def register(self, info: ItemInfo) -> None:
        if info.item_id in self._info and self._info[info.item_id] != info:
            raise ValueError(f"布件 {info.item_id} 重复登记且身份不一致")
        self._info[info.item_id] = info
        self._constraints.setdefault(info.item_id, [])

    def add_constraint(self, item_id: str, constraint: Constraint) -> None:
        if item_id not in self._info:
            raise KeyError(f"未知布件 {item_id}")
        self._constraints[item_id].append(constraint)

    def info(self, item_id: str) -> ItemInfo:
        return self._info[item_id]

    def item_constraints(self, item_id: str) -> list[Constraint]:
        return list(self._constraints[item_id])

    def build_item(self, item_id: str,
                   extra: Sequence[Constraint] = ()) -> Item:
        return Item(self._info[item_id],
                    self.item_constraints(item_id) + list(extra))


# --------------------------------------------------------------------- 网袋
@dataclass
class Bag:
    """实体网袋。``item_ids`` 内各件只准在一处。"""

    bag_id: str
    item_ids: list[str] = field(default_factory=list)
    state: str = BAG_OPEN
    drum_id: str | None = None
    return_slip: str | None = None   # 交还时的回条号

    def __post_init__(self) -> None:
        if self.state not in BAG_STATES:
            raise ValueError(f"未知袋状态 {self.state!r}")
        self.item_ids = list(dict.fromkeys(self.item_ids))  # 去重且保序

    # 袋不可在机里拆；已封/送洗的袋不能再凭同一回条在更衣室拆开。
    def can_split(self, slip: str | None = None) -> bool:
        if self.state == BAG_OPEN:
            return True
        if self.state == BAG_SEALED and slip is not None and slip != self.return_slip:
            # 凭**新回条**重新处理是允许的；同一回条不行。
            return True
        return False

    def to_dict(self) -> dict:
        return {"bag_id": self.bag_id, "item_ids": list(self.item_ids),
                "state": self.state, "drum_id": self.drum_id,
                "return_slip": self.return_slip}


# --------------------------------------------------------------------- 洗衣筒
@dataclass(frozen=True)
class Drum:
    """一次可用的装载：整点启动、单程序、单容量、固定用时。

    ``machine_id`` 给定时，同一台机器上的两次装载在时间上不得重叠；
    缺省 ``None`` 表示每次装载各自独立可用。
    """

    drum_id: str
    program: str
    start: int
    capacity: int
    duration: int
    machine_id: str | None = None

    def __post_init__(self) -> None:
        if self.program not in PROGRAMS:
            raise ValueError(f"未知程序 {self.program!r}")
        if self.start < 0 or self.capacity <= 0 or self.duration <= 0:
            raise ValueError("start>=0、capacity>0、duration>0")

    @property
    def end(self) -> int:
        return self.start + self.duration

    def overlaps(self, other: "Drum") -> bool:
        return self.start < other.end and other.start < self.end

    def to_dict(self) -> dict:
        return {"drum_id": self.drum_id, "program": self.program,
                "start": self.start, "capacity": self.capacity,
                "duration": self.duration, "end": self.end,
                "machine_id": self.machine_id}


def machine_conflicts(selected: Sequence[Drum]) -> list[tuple[str, str]]:
    """同机同时间重叠的装载对；空列表表示这批实筒在同机时间上互不冲突。"""
    tools = [d for d in selected if d.machine_id is not None]
    out: list[tuple[str, str]] = []
    for i, a in enumerate(tools):
        for b in tools[i + 1:]:
            if a.machine_id == b.machine_id and a.overlaps(b):
                out.append((a.drum_id, b.drum_id))
    return out
