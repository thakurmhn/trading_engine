"""Entry logic — bridges to root entry_logic.py.

Re-exports the scoring framework and quality gate functions.
"""
from __future__ import annotations

import importlib as _il

_el = _il.import_module("entry_logic")

check_entry_condition = _el.check_entry_condition
compute_daily_sentiment = _el.compute_daily_sentiment
detect_spread_noise = _el.detect_spread_noise
liquidity_zone = _el.liquidity_zone

__all__ = [
    "check_entry_condition", "compute_daily_sentiment",
    "detect_spread_noise", "liquidity_zone",
]
