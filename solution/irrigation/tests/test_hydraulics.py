"""试水压力与干管树口径测试。"""

import unittest

import _path  # noqa: F401  统一样包目录到 sys.path
from hydraulics import HydraulicError, Nozzle, Pipe, PipeTree


def base_tree(head: int = 3, uninspected_pipe: bool = False,
              uninspected_nozzle: bool = False) -> PipeTree:
    return PipeTree(
        root_id="R",
        head=head,
        pipes=[
            Pipe("R", "J", 1, inspected=not uninspected_pipe),
            Pipe("J", "A", 0),
            Pipe("J", "B", 0),
        ],
        nozzles=[
            Nozzle("A", label="嘴A", flow=1, min_pressure=2,
                   inspected=not uninspected_nozzle),
            Nozzle("B", label="嘴B", flow=1, min_pressure=2),
        ],
    )


class PressureTests(unittest.TestCase):
    def test_single_open_passes_at_head_three(self):
        tree = base_tree()
        for nozzle in ("A", "B"):
            test = tree.pressure_test({nozzle})
            self.assertTrue(test.permitted)
            self.assertEqual(test.readings[0].residual, 2)
            self.assertTrue(test.passed)

    def test_joint_open_fails_and_single_certs_do_not_add(self):
        tree = base_tree()
        joint = tree.pressure_test({"A", "B"})
        self.assertEqual({r.residual for r in joint.readings}, {1})
        self.assertFalse(joint.passed)
        # 子集分别达标，不蕴含并开达标：逐子集独立出证。
        subsets = tree.all_subset_tests()
        self.assertTrue(subsets[frozenset({"A"})].passed)
        self.assertTrue(subsets[frozenset({"B"})].passed)
        self.assertFalse(subsets[frozenset({"A", "B"})].passed)

    def test_head_two_makes_single_open_fail(self):
        tree = base_tree(head=2)
        test = tree.pressure_test({"A"})
        self.assertEqual(test.readings[0].residual, 1)
        self.assertFalse(test.passed)

    def test_uninspected_pipe_blocks_permit(self):
        tree = base_tree(uninspected_pipe=True)
        test = tree.pressure_test({"A"})
        self.assertFalse(test.permitted)
        self.assertIn("未检", test.reason)
        self.assertEqual(test.readings, ())

    def test_uninspected_nozzle_blocks_permit(self):
        tree = base_tree(uninspected_nozzle=True)
        self.assertFalse(tree.pressure_test({"A"}).permitted)
        # 另一嘴的管段全检，仍可独立出证。
        self.assertTrue(tree.pressure_test({"B"}).passed)

    def test_duplicate_labels_are_distinct_entities(self):
        tree = PipeTree(
            root_id="R",
            head=3,
            pipes=[Pipe("R", "n1", 1), Pipe("R", "n2", 1)],
            nozzles=[
                Nozzle("n1", label="重号X", flow=1, min_pressure=2),
                Nozzle("n2", label="重号X", flow=1, min_pressure=2),
            ],
        )
        self.assertEqual(tree.nozzles["n1"].label, tree.nozzles["n2"].label)
        self.assertIsNot(tree.nozzles["n1"], tree.nozzles["n2"])
        # 两嘴各自独立挂在根下（两条兄弟管，没有 n1->n2 相连管），并开时
        # 按两个实体各算各的流量。
        test = tree.pressure_test({"n1", "n2"})
        self.assertEqual(len(test.readings), 2)

    def test_non_tree_and_unknown_nozzle_rejected(self):
        with self.assertRaises(HydraulicError):
            PipeTree(
                root_id="R", head=3,
                pipes=[Pipe("R", "x", 1), Pipe("y", "x", 1)],
                nozzles=[Nozzle("x", "x", 1, 1)],
            )
        with self.assertRaises(HydraulicError):
            base_tree().pressure_test({"ghost"})

    def test_integer_pressure_with_nonzero_branch_coeff(self):
        # 根->X 系数2，X->N 系数3；单嘴流量2：2*2 + 3*2 = 10，首压 12 -> 余压 2。
        tree = PipeTree(
            root_id="R", head=12,
            pipes=[Pipe("R", "X", 2), Pipe("X", "N", 3)],
            nozzles=[Nozzle("N", "N", flow=2, min_pressure=2)],
        )
        self.assertEqual(tree.pressure_test({"N"}).readings[0].residual, 2)


if __name__ == "__main__":
    unittest.main()
