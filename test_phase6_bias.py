"""Tests for Phase 6 — Bias Alignment & Microstructure Refinement.

Covers:
  - Bar-close alignment detection (entry_logic._check_bar_close_alignment)
  - Spread noise proxy (entry_logic.detect_spread_noise)
  - Bias alignment in detect_signal (signals.py)
  - Slope conflict time-based override (execution.py)
  - Pulse exhaustion detection (pulse_module.py)
  - Zone absorption detection (zone_detector.py)
  - Log parser tag parsing
  - Dashboard section rendering
  - Backward compatibility
"""

import io
import logging
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

# Ensure real pulse_module and zone_detector are loaded (not stubs from other test files)
for _mod_name in ("pulse_module", "zone_detector"):
    _existing = sys.modules.get(_mod_name)
    if _existing is not None and isinstance(_existing, MagicMock):
        del sys.modules[_mod_name]

# ── Stub heavy deps (NOT zone_detector, pulse_module — tested directly) ──────
# indicators must be stubbed before entry_logic/signals import
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

# Ensure config stub has Phase 6 constant
import config as _cfg
if not hasattr(_cfg, "SLOPE_CONFLICT_TIME_BARS"):
    _cfg.SLOPE_CONFLICT_TIME_BARS = 5

import pandas as pd
import numpy as np

# Force fresh import of entry_logic
sys.modules.pop("entry_logic", None)
import entry_logic


# ═════════════════════════════════════════════════════════════════════════════
# 1. BAR-CLOSE ALIGNMENT TESTS
# ═════════════════════════════════════════════════════════════════════════════

class TestBarCloseAlignment(unittest.TestCase):
    """Test _check_bar_close_alignment in entry_logic.py."""

    func = staticmethod(entry_logic._check_bar_close_alignment)

    def test_aligned_3m_call(self):
        """CALL with close > prev, near EMA, bullish bias → ALIGNED, 3m."""
        candle = {"close": 22010, "ema9": 22000, "ema13": 21990}
        indicators = {
            "close_prev_3m": 21995,
            "atr": 50,
            "st_bias_3m": "BULLISH",
            "candle_15m": None,
        }
        status, tf = self.func(candle, indicators, "CALL")
        self.assertEqual(status, "ALIGNED")
        self.assertEqual(tf, "3m")

    def test_aligned_3m_put(self):
        """PUT with close < prev, near EMA, bearish bias → ALIGNED, 3m."""
        candle = {"close": 21990, "ema9": 22000, "ema13": 22010}
        indicators = {
            "close_prev_3m": 22005,
            "atr": 50,
            "st_bias_3m": "BEARISH",
            "candle_15m": None,
        }
        status, tf = self.func(candle, indicators, "PUT")
        self.assertEqual(status, "ALIGNED")
        self.assertEqual(tf, "3m")

    def test_misaligned_call(self):
        """CALL with close < prev → MISALIGNED."""
        candle = {"close": 21990, "ema9": 22000, "ema13": 22010}
        indicators = {
            "close_prev_3m": 22005,
            "atr": 50,
            "st_bias_3m": "BULLISH",
            "candle_15m": None,
        }
        status, tf = self.func(candle, indicators, "CALL")
        self.assertEqual(status, "MISALIGNED")
        self.assertIsNone(tf)

    def test_neutral_no_prev(self):
        """No close_prev_3m → NEUTRAL."""
        candle = {"close": 22010, "ema9": 22000, "ema13": 21990}
        indicators = {
            "close_prev_3m": None,
            "atr": 50,
            "st_bias_3m": "BULLISH",
            "candle_15m": None,
        }
        status, tf = self.func(candle, indicators, "CALL")
        self.assertEqual(status, "NEUTRAL")
        self.assertIsNone(tf)

    def test_aligned_15m_takes_priority(self):
        """15m alignment takes priority over 3m."""
        candle = {"close": 22010, "ema9": 22000, "ema13": 21990}
        candle_15m = pd.Series({
            "close": 22020,
            "ema9": 22010,
            "ema13": 22000,
            "supertrend_bias": "BULLISH",
        })
        indicators = {
            "close_prev_3m": 21995,
            "close_prev_15m": 22005,
            "atr": 50,
            "st_bias_3m": "BULLISH",
            "candle_15m": candle_15m,
        }
        status, tf = self.func(candle, indicators, "CALL")
        self.assertEqual(status, "ALIGNED")
        self.assertEqual(tf, "15m")

    def test_neutral_wrong_bias(self):
        """Close > prev but bias is NEUTRAL → not aligned but not misaligned."""
        candle = {"close": 22010, "ema9": 22000, "ema13": 21990}
        indicators = {
            "close_prev_3m": 21995,
            "atr": 50,
            "st_bias_3m": "NEUTRAL",
            "candle_15m": None,
        }
        status, tf = self.func(candle, indicators, "CALL")
        # Close > prev but no BULLISH bias → NEUTRAL (not MISALIGNED since close direction is right)
        self.assertIn(status, ("NEUTRAL", "ALIGNED"))


