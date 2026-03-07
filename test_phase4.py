"""Unit tests for Phase 4: Zone + Pulse integration and Regime-Adaptive Exits."""

import unittest
import logging

from entry_logic import _score_zone, _score_pulse, check_entry_condition


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 4A: Zone Scoring Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestScoreZone(unittest.TestCase):
    """Test _score_zone() — zone_detector output scoring."""

    def test_none_zone_returns_zero(self):
        self.assertEqual(_score_zone(None, "CALL"), 0)
        self.assertEqual(_score_zone(None, "PUT"), 0)

    def test_breakout_aligned_call(self):
        zone = {"zone_type": "SUPPLY", "action": "BREAKOUT", "side": "CALL"}
        self.assertEqual(_score_zone(zone, "CALL"), 10)

    def test_breakout_aligned_put(self):
        zone = {"zone_type": "DEMAND", "action": "BREAKOUT", "side": "PUT"}
        self.assertEqual(_score_zone(zone, "PUT"), 10)

    def test_reversal_aligned_call(self):
        zone = {"zone_type": "DEMAND", "action": "REVERSAL", "side": "CALL"}
        self.assertEqual(_score_zone(zone, "CALL"), 8)

    def test_reversal_aligned_put(self):
        zone = {"zone_type": "SUPPLY", "action": "REVERSAL", "side": "PUT"}
        self.assertEqual(_score_zone(zone, "PUT"), 8)

    def test_opposing_zone_suppresses(self):
        zone = {"zone_type": "SUPPLY", "action": "BREAKOUT", "side": "CALL"}
        self.assertEqual(_score_zone(zone, "PUT"), -5)

    def test_opposing_reversal_suppresses(self):
        zone = {"zone_type": "DEMAND", "action": "REVERSAL", "side": "CALL"}
        self.assertEqual(_score_zone(zone, "PUT"), -5)

    def test_no_side_in_zone_returns_zero(self):
        zone = {"zone_type": "DEMAND", "action": "BREAKOUT"}
        self.assertEqual(_score_zone(zone, "CALL"), 0)

    def test_empty_dict_returns_zero(self):
        self.assertEqual(_score_zone({}, "CALL"), 0)


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 4B: Pulse Scoring Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestScorePulse(unittest.TestCase):
    """Test _score_pulse() — pulse_module output scoring."""

    def test_none_pulse_returns_zero(self):
        self.assertEqual(_score_pulse(None, "CALL"), 0)
        self.assertEqual(_score_pulse(None, "PUT"), 0)

    def test_no_burst_returns_zero(self):
        pulse = {"burst_flag": False, "direction_drift": "UP", "tick_rate": 20.0}
        self.assertEqual(_score_pulse(pulse, "CALL"), 0)

    def test_burst_aligned_call_up(self):
        pulse = {"burst_flag": True, "direction_drift": "UP", "tick_rate": 20.0}
        self.assertEqual(_score_pulse(pulse, "CALL"), 8)

    def test_burst_aligned_put_down(self):
        pulse = {"burst_flag": True, "direction_drift": "DOWN", "tick_rate": 20.0}
        self.assertEqual(_score_pulse(pulse, "PUT"), 8)

    def test_burst_opposing_call_down(self):
        pulse = {"burst_flag": True, "direction_drift": "DOWN", "tick_rate": 20.0}
        self.assertEqual(_score_pulse(pulse, "CALL"), -5)

    def test_burst_opposing_put_up(self):
        pulse = {"burst_flag": True, "direction_drift": "UP", "tick_rate": 20.0}
        self.assertEqual(_score_pulse(pulse, "PUT"), -5)

    def test_burst_neutral_drift(self):
        pulse = {"burst_flag": True, "direction_drift": "NEUTRAL", "tick_rate": 20.0}
        self.assertEqual(_score_pulse(pulse, "CALL"), 3)
        self.assertEqual(_score_pulse(pulse, "PUT"), 3)

    def test_empty_dict_returns_zero(self):
        self.assertEqual(_score_pulse({}, "CALL"), 0)


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 4C: Zone + Pulse in check_entry_condition integration
# ═══════════════════════════════════════════════════════════════════════════════

