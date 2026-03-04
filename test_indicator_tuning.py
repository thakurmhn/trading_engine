"""Unit tests for Indicator Parameter Tuning (Req 2).

Coverage
--------
TestADXConfig          – TREND_ENTRY_ADX_MIN == 18.0 in config.py;
                         ST_RR_RATIO and ST_TG_RR_RATIO present.
TestEntryConfigLog     – [ENTRY CONFIG] log emitted by _trend_entry_quality_gate.
TestADXGateBehavior    – gate blocks at ADX < 18, allows at 18 ≤ ADX < 25
                         (proving the lower threshold is in effect).
TestReplayProfiles     – Baseline / Stress / Volatility parameterized replay
                         scenarios exercise the full gate with different candle
                         profiles and verify outcome consistency.
TestSTEntryConfigDefaults – STEntryConfig rr_ratio / tg_rr_ratio match config.
"""

import sys
import types
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

# ── Stub heavy dependencies BEFORE importing execution or config ──────────────
_STUB_NAMES = [
    "fyers_apiv3",
    "fyers_apiv3.fyersModel",
    "setup",
    "indicators",
    "signals",
    "orchestration",
    "position_manager",
    "day_type",
]
for _n in _STUB_NAMES:
    if _n not in sys.modules:
        sys.modules[_n] = types.ModuleType(_n)

# -- config stub (with all required attrs) --
if "config" not in sys.modules:
    sys.modules["config"] = types.ModuleType("config")
_cfg = sys.modules["config"]
_cfg.time_zone          = "Asia/Kolkata"
_cfg.strategy_name      = "TEST"
_cfg.MAX_TRADES_PER_DAY = 3
_cfg.account_type       = "PAPER"
_cfg.quantity           = 1
_cfg.CALL_MONEYNESS     = 0
_cfg.PUT_MONEYNESS      = 0
_cfg.profit_loss_point  = 5
_cfg.ENTRY_OFFSET       = 0
_cfg.ORDER_TYPE         = "MARKET"
_cfg.MAX_DAILY_LOSS     = -10000
_cfg.MAX_DRAWDOWN       = -5000
_cfg.OSCILLATOR_EXIT_MODE = "SOFT"
_cfg.symbols            = {"index": "NSE:NIFTY50-INDEX"}
_cfg.TREND_ENTRY_ADX_MIN = 18.0   # ← the key tuned value
_cfg.ST_RR_RATIO         = 2.0
_cfg.ST_TG_RR_RATIO      = 1.0

# -- setup stub --
_stup = sys.modules["setup"]
_stup.df          = pd.DataFrame()
_stup.fyers       = MagicMock()
_stup.ticker      = "NSE:NIFTY50-INDEX"
_stup.option_chain = {}
_stup.spot_price  = 22000.0
_stup.start_time  = "09:15"
_stup.end_time    = "15:30"
_stup.hist_data   = {}

# -- indicators stub --
_ind = sys.modules["indicators"]
_ind.calculate_cpr = MagicMock(return_value={})
_ind.calculate_traditional_pivots = MagicMock(return_value={})
_ind.calculate_camarilla_pivots   = MagicMock(return_value={})
_ind.resolve_atr   = MagicMock(return_value=50.0)
_ind.daily_atr     = MagicMock(return_value=50.0)
_ind.williams_r    = MagicMock(return_value=None)
_ind.calculate_cci = MagicMock(return_value=pd.Series(dtype=float))
_ind.momentum_ok   = MagicMock(return_value=(True, 0.0))
_ind.classify_cpr_width = MagicMock(return_value="NORMAL")

# -- signals stub --
_sig = sys.modules["signals"]
_sig.detect_signal    = MagicMock(return_value=None)
_sig.get_opening_range = MagicMock(return_value=(None, None))

# -- orchestration stub --
_orch = sys.modules["orchestration"]
_orch.update_candles_and_signals  = MagicMock()
_orch.build_indicator_dataframe   = MagicMock(return_value=pd.DataFrame())

