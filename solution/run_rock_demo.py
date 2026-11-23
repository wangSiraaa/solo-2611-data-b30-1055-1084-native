"""把题目各段叙述逐条落成可复算用例并打印结果。

运行：python3 solution/run_rock_demo.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rock_studio import (  # noqa: E402
    ALONG_SHORE,
    BearingConstraint,
    Block,
    Cut,
    FaceMode,
    LoanBox,
    OrientationCase,
    TurnConstraint,
    allocate,
    plane_relation,
    robust_pair_relation,
)
from rock_late import (  # noqa: E402
    JigTestGrindPolicy,
    LateOrderSpec,
    LateOutcome,
    PreSealPolicy,
)

SRC = "同一处已巡检沿岸岩面"
PATTERN = "层理纹"


def heading(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show_assignments(case: OrientationCase) -> list[dict]:
    assignments = case.feasible_assignments()
    print(f"可行现场朝向解释数：{len(assignments)}")
    for a in assignments[:8]:
        print("  " + ", ".join(f"{k}槽→{v}格" for k, v in a.items()))
    if len(assignments) > 8:
        print(f"  …其余 {len(assignments) - 8} 组省略")
    return assignments


def scenario_stated_series() -> None:
    heading("用例 1-3：甲乙一红一灰、盒需正交，照片与夹具三态")

    red = Block("甲", "红", PATTERN, SRC, FaceMode.SLOT_ONLY)
    gray = Block("乙", "灰", PATTERN, SRC, FaceMode.SLOT_ONLY)
    box = LoanBox("正交教材盒", ("红", "灰"), "正交", source=SRC)

    # 用例 1：乙槽比甲槽顺转一格 —— 原位恒正交。
    case1 = OrientationCase(
        ("甲", "乙"),
        turn_constraints=[TurnConstraint("甲", "乙", (1,))],
    )
    a1 = show_assignments(case1)
    r1 = allocate([box], [red, gray], a1)
    print(
        f"[1] 乙顺转一格：稳健关系={robust_pair_relation(red, gray, a1)}，"
        f"发出 {r1.box_count} 盒 -> {[f.box.name for f in r1.fills]}，"
        f"切单={ {k: (v.face.value, v.plane) for k, v in r1.cuts.items()} }"
    )

    # 用例 2：可能转 0 格或 1 格 —— 平行/正交都可能，正交不再保证。
    case2 = OrientationCase(
        ("甲", "乙"),
        turn_constraints=[TurnConstraint("甲", "乙", (0, 1))],
    )
    a2 = show_assignments(case2)
    print(f"[2] 转 0 或 1 格：稳健关系={robust_pair_relation(red, gray, a2)}（None=两者皆可能）")
    r2 = allocate([box], [red, gray], a2)
    print(f"    配发结果：{r2.box_count} 盒，未发盒={r2.unissued}（题述：最多零盒）")

    # 用例 3：恢复原始照片，仅灰石夹具改邻面 —— 两面恒平行。
    gray_adj = Block("乙", "灰", PATTERN, SRC, FaceMode.ADJACENT_ONLY)
    r3_rel = robust_pair_relation(red, gray_adj, a1)
    r3 = allocate([box], [red, gray_adj], a1)
    print(f"[3] 灰石只能邻面：稳健关系={r3_rel}，配发 {r3.box_count} 盒，未发={r3.unissued}（题述：零盒）")


def scenario_two_museums() -> None:
    heading("用例 4：第三块可替代料——两馆各能单借，同时不能各得一套")

    r1 = Block("红一", "红", PATTERN, SRC, FaceMode.SLOT_ONLY)
    g1 = Block("灰一", "灰", PATTERN, SRC, FaceMode.SLOT_ONLY)
    g2 = Block("灰二(替代)", "灰", PATTERN, SRC, FaceMode.SLOT_ONLY)
    case = OrientationCase(
        ("红一", "灰一", "灰二(替代)"),
        turn_constraints=[
            TurnConstraint("红一", "灰一", (1,)),       # 红一-灰一恒正交
            TurnConstraint("红一", "灰二(替代)", (1,)),  # 红一-灰二亦恒正交
        ],
    )
    assignments = case.feasible_assignments()
    box_m1 = LoanBox("甲馆正交盒", ("红", "灰"), "正交", source=SRC)
    box_m2 = LoanBox("乙馆正交盒", ("红", "灰"), "正交", source=SRC)

    alone1 = allocate([box_m1], [r1, g1, g2], assignments)
    alone2 = allocate([box_m2], [r1, g1, g2], assignments)
    together = allocate([box_m1, box_m2], [r1, g1, g2], assignments)
    print(f"甲馆单借：{alone1.box_count} 盒（用 {[f.block_b.name for f in alone1.fills]}）")
    print(f"乙馆单借：{alone2.box_count} 盒")
    print(
        f"两馆同借：仅 {together.box_count} 盒，未发={together.unissued}"
        f"——红料只有一块，两套盒争同一红件，不能各得一套"
    )


def scenario_conflicting_photos() -> None:
    heading("用例 5：照片相抵——定位互相打架的原始约束")

    case = OrientationCase(
        ("甲", "乙"),
        bearing_constraints=[
            BearingConstraint("甲", (ALONG_SHORE,)),
            BearingConstraint("乙", (ALONG_SHORE,)),
        ],
        turn_constraints=[
            TurnConstraint("甲", "乙", (1,)),   # 与两槽同朝向直接矛盾
        ],
    )
    assignments = case.feasible_assignments()
    print(f"可行解释数：{len(assignments)}")
    bad_b, bad_t = case.conflicting_constraints()
    print("相抵原件：")
    for c in bad_b:
        print(f"  朝向约束 料={c.block} 允许={c.bearings}")
    for c in bad_t:
        print(f"  转格约束 {c.block_a}->{c.block_b} 允许格={c.turns}")


def scenario_late_orders() -> None:
    heading("用例 6-7：原位平行料的早盒与晚盒（封料 vs 定位架试纹）")

    # 早盒有自己的一对料；晚盒候补是另一红一灰，互不挤占。
    red_early = Block("早红", "红", PATTERN, SRC, FaceMode.EITHER, thickness=2)
    gray_early = Block("早灰", "灰", PATTERN, SRC, FaceMode.EITHER, thickness=2)
    red = Block("候补红", "红", PATTERN, SRC, FaceMode.EITHER, thickness=3)
    gray = Block("候补灰", "灰", PATTERN, SRC, FaceMode.EITHER, thickness=3)
    all_blocks = [red_early, gray_early, red, gray]
    # 原位平行：任意两槽格数差恒为 0。
    names = tuple(b.name for b in all_blocks)
    case = OrientationCase(
        names,
        turn_constraints=[
            TurnConstraint(names[0], name, (0,)) for name in names[1:]
        ],
    )
    assignments = case.feasible_assignments()
    early = [LoanBox("早盒(平行)", ("红", "灰"), "平行", source=SRC)]
    late_spec = LateOrderSpec(
        box_name="晚盒",
        colors=("红", "灰"),
        possible_relations=("平行", "正交"),
        allowed_loss=1,
        max_return_gap=2,
        source=SRC,
    )

    # 路径 A：把候补红首刀前封给晚盒，且已按平行切好胶封。
    # 题述“遇正交只能拒”对应夹具只能沿槽的封料对：红锁槽面后，灰槽面
    # 与其恒平行，不存在邻面选项，正交单无稳健面组合。
    red_slot = Block("候补红(槽夹)", "红", PATTERN, SRC, FaceMode.SLOT_ONLY, thickness=3)
    gray_slot = Block("候补灰(槽夹)", "灰", PATTERN, SRC, FaceMode.SLOT_ONLY, thickness=3)
    blocks_a = [red_early, gray_early, red_slot, gray_slot]
    names_a = tuple(b.name for b in blocks_a)
    case_a = OrientationCase(
        names_a,
        turn_constraints=[
            TurnConstraint(names_a[0], name, (0,)) for name in names_a[1:]
        ],
    )
    assign_a = case_a.feasible_assignments()
    sealed_red = Cut(
        block="候补红(槽夹)",
        plane=assign_a[0]["候补红(槽夹)"],
        face=FaceMode.SLOT_ONLY,
    )
    pre = PreSealPolicy(
        blocks_a,
        assign_a,
        reservations={"候补红(槽夹)": "晚盒"},
        cuts={"候补红(槽夹)": sealed_red},
    )
    print("路径A：首刀前封一块已切平行的槽夹红料给晚盒（两面都无邻面可选）")
    for outcome, verdict in pre.serve(early, late_spec).items():
        print(
            f"  晚单={outcome.value:>11} 发盒={verdict.issued} "
            f"拒晚单={verdict.refused_late} | {verdict.reason}"
        )

    # 路径 B：候补红/灰为两面可选夹具，不预切、保留到晚单通知。
    jig = JigTestGrindPolicy(all_blocks, assignments)
    print("\n路径B：两面可选的候补红灰保留原块 + 双台定位架（许可磨 1 层）")
    verdicts = jig.adaptive_serve(
        early, ["候补红", "候补灰"], late_spec
    )
    for outcome, verdict in verdicts.items():
        print(
            f"  晚单={outcome.value:>11} 发盒={verdict.issued} "
            f"拒晚单={verdict.refused_late} | {verdict.reason}"
        )

    # 路径 C：通知到达前最后候补红料已被切成平行并胶封（槽面夹具，无邻面可选）。
    print("\n路径C：最后候补红料通知前已切平行并胶封")
    last_red = Block("末红", "红", PATTERN, SRC, FaceMode.SLOT_ONLY, thickness=1)
    gray_c = Block("灰C", "灰", PATTERN, SRC, FaceMode.SLOT_ONLY, thickness=2)
    case_c = OrientationCase(
        ("末红", "灰C"),
        turn_constraints=[TurnConstraint("末红", "灰C", (0,))],
    )
    assign_c = case_c.feasible_assignments()
    sealed_last = Cut(
        block="末红",
        plane=assign_c[0]["末红"],
        face=FaceMode.SLOT_ONLY,
    )
    late_box_orth = LoanBox("晚盒(正交)", ("红", "灰"), "正交", source=SRC)
    res = allocate(
        [late_box_orth],
        [last_red, gray_c],
        assign_c,
        existing_cuts={"末红": sealed_last},
    )
    print(
        f"  正交晚盒配发：{res.box_count} 盒，未发={res.unissued}"
        f"——胶封平行截面无法重新锯成正交，即使拿到全套照片"
    )
    # 对照：若晚单其实是平行，同一封料可直接随截面转出，不必重切。
    late_box_par = LoanBox("晚盒(平行)", ("红", "灰"), "平行", source=SRC)
    res_par = allocate(
        [late_box_par],
        [last_red, gray_c],
        assign_c,
        existing_cuts={"末红": sealed_last},
    )
    print(
        f"  对照平行晚盒：{res_par.box_count} 盒（封料截面正好满足，随料转出）"
    )

    # 厚度不足时试纹被规则直接拒绝（独立小块，不进任何配盒）。
    thin = Block("薄红", "红", PATTERN, SRC, FaceMode.EITHER, thickness=1)
    thin_case = OrientationCase(("薄红",))
    jig_thin = JigTestGrindPolicy([thin], thin_case.feasible_assignments())
    print(
        "\n许可损耗=1 而薄红厚度=1（磨完不够再切一次）："
        f"can_test_grind={jig_thin.can_test_grind('薄红', late_spec)}"
    )


def scenario_max_boxes() -> None:
    heading("用例 8：最多八块原料、四盒——按两侧同时需要的实料算最大配发")

    blocks = [
        Block(f"红{i}", "红", PATTERN, SRC, FaceMode.EITHER) for i in range(1, 5)
    ] + [
        Block(f"灰{i}", "灰", PATTERN, SRC, FaceMode.EITHER) for i in range(1, 5)
    ]
    names = tuple(b.name for b in blocks)
    # 全部槽向相同：任意红灰在同面下平行、异面下正交，面可选 => 两种盒都能稳配。
    case = OrientationCase(
        names,
        turn_constraints=[
            TurnConstraint(names[0], name, (0,)) for name in names[1:]
        ],
    )
    assignments = case.feasible_assignments()
    boxes = [
        LoanBox(f"盒{i}", ("红", "灰"), "正交" if i % 2 else "平行", source=SRC)
        for i in range(1, 5)
    ]
    result = allocate(boxes, blocks, assignments)
    print(f"发出 {result.box_count} 盒（上限 4 盒 / 8 料）：")
    for f in result.fills:
        print(
            f"  {f.box.name}（{f.relation}）= {f.block_a.name}({f.face_a.value})"
            f" + {f.block_b.name}({f.face_b.value})"
        )
    print("切单：")
    for name, cut in result.cuts.items():
        print(f"  {name}: 面={cut.face.value} 切面向={cut.plane}格")

    # 在途片不能再装盒：让一块灰料在途，最大盒数降到 3。
    blocks[7].in_transit = True
    result_transit = allocate(boxes, blocks, assignments)
    print(
        f"\n灰4 在途归还中：发出 {result_transit.box_count} 盒，"
        f"未发={result_transit.unissued}（在途片不参与配盒）"
    )


def scenario_cut_relation_primer() -> None:
    heading("附：给定两件、各沿指定本地面下刀时的独立判定")
    # 照片只知乙相对甲转 0 或 1 格；甲沿槽面、乙沿槽面。
    case = OrientationCase(
        ("甲", "乙"),
        turn_constraints=[TurnConstraint("甲", "乙", (0, 1))],
    )
    assignments = case.feasible_assignments()
    red = Block("甲", "红", PATTERN, SRC, FaceMode.SLOT_ONLY)
    gray = Block("乙", "灰", PATTERN, SRC, FaceMode.SLOT_ONLY)
    observed = {
        plane_relation(a["甲"], a["乙"]) for a in assignments
    }
    print(f"两料都沿槽面，解释集内关系集合={observed} => 两者皆可能，不能装关系盒")
    rel = robust_pair_relation(red, gray, assignments)
    print(f"robust_pair_relation = {rel}")


if __name__ == "__main__":
    scenario_stated_series()
    scenario_two_museums()
    scenario_conflicting_photos()
    scenario_late_orders()
    scenario_max_boxes()
    scenario_cut_relation_primer()
