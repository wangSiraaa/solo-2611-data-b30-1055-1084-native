"""虚构灌水展台教学例：逐场景复算题面给出的全部口径与判定。

运行：python3 solution/irrigation/run_demo.py
"""

from __future__ import annotations

from hydraulics import Nozzle, Pipe, PipeTree
from scheduling import Bed, enumerate_schedules
from valves import (
    BranchPermits,
    DosingLedger,
    MasterValveBoard,
    ValveError,
    ValveTag,
)

LINE = "=" * 72


def teaching_tree(head: int = 3, uninspected: bool = False) -> PipeTree:
    """干管系数 1、两根末支系数 0；A、B 每嘴流量 1、最低压 2。"""
    return PipeTree(
        root_id="R",
        head=head,
        pipes=[
            Pipe("R", "J", 1, inspected=not uninspected),
            Pipe("J", "A", 0),
            Pipe("J", "B", 0),
        ],
        nozzles=[
            Nozzle("A", label="嘴A", flow=1, min_pressure=2),
            Nozzle("B", label="嘴B", flow=1, min_pressure=2),
        ],
    )


def show(title: str) -> None:
    print("\n" + LINE)
    print(title)
    print(LINE)


def main() -> None:
    # ------------------------------------------------------------------
    show("场景 1｜试水：单开各达标，但成功证不能相加（首压 3）")
    tree = teaching_tree(head=3)
    for name, group in [("只开 A", frozenset({"A"})),
                        ("只开 B", frozenset({"B"})),
                        ("A、B 同开", frozenset({"A", "B"}))]:
        test = tree.pressure_test(group)
        rows = ", ".join(
            f"{r.nozzle_id} 余压 {r.residual}（下限 {r.min_pressure}，"
            f"{'达标' if r.ok else '亏压'}）" for r in test.readings
        )
        print(f"  {name:8s} -> {rows}；本组判定：{'通过' if test.passed else '不通过'}")
    print("  结论：两张单开证不能并成同开许可；同开余压 1 < 2。")

    show("场景 2｜仅把实测首压改成 2：单口余压也只剩 1")
    tree2 = teaching_tree(head=2)
    for group in (frozenset({"A"}), frozenset({"B"})):
        test = tree2.pressure_test(group)
        r = test.readings[0]
        print(f"  单开 {r.nozzle_id} -> 余压 {r.residual}，{'达标' if r.ok else '亏压'}")

    show("场景 3｜跨未检段不给出水许可（干管段标记未检）")
    tree_bad = teaching_tree(head=3, uninspected=True)
    test = tree_bad.pressure_test(frozenset({"A"}))
    print(f"  permitted={test.permitted}，原因：{test.reason}")

    show("场景 4｜重名水嘴是两个具名实体，不能相连")
    dup = PipeTree(
        root_id="R",
        head=3,
        pipes=[Pipe("R", "n1", 1), Pipe("R", "n2", 1)],
        nozzles=[
            Nozzle("n1", label="同标签X", flow=1, min_pressure=2),
            Nozzle("n2", label="同标签X", flow=1, min_pressure=2),
        ],
    )
    print(f"  两个节点 id 不同、标签同为 '同标签X'："
          f"{[n.label for n in dup.nozzles.values()]}，各自独立挂树、不相连接。")

    # ------------------------------------------------------------------
    show("场景 5｜受水单：两畦各需一格、前两格可浇（默认同格总阀、调阀拍 0）")
    beds = [
        Bed("畦A", frozenset({"A"}), frozenset({1, 2}), need=1, gap=0),
        Bed("畦B", frozenset({"B"}), frozenset({1, 2}), need=1, gap=0),
    ]
    board = MasterValveBoard(tree, adjust_beats=0)
    result = enumerate_schedules(beds, [1, 2], board.group_ok)
    print(f"  可交场方案数：{len(result.feasible)}")
    for order in result.feasible:
        text = "；".join(
            f"格{t} " + ("、".join(f"{b}用{z}" for b, z in a.items()) or "空")
            for t, a in sorted(order.assignments.items())
        )
        print("   -", text)
    ab = next(
        o for o in result.feasible
        if o.assignments[1].get("畦A") == "A" and o.assignments[2].get("畦B") == "B"
    )
    ledger = board.execute(ab, DosingLedger())
    print(f"  先 A 后 B 实浇回条：{[(s, b, z) for s, b, z, _ in ledger.records]}，"
          f"耗调阀拍 {board.beats_used}")

    show("场景 6｜两畦都只许第 1 格：有水却排不下")
    beds_tight = [
        Bed("畦A", frozenset({"A"}), frozenset({1}), need=1),
        Bed("畦B", frozenset({"B"}), frozenset({1}), need=1),
    ]
    tight = enumerate_schedules(beds_tight, [1, 2], board.group_ok)
    print(f"  可行方案数：{len(tight.feasible)}；阻塞原因：{tight.blockers}")
    print("  唯一容量解要求格 1 同开 A+B，试水余压 1 < 2，故无单可交。")

    show("场景 7｜为什么不能把两个单开单子合开（同格总阀的解释）")
    for group in (frozenset({"A"}), frozenset({"B"}), frozenset({"A", "B"})):
        print(f"  阀组 {sorted(group)} 总阀台放行？ {board.group_ok(group)}")
    print("  合开后公共段总流量 1+1=2，压降 1×2=2，两嘴到达压 3-2=1，双双亏压。")

    # ------------------------------------------------------------------
    show("场景 8｜换班间歇：一畦浇两次（gap=1），明早能否开")
    # 时格全局连续：上一班末尾为格 0，本班为格 1、2、3。
    # 情形甲：换班前尚未浇过 -> 本班要完成两次受水。
    bed_full = Bed("畦C", frozenset({"A"}), frozenset({1, 2, 3}), need=2, gap=1)
    fresh = enumerate_schedules([bed_full], [1, 2, 3], board.group_ok)
    # 情形乙：换班前格 0 已真浇过一次 -> 本班只剩一格配额，间歇从格 0 续算。
    bed_rest = Bed("畦C", frozenset({"A"}), frozenset({1, 2, 3}), need=1, gap=1)
    waited = enumerate_schedules(
        [bed_rest], [1, 2, 3], board.group_ok, history={"畦C": (0,)}
    )
    print("  甲、换班前未浇过，本班两次受水格组合：",
          sorted({o.watered_slots("畦C") for o in fresh.feasible}))
    print("  乙、换班前格 0 已浇过，本班这一格可开的时格：",
          sorted({o.watered_slots("畦C") for o in waited.feasible}))
    print("  结论：乙情形下明早格 1 必须空出（0→1 之间空 0 格 < gap 1），")
    print("        最早格 2 才能开；换班不重置间歇，格 0 那一格仍算在内。")

    # ------------------------------------------------------------------
    show("场景 9｜方式二：两支各发'单开达标'牌，并开亏压 -> 发牌即拒")
    permits = BranchPermits(tree, {"A": "支1", "B": "支2"})
    permits.issue(ValveTag("T1", "支1", frozenset({"A"})))
    try:
        permits.issue(ValveTag("T2", "支2", frozenset({"B"})))
    except ValveError as exc:
        print("  发第二张牌被拒：", exc)
    print("  守一嘴：收回支1阀牌后，全场活许可只剩 B。")
    permits.collect("支1")
    permits.issue(ValveTag("T2", "支2", frozenset({"B"})))
    print("  活许可并：", set(permits._live_union()),
          "；组 {B} 可自开：", permits.group_ok(frozenset({"B"})))

    show("场景 10｜收走的阀牌换班后不能复用，须重新申领新牌")
    permits.collect("支2")
    try:
        permits.issue(ValveTag("T2", "支2", frozenset({"B"})))  # 同 tag_id
    except ValveError as exc:
        print("  复用旧牌被拒：", exc)
    permits.issue(ValveTag("T3", "支2", frozenset({"B"})))
    print("  以新牌 T3 重新申领成功；活许可：", set(permits._live_union()))

    show("场景 11｜总阀掌握两畦请求、允许 1 个调阀拍：宽窗可交替实浇")
    board1 = MasterValveBoard(tree, adjust_beats=1, free_reconfigure=False)
    wide_beds = [
        Bed("畦A", frozenset({"A"}), frozenset({1, 2, 3}), need=1),
        Bed("畦B", frozenset({"B"}), frozenset({1, 2, 3}), need=1),
    ]
    wide = enumerate_schedules(wide_beds, [1, 2, 3], board1.group_ok)
    alt = next(
        o for o in wide.feasible
        if o.assignments[1].get("畦A") == "A" and o.assignments[3].get("畦B") == "B"
    )
    ledger = board1.execute(alt, DosingLedger())
    print("  方案：格1 浇A -> 格2 调阀拍（无畦记浇）-> 格3 浇B；"
          f"实耗调阀拍 {board1.beats_used}")
    print("  回条：", [(s, b, z) for s, b, z, _ in ledger.records])

    show("场景 12｜再缩短 B 的窗口到只剩格 2：1 拍预算的代价")
    beds_short = [
        Bed("畦A", frozenset({"A"}), frozenset({1, 2, 3}), need=1),
        Bed("畦B", frozenset({"B"}), frozenset({2}), need=1),
    ]
    short = enumerate_schedules(beds_short, [1, 2, 3], board1.group_ok)
    affordable = []
    for order in short.feasible:
        probe = MasterValveBoard(tree, adjust_beats=1, free_reconfigure=False)
        try:
            probe.execute(order, DosingLedger())
            affordable.append(order)
        except ValveError:
            pass
    print(f"  试水+节奏可行方案 {len(short.feasible)} 个；其中 1 调阀拍内可执行："
          f"{len(affordable)} 个")
    for order in short.feasible:
        text = "；".join(
            f"格{t} " + "、".join(f"{b}用{z}" for b, z in a.items())
            for t, a in sorted(order.assignments.items()) if a
        )
        print("   -", text)
    print("  B 被顶死在格 2，A 只能去格 1 或格 3：两种排法里两个非空阀组都相邻，")
    print("  调阀拍必须独占空格而无处安放 -> 1 拍预算内 0 个方案，")
    print("  代价是追加调阀拍（窗口再放宽一格）或放弃一畦。")


if __name__ == "__main__":
    main()
