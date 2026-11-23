"""Walk every synthetic recycling scenario using the public API.

Run: python3 examples/replay_recycling.py [scenario ...]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from address_delivery.scenarios import load_scenario, run  # noqa: E402

SCENARIOS = ROOT / "scenarios"
DEFAULT = [
    "canonical",
    "clean_proven",
    "certificate_withdrawn",
    "locked_paper1_glass2",
    "mix_downgrade",
    "zoning_change",
    "late_planning",
    "reissue_certificate",
]


def summarize(name: str, result: dict) -> dict:
    out = {
        "scenario": name,
        "bins": result["bins"],
        "explain": [
            {
                "bin_id": e["bin_id"],
                "weight": e["round_weight"],
                "grade": e["round_grade"],
                "last_round_residue": e["last_round_residue"],
                "rejected": [r["outcome"] for r in e["rejected_events"]],
            }
            for e in result["explain"]
        ],
    }
    for method in ("method_a", "method_b"):
        report = result.get(method)
        if not report:
            continue
        out[method] = {
            "feasible": report.get("feasible"),
            "assignment": report.get("assignment"),
            "completed_committed_stations": report.get("completed_committed_stations"),
            "weighing_slip": {
                k: report["weighing_slip"][k]
                for k in (
                    "qualified_paper_weight",
                    "qualified_glass_weight",
                    "mixed_weight",
                )
            }
            if "weighing_slip" in report
            else None,
            "reissue_needed": report.get("reissue_needed", []),
        }
    if "reservation_proof" in result:
        proof = result["reservation_proof"]
        out["reservation_proof"] = {
            "proved": proof["proved"],
            "early_completed": proof["early_completed_committed"],
            "early_best": proof["early_best_possible"],
            "ledger": proof["capacity_ledger"],
        }
    if result.get("probes"):
        out["probes"] = result["probes"]
    return out


def main(argv: list[str]) -> int:
    names = argv[1:] or DEFAULT
    for name in names:
        result = run(load_scenario(SCENARIOS / f"{name}.json"))
        print(f"\n== {name} ==")
        print(json.dumps(summarize(name, result), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