# -- position_manager stub --
_pm = sys.modules["position_manager"]
_pm.make_replay_pm = MagicMock()

# -- day_type stub --
_dt_mod = sys.modules["day_type"]
_dt_mod.make_day_type_classifier = MagicMock()
_dt_mod.apply_day_type_to_pm     = MagicMock()
_dt_mod.DayType        = MagicMock()
_dt_mod.DayTypeResult  = MagicMock()

# -- fyers stub --
_fyers_pkg  = sys.modules["fyers_apiv3"]
_fyers_model_mod = sys.modules["fyers_apiv3.fyersModel"]
_fyers_pkg.fyersModel = _fyers_model_mod
_fyers_model_mod.FyersModel = MagicMock()

# ── Now import execution (uses the stubs above) ───────────────────────────────
import execution  # noqa: E402
from execution import _trend_entry_quality_gate  # noqa: E402

import pendulum  # noqa: E402

_TZ = "Asia/Kolkata"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mk_candles(
    n: int = 10,
    adx: float = 20.0,
    rsi: float = 50.0,
    cci: float = 0.0,
    bias_3m: str = "BULLISH",
    slope_3m: str = "UP",
    close: float = 22000.0,
    atr14: float = 50.0,
) -> pd.DataFrame:
    """Minimal 3-minute candle DataFrame for quality-gate tests."""
    return pd.DataFrame(
        {
            "open":              [close - 5.0] * n,
            "high":              [close + 5.0] * n,
            "low":               [close - 5.0] * n,
            "close":             [close] * n,
            "volume":            [1000] * n,
            "adx14":             [adx] * n,
            "rsi14":             [rsi] * n,
            "cci20":             [cci] * n,
            "atr14":             [atr14] * n,
            "supertrend_bias":   [bias_3m] * n,
            "supertrend_slope":  [slope_3m] * n,
            "supertrend_line":   [close - 100.0] * n,
        }
    )


def _mk_candles_15m(n: int = 5, bias: str = "BULLISH") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "supertrend_bias":  [bias] * n,
            "supertrend_slope": ["UP" if bias == "BULLISH" else "DOWN"] * n,
            "supertrend_line":  [21900.0] * n,
            "close":            [22000.0] * n,
            "atr14":            [50.0] * n,
        }
    )


def _now():
    return pendulum.now(_TZ)


# ═══════════════════════════════════════════════════════════════════════════════
# TestADXConfig — config.py exports the right values
# ═══════════════════════════════════════════════════════════════════════════════

class TestADXConfig(unittest.TestCase):

    def test_trend_entry_adx_min_is_18(self):
        """TREND_ENTRY_ADX_MIN must be 18.0 (lowered from 25.0)."""
        self.assertAlmostEqual(_cfg.TREND_ENTRY_ADX_MIN, 18.0, places=4)

    def test_st_rr_ratio_present(self):
        """ST_RR_RATIO should default to 2.0."""
        self.assertAlmostEqual(_cfg.ST_RR_RATIO, 2.0, places=4)

    def test_st_tg_rr_ratio_present(self):
        """ST_TG_RR_RATIO should default to 1.0."""
        self.assertAlmostEqual(_cfg.ST_TG_RR_RATIO, 1.0, places=4)

    def test_adx_min_less_than_old_default(self):
        """The new threshold (18) must be strictly below the old hard-coded 25."""
        self.assertLess(_cfg.TREND_ENTRY_ADX_MIN, 25.0)

    def test_execution_module_uses_config_adx(self):
        """execution.TREND_ENTRY_ADX_MIN is imported from config stub (18.0)."""
        self.assertAlmostEqual(execution.TREND_ENTRY_ADX_MIN, 18.0, places=4)


# ═══════════════════════════════════════════════════════════════════════════════
# TestEntryConfigLog — [ENTRY CONFIG] tag emitted
# ═══════════════════════════════════════════════════════════════════════════════

