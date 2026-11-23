"""Archive adjacency: rules/event interpretation and whole-case handoff."""

from .models import (  # noqa: F401
    ActiveHold,
    BlockReason,
    ConflictError,
    DocQualification,
    Document,
    Event,
    Hold,
    InvalidInterval,
    QualificationProduct,
    RejectError,
    Rule,
    RuleReason,
    UnknownReference,
)
from .rules_service import RuleEventStore  # noqa: F401
from .handoff_service import HandoffRepo  # noqa: F401
from .clepsydra import (  # noqa: F401
    STOP,
    Beat,
    Entry,
    ShowPlan,
    Solution,
    Witness,
    determined_beats,
    explain,
    possible_ring_patterns,
    ring_pattern,
    run_clock,
    run_stand,
)
from .clepsydra_case import LogCopy, Revision, WaterClockCase  # noqa: F401

__all__ = [
    "RuleEventStore",
    "HandoffRepo",
    "QualificationProduct",
    "DocQualification",
    "RejectError",
    "ConflictError",
    "InvalidInterval",
    "UnknownReference",
    "WaterClockCase",
    "LogCopy",
    "Revision",
    "Witness",
    "Entry",
    "Solution",
    "ShowPlan",
    "Beat",
    "STOP",
    "run_clock",
    "run_stand",
    "ring_pattern",
    "explain",
    "possible_ring_patterns",
    "determined_beats",
]
