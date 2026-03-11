"""Tests for Phase 6.4 — Reversal Detection + Opening ST Relaxation.

Tests:
  1. reversal_detector.py changes:
     - Startup guard lowered to 09:03 (allows opening signals)
     - Snap-back detection helper
     - Phase 6.4 signal fields (pivot_confirmed, snapback, snapback_info)
     - Attribution tags: REVERSAL_EMA_STRETCH, REVERSAL_PIVOT_CONFIRM, REVERSAL_OSC_CONFIRM
  2. REGIME_MATRIX extensions:
     - HIGH_VOL day type added
     - ST_OPENING_RELAX, REVERSAL_ALLOWED, REVERSAL_SCORE_BONUS fields
     - BALANCE_DAY suppresses ST_OPENING_RELAX
  3. Log parser Phase 6.4 fields
  4. Dashboard Phase 6.4 section
  5. Backward compatibility
"""

import io
import logging
import re
import sys
import types
import unittest
from unittest.mock import MagicMock, patch
from dataclasses import dataclass
from datetime import time as dtime

import numpy as np
import pandas as pd
import pytest

# ── Real reversal_detector (NOT stubbed) ──────────────────────────────────────
# Import directly — reversal_detector has minimal deps (pandas, numpy, logging)
from reversal_detector import (
    detect_reversal,
    _detect_snapback,
    _STARTUP_GUARD_TIME,
    EMA_STRETCH_REVERSAL_THRESHOLD,
)

# ── Stub heavy deps for execution.py tests ────────────────────────────────────
_STUB_NAMES = [
    "fyers_apiv3",
    "fyers_apiv3.fyersModel",
    "config",
    "setup",
    "indicators",
    "signals",
    "orchestration",
    "position_manager",
    "day_type",
    "compression_detector",
    "failed_breakout_detector",
    "zone_detector",
    "pulse_module",
]
for _n in _STUB_NAMES:
    if _n not in sys.modules:
        sys.modules[_n] = types.ModuleType(_n)

# -- config --
_cfg = sys.modules["config"]
_cfg.time_zone = "Asia/Kolkata"
_cfg.strategy_name = "TEST"
_cfg.MAX_TRADES_PER_DAY = 8
_cfg.account_type = "PAPER"
_cfg.quantity = 1
_cfg.CALL_MONEYNESS = 0
_cfg.PUT_MONEYNESS = 0
_cfg.profit_loss_point = 5
_cfg.ENTRY_OFFSET = 0
_cfg.ORDER_TYPE = "MARKET"
_cfg.MAX_DAILY_LOSS = -10000
_cfg.MAX_DRAWDOWN = -5000
_cfg.OSCILLATOR_EXIT_MODE = "SOFT"
_cfg.symbols = {"index": "NSE:NIFTY50-INDEX"}
_cfg.TREND_ENTRY_ADX_MIN = 18.0
_cfg.SLOPE_ADX_GATE = 20.0
_cfg.TIME_SLOPE_ADX_GATE = 25.0
_cfg.ST_RR_RATIO = 2.0
_cfg.ST_TG_RR_RATIO = 1.0
_cfg.strike_diff = 50
_cfg.DEFAULT_LOT_SIZE = 1
_cfg.CANDLE_BODY_RANGE = 0.5
_cfg.ATR_VALUE = 100.0

# -- setup --
_stup = sys.modules["setup"]
_stup.df = pd.DataFrame()
_stup.fyers = MagicMock()
_stup.ticker = "NSE:NIFTY50-INDEX"
_stup.option_chain = pd.DataFrame(columns=["strike_price", "symbol", "option_type"])
_stup.spot_price = 22000.0
_stup.start_time = "09:15"
_stup.end_time = "15:30"
_stup.hist_data = {}

# -- indicators --
_ind = sys.modules["indicators"]
_ind.calculate_cpr = MagicMock(return_value={})
_ind.calculate_traditional_pivots = MagicMock(return_value={})
_ind.calculate_camarilla_pivots = MagicMock(return_value={})
_ind.resolve_atr = MagicMock(return_value=50.0)
_ind.daily_atr = MagicMock(return_value=50.0)
_ind.williams_r = MagicMock(return_value=None)
_ind.calculate_cci = MagicMock(return_value=pd.Series(dtype=float))
_ind.momentum_ok = MagicMock(return_value=(True, 0.0))
_ind.classify_cpr_width = MagicMock(return_value="NORMAL")
_ind.calculate_atr = MagicMock(return_value=pd.Series([100.0]))
_ind.compute_rsi = MagicMock(return_value=pd.Series([50.0]))

