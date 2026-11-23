"""题目情形的可复跑走查（三卷以下本地合成小刊）。

情形索引
--------
A 基例：四月二 / 闰四月二 / 五月二，每日出刊；合刊“四月二、五月一”第一栏
  未写闰字，必占日 3、4；缺 3、4 + 一个 double 格，两条路由均可补成。
B 历书订正：闰四月与五月间另加一日小月，原两栏再无相邻解释 → 停止补拍。
C 空位移除：恢复原历书，double 格换成两个 single 格 → 仍有合法合刊却不能交。
D 同案保全：保全只压住同案另一件，整案不得登记成已到；副片须有在馆依据。
E 迟到重读（散文首段例子）：原件可等迟送校读；副片出馆后不可改、有号片不跨卷。
"""

from __future__ import annotations

import json
import os
from typing import Optional

from .collation import CollationEngine
from .microfilm import MicrofilmOrderService, PreservationHold, SurrogateStock
from .models import (
    Almanac,
    ChronologyProduct,
    Issue,
    Masthead,
    ServiceWindow,
    Slot,
    UnfillableOrder,
)

# 整数日轴上的时窗（合成数据，单位为“日”）。
LATEST_PROOF = 12
READER_BLACKOUT = (8, 11)   # 原馆查阅单不可中止窗口
SHIP_DAYS = 1


def base_almanac(revision: int = 1) -> Almanac:
    """小历：日0起；四月两日、闰四月两日、五月两日。"""
    return _almanac("AL-XIAOLI", revision, [
        ("四月", False, 2),
        ("四月", True, 2),
        ("五月", False, 2),
    ])


def _almanac(almanac_id: str, revision: int, months: list[tuple]) -> Almanac:
    from .models import Month
    return Almanac(
        almanac_id=almanac_id,
        revision=revision,
        epoch_day=0,
        months=tuple(Month(i, name, leap, length)
                     for i, (name, leap, length) in enumerate(months)),
    )


def amended_almanac() -> Almanac:
    """情形 B：闰四月与五月之间另有一日的小月。"""
    return _almanac("AL-XIAOLI", 2, [
        ("四月", False, 2),
        ("四月", True, 2),
        ("四月后小月", False, 1),
        ("五月", False, 2),
    ])


def base_issues() -> list[Issue]:
    """三卷以下的合成小刊：四件单期 + 一件同张双格合刊（共占六个应刊日）。"""
    return [
        Issue("I0", "S0", 0, (Masthead("四月", 1),)),
        Issue("I1", "S1", 1, (Masthead("四月", 2),)),
        Issue("I2S", "S2", 2, (Masthead("四月", 1, leap=None),)),  # 闰四月一，闰字漏写
        Issue("I2", "S3", 3, (Masthead("四月", 2, leap=None),
                              Masthead("五月", 1))),  # 合刊，首栏漏写闰字
        Issue("I4", "S4", 4, (Masthead("五月", 2),)),
    ]


def base_window() -> ServiceWindow:
    return ServiceWindow(latest_proof_day=LATEST_PROOF,
                         reader_blackout=READER_BLACKOUT,
                         ship_days=SHIP_DAYS)


def freeze(engine: CollationEngine, product_id: str, signer: str = "LOCAL",
           catalog_revision: int = 1, note: str = "") -> ChronologyProduct:
    valid = engine.solve()
    if len(valid) != 1:
        raise UnfillableOrder("ambiguous_chronology",
                              f"expected exactly one chronology, got {len(valid)}")
    placements = valid[0]
    return ChronologyProduct(
        product_id=product_id,
        almanac_id=engine.almanac.almanac_id,
        as_of_day=LATEST_PROOF,
        scheduled_days=list(engine.scheduled_days),
        placements=placements,
        signer=signer,
        catalog_revision=catalog_revision,
        almanac_revision=engine.almanac.revision,
        note=note,
    )


def base_surrogates() -> list[SurrogateStock]:
    return [SurrogateStock("F-I2", "LOCAL", "I2", capacity=1),
            SurrogateStock("F-I4", "LOCAL", "I4", capacity=1)]


# --------------------------------------------------------------------- 情形 A
def scenario_a() -> dict:
    almanac = base_almanac()
    scheduled = list(range(almanac.last_day() + 1))  # 每日出刊：0..5
    engine = CollationEngine(almanac, scheduled, base_issues())
    product = freeze(engine, "CH-A1", note="基例：合刊首栏闰字漏写，唯一收敛到闰四月二")
    designation = engine.verify_designation("I2", 3)

    svc = MicrofilmOrderService()
    svc.ingest_product(product)
    slots = [Slot("K1", "double", (3, 4))]
    order = svc.plan_order(
        "MO-A", ["CH-A1"], slots, base_window(),
        surrogates=base_surrogates(),
        existing_filled_days=[0, 1, 2, 5],
        preferred_route="surrogate",
    )
    central_only = MicrofilmOrderService()
    central_only.ingest_product(product)
    central_order = central_only.plan_order(
        "MO-A-C", ["CH-A1"], slots, base_window(),
        existing_filled_days=[0, 1, 2, 5],
        route_available={"central"},
    )
    return {
        "case": "A 基例：漏写闰字的合刊必占日3、4，缺3/4可补成",
        "day_table": [{"day": d.day, "label": d.label()} for d in almanac.days()],
        "valid_chronologies": [[p.as_dict() for p in engine.solve()[0]]],
        "designation": designation,
        "product": product.as_dict(),
        "chosen_work_order": order.as_dict(),
        "central_alternative": central_order.as_dict(),
        "ok": order.feasible and central_order.feasible
        and designation["holds_in_every_chronology"],
    }