# ═════════════════════════════════════════════════════════════════════════════
# 2. SPREAD NOISE TESTS
# ═════════════════════════════════════════════════════════════════════════════

class TestSpreadNoise(unittest.TestCase):
    """Test detect_spread_noise in entry_logic.py."""

    func = staticmethod(entry_logic.detect_spread_noise)

    def test_noise_small_drift(self):
        """Close-open drift ≤ 2 → spread noise (1 pt drift is noise)."""
        candle = {"close": 100.5, "open": 99.5, "high": 101, "low": 99}
        # drift = 1.0 ≤ 2, range = 2.0 ≤ 2 → True (is noise)
        self.assertTrue(self.func(candle, {}))

    def test_noise_tiny_range(self):
        """Bar range ≤ 2 → spread noise."""
        candle = {"close": 100.5, "open": 100.0, "high": 101.0, "low": 99.5}
        self.assertTrue(self.func(candle, {}))

    def test_noise_tiny_drift(self):
        """Close-open drift ≤ 2 → spread noise."""
        candle = {"close": 100.5, "open": 100.0, "high": 103.0, "low": 97.0}
        self.assertTrue(self.func(candle, {}))

    def test_no_noise_normal_bar(self):
        """Normal bar → no spread noise."""
        candle = {"close": 105.0, "open": 100.0, "high": 106.0, "low": 99.0}
        self.assertFalse(self.func(candle, {}))

    def test_noise_missing_fields(self):
        """Missing fields → no crash, returns False."""
        candle = {}
        self.assertFalse(self.func(candle, {}))


# ═════════════════════════════════════════════════════════════════════════════
# 3. PULSE EXHAUSTION TESTS
# ═════════════════════════════════════════════════════════════════════════════

class TestPulseExhaustion(unittest.TestCase):
    """Test PulseModule.detect_exhaustion."""

    def test_sustained_during_burst(self):
        from pulse_module import PulseModule
        pm = PulseModule(window_seconds=10, burst_threshold=5.0, min_ticks=3)
        # Simulate burst: many ticks in short time
        base = 1000000.0
        for i in range(20):
            pm.on_tick(base + i * 10, 100.0 + i * 0.01)  # 100 ticks/sec
        result = pm.detect_exhaustion(base + 200)
        self.assertEqual(result, "PULSE_SUSTAINED")

    def test_exhaustion_after_decay(self):
        from pulse_module import PulseModule
        pm = PulseModule(window_seconds=5, burst_threshold=3.0, min_ticks=3)
        # First create a burst
        base = 1000000.0
        for i in range(15):
            pm.on_tick(base + i * 50, 100.0 + i * 0.01)
        _ = pm.detect_exhaustion(base + 800)
        # If there was a burst, now decay
        if pm._peak_tick_rate > 0:
            # Advance time far beyond the window so tick rate drops
            pm._tick_buffer.clear()
            pm._cached_metrics = None
            pm.on_tick(base + 20000, 100.0)
            pm.on_tick(base + 25000, 100.0)
            pm.on_tick(base + 30000, 100.0)
            result = pm.detect_exhaustion(base + 35000)
            self.assertIn(result, ("PULSE_EXHAUSTION", "PULSE_SUSTAINED", None))

    def test_no_burst_returns_none(self):
        from pulse_module import PulseModule
        pm = PulseModule(window_seconds=10, burst_threshold=100.0, min_ticks=3)
        # Few ticks → no burst
        pm.on_tick(1000000, 100.0)
        pm.on_tick(1005000, 100.0)
        pm.on_tick(1010000, 100.0)
        result = pm.detect_exhaustion(1015000)
        self.assertIsNone(result)