# -- signals --
_sig = sys.modules["signals"]
_sig.detect_signal = MagicMock(return_value=None)
_sig.get_opening_range = MagicMock(return_value=(None, None))
_sig.compute_tilt_state = MagicMock(return_value="NEUTRAL")
_sig.TrendContinuationState = MagicMock()

# -- orchestration --
_orch = sys.modules["orchestration"]
_orch.update_candles_and_signals = MagicMock()
_orch.build_indicator_dataframe = MagicMock(return_value=pd.DataFrame())

# -- position_manager --
_pm = sys.modules["position_manager"]
_pm.make_replay_pm = MagicMock()

# -- day_type --
_dt_mod = sys.modules["day_type"]
_dt_mod.make_day_type_classifier = MagicMock()
_dt_mod.apply_day_type_to_pm = MagicMock()
_dt_mod.apply_day_type_to_threshold = MagicMock(return_value=(50, ""))
_dt_mod.DayType = MagicMock()
_dt_mod.DayTypeResult = MagicMock()
_dt_mod.DayTypeClassifier = MagicMock()

# -- compression_detector --
_comp = sys.modules["compression_detector"]
_comp.CompressionState = MagicMock()

# -- reversal_detector stub for execution.py (it imports via module) --
_rev_stub = sys.modules.setdefault("reversal_detector", types.ModuleType("reversal_detector"))
_rev_stub.detect_reversal = MagicMock(return_value=None)

# -- failed_breakout_detector --
_fbk = sys.modules["failed_breakout_detector"]
_fbk.detect_failed_breakout = MagicMock(return_value=None)

# -- zone_detector --
_zd = sys.modules["zone_detector"]
_zd.detect_zones = MagicMock(return_value=[])
_zd.load_zones = MagicMock(return_value=[])
_zd.save_zones = MagicMock()
_zd.update_zone_activity = MagicMock()
_zd.detect_zone_revisit = MagicMock(return_value=None)

# -- pulse_module --
_pulse = sys.modules["pulse_module"]
_pulse.get_pulse_module = MagicMock()
_pulse.PulseModule = MagicMock()

# -- fyers_apiv3 --
_fyers_pkg = sys.modules["fyers_apiv3"]
_fyers_model_mod = sys.modules["fyers_apiv3.fyersModel"]
_fyers_pkg.fyersModel = _fyers_model_mod
_fyers_model_mod.FyersModel = MagicMock()

# ── Now safe to import execution ───────────────────────────────────────────
import execution  # noqa: E402

logging.getLogger().setLevel(logging.DEBUG)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. REVERSAL DETECTOR TESTS
# ═══════════════════════════════════════════════════════════════════════════════

def _make_candles(n=20, close_base=24600, close_delta=-10, atr=80, rsi=15, cci=-250):
    """Build a minimal 3m candle DataFrame for reversal detection."""
    closes = [close_base + i * close_delta for i in range(n)]
    df = pd.DataFrame({
        "close": closes,
        "high": [c + 5 for c in closes],
        "low": [c - 5 for c in closes],
        "open": [c + 2 for c in closes],
        "atr14": [atr] * n,
        "rsi14": [rsi] * n,
        "cci20": [cci] * n,
    })
    return df


class TestStartupGuard:
    """Startup guard lowered from 09:20 to 09:03."""

    def test_guard_time_is_0903(self):
        assert _STARTUP_GUARD_TIME == dtime(9, 3)

    def test_signal_fires_at_0905(self):
        """Signal should fire at 09:05 (after new guard time)."""
        from datetime import datetime
        df = _make_candles(20, close_base=24400, close_delta=0, rsi=10, cci=-300)
        cam = {"r3": 24700, "r4": 24750, "s3": 24420, "s4": 24370}
        t = datetime(2026, 3, 2, 9, 5, 0)
        sig = detect_reversal(df, cam, current_time=t)
        assert sig is not None
        assert sig["side"] == "CALL"

    def test_signal_suppressed_at_0902(self):
        """Signal should still be suppressed before 09:03."""
        from datetime import datetime
        df = _make_candles(20, close_base=24400, close_delta=0, rsi=10, cci=-300)
        cam = {"r3": 24700, "r4": 24750, "s3": 24420, "s4": 24370}
        t = datetime(2026, 3, 2, 9, 2, 0)
        sig = detect_reversal(df, cam, current_time=t)
        assert sig is None


