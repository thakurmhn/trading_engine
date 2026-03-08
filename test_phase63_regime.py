"""Tests for Phase 6.3 — Regime-Aware Fixes Validation.

Tests the 6 regime-aware fixes:
  Fix 1: OSC_TREND_OVERRIDE — RSI floor removal on trend days
  Fix 2: TREND_ALIGN_OVERRIDE — alignment boost via ST conflict override
  Fix 3: DAY_BIAS_PENALTY — counter-trend score penalty
  Fix 4: MOMENTUM_ENTRY — momentum path signal synthesis
  Fix 5: SIGNAL_SKIP — gate open but detect_signal returned None
  Fix 6: COOLDOWN_REDUCED — regime-specific shorter cooldown

Also tests:
  - REGIME_MATRIX constant structure
  - Log parser integration (6 new SessionSummary fields)
  - Dashboard rendering of Phase 6.3 section
  - Backward compatibility (no prior logic broken)
"""

import io
import logging
import re
import sys
import types
import unittest
from unittest.mock import MagicMock, patch
from dataclasses import dataclass

import numpy as np
import pandas as pd
import pytest

# ── Stub all heavy dependencies BEFORE importing execution.py ─────────────────
_STUB_NAMES = [
    "fyers_apiv3",
    "fyers_apiv3.fyersModel",
    "config",
    "setup",
    "indicators",
    # "signals" — NOT stubbed; we need the real module for Fix 4 tests
    "orchestration",
    "position_manager",
    "day_type",
    "compression_detector",
    "reversal_detector",
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

# -- reversal_detector --
_rev = sys.modules["reversal_detector"]
_rev.detect_reversal = MagicMock(return_value=None)

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

# ── Now safe to import execution + real signals/entry_logic ───────────────────
import execution  # noqa: E402
from signals import detect_signal  # noqa: E402
from entry_logic import check_entry_condition  # noqa: E402

# Ensure root logger captures all levels
logging.getLogger().setLevel(logging.DEBUG)

# ── REGIME_MATRIX Tests ──────────────────────────────────────────────────────

class TestRegimeMatrix:
    """Test REGIME_MATRIX constant structure and defaults."""

    def test_regime_matrix_has_four_day_types(self):
        from execution import REGIME_MATRIX
        expected = {"TRENDING_DAY", "RANGE_DAY", "GAP_DAY", "BALANCE_DAY"}
        assert set(REGIME_MATRIX.keys()) == expected

    def test_trending_day_rsi_floor_zero(self):
        from execution import REGIME_MATRIX
        assert REGIME_MATRIX["TRENDING_DAY"]["RSI_FLOOR"] == 0

    def test_trending_day_counter_penalty_minus15(self):
        from execution import REGIME_MATRIX
        assert REGIME_MATRIX["TRENDING_DAY"]["COUNTER_PENALTY"] == -15

    def test_trending_day_cooldown_reduced(self):
        from execution import REGIME_MATRIX
        cfg = REGIME_MATRIX["TRENDING_DAY"]
        assert cfg["COOLDOWN_LOSS"] == 5
        assert cfg["COOLDOWN_WIN"] == 3

    def test_range_day_no_rsi_override(self):
        from execution import REGIME_MATRIX
        assert REGIME_MATRIX["RANGE_DAY"]["RSI_FLOOR"] is None

    def test_range_day_no_counter_penalty(self):
        from execution import REGIME_MATRIX
        assert REGIME_MATRIX["RANGE_DAY"]["COUNTER_PENALTY"] == 0

    def test_range_day_standard_cooldown(self):
        from execution import REGIME_MATRIX
        cfg = REGIME_MATRIX["RANGE_DAY"]
        assert cfg["COOLDOWN_LOSS"] == 10
        assert cfg["COOLDOWN_WIN"] == 5

    def test_gap_day_rsi_floor_five(self):
        from execution import REGIME_MATRIX
        assert REGIME_MATRIX["GAP_DAY"]["RSI_FLOOR"] == 5

    def test_gap_day_counter_penalty_minus10(self):
        from execution import REGIME_MATRIX
        assert REGIME_MATRIX["GAP_DAY"]["COUNTER_PENALTY"] == -10

    def test_balance_day_moderate_cooldown(self):
        from execution import REGIME_MATRIX
        cfg = REGIME_MATRIX["BALANCE_DAY"]
        assert cfg["COOLDOWN_LOSS"] == 7
        assert cfg["COOLDOWN_WIN"] == 5

    def test_regime_default_exists(self):
        from execution import _REGIME_DEFAULT
        assert _REGIME_DEFAULT["COOLDOWN_LOSS"] == 10
        assert _REGIME_DEFAULT["RSI_FLOOR"] is None

    def test_all_regimes_have_required_keys(self):
        from execution import REGIME_MATRIX
        required = {"RSI_FLOOR", "COUNTER_PENALTY", "COOLDOWN_LOSS", "COOLDOWN_WIN"}
        for day_type, cfg in REGIME_MATRIX.items():
            assert required.issubset(set(cfg.keys())), f"{day_type} missing keys"


# ── Fix 2: TREND_ALIGN_OVERRIDE (entry_logic.py) ─────────────────────────────

class TestTrendAlignOverride:
    """Fix 2: When st_conflict_override=True and ADX>=25, trend_alignment boosted to 10."""

    def _build_candle(self, close=22000.0):
        return {"close": close, "high": close + 50, "low": close - 50, "open": close - 10}

    def _build_indicators(self, rsi=50.0, cci=120.0, adx=30.0, ema9=22000.0,
                          st_bias_3m="BULLISH", supertrend_15m="BEARISH",
                          supertrend_slope="UP"):
        return {
            "rsi14": rsi, "cci14": cci, "adx14": adx, "adx": adx,
            "ema9": ema9, "ema13": ema9 - 5, "ema26": ema9 - 20,
            "atr14": 100.0, "atr": 100.0, "vwap": 22000.0,
            "st_bias_3m": st_bias_3m,
            "supertrend_3m": st_bias_3m,
            "supertrend_15m": supertrend_15m,
            "supertrend_slope": supertrend_slope,
            "macd": 5.0, "macd_signal": 3.0, "macd_hist": 2.0,
        }

    def test_override_boosts_alignment_when_adx_high(self, caplog):
        """With st_conflict_override=True and ADX>=25, trend_alignment goes from 0 to 10."""
        candle = self._build_candle()
        indicators = self._build_indicators(adx=30.0, st_bias_3m="BULLISH", supertrend_15m="BEARISH")
        st_details = {"st_conflict_override": True, "bias": "Positive"}

        with caplog.at_level(logging.DEBUG):
            result = check_entry_condition(
                candle, indicators, bias_15m="BEARISH",
                pivot_signal=("CALL", "BREAKOUT"),
                st_details=st_details,
            )

        assert "[TREND_ALIGN_OVERRIDE]" in caplog.text

    def test_no_override_when_adx_low(self, caplog):
        """With st_conflict_override=True but ADX<25, no override fires."""
        candle = self._build_candle()
        indicators = self._build_indicators(adx=20.0, st_bias_3m="BULLISH", supertrend_15m="BEARISH")
        st_details = {"st_conflict_override": True, "bias": "Positive"}

        with caplog.at_level(logging.DEBUG):
            result = check_entry_condition(
                candle, indicators, bias_15m="BEARISH",
                pivot_signal=("CALL", "BREAKOUT"),
                st_details=st_details,
            )

        assert "[TREND_ALIGN_OVERRIDE]" not in caplog.text

    def test_no_override_without_conflict_flag(self, caplog):
        """Without st_conflict_override, no override fires even with ADX>=25."""
        candle = self._build_candle()
        indicators = self._build_indicators(adx=35.0, st_bias_3m="BULLISH", supertrend_15m="BULLISH")
        st_details = {"st_conflict_override": False, "bias": "Positive"}

        with caplog.at_level(logging.DEBUG):
            result = check_entry_condition(
                candle, indicators, bias_15m="BULLISH",
                pivot_signal=("CALL", "BREAKOUT"),
                st_details=st_details,
            )

        assert "[TREND_ALIGN_OVERRIDE]" not in caplog.text


# ── Fix 3: DAY_BIAS_PENALTY (entry_logic.py) ─────────────────────────────────

class TestDayBiasPenalty:
    """Fix 3: Counter-trend entries on TRENDING days get -15 penalty."""

    def _build_candle(self, close=22000.0):
        return {"close": close, "high": close + 50, "low": close - 50, "open": close - 10}

    def _build_indicators(self, rsi=50.0, cci=120.0, adx=30.0):
        return {
            "rsi14": rsi, "cci14": cci, "adx14": adx, "adx": adx,
            "ema9": 22000.0, "ema13": 21995.0, "ema26": 21980.0,
            "atr14": 100.0, "atr": 100.0, "vwap": 22000.0,
            "st_bias_3m": "BULLISH",
            "supertrend_3m": "BULLISH", "supertrend_15m": "BULLISH",
            "supertrend_slope": "UP",
            "macd": 5.0, "macd_signal": 3.0, "macd_hist": 2.0,
        }

    def test_counter_trend_penalty_on_trending_day(self, caplog):
        """CALL entry on a BEARISH trending day gets -15 penalty."""
        # Create a mock day_type_result with name.value = "TRENDING"
        dt_name = MagicMock()
        dt_name.value = "TRENDING"
        day_type_result = MagicMock()
        day_type_result.name = dt_name

        candle = self._build_candle()
        indicators = self._build_indicators()
        # bias is BEARISH → CALL is counter-trend
        st_details = {"bias": "Negative", "open_bias_context": {"gap_tag": "NONE"}}

        with caplog.at_level(logging.DEBUG):
            result = check_entry_condition(
                candle, indicators, bias_15m="BEARISH",
                pivot_signal=("CALL", "BREAKOUT"),
                day_type_result=day_type_result,
                st_details=st_details,
            )

        assert "[DAY_BIAS_PENALTY]" in caplog.text
        assert "penalty=-15" in caplog.text

    def test_no_penalty_on_range_day(self, caplog):
        """No penalty on RANGE days regardless of direction."""
        dt_name = MagicMock()
        dt_name.value = "RANGE"
        day_type_result = MagicMock()
        day_type_result.name = dt_name

        candle = self._build_candle()
        indicators = self._build_indicators()
        st_details = {"bias": "Negative", "open_bias_context": {"gap_tag": "NONE"}}

        with caplog.at_level(logging.DEBUG):
            result = check_entry_condition(
                candle, indicators, bias_15m="BEARISH",
                pivot_signal=("CALL", "BREAKOUT"),
                day_type_result=day_type_result,
                st_details=st_details,
            )

        assert "[DAY_BIAS_PENALTY]" not in caplog.text


# ── Fix 4: MOMENTUM_ENTRY (signals.py) ───────────────────────────────────────

class TestMomentumEntry:
    """Fix 4: Momentum entry path when no pullback detected but extreme momentum."""

    def _build_3m_df(self, close=21800.0, bias="BEARISH", slope="DOWN", adx=35.0):
        """Build minimal 3m DataFrame for detect_signal with correct column names."""
        n = 20
        df = pd.DataFrame({
            "close": [close] * n,
            "high": [close + 100] * n,  # wide range to pass range_is_ok
            "low": [close - 100] * n,
            "open": [close + 10] * n,
            "volume": [1000] * n,
            "rsi14": [40.0] * n,  # avoid RSI oversold block
            "cci14": [-150.0] * n,
            "adx14": [adx] * n,
            "atr14": [100.0] * n,
            "ema9": [close + 5] * n,
            "ema13": [close + 15] * n,
            "ema26": [close + 30] * n,
            "vwap": [close] * n,  # VWAP at price to avoid bounce signals
            "macd": [-5.0] * n,
            "macd_signal": [-3.0] * n,
            "macd_hist": [-2.0] * n,
            "supertrend_bias": [bias] * n,  # correct column name
            "supertrend_slope": [slope] * n,
        })
        return df

    def _build_15m_df(self, close=21800.0, bias="BEARISH"):
        n = 5
        df = pd.DataFrame({
            "close": [close] * n,
            "high": [close + 50] * n,
            "low": [close - 50] * n,
            "open": [close + 10] * n,
            "volume": [5000] * n,
            "supertrend_bias": [bias] * n,  # correct column name
            "atr14": [150.0] * n,
        })
        return df

    @patch("signals._best_pivot_for_side", return_value=None)
    def test_momentum_entry_fires_on_extreme_stretch(self, mock_pivot, caplog):
        """With ATR stretch > 2.0, ADX > 30, bias+slope confirm → MOMENTUM_ENTRY fires."""
        candles_3m = self._build_3m_df(close=21800.0, bias="BEARISH", slope="DOWN", adx=35.0)
        candles_15m = self._build_15m_df(close=21800.0, bias="BEARISH")
        st_details = {"atr_stretch": 2.5, "bias": "Negative"}

        with caplog.at_level(logging.DEBUG):
            result = detect_signal(
                candles_3m, candles_15m,
                cpr_levels={"tc": 22000, "bc": 21900, "pivot": 21950},
                camarilla_levels={"r3": 22100, "r4": 22200, "s3": 21700, "s4": 21600},
                traditional_levels={"pivot": 22000, "r1": 22050, "r2": 22100, "r3": 22150, "s1": 21950, "s2": 21900, "s3": 21850},
                atr=100.0,
                st_details=st_details,
            )

        assert "[MOMENTUM_ENTRY]" in caplog.text

    @patch("signals._best_pivot_for_side", return_value=None)
    def test_no_momentum_entry_when_adx_low(self, mock_pivot, caplog):
        """With ADX <= 30, momentum entry should not fire."""
        candles_3m = self._build_3m_df(close=21800.0, bias="BEARISH", slope="DOWN", adx=20.0)
        candles_15m = self._build_15m_df(close=21800.0, bias="BEARISH")
        st_details = {"atr_stretch": 2.5, "bias": "Negative"}

        with caplog.at_level(logging.DEBUG):
            result = detect_signal(
                candles_3m, candles_15m,
                cpr_levels={"tc": 22000, "bc": 21900, "pivot": 21950},
                camarilla_levels={"r3": 22100, "r4": 22200, "s3": 21700, "s4": 21600},
                traditional_levels={"pivot": 22000, "r1": 22050, "r2": 22100, "r3": 22150, "s1": 21950, "s2": 21900, "s3": 21850},
                atr=100.0,
                st_details=st_details,
            )

        assert "[MOMENTUM_ENTRY]" not in caplog.text

    @patch("signals._best_pivot_for_side", return_value=None)
    def test_no_momentum_entry_when_stretch_low(self, mock_pivot, caplog):
        """With ATR stretch <= 2.0, momentum entry should not fire."""
        candles_3m = self._build_3m_df(close=21800.0, bias="BEARISH", slope="DOWN", adx=35.0)
        candles_15m = self._build_15m_df(close=21800.0, bias="BEARISH")
        st_details = {"atr_stretch": 1.5, "bias": "Negative"}

        with caplog.at_level(logging.DEBUG):
            result = detect_signal(
                candles_3m, candles_15m,
                cpr_levels={"tc": 22000, "bc": 21900, "pivot": 21950},
                camarilla_levels={"r3": 22100, "r4": 22200, "s3": 21700, "s4": 21600},
                traditional_levels={"pivot": 22000, "r1": 22050, "r2": 22100, "r3": 22150, "s1": 21950, "s2": 21900, "s3": 21850},
                atr=100.0,
                st_details=st_details,
            )

        assert "[MOMENTUM_ENTRY]" not in caplog.text


# ── Log Parser Integration ───────────────────────────────────────────────────

class TestLogParserPhase63:
    """Test that log_parser parses Phase 6.3 tags into SessionSummary fields."""

    def _make_log_lines(self):
        """Generate sample log lines with Phase 6.3 tags."""
        return [
            "2026-03-02 09:16:00 INFO [OSC_TREND_OVERRIDE] timestamp=09:16 symbol=NIFTY side=PUT day_type=TRENDING_DAY",
            "2026-03-02 09:16:01 INFO [OSC_TREND_OVERRIDE] timestamp=09:16 symbol=NIFTY side=PUT day_type=TRENDING_DAY",
            "2026-03-02 09:20:00 INFO [TREND_ALIGN_OVERRIDE] side=PUT trend_alignment=0->10 adx=32.0",
            "2026-03-02 09:25:00 INFO [DAY_BIAS_PENALTY] side=CALL penalty=-15 day_type=TRENDING",
            "2026-03-02 09:30:00 INFO [MOMENTUM_ENTRY] side=PUT atr_stretch=2.80 adx=35.0",
            "2026-03-02 09:30:01 INFO [MOMENTUM_ENTRY] side=PUT atr_stretch=3.10 adx=38.0",
            "2026-03-02 09:35:00 INFO [SIGNAL_SKIP] bar=50 timestamp=09:35 symbol=NIFTY",
            "2026-03-02 10:00:00 INFO [COOLDOWN_REDUCED] day_type=TRENDING_DAY cooldown=5 bars",
            "2026-03-02 10:00:01 INFO [COOLDOWN_REDUCED] day_type=TRENDING_DAY cooldown=3 bars",
            "2026-03-02 10:00:02 INFO [COOLDOWN_REDUCED] day_type=TRENDING_DAY cooldown=5 bars",
        ]

    def test_regex_patterns_match_tags(self):
        """Verify the 6 regex patterns match their respective log tags."""
        from log_parser import (
            _RE_OSC_TREND_OVERRIDE, _RE_TREND_ALIGN_OVERRIDE,
            _RE_DAY_BIAS_PENALTY, _RE_MOMENTUM_ENTRY,
            _RE_SIGNAL_SKIP, _RE_COOLDOWN_REDUCED,
        )
        lines = self._make_log_lines()
        assert _RE_OSC_TREND_OVERRIDE.search(lines[0])
        assert _RE_TREND_ALIGN_OVERRIDE.search(lines[2])
        assert _RE_DAY_BIAS_PENALTY.search(lines[3])
        assert _RE_MOMENTUM_ENTRY.search(lines[4])
        assert _RE_SIGNAL_SKIP.search(lines[6])
        assert _RE_COOLDOWN_REDUCED.search(lines[7])

    def test_session_summary_has_phase63_fields(self):
        """SessionSummary dataclass has all 6 new Phase 6.3 fields."""
        from log_parser import SessionSummary
        s = SessionSummary(log_path="test.log", session_type="replay", date_tag="2026-03-02")
        assert hasattr(s, "osc_trend_override_count")
        assert hasattr(s, "trend_align_override_count")
        assert hasattr(s, "day_bias_penalty_count")
        assert hasattr(s, "momentum_entry_count")
        assert hasattr(s, "signal_skip_count")
        assert hasattr(s, "cooldown_reduced_count")

    def test_to_dict_includes_phase63_fields(self):
        """to_dict() returns all 6 Phase 6.3 counter fields."""
        from log_parser import SessionSummary
        s = SessionSummary(
            log_path="test.log", session_type="replay", date_tag="2026-03-02",
            osc_trend_override_count=2,
            trend_align_override_count=1,
            day_bias_penalty_count=3,
            momentum_entry_count=1,
            signal_skip_count=4,
            cooldown_reduced_count=2,
        )
        d = s.to_dict()
        assert d["osc_trend_override_count"] == 2
        assert d["trend_align_override_count"] == 1
        assert d["day_bias_penalty_count"] == 3
        assert d["momentum_entry_count"] == 1
        assert d["signal_skip_count"] == 4
        assert d["cooldown_reduced_count"] == 2

    def test_default_values_are_zero(self):
        """All 6 Phase 6.3 fields default to 0."""
        from log_parser import SessionSummary
        s = SessionSummary(log_path="test.log", session_type="replay", date_tag="2026-03-02")
        assert s.osc_trend_override_count == 0
        assert s.trend_align_override_count == 0
        assert s.day_bias_penalty_count == 0
        assert s.momentum_entry_count == 0
        assert s.signal_skip_count == 0
        assert s.cooldown_reduced_count == 0


# ── Dashboard Rendering ──────────────────────────────────────────────────────

class TestDashboardPhase63:
    """Test that dashboard renders Phase 6.3 section when counters > 0."""

    def _make_session(self, **overrides):
        """Build a minimal SessionSummary-like object for dashboard."""
        from log_parser import SessionSummary
        defaults = dict(
            log_path="test.log", session_type="replay", date_tag="2026-03-02",
            osc_trend_override_count=2,
            trend_align_override_count=1,
            day_bias_penalty_count=1,
            momentum_entry_count=3,
            signal_skip_count=1,
            cooldown_reduced_count=2,
        )
        defaults.update(overrides)
        return SessionSummary(**defaults)

    def test_phase63_section_appears_in_report(self):
        """When Phase 6.3 counters > 0, dashboard text report includes the section."""
        from dashboard import _write_text_report
        from pathlib import Path
        import tempfile

        session = self._make_session()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            path = Path(f.name)

        try:
            _write_text_report(session, path)
            content = path.read_text()
            assert "REGIME-AWARE FIXES (Phase 6.3)" in content
            assert "OSC trend overrides" in content
            assert "Momentum entries" in content
            assert "Cooldown reductions" in content
        finally:
            path.unlink(missing_ok=True)

    def test_phase63_section_hidden_when_all_zero(self):
        """When all Phase 6.3 counters are 0, section should not appear."""
        from dashboard import _write_text_report
        from pathlib import Path
        import tempfile

        session = self._make_session(
            osc_trend_override_count=0, trend_align_override_count=0,
            day_bias_penalty_count=0, momentum_entry_count=0,
            signal_skip_count=0, cooldown_reduced_count=0,
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            path = Path(f.name)

        try:
            _write_text_report(session, path)
            content = path.read_text()
            assert "REGIME-AWARE FIXES (Phase 6.3)" not in content
        finally:
            path.unlink(missing_ok=True)


# ── Backward Compatibility ───────────────────────────────────────────────────

class TestBackwardCompatibility:
    """Ensure Phase 6.3 changes don't break prior behavior."""

    def test_check_entry_without_st_details(self):
        """check_entry_condition works without st_details (defaults to None)."""
        from entry_logic import check_entry_condition
        candle = {"close": 22000, "high": 22050, "low": 21950, "open": 21990}
        indicators = {
            "rsi14": 50.0, "cci14": 100.0, "adx14": 25.0,
            "ema9": 22000, "ema13": 21995, "ema26": 21980,
            "atr14": 100.0, "atr": 100.0, "vwap": 22000,
            "st_bias_3m": "BULLISH",
            "supertrend_3m": "BULLISH", "supertrend_15m": "BULLISH",
            "supertrend_slope": "UP",
            "macd": 5.0, "macd_signal": 3.0, "macd_hist": 2.0,
        }
        # Should not raise — st_details defaults to None
        result = check_entry_condition(candle, indicators, bias_15m="BULLISH",
                                       pivot_signal=("CALL", "BREAKOUT"))
        assert result is not None

    def test_detect_signal_without_st_details(self):
        """detect_signal works without st_details (defaults to None)."""
        from signals import detect_signal
        n = 20
        candles_3m = pd.DataFrame({
            "close": [22000.0] * n, "high": [22050.0] * n,
            "low": [21950.0] * n, "open": [21990.0] * n,
            "volume": [1000] * n,
            "rsi14": [50.0] * n, "cci14": [100.0] * n, "adx14": [25.0] * n,
            "atr14": [100.0] * n, "ema9": [22000.0] * n,
            "ema13": [21995.0] * n, "ema26": [21980.0] * n,
            "vwap": [22000.0] * n,
            "macd": [5.0] * n, "macd_signal": [3.0] * n, "macd_hist": [2.0] * n,
            "supertrend_3m": ["BULLISH"] * n, "supertrend_slope": ["UP"] * n,
        })
        candles_15m = pd.DataFrame({
            "close": [22000.0] * 5, "high": [22050.0] * 5,
            "low": [21950.0] * 5, "open": [21990.0] * 5,
            "volume": [5000] * 5,
            "supertrend_15m": ["BULLISH"] * 5, "atr14": [150.0] * 5,
        })
        # Should not raise — st_details defaults to None
        result = detect_signal(
            candles_3m, candles_15m,
            cpr_levels={"tc": 22000, "bc": 21900, "pivot": 21950},
            camarilla_levels={"r3": 22100, "r4": 22200, "s3": 21700, "s4": 21600},
            traditional_levels={"pivot": 22000, "r1": 22050, "r2": 22100, "r3": 22150, "s1": 21950, "s2": 21900, "s3": 21850},
            atr=100.0,
        )
        # Result can be None or a signal dict — either is fine

    def test_session_summary_backward_compat(self):
        """SessionSummary from log_parser still works with no Phase 6.3 args."""
        from log_parser import SessionSummary
        s = SessionSummary(log_path="test.log", session_type="replay", date_tag="2026-03-02")
        d = s.to_dict()
        # All Phase 6.3 fields should be 0 by default
        assert d["osc_trend_override_count"] == 0
        assert d["momentum_entry_count"] == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