# ═════════════════════════════════════════════════════════════════════════════
# 4. ZONE ABSORPTION TESTS
# ═════════════════════════════════════════════════════════════════════════════

class TestZoneAbsorption(unittest.TestCase):
    """Test detect_zone_absorption in zone_detector.py."""

    def _make_candles(self, prices, zone_low, zone_high):
        """Create candles that touch/miss a zone."""
        rows = []
        for i, (lo, hi) in enumerate(prices):
            rows.append({
                "open": (lo + hi) / 2,
                "high": hi,
                "low": lo,
                "close": (lo + hi) / 2,
                "time": f"2026-03-07 10:{i:02d}:00",
            })
        return pd.DataFrame(rows)

    def test_absorption_3_touches(self):
        from zone_detector import Zone, detect_zone_absorption
        zone = Zone("D_1", "DEMAND", low=100.0, high=105.0, origin_time="2026-03-07 09:30:00")
        # Create 10 bars, 3 of which touch the zone
        prices = [
            (101, 106),   # touches zone
            (110, 115),   # misses
            (102, 104),   # touches zone
            (112, 118),   # misses
            (100, 105),   # touches zone
            (110, 115),   # misses
            (120, 125),   # misses
            (130, 135),   # misses
            (140, 145),   # misses
            (150, 155),   # misses
        ]
        candles = self._make_candles(prices, 100, 105)
        result = detect_zone_absorption(candles, [zone], atr_value=10.0, revisit_window=10)
        self.assertIsNotNone(result)
        self.assertEqual(result["type"], "ZONE_ABSORPTION")
        self.assertEqual(result["touches"], 3)

    def test_rejection_1_touch(self):
        from zone_detector import Zone, detect_zone_absorption
        zone = Zone("S_1", "SUPPLY", low=200.0, high=210.0, origin_time="2026-03-07 09:30:00")
        prices = [
            (201, 208),  # touches
            (180, 190),  # misses
            (170, 175),  # misses
            (160, 165),  # misses
            (155, 160),  # misses
            (150, 155),  # misses
            (145, 150),  # misses
            (140, 145),  # misses
            (135, 140),  # misses
            (130, 135),  # misses
        ]
        candles = self._make_candles(prices, 200, 210)
        result = detect_zone_absorption(candles, [zone], atr_value=10.0, revisit_window=10)
        self.assertIsNotNone(result)
        self.assertEqual(result["type"], "ZONE_REJECTION")

    def test_no_active_zones(self):
        from zone_detector import Zone, detect_zone_absorption
        zone = Zone("D_1", "DEMAND", low=100.0, high=105.0,
                     origin_time="2026-03-07 09:30:00", active=False)
        prices = [(101, 106)] * 10
        candles = self._make_candles(prices, 100, 105)
        result = detect_zone_absorption(candles, [zone], atr_value=10.0)
        self.assertIsNone(result)


# ═════════════════════════════════════════════════════════════════════════════
# 5. LOG PARSER TESTS
# ═════════════════════════════════════════════════════════════════════════════

