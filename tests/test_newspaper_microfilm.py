"""缩微订补服务的 unittest 覆盖。

覆盖：所有仍成立排年中的稳定补法、已有期不得再贴、合刊必须同双格且顺序固定、
集中拍（在途/查阅黑窗）、副片（有号片不跨卷、出馆冻结、错片另用余量）、同案
保全不登记整案已到与在馆举证、迟到重读下的旧签保留/新签重开。
"""

from __future__ import annotations

import unittest

from newspaper import (
    ChronologyProduct,
    CollationEngine,
    CollationError,
    MicrofilmOrderService,
    PreservationHold,
    ServiceWindow,
    Slot,
    SurrogateStock,
    UnfillableOrder,
)
from newspaper.scenario import (
    amended_almanac,
    base_almanac,
    base_issues,
    base_surrogates,
    freeze,
)


def product(product_id: str = "CH1", **kwargs) -> ChronologyProduct:
    engine = CollationEngine(base_almanac(), [0, 1, 2, 3, 4, 5], base_issues())
    return freeze(engine, product_id, **kwargs)


def window(**kwargs) -> ServiceWindow:
    base = dict(latest_proof_day=12, reader_blackout=(8, 11), ship_days=1)
    base.update(kwargs)
    return ServiceWindow(**base)


class StableFilling(unittest.TestCase):
    def setUp(self):
        self.svc = MicrofilmOrderService()
        self.svc.ingest_product(product())

    def test_missing_days_3_4_fill_once_via_double_slot(self):
        order = self.svc.plan_order(
            "M1", ["CH1"], [Slot("K1", "double", (3, 4))], window(),
            existing_filled_days=[0, 1, 2, 5],
        )
        self.assertEqual(order.assignments[0]["issue_id"], "I2")
        self.assertEqual(order.assignments[0]["missing_days"], [3, 4])

    def test_already_filmed_day_cannot_be_reshot(self):
        with self.assertRaises(UnfillableOrder) as ctx:
            self.svc.plan_order(
                "M2", ["CH1"], [Slot("K1", "double", (3, 4))], window(),
                existing_filled_days=[3],
            )
        self.assertEqual(ctx.exception.code, "already_filmed")

    def test_two_single_slots_cannot_take_combined_issue(self):
        with self.assertRaises(UnfillableOrder) as ctx:
            self.svc.plan_order(
                "M3", ["CH1"],
                [Slot("K1", "single", (3,)), Slot("K2", "single", (4,))],
                window(), existing_filled_days=[0, 1, 2, 5],
            )
        self.assertEqual(ctx.exception.code, "combined_requires_double")

    def test_double_slot_rejects_plain_single_issue(self):
        # 日1（单期 I1）与日2（单期 I2S）分属两张原件，一个 double 格无法整体承接。
        with self.assertRaises(UnfillableOrder) as ctx:
            self.svc.plan_order(
                "M4", ["CH1"], [Slot("K1", "double", (1, 2))], window(),
            )
        self.assertEqual(ctx.exception.code, "slot_crosses_sheets")

    def test_duplicate_missing_day_across_slots_rejected(self):
        with self.assertRaises(CollationError):
            self.svc.plan_order(
                "M5", ["CH1"],
                [Slot("K1", "single", (1,)), Slot("K2", "single", (1,))],
                window(),
            )

    def test_filling_must_hold_in_every_live_chronology(self):
        # 第二版冻结产品（新馆重签）仍含同一排年：交集稳定，可补。
        p2 = product("CH2", signer="VAULT1", note="新馆重签")
        self.svc.ingest_product(p2)
        order = self.svc.plan_order(
            "M6", ["CH1", "CH2"], [Slot("K1", "double", (3, 4))], window(),
            existing_filled_days=[0, 1, 2, 5], route_available={"central"},
        )
        self.assertTrue(order.feasible)


class CentralRoute(unittest.TestCase):
    def setUp(self):
        self.svc = MicrofilmOrderService()
        self.svc.ingest_product(product())
        self.slots = [Slot("K1", "double", (3, 4))]

    def test_loan_then_wait_for_late_calendar_is_feasible(self):
        order = self.svc.plan_order(
            "C1", ["CH1"], self.slots, window(),
            existing_filled_days=[0, 1, 2, 5], route_available={"central"},
        )
        self.assertEqual(order.route, "central")
        self.assertEqual(order.occupied_originals, ["I2"])
        # 中心取得原件后可等迟送校读，截止取最迟校读日。
        self.assertEqual(order.shoot_by_day, 12)

    def test_transit_eating_last_open_day_conflicts(self):
        bad = window(latest_proof_day=11, reader_blackout=(11, 14), ship_days=1)
        with self.assertRaises(UnfillableOrder) as ctx:
            self.svc.plan_order(
                "C2", ["CH1"], self.slots, bad,
                existing_filled_days=[0, 1, 2, 5], route_available={"central"},
            )
        self.assertEqual(ctx.exception.code, "no_feasible_route")
        self.assertTrue(any("reader blackout" in r for r in ctx.exception.detail.split("; ")))

    def test_case_hold_blocks_whole_loan_but_is_not_registered_arrived(self):
        hold = PreservationHold("H1", "case", "CASE-D", ("I4",), start=0, end=None)
        with self.assertRaises(UnfillableOrder) as ctx:
            self.svc.plan_order(
                "C3", ["CH1"], self.slots, window(),
                case_members={"CASE-D": ["I2", "I4"]}, holds=[hold],
                existing_filled_days=[0, 1, 2, 5], route_available={"central"},
            )
        self.assertIn("must not be registered as arrived", ctx.exception.detail)