# --------------------------------------------------------------------- 情形 B
def scenario_b() -> dict:
    almanac = amended_almanac()
    scheduled = list(range(almanac.last_day() + 1))
    engine = CollationEngine(almanac, scheduled, base_issues())
    valid = engine.solve()
    undatable = engine.undatable_sheets()
    svc = MicrofilmOrderService()
    order_error: Optional[dict] = None
    if valid:
        product = freeze(engine, "CH-B1", catalog_revision=2,
                         note="订正历书：闰四月与五月间加一日小月")
        svc.ingest_product(product)
        try:
            svc.plan_order("MO-B", ["CH-B1"], [Slot("K1", "double", (3, 4))],
                           base_window(), existing_filled_days=[0, 1, 2, 5, 6])
        except UnfillableOrder as exc:
            order_error = {"code": exc.code, "detail": exc.detail}
    return {
        "case": "B 历书订正：原两栏再无相邻解释，停止补拍",
        "day_table": [{"day": d.day, "label": d.label()} for d in almanac.days()],
        "valid_chronology_count": len(valid),
        "sheets_that_cannot_be_catalogued": undatable,
        "order_error": order_error or {"code": "no_chronology",
                                       "detail": "合刊在订正历书下无相邻两应刊日"},
        "ok": (not valid
               and any(s["issue_id"] == "I2"
                       and s["reason"] == "no_adjacent_scheduled_pair"
                       for s in undatable)),
    }


# --------------------------------------------------------------------- 情形 C
def scenario_c() -> dict:
    almanac = base_almanac()
    scheduled = list(range(almanac.last_day() + 1))
    engine = CollationEngine(almanac, scheduled, base_issues())
    product = freeze(engine, "CH-C1", note="恢复原历书；合法合刊仍在")
    svc = MicrofilmOrderService()
    svc.ingest_product(product)
    try:
        svc.plan_order(
            "MO-C", ["CH-C1"],
            [Slot("K1", "single", (3,)), Slot("K2", "single", (4,))],
            base_window(), existing_filled_days=[0, 1, 2, 5],
        )
        raise AssertionError("order must be rejected")
    except UnfillableOrder as exc:
        error = {"code": exc.code, "detail": exc.detail}
    return {
        "case": "C 空位换两个单格：仍有合法合刊，却不能交",
        "valid_chronology_count": len(engine.solve()),
        "combined_placement": next(p.as_dict() for p in product.placements
                                   if p.issue_id == "I2"),
        "order_error": error,
        "ok": error["code"] == "combined_requires_double",
    }


# --------------------------------------------------------------------- 情形 D
def scenario_d() -> dict:
    almanac = base_almanac()
    scheduled = list(range(almanac.last_day() + 1))
    # 两件同案（CASE-D）：合刊 I2 与单期 I4；保全只压住同案另一件。
    issues = [Issue(i.issue_id, i.sheet_id, i.run_no, i.mastheads) for i in base_issues()]
    engine = CollationEngine(almanac, scheduled, issues)
    product = freeze(engine, "CH-D1", note="同案保全情形")
    case_members = {"CASE-D": ["I2", "I4"]}
    hold = PreservationHold("HD1", "case", "CASE-D", ("I4",), start=0, end=None)

    # D-1 副片齐全且在馆有据：副片可交；原件整案借调被挡，且不得登记整案已到。
    svc = MicrofilmOrderService()
    svc.ingest_product(product)
    surrogate_order = svc.plan_order(
        "MO-D", ["CH-D1"], [Slot("K1", "double", (3, 4))], base_window(),
        case_members=case_members, holds=[hold],
        surrogates=base_surrogates(),
        existing_filled_days=[0, 1, 2, 5], preferred_route="surrogate",
    )
    central_attempt: dict
    try:
        svc.plan_order(
            "MO-D-C", ["CH-D1"], [Slot("K2", "double", (3, 4))], base_window(),
            case_members=case_members, holds=[hold],
            route_available={"central"},
            existing_filled_days=[0, 1, 2, 5],
        )
        central_attempt = {"accepted": True}
    except UnfillableOrder as exc:
        central_attempt = {"accepted": False, "code": exc.code, "detail": exc.detail}

    # D-2 副片无在馆依据 → 副片同样不能交。
    no_evidence = [SurrogateStock("F-I2", "LOCAL", "I2", capacity=1,
                                  in_house_at_commit=False)]
    svc2 = MicrofilmOrderService()
    svc2.ingest_product(product)
    try:
        svc2.plan_order(
            "MO-D2", ["CH-D1"], [Slot("K1", "double", (3, 4))], base_window(),
            case_members=case_members, holds=[hold], surrogates=no_evidence,
            existing_filled_days=[0, 1, 2, 5],
        )
        evidence_error = None
    except UnfillableOrder as exc:
        evidence_error = {"code": exc.code, "detail": exc.detail}

    return {
        "case": "D 同案保全：整案不登记已到；副片须出示在馆依据",
        "surrogate_work_order": surrogate_order.as_dict(),
        "central_attempt": central_attempt,
        "evidence_required": surrogate_order.in_house_evidence,
        "missing_evidence_error": evidence_error,
        "ok": (surrogate_order.route == "surrogate" and surrogate_order.feasible
               and central_attempt["accepted"] is False
               and evidence_error is not None
               and evidence_error["code"] == "no_feasible_route"),
    }