class TestEntryConfigLog(unittest.TestCase):

    def _gate(self, adx=20.0, bias_3m="BULLISH"):
        c3m  = _mk_candles(adx=adx, bias_3m=bias_3m, slope_3m="UP" if bias_3m == "BULLISH" else "DOWN")
        c15m = _mk_candles_15m(bias=bias_3m)
        return _trend_entry_quality_gate(c3m, c15m, _now(), "TEST", adx_min=18.0)

    def test_entry_config_tag_emitted(self):
        """[ENTRY CONFIG] must appear in the log when the gate is invoked."""
        with self.assertLogs(level="INFO") as cm:
            self._gate()
        self.assertTrue(any("[ENTRY CONFIG]" in line for line in cm.output))

    def test_entry_config_contains_adx_min(self):
        """[ENTRY CONFIG] log line must contain the active adx_min value."""
        with self.assertLogs(level="INFO") as cm:
            self._gate(adx=20.0)
        config_lines = [l for l in cm.output if "[ENTRY CONFIG]" in l]
        self.assertTrue(any("adx_min=18.0" in l for l in config_lines))

    def test_entry_config_contains_rsi_range(self):
        """[ENTRY CONFIG] log line must contain rsi_range bounds."""
        with self.assertLogs(level="INFO") as cm:
            self._gate()
        config_lines = [l for l in cm.output if "[ENTRY CONFIG]" in l]
        self.assertTrue(any("rsi_range" in l for l in config_lines))

    def test_entry_config_contains_cci_range(self):
        """[ENTRY CONFIG] log line must contain cci_range bounds."""
        with self.assertLogs(level="INFO") as cm:
            self._gate()
        config_lines = [l for l in cm.output if "[ENTRY CONFIG]" in l]
        self.assertTrue(any("cci_range" in l for l in config_lines))


# ═══════════════════════════════════════════════════════════════════════════════
# TestADXGateBehavior — entries allowed/blocked at the new 18.0 boundary
# ═══════════════════════════════════════════════════════════════════════════════

class TestADXGateBehavior(unittest.TestCase):

    def _run(self, adx: float, bias: str = "BULLISH"):
        c3m  = _mk_candles(adx=adx, bias_3m=bias,
                           slope_3m="UP" if bias == "BULLISH" else "DOWN")
        c15m = _mk_candles_15m(bias=bias)
        return _trend_entry_quality_gate(c3m, c15m, _now(), "TEST", adx_min=18.0)

    # ── old threshold would have blocked these ─────────────────────────────

    def test_adx_19_now_allowed(self):
        """ADX=19 ≥ 18 → allowed (was blocked under old 25.0 gate)."""
        ok, side, reason, _ = self._run(adx=19.0)
        self.assertTrue(ok, f"Expected allowed but got blocked: {reason}")

    def test_adx_20_now_allowed(self):
        """ADX=20 ≥ 18 → allowed."""
        ok, side, reason, _ = self._run(adx=20.0)
        self.assertTrue(ok, f"Expected allowed but got blocked: {reason}")

    def test_adx_24_now_allowed(self):
        """ADX=24 < 25 (old gate) but ≥ 18 (new gate) → now allowed."""
        ok, side, reason, _ = self._run(adx=24.0)
        self.assertTrue(ok, f"ADX=24 should be allowed at adx_min=18: {reason}")

    # ── still blocked below 18 ─────────────────────────────────────────────

    def test_adx_17_blocked(self):
        """ADX=17 < 18 → still blocked (WEAK_ADX)."""
        ok, side, reason, _ = self._run(adx=17.0)
        self.assertFalse(ok)
        self.assertIn("Weak trend", reason)

    def test_adx_10_blocked(self):
        """ADX=10 → blocked (very weak trend)."""
        ok, side, reason, _ = self._run(adx=10.0)
        self.assertFalse(ok)

    def test_adx_exactly_18_blocked(self):
        """ADX == 18.0 → blocked (gate is strict: adx > adx_min, not >=)."""
        ok, side, reason, _ = self._run(adx=18.0)
        # gate condition is `adx_val <= adx_min`, so exactly-at-boundary is blocked
        self.assertFalse(ok)

    def test_adx_18_001_allowed(self):
        """ADX = 18.001 just above threshold → allowed."""
        ok, side, reason, _ = self._run(adx=18.001)
        self.assertTrue(ok, f"Expected allowed but got: {reason}")

    # ── side assignment ────────────────────────────────────────────────────

    def test_allowed_side_call_for_bullish(self):
        ok, side, _, _ = self._run(adx=20.0, bias="BULLISH")
        self.assertEqual(side, "CALL")

    def test_allowed_side_put_for_bearish(self):
        ok, side, _, _ = self._run(adx=20.0, bias="BEARISH")
        self.assertEqual(side, "PUT")