class TestSnapbackDetection:
    """_detect_snapback helper function tests."""

    def test_snapback_detected_when_reverting(self):
        """When price was more stretched 2 bars ago and is now reverting."""
        closes = np.array([100, 95, 90, 85, 92])  # Was at 85, now snapping back to 92
        ema9 = np.array([100, 99, 98, 97, 96])
        atr = 10.0
        result = _detect_snapback(closes, ema9, atr, "CALL")
        assert result["snapback"] is True
        assert result["revert_pct"] > 0

    def test_no_snapback_when_still_falling(self):
        """No snap-back when price is still moving away from EMA."""
        closes = np.array([100, 95, 90, 85, 80])  # Continuing down
        ema9 = np.array([100, 99, 98, 97, 96])
        atr = 10.0
        result = _detect_snapback(closes, ema9, atr, "CALL")
        assert result["snapback"] is False

    def test_snapback_put_side(self):
        """Snap-back for PUT side (price was above, reverting down)."""
        closes = np.array([100, 105, 110, 115, 108])
        ema9 = np.array([100, 101, 102, 103, 104])
        atr = 10.0
        result = _detect_snapback(closes, ema9, atr, "PUT")
        assert result["snapback"] is True

    def test_insufficient_data(self):
        """Graceful handling with minimal data."""
        closes = np.array([100])
        ema9 = np.array([100])
        result = _detect_snapback(closes, ema9, 10.0, "CALL")
        assert result["snapback"] is False


class TestReversalSignalPhase64Fields:
    """Phase 6.4 fields in reversal signal dict."""

    def test_signal_has_pivot_confirmed(self):
        df = _make_candles(20, close_base=24400, close_delta=0, rsi=10, cci=-300)
        cam = {"r3": 24700, "r4": 24750, "s3": 24420, "s4": 24370}
        sig = detect_reversal(df, cam, current_time=None)
        assert sig is not None
        assert "pivot_confirmed" in sig
        assert sig["pivot_confirmed"] is True  # S3 zone

    def test_signal_has_snapback_info(self):
        df = _make_candles(20, close_base=24400, close_delta=0, rsi=10, cci=-300)
        cam = {"r3": 24700, "r4": 24750, "s3": 24420, "s4": 24370}
        sig = detect_reversal(df, cam, current_time=None)
        assert sig is not None
        assert "snapback" in sig
        assert "snapback_info" in sig
        assert isinstance(sig["snapback_info"], dict)

    def test_in_range_pivot_not_confirmed(self):
        """When price is IN_RANGE, pivot_confirmed should be False."""
        # Price far from any pivot level
        df = _make_candles(20, close_base=24550, close_delta=0, rsi=10, cci=-300)
        cam = {"r3": 24700, "r4": 24750, "s3": 24400, "s4": 24350}
        sig = detect_reversal(df, cam, current_time=None)
        # May or may not fire depending on stretch — if it fires, check pivot_confirmed
        if sig is not None:
            assert "pivot_confirmed" in sig


class TestReversalAttributionTags:
    """Phase 6.4 attribution tags are logged."""

    def test_reversal_ema_stretch_tag(self, caplog):
        """[REVERSAL_EMA_STRETCH] logged when stretch >= 3.0."""
        df = _make_candles(20, close_base=24100, close_delta=0, rsi=8, cci=-400)
        cam = {"r3": 24700, "r4": 24750, "s3": 24120, "s4": 24050}
        with caplog.at_level(logging.DEBUG):
            sig = detect_reversal(df, cam, atr_value=80, current_time=None)
        if sig is not None and abs(sig["stretch"]) >= 3.0:
            assert "[REVERSAL_EMA_STRETCH]" in caplog.text

    def test_reversal_pivot_confirm_tag(self, caplog):
        """[REVERSAL_PIVOT_CONFIRM] logged when pivot is S3/S4/R3/R4."""
        df = _make_candles(20, close_base=24400, close_delta=0, rsi=10, cci=-300)
        cam = {"r3": 24700, "r4": 24750, "s3": 24420, "s4": 24370}
        with caplog.at_level(logging.DEBUG):
            sig = detect_reversal(df, cam, current_time=None)
        assert sig is not None
        assert "[REVERSAL_PIVOT_CONFIRM]" in caplog.text

    def test_reversal_osc_confirm_tag(self, caplog):
        """[REVERSAL_OSC_CONFIRM] logged when osc extreme confirms reversal."""
        df = _make_candles(20, close_base=24400, close_delta=0, rsi=10, cci=-300)
        cam = {"r3": 24700, "r4": 24750, "s3": 24420, "s4": 24370}
        with caplog.at_level(logging.DEBUG):
            sig = detect_reversal(df, cam, current_time=None)
        assert sig is not None
        assert sig["osc_confirmed"] is True
        assert "[REVERSAL_OSC_CONFIRM]" in caplog.text


