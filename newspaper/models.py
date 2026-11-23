"""编年校勘与缩微订补的不可变领域记录。

两个职责独立的服务（校勘 / 订补）只通过本模块的普通记录交换数据，没有 ORM：
校勘侧产出的“排年产品（ChronologyProduct）”被订补侧原样持久化并按摘要校验，
订补侧从不自行重算历书与刊头的对应关系。

术语
----
* :class:`Almanac`  具名历书原档：纪元首日、逐月月名/月长，闰同名月有独立身份。
* :class:`Masthead` 报纸刊头：月名 + 日序，闰字可漏写（``leap=None`` 表歧义）。
* :class:`Issue`    馆藏原件上的一期：单期一个刊头，合刊两个刊头（顺序固定）。
* :class:`ChronologyProduct` 校勘员冻结的合法排年：每期刊头到应刊日的对应。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Optional


class CollationError(Exception):
    """输入或勘误不成立时抛出；拒绝针对整批/整单。"""

    def __init__(self, code: str, detail: str, index: Optional[int] = None):
        self.code = code
        self.detail = detail
        self.index = index
        super().__init__(f"{code}: {detail}" + (f" (item {index})" if index is not None else ""))


class ConflictError(CollationError):
    """同号复读但内容不同。"""


class UnfillableOrder(CollationError):
    """订补单在所有仍成立的排年中都无法补齐。"""


def canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# --------------------------------------------------------------------- 历书
@dataclass(frozen=True)
class Month:
    """一个历月。``name`` 为月名（如 “四月”），``leap`` 是该月自身的身份。"""

    order: int
    name: str
    leap: bool
    length: int


@dataclass(frozen=True)
class Almanac:
    """具名历书原档：自纪元首日（日 0）起逐月展开的整数日历。

    同名月可出现两次（平月与闰月），二者是不同的 :class:`Month`；日序号是
    全局整数（从 0 起），报纸的“应刊日清单”就是该整数轴上的逐次出刊日。
    """

    almanac_id: str
    revision: int
    epoch_day: int  # 纪元首日落在整数日轴上的位置
    months: tuple[Month, ...]

    def validate(self) -> None:
        if not self.almanac_id:
            raise CollationError("invalid_almanac", "almanac_id required")
        if not _is_int(self.revision) or self.revision < 1:
            raise CollationError("invalid_almanac", "revision must be int >= 1")
        if not _is_int(self.epoch_day) or self.epoch_day < 0:
            raise CollationError("invalid_almanac", "epoch_day must be int >= 0")
        if not self.months:
            raise CollationError("invalid_almanac", "almanac needs at least one month")
        orders = [m.order for m in self.months]
        if orders != list(range(len(self.months))):
            raise CollationError("invalid_almanac", "month order must be 0..n-1 in sequence")
        seen: set[tuple[str, bool]] = set()
        for m in self.months:
            if not m.name or not _is_int(m.length) or m.length < 1:
                raise CollationError("invalid_almanac", "month name required and length >= 1")
            key = (m.name, m.leap)
            if key in seen:
                raise CollationError("invalid_almanac", f"duplicate month identity {key}")
            seen.add(key)
        # 同名月至多两次：一平一闰。
        names = [m.name for m in self.months]
        if any(names.count(n) > 2 for n in set(names)):
            raise CollationError("invalid_almanac", "a month name may appear at most twice")

    def days(self) -> list["DateIndex"]:
        """逐日展开的日期索引（含全局日序号）。"""
        out: list[DateIndex] = []
        cursor = self.epoch_day
        for m in self.months:
            for dom in range(1, m.length + 1):
                out.append(DateIndex(day=cursor, month_order=m.order,
                                     month_name=m.name, leap=m.leap, day_of_month=dom))
                cursor += 1
        return out

    def last_day(self) -> int:
        return self.epoch_day + sum(m.length for m in self.months) - 1

    def as_dict(self) -> dict:
        return {
            "almanac_id": self.almanac_id,
            "revision": self.revision,
            "epoch_day": self.epoch_day,
            "months": [
                {"order": m.order, "name": m.name, "leap": m.leap, "length": m.length}
                for m in self.months
            ],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Almanac":
        return cls(
            almanac_id=str(d["almanac_id"]),
            revision=int(d["revision"]),
            epoch_day=int(d["epoch_day"]),
            months=tuple(
                Month(order=int(m["order"]), name=str(m["name"]),
                      leap=bool(m["leap"]), length=int(m["length"]))
                for m in d["months"]
            ),
        )


@dataclass(frozen=True)
class DateIndex:
    """整数日轴上的一个具体日期；闰同名月靠 ``leap`` 区分身份。"""

    day: int
    month_order: int
    month_name: str
    leap: bool
    day_of_month: int

    def label(self) -> str:
        prefix = "闰" if self.leap else ""
        return f"{prefix}{self.month_name}{self.day_of_month}"


# --------------------------------------------------------------------- 刊头
@dataclass(frozen=True)
class Masthead:
    """报纸刊头写的本地纪年。

    ``leap`` 三态：``True`` 明写闰字、``False`` 明写平月、``None`` 只写月名
    漏写闰字——单期允许，合刊的第一栏题目也允许，校勘时须在两种身份间求解。
    """

    month_name: str
    day_of_month: int
    leap: Optional[bool] = None

    def matches(self, date: DateIndex) -> bool:
        if self.month_name != date.month_name or self.day_of_month != date.day_of_month:
            return False
        if self.leap is None:
            return True  # 闰字漏写：平/闰两种身份都可对应
        return self.leap == date.leap

    def as_dict(self) -> dict:
        return {"month_name": self.month_name, "day_of_month": self.day_of_month,
                "leap": self.leap}

    @classmethod
    def from_dict(cls, d: dict) -> "Masthead":
        return cls(month_name=str(d["month_name"]), day_of_month=int(d["day_of_month"]),
                   leap=None if d.get("leap") is None else bool(d["leap"]))


# --------------------------------------------------------------------- 馆藏
@dataclass(frozen=True)
class Issue:
    """一张馆藏原件上的一期。

    * 单期：``mastheads`` 长度 1，占一个应刊日。
    * 合刊：``mastheads`` 长度 2，两半各带刊头、顺序固定，占两个相邻应刊日。
    * ``sheet_id`` 是同张原件身份；不同原件不得占同一应刊日。
    * ``run_no`` 是连续印次号，合法排年中必须按应刊日严格递增、连续无跳号。
    """

    issue_id: str
    sheet_id: str
    run_no: int
    mastheads: tuple[Masthead, ...]
    holding_library: str = "LOCAL"

    @property
    def combined(self) -> bool:
        return len(self.mastheads) == 2

    def as_dict(self) -> dict:
        return {
            "issue_id": self.issue_id,
            "sheet_id": self.sheet_id,
            "run_no": self.run_no,
            "combined": self.combined,
            "mastheads": [m.as_dict() for m in self.mastheads],
            "holding_library": self.holding_library,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Issue":
        mhs = tuple(Masthead.from_dict(m) for m in d["mastheads"])
        return cls(issue_id=str(d["issue_id"]), sheet_id=str(d["sheet_id"]),
                   run_no=int(d["run_no"]), mastheads=mhs,
                   holding_library=str(d.get("holding_library", "LOCAL")))


@dataclass(frozen=True)
class Placement:
    """一次合法排年里，一张原件对应到的一个（单期）或两个（合刊）应刊日。"""

    issue_id: str
    sheet_id: str
    run_no: int
    days: tuple[int, ...]
    labels: tuple[str, ...]

    def as_dict(self) -> dict:
        return {"issue_id": self.issue_id, "sheet_id": self.sheet_id,
                "run_no": self.run_no, "days": list(self.days),
                "labels": list(self.labels)}


@dataclass
class ChronologyProduct:
    """校勘员冻结的一版完整排年（按具名历书原档签读）。

    历史产品不可被后续历书修订倒改；``catalog_revision`` 与各原档修订号随产品
    固定，移交到新馆后由新馆重签，旧签保留它当时的依据。
    """

    product_id: str
    almanac_id: str
    as_of_day: int
    scheduled_days: list[int]
    placements: list[Placement]
    signer: str
    supersedes: Optional[str] = None
    catalog_revision: int = 0
    almanac_revision: int = 0
    note: str = ""

    def day_to_issue(self) -> dict[int, str]:
        out: dict[int, str] = {}
        for p in self.placements:
            for d in p.days:
                out[d] = p.issue_id
        return out

    def as_dict(self) -> dict:
        return {
            "product_id": self.product_id,
            "almanac_id": self.almanac_id,
            "as_of_day": self.as_of_day,
            "scheduled_days": list(self.scheduled_days),
            "signer": self.signer,
            "supersedes": self.supersedes,
            "catalog_revision": self.catalog_revision,
            "almanac_revision": self.almanac_revision,
            "note": self.note,
            "placements": [p.as_dict() for p in self.placements],
            "digest": self.digest(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ChronologyProduct":
        return cls(
            product_id=d["product_id"],
            almanac_id=d["almanac_id"],
            as_of_day=int(d["as_of_day"]),
            scheduled_days=[int(x) for x in d["scheduled_days"]],
            placements=[
                Placement(issue_id=p["issue_id"], sheet_id=p["sheet_id"],
                          run_no=int(p["run_no"]), days=tuple(p["days"]),
                          labels=tuple(p["labels"]))
                for p in d["placements"]
            ],
            signer=d["signer"],
            supersedes=d.get("supersedes"),
            catalog_revision=int(d.get("catalog_revision", 0)),
            almanac_revision=int(d.get("almanac_revision", 0)),
            note=d.get("note", ""),
        )

    def digest(self) -> str:
        payload = {
            "product_id": self.product_id,
            "signer": self.signer,
            "supersedes": self.supersedes,
            "note": self.note,
            "almanac_id": self.almanac_id,
            "as_of_day": self.as_of_day,
            "scheduled_days": list(self.scheduled_days),
            "placements": sorted(
                (p.issue_id, p.sheet_id, p.run_no, list(p.days), list(p.labels))
                for p in self.placements
            ),
        }
        return hashlib.sha256(canon(payload).encode("utf-8")).hexdigest()


# ----------------------------------------------------------------- 订补单/工单
@dataclass(frozen=True)
class Slot:
    """缩微中心的一个空片位。

    ``kind``：``single`` 单格（只收单期）、``double`` 同张双格（只收一件合刊，
    两半顺序固定）。一件合刊必须整体用同一个 double 格，不能拆成两个单格。
    """

    slot_id: str
    kind: str  # "single" | "double"
    missing_days: tuple[int, ...]

    def as_dict(self) -> dict:
        return {"slot_id": self.slot_id, "kind": self.kind,
                "missing_days": list(self.missing_days)}

    @classmethod
    def from_dict(cls, d: dict) -> "Slot":
        kind = str(d["kind"])
        if kind not in ("single", "double"):
            raise CollationError("invalid_slot", f"unknown slot kind {kind}")
        days = tuple(int(x) for x in d["missing_days"])
        if kind == "single" and len(days) != 1:
            raise CollationError("invalid_slot", "single slot covers exactly one day")
        if kind == "double" and len(days) != 2:
            raise CollationError("invalid_slot", "double slot covers exactly two days")
        return cls(slot_id=str(d["slot_id"]), kind=kind, missing_days=days)


@dataclass(frozen=True)
class ServiceWindow:
    """订补单随附的时窗（整数日）。

    * ``latest_proof_day``  最迟校读：承诺内容必须在该日之前签认定稿。
    * ``reader_blackout``   原馆查阅不可中止窗口：期间原件不能借出（走原件冲突）。
    * ``ship_days``         原件在途天数（取件到中心可拍）。
    """

    latest_proof_day: int
    reader_blackout: tuple[Optional[int], Optional[int]] = (None, None)
    ship_days: int = 0

    def original_unavailable(self, day: int) -> bool:
        start, end = self.reader_blackout
        if start is None:
            return False
        if end is None:
            return day >= start
        return start <= day < end


@dataclass
class WorkOrder:
    """一种实际可行的馆际补拍工单。"""

    order_id: str
    route: str  # "central" 整件借调集中拍 | "surrogate" 持馆直拍有号副片
    product_id: str
    assignments: list[dict] = field(default_factory=list)
    occupied_originals: list[str] = field(default_factory=list)
    occupied_surrogates: list[str] = field(default_factory=list)
    spare_sheets_used: list[str] = field(default_factory=list)
    commit_by_day: int = 0
    shoot_by_day: int = 0
    feasible: bool = True
    reasons: list[str] = field(default_factory=list)
    in_house_evidence: Optional[dict] = None

    def as_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "route": self.route,
            "product_id": self.product_id,
            "feasible": self.feasible,
            "assignments": list(self.assignments),
            "occupied_originals": list(self.occupied_originals),
            "occupied_surrogates": list(self.occupied_surrogates),
            "spare_sheets_used": list(self.spare_sheets_used),
            "shoot_by_day": self.shoot_by_day,
            "commit_by_day": self.commit_by_day,
            "reasons": list(self.reasons),
            "in_house_evidence": self.in_house_evidence,
        }
