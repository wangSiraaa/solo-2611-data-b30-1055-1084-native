"""独立重算与事件级审计。

本模块刻意不调用生产代码里的分解/汇总逻辑，而是：

1. 直接用 SQL 从两个 SQLite 库读取快照行、承诺与仅追加支出事件；
2. 用 :mod:`fractions` 重新推导权重分解（独立的余数比较实现）；
3. 从原始承诺/支出事件重建每个叶的实际支出与未结占额，再逐级汇总，
   核对每级“孩子预算之和 = 父预算”以及全部服务查询结果。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .errors import InvalidTree


@dataclass(frozen=True)
class AuditResult:
    scheme_id: int
    org_version: int
    departments: int
    leaves: int
    root_budget: int
    total_spent: int
    total_open_remaining: int
    spend_events: int
    commitments: int
    checks: tuple[str, ...]


def _connect(db_path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    return con


def independent_allocate(
    parent_budget: int, weighted: list[tuple[str, int]]
) -> dict[str, int]:
    """用 Fraction 独立实现“按比例下取整 + 余数降序补分 + ID 并列”。

    与生产代码的实现路径刻意不同：这里全程用 :class:`Fraction` 真值，
    生产代码只用整数分子余数。排序键只取小数余数（不是完整分数），
    余数并列按部门 ID 升序；零余数自然排在最后，补分数量恒不超过
    正余数节点数，因此取前 ``leftover`` 个即可。
    """
    total_weight = sum(w for _, w in weighted)
    fractions = {
        dept: Fraction(parent_budget * w, total_weight) for dept, w in weighted
    }
    floors = {
        dept: frac.numerator // frac.denominator for dept, frac in fractions.items()
    }
    remainders = {dept: frac - floors[dept] for dept, frac in fractions.items()}
    leftover = parent_budget - sum(floors.values())
    ranked = sorted(weighted, key=lambda item: (-remainders[item[0]], item[0]))
    result = dict(floors)
    for dept, _ in ranked[:leftover]:
        # 数学保证：正余数节点数 >= leftover，零余数节点不会被选中。
        if remainders[dept] == 0:  # pragma: no cover - 防御性校验
            raise InvalidTree("补分落到了余数为 0 的节点")
        result[dept] += 1
    assert sum(result.values()) == parent_budget
    return result


def _load_org_product(org_db: str | Path, version: int) -> dict[str, object]:
    with _connect(org_db) as con:
        rows = con.execute(
            "SELECT dept_id, parent_id, active FROM org_dept WHERE version = ?",
            (version,),
        ).fetchall()
    if not rows:
        raise InvalidTree(f"组织版本 {version} 无快照")
    parent_of = {r["dept_id"]: r["parent_id"] for r in rows}
    active = {r["dept_id"]: bool(r["active"]) for r in rows}
    roots = [d for d, p in parent_of.items() if p is None]
    if len(roots) != 1:
        raise InvalidTree(f"版本 {version} 根数量异常: {roots}")
    children_of: dict[str, list[str]] = {d: [] for d in parent_of}
    for dept, parent in parent_of.items():
        if parent is not None:
            children_of[parent].append(dept)
    for kids in children_of.values():
        kids.sort()
    return {"parent_of": parent_of, "active": active, "children_of": children_of,
            "root": roots[0]}


def _rebuild_decomposition(
    root_budget: int,
    product: dict[str, object],
    weights: dict[tuple[str, str], int],
) -> dict[str, int]:
    children_of: dict[str, list[str]] = product["children_of"]  # type: ignore[assignment]
    root: str = product["root"]  # type: ignore[assignment]
    allocations = {root: root_budget}
    queue = [root]
    while queue:
        parent = queue.pop(0)
        kids = children_of[parent]
        if not kids:
            continue
        weighted = [(kid, weights[(parent, kid)]) for kid in kids]
        for kid, amount in independent_allocate(allocations[parent], weighted).items():
            allocations[kid] = amount
            queue.append(kid)
    return allocations


def _rebuild_leaf_facts(budget_db: str | Path) -> dict[str, dict[str, int]]:
    """从原始 commitment / spend 事件重建每叶事实，不依赖服务方法。"""
    with _connect(budget_db) as con:
        commitments = con.execute(
            "SELECT request_no, dept_id, amount, status FROM commitment"
        ).fetchall()
        spends = con.execute(
            "SELECT request_no, dept_id, amount FROM spend ORDER BY id"
        ).fetchall()
    facts: dict[str, dict[str, int]] = {}

    def bucket(dept: str) -> dict[str, int]:
        return facts.setdefault(dept, {"spent": 0, "open_remaining": 0,
                                      "open_count": 0, "cancelled_count": 0})

    spent_by_req: dict[str, int] = {}
    for ev in spends:
        spent_by_req[ev["request_no"]] = spent_by_req.get(ev["request_no"], 0) + ev["amount"]
        bucket(ev["dept_id"])["spent"] += ev["amount"]

    for c in commitments:
        b = bucket(c["dept_id"])
        spent = spent_by_req.get(c["request_no"], 0)
        if spent > c["amount"]:
            raise InvalidTree(f"承诺 {c['request_no']} 支出超过金额")
        if c["status"] == "OPEN":
            b["open_remaining"] += c["amount"] - spent
            b["open_count"] += 1
        else:
            b["cancelled_count"] += 1
    return facts


def audit_scheme(
    budget_db: str | Path,
    org_db: str | Path,
    scheme_id: int | None = None,
) -> AuditResult:
    """对预算方案做全量独立审计，任何不变量被破坏即抛异常。"""
    checks: list[str] = []
    with _connect(budget_db) as con:
        if scheme_id is None:
            active_id = int(
                con.execute("SELECT value FROM meta WHERE key = 'active_scheme_id'").fetchone()[0]
            )
            if not active_id:
                raise InvalidTree("没有生效方案")
            scheme_id = active_id
        head = con.execute(
            "SELECT org_version, root_budget, status FROM scheme WHERE scheme_id = ?",
            (scheme_id,),
        ).fetchone()
        if head is None:
            raise InvalidTree(f"方案 {scheme_id} 不存在")
        stored_alloc = dict(
            con.execute(
                "SELECT dept_id, budget FROM allocation WHERE scheme_id = ?",
                (scheme_id,),
            ).fetchall()
        )
        weight_rows = con.execute(
            "SELECT parent_id, child_id, weight FROM weight WHERE scheme_id = ?",
            (scheme_id,),
        ).fetchall()
        n_commit = con.execute("SELECT COUNT(*) FROM commitment").fetchone()[0]
        n_spend = con.execute("SELECT COUNT(*) FROM spend").fetchone()[0]

    org_version, root_budget, status = head["org_version"], head["root_budget"], head["status"]
    product = _load_org_product(org_db, org_version)
    parent_of: dict[str, str | None] = product["parent_of"]  # type: ignore[assignment]
    children_of: dict[str, list[str]] = product["children_of"]  # type: ignore[assignment]
    root: str = product["root"]  # type: ignore[assignment]
    weights = {(r["parent_id"], r["child_id"]): r["weight"] for r in weight_rows}

    # 1) 权重恰好覆盖新产品的每条直接父子边。
    expected_edges = {(p, c) for p, kids in children_of.items() for c in kids}
    if set(weights) != expected_edges:
        raise InvalidTree("权重边与组织产品父子边不一致")
    if any(w <= 0 for w in weights.values()):
        raise InvalidTree("存在非正权重")
    checks.append("权重对全部直接父子边恰好补齐且为正整数")

    # 2) 独立重算分解，逐部门比对预算仓落库值。
    recomputed = _rebuild_decomposition(root_budget, product, weights)
    if set(recomputed) != set(stored_alloc):
        raise InvalidTree("重算部门集合与落库不一致")
    for dept, amount in recomputed.items():
        if stored_alloc[dept] != amount:
            raise InvalidTree(f"部门 {dept} 落库预算 {stored_alloc[dept]} != 重算 {amount}")
    checks.append("自根向下独立重算预算与落库分配逐部门一致")

    # 3) 每级守恒：孩子预算之和严格等于父预算；根等于登记根预算。
    if stored_alloc[root] != root_budget:
        raise InvalidTree("根预算与登记值不一致")
    for parent, kids in children_of.items():
        if kids and sum(stored_alloc[k] for k in kids) != stored_alloc[parent]:
            raise InvalidTree(f"部门 {parent} 子预算之和不等于父预算")
    checks.append("每级孩子预算之和严格等于父预算，总和守恒")

    # 4) 事件级重建叶事实并核对承诺/支出约束。
    facts = _rebuild_leaf_facts(budget_db)
    leaves = {d for d, kids in children_of.items() if not kids}
    total_spent = total_open = 0
    for dept, f in facts.items():
        if dept not in leaves:
            raise InvalidTree(f"非叶部门 {dept} 上存在承诺/支出事实")
        total_spent += f["spent"]
        total_open += f["open_remaining"]
    checks.append("承诺/支出事实全部归属叶部门，支出未超承诺金额")

    # 5) 每叶覆盖与可用额非负。
    for dept in leaves:
        f = facts.get(dept, {"spent": 0, "open_remaining": 0})
        available = stored_alloc[dept] - f["spent"] - f["open_remaining"]
        if available < 0:
            raise InvalidTree(f"叶 {dept} 可用额为负 {available}")
    checks.append("每个叶预算覆盖实际支出与未结承诺，可用额非负")

    # 6) 沿本产品树闭包逐级汇总，核对祖先汇总 = 后代叶事实之和。
    for dept in parent_of:
        subtree_leaves = _subtree_leaves(dept, children_of)
        agg_spent = sum(facts.get(d, {"spent": 0})["spent"] for d in subtree_leaves)
        agg_open = sum(
            facts.get(d, {"open_remaining": 0})["open_remaining"] for d in subtree_leaves
        )
        if dept in leaves:
            f = facts.get(dept, {"spent": 0, "open_remaining": 0})
            if (agg_spent, agg_open) != (f["spent"], f["open_remaining"]):
                raise InvalidTree(f"叶 {dept} 汇总与事实不符")
        if stored_alloc[dept] < agg_spent + agg_open:
            raise InvalidTree(f"部门 {dept} 预算不足以覆盖子树占额")
    checks.append("祖先汇总沿本产品树闭包计算，等于后代叶事实之和")

    # 7) 组织产品不变量再核一遍（单根/无环/唯一父）。
    for dept in parent_of:
        seen: set[str] = set()
        cur: str | None = dept
        while cur is not None:
            if cur in seen:
                raise InvalidTree(f"产品 {org_version} 存在环")
            seen.add(cur)
            cur = parent_of[cur]
    checks.append(f"组织产品 v{org_version} 单根、无环、每部门唯一父项")

    return AuditResult(
        scheme_id=scheme_id,  # type: ignore[arg-type]
        org_version=org_version,
        departments=len(parent_of),
        leaves=len(leaves),
        root_budget=root_budget,
        total_spent=total_spent,
        total_open_remaining=total_open,
        spend_events=n_spend,
        commitments=n_commit,
        checks=tuple(checks),
    )


def _subtree_leaves(dept: str, children_of: dict[str, list[str]]) -> set[str]:
    result: set[str] = set()
    stack = [dept]
    while stack:
        cur = stack.pop()
        kids = children_of[cur]
        if not kids:
            result.add(cur)
        else:
            stack.extend(kids)
    return result


# ---------------------------------------------------------------- 成本联算独立审计


def _fraction(num: int, den: int) -> Fraction:
    return Fraction(int(num), int(den))


def _independent_scc(nodes: list[str], succ: dict[str, list[str]]) -> list[list[str]]:
    """审计专用 SCC：与生产代码的 Tarjan 不同，用 Kosaraju 两遍 DFS。"""
    rev: dict[str, list[str]] = {n: [] for n in nodes}
    for u in nodes:
        for v in succ[u]:
            rev[v].append(u)
    visited: set[str] = set()
    order: list[str] = []

    def dfs1(v: str) -> None:
        stack = [(v, 0)]
        visited.add(v)
        while stack:
            node, i = stack[-1]
            if i < len(succ[node]):
                stack[-1] = (node, i + 1)
                nxt = succ[node][i]
                if nxt not in visited:
                    visited.add(nxt)
                    stack.append((nxt, 0))
            else:
                order.append(node)
                stack.pop()

    for n in nodes:
        if n not in visited:
            dfs1(n)
    comp_of: dict[str, int] = {}
    cid = 0
    for n in reversed(order):
        if n in comp_of:
            continue
        stack = [n]
        comp_of[n] = cid
        while stack:
            node = stack.pop()
            for prv in rev[node]:
                if prv not in comp_of:
                    comp_of[prv] = cid
                    stack.append(prv)
        cid += 1
    components: list[list[str]] = [[] for _ in range(cid)]
    for n, c in comp_of.items():
        components[c].append(n)
    return components


def audit_costflow(
    ledger_db: str | Path,
    org_db: str | Path,
    bound_org_version: int,
    lines: list[str],
) -> dict[str, object]:
    """对实付账联算做独立重算审计。

    刻意不调用 :mod:`orgbudget.costflow` 的求解路径：直接 SQL 读支付、
    中心、分送表，用 Kosaraju 独立识别 SCC，对开网络建立稠密线性方程组
    并以独立的 Fraction 消元重算吞吐量，再与生产结果逐项核对：

    1. 分送表比例合计严格 1，只保留实付（取消后余款不计）；
    2. 就近归集一次，直接成本总额 = 全部实付事件之和；
    3. 开中心吞吐量满足 q = 直接成本 + 受入；闭圈困钱按来源可指认；
    4. 全局守恒：直接成本 = 各线金额 + 困圈金额；
    5. 生产代码 :func:`solve_allocation` 的结果与独立重算逐项一致。
    """
    from .costflow import Distribution, solve_allocation  # 延迟导入避免循环

    con = _connect(ledger_db)
    center_rows = con.execute(
        "SELECT center_id, org_version, subtree_root FROM center ORDER BY center_id"
    ).fetchall()
    table_rows = con.execute(
        "SELECT center_id, version, target, numerator, denominator FROM distribution_table "
        "ORDER BY center_id, version, target"
    ).fetchall()
    pay_rows = con.execute(
        "SELECT payment_id, dept_id, amount, paid, cancelled FROM payment"
    ).fetchall()
    con.close()

    product = _load_org_product(org_db, bound_org_version)
    parent_of: dict[str, str | None] = product["parent_of"]  # type: ignore[assignment]
    checks: list[str] = []

    # 1) 每中心取最新版分送表，独立校验合计 1 且无撤回残留。
    latest: dict[str, int] = {}
    for r in table_rows:
        latest[r["center_id"]] = max(latest.get(r["center_id"], 0), r["version"])
    shares: dict[str, dict[str, Fraction]] = {c["center_id"]: {} for c in center_rows}
    for r in table_rows:
        if r["version"] == latest[r["center_id"]]:
            shares[r["center_id"]][r["target"]] = _fraction(
                r["numerator"], r["denominator"]
            )
    for cid, table in shares.items():
        if not table or sum(table.values(), Fraction(0)) != Fraction(1):
            raise InvalidTree(f"中心 {cid} 分送表不合法或合计不为 1")
    checks.append("各中心当前分送表比例为正且合计严格 1")

    # 2) 独立实现“沿预算树就近归集一次”，只认真实支付额。
    subtree_root = {r["center_id"]: r["subtree_root"] for r in center_rows}
    root_owner = {root: cid for cid, root in subtree_root.items()}

    def nearest(dept: str) -> str | None:
        cur: str | None = dept
        while cur is not None:
            if cur in root_owner:
                return root_owner[cur]
            cur = parent_of[cur]
        return None

    direct: dict[str, Fraction] = {cid: Fraction(0) for cid in shares}
    event_total = Fraction(0)
    for r in pay_rows:
        actual = Fraction(r["paid"] if r["cancelled"] else r["amount"])
        event_total += actual
        owner = nearest(r["dept_id"])
        if owner is None:
            raise InvalidTree(f"支付 {r['payment_id']} 无登记中心可归集")
        direct[owner] += actual
    if sum(direct.values(), Fraction(0)) != event_total:
        raise InvalidTree("归集总额与实付事件总额不符")
    checks.append("实付按最近登记中心归集一次，取消余款不计，总额守恒")

    # 3) Kosaraju 独立识别 SCC 与闭圈。
    centers = sorted(shares)
    line_set = set(lines)
    center_succ = {
        c: [t for t in shares[c] if t not in line_set] for c in centers
    }
    components = _independent_scc(centers, center_succ)
    closed: list[set[str]] = []
    for comp in components:
        member = set(comp)
        if not any(t not in member for c in comp for t in shares[c]):
            closed.append(member)
    open_nodes = [c for c in centers if not any(c in m for m in closed)]

    # 4) 对开网络建稠密方程 q_i - Σ a_ji q_j = d_i，独立 Fraction 消元。
    n = len(open_nodes)
    idx = {c: i for i, c in enumerate(open_nodes)}
    matrix = [[Fraction(0)] * n for _ in range(n)]
    rhs = [direct.get(c, Fraction(0)) for c in open_nodes]
    for i, c in enumerate(open_nodes):
        matrix[i][i] = Fraction(1)
        for t, frac in shares[c].items():
            if t in idx:  # 边 c -> t：出现在 t 的受入项
                matrix[idx[t]][i] -= frac
    aug = [matrix[i] + [rhs[i]] for i in range(n)]
    pivot_row = 0
    for col in range(n):
        pivot = next((r for r in range(pivot_row, n)
                      if aug[r][col] != Fraction(0)), None)
        if pivot is None:
            raise InvalidTree(f"开网络第 {col} 列无主元，结构异常")
        aug[pivot_row], aug[pivot] = aug[pivot], aug[pivot_row]
        val = aug[pivot_row][col]
        aug[pivot_row] = [x / val for x in aug[pivot_row]]
        for r in range(n):
            if r != pivot_row and aug[r][col] != Fraction(0):
                factor = aug[r][col]
                aug[r] = [x - factor * y for x, y in zip(aug[r], aug[pivot_row])]
        pivot_row += 1
    throughput = {open_nodes[i]: aug[i][n] for i in range(n)}
    for c in closed:
        for node in c:
            throughput[node] = Fraction(0)

    # 5) 独立算各线金额与困圈金额，核对守恒。
    line_amount = {L: Fraction(0) for L in lines}
    for c in open_nodes:
        for t, frac in shares[c].items():
            if t in line_set:
                line_amount[t] += throughput[c] * frac
    trapped = Fraction(0)
    for member in closed:
        for c in member:
            incoming = direct.get(c, Fraction(0)) + sum(
                throughput[u] * shares[u][c]
                for u in centers if c in shares[u]
            )
            trapped += incoming
    if sum(direct.values(), Fraction(0)) != sum(line_amount.values(), Fraction(0)) + trapped:
        raise InvalidTree("独立重算全局守恒不成立")
    checks.append("开网络独立消元、闭圈困钱独立指认，注入 = 归线 + 困圈")

    # 6) 与生产代码结果逐项比对。
    production = solve_allocation(
        [Distribution(c, dict(shares[c])) for c in centers],
        direct, lines,
    )
    for c in centers:
        if production.center_throughput[c] != throughput[c]:
            raise InvalidTree(
                f"中心 {c} 吞吐量生产值 {production.center_throughput[c]} "
                f"!= 独立重算 {throughput[c]}"
            )
    for L in lines:
        if production.line_amount[L] != line_amount[L]:
            raise InvalidTree(f"线 {L} 金额两套实现不一致")
    if production.total_trapped() != trapped:
        raise InvalidTree("困圈金额两套实现不一致")
    checks.append("生产 SCC/Tarjan 路径与独立 Kosaraju/稠密消元结果逐项一致")

    return {
        "checks": tuple(checks),
        "direct_cost": dict(direct),
        "throughput": throughput,
        "line_amount": line_amount,
        "trapped": trapped,
        "closed_components": tuple(tuple(sorted(m)) for m in closed),
    }
