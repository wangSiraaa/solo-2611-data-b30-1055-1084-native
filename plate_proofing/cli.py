"""Command-line entry point for the plate proofing work order.

Mirrors :mod:`archive_handoff.cli` conventions.

Examples::

    python3 -m plate_proofing.cli --out artifacts/workorder workorder
    python3 -m plate_proofing.cli --coord materials/751/coordinate_table.json grid
"""

from __future__ import annotations

import argparse
import json
import sys

from .engine import CoordinateTable
from .scenarios import load_coordinate_table, run_all


def _dump(obj) -> None:
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def cmd_workorder(args) -> None:
    result = run_all(materials_dir=args.materials, out_dir=args.out)
    _dump(result)
    if not result.get("ok"):
        raise SystemExit(1)


def cmd_grid(args) -> None:
    coord = load_coordinate_table(args.coord)
    _dump({
        "plate": coord.plate,
        "points": {p: dict(v) for p, v in coord.points.items()},
        "coords": coord.coords(),
        "locked_slots": sorted(coord.locked_slots),
        "locked_pins": sorted(coord.locked_pins),
    })


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="plate_proofing")
    p.add_argument("--materials", default="materials/751")
    p.add_argument("--out", default="artifacts/workorder")
    p.add_argument("--coord", default="materials/751/coordinate_table.json")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("workorder")
    sp.set_defaults(func=cmd_workorder)

    sp = sub.add_parser("grid")
    sp.set_defaults(func=cmd_grid)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