class TestCheckEntryWithZonePulse(unittest.TestCase):
    """Test that zone_signal and pulse_metrics are wired into scoring."""

    def _make_candle(self, rsi=55.0, cci=120.0, close=22100.0):
        return {"rsi14": rsi, "cci20": cci, "close": close, "open": 22050.0,
                "high": 22150.0, "low": 22000.0}

    def _make_indicators(self, **overrides):
        base = {
            "atr": 80.0,
            "st_bias_3m": "BULLISH",
            "st_bias_15m": "BULLISH",
            "momentum_ok_call": True,
            "momentum_ok_put": False,
            "cpr_width": "NARROW",
            "adx14": 30.0,
            "open_bias": "NONE",
            "gap_tag": "NO_GAP",
            "rsi_prev": 52.0,
        }
        base.update(overrides)
        return base

    def test_zone_breakout_increases_score(self):
        """Zone breakout aligned with CALL should add +10 to score."""
        zone = {"zone_type": "SUPPLY", "action": "BREAKOUT", "side": "CALL"}
        result_with = check_entry_condition(
            candle=self._make_candle(),
            indicators=self._make_indicators(),
            bias_15m="BULLISH",
            zone_signal=zone,
        )
        result_without = check_entry_condition(
            candle=self._make_candle(),
            indicators=self._make_indicators(),
            bias_15m="BULLISH",
            zone_signal=None,
        )
        self.assertEqual(
            result_with["breakdown"].get("zone_score", 0) - result_without["breakdown"].get("zone_score", 0),
            10
        )

    def test_pulse_burst_aligned_increases_score(self):
        """Pulse burst aligned with CALL should add +8 to score."""
        pulse = {"burst_flag": True, "direction_drift": "UP", "tick_rate": 20.0}
        result_with = check_entry_condition(
            candle=self._make_candle(),
            indicators=self._make_indicators(),
            bias_15m="BULLISH",
            pulse_metrics=pulse,
        )
        result_without = check_entry_condition(
            candle=self._make_candle(),
            indicators=self._make_indicators(),
            bias_15m="BULLISH",
            pulse_metrics=None,
        )
        self.assertEqual(
            result_with["breakdown"].get("pulse_score", 0) - result_without["breakdown"].get("pulse_score", 0),
            8
        )

    def test_opposing_zone_decreases_score(self):
        """Zone favouring PUT should decrease CALL score by 5."""
        zone = {"zone_type": "DEMAND", "action": "BREAKOUT", "side": "PUT"}
        result = check_entry_condition(
            candle=self._make_candle(),
            indicators=self._make_indicators(),
            bias_15m="BULLISH",
            zone_signal=zone,
        )
        # CALL is the best side due to RSI>50 + BULLISH, zone opposes it
        if result["side"] == "CALL":
            self.assertEqual(result["breakdown"].get("zone_score", 0), -5)

    def test_none_zone_pulse_backward_compat(self):
        """Without zone/pulse params, scoring should work as before."""
        result = check_entry_condition(
            candle=self._make_candle(),
            indicators=self._make_indicators(),
            bias_15m="BULLISH",
        )
        self.assertEqual(result["breakdown"].get("zone_score", 0), 0)
        self.assertEqual(result["breakdown"].get("pulse_score", 0), 0)


# ═══════════════════════════════════════════════════════════════════════════════
# Regime-Adaptive Exits Tests
# ═══════════════════════════════════════════════════════════════════════════════

import sys
import types
from unittest.mock import MagicMock

import numpy as np
import pandas as pd

# Stub heavy deps for execution import — must happen BEFORE importing execution
_STUB_NAMES = [
    "fyers_apiv3", "fyers_apiv3.fyersModel",
    "config", "setup", "indicators", "signals",
    "orchestration", "position_manager", "day_type",
    "compression_detector", "reversal_detector",
    "failed_breakout_detector", "zone_detector", "pulse_module",
]
for _n in _STUB_NAMES:
    if _n not in sys.modules:
        sys.modules[_n] = types.ModuleType(_n)

_cfg = sys.modules["config"]
_cfg.time_zone = "Asia/Kolkata"
_cfg.strategy_name = "TEST"
_cfg.MAX_TRADES_PER_DAY = 3
_cfg.account_type = "PAPER"
_cfg.quantity = 1
_cfg.CALL_MONEYNESS = 0
_cfg.PUT_MONEYNESS = 0
_cfg.profit_loss_point = 5
_cfg.ENTRY_OFFSET = 0
_cfg.ORDER_TYPE = "MARKET"
_cfg.MAX_DAILY_LOSS = -10000
_cfg.MAX_DRAWDOWN = -5000
_cfg.OSCILLATOR_EXIT_MODE = "HARD"
_cfg.symbols = {"index": "NSE:NIFTY50-INDEX"}
_cfg.TREND_ENTRY_ADX_MIN = 18.0
_cfg.SLOPE_ADX_GATE = 20.0
_cfg.TIME_SLOPE_ADX_GATE = 25.0
_cfg.ST_RR_RATIO = 2.0
_cfg.ST_TG_RR_RATIO = 1.0
_cfg.strike_diff = 50
_cfg.DEFAULT_LOT_SIZE = 1