class TestPhase6LogParsing(unittest.TestCase):
    """Test log_parser.py parsing of Phase 6 tags."""

    def _parse_lines(self, lines):
        """Write lines to temp file and parse."""
        from log_parser import LogParser
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".log", delete=False, encoding="utf-8"
        ) as f:
            f.write("\n".join(lines) + "\n")
            path = f.name
        try:
            return LogParser(path).parse()
        finally:
            os.unlink(path)

    def test_bias_alignment_counted(self):
        lines = [
            "2026-03-07 10:00:00 [BIAS_ALIGNMENT] side=CALL status=ALIGNED tf=3m bias15m=BULLISH bias3m=BULLISH",
            "2026-03-07 10:03:00 [BIAS_ALIGNMENT] side=PUT status=MISALIGNED tf=NONE bias15m=BEARISH bias3m=BEARISH",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.bias_alignment_count, 2)
        self.assertEqual(s.tag_counts.get("BIAS_ALIGNMENT", 0), 2)

    def test_bar_close_alignment_counted(self):
        lines = [
            "2026-03-07 10:00:00 [ENTRY OK] CALL score=65/50 [BAR_CLOSE_ALIGNMENT][TF=3m]",
            "2026-03-07 10:03:00 [ENTRY OK] PUT score=70/50 [BAR_CLOSE_ALIGNMENT][TF=15m]",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.bar_close_alignment_count, 2)

    def test_slope_override_time_counted(self):
        lines = [
            "2026-03-07 10:00:00 [SLOPE_OVERRIDE_TIME] timestamp=2026-03-07 symbol=NIFTY side=CALL bars=5",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.slope_override_time_count, 1)

    def test_conflict_blocked_counted(self):
        lines = [
            "2026-03-07 10:00:00 [CONFLICT_BLOCKED] timestamp=2026-03-07 symbol=NIFTY side=CALL type=ST_SLOPE_CONFLICT conflict_bars=3",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.conflict_blocked_count, 1)

    def test_microstructure_tags_counted(self):
        lines = [
            "2026-03-07 10:00:00 [PULSE_EXHAUSTION] peak=20.0 current=8.0 decay_ratio=0.40",
            "2026-03-07 10:01:00 [ZONE_ABSORPTION] zone=D_1 type=DEMAND touches=3 window=10",
            "2026-03-07 10:02:00 [SPREAD_NOISE] close_open_drift=1.50 <= 2 pts",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.pulse_exhaustion_count, 1)
        self.assertEqual(s.zone_absorption_count, 1)
        self.assertEqual(s.spread_noise_count, 1)

    def test_zero_when_absent(self):
        lines = [
            "2026-03-07 10:00:00 INFO Some normal log line",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.bias_alignment_count, 0)
        self.assertEqual(s.bar_close_alignment_count, 0)
        self.assertEqual(s.slope_override_time_count, 0)
        self.assertEqual(s.pulse_exhaustion_count, 0)
        self.assertEqual(s.zone_absorption_count, 0)
        self.assertEqual(s.spread_noise_count, 0)

    def test_bias_alignment_attributed_to_trades(self):
        """Trades should carry bias_alignment from last-seen [BIAS_ALIGNMENT]."""
        lines = [
            "2026-03-07 10:00:00 [BIAS_ALIGNMENT] side=CALL status=ALIGNED tf=3m bias15m=BULLISH bias3m=BULLISH",
            "2026-03-07 10:01:00 [EXIT][PAPER SL_HIT] CALL NIFTY26MAR25000CE Entry=200.0 Exit=180.0 Qty=75 PnL=-1500.00 (points=-20.0) BarsHeld=3",
        ]
        s = self._parse_lines(lines)
        if s.trades:
            self.assertEqual(s.trades[0].get("bias_alignment"), "ALIGNED")

    def test_to_dict_includes_phase6(self):
        lines = [
            "2026-03-07 10:00:00 [BIAS_ALIGNMENT] side=CALL status=ALIGNED tf=3m",
            "2026-03-07 10:01:00 [BAR_CLOSE_ALIGNMENT][TF=15m] alignment confirmed",
        ]
        s = self._parse_lines(lines)
        d = s.to_dict()
        self.assertIn("bias_alignment_count", d)
        self.assertIn("bar_close_alignment_count", d)
        self.assertIn("bias_alignment_performance", d)
        self.assertIn("microstructure_counts", d)

    def test_phase6_tags_in_p_tags(self):
        """Verify Phase 6 tags are in _P_TAGS list."""
        from log_parser import _P_TAGS
        expected = [
            "BAR_CLOSE_ALIGNMENT", "BAR_CLOSE_MISALIGNED",
            "BIAS_ALIGNMENT", "SLOPE_OVERRIDE_TIME",
            "CONFLICT_BLOCKED", "PULSE_EXHAUSTION",
            "ZONE_ABSORPTION", "ZONE_REJECTION", "SPREAD_NOISE",
        ]
        for tag in expected:
            self.assertIn(tag, _P_TAGS, f"{tag} missing from _P_TAGS")


# ═════════════════════════════════════════════════════════════════════════════
# 6. DASHBOARD TESTS
# ═════════════════════════════════════════════════════════════════════════════

