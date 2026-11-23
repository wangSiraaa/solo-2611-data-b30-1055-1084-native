"""编年校勘引擎（纯函数，无持久化）。

输入具名历书原档、逐次应刊日清单与馆藏原件，穷举所有同时满足下列勘误的完整
排年；任一勘误不成立即不产生排年，改由调用方指出哪几张不能照此入目：

1. 刊头对应：每期每个刊头都能在历书里对上具体日期；闰字漏写（``leap=None``）
   在平月/闰月两种身份间求解，明写闰字则身份固定。
2. 合刊约束：两栏各带刊头，必须对应**相邻两次应刊日**，且栏序固定。
3. 印次号顺序：按应刊日先后，原件的连续印次号严格递增、无跳号。
4. 同张/异张身份：同张原件的两个刊头共占相邻两日；不同原件不得占同一应刊日
   （等价于所有应刊日被各原件恰好瓜分一次）。
"""

from __future__ import annotations

from .models import (
    Almanac,
    CollationError,
    DateIndex,
    Issue,
    Masthead,
    Placement,
)


class CollationEngine:
    def __init__(self, almanac: Almanac, scheduled_days: list[int], issues: list[Issue]):
        almanac.validate()
        self.almanac = almanac
        self.dates = almanac.days()
        self._by_day = {d.day: d for d in self.dates}
        self._validate_inputs(scheduled_days, issues)
        self.scheduled_days = list(scheduled_days)
        self.issues = list(issues)
        self._by_issue = {i.issue_id: i for i in self.issues}
        # 每个刊头的候选日期（全局日序号），按日排序。
        self.candidates: dict[str, list[int]] = {}
        for issue in self.issues:
            for col, mh in enumerate(issue.mastheads):
                self.candidates[(issue.issue_id, col)] = sorted(
                    d.day for d in self.dates if mh.matches(d)
                )

    # ------------------------------------------------------------- 输入校验
    def _validate_inputs(self, scheduled_days: list[int], issues: list[Issue]) -> None:
        if not scheduled_days:
            raise CollationError("invalid_schedule", "scheduled_days must be non-empty")
        if any(not isinstance(d, int) or isinstance(d, bool) for d in scheduled_days):
            raise CollationError("invalid_schedule", "scheduled_days must be integers")
        if scheduled_days != sorted(scheduled_days) or len(set(scheduled_days)) != len(scheduled_days):
            raise CollationError("invalid_schedule",
                                 "scheduled_days must be strictly ascending and unique")
        unknown = [d for d in scheduled_days if d not in self._by_day]
        if unknown:
            raise CollationError("day_outside_almanac",
                                 f"scheduled days outside the named almanac: {unknown}")
        if not issues:
            raise CollationError("invalid_holdings", "at least one issue is required")
        ids = [i.issue_id for i in issues]
        if len(set(ids)) != len(ids):
            raise CollationError("invalid_holdings", "duplicate issue_id")
        sheets = [i.sheet_id for i in issues]
        if len(set(sheets)) != len(sheets):
            raise CollationError("invalid_holdings", "same sheet_id on two records")
        runs = sorted(i.run_no for i in issues)
        if runs != list(range(runs[0], runs[0] + len(runs))):
            raise CollationError("invalid_holdings",
                                 "run numbers must form one consecutive range before solving")
        for i in issues:
            if len(i.mastheads) not in (1, 2):
                raise CollationError("invalid_holdings",
                                     f"{i.issue_id}: an issue carries one or two mastheads")
            for col, mh in enumerate(i.mastheads):
                if not any(mh.matches(d) for d in self.dates):
                    raise CollationError(
                        "masthead_undatable",
                        f"{i.issue_id} column {col + 1} ({self._label(mh)}) has no date "
                        "in the named almanac",
                    )

    @staticmethod
    def _label(mh: Masthead) -> str:
        prefix = "闰" if mh.leap else ""
        return f"{prefix}{mh.month_name}{mh.day_of_month}"

    # --------------------------------------------------------------- 求解
    def solve(self) -> list[list[Placement]]:
        """穷举合法排年；返回去重后的 Placement 列表（按应刊日起点排序）。"""
        n = len(self.scheduled_days)
        solutions: set[tuple] = set()
        found: list[list[Placement]] = []

        # 按应刊日清单的位置做划分：每个位置块长度 1（单期）或 2（合刊）。
        def backtrack(pos: int, used_issues: set[str], blocks: list[tuple[int, int, str]]):
            if pos == n:
                self._collect(blocks, used_issues, solutions, found)
                return
            for issue in self.issues:
                if issue.issue_id in used_issues:
                    continue
                width = len(issue.mastheads)
                if pos + width > n:
                    continue
                days = tuple(self.scheduled_days[pos:pos + width])
                if not self._fits(issue, days):
                    continue
                backtrack(pos + width, used_issues | {issue.issue_id},
                          blocks + [(pos, pos + width, issue.issue_id)])

        backtrack(0, set(), [])
        return found

    def _fits(self, issue: Issue, days: tuple[int, ...]) -> bool:
        if len(days) != len(issue.mastheads):
            return False
        for col, (day, mh) in enumerate(zip(days, issue.mastheads)):
            date = self._by_day.get(day)
            if date is None or not mh.matches(date):
                return False
        if issue.combined and days[1] <= days[0]:
            return False
        return True

    def _collect(self, blocks, used_issues, solutions: set[tuple],
                 found: list[list[Placement]]) -> None:
        if len(used_issues) != len(self.issues):
            return  # 每张原件都必须入目，不得弃件
        ordered = sorted(blocks, key=lambda b: b[0])
        # 印次号随应刊日顺序连续递增。
        run_sequence = [self._by_issue[issue_id].run_no for _, _, issue_id in ordered]
        if run_sequence != sorted(run_sequence) or len(set(run_sequence)) != len(run_sequence):
            return
        placements: list[Placement] = []
        for _, _, issue_id in ordered:
            issue = self._by_issue[issue_id]
            start = next(b[0] for b in ordered if b[2] == issue_id)
            days = tuple(self.scheduled_days[start:start + len(issue.mastheads)])
            labels = tuple(self._by_day[d].label() for d in days)
            placements.append(
                Placement(issue_id=issue.issue_id, sheet_id=issue.sheet_id,
                          run_no=issue.run_no, days=days, labels=labels)
            )
        key = tuple(sorted((p.issue_id, p.days) for p in placements))
        if key in solutions:
            return
        solutions.add(key)
        found.append(placements)

    # --------------------------------------------------------- 指定归期核验
    def verify_designation(self, issue_id: str, day: int) -> dict:
        """核一项指定归期：在哪些合法排年中成立、对应哪一天的什么身份。"""
        if issue_id not in self._by_issue:
            raise CollationError("unknown_issue", f"unknown issue {issue_id}")
        if day not in self._by_day:
            raise CollationError("unknown_day", f"day {day} is outside the almanac")
        valid = self.solve()
        supporting = []
        for placements in valid:
            for p in placements:
                if p.issue_id == issue_id and day in p.days:
                    supporting.append(p)
        date = self._by_day[day]
        return {
            "issue_id": issue_id,
            "day": day,
            "date_label": date.label(),
            "holds_in": len(supporting),
            "total_valid_chronologies": len(valid),
            "holds_in_every_chronology": len(supporting) == len(valid) and bool(valid),
            "placement": supporting[0].as_dict() if supporting else None,
        }

    def undatable_sheets(self) -> list[dict]:
        """指出哪几张在当前历书下根本无法入目（供停止补拍时回报）。"""
        out = []
        for issue in self.issues:
            bad_cols = []
            for col, mh in enumerate(issue.mastheads):
                if not self.candidates[(issue.issue_id, col)]:
                    bad_cols.append({"column": col + 1, "masthead": self._label(mh)})
            if bad_cols:
                out.append({"issue_id": issue.issue_id, "sheet_id": issue.sheet_id,
                            "combined": issue.combined, "bad_columns": bad_cols,
                            "reason": "masthead_undatable"})
            elif not self._has_any_placement(issue):
                detail = ("the two mastheads no longer correspond to two adjacent "
                          "scheduled days" if issue.combined else
                          "no scheduled day matches the masthead")
                out.append({"issue_id": issue.issue_id, "sheet_id": issue.sheet_id,
                            "combined": issue.combined, "bad_columns": [],
                            "reason": "no_adjacent_scheduled_pair" if issue.combined
                            else "no_scheduled_day",
                            "detail": detail})
        return out

    def _has_any_placement(self, issue: Issue) -> bool:
        """该原件能否在应刊日清单上找到至少一个合规位置（不考虑其他原件占用）。

        合刊要求的是清单上**相邻两次应刊日**（位置相邻），不是整数日差 1。
        """
        on_schedule = set(self.scheduled_days)
        if not issue.combined:
            return any(d in on_schedule for d in self.candidates[(issue.issue_id, 0)])
        for pos in range(len(self.scheduled_days) - 1):
            a, b = self.scheduled_days[pos], self.scheduled_days[pos + 1]
            if (a in self.candidates[(issue.issue_id, 0)]
                    and b in self.candidates[(issue.issue_id, 1)]):
                return True
        return False
