"""石样装盒规则的回归测试。

运行：python3 -m unittest discover -s solution/tests -v
（本文件与既有里程题测试并存，互不依赖。）
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
    plane_for_cut,
    plane_relation,
    robust_face_pairs,
    robust_pair_relation,
)
from rock_late import (  # noqa: E402
    JigTestGrindPolicy,
    LateOrderSpec,
    LateOutcome,
    PreSealPolicy,
)

SRC = "巡检沿岸岩面"
PAT = "层理纹"


def block(name, color, mode, **kw):
    return Block(name, color, PAT, SRC, mode, **kw)


class OrientationTests(unittest.TestCase):
    def test_relative_turn_one_quarter_is_robust_orthogonal(self):
        case = OrientationCase(
            ("甲", "乙"),
            turn_constraints=[TurnConstraint("甲", "乙", (1,))]
        )
        assignments = case.feasible_assignments()
        self.assertEqual(len(assignments), 4)
        red = block("甲", "红", FaceMode.SLOT_ONLY)
        gray = block("乙", "灰", FaceMode.SLOT_ONLY)
        self.assertEqual(robust_pair_relation(red, gray, assignments), "正交")

    def test_turn_zero_or_one_is_ambiguous(self):
        case = OrientationCase(
            ("甲", "乙"),
            turn_constraints=[TurnConstraint("甲", "乙", (0, 1))]
        )
        assignments = case.feasible_assignments()
        red = block("甲", "红", FaceMode.SLOT_ONLY)
        gray = block("乙", "灰", FaceMode.SLOT_ONLY)
        self.assertIsNone(robust_pair_relation(red, gray, assignments))

    def test_adjacent_only_fixture_forces_parallel(self):
        case = OrientationCase(
            ("甲", "乙"),
            turn_constraints=[TurnConstraint("甲", "乙", (1,))]
        )
        assignments = case.feasible_assignments()
        red = block("甲", "红", FaceMode.SLOT_ONLY)
        gray = block("乙", "灰", FaceMode.ADJACENT_ONLY)
        self.assertEqual(robust_pair_relation(red, gray, assignments), "平行")

    def test_conflicting_photos_identify_originals(self):
        case = OrientationCase(
            ("甲", "乙"),
            bearing_constraints=[
                BearingConstraint("甲", (ALONG_SHORE,)),
                BearingConstraint("乙", (ALONG_SHORE,)),
            ],
            turn_constraints=[TurnConstraint("甲", "乙", (1,))],
        )
        self.assertEqual(case.feasible_assignments(), [])
        bad_b, bad_t = case.conflicting_constraints()
        self.assertTrue(bad_b and bad_t)

    def test_opposite_directions_share_plane(self):
        # 0 与 2 格相反，属同一切面方向 => 平行。
        self.assertEqual(plane_relation(0, 2), "平行")
        self.assertEqual(plane_relation(1, 3), "平行")
        self.assertEqual(plane_relation(0, 1), "正交")


class AllocationTests(unittest.TestCase):
    def setUp(self):
        self.case = OrientationCase(
            ("甲", "乙"),
            turn_constraints=[TurnConstraint("甲", "乙", (1,))]
        )
        self.assignments = self.case.feasible_assignments()
        self.box = LoanBox("正交盒", ("红", "灰"), "正交", source=SRC)

    def test_orthogonal_box_issues_when_guaranteed(self):
        red = block("甲", "红", FaceMode.SLOT_ONLY)
        gray = block("乙", "灰", FaceMode.SLOT_ONLY)
        result = allocate([self.box], [red, gray], self.assignments)
        self.assertEqual(result.box_count, 1)
        self.assertEqual(result.unissued, [])

    def test_ambiguous_relation_issues_zero_boxes(self):
        case = OrientationCase(
            ("甲", "乙"),
            turn_constraints=[TurnConstraint("甲", "乙", (0, 1))]
        )
        red = block("甲", "红", FaceMode.SLOT_ONLY)
        gray = block("乙", "灰", FaceMode.SLOT_ONLY)
        result = allocate([self.box], [red, gray], case.feasible_assignments())
        self.assertEqual(result.box_count, 0)
        self.assertEqual(result.unissued, ["正交盒"])

    def test_adjacent_fixture_parallel_cannot_fill_orthogonal_box(self):
        red = block("甲", "红", FaceMode.SLOT_ONLY)
        gray = block("乙", "灰", FaceMode.ADJACENT_ONLY)
        result = allocate([self.box], [red, gray], self.assignments)
        self.assertEqual(result.box_count, 0)

    def test_two_museums_share_single_red_block(self):
        r1 = block("红一", "红", FaceMode.SLOT_ONLY)
        g1 = block("灰一", "灰", FaceMode.SLOT_ONLY)
        g2 = block("灰二", "灰", FaceMode.SLOT_ONLY)
        case = OrientationCase(
            ("红一", "灰一", "灰二"),
            turn_constraints=[
                TurnConstraint("红一", "灰一", (1,)),
                TurnConstraint("红一", "灰二", (1,)),
            ],
        )
        a = case.feasible_assignments()
        m1 = LoanBox("甲馆", ("红", "灰"), "正交", source=SRC)
        m2 = LoanBox("乙馆", ("红", "灰"), "正交", source=SRC)
        self.assertEqual(allocate([m1], [r1, g1, g2], a).box_count, 1)
        self.assertEqual(allocate([m2], [r1, g1, g2], a).box_count, 1)
        together = allocate([m1, m2], [r1, g1, g2], a)
        self.assertEqual(together.box_count, 1)

    def test_in_transit_block_cannot_be_boxed(self):
        red = block("甲", "红", FaceMode.SLOT_ONLY)
        gray = block("乙", "灰", FaceMode.SLOT_ONLY, in_transit=True)
        result = allocate([self.box], [red, gray], self.assignments)
        self.assertEqual(result.box_count, 0)

    def test_sealed_cut_cannot_be_reoriented(self):
        # 槽夹红料已平行胶封；灰也是槽夹，原位平行，正交盒无面可选。
        case = OrientationCase(
            ("末红", "灰C"),
            turn_constraints=[TurnConstraint("末红", "灰C", (0,))],
        )
        a = case.feasible_assignments()
        red = block("末红", "红", FaceMode.SLOT_ONLY, thickness=1)
        gray = block("灰C", "灰", FaceMode.SLOT_ONLY)
        sealed = Cut("末红", a[0]["末红"], FaceMode.SLOT_ONLY)
        orth = LoanBox("正交晚盒", ("红", "灰"), "正交", source=SRC)
        self.assertEqual(
            allocate([orth], [red, gray], a, existing_cuts={"末红": sealed}).box_count,
            0,
        )
        par = LoanBox("平行晚盒", ("红", "灰"), "平行", source=SRC)
        self.assertEqual(
            allocate([par], [red, gray], a, existing_cuts={"末红": sealed}).box_count,
            1,
        )

    def test_max_eight_blocks_four_boxes(self):
        blocks = [
            block(f"红{i}", "红", FaceMode.EITHER) for i in range(1, 5)
        ] + [block(f"灰{i}", "灰", FaceMode.EITHER) for i in range(1, 5)]
        names = tuple(b.name for b in blocks)
        case = OrientationCase(
            names,
            turn_constraints=[
                TurnConstraint(names[0], n, (0,)) for n in names[1:]
            ],
        )
        a = case.feasible_assignments()
        boxes = [
            LoanBox(f"盒{i}", ("红", "灰"), "正交" if i % 2 else "平行", source=SRC)
            for i in range(1, 5)
        ]
        result = allocate(boxes, blocks, a)
        self.assertEqual(result.box_count, 4)
        self.assertEqual(len(result.cuts), 8)


class LatePolicyTests(unittest.TestCase):
    def _world(self, red_mode=FaceMode.EITHER, gray_mode=FaceMode.EITHER):
        early = [
            block("早红", "红", FaceMode.EITHER, thickness=2),
            block("早灰", "灰", FaceMode.EITHER, thickness=2),
        ]
        late_blocks = [
            block("候补红", "红", red_mode, thickness=3),
            block("候补灰", "灰", gray_mode, thickness=3),
        ]
        all_blocks = early + late_blocks
        names = tuple(b.name for b in all_blocks)
        case = OrientationCase(
            names,
            turn_constraints=[
                TurnConstraint(names[0], n, (0,)) for n in names[1:]
            ],
        )
        return all_blocks, case.feasible_assignments()

    def _spec(self):
        return LateOrderSpec(
            box_name="晚盒",
            colors=("红", "灰"),
            possible_relations=("平行", "正交"),
            allowed_loss=1,
            max_return_gap=2,
            source=SRC,
        )

    def test_presealed_parallel_rejects_orthogonal_with_slot_fixtures(self):
        blocks, a = self._world(
            FaceMode.SLOT_ONLY, FaceMode.SLOT_ONLY
        )
        sealed = Cut("候补红", a[0]["候补红"], FaceMode.SLOT_ONLY)
        policy = PreSealPolicy(
            blocks,
            a,
            reservations={"候补红": "晚盒"},
            cuts={"候补红": sealed},
        )
        early = [LoanBox("早盒", ("红", "灰"), "平行", source=SRC)]
        verdicts = policy.serve(early, self._spec())
        self.assertFalse(verdicts[LateOutcome.PARALLEL].refused_late)
        self.assertTrue(verdicts[LateOutcome.ORTHOGONAL].refused_late)

    def test_jig_keeps_both_relations_feasible_within_loss_budget(self):
        blocks, a = self._world()
        policy = JigTestGrindPolicy(blocks, a)
        early = [LoanBox("早盒", ("红", "灰"), "平行", source=SRC)]
        verdicts = policy.adaptive_serve(
            early, ["候补红", "候补灰"], self._spec()
        )
        self.assertFalse(verdicts[LateOutcome.PARALLEL].refused_late)
        self.assertFalse(verdicts[LateOutcome.ORTHOGONAL].refused_late)

    def test_jig_refused_when_thickness_cannot_cover_loss_plus_cut(self):
        thin = block("薄红", "红", FaceMode.EITHER, thickness=1)
        policy = JigTestGrindPolicy(
            [thin], OrientationCase(("薄红",)).feasible_assignments()
        )
        self.assertFalse(policy.can_test_grind("薄红", self._spec()))

    def test_return_gap_enforced(self):
        spec = self._spec()  # max_return_gap = 2
        self.assertTrue(spec.return_within_gap(2))
        self.assertFalse(spec.return_within_gap(3))
        self.assertFalse(spec.return_within_gap(None))

    def test_test_grind_consumes_face_and_thickness(self):
        red = block("候补红", "红", FaceMode.EITHER, thickness=3)
        gray = block("候补灰", "灰", FaceMode.EITHER, thickness=3)
        case = OrientationCase(
            ("候补红", "候补灰"),
            turn_constraints=[TurnConstraint("候补红", "候补灰", (0,))],
        )
        policy = JigTestGrindPolicy(
            [red, gray], case.feasible_assignments()
        )
        st = policy.test_grind("候补红", FaceMode.ADJACENT_ONLY, self._spec())
        self.assertEqual(st.thickness, 2)
        self.assertIn(FaceMode.ADJACENT_ONLY, st.test_ground_faces)
        self.assertEqual(st.remaining_modes(), (FaceMode.SLOT_ONLY,))


if __name__ == "__main__":
    unittest.main()