_stup = sys.modules["setup"]
_stup.df = pd.DataFrame()
_stup.fyers = MagicMock()
_stup.ticker = "NSE:NIFTY50-INDEX"
_stup.option_chain = pd.DataFrame(columns=["strike_price", "symbol", "option_type"])
_stup.spot_price = 22000.0
_stup.start_time = "09:15"
_stup.end_time = "15:30"
_stup.hist_data = {}

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

_sig = sys.modules["signals"]
_sig.detect_signal = MagicMock(return_value=None)
_sig.get_opening_range = MagicMock(return_value=(None, None))

_orch = sys.modules["orchestration"]
_orch.update_candles_and_signals = MagicMock()
_orch.build_indicator_dataframe = MagicMock(return_value=pd.DataFrame())

_pm = sys.modules["position_manager"]
_pm.make_replay_pm = MagicMock()

_dt_mod = sys.modules["day_type"]
_dt_mod.make_day_type_classifier = MagicMock()
_dt_mod.apply_day_type_to_pm = MagicMock()
_dt_mod.DayType = MagicMock()
_dt_mod.DayTypeResult = MagicMock()
_dt_mod.DayTypeClassifier = MagicMock()

_comp = sys.modules["compression_detector"]
_comp.CompressionState = MagicMock()

_rev = sys.modules["reversal_detector"]
_rev.detect_reversal = MagicMock(return_value=None)

_fbk = sys.modules["failed_breakout_detector"]
_fbk.detect_failed_breakout = MagicMock(return_value=None)

_zd = sys.modules["zone_detector"]
_zd.detect_zones = MagicMock(return_value=[])
_zd.load_zones = MagicMock(return_value=[])
_zd.save_zones = MagicMock()
_zd.update_zone_activity = MagicMock()
_zd.detect_zone_revisit = MagicMock(return_value=None)

_pm_mod = sys.modules["pulse_module"]
_pm_mod.get_pulse_module = MagicMock()
_pm_mod.PulseModule = MagicMock()

_fyers_pkg = sys.modules["fyers_apiv3"]
_fyers_model_mod = sys.modules["fyers_apiv3.fyersModel"]
_fyers_pkg.fyersModel = _fyers_model_mod
_fyers_model_mod.FyersModel = MagicMock()

# Now import execution
import execution  # noqa: E402

from regime_context import RegimeContext


def _base_df(n=10, close=100.0, rsi=55.0, cci=50.0, adx=25.0):
    """Create a minimal DataFrame for check_exit_condition."""
    df = pd.DataFrame({
        "close": [close] * n,
        "open": [close - 1] * n,
        "high": [close + 2] * n,
        "low": [close - 2] * n,
        "rsi14": [rsi] * n,
        "cci20": [cci] * n,
        "adx14": [adx] * n,
    })
    return df


def _base_state(side="CALL", entry_candle=0, buy_price=100.0, **overrides):
    """Create a base state dict for exit testing."""
    state = {
        "side": side,
        "position_side": "LONG",
        "option_name": "TEST_OPT",
        "position_id": "POS1",
        "buy_price": buy_price,
        "entry_candle": entry_candle,
        "is_open": True,
        "trade_class": "TREND",
        "quantity": 1,
        "trail_step": 5,
        "time_exit_candles": 8,
        "atr_value": 80.0,
    }
    state.update(overrides)
    return state


class TestRegimeAdaptiveMinHold(unittest.TestCase):
    """Test that day_type adjusts min_hold via RegimeContext."""

    def test_trend_day_increases_min_hold(self):
        """TREND_DAY should increase min_hold — deferred PT exit at bars_held=2."""
        rc = RegimeContext(day_type="TREND_DAY", adx_tier="ADX_DEFAULT", gap_tag="NO_GAP",
                           atr_value=80.0, atr_regime="LOW")
        # ATR=80 → base min_bars=3, TREND_DAY +1 → min=4
        state = _base_state(entry_candle=0, buy_price=100.0, pt=110.0, tg=120.0,
                            entry_regime_context=rc)
        df = _base_df(n=4, close=115.0)  # bars_held=3, < min of 4
        exited, reason = execution.check_exit_condition(df, state, option_price=115.0)
        # Should defer because bars_held=3 < 4
        self.assertFalse(exited)

    def test_range_day_decreases_min_hold(self):
        """RANGE_DAY should decrease min_hold — quicker PT booking."""
        rc = RegimeContext(day_type="RANGE_DAY", adx_tier="ADX_DEFAULT", gap_tag="NO_GAP",
                           atr_value=80.0, atr_regime="LOW")
        # ATR=80 → base min_bars=3, RANGE_DAY -1 → min=2
        state = _base_state(entry_candle=0, buy_price=100.0, tg=110.0,
                            entry_regime_context=rc)
        df = _base_df(n=3, close=115.0)  # bars_held=2, >= min of 2
        exited, reason = execution.check_exit_condition(df, state, option_price=115.0)
        self.assertTrue(exited)
        self.assertIn("TG", reason)


