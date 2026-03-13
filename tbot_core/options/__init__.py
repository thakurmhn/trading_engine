"""Options-specific modules — bridges to root option helpers.

Re-exports contract metadata, expiry management, and moneyness selection.
"""
from __future__ import annotations

import importlib as _il

# ── Root contract_metadata.py ────────────────────────────────────
try:
    _cm = _il.import_module("contract_metadata")
    ContractMetadataCache = _cm.ContractMetadataCache
    get_lot_size = _cm.get_lot_size
    get_expiry = _cm.get_expiry
    filter_intrinsic = _cm.filter_intrinsic
except (ImportError, AttributeError):
    ContractMetadataCache = None
    get_lot_size = None
    get_expiry = None
    filter_intrinsic = None

# ── Root expiry_manager.py ───────────────────────────────────────
try:
    _em = _il.import_module("expiry_manager")
    ExpiryManager = _em.ExpiryManager
    check_roll = _em.check_roll
    get_valid_contracts = _em.get_valid_contracts
except (ImportError, AttributeError):
    ExpiryManager = None
    check_roll = None
    get_valid_contracts = None

__all__ = [
    "ContractMetadataCache", "get_lot_size", "get_expiry", "filter_intrinsic",
    "ExpiryManager", "check_roll", "get_valid_contracts",
]
