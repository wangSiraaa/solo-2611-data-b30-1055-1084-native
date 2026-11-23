"""Address catalog and delivery jobs (synthetic addresses only).

Two independent stores share a SQLite backend:

* :mod:`address_delivery.catalog` - cities/districts/streets, scoped names and
  address resolution through the complete parent chain, plus coverages.
* :mod:`address_delivery.delivery` - parcel delivery attempts and frozen jobs.

:class:`address_delivery.service.AddressService` wires them together for the
combined workflow.
"""
from __future__ import annotations

from .catalog import Catalog, CoverageHit, Resolution, StreetCandidate
from .delivery import AttemptResult, BatchResult, DeliveryStore
from .errors import CatalogError, DeliveryError, InputFormatError
from .input import (
    address_input_document,
    parse_address_input_document,
    parse_csv_text,
    read_address_input_json,
    read_csv_addresses,
)
from .models import AddressInput, validate_address_input
from .normalization import make_identity, normalize_text
from .recycling import LendError, Lending, RecyclingLedger, replay_events
from .service import AddressService
from .truck import (
    Bin,
    Compartment,
    Truck,
    bins_from_manifest,
    make_bin,
    plan_late,
    plan_with_partition,
    prove_early_capacity,
)

__all__ = [
    "AddressInput",
    "AddressService",
    "AttemptResult",
    "BatchResult",
    "Bin",
    "Catalog",
    "CatalogError",
    "Compartment",
    "CoverageHit",
    "DeliveryError",
    "DeliveryStore",
    "InputFormatError",
    "LendError",
    "Lending",
    "RecyclingLedger",
    "Resolution",
    "StreetCandidate",
    "Truck",
    "address_input_document",
    "bins_from_manifest",
    "make_bin",
    "make_identity",
    "normalize_text",
    "parse_address_input_document",
    "parse_csv_text",
    "plan_late",
    "plan_with_partition",
    "prove_early_capacity",
    "read_address_input_json",
    "read_csv_addresses",
    "replay_events",
    "validate_address_input",
]

__version__ = "1.1.0"
