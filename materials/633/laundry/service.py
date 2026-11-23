"""把洗衣子系统接到旧仓：从赛程 edition + 比分事实派生并规划。

本模块只读旧仓（:class:`~league.schedule_service.ScheduleService` 与
:class:`~league.standings_repo.StandingsRepository`），不改写任何赛程/比分。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from league.schedule_service import ScheduleService
from league.standings_repo import StandingsRepository

from .model import Drum, EquipmentLedger
from .solver import Solution, WashProblem, plan, replan, scheme_first, drum_first
from .towash import IssuanceBook, ToWashResult, derive_to_wash


@dataclass
class WashRequest:
    """一次洗程鉴定请求（固定 edition + 事实切面 + 实筒 + 截止）。"""

    edition_id: int
    drums: Sequence[Drum]
    deadline: int
    bags: dict[str, list[str]] = field(default_factory=dict)
    sealed: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.drums = list(self.drums)
        # 各件只准在一处：布件不得同时出现在两个初始袋
        seen: set[str] = set()
        for bid, ids in self.bags.items():
            for i in ids:
                if i in seen:
                    raise ValueError(f"布件 {i} 同时出现在多个袋中（各件只准在一处）")
                seen.add(i)


def derive_for_request(svc: ScheduleService, repo: StandingsRepository,
                       issuance: IssuanceBook, ledger: EquipmentLedger,
                       edition_id: int) -> ToWashResult:
    """用旧仓当前事实切面（active results）派生待洗集合。"""
    matches = svc.get_edition(edition_id)["matches"]
    results = repo.active_results()
    return derive_to_wash(matches, results, issuance, ledger)


def build_problem(towash: ToWashResult, req: WashRequest) -> WashProblem:
    return WashProblem(items=towash.items, bags=dict(req.bags),
                       drums=list(req.drums), deadline=req.deadline,
                       sealed=dict(req.sealed))


def plan_request(svc: ScheduleService, repo: StandingsRepository,
                 issuance: IssuanceBook, ledger: EquipmentLedger,
                 req: WashRequest, mode: str = "scheme") -> tuple[ToWashResult, Solution]:
    """端到端：派生待洗 → 求解。``mode`` 取 ``scheme``/``drum``/``plan``。"""
    towash = derive_for_request(svc, repo, issuance, ledger, req.edition_id)
    problem = build_problem(towash, req)
    if mode == "scheme":
        sol = scheme_first(problem)
    elif mode == "drum":
        sol = drum_first(problem)
    elif mode == "plan":
        sol = plan(problem)
    else:
        raise ValueError(f"未知 mode {mode!r}")
    return towash, sol


def replan_request(svc: ScheduleService, repo: StandingsRepository,
                   issuance: IssuanceBook, ledger: EquipmentLedger,
                   req: WashRequest, started_drum_id: str,
                   prior_assign: dict[str, str],
                   available_drums: Sequence[Drum] | None = None
                   ) -> tuple[ToWashResult, Solution]:
    towash = derive_for_request(svc, repo, issuance, ledger, req.edition_id)
    problem = build_problem(towash, req)
    return towash, replan(problem, started_drum_id, prior_assign,
                          available_drums=available_drums)
