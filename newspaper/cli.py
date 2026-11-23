"""newspaper 命令行：场景走查与工件落盘。

::

    python -m newspaper.cli scenarios --out artifacts/newspaper
"""

from __future__ import annotations

import argparse
import json
import sys

from .scenario import run_all


def _dump(obj) -> None:
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def cmd_scenarios(args) -> None:
    report = run_all(args.out)
    _dump({"ok": report["ok"],
           "cases": {k: v["ok"] for k, v in report.items() if k != "ok"}})
    if not report["ok"]:
        raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="newspaper")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("scenarios", help="run A..E worked cases and write artifacts")
    sp.add_argument("--out", default="artifacts/newspaper")
    sp.set_defaults(func=cmd_scenarios)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
