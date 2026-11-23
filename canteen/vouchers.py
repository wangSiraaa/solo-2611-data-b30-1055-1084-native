"""餐签与点数的核验（严格建立在 :mod:`orgbudget` 的实付事实之上）。

题面约束逐条落点：

* **面额取实际付款**：一张餐签的面额 = 该赞助原件在 ``orgbudget`` 里
  已经实际支出（``spend`` 事件）的累计金额；只承诺而未实际支付的预额度
  不印签（:func:`issue_voucher` 拒绝 ``spent == 0``）。
* **取消后的已付部分仍在**：``orgbudget`` 的支出事件仅追加、取消只释放
  未花剩余，因此已付部分自然继续印签。
* **加餐基金限同预算分部承办的场次**：``Voucher.top_up`` 的签只有当
  ``voucher.division == session.division`` 时可用。
* **通用点 / 限桌点 / 限班点可共同支付，同一点只扣一次**：多桌联合审批
  被建模为「签 → 桌」二部容量流，一张签的额度至多被一桌消耗一次。
* **识别两桌分开有钱、一起却同花一张限班点**：流不足以覆盖总需求时，
  用 Hall 条件枚举桌的子集给出最小见证（witness）。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Iterable, Optional

from orgbudget.budget_service import BudgetService

from .errors import VoucherConflict, VoucherError
from .model import SessionSpec, TablePlan

GENERAL = "general"
TABLE = "table"
SHIFT = "shift"
_KINDS = (GENERAL, TABLE, SHIFT)


@dataclass(frozen=True)
class Voucher:
    """一张餐签：面额、归属分部、使用限制与来源（赞助原件请求号）。"""

    voucher_id: str
    division: str
    amount: int
    kind: str = GENERAL
    target: Optional[str] = None
    request_no: Optional[str] = None
    top_up: bool = False

    def __post_init__(self) -> None:
        if self.amount <= 0:
            raise ValueError("餐签面额必须为正")
        if self.kind not in _KINDS:
            raise ValueError(f"未知餐签限制 {self.kind!r}")
        if self.kind in (TABLE, SHIFT) and not self.target:
            raise ValueError(f"{self.kind} 限制的餐签必须给出 target")

    def usable_in(self, session: SessionSpec) -> bool:
        """加餐基金只在同一预算分部承办的场次可用；其余赞助签按来源放行。"""
        if self.top_up and self.division != session.division:
            return False
        return True

    def eligible_for(self, session: SessionSpec, table_id: str) -> bool:
        if not self.usable_in(session):
            return False
        if table_id not in session.tables:
            return False
        table = session.tables[table_id]
        if self.kind == GENERAL:
            return True
        if self.kind == TABLE:
            return self.target == table_id
        return self.target == table.shift_id


def issue_voucher(
    budget: BudgetService,
    voucher_id: str,
    request_no: str,
    *,
    division: str,
    kind: str = GENERAL,
    target: Optional[str] = None,
    top_up: bool = False,
) -> Voucher:
    """把一个已入账的赞助原件兑换成一册餐签。

    面额取该承诺**实际已支出**的金额：只开承诺、尚未付款的原件拿不到签。
    """
    commitment = budget.get_commitment(request_no)
    if commitment.spent <= 0:
        raise VoucherError(
            f"赞助原件 {request_no} 尚未实际支付（承诺 {commitment.amount} 分），不能印签"
        )
    return Voucher(
        voucher_id=voucher_id,
        division=division,
        amount=commitment.spent,
        kind=kind,
        target=target,
        request_no=request_no,
        top_up=top_up,
    )


@dataclass(frozen=True)
class Allocation:
    """某桌从各签取用的点数明细。"""

    table_id: str
    parts: tuple[tuple[str, int], ...]  # (voucher_id, points)

    @property
    def total(self) -> int:
        return sum(p for _, p in self.parts)


@dataclass(frozen=True)
class Approval:
    """几桌菜单的联合批准结果，附逐桌点数来路。"""

    allocations: tuple[Allocation, ...]

    def for_table(self, table_id: str) -> Allocation:
        for a in self.allocations:
            if a.table_id == table_id:
                return a
        raise KeyError(table_id)

    def describe(self) -> str:
        lines = []
        for a in self.allocations:
            parts = " + ".join(f"{vid}:{pts}" for vid, pts in a.parts) or "（无）"
            lines.append(f"  {a.table_id} <- {parts} = {a.total}")
        return "\n".join(lines)


class VoucherBook:
    """一本场次餐签合集；负责多桌联合审批。"""

    def __init__(self, vouchers: Iterable[Voucher] = ()) -> None:
        self._vouchers: dict[str, Voucher] = {}
        for v in vouchers:
            self.add(v)

    def add(self, voucher: Voucher) -> None:
        if voucher.voucher_id in self._vouchers:
            raise VoucherError(f"餐签 {voucher.voucher_id} 已存在")
        self._vouchers[voucher.voucher_id] = voucher

    def vouchers(self) -> tuple[Voucher, ...]:
        return tuple(self._vouchers[k] for k in sorted(self._vouchers))

    def get(self, voucher_id: str) -> Voucher:
        return self._vouchers[voucher_id]

    # ------------------------------------------------------------ 联合审批
    def approve_together(
        self, session: SessionSpec, plans: Iterable[TablePlan]
    ) -> Approval:
        """判断多桌的具体菜单能否**一起**批准；可行则给出点数来路。

        不可行时抛 :class:`~canteen.errors.VoucherConflict`，并给出最小
        的桌集合见证（通常是两桌分开各自够、合起来却抢同一张限班点）。
        """
        plans_t = tuple(plans)
        if not plans_t:
            return Approval(())
        seen: set[str] = set()
        for p in plans_t:
            if p.table_id in seen:
                raise VoucherError(f"桌 {p.table_id} 出现多份菜单")
            seen.add(p.table_id)
            if p.table_id not in session.tables:
                raise VoucherError(f"场次里没有桌 {p.table_id}")

        usable = [v for v in self.vouchers() if v.usable_in(session)]
        flow, allocated = self._max_flow(session, usable, plans_t)
        need = sum(p.points for p in plans_t)
        if flow != need:
            witness = self._witness(session, usable, plans_t)
            detail = "、".join(sorted(witness)) if witness else "（全部桌）"
            raise VoucherConflict(
                f"联合审批不足：需 {need} 点，仅能凑出 {flow} 点；"
                f"最小冲突桌集 = {{{detail}}}"
            )
        allocations = []
        for p in plans_t:
            parts = tuple(
                (vid, pts) for (vid, tid), pts in allocated.items() if tid == p.table_id and pts
            )
            allocations.append(Allocation(p.table_id, parts))
        return Approval(tuple(allocations))

    def can_approve(self, session: SessionSpec, plans: Iterable[TablePlan]) -> bool:
        try:
            self.approve_together(session, plans)
        except (VoucherConflict, VoucherError):
            return False
        return True

    # ------------------------------------------------------------ 容量流
    @staticmethod
    def _max_flow(
        session: SessionSpec,
        vouchers: list[Voucher],
        plans: tuple[TablePlan, ...],
    ) -> tuple[int, dict[tuple[str, str], int]]:
        tables = [p.table_id for p in plans]
        need = {p.table_id: p.points for p in plans}
        # 节点：0=源, 1..V=签, V+1..V+T=桌, 末=汇。
        n_v, n_t = len(vouchers), len(tables)
        src = 0
        v_off = 1
        t_off = 1 + n_v
        snk = 1 + n_v + n_t
        n = snk + 1
        cap: list[list[int]] = [[0] * n for _ in range(n)]

        for i, v in enumerate(vouchers):
            cap[src][v_off + i] = v.amount
        for i, v in enumerate(vouchers):
            for j, tid in enumerate(tables):
                if v.eligible_for(session, tid):
                    cap[v_off + i][t_off + j] = v.amount
        for j, tid in enumerate(tables):
            cap[t_off + j][snk] = need[tid]

        # Edmonds-Karp。
        total = 0
        while True:
            parent = [-1] * n
            parent[src] = src
            q = deque([src])
            while q and parent[snk] == -1:
                u = q.popleft()
                for w in range(n):
                    if parent[w] == -1 and cap[u][w] > 0:
                        parent[w] = u
                        q.append(w)
            if parent[snk] == -1:
                break
            # 找瓶颈。
            path = []
            cur = snk
            bottle = 10**18
            while cur != src:
                u = parent[cur]
                bottle = min(bottle, cap[u][cur])
                path.append((u, cur))
                cur = u
            for u, w in path:
                cap[u][w] -= bottle
                cap[w][u] += bottle
            total += bottle

        # 从残量的反向边恢复每张签流向每桌的点数。
        allocated: dict[tuple[str, str], int] = {}
        for i, v in enumerate(vouchers):
            for j, tid in enumerate(tables):
                if cap[v_off + i][t_off + j] < v.amount and v.eligible_for(session, tid):
                    used = v.amount - cap[v_off + i][t_off + j]
                    if used > 0:
                        allocated[(v.voucher_id, tid)] = used
        return total, allocated

    @staticmethod
    def _witness(
        session: SessionSpec,
        vouchers: list[Voucher],
        plans: tuple[TablePlan, ...],
    ) -> tuple[str, ...]:
        """按 Hall 条件找最小的超额桌集。"""
        tables = list(plans)
        n = len(tables)
        best: Optional[tuple[str, ...]] = None
        for mask in range(1, 1 << n):
            subset = [tables[i] for i in range(n) if mask & (1 << i)]
            req = sum(p.points for p in subset)
            ids = {p.table_id for p in subset}
            cap = sum(
                v.amount
                for v in vouchers
                if any(v.eligible_for(session, tid) for tid in ids)
            )
            if req > cap:
                key = tuple(sorted(p.table_id for p in subset))
                if best is None or (len(key), key) < (len(best), best):
                    best = key
        return best or ()
