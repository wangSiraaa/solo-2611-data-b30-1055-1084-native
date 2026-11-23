"""独立回放：对交班单允许的每个止位各演一遍，产出现场记录。

回放不偷看真相来决定动作——控制器始终拿全部可能止位做规划，只是把硬件
初始化成该条止位。每条记录包含：
- 该止位的证据见证（哪些失回执脉冲真的走了、逐拍时间线）；
- 控制器实际执行的逐拍动作与读头读数；
- 最终鼓面与目标核对、实际拍数与遮幕时限核对。
"""

from __future__ import annotations

from . import MAX_BEATS
from .controller import DrumHardware, SiteController, decide
from .core import Stop
from .reconcile import ReconcileResult, reconcile


def replay_stop(result: ReconcileResult, stop: Stop, target: Stop,
                curtain_beats: int) -> dict:
    witness = result.witness_for(stop)
    hardware = DrumHardware(stop)
    # 规划一律在馆方固定六拍内求策略，实际拍数再与本次遮幕时限核对，
    # 这样时限不足的情形也能留下“超时”的现场记录而不是直接报错。
    controller = SiteController(result.stops, target, MAX_BEATS)
    execution: list[dict] = []
    while not controller.finished():
        before = hardware.snapshot()
        record = controller.step(hardware)
        record = dict(record)
        record["digits_before"] = before
        record["digits_after"] = hardware.snapshot()
        execution.append(record)

    final = hardware.snapshot()
    return {
        "stop": list(stop.key()),
        "evidence": {
            "pulse_truth": {str(k): v for k, v in witness.pulse_truth.items()},
            "group_truth": dict(witness.group_truth),
            "timeline": witness.replay_log(),
        },
        "execution": execution,
        "beats_used": controller.beats_used,
        "final_digits": final,
        "reached_target": final == target.digits,
        "within_curtain": controller.beats_used <= curtain_beats,
    }


def replay_all(records: list, target: Stop | None, curtain_beats: int) -> dict:
    """核对 + 判定 + 每个止位独立回放，汇总成一份可归档的现场记录。"""
    result = reconcile(records)
    record = {
        "feasible": result.feasible,
        "target": None if target is None else list(target.key()),
        "curtain_beats": curtain_beats,
        "possible_stops": [list(s) for s in result.stop_keys()],
        "replays": [],
    }
    if result.feasible:
        record["decision"] = decide(result, target, curtain_beats).to_dict()
        for stop in result.stops:
            record["replays"].append(
                replay_stop(result, stop, target, curtain_beats))
    else:
        record["decision"] = None
        record["conflict"] = {
            "reason": result.conflict_reason,
            "records": [
                {"seq": r.seq, "type": type(r).__name__,
                 "drum": getattr(r, "drum", None)}
                for r in result.conflict
            ],
        }
    return record