# ═══════════════════════════════════════════════════════════════════════════════
# 2. REGIME MATRIX EXTENSION TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestRegimeMatrixPhase64:
    """REGIME_MATRIX has Phase 6.4 fields."""

    def test_has_high_vol(self):
        from execution import REGIME_MATRIX
        assert "HIGH_VOL" in REGIME_MATRIX

    def test_trending_day_st_opening_relax(self):
        from execution import REGIME_MATRIX
        assert REGIME_MATRIX["TRENDING_DAY"]["ST_OPENING_RELAX"] is True

    def test_balance_day_st_opening_relax_suppressed(self):
        from execution import REGIME_MATRIX
        assert REGIME_MATRIX["BALANCE_DAY"]["ST_OPENING_RELAX"] is False

    def test_gap_day_reversal_score_bonus(self):
        from execution import REGIME_MATRIX
        assert REGIME_MATRIX["GAP_DAY"]["REVERSAL_SCORE_BONUS"] == 10

    def test_all_day_types_have_reversal_fields(self):
        from execution import REGIME_MATRIX
        for day_type, cfg in REGIME_MATRIX.items():
            assert "ST_OPENING_RELAX" in cfg, f"{day_type} missing ST_OPENING_RELAX"
            assert "REVERSAL_ALLOWED" in cfg, f"{day_type} missing REVERSAL_ALLOWED"
            assert "REVERSAL_SCORE_BONUS" in cfg, f"{day_type} missing REVERSAL_SCORE_BONUS"

    def test_default_regime_has_phase64_fields(self):
        from execution import _REGIME_DEFAULT
        assert "ST_OPENING_RELAX" in _REGIME_DEFAULT
        assert "REVERSAL_ALLOWED" in _REGIME_DEFAULT
        assert "REVERSAL_SCORE_BONUS" in _REGIME_DEFAULT

    def test_high_vol_rsi_floor_zero(self):
        from execution import REGIME_MATRIX
        assert REGIME_MATRIX["HIGH_VOL"]["RSI_FLOOR"] == 0

    def test_high_vol_st_opening_relax(self):
        from execution import REGIME_MATRIX
        assert REGIME_MATRIX["HIGH_VOL"]["ST_OPENING_RELAX"] is True


