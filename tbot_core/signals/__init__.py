"""Signal detection — bridges to root signal modules.

Re-exports all signal-related functions from the monolith for use by
strategies built on tbot_core.
"""
from __future__ import annotations

import importlib as _il

# ── Root signals.py ──────────────────────────────────────────────
_sig = _il.import_module("signals")

detect_signal = _sig.detect_signal
TrendContinuationState = _sig.TrendContinuationState
compute_tilt_state = _sig.compute_tilt_state
calculate_vwap = _sig.calculate_vwap
get_opening_range = _sig.get_opening_range
range_is_ok = _sig.range_is_ok
candle_strength = _sig.candle_strength
classify_volatility = _sig.classify_volatility
dynamic_targets = _sig.dynamic_targets
signal_confidence = _sig.signal_confidence

# ── Root reversal_detector.py ────────────────────────────────────
_rev = _il.import_module("reversal_detector")
detect_reversal = _rev.detect_reversal

# ── Root compression_detector.py ─────────────────────────────────
_comp = _il.import_module("compression_detector")
CompressionState = _comp.CompressionState
detect_compression = _comp.detect_compression
detect_expansion = _comp.detect_expansion

# ── Root failed_breakout_detector.py ─────────────────────────────
_fb = _il.import_module("failed_breakout_detector")
detect_failed_breakout = _fb.detect_failed_breakout

# ── Root zone_detector.py ────────────────────────────────────────
_zd = _il.import_module("zone_detector")
detect_zones = _zd.detect_zones
detect_zone_revisit = _zd.detect_zone_revisit
detect_zone_absorption = _zd.detect_zone_absorption
update_zone_activity = _zd.update_zone_activity
Zone = _zd.Zone

# ── Root pulse_module.py ─────────────────────────────────────────
_pm = _il.import_module("pulse_module")
PulseModule = _pm.PulseModule
PulseMetrics = _pm.PulseMetrics
get_pulse_module = _pm.get_pulse_module
reset_pulse_module = _pm.reset_pulse_module

__all__ = [
    "detect_signal", "TrendContinuationState", "compute_tilt_state",
    "calculate_vwap", "get_opening_range", "range_is_ok", "candle_strength",
    "classify_volatility", "dynamic_targets", "signal_confidence",
    "detect_reversal", "CompressionState", "detect_compression",
    "detect_expansion", "detect_failed_breakout",
    "detect_zones", "detect_zone_revisit", "detect_zone_absorption",
    "update_zone_activity", "Zone",
    "PulseModule", "PulseMetrics", "get_pulse_module", "reset_pulse_module",
]
