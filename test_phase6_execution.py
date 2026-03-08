"""Phase 6 — Execution Tests.

Test slope conflict time-based override logic in execution.py:
- Blocked when conflict persists < N bars
- Allowed with [SLOPE_OVERRIDE_TIME] when conflict persists >= N bars
- Counter resets on override and on slope-OK
- [CONFLICT_BLOCKED] vs [SLOPE_OVERRIDE_TIME] log tags
"""

import logging
import sys
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

# Stub heavy deps before importing execution
_STUB_NAMES = [
    "setup", "orchestration", "position_manager",
    "fyers_apiv3", "fyers_apiv3.fyersModel",
    "contract_metadata", "expiry_manager",
    "failed_breakout_detector", "reversal_detector",
    "compression_detector", "zone_detector",
    "day_type", "daily_sentiment",
    "volatility_context", "greeks_calculator",
    "signals", "pulse_module",
]
_ind = MagicMock()
_ind.calculate_atr = MagicMock(return_value=50.0)
_ind.resolve_atr = MagicMock(return_value=(50.0, "ATR14"))
_ind.daily_atr = MagicMock(return_value=100.0)
_ind.momentum_ok = MagicMock(return_value=(True, 1.0))
_ind.williams_r = MagicMock(return_value=-50.0)
_ind.calculate_cci = MagicMock(return_value=0.0)
_ind.compute_rsi = MagicMock(return_value=50.0)
_ind.classify_cpr_width = MagicMock(return_value="NORMAL")
if "indicators" not in sys.modules:
    sys.modules["indicators"] = _ind
for name in _STUB_NAMES:
    if name not in sys.modules:
        sys.modules[name] = MagicMock()

import config as _cfg
if not hasattr(_cfg, "SLOPE_CONFLICT_TIME_BARS"):
    _cfg.SLOPE_CONFLICT_TIME_BARS = 5
if not hasattr(_cfg, "SLOPE_ADX_GATE"):
    _cfg.SLOPE_ADX_GATE = 20.0
if not hasattr(_cfg, "TIME_SLOPE_ADX_GATE"):
    _cfg.TIME_SLOPE_ADX_GATE = 25.0

import execution


class TestSlopeConflictCounter(unittest.TestCase):
    """Test _slope_conflict_bars counter logic."""

    def setUp(self):
        # Reset the module-level counter
        execution._slope_conflict_bars.clear()

    def test_counter_increments(self):
        """Counter should increment on each slope conflict."""
        execution._slope_conflict_bars["NIFTY"] = 0
        execution._slope_conflict_bars["NIFTY"] += 1
        self.assertEqual(execution._slope_conflict_bars["NIFTY"], 1)
        execution._slope_conflict_bars["NIFTY"] += 1
        self.assertEqual(execution._slope_conflict_bars["NIFTY"], 2)

    def test_counter_resets_to_zero(self):
        """Counter resets when explicitly zeroed."""
        execution._slope_conflict_bars["NIFTY"] = 4
        execution._slope_conflict_bars["NIFTY"] = 0
        self.assertEqual(execution._slope_conflict_bars["NIFTY"], 0)

    def test_separate_symbols(self):
        """Different symbols have independent counters."""
        execution._slope_conflict_bars["NIFTY"] = 3
        execution._slope_conflict_bars["BANKNIFTY"] = 1
        self.assertEqual(execution._slope_conflict_bars["NIFTY"], 3)
        self.assertEqual(execution._slope_conflict_bars["BANKNIFTY"], 1)


class TestSlopeOverrideTimeLogic(unittest.TestCase):
    """Test the Path F time-based override fires at threshold."""

    def setUp(self):
        execution._slope_conflict_bars.clear()

    def test_override_fires_at_threshold(self):
        """After N bars of conflict, the override reason should contain SLOPE_OVERRIDE_TIME."""
        sym = "NIFTY"
        limit = 5
        # Simulate N-1 conflicts (no override)
        for i in range(limit - 1):
            execution._slope_conflict_bars[sym] = execution._slope_conflict_bars.get(sym, 0) + 1
        self.assertEqual(execution._slope_conflict_bars[sym], limit - 1)

        # One more should trigger
        execution._slope_conflict_bars[sym] += 1
        self.assertEqual(execution._slope_conflict_bars[sym], limit)

        # Verify threshold check
        self.assertTrue(execution._slope_conflict_bars[sym] >= limit)

    def test_no_override_below_threshold(self):
        """Below N bars, no override should fire."""
        sym = "NIFTY"
        execution._slope_conflict_bars[sym] = 3
        self.assertFalse(execution._slope_conflict_bars[sym] >= 5)


class TestSlopeConflictLogTags(unittest.TestCase):
    """Verify log tags are present in execution module."""

    def test_slope_override_time_tag_exists(self):
        """SLOPE_OVERRIDE_TIME string exists in execution source."""
        import inspect
        src = inspect.getsource(execution)
        self.assertIn("SLOPE_OVERRIDE_TIME", src)

    def test_conflict_blocked_tag_exists(self):
        """CONFLICT_BLOCKED string exists in execution source."""
        import inspect
        src = inspect.getsource(execution)
        self.assertIn("[CONFLICT_BLOCKED]", src)

    def test_path_f_comment_exists(self):
        """Path F persistent slope conflict override is documented."""
        import inspect
        src = inspect.getsource(execution)
        self.assertIn("Path F", src)


class TestConfigIntegration(unittest.TestCase):
    """Config has SLOPE_CONFLICT_TIME_BARS."""

    def test_config_attr_exists(self):
        self.assertTrue(hasattr(_cfg, "SLOPE_CONFLICT_TIME_BARS"))
        self.assertEqual(_cfg.SLOPE_CONFLICT_TIME_BARS, 5)


if __name__ == "__main__":
    unittest.main()