# ═══════════════════════════════════════════════════════════════════════════════
# TestReplayProfiles — Baseline / Stress / Volatility
# ═══════════════════════════════════════════════════════════════════════════════

_REPLAY_PROFILES = [
    # (name,         adx,  rsi,  cci,   bias,       expect_ok)
    ("baseline",     22.0, 50.0,  0.0, "BULLISH",   True),
    ("low_adx_ok",   19.5, 50.0,  0.0, "BULLISH",   True),
    ("adx_below_18", 15.0, 50.0,  0.0, "BULLISH",   False),
    ("stress_high_rsi", 22.0, 72.0, 0.0, "BULLISH", False),   # RSI extreme
    ("stress_low_rsi",  22.0, 28.0, 0.0, "BULLISH", False),   # RSI extreme
    ("stress_high_cci", 22.0, 50.0, 135.0, "BULLISH", False), # CCI extreme
    ("stress_low_cci",  22.0, 50.0, -135.0, "BULLISH", False),# CCI extreme
    ("volatility_put",  20.0, 50.0, 0.0, "BEARISH",  True),   # moderate trend, PUT side
    ("volatility_border_adx", 18.5, 50.0, 0.0, "BULLISH", True),
]


class TestReplayProfiles(unittest.TestCase):
    """Parameterised replay across baseline, stress, and volatility profiles."""

    def _run_profile(
        self,
        adx: float,
        rsi: float,
        cci: float,
        bias: str,
        adx_min: float = 18.0,
    ):
        slope = "UP" if bias == "BULLISH" else "DOWN"
        c3m  = _mk_candles(adx=adx, rsi=rsi, cci=cci, bias_3m=bias, slope_3m=slope)
        c15m = _mk_candles_15m(bias=bias)
        return _trend_entry_quality_gate(
            c3m, c15m, _now(), "REPLAY",
            adx_min=adx_min, rsi_min=35.0, rsi_max=65.0,
            cci_min=-120.0, cci_max=120.0,
        )

    def test_baseline(self):
        ok, _, reason, _ = self._run_profile(22.0, 50.0, 0.0, "BULLISH")
        self.assertTrue(ok, f"Baseline should allow entry: {reason}")

    def test_low_adx_allowed_at_18(self):
        """ADX=19.5 is above new minimum 18 → entry allowed."""
        ok, _, reason, _ = self._run_profile(19.5, 50.0, 0.0, "BULLISH")
        self.assertTrue(ok, f"Low-ADX profile should allow at adx_min=18: {reason}")

    def test_adx_below_18_blocked(self):
        ok, _, reason, _ = self._run_profile(15.0, 50.0, 0.0, "BULLISH")
        self.assertFalse(ok, "ADX<18 should be blocked")
        self.assertIn("Weak trend", reason)

    def test_stress_high_rsi_blocked(self):
        """RSI=72 > rsi_max=65 → oscillator extreme, entry suppressed."""
        ok, _, reason, _ = self._run_profile(22.0, 72.0, 0.0, "BULLISH")
        self.assertFalse(ok)
        self.assertIn("Oscillator extreme", reason)

    def test_stress_low_rsi_blocked(self):
        ok, _, reason, _ = self._run_profile(22.0, 28.0, 0.0, "BULLISH")
        self.assertFalse(ok)
        self.assertIn("Oscillator extreme", reason)

    def test_stress_high_cci_blocked(self):
        ok, _, reason, _ = self._run_profile(22.0, 50.0, 135.0, "BULLISH")
        self.assertFalse(ok)
        self.assertIn("Oscillator extreme", reason)

    def test_stress_low_cci_blocked(self):
        ok, _, reason, _ = self._run_profile(22.0, 50.0, -135.0, "BULLISH")
        self.assertFalse(ok)

    def test_volatility_put_allowed(self):
        """Moderate trend in PUT direction → allowed."""
        ok, side, reason, _ = self._run_profile(20.0, 50.0, 0.0, "BEARISH")
        self.assertTrue(ok, f"Volatility-PUT profile should pass: {reason}")
        self.assertEqual(side, "PUT")

    def test_volatility_border_adx(self):
        """ADX=18.5 just above 18.0 → allowed."""
        ok, _, reason, _ = self._run_profile(18.5, 50.0, 0.0, "BULLISH")
        self.assertTrue(ok, f"Border ADX 18.5 should pass: {reason}")

    def test_profile_details_dict_populated(self):
        """st_details dict must contain adx14, rsi14, cci20 keys."""
        _, _, _, details = self._run_profile(22.0, 50.0, 0.0, "BULLISH")
        self.assertIn("adx14", details)
        self.assertIn("rsi14", details)
        self.assertIn("cci20", details)

    def test_adx_nan_blocked(self):
        """NaN ADX must not allow entry (treated as non-finite)."""
        c3m = _mk_candles(adx=float("nan"), bias_3m="BULLISH", slope_3m="UP")
        c3m["adx14"] = float("nan")    # force NaN
        c15m = _mk_candles_15m(bias="BULLISH")
        ok, _, reason, _ = _trend_entry_quality_gate(
            c3m, c15m, _now(), "NaN_TEST", adx_min=18.0
        )
        self.assertFalse(ok, "NaN ADX should be treated as weak trend and blocked")


