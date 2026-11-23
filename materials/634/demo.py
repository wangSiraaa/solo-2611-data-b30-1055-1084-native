"""端到端演示：待核 -> 确认 -> 驳回 -> 矛盾拒入 -> 换边镜像，附申诉与换图。

运行：python3 demo.py
"""

from __future__ import annotations

import os
import tempfile

from review.calibration import admit
from review.charts import ChartRegistry, build_bases
from review.disclosure import full_coordinate_view, minimal_ordering_view
from review.fixtures import (BALL, confirmed_case, conflicting_flash_case,
                             mirrored_case, pending_case, rejected_case)
from review.model import Side, Verdict, validate_case
from review.offside import review
from review.schedule import ScheduleBook, team_attack_sign

FIXTURES = (
    {"match_id": "m1", "round": 1, "home": "HOME-FC", "away": "AWAY-XI"},
    {"match_id": "m2", "round": 1, "home": "HOME-FC", "away": "AWAY-XI"},
)


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show_admission(name, case):
    result = admit(case.cameras)
    print(f"\n[{name}] 准入={result.admitted}，联合解释数="
          f"{len(result.interpretations)}")
    for f in result.failures:
        print(f"  失败原因 {f.code.value}: {f.detail}")
    for idx, interp in enumerate(result.interpretations):
        axes = ", ".join(
            f"{cid}: sign={a.sign}, t={a.translation}"
            for cid, a in sorted(interp.axes.items()))
        offsets = ", ".join(
            f"{cid}: off={off}"
            for cid, off in sorted(interp.offsets.items()))
        print(f"  解释#{idx} 横轴[{axes}] 时钟[{offsets}]")
    return result


def show_decision(name, case, result, side=Side.HOME):
    decision = review(case, result, side)
    print(f"[{name}] 裁决={decision.verdict.value}")
    for cell in decision.cells:
        if cell.decidable:
            print(f"  拍{cell.beat} 解释#{cell.interpretation_index} "
                  f"越线={cell.offside} | {cell.reason}")
        else:
            print(f"  拍{cell.beat} 解释#{cell.interpretation_index} "
                  f"不可裁决 | {cell.reason}")
    if decision.verdict == Verdict.PENDING and decision.retained_scenarios:
        print("  保留相反两场景：")
        for cell in decision.retained_scenarios:
            print(f"    -> {'越线' if cell.offside else '未越线'}: {cell.reason}")
    for miss in decision.missing_frames:
        print(f"  缺帧报告：{miss}")
    for conflict in decision.conflicts:
        print(f"  证据冲突：{conflict}")
    return decision


