"""Phase 6 — Entry Logic Tests.

Test _check_bar_close_alignment and detect_spread_noise in entry_logic.py.
Covers CALL/PUT alignment, 3m/15m timeframes, misalignment, neutral cases,
and attribution tag generation.
"""

import sys
from unittest.mock import MagicMock

# Stub heavy deps before importing entry_logic
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

_STUB_NAMES = [
    "setup", "orchestration", "position_manager",
    "fyers_apiv3", "fyers_apiv3.fyersModel",
    "contract_metadata", "expiry_manager",
    "failed_breakout_detector", "reversal_detector",
    "compression_detector",
    "day_type", "daily_sentiment",
    "volatility_context", "greeks_calculator",
    "signals",
]
for name in _STUB_NAMES:
    if name not in sys.modules:
        sys.modules[name] = MagicMock()

import config as _cfg
if not hasattr(_cfg, "SLOPE_CONFLICT_TIME_BARS"):
    _cfg.SLOPE_CONFLICT_TIME_BARS = 5

sys.modules.pop("entry_logic", None)

import unittest
import pandas as pd
import entry_logic


class TestBarCloseAlignmentCALL(unittest.TestCase):
    """CALL entry bar-close alignment."""

    func = staticmethod(entry_logic._check_bar_close_alignment)

    def test_aligned_3m(self):
        """Close > prev, near EMA, bullish bias → ALIGNED, 3m."""
        candle = {"close": 22010, "ema9": 22000, "ema13": 21990}
        indicators = {
            "close_prev_3m": 21995, "atr": 50,
            "st_bias_3m": "BULLISH", "candle_15m": None,
        }
        status, tf = self.func(candle, indicators, "CALL")
        self.assertEqual(status, "ALIGNED")
        self.assertEqual(tf, "3m")

    def test_aligned_15m_priority(self):
        """15m alignment takes priority over 3m."""
        candle = {"close": 22010, "ema9": 22000, "ema13": 21990}
        candle_15m = pd.Series({
            "close": 22020, "ema9": 22010, "ema13": 22000,
            "supertrend_bias": "BULLISH",
        })
        indicators = {
            "close_prev_3m": 21995, "close_prev_15m": 22005,
            "atr": 50, "st_bias_3m": "BULLISH", "candle_15m": candle_15m,
        }
        status, tf = self.func(candle, indicators, "CALL")
        self.assertEqual(status, "ALIGNED")
        self.assertEqual(tf, "15m")

    def test_misaligned_close_below_prev(self):
        """CALL but close < prev → MISALIGNED."""
        candle = {"close": 21990, "ema9": 22000, "ema13": 22010}
        indicators = {
            "close_prev_3m": 22005, "atr": 50,
            "st_bias_3m": "BULLISH", "candle_15m": None,
        }
        status, tf = self.func(candle, indicators, "CALL")
        self.assertEqual(status, "MISALIGNED")
        self.assertIsNone(tf)


class TestBarCloseAlignmentPUT(unittest.TestCase):
    """PUT entry bar-close alignment."""

    func = staticmethod(entry_logic._check_bar_close_alignment)

    def test_aligned_3m(self):
        """Close < prev, near EMA, bearish bias → ALIGNED, 3m."""
        candle = {"close": 21990, "ema9": 22000, "ema13": 22010}
        indicators = {
            "close_prev_3m": 22005, "atr": 50,
            "st_bias_3m": "BEARISH", "candle_15m": None,
        }
        status, tf = self.func(candle, indicators, "PUT")
        self.assertEqual(status, "ALIGNED")
        self.assertEqual(tf, "3m")

    def test_misaligned_close_above_prev(self):
        """PUT but close > prev → MISALIGNED."""
        candle = {"close": 22010, "ema9": 22000, "ema13": 21990}
        indicators = {
            "close_prev_3m": 21995, "atr": 50,
            "st_bias_3m": "BEARISH", "candle_15m": None,
        }
        status, tf = self.func(candle, indicators, "PUT")
        self.assertEqual(status, "MISALIGNED")
        self.assertIsNone(tf)


