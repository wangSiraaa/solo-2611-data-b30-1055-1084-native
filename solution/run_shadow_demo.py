"""Demonstration of the daytime shadow-puppet stage on materials/730.

Runs the two worked shows and prints, in order:
  * the per-beat light status with the blocking-board basis,
  * the rabbit -> crane handoff (both beats lit; board height 1),
  * the same handoff darkened on beat 1 by raising the board to height 2,
  * the handoff broken by removing the second master (rebind needs an empty
    beat),
  * a front/back depth swap where one face is genuinely covered,
  * the standby substitute (with a wing) and the flip-rig alternative (no wing,
    a lit slot to 甲's right), catching the forbidden passing face.

Usage: python3 solution/run_shadow_demo.py [--materials DIR]
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scene_730 as S  # noqa: E402
from shadow_stage import check_limits, light_report, run_plan  # noqa: E402


def _line(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def _print_result(r):
    status = "SIGNABLE" if r["ok"] else f"BLOCKED at beat {r['earliest_failure']}"
    print(f"  result: {status}")
    for f in r["frames"]:
        lit = ", ".join(f"{s}={v}" for s, v in f["lit"].items())
        vis = ", ".join(f"{s}->{v}" for s, v in f["visible"].items()) or "(none)"
        op = ", ".join(f"{s}:{v}" for s, v in f["operator"].items())
        print(f"    beat {f['beat']}: light[{lit}]  operators[{op}]  faces[{vis}]")
    for fail in r["failures"]:
        print(f"    ! beat {fail['beat']}: {fail['error']}")


def handoff_plan(second_master=True):
    if second_master:
        beats = {1: [["甲", "兔", "S"]], 2: [["乙", "鹤", "S"]]}
    else:
        beats = {1: [["甲", "兔", "S"]], 2: [["甲", "鹤", "S"]]}
    return {
        "beats": beats,
        "scenes": {
            1: {"required": [{"slot": "S", "face": "甲"}]},
            2: {"required": [{"slot": "S", "face": "甲"}]},
        },
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    default_materials = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "materials", "730",
    )
    ap.add_argument("--materials", default=default_materials)
    args = ap.parse_args(argv)
    M = args.materials

    _line("1. Light basis: board height 1, both beats lit (slope 1 then 2)")
    env = S.build_handoff_env(M, board_height=1)
    lr = light_report(env)
    for t, row in lr["beats"].items():
        s = row["slots"]["S"]
        reason = ""
        if s.get("blocker"):
            b = s["blocker"]
            reason = (f"  blocker {b['panel_id']} d={b['grid_distance']} "
                      f"h={b['height']} since {b['comparison']}")
        print(f"  beat {t}: slope {row['light']['slope']} -> S {s['status']}{reason}")
    _print_result(run_plan(env, handoff_plan()))

    _line("2. Raise the same west board to height 2 -> beat 1 dark, rabbit can't show")
    env2 = S.build_handoff_env(M, board_height=2)
    _print_result(run_plan(env2, handoff_plan()))

    _line("3. Remove the second master: master 甲 rebinds without an empty beat")
    env3 = S.build_handoff_env(M, with_second_master=False)
    _print_result(run_plan(env3, handoff_plan(second_master=False)))

    _line("4. Front/back depth swap: the back face is genuinely covered")
    env4 = S.build_occlusion_env(M)
    plan4 = {
        "beats": {
            1: [["甲", "兔", "F"], ["乙", "鹤", "K"]],
            2: [["甲", "兔", "K"], ["乙", "鹤", "F"]],
        },
        "scenes": {
            1: {"required": [{"slot": "F", "face": "甲"},
                             {"slot": "K", "face": "乙"}]},
            2: {"required": [{"slot": "F", "face": "乙"}],
                "hidden": [{"slot": "K", "face": "甲"}]},
        },
    }
    _print_result(run_plan(env4, plan4))

    _line("5a. Standby substitute: 替 pre-binds 乙P, lifts it in the wing")
    env5 = S.build_standby_env(M, wing_has_spot=True, rig_right_slot=True)
    plan5a = {
        "beats": {1: [["甲", "甲P", "J"]], 2: [["替", "乙P", "J"]]},
        "standby": {"乙P": {"machine": "替", "beat": 2, "hold": 2, "slot": "WING"}},
        "scenes": {
            1: {"required": [{"slot": "J", "face": "旦"}]},
            2: {"required": [{"slot": "J", "face": "丑"}]},
        },
        "notices": [{"role": "乙", "beat": 2, "available": ["甲", "替"]}],
    }
    _print_result(run_plan(env5, plan5a))

    _line("5b. No wing: flip rig occupies the lit slot to 甲's right; hide old face")
    env6 = S.build_standby_env(M, wing_has_spot=False, rig_right_slot=True)
    plan5b = {
        "beats": {1: [["甲", "甲P", "J"]],
                  2: [["甲", "乙P", "J", "rigflip", {}]]},
        "rigs": {"甲": {"puppets": ["甲P", "乙P"], "load_beats": 1,
                        "occupy_slot": "R", "since": 1, "passing_hidden": True}},
        "scenes": {
            1: {"required": [{"slot": "J", "face": "旦"}]},
            2: {"required": [{"slot": "J", "face": "丑"}],
                "forbid_passing": [{"face": "旦"}]},
        },
        "notices": [{"role": "乙", "beat": 2, "available": ["甲"]}],
    }
    _print_result(run_plan(env6, plan5b))

    _line("5c. Same flip rig, but the forbidden old face is exposed -> caught")
    plan5c = dict(plan5b)
    plan5c["rigs"] = {"甲": {"puppets": ["甲P", "乙P"], "load_beats": 1,
                            "occupy_slot": "R", "since": 1, "passing_hidden": False}}
    _print_result(run_plan(env6, plan5c))

    _line("Stage-size limits")
    for name, e in (("handoff", env), ("standby", env5)):
        probs = check_limits(e)
        print(f"  {name}: {'OK' if not probs else probs}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
