"""Market context — bridges to root context modules.

Re-exports regime detection, day classification, and sentiment analysis.
"""
from __future__ import annotations

import importlib as _il

# ── Root regime_context.py ───────────────────────────────────────
_rc = _il.import_module("regime_context")
RegimeContext = _rc.RegimeContext
compute_regime_context = _rc.compute_regime_context

# ── Root day_type.py ─────────────────────────────────────────────
_dt = _il.import_module("day_type")
DayTypeClassifier = _dt.DayTypeClassifier
DayTypeResult = _dt.DayTypeResult
apply_day_type_to_threshold = _dt.apply_day_type_to_threshold
apply_day_type_to_pm = _dt.apply_day_type_to_pm

# ── Root daily_sentiment.py ──────────────────────────────────────
_ds = _il.import_module("daily_sentiment")
get_daily_sentiment = _ds.get_daily_sentiment
get_daily_sentiment_from_candles = _ds.get_daily_sentiment_from_candles
compute_intraday_sentiment = _ds.compute_intraday_sentiment
classify_day_type = _ds.classify_day_type
get_opening_bias = _ds.get_opening_bias
get_open_position_bias = _ds.get_open_position_bias

__all__ = [
    "RegimeContext", "compute_regime_context",
    "DayTypeClassifier", "DayTypeResult",
    "apply_day_type_to_threshold", "apply_day_type_to_pm",
    "get_daily_sentiment", "get_daily_sentiment_from_candles",
    "compute_intraday_sentiment", "classify_day_type",
    "get_opening_bias", "get_open_position_bias",
]