class TestRegimeAdaptiveTrailStep(unittest.TestCase):
    """Test that ADX tier adjusts trail_step."""

    def test_strong_adx_widens_trail(self):
        """ADX_STRONG_40 should use trail_step >= 8."""
        rc = RegimeContext(adx_tier="ADX_STRONG_40", day_type="UNKNOWN", gap_tag="NO_GAP",
                           atr_value=80.0, atr_regime="LOW")
        state = _base_state(entry_candle=0, buy_price=100.0, trail_step=5,
                            entry_regime_context=rc)
        df = _base_df(n=6, close=115.0)  # bars_held=5, pnl=15 triggers trail
        execution.check_exit_condition(df, state, option_price=115.0)
        # Trail should have been computed with trail_step=8 (max(5,8))
        # The new stop should be close - 8 = 115 - 8 = 107
        self.assertAlmostEqual(state.get("stop", 0), 107.0, places=1)

    def test_weak_adx_tightens_trail(self):
        """ADX_WEAK_20 should tighten trail_step."""
        rc = RegimeContext(adx_tier="ADX_WEAK_20", day_type="UNKNOWN", gap_tag="NO_GAP",
                           atr_value=80.0, atr_regime="LOW")
        state = _base_state(entry_candle=0, buy_price=100.0, trail_step=5,
                            entry_regime_context=rc)
        df = _base_df(n=6, close=115.0)  # bars_held=5
        execution.check_exit_condition(df, state, option_price=115.0)
        # Trail step = max(1, 5-2) = 3; new stop = 115 - 3 = 112
        self.assertAlmostEqual(state.get("stop", 0), 112.0, places=1)


class TestRegimeAdaptiveGapDay(unittest.TestCase):
    """Test that gap days suppress premature oscillator exits."""

    def test_gap_day_requires_3_osc_hits(self):
        """On GAP_UP day with HARD osc mode, 2 osc_hits should NOT exit."""
        rc = RegimeContext(gap_tag="GAP_UP", day_type="GAP_DAY", adx_tier="ADX_DEFAULT",
                           atr_value=80.0, atr_regime="LOW")
        state = _base_state(entry_candle=0, buy_price=100.0, trail_step=5,
                            entry_regime_context=rc, osc_rsi_call=70.0, osc_cci_call=100.0)
        # Create df with RSI=80 and CCI=150 — 2 osc hits (RSI + CCI)
        df = _base_df(n=6, close=105.0, rsi=80.0, cci=150.0)
        exited, reason = execution.check_exit_condition(df, state, option_price=105.0)
        # With gap day suppression: 2 hits < 3 threshold, should NOT exit on OSC
        self.assertNotEqual(reason, "OSC_EXHAUSTION")

    def test_no_gap_day_exits_on_2_osc_hits(self):
        """On normal day with HARD osc mode, 2 osc_hits should exit."""
        rc = RegimeContext(gap_tag="NO_GAP", day_type="NEUTRAL_DAY", adx_tier="ADX_DEFAULT",
                           atr_value=80.0, atr_regime="LOW")
        state = _base_state(entry_candle=0, buy_price=100.0, trail_step=5,
                            entry_regime_context=rc, osc_rsi_call=70.0, osc_cci_call=100.0)
        df = _base_df(n=6, close=105.0, rsi=80.0, cci=150.0)
        exited, reason = execution.check_exit_condition(df, state, option_price=105.0)
        self.assertTrue(exited)
        self.assertEqual(reason, "OSC_EXHAUSTION")