class SurrogateRoute(unittest.TestCase):
    def setUp(self):
        self.svc = MicrofilmOrderService()
        self.svc.ingest_product(product())
        self.slots = [Slot("K1", "double", (3, 4))]

    def test_in_house_surrogate_keeps_original_open(self):
        order = self.svc.plan_order(
            "S1", ["CH1"], self.slots,
            window(reader_blackout=(0, 100)),  # 原件全程不可摸，副片不受影响
            surrogates=base_surrogates(),
            existing_filled_days=[0, 1, 2, 5],
            route_available={"surrogate"},
        )
        self.assertEqual(order.route, "surrogate")
        self.assertEqual(order.occupied_surrogates, ["F-I2"])
        self.assertTrue(order.in_house_evidence["I2"]["in_house_at_commit"])

    def test_numbered_sheet_targeting_another_volume_cannot_be_reused(self):
        wrong = [SurrogateStock("F-OTHER", "LOCAL", "I4", capacity=1)]
        with self.assertRaises(UnfillableOrder) as ctx:
            self.svc.plan_order(
                "S2", ["CH1"], self.slots, window(), surrogates=wrong,
                existing_filled_days=[0, 1, 2, 5],
                route_available={"surrogate"},
            )
        self.assertEqual(ctx.exception.code, "no_feasible_route")

    def test_missing_in_house_evidence_blocks_surrogate_route(self):
        stock = [SurrogateStock("F-I2", "LOCAL", "I2", capacity=1,
                                in_house_at_commit=False)]
        with self.assertRaises(UnfillableOrder) as ctx:
            self.svc.plan_order(
                "S3", ["CH1"], self.slots, window(), surrogates=stock,
                existing_filled_days=[0, 1, 2, 5],
                route_available={"surrogate"},
            )
        self.assertIn("in the holding library at shooting time", ctx.exception.detail)

    def test_consumed_sheet_has_no_remaining_capacity(self):
        stock = base_surrogates()
        first = self.svc.plan_order(
            "S4", ["CH1"], self.slots, window(), surrogates=stock,
            existing_filled_days=[0, 1, 2, 5], route_available={"surrogate"},
        )
        self.assertTrue(first.feasible)
        # 同一张有号片已实耗：再开一单无余量可用。
        with self.assertRaises(UnfillableOrder):
            self.svc.plan_order(
                "S5", ["CH1"], [Slot("K9", "double", (3, 4))], window(),
                surrogates=stock, existing_filled_days=[0, 1, 2, 5],
                route_available={"surrogate"},
            )


class ReSigningAndReopen(unittest.TestCase):
    def test_historical_signature_kept_late_reread_resigns(self):
        svc = MicrofilmOrderService()
        old = product("CH-OLD", note="重读未收敛")
        new = product("CH-NEW", signer="VAULT1", note="迟到重读收敛")
        new.supersedes = "CH-OLD"
        svc.ingest_product(old)
        svc.ingest_product(new)
        # 旧签原样保留它当时的依据：两版产品并存，历史产品不被新签倒改。
        kept = {p.product_id for p in svc.live_products()}
        self.assertEqual(kept, {"CH-OLD", "CH-NEW"})
        self.assertEqual(svc._products["CH-OLD"].note, "重读未收敛")
        self.assertEqual(svc._products["CH-NEW"].signer, "VAULT1")
        order = svc.plan_order(
            "R1", ["CH-NEW"], [Slot("K1", "double", (3, 4))], window(),
            existing_filled_days=[0, 1, 2, 5], route_available={"central"},
        )
        self.assertEqual(order.product_id, "CH-NEW")

    def test_replay_same_product_is_idempotent_conflict_rejected(self):
        svc = MicrofilmOrderService()
        p = product()
        self.assertTrue(svc.ingest_product(p)["replayed"] is False)
        self.assertTrue(svc.ingest_product(p)["replayed"] is True)
        tampered = product()
        tampered.note = "changed"
        with self.assertRaises(Exception):
            svc.ingest_product(tampered)


if __name__ == "__main__":
    unittest.main()