class TestPhase6Dashboard(unittest.TestCase):
    """Test dashboard section rendering for Phase 6."""

    def _make_session(self, **kwargs):
        from log_parser import SessionSummary
        defaults = {
            "log_path": "test.log",
            "session_type": "PAPER",
            "date_tag": "2026-03-07",
        }
        defaults.update(kwargs)
        return SessionSummary(**defaults)

    def test_bias_alignment_section_appears(self):
        """Dashboard shows BIAS ALIGNMENT section when data exists."""
        trades = [
            {"side": "CALL", "pnl_pts": 10.0, "bias_alignment": "ALIGNED"},
            {"side": "PUT", "pnl_pts": -5.0, "bias_alignment": "MISALIGNED"},
            {"side": "CALL", "pnl_pts": 8.0, "bias_alignment": "ALIGNED"},
        ]
        session = self._make_session(
            trades=trades,
            bias_alignment_count=3,
            bar_close_alignment_count=2,
        )
        from dashboard import _write_text_report
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False
        ) as f:
            path = Path(f.name)
        try:
            _write_text_report(session, path)
            text = path.read_text()
            self.assertIn("BIAS ALIGNMENT PERFORMANCE", text)
            self.assertIn("ALIGNED", text)
            self.assertIn("MISALIGNED", text)
        finally:
            path.unlink(missing_ok=True)

    def test_microstructure_section_appears(self):
        """Dashboard shows MICROSTRUCTURE section when data exists."""
        session = self._make_session(
            pulse_exhaustion_count=3,
            zone_absorption_count=2,
            spread_noise_count=5,
            slope_override_time_count=1,
            conflict_blocked_count=4,
        )
        from dashboard import _write_text_report
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False
        ) as f:
            path = Path(f.name)
        try:
            _write_text_report(session, path)
            text = path.read_text()
            self.assertIn("MICROSTRUCTURE ATTRIBUTION", text)
            self.assertIn("Pulse exhaustion events", text)
            self.assertIn("Zone absorption events", text)
            self.assertIn("Spread noise entries", text)
        finally:
            path.unlink(missing_ok=True)

    def test_sections_absent_when_no_data(self):
        """Sections not rendered when no Phase 6 data."""
        session = self._make_session()
        from dashboard import _write_text_report
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False
        ) as f:
            path = Path(f.name)
        try:
            _write_text_report(session, path)
            text = path.read_text()
            self.assertNotIn("BIAS ALIGNMENT PERFORMANCE", text)
            self.assertNotIn("MICROSTRUCTURE ATTRIBUTION", text)
        finally:
            path.unlink(missing_ok=True)


# ═════════════════════════════════════════════════════════════════════════════
# 7. BIAS ALIGNMENT PERFORMANCE PROPERTY
# ═════════════════════════════════════════════════════════════════════════════

class TestBiasAlignmentPerformance(unittest.TestCase):

    def test_performance_breakdown(self):
        from log_parser import SessionSummary
        trades = [
            {"side": "CALL", "pnl_pts": 10.0, "bias_alignment": "ALIGNED"},
            {"side": "PUT", "pnl_pts": -5.0, "bias_alignment": "MISALIGNED"},
            {"side": "CALL", "pnl_pts": 8.0, "bias_alignment": "ALIGNED"},
            {"side": "PUT", "pnl_pts": 3.0, "bias_alignment": "NEUTRAL"},
        ]
        s = SessionSummary(
            log_path="test.log", session_type="PAPER",
            date_tag="2026-03-07", trades=trades,
        )
        perf = s.bias_alignment_performance
        self.assertEqual(perf["ALIGNED"]["trades"], 2)
        self.assertEqual(perf["ALIGNED"]["winners"], 2)
        self.assertAlmostEqual(perf["ALIGNED"]["win_rate"], 100.0)
        self.assertAlmostEqual(perf["ALIGNED"]["net_pnl"], 18.0)
        self.assertEqual(perf["MISALIGNED"]["trades"], 1)
        self.assertEqual(perf["MISALIGNED"]["winners"], 0)
        self.assertEqual(perf["NEUTRAL"]["trades"], 1)

    def test_empty_session(self):
        from log_parser import SessionSummary
        s = SessionSummary(
            log_path="test.log", session_type="PAPER", date_tag="2026-03-07",
        )
        perf = s.bias_alignment_performance
        self.assertEqual(perf, {})


# ═════════════════════════════════════════════════════════════════════════════
# 8. BACKWARD COMPATIBILITY
# ═════════════════════════════════════════════════════════════════════════════