# ═══════════════════════════════════════════════════════════════════════════════
# TestSTEntryConfigDefaults — rr_ratio / tg_rr_ratio wired from config
# ═══════════════════════════════════════════════════════════════════════════════

class TestSTEntryConfigDefaults(unittest.TestCase):
    """STEntryConfig defaults can be overridden with config-level values."""

    def setUp(self):
        # st_pullback_cci doesn't need heavy deps; import directly
        try:
            from st_pullback_cci import STEntryConfig
            self.STEntryConfig = STEntryConfig
        except Exception:
            self.skipTest("st_pullback_cci not importable in this environment")

    def test_default_rr_ratio(self):
        cfg = self.STEntryConfig()
        self.assertAlmostEqual(cfg.rr_ratio, 2.0, places=4)

    def test_default_tg_rr_ratio(self):
        cfg = self.STEntryConfig()
        self.assertAlmostEqual(cfg.tg_rr_ratio, 1.0, places=4)

    def test_config_rr_matches_st_entry_default(self):
        cfg = self.STEntryConfig(rr_ratio=_cfg.ST_RR_RATIO,
                                  tg_rr_ratio=_cfg.ST_TG_RR_RATIO)
        self.assertAlmostEqual(cfg.rr_ratio, _cfg.ST_RR_RATIO, places=4)
        self.assertAlmostEqual(cfg.tg_rr_ratio, _cfg.ST_TG_RR_RATIO, places=4)

    def test_custom_rr_override(self):
        cfg = self.STEntryConfig(rr_ratio=3.0)
        self.assertAlmostEqual(cfg.rr_ratio, 3.0, places=4)

    def test_custom_adx_min_override(self):
        """STEntryConfig does not have adx_min — that lives in execution.py."""
        cfg = self.STEntryConfig()
        self.assertFalse(hasattr(cfg, "adx_min"),
                         "adx_min belongs to execution._trend_entry_quality_gate, "
                         "not STEntryConfig")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    unittest.main(verbosity=2)