# --------------------------------------------------------------------- 情形 E
def scenario_e() -> dict:
    """迟到重读：原馆剩一张实副片及一天查阅开放。"""
    almanac = base_almanac()
    scheduled = list(range(almanac.last_day() + 1))
    engine = CollationEngine(almanac, scheduled, base_issues())
    early = freeze(engine, "CH-E0", note="重读未收敛前的旧签（保留当时依据）")
    late = freeze(engine, "CH-E1", note="馆方迟到重读后收敛的新签")
    late.product_id = "CH-E1"
    late.supersedes = "CH-E0"

    # 时窗：只剩一天查阅开放（日11），在途 1 天；取晚仍赶得及集中拍。
    tight = ServiceWindow(latest_proof_day=11, reader_blackout=(12, 14), ship_days=1)
    svc = MicrofilmOrderService()
    svc.ingest_product(early)
    svc.ingest_product(late)  # 旧产品不倒改，新签重开
    central = svc.plan_order(
        "MO-E", ["CH-E1"], [Slot("K1", "double", (3, 4))], tight,
        existing_filled_days=[0, 1, 2, 5], route_available={"central"},
    )
    # 运程吃掉最后开放日：在途抵达日 12 落入黑窗 → 原件路线冲突。
    bad_window = ServiceWindow(latest_proof_day=11, reader_blackout=(11, 14), ship_days=1)
    svc2 = MicrofilmOrderService()
    svc2.ingest_product(late)
    try:
        svc2.plan_order(
            "MO-E-BAD", ["CH-E1"], [Slot("K1", "double", (3, 4))], bad_window,
            existing_filled_days=[0, 1, 2, 5], route_available={"central"},
        )
        transit_error = None
    except UnfillableOrder as exc:
        transit_error = {"code": exc.code, "detail": exc.detail}

    # 副片路线：有号片只认指定期目；若出馆前那张拍的是另一卷，不能再用于本卷。
    wrong_sheet = [SurrogateStock("F-OTHER", "LOCAL", "I4", capacity=1)]
    svc3 = MicrofilmOrderService()
    svc3.ingest_product(late)
    try:
        svc3.plan_order(
            "MO-E-S", ["CH-E1"], [Slot("K1", "double", (3, 4))], tight,
            surrogates=wrong_sheet, existing_filled_days=[0, 1, 2, 5],
            route_available={"surrogate"},
        )
        sheet_error = None
    except UnfillableOrder as exc:
        sheet_error = {"code": exc.code, "detail": exc.detail}
    # 错拍不记作缺位已补：另用一张有余量、指定期目正确的片即可成单。
    spare = [SurrogateStock("F-I2-SPARE", "LOCAL", "I2", capacity=1)]
    svc4 = MicrofilmOrderService()
    svc4.ingest_product(late)
    spare_order = svc4.plan_order(
        "MO-E-SPARE", ["CH-E1"], [Slot("K1", "double", (3, 4))], tight,
        surrogates=spare, existing_filled_days=[0, 1, 2, 5],
        route_available={"surrogate"},
    )
    return {
        "case": "E 迟到重读：原件可取晚等待；有号副片不跨卷复用",
        "historical_product_kept": early.as_dict(),
        "new_product": late.as_dict(),
        "central_work_order": central.as_dict(),
        "transit_conflict": transit_error,
        "wrong_surrogate_error": sheet_error,
        "spare_sheet_work_order": spare_order.as_dict(),
        "ok": (central.feasible and transit_error is not None
               and sheet_error is not None
               and sheet_error["code"] == "no_feasible_route"
               and spare_order.feasible
               and spare_order.occupied_surrogates == ["F-I2-SPARE"]),
    }


def run_all(out_dir: Optional[str] = None) -> dict:
    report = {"A": scenario_a(), "B": scenario_b(), "C": scenario_c(),
              "D": scenario_d(), "E": scenario_e()}
    report["ok"] = all(v["ok"] for v in report.values())
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        for key, payload in report.items():
            if key == "ok":
                continue
            with open(os.path.join(out_dir, f"scenario_{key}.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.write("\n")
        with open(os.path.join(out_dir, "summary.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"ok": report["ok"],
                       "cases": {k: v["ok"] for k, v in report.items() if k != "ok"}},
                      fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
    return report