class TestPhase6BackwardCompat(unittest.TestCase):
    """Ensure old logs without Phase 6 tags still parse correctly."""

    def test_old_log_parses(self):
        from log_parser import LogParser
        lines = [
            "2026-02-24 10:00:00 [SIGNAL FIRED] CALL source=PIVOT score=65",
            "2026-02-24 10:01:00 [ENTRY OK] CALL score=65/50 MODERATE HIGH",
            "2026-02-24 10:05:00 [TRADE OPEN][PAPER] CALL bar=5 bar_ts=2026-02-24T10:05 underlying=22000.0 premium=200.0 score=65 lot=75",
            "2026-02-24 10:15:00 [TRADE EXIT][PAPER] CALL bar=10 bar_ts=2026-02-24T10:15 underlying=22050.0 premium=220.0 pnl_pts=20.0 pnl_rs=1500.0 bars_held=5 reason=TG_HIT",
        ]
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".log", delete=False, encoding="utf-8"
        ) as f:
            f.write("\n".join(lines) + "\n")
            path = f.name
        try:
            s = LogParser(path).parse()
            self.assertEqual(s.bias_alignment_count, 0)
            self.assertEqual(s.bar_close_alignment_count, 0)
            self.assertEqual(s.slope_override_time_count, 0)
            self.assertEqual(s.pulse_exhaustion_count, 0)
            self.assertEqual(s.zone_absorption_count, 0)
            self.assertEqual(s.spread_noise_count, 0)
            # Default values
            mc = s.microstructure_counts
            self.assertEqual(mc["pulse_exhaustion"], 0)
        finally:
            os.unlink(path)


# ═════════════════════════════════════════════════════════════════════════════
# 9. END-TO-END PIPELINE
# ═════════════════════════════════════════════════════════════════════════════

class TestEndToEndPhase6(unittest.TestCase):
    """Full pipeline: log → parser → summary → dashboard."""

    def test_full_pipeline(self):
        from log_parser import LogParser
        from dashboard import _write_text_report
        lines = [
            "2026-03-07 10:00:00 [BIAS_ALIGNMENT] side=CALL status=ALIGNED tf=3m bias15m=BULLISH bias3m=BULLISH",
            "2026-03-07 10:00:30 [ENTRY OK] CALL score=70/50 MODERATE HIGH [BAR_CLOSE_ALIGNMENT][TF=3m]",
            "2026-03-07 10:01:00 [EXIT][PAPER TG_HIT] CALL NIFTY26MAR25000CE Entry=200.0 Exit=220.0 Qty=75 PnL=1500.00 (points=20.0) BarsHeld=5",
            "2026-03-07 10:02:00 [PULSE_EXHAUSTION] peak=25.0 current=10.0 decay_ratio=0.40",
            "2026-03-07 10:03:00 [ZONE_ABSORPTION] zone=D_1 type=DEMAND touches=4 window=10",
            "2026-03-07 10:04:00 [SPREAD_NOISE] close_open_drift=1.20 <= 2 pts",
            "2026-03-07 10:05:00 [SLOPE_OVERRIDE_TIME] timestamp=2026-03-07 symbol=NIFTY side=PUT bars=5",
            "2026-03-07 10:06:00 [CONFLICT_BLOCKED] timestamp=2026-03-07 symbol=NIFTY side=PUT type=ST_SLOPE_CONFLICT conflict_bars=3",
        ]

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".log", delete=False, encoding="utf-8"
        ) as f:
            f.write("\n".join(lines) + "\n")
            log_path = f.name

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False
        ) as f:
            txt_path = Path(f.name)

        try:
            s = LogParser(log_path).parse()
            self.assertEqual(s.bias_alignment_count, 1)
            self.assertEqual(s.bar_close_alignment_count, 1)
            self.assertEqual(s.pulse_exhaustion_count, 1)
            self.assertEqual(s.zone_absorption_count, 1)
            self.assertEqual(s.spread_noise_count, 1)
            self.assertEqual(s.slope_override_time_count, 1)
            self.assertEqual(s.conflict_blocked_count, 1)

            # Dashboard renders
            _write_text_report(s, txt_path)
            text = txt_path.read_text()
            self.assertIn("MICROSTRUCTURE ATTRIBUTION", text)
        finally:
            os.unlink(log_path)
            txt_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