def main() -> None:
    book = ScheduleBook(tuple(FIXTURES))
    slot = book.lookup("m1")
    print(f"旧赛程 m1 真实主客：主队 {slot.home} / 客队 {slot.away}")

    # 场景一：机钟差零或一拍，结论待核
    section("场景一 无同步卡：正式5出球，机钟可差 0 或 -1 拍 -> 待核")
    pending = pending_case()
    validate_case(pending)
    res_pending = show_admission("待核", pending)
    dec_pending = show_decision("待核", pending, res_pending)
    assert dec_pending.verdict == Verdict.PENDING

    # 场景二：追加真实同步卡，正式5锁机内4 -> 确认
    section("场景二 追加真实同步卡（正式5 -> 机内4）-> 确认越线")
    confirmed = confirmed_case()
    res_conf = show_admission("确认", confirmed)
    dec_conf = show_decision("确认", confirmed, res_conf)
    assert dec_conf.verdict == Verdict.CONFIRMED
    assert len(res_conf.interpretations) == 1
    assert res_conf.interpretations[0].offsets == {"CAM-A": -1, "CAM-B": -1}

    # 场景三：同一校场产品，改审正式6（机内5），平齐 -> 驳回
    section("场景三 裁判改审正式6（对应机内5）：与 D2 平齐 -> 驳回")
    rejected = rejected_case()
    res_rej = show_admission("驳回", rejected)
    dec_rej = show_decision("驳回", rejected, res_rej)
    assert dec_rej.verdict == Verdict.REJECTED
    flat = [c for c in dec_rej.cells if c.decidable][0]
    assert flat.offside is False and flat.receiver_position == 60
    assert flat.second_last_defender == ("D2", 60)

    # 场景四：闪灯对不上 -> 不准入
    section("场景四 两台各自自洽但同一闪灯映射不同拍 -> 素材拒入")
    conflict = conflicting_flash_case()
    res_bad = show_admission("矛盾", conflict)
    dec_bad = show_decision("矛盾", conflict, res_bad)
    assert not res_bad.admitted
    assert dec_bad.verdict == Verdict.NOT_ADMITTED
    assert any(f.code.value == "flash_conflict" for f in res_bad.failures)

    # 场景五：换边镜像，反向横轴
    section("场景五 双方换端镜像：主队攻 LOW 端，机横轴 sign=-1/t=100")
    mirror = mirrored_case()
    validate_case(mirror)
    res_m = show_admission("镜像", mirror)
    sign = team_attack_sign(mirror.home_attack_end, Side.AWAY)
    print(f"客队（接应方）进攻符号={sign}（朝 100 端）")
    dec_m = show_decision("镜像", mirror, res_m, Side.AWAY)
    assert dec_m.verdict == Verdict.CONFIRMED
    assert all(a.sign == -1 and a.translation == 100
               for a in res_m.interpretations[0].axes.values())

    # 两种披露视图
    section("披露视图：完整坐标（可离线重排防线）与最小相对次序")
    interp = res_conf.interpretations[0]
    full = full_coordinate_view(
        confirmed, interp, (5,),
        frozenset({"R", BALL, "D1", "D2"}))
    for row in full.rows:
        print(f"  完整 拍{row.beat} {row.label} = {row.true_position} "
              f"来源={list(row.sources)}")
    minimal = minimal_ordering_view(
        confirmed, interp, (5,), Side.HOME,
        frozenset({"R", BALL}))  # 未授权 D1/D2，隔离其原始位置
    for st in minimal.statements:
        print(f"  最小 拍{st.beat} 次序={list(st.order)} "
              f"关系={list(st.relations)} 球边界={st.ball_boundary}")
    print(f"  最小视图被隔离对象={list(minimal.withheld_labels)}")
    assert "D1" in minimal.withheld_labels and "D2" in minimal.withheld_labels

    # 判图登记：签发、断网缩窄申诉、时钟修订换图
    section("判图登记：断网缩窄申诉 + 某台机修时钟后标出需换旧图")
    with tempfile.TemporaryDirectory() as tmp:
        registry = ChartRegistry(os.path.join(tmp, "charts.json"))
        bases = build_bases(
            res_pending,
            flash_ids={c.camera_id: tuple(f.flash_id for f in c.flashes)
                       for c in pending.cameras},
            benchmark_count={c.camera_id: len(c.benchmarks)
                             for c in pending.cameras})
        chart = registry.issue(
            "m1-h1", "full", dec_pending.verdict,
            sorted(pending.release_beats),
            ("R", BALL, "D1", "D2"), bases, "evidence-v1")
        print(f"  签发 {chart.chart_id}（版本 {chart.evidence_version}，"
              f"依据偏移 {[b.offsets for b in bases]}）")

        appeal = registry.narrow_appeal(
            "appeal-1", chart.chart_id, (5,), dec_pending.verdict)
        print(f"  断网申诉 granted={appeal.granted} -> "
              f"{appeal.narrowed_chart_id}：{appeal.reason}")
        bad = registry.narrow_appeal("appeal-2", chart.chart_id, (5, 7),
                                     dec_pending.verdict)
        print(f"  越界申诉 granted={bad.granted}：{bad.reason}")
        assert appeal.granted and not bad.granted

        # CAM-A 修时钟：新证据只兼容 offset=-1（同步卡到校后的版本）
        superseded = registry.mark_clock_revision(
            "m1-h1", "CAM-A", frozenset({-1}), "evidence-v2",
            dec_conf.verdict,
            build_bases(
                res_conf,
                flash_ids={c.camera_id: tuple(f.flash_id for f in c.flashes)
                           for c in confirmed.cameras},
                benchmark_count={c.camera_id: len(c.benchmarks)
                                 for c in confirmed.cameras}))
        for old in superseded:
            print(f"  旧判图 {old.chart_id} 需换发 -> {old.superseded_by}；"
                  f"原依据保留：{old.supersede_reason}")
        assert superseded and all(c.superseded_by for c in superseded)

        # 重开复算
        registry2 = ChartRegistry(os.path.join(tmp, "charts.json"))
        reopened = registry2.get(chart.chart_id)
        print(f"  重开登记处：{reopened.chart_id} 仍标记被 "
              f"{reopened.superseded_by} 取代，旧依据偏移未被改写")
        assert reopened.superseded_by is not None

    print("\n全部演示断言通过。")


if __name__ == "__main__":
    main()
