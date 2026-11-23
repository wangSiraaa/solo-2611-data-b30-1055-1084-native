"""Concrete daytime shadow-puppet scene wired to the materials/730 route.

The stage slots are physical: each is bound to a real rebuilt locator plaque
(an ``asset`` on route ``L``) and certified against the inspected coverage.
The stage *grid* (``grid_x``) is the puppet-layout axis and is deliberately
kept separate from the route physical axis.

Two shows are modelled, matching the task's worked examples:

``show_handoff``
    Two beats, one visible face each: the rabbit (兔) shows screen cell 甲,
    then the crane (鹤) takes the same cell.  Both puppets can project 甲 at
    the same slot.  Master 甲 starts bound to the rabbit; master 乙 starts
    bound to the crane and waits in the wing (instant advance/withdraw
    between beats).  One height-1 board sits one grid cell west of the slot;
    light comes from the west with slope 1 then 2, so both beats are lit.

``show_standby``
    Master 甲 owns slot 甲位; the master bound to role 乙 is absent and the
    real crane puppet must immediately take the same face cell.  Either a
    substitute pre-binds the crane and waits in the wing, or — if the wing
    has no waiting spot but a lit slot to the right of 甲 is free for a whole
    beat — a flip rig produces the change inside one beat, and the forbidden
    old face must be caught along the flip seam.
"""

from __future__ import annotations

import json
import os

from chainage_catalog import CatalogRegistry, RouteCatalog
from inspection_library import Asset, InspectionLibrary

from shadow_stage import (
    certify_slots,
)


# ---------------------------------------------------------------------------
# Route-bound locator plaques
# ---------------------------------------------------------------------------


def _read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def build_route_and_coverage(materials_dir: str):
    """Load r1 and publish a whole-route inspection report (physical [0,1000)).

    The report's merged coverage is what the slot certification checks against:
    a locator whose physical position falls outside it is not certified.
    """
    route = _read_json(os.path.join(materials_dir, "route.json"))
    assets_data = _read_json(os.path.join(materials_dir, "assets.json"))
    assets = [Asset.from_dict(a) for a in assets_data["assets"]]
    # Add extra locator plaques used by the stage (stable physical positions).
    for i, pos in enumerate([100, 300, 700, 900]):
        assets.append(Asset(f"L{i}", "L", pos))

    catalog = RouteCatalog.from_dict(route)
    reg = CatalogRegistry()
    reg.add(catalog)
    lib = InspectionLibrary(reg, assets)
    # Whole-route coverage: one span per section, upper bound at end label.
    job = {
        "job_id": "STAGE",
        "route_id": "L",
        "spans": [
            {"section_id": "P1", "label_start": 0, "label_end": 400},
            {"section_id": "P2", "label_start": 300, "label_end": 600},
            {"section_id": "P3", "label_start": 650, "label_end": 950},
        ],
    }
    report = lib.resolve_and_inspect(job, "STAGE")
    return reg, assets, report, catalog


# ---------------------------------------------------------------------------
# Puppets
# ---------------------------------------------------------------------------


def rabbit_puppet(actor="jia"):
    return {
        "puppet_id": "兔",
        "actor": "兔",
        "home_machine": actor,
        "face_label": "甲",
        "face": [0, 0],
        "shape": ["#.", "##"],   # cells (0,0),(1,0),(1,1)
    }


def crane_puppet(actor="yi"):
    return {
        "puppet_id": "鹤",
        "actor": "鹤",
        "home_machine": actor,
        "face_label": "甲",       # also projects screen cell 甲
        "face": [0, 1],
        "shape": ["##"],          # cells (0,0),(0,1)
    }


# ---------------------------------------------------------------------------
# Handoff show (rabbit -> crane at the same screen cell)
# ---------------------------------------------------------------------------


def build_handoff_env(materials_dir: str, board_height=1, with_second_master=True,
                      slot_survey="walked") -> dict:
    reg, assets, report, catalog = build_route_and_coverage(materials_dir)
    slots = {
        "S": {
            "slot_id": "S",
            "grid_x": 0,
            "position": "350",
            "locator_asset": "A",     # asset A @ 350
            "survey_status": slot_survey,
        },
    }
    # One shade board one grid cell west, height configurable, really measured.
    panels = [
        {
            "panel_id": "B west",
            "grid_x": -1,
            "height": board_height,
            "position": "300",
            "surveyed": True,
        }
    ]
    machines = {
        "甲": {"machine_id": "甲", "learned": ["兔", "鹤"], "start_holding": {"兔": 1}},
        "乙": {"machine_id": "乙", "learned": ["鹤"], "start_holding": {"鹤": 2}},
    }
    if not with_second_master:
        machines.pop("乙")
    env = {
        "env_id": "handoff",
        "route_id": "L",
        "slots": slots,
        "panels": panels,
        "machines": machines,
        "puppets": {"兔": rabbit_puppet("甲"),
                    "鹤": crane_puppet("甲" if not with_second_master else "乙")},
        "screen": {"oc1": {"slot": "S", "role": "front"}},
        "stage": {
            "adjacent_move": [{"distance": 1, "beats": 0}],  # wing <-> slot instant
            "move_beats_per_cell": 1,
            "turn_beats": {"*": 0},
        },
        "beats": [
            {"beat": 1, "light": {"dir": "W", "slope": 1}},
            {"beat": 2, "light": {"dir": "W", "slope": 2}},
        ],
        "certification": certify_slots(env_slots_only(slots), assets, report.merged, catalog),
        "coverage": [str(iv) for iv in report.merged],
    }
    return env


