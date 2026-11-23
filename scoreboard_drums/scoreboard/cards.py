"""电工席逐拍操作卡：把策略树压成纯数据，现场断链可离线执行。

操作卡不依赖现场任何连接：每张卡绑定一场比赛、一个版次和一个目标止位，
内容是带页码的决策树——每页写明本拍动作；读头页按实际读到的数字翻到
对应下一页，走格页只有固定下一页。临时更换目标必须换对应操作卡，
卡头的目标/版次对不上时执行器拒绝执行，防止拿错卡。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .broadcast import ChangeOrder
from .planner import Plan, PolicyNode


@dataclass(frozen=True)
class CardPage:
    page_id: str
    action: dict
    next_page: str | None = None          # 走格页：固定下一页
    branches: dict[int, str] | None = None  # 读头页：读数 -> 下一页

    def to_dict(self) -> dict:
        data = {"page_id": self.page_id, "action": self.action}
        if self.next_page is not None:
            data["next_page"] = self.next_page
        if self.branches is not None:
            data["branches"] = {str(d): p for d, p in sorted(self.branches.items())}
        return data


class OperationCard:
    """可 JSON 序列化的逐拍操作卡（只含数据，不含现场连接）。"""

    def __init__(self, header: dict, pages: list[CardPage], root_page: str):
        self.header = header
        self.pages = {p.page_id: p for p in pages}
        self.root_page = root_page

    @classmethod
    def build(cls, order: ChangeOrder, plan: Plan, curtain_beats: int) -> "OperationCard":
        if not plan.issuable(curtain_beats):
            raise ValueError(
                f"目标 {plan.target} 在 {curtain_beats} 拍内无法保证，不能签卡")
        pages: list[CardPage] = []

        def reserve_page(action: dict) -> str:
            page_id = f"p{len(pages)}"
            pages.append(CardPage(page_id, action))
            return page_id

        def walk(node: PolicyNode) -> str:
            if node.done:
                return reserve_page({"kind": "done"})
            action = {"kind": node.action.kind, "drum": node.action.drum}
            if node.action.kind == "read":
                page_id = reserve_page(action)
                branches = {digit: walk(child)
                            for digit, child in node.branches.items()}
                pages[int(page_id[1:])] = CardPage(
                    page_id, action, branches=branches)
                return page_id
            next_page = walk(node.next)
            page_id = f"p{len(pages)}"
            pages.append(CardPage(page_id, action, next_page=next_page))
            return page_id

        root = walk(plan.root)
        header = {
            "match_id": order.match_id,
            "edition_id": order.edition_id,
            "target": list(order.target.key()),
            "curtain_beats": curtain_beats,
            "worst_beats": plan.worst_beats,
        }
        return cls(header, pages, root)

    def to_dict(self) -> dict:
        return {
            "header": self.header,
            "root_page": self.root_page,
            "pages": [p.to_dict() for p in self.pages.values()],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: dict) -> "OperationCard":
        pages = []
        for raw in data["pages"]:
            branches = None
            if "branches" in raw:
                branches = {int(d): p for d, p in raw["branches"].items()}
            pages.append(CardPage(
                page_id=raw["page_id"],
                action=raw["action"],
                next_page=raw.get("next_page"),
                branches=branches,
            ))
        return cls(data["header"], pages, data["root_page"])

    @classmethod
    def from_json(cls, text: str) -> "OperationCard":
        return cls.from_dict(json.loads(text))


class CardExecutor:
    """现场执行器：只吃操作卡数据 + 本地读头/走格硬件，断链可用。"""

    def __init__(self, card: OperationCard, hardware):
        self.card = card
        self.hardware = hardware  # 需提供 advance(drum) 与 read(drum)
        self.page_id = card.root_page
        self.beats_used = 0
        self.actions_done: list[dict] = []

    def _check_header(self, order: ChangeOrder) -> None:
        header = self.card.header
        if (header["match_id"] != order.match_id
                or header["edition_id"] != order.edition_id
                or tuple(header["target"]) != order.target.key()):
            raise ValueError("操作卡卡头与本次换牌单不符，拒绝执行（请更换对应操作卡）")

    def run(self, order: ChangeOrder) -> dict:
        self._check_header(order)
        while True:
            page = self.card.pages[self.page_id]
            action = page.action
            if action["kind"] == "done":
                return {
                    "beats_used": self.beats_used,
                    "actions": list(self.actions_done),
                    "final": self.hardware.snapshot(),
                }
            drum = action["drum"]
            if action["kind"] == "advance":
                self.hardware.advance(drum)
                self.actions_done.append({"beat": self.beats_used + 1,
                                          "action": "advance", "drum": drum})
                self.beats_used += 1
                self.page_id = page.next_page
            else:
                digit = self.hardware.read(drum)
                self.actions_done.append({"beat": self.beats_used + 1,
                                          "action": "read", "drum": drum,
                                          "observed": digit})
                self.beats_used += 1
                if digit not in page.branches:
                    raise RuntimeError(f"读头读数 {digit} 不在操作卡分支内")
                self.page_id = page.branches[digit]
            if self.beats_used > self.card.header["curtain_beats"]:
                raise RuntimeError("实际拍数超过遮幕时限，操作卡无效")