class TestRegimeAdaptiveTimeExit(unittest.TestCase):
    """Test that ADX tier adjusts time_exit_candles."""

    def test_strong_adx_extends_time_exit(self):
        """ADX_STRONG_40 should extend time_exit by +4 candles."""
        rc = RegimeContext(adx_tier="ADX_STRONG_40", day_type="UNKNOWN", gap_tag="NO_GAP",
                           atr_value=80.0, atr_regime="LOW")
        state = _base_state(entry_candle=0, buy_price=100.0, time_exit_candles=8,
                            entry_regime_context=rc)
        # bars_held=9, which >= 8 but < 12 (8+4)
        df = _base_df(n=10, close=100.0)  # no trail updates
        exited, reason = execution.check_exit_condition(df, state, option_price=100.0)
        # Should NOT time-exit because time_exit_candles is now 12
        self.assertNotEqual(reason, "TIME_EXIT")

    def test_weak_adx_shortens_time_exit(self):
        """ADX_WEAK_20 should shorten time_exit by -3 candles."""
        rc = RegimeContext(adx_tier="ADX_WEAK_20", day_type="UNKNOWN", gap_tag="NO_GAP",
                           atr_value=80.0, atr_regime="LOW")
        state = _base_state(entry_candle=0, buy_price=100.0, time_exit_candles=8,
                            entry_regime_context=rc)
        # bars_held=5, which >= 5 (8-3)
        df = _base_df(n=6, close=100.0)  # no trail updates
        exited, reason = execution.check_exit_condition(df, state, option_price=100.0)
        self.assertTrue(exited)
        self.assertEqual(reason, "TIME_EXIT")


class TestRegimeAdaptiveBackwardCompat(unittest.TestCase):
    """Test that legacy state dicts (no entry_regime_context) still work."""

    def test_no_regime_context_works(self):
        """Without entry_regime_context, exits should use defaults."""
        state = _base_state(entry_candle=0, buy_price=100.0, stop=90.0)
        df = _base_df(n=6, close=85.0)
        exited, reason = execution.check_exit_condition(df, state, option_price=85.0)
        self.assertTrue(exited)
        self.assertEqual(reason, "SL_HIT")

    def test_state_with_day_type_string(self):
        """Legacy state with day_type string (no RC) should apply adaptations."""
        state = _base_state(entry_candle=0, buy_price=100.0, time_exit_candles=8,
                            day_type="TREND_DAY", adx_tier="ADX_DEFAULT")
        df = _base_df(n=10, close=100.0)
        # bars_held=9, >= 8 default → would time-exit
        # TREND_DAY adds +1 to min_hold but doesn't change time_exit
        exited, reason = execution.check_exit_condition(df, state, option_price=100.0)
        self.assertTrue(exited)
        self.assertEqual(reason, "TIME_EXIT")


class TestRegimeAdaptiveAuditLogs(unittest.TestCase):
    """Test that [EXIT AUDIT] logs include regime-adaptive parameters."""

    def test_audit_includes_regime_params(self):
        """Exit audit should show day type, ADX tier, and adaptive params."""
        rc = RegimeContext(day_type="TREND_DAY", adx_tier="ADX_STRONG_40", gap_tag="GAP_UP",
                           atr_value=120.0, atr_regime="MODERATE")
        state = _base_state(entry_candle=0, buy_price=100.0, stop=90.0,
                            entry_regime_context=rc)
        df = _base_df(n=6, close=85.0)
        with self.assertLogs("root", level="INFO") as cm:
            execution.check_exit_condition(df, state, option_price=85.0)
        audit_lines = [l for l in cm.output if "[EXIT AUDIT]" in l]
        self.assertTrue(len(audit_lines) > 0)
        # Check that regime-adaptive fields appear in audit
        audit_text = " ".join(audit_lines)
        self.assertIn("day=TREND_DAY", audit_text)
        self.assertIn("adx=ADX_STRONG_40", audit_text)
        self.assertIn("gap=GAP_UP", audit_text)

    def test_regime_adaptive_log_emitted(self):
        """[EXIT AUDIT][REGIME_ADAPTIVE] should be logged once per trade."""
        rc = RegimeContext(day_type="RANGE_DAY", adx_tier="ADX_WEAK_20", gap_tag="NO_GAP",
                           atr_value=80.0, atr_regime="LOW")
        state = _base_state(entry_candle=0, buy_price=100.0, stop=90.0,
                            entry_regime_context=rc)
        df = _base_df(n=6, close=85.0)
        with self.assertLogs("root", level="INFO") as cm:
            execution.check_exit_condition(df, state, option_price=85.0)
        adaptive_lines = [l for l in cm.output if "[REGIME_ADAPTIVE]" in l]
        self.assertTrue(len(adaptive_lines) >= 1)
        self.assertIn("day_type=RANGE_DAY", adaptive_lines[0])
        self.assertIn("adx_tier=ADX_WEAK_20", adaptive_lines[0])


if __name__ == "__main__":
    unittest.main()
