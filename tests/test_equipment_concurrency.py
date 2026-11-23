"""设备会计跨进程并发：两个进程争同一期限额，最多一方拨付成功。"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import unittest

from tests.support import EquipmentTestCase


class DisburseProcessRaceTest(EquipmentTestCase):
    def test_two_processes_race_only_one_wins(self) -> None:
        self.build_workshop()
        self.equipment.register_equipment("MA", "W", 1, {"PA": 2})
        self.equipment.register_equipment("MB", "W", 1, {"PB": 2})
        self.equipment.set_loss_limit("W", 1, 6)

        worker = textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {os.getcwd()!r})
            from orgbudget import BudgetService, EquipmentService, OrgService
            from orgbudget.errors import InsufficientLossAllowance
            org = OrgService({str(self.org.db_path)!r})
            budget = BudgetService({str(self.budget.db_path)!r}, org,
                                  busy_timeout_ms=30000)
            svc = EquipmentService({str(self.equipment.db_path)!r}, budget,
                                   busy_timeout_ms=30000)
            try:
                svc.disburse_to_cabinet(sys.argv[1], "W", [sys.argv[2]],
                                        f"CAB-{{sys.argv[1]}}", 4)
                print("ok")
            except InsufficientLossAllowance:
                print("reject")
            """
        )
        env = dict(os.environ, PYTHONPATH=os.getcwd())
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", worker, f"D-{tag}", device],
                stdout=subprocess.PIPE,
                env=env,
                text=True,
            )
            for tag, device in (("A", "MA"), ("B", "MB"))
        ]
        outcomes = sorted(p.communicate()[0].strip() for p in procs)
        self.assertEqual([p.returncode for p in procs], [0, 0])
        self.assertEqual(outcomes, ["ok", "reject"])
        allowance = self.equipment.loss_allowance("W", 1)
        self.assertEqual((allowance.outstanding, allowance.available), (4, 2))


if __name__ == "__main__":
    unittest.main()
