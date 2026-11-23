"""披露视图测试：完整坐标、最小次序与未授权位置隔离。"""

from __future__ import annotations

import unittest

from review.calibration import admit
from review.disclosure import full_coordinate_view, minimal_ordering_view
from review.fixtures import BALL, confirmed_case
from review.model import Side


class TestDisclosure(unittest.TestCase):
    def setUp(self):
        self.case = confirmed_case()
        self.interp = admit(self.case.cameras).interpretations[0]

    def test_full_view_signs_out_synchronized_coordinates(self):
        view = full_coordinate_view(
            self.case, self.interp, (5,),
            frozenset({"R", BALL, "D1", "D2"}))
        by_label = {row.label: row.true_position for row in view.rows}
        self.assertEqual(by_label, {"R": 61, BALL: 50, "D1": 90, "D2": 60})
        self.assertEqual(view.withheld_labels, ("P",))

    def test_minimal_view_withholds_unauthorized_raw_positions(self):
        view = minimal_ordering_view(
            self.case, self.interp, (5,), Side.HOME,
            frozenset({"R", BALL}))
        statement = view.statements[0]
        self.assertEqual(statement.order, ("R", BALL))
        self.assertEqual(statement.ball_boundary, "R > ball")
        # D1/D2/P 的原始坐标绝不出现在最小视图任何字段
        payload = repr(statement)
        for label in ("D1", "D2", "P"):
            self.assertNotIn(label, payload)
        self.assertEqual(set(view.withheld_labels), {"D1", "D2", "P"})

    def test_minimal_view_equality_relation(self):
        # 正式 6：R=60 与 D2 平齐；最小视图只授权 R 与 D2
        from review.fixtures import rejected_case
        case = rejected_case()
        interp = admit(case.cameras).interpretations[0]
        view = minimal_ordering_view(
            case, interp, (6,), Side.HOME, frozenset({"R", "D2"}))
        # 平齐关系与标签先后无关，只断言存在 "=" 且两对象都在关系串里
        equality = [r for r in view.statements[0].relations if "=" in r]
        self.assertEqual(len(equality), 1)
        self.assertIn("R", equality[0])
        self.assertIn("D2", equality[0])


if __name__ == "__main__":
    unittest.main()