def env_slots_only(slots):
    return {"slots": slots}


# ---------------------------------------------------------------------------
# Standby / flip-rig show
# ---------------------------------------------------------------------------


def build_standby_env(materials_dir: str, wing_has_spot=True,
                      rig_right_slot=True) -> dict:
    reg, assets, report, catalog = build_route_and_coverage(materials_dir)
    slots = {
        "J": {   # 甲位
            "slot_id": "J",
            "grid_x": 0,
            "position": "470",
            "locator_asset": "B",     # B @ 470
            "survey_status": "walked",
        },
        "R": {   # 甲右方: a lit, occupiable slot (only when rig_right_slot)
            "slot_id": "R",
            "grid_x": 1,
            "position": "440",
            "locator_asset": "C",     # C @ 440
            "survey_status": "walked",
        },
        "WING": {
            "slot_id": "WING",
            "grid_x": -1,
            "position": "100",
            "locator_asset": "L0",
            "survey_status": "walked",
        },
    }
    if not rig_right_slot:
        slots.pop("R")
    # No boards on the up-light side -> every declared slot is lit.
    panels = []
    machines = {
        "甲": {"machine_id": "甲", "learned": ["甲", "乙"], "start_holding": {"甲P": 1}},
        "乙": {"machine_id": "乙", "learned": ["乙"], "start_holding": {}},
        "替": {"machine_id": "替", "learned": ["乙"], "start_holding": {}},
    }
    puppets = {
        "甲P": {
            "puppet_id": "甲P", "actor": "甲", "home_machine": "甲",
            "face_label": "旦", "face": [0, 0], "shape": ["#"],
        },
        "乙P": {
            "puppet_id": "乙P", "actor": "乙", "home_machine": "乙",
            "face_label": "丑", "face": [0, 0], "shape": ["#"],
        },
    }
    env = {
        "env_id": "standby",
        "route_id": "L",
        "slots": slots,
        "panels": panels,
        "machines": machines,
        "puppets": puppets,
        "screen": {"oc1": {"slot": "J", "role": "front"}},
        "stage": {
            "adjacent_move": [{"distance": 1, "beats": 0}],
            "move_beats_per_cell": 1,
            "turn_beats": {"*": 0},
        },
        "beats": [
            {"beat": 1, "light": {"dir": "W", "slope": 1}},
            {"beat": 2, "light": {"dir": "W", "slope": 1}},
        ],
        "certification": certify_slots({"slots": slots}, assets, report.merged, catalog),
        "coverage": [str(iv) for iv in report.merged],
        "wing_has_spot": wing_has_spot,
        "rig_right_slot": rig_right_slot,
    }
    return env


# ---------------------------------------------------------------------------
# Front/back occlusion show
# ---------------------------------------------------------------------------


def build_occlusion_env(materials_dir: str) -> dict:
    reg, assets, report, catalog = build_route_and_coverage(materials_dir)
    slots = {
        "F": {"slot_id": "F", "grid_x": 0, "position": "350",
              "locator_asset": "A", "survey_status": "walked"},
        "K": {"slot_id": "K", "grid_x": 0, "position": "470",
              "locator_asset": "B", "survey_status": "walked"},
    }
    machines = {
        "甲": {"machine_id": "甲", "learned": ["兔", "鹤"], "start_holding": {"兔": 1}},
        "乙": {"machine_id": "乙", "learned": ["兔", "鹤"], "start_holding": {"鹤": 1}},
    }
    rabbit = rabbit_puppet("甲")
    rabbit["face_label"] = "甲"
    crane = crane_puppet("乙")
    crane["face_label"] = "乙"
    env = {
        "env_id": "occlusion",
        "route_id": "L",
        "slots": slots,
        "panels": [],
        "machines": machines,
        "puppets": {"兔": rabbit, "鹤": crane},
        "screen": {
            "oc1": {"slot": "F", "role": "front"},
            "oc2": {"slot": "K", "role": "back"},
        },
        "stage": {
            "adjacent_move": [{"distance": 1, "beats": 0}],
            "move_beats_per_cell": 1,
            "turn_beats": {"*": 0},
        },
        "beats": [
            {"beat": 1, "light": {"dir": "W", "slope": 1}},
            {"beat": 2, "light": {"dir": "W", "slope": 1}},
        ],
        "certification": certify_slots({"slots": slots}, assets, report.merged, catalog),
        "coverage": [str(iv) for iv in report.merged],
    }
    return env
