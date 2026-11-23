"""判图登记、断网申诉缩窄与时钟证据修订后的换图标记。

判图（RulingChart）一经签发即不可变，完整保留其原依据（所用联合解释的
横轴/时钟参数与支撑卡编号）。录像站断开后，申诉只能在“已受理范围”内
缩窄时段，不允许新增对象或扩大拍段。某台机修复时钟证据后重新校场，
登记处按原依据标出哪些旧判图需要换发，旧图不删除、只标记被谁取代。
登记处落本地 JSON，断网可用、重开可复算。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from .model import AdmissionResult, JointInterpretation, Verdict


@dataclass(frozen=True)
class InterpretationBasis:
    """一张判图对某个联合解释的原依据快照。"""

    interpretation_index: int
    axes: Mapping[str, Tuple[int, int]]       # camera -> (sign, translation)
    offsets: Mapping[str, int]                # camera -> offset
    flash_ids: Mapping[str, Tuple[str, ...]]  # camera -> 支撑闪灯编号
    benchmark_count: Mapping[str, int]        # camera -> 标杆条数


@dataclass(frozen=True)
class RulingChart:
    chart_id: str
    case_id: str
    mode: str                                 # full / minimal
    verdict: str
    accepted_beats: Tuple[int, ...]           # 已受理拍段
    authorized_labels: Tuple[str, ...]
    bases: Tuple[InterpretationBasis, ...]
    evidence_version: str                     # 校场证据版本
    parent_chart_id: Optional[str] = None
    superseded_by: Optional[str] = None
    supersede_reason: Optional[str] = None


@dataclass(frozen=True)
class AppealRecord:
    appeal_id: str
    chart_id: str
    requested_beats: Tuple[int, ...]
    granted: bool
    narrowed_chart_id: Optional[str]
    reason: str


def build_bases(admission: AdmissionResult,
                flash_ids: Mapping[str, Sequence[str]],
                benchmark_count: Mapping[str, int]
                ) -> Tuple[InterpretationBasis, ...]:
    """从校场结果固化每个联合解释的可审计依据。"""

    bases: List[InterpretationBasis] = []
    for idx, interp in enumerate(admission.interpretations):
        bases.append(InterpretationBasis(
            interpretation_index=idx,
            axes={cid: (axis.sign, axis.translation)
                  for cid, axis in interp.axes.items()},
            offsets=dict(interp.offsets),
            flash_ids={cid: tuple(flash_ids.get(cid, ()))
                       for cid in interp.offsets},
            benchmark_count=dict(benchmark_count)))
    return tuple(bases)


class ChartRegistry:
    """本地 JSON 判图登记处；所有操作不依赖录像站在线。"""

    def __init__(self, path: Optional[str] = None):
        self._path = path
        self._charts: Dict[str, RulingChart] = {}
        self._appeals: List[AppealRecord] = []
        self._seq = 0
        if path and os.path.exists(path):
            self._load()

    # ---- 签发 ----------------------------------------------------------
    def issue(self, case_id: str, mode: str, verdict: Verdict,
              accepted_beats: Sequence[int],
              authorized_labels: Sequence[str],
              bases: Sequence[InterpretationBasis],
              evidence_version: str,
              parent_chart_id: Optional[str] = None) -> RulingChart:
        self._seq += 1
        chart = RulingChart(
            chart_id=f"chart-{self._seq:04d}", case_id=case_id, mode=mode,
            verdict=verdict.value,
            accepted_beats=tuple(sorted(accepted_beats)),
            authorized_labels=tuple(sorted(authorized_labels)),
            bases=tuple(bases), evidence_version=evidence_version,
            parent_chart_id=parent_chart_id)
        self._charts[chart.chart_id] = chart
        self._save()
        return chart

    def get(self, chart_id: str) -> RulingChart:
        if chart_id not in self._charts:
            raise KeyError(f"判图 {chart_id} 不存在")
        return self._charts[chart_id]

    def charts_for_case(self, case_id: str) -> Tuple[RulingChart, ...]:
        return tuple(c for c in self._charts.values() if c.case_id == case_id)

    # ---- 断网申诉：只许在已受理范围内缩窄 ------------------------------
    def narrow_appeal(self, appeal_id: str, chart_id: str,
                      requested_beats: Sequence[int],
                      verdict_on_narrowed: Verdict) -> AppealRecord:
        chart = self.get(chart_id)
        requested = tuple(sorted(requested_beats))
        accepted = set(chart.accepted_beats)
        if not requested or not set(requested).issubset(accepted):
            record = AppealRecord(
                appeal_id=appeal_id, chart_id=chart_id,
                requested_beats=requested, granted=False,
                narrowed_chart_id=None,
                reason=("申诉拍段必须非空且完全落在已受理范围 "
                        f"{chart.accepted_beats} 内"))
            self._appeals.append(record)
            self._save()
            return record
        narrowed = self.issue(
            chart.case_id, chart.mode, verdict_on_narrowed, requested,
            chart.authorized_labels, chart.bases, chart.evidence_version,
            parent_chart_id=chart.chart_id)
        record = AppealRecord(
            appeal_id=appeal_id, chart_id=chart_id,
            requested_beats=requested, granted=True,
            narrowed_chart_id=narrowed.chart_id,
            reason="断网办理：仅缩窄时段，依据与授权不变")
        self._appeals.append(record)
        self._save()
        return record

    @property
    def appeals(self) -> Tuple[AppealRecord, ...]:
        return tuple(self._appeals)

    # ---- 时钟证据修订：标出需换发的旧判图 ------------------------------
    def mark_clock_revision(self, case_id: str, camera_id: str,
                            new_offsets: FrozenSet[int],
                            new_evidence_version: str,
                            reissue_verdict: Verdict,
                            reissue_bases: Sequence[InterpretationBasis]
                            ) -> Tuple[RulingChart, ...]:
        """机 camera_id 修了时钟后，标出依据旧偏移的判图并换发。

        旧判图保留原依据，仅写 superseded_by / supersede_reason；
        新判图挂新证据版本，返回被取代的旧判图清单。
        """

        superseded: List[RulingChart] = []
        for chart in self.charts_for_case(case_id):
            if chart.superseded_by is not None:
                continue
            used_offsets = {b.offsets.get(camera_id) for b in chart.bases}
            if used_offsets <= set(new_offsets):
                continue  # 新证据仍包含该图用过的全部偏移，旧依据仍成立
            replacement = self.issue(
                case_id, chart.mode, reissue_verdict, chart.accepted_beats,
                chart.authorized_labels, reissue_bases,
                new_evidence_version, parent_chart_id=chart.chart_id)
            updated = RulingChart(
                **{**chart.__dict__,
                   "superseded_by": replacement.chart_id,
                   "supersede_reason": (
                       f"机位 {camera_id} 时钟证据修订（新版本 "
                       f"{new_evidence_version}），旧偏移 "
                       f"{sorted(o for o in used_offsets if o is not None)} "
                       f"不在新兼容集合 {sorted(new_offsets)} 内")})
            self._charts[chart.chart_id] = updated
            superseded.append(updated)
        self._save()
        return tuple(superseded)

    # ---- 持久化 --------------------------------------------------------
    def _to_dict(self) -> dict:
        return {
            "seq": self._seq,
            "charts": [
                {"chart_id": c.chart_id, "case_id": c.case_id, "mode": c.mode,
                 "verdict": c.verdict, "accepted_beats": list(c.accepted_beats),
                 "authorized_labels": list(c.authorized_labels),
                 "bases": [
                     {"interpretation_index": b.interpretation_index,
                      "axes": {k: list(v) for k, v in b.axes.items()},
                      "offsets": dict(b.offsets),
                      "flash_ids": {k: list(v)
                                    for k, v in b.flash_ids.items()},
                      "benchmark_count": dict(b.benchmark_count)}
                     for b in c.bases],
                 "evidence_version": c.evidence_version,
                 "parent_chart_id": c.parent_chart_id,
                 "superseded_by": c.superseded_by,
                 "supersede_reason": c.supersede_reason}
                for c in self._charts.values()],
            "appeals": [a.__dict__ for a in self._appeals],
        }

    def _save(self) -> None:
        if not self._path:
            return
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)

    def _load(self) -> None:
        with open(self._path, encoding="utf-8") as f:
            data = json.load(f)
        self._seq = data["seq"]
        for raw in data["charts"]:
            bases = tuple(
                InterpretationBasis(
                    interpretation_index=b["interpretation_index"],
                    axes={k: tuple(v) for k, v in b["axes"].items()},
                    offsets=dict(b["offsets"]),
                    flash_ids={k: tuple(v)
                               for k, v in b["flash_ids"].items()},
                    benchmark_count=dict(b["benchmark_count"]))
                for b in raw["bases"])
            self._charts[raw["chart_id"]] = RulingChart(
                chart_id=raw["chart_id"], case_id=raw["case_id"],
                mode=raw["mode"], verdict=raw["verdict"],
                accepted_beats=tuple(raw["accepted_beats"]),
                authorized_labels=tuple(raw["authorized_labels"]),
                bases=bases, evidence_version=raw["evidence_version"],
                parent_chart_id=raw.get("parent_chart_id"),
                superseded_by=raw.get("superseded_by"),
                supersede_reason=raw.get("supersede_reason"))
        for raw in data["appeals"]:
            self._appeals.append(AppealRecord(**raw))
