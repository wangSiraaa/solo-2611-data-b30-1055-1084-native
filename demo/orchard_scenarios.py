"""果园采果份额与剪枝的端到端演示。

运行：

    python -m demo.orchard_scenarios

依次演示题面给出的工作场景：

1. 主枝分工：两户两串保 2 芽；留芽下限改 3 后不可交付；
2. 份额只够一串：两个果串都在也交不了两户；
3. 一刀 vs 两刀：同数失芽时比较是否多剪无主果；
4. 预算联动：实付/担保 -> 份额 -> 逐户核对（含互相填零被拒）；
5. 两早晨：保改约挂签占用、次日改约不可复原、接真实余额。
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from orgbudget import BudgetService, OrgService
from orchard import (
    AmendmentDecision,
    Adoption,
    BalanceTakeover,
    Claim,
    ClaimLine,
    CommitmentSnapshot,
    HoldItem,
    HouseholdOrder,
    HarvestSession,
    Orchard,
    Planner,
    ReservationBook,
    ShareLedger,
    VerificationError,
    Undeliverable,
    available_branches_for_takeover,
    evaluate_cuts,
    fen_pools,
    honor_amendment,
)
from orchard.errors import AmendmentNotHonorable, ReservationConflict


def _line(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def _check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'[通过]' if ok else '[失败]'} {name}{(' —— ' + detail) if detail else ''}")
    if not ok:
        raise AssertionError(name)


def _household_orchard(min_buds: int) -> Orchard:
    """保留主枝根 M(2 芽)，左 L(1 芽)青梨、右 R(1 芽)黄梨。"""
    o = Orchard()
    o.add_tree("T1")
    o.add_main_branch("T1", "M", buds=2, min_buds=min_buds)
    o.add_branch("T1", "L", "M", buds=1)
    o.add_branch("T1", "R", "M", buds=1)
    o.add_string("SG", "L", row="R1", variety="GREEN", grade="A")
    o.add_string("SY", "R", row="R1", variety="YELLOW", grade="A")
    o.validate()
    return o


def _ledger(paid: int = 5, guarantee: int = 5) -> ShareLedger:
    led = ShareLedger()
    led.adopt(Adoption("CJ", "CJ", "JIA", "R1", "ANY", "A"),
              CommitmentSnapshot("CJ", "JIA", paid, guarantee))
    led.adopt(Adoption("CY", "CY", "YI", "R1", "YELLOW", "A"),
              CommitmentSnapshot("CY", "YI", paid, guarantee))
    return led


def main() -> None:
    # ------------------------------------------------------------ 1. 主枝分工
    _line("案例 1：甲取青、乙取黄，各剪一边，保住 2 芽")
    o = _household_orchard(min_buds=2)
    plan = Planner(o).plan(
        _ledger(),
        [HouseholdOrder("JIA", "JIA", ("SG",)), HouseholdOrder("YI", "YI", ("SY",))],
    )
    print(plan.describe())
    _check("剪左右两刀", plan.cut_nodes == ("L", "R"))
    _check("保住 2 芽", plan.remaining_buds["M"] == 2)
    _check("每串去向明确",
           {a.string_id: a.household_id for a in plan.assignments}
           == {"SG": "JIA", "SY": "YI"})

    print("\n  沿用原钱和订单，把留芽下限从 2 改为 3：")
    o3 = _household_orchard(min_buds=3)
    try:
        Planner(o3).plan(
            _ledger(paid=50, guarantee=50),
            [HouseholdOrder("JIA", "JIA", ("SG",)),
             HouseholdOrder("YI", "YI", ("SY",))],
        )
        raise AssertionError("应当不可交付")
    except Undeliverable as exc:
        print(f"  按预期不可交付：{exc}")
    _check("下限 3 时至多剪一边，再多付款也拿不下两串", True)

    # ------------------------------------------------------------ 2. 份额不足
    _line("案例 2：只取消未付的 5 分，两个果串都在也没法交两户")
    led = ShareLedger()
    led.adopt(Adoption("CJ", "CJ", "JIA", "R1", "ANY", "A"),
              CommitmentSnapshot("CJ", "JIA", paid=5, guarantee=0))
    try:
        Planner(_household_orchard(2)).plan(
            led, [HouseholdOrder("JIA", "JIA", ("SG", "SY"))]
        )
        raise AssertionError("应当因份额不足失败")
    except Undeliverable as exc:
        print(f"  按预期不可交付：{exc}")
    _check("实付 5 只买 1 串，两串交不出一户", True)

    # ------------------------------------------------------------ 3. 一刀 vs 两刀
    _line("案例 3：一刀同剪两种果 + 无人要的串，是否比两刀少损芽？")
    fork = Orchard()
    fork.add_tree("T1")
    fork.add_main_branch("T1", "M", buds=2, min_buds=0)
    fork.add_branch("T1", "X", "M", buds=2)
    fork.add_branch("T1", "L", "X", buds=1)
    fork.add_branch("T1", "R", "X", buds=1)
    fork.add_string("SG", "L", row="R", variety="GREEN", grade="A")
    fork.add_string("SY", "R", row="R", variety="YELLOW", grade="A")
    fork.add_string("SU", "X", row="R", variety="YELLOW", grade="A")  # 无人要
    fork.validate()
    one = evaluate_cuts(fork, ["X"])
    two = evaluate_cuts(fork, ["L", "R"])
    print(f"  一刀剪 X：失芽 {one.buds_lost}，带走无人要 {[s.string_id for s in one.harvested_strings if s.string_id == 'SU']}")
    print(f"  两刀 L,R：失芽 {two.buds_lost}，无主串 {[s.string_id for s in two.harvested_strings if s.string_id not in ('SG','SY')]}")
    _check("一刀并不比两刀少损芽（反而多 2）", one.buds_lost > two.buds_lost)
    _check("一刀还会多剪无主果", any(s.string_id == "SU" for s in one.harvested_strings)
           and not any(s.string_id == "SU" for s in two.harvested_strings))

    # ------------------------------------------------------------ 4. 预算联动
    _line("案例 4：合作社按实付/担保发行份额，逐户核对来源")
    tmp = Path(tempfile.mkdtemp())
    try:
        org = OrgService(tmp / "org.db")
        org.create_department("HQ", None)
        org.create_department("DIV", "HQ")
        org.create_department("JIA", "DIV")
        org.create_department("YI", "DIV")
        product = org.publish("v1")
        budget = BudgetService(tmp / "budget.db", org)
        weights = {(d, k): 1 for d in product.department_ids
                   for k in product.children(d)}
        budget.register_root_budget(product.version, 4_000, weights)
        budget.create_commitment("CJ", "JIA", 10)
        budget.register_spend("CJ", 5, "SJ")
        budget.create_commitment("CY", "YI", 10)
        budget.register_spend("CY", 5, "SY")

        led = ShareLedger()
        led.adopt(Adoption("CJ", "CJ", "JIA", "R1", "ANY", "A"),
                  CommitmentSnapshot.from_commitment(budget.get_commitment("CJ")))
        led.adopt(Adoption("CY", "CY", "YI", "R1", "YELLOW", "A"),
                  CommitmentSnapshot.from_commitment(budget.get_commitment("CY")))
        claim_ok = Claim("JIA", (
            ClaimLine("SG", ("CJ:PAID:0",)),
        ))
        v = led.verify_claim(claim_ok)
        print(f"  核对通过：{v.describe()}")
        _check("每串列明来源承诺/类别", len(v.provenances) == 1)
        # 零头不能互相填零：两笔 2 分 + 3 分也不成串。
        fen_pools(led, {
            "CJ": CommitmentSnapshot("CJ", "JIA", 7, 0),
            "CY": CommitmentSnapshot("CY", "JIA", 8, 0),
        })
        try:
            led.verify_claim(Claim("JIA", (ClaimLine(
                "SX", fen_sources=(("CJ", "PAID", 2), ("CY", "PAID", 3))),)))
            raise AssertionError("应当拒绝互相填零")
        except VerificationError as exc:
            print(f"  零头凑串被拒：{exc}")
        _check("承诺之间不能互相填零", True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ------------------------------------------------------------ 5. 两早晨
    _line("案例 5：两早晨的保改约挂签与接真实余额")
    o = Orchard()
    o.add_tree("T1")
    o.add_main_branch("T1", "M", buds=2, min_buds=0)
    o.add_branch("T1", "L", "M", buds=1)   # 黄
    o.add_branch("T1", "R", "M", buds=1)   # 青
    o.add_string("SY", "L", row="R", variety="YELLOW", grade="A")
    o.add_string("SG", "R", row="R", variety="GREEN", grade="A")
    o.validate()

    book = ReservationBook(o)
    book.hold("H1", "JIA", (HoldItem("STRING", "SY"),))
    print("  给甲挂了保黄签。")
    try:
        book.take_string("SY")
        raise AssertionError("乙首日不应能带走被挂的黄")
    except ReservationConflict as exc:
        print(f"  乙首日不能带走黄：{exc}")
    _check("挂签由实际占用方占据：乙首日不得取黄", True)

    # 若改接乙、次日只剩青：不能事后才说始终保证黄。
    session = HarvestSession(o)
    book.release("H1")
    book.take_string("SY")     # 乙首日取黄
    session.cut("L")           # 乙剪走黄枝
    try:
        honor_amendment(o, session, AmendmentDecision("JIA", "SY", "L"))
        raise AssertionError("黄已被剪走，不能补诺")
    except AmendmentNotHonorable as exc:
        print(f"  次日不能补诺黄：{exc}")
    _check("已剪走的枝回不到树上，不能始终保证黄", True)

    # 接真实余额：甲改约前不替他封候补枝，临时来户能占实际可用的枝。
    session2 = HarvestSession(o)
    takeover = BalanceTakeover("YI", "CY", reserved_branches=frozenset())
    free = available_branches_for_takeover(o, session2, takeover, ("SY",))
    print(f"  接真实余额、无挂签时，乙可占的替枝：{free}")
    _check("未挂签时临时来户可占实际可用的枝", free == ("L",))
    opt = honor_amendment(o, session2, AmendmentDecision("JIA", "SG", "R"))
    print(f"  甲次日改青：从尚存的 R 兑现，实际剪 {opt.branch_id}")
    _check("改约只能从尚实存、满足留芽线的枝兑现", opt.branch_id == "R")

    _line("全部演示完成")


if __name__ == "__main__":
    main()