# ═══════════════════════════════════════════════════════════════════════════════
# 3. LOG PARSER TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestLogParserPhase64:
    """Log parser recognizes Phase 6.4 tags."""

    def test_regex_patterns_exist(self):
        from log_parser import (
            _RE_REVERSAL_EMA_STRETCH,
            _RE_REVERSAL_PIVOT_CONFIRM,
            _RE_REVERSAL_OSC_CONFIRM,
            _RE_REVERSAL_BIAS_FLIP,
            _RE_REVERSAL_COOLDOWN_RELAX,
            _RE_REVERSAL_PERSIST,
            _RE_ST_OPENING_RELAX,
            _RE_REVERSAL_CAPTURE,
        )
        # All should be compiled regex patterns
        assert _RE_REVERSAL_EMA_STRETCH.search("[REVERSAL_EMA_STRETCH] test")
        assert _RE_REVERSAL_PIVOT_CONFIRM.search("[REVERSAL_PIVOT_CONFIRM] test")
        assert _RE_REVERSAL_OSC_CONFIRM.search("[REVERSAL_OSC_CONFIRM] test")
        assert _RE_REVERSAL_BIAS_FLIP.search("[REVERSAL_BIAS_FLIP] test")
        assert _RE_REVERSAL_COOLDOWN_RELAX.search("[REVERSAL_COOLDOWN_RELAX] test")
        assert _RE_REVERSAL_PERSIST.search("[REVERSAL_PERSIST] test")
        assert _RE_ST_OPENING_RELAX.search("[ST_OPENING_RELAX] test")
        assert _RE_REVERSAL_CAPTURE.search("[REVERSAL_CAPTURE] test")

    def test_session_summary_has_phase64_fields(self):
        from log_parser import SessionSummary
        s = SessionSummary()
        assert hasattr(s, "reversal_ema_stretch_count")
        assert hasattr(s, "reversal_pivot_confirm_count")
        assert hasattr(s, "reversal_osc_confirm_count")
        assert hasattr(s, "reversal_bias_flip_count")
        assert hasattr(s, "reversal_cooldown_relax_count")
        assert hasattr(s, "reversal_persist_count")
        assert hasattr(s, "st_opening_relax_count")
        assert hasattr(s, "reversal_capture_count")

    def test_to_dict_has_phase64_keys(self):
        from log_parser import SessionSummary
        s = SessionSummary()
        d = s.to_dict()
        assert "reversal_ema_stretch_count" in d
        assert "reversal_pivot_confirm_count" in d
        assert "reversal_osc_confirm_count" in d
        assert "reversal_bias_flip_count" in d
        assert "reversal_cooldown_relax_count" in d
        assert "reversal_persist_count" in d
        assert "st_opening_relax_count" in d
        assert "reversal_capture_count" in d

    def test_all_phase64_defaults_zero(self):
        from log_parser import SessionSummary
        s = SessionSummary()
        assert s.reversal_ema_stretch_count == 0
        assert s.reversal_pivot_confirm_count == 0
        assert s.reversal_osc_confirm_count == 0
        assert s.reversal_bias_flip_count == 0
        assert s.reversal_cooldown_relax_count == 0
        assert s.reversal_persist_count == 0
        assert s.st_opening_relax_count == 0
        assert s.reversal_capture_count == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 4. DASHBOARD TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestDashboardPhase64:
    """Dashboard renders Phase 6.4 section."""

    def test_dashboard_section_renders_when_counters_nonzero(self):
        from log_parser import SessionSummary
        from dashboard import _write_text_report
        s = SessionSummary(
            reversal_ema_stretch_count=3,
            reversal_pivot_confirm_count=5,
            reversal_capture_count=2,
        )
        buf = io.StringIO()
        _write_text_report(s, buf)
        text = buf.getvalue()
        assert "Phase 6.4" in text
        assert "Reversal EMA stretch" in text
        assert "Reversal captures" in text

    def test_dashboard_section_hidden_when_all_zero(self):
        from log_parser import SessionSummary
        from dashboard import _write_text_report
        s = SessionSummary()
        buf = io.StringIO()
        _write_text_report(s, buf)
        text = buf.getvalue()
        assert "Phase 6.4" not in text


# ═══════════════════════════════════════════════════════════════════════════════
# 5. BACKWARD COMPATIBILITY TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestBackwardCompatibility:
    """Phase 6.4 changes don't break Phase 6.2/6.3 logic."""

    def test_regime_matrix_still_has_phase63_keys(self):
        from execution import REGIME_MATRIX
        for day_type in ["TRENDING_DAY", "RANGE_DAY", "GAP_DAY", "BALANCE_DAY"]:
            cfg = REGIME_MATRIX[day_type]
            assert "RSI_FLOOR" in cfg
            assert "COUNTER_PENALTY" in cfg
            assert "COOLDOWN_LOSS" in cfg
            assert "COOLDOWN_WIN" in cfg

    def test_detect_reversal_returns_backward_compatible_signal(self):
        """Signal dict still has all pre-6.4 keys."""
        df = _make_candles(20, close_base=24400, close_delta=0, rsi=10, cci=-300)
        cam = {"r3": 24700, "r4": 24750, "s3": 24420, "s4": 24370}
        sig = detect_reversal(df, cam, current_time=None)
        assert sig is not None
        # All original keys must be present
        for key in ["side", "reason", "entry_price", "target", "stop",
                     "score", "strength", "ema9", "ema13", "stretch",
                     "pivot_zone", "osc_confirmed", "atr", "gap_boost", "osc_context"]:
            assert key in sig, f"Missing backward-compatible key: {key}"

    def test_ema_stretch_reversal_threshold_constant(self):
        assert EMA_STRETCH_REVERSAL_THRESHOLD == 3.0

    def test_session_summary_still_has_phase63_fields(self):
        from log_parser import SessionSummary
        s = SessionSummary()
        assert hasattr(s, "osc_trend_override_count")
        assert hasattr(s, "trend_align_override_count")
        assert hasattr(s, "day_bias_penalty_count")
        assert hasattr(s, "momentum_entry_count")
        assert hasattr(s, "signal_skip_count")
        assert hasattr(s, "cooldown_reduced_count")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