class TestNeutralCases(unittest.TestCase):
    """Neutral / insufficient data cases."""

    func = staticmethod(entry_logic._check_bar_close_alignment)

    def test_neutral_no_prev(self):
        """No close_prev_3m → NEUTRAL."""
        candle = {"close": 22010, "ema9": 22000, "ema13": 21990}
        indicators = {
            "close_prev_3m": None, "atr": 50,
            "st_bias_3m": "BULLISH", "candle_15m": None,
        }
        status, tf = self.func(candle, indicators, "CALL")
        self.assertEqual(status, "NEUTRAL")
        self.assertIsNone(tf)

    def test_neutral_wrong_bias(self):
        """Close > prev but neutral bias → NEUTRAL or ALIGNED depending on logic."""
        candle = {"close": 22010, "ema9": 22000, "ema13": 21990}
        indicators = {
            "close_prev_3m": 21995, "atr": 50,
            "st_bias_3m": "NEUTRAL", "candle_15m": None,
        }
        status, tf = self.func(candle, indicators, "CALL")
        self.assertIn(status, ("NEUTRAL", "ALIGNED"))


class TestSpreadNoise(unittest.TestCase):
    """detect_spread_noise edge cases."""

    func = staticmethod(entry_logic.detect_spread_noise)

    def test_noise_small_drift(self):
        """Close-open drift ≤ 2 → True."""
        candle = {"close": 100.5, "open": 99.5, "high": 101, "low": 99}
        self.assertTrue(self.func(candle, {}))

    def test_noise_tiny_range(self):
        """Bar range ≤ 2 → True."""
        candle = {"close": 100.5, "open": 100.0, "high": 101.0, "low": 99.5}
        self.assertTrue(self.func(candle, {}))

    def test_noise_tiny_drift_large_range(self):
        """Small drift but large range → True (drift ≤ 2)."""
        candle = {"close": 100.5, "open": 100.0, "high": 103.0, "low": 97.0}
        self.assertTrue(self.func(candle, {}))

    def test_no_noise_normal_bar(self):
        """Normal bar → False."""
        candle = {"close": 105.0, "open": 100.0, "high": 106.0, "low": 99.0}
        self.assertFalse(self.func(candle, {}))

    def test_missing_fields_no_crash(self):
        """Missing fields → False, no crash."""
        self.assertFalse(self.func({}, {}))

    def test_exact_boundary(self):
        """Exactly 2 pt drift → True (≤ 2)."""
        candle = {"close": 102.0, "open": 100.0, "high": 103.0, "low": 97.0}
        self.assertTrue(self.func(candle, {}))


class TestAttributionTags(unittest.TestCase):
    """Verify attribution tags include timeframe."""

    func = staticmethod(entry_logic._check_bar_close_alignment)

    def test_3m_returns_3m_tf(self):
        candle = {"close": 22010, "ema9": 22000, "ema13": 21990}
        indicators = {
            "close_prev_3m": 21995, "atr": 50,
            "st_bias_3m": "BULLISH", "candle_15m": None,
        }
        _, tf = self.func(candle, indicators, "CALL")
        self.assertEqual(tf, "3m")

    def test_15m_returns_15m_tf(self):
        candle = {"close": 22010, "ema9": 22000, "ema13": 21990}
        candle_15m = pd.Series({
            "close": 22020, "ema9": 22010, "ema13": 22000,
            "supertrend_bias": "BULLISH",
        })
        indicators = {
            "close_prev_3m": 21995, "close_prev_15m": 22005,
            "atr": 50, "st_bias_3m": "BULLISH", "candle_15m": candle_15m,
        }
        _, tf = self.func(candle, indicators, "CALL")
        self.assertEqual(tf, "15m")

    def test_misaligned_returns_none_tf(self):
        candle = {"close": 21990, "ema9": 22000, "ema13": 22010}
        indicators = {
            "close_prev_3m": 22005, "atr": 50,
            "st_bias_3m": "BULLISH", "candle_15m": None,
        }
        _, tf = self.func(candle, indicators, "CALL")
        self.assertIsNone(tf)


if __name__ == "__main__":
    unittest.main()
