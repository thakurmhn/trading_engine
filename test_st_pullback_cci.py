# ===== test_st_pullback_cci.py =====
"""
Unit tests for the Supertrend Pullback + CCI Rejection entry module.

Coverage
--------
T01 — Bias conflict: 15m BULLISH vs 3m BEARISH → ST_CONFLICT, NONE
T02 — Bias conflict: 15m BEARISH vs 3m BULLISH → ST_CONFLICT, NONE
T03 — Neutral bias on 15m → ST_CONFLICT, NONE
T04 — Slope conflict: biases aligned BEARISH, but 3m slope UP → ST_SLOPE_CONFLICT, NONE
T05 — Slope conflict: biases aligned BULLISH, but 3m slope DOWN → ST_SLOPE_CONFLICT, NONE
T06 — Gate pass + no trigger → NO_TRIGGER, NONE
T07 — CCI rejection, PUT: bias aligned BEARISH + slope DOWN + CCI >= +100 → SELL
T08 — CCI rejection, CALL: bias aligned BULLISH + slope UP  + CCI <= -100 → BUY
T09 — Pullback trigger, PUT: price pullback toward ST line then rejection → SELL
T10 — Pullback trigger, CALL: price pullback toward ST line then rejection → BUY
T11 — SL for PUT placed above ST line
T12 — SL for CALL placed below ST line
T13 — Profit target (PT) uses configured RR ratio
T14 — Tracker resets on bias flip
T15 — Pullback does not fire if price was never "away"
T16 — signal_side() returns 'BUY', 'SELL', or 'NONE'
T17 — Empty df_3m returns None without crashing
T18 — Empty df_15m returns None without crashing
T19 — TG (trailing goal) uses conservative tg_rr_ratio; TG < PT for PUT, TG > entry for CALL
T20 — Signal dict always contains 'tg' key when signal is produced
T21 — BrokerAdapter subclass: place_entry dispatches to adapter.place_entry()
T22 — BrokerAdapter subclass: place_exit dispatches to adapter.place_exit()
T23 — BrokerAdapter: adapter failure returns (False, None) correctly
T24 — Legacy callable broker_fn still works after BrokerAdapter changes

Run with:  python -m pytest test_st_pullback_cci.py -v
"""

from __future__ import annotations

import logging
import unittest

import numpy as np
import pandas as pd

from st_pullback_cci import (
    STEntryConfig,
    PullbackTracker,
    BrokerAdapter,
    check_entry_signal,
    compute_stop_loss,
    compute_profit_target,
    compute_trailing_target,
    signal_side,
    place_st_pullback_entry,
    place_st_pullback_exit,
)

# ---------------------------------------------------------------------------
# Silence module-level log output during tests
# ---------------------------------------------------------------------------
logging.disable(logging.CRITICAL)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_N = 20  # number of candles in synthetic frames


def _mk_df(
    bias: str,
    slope: str,
    close: float = 100.0,
    st_line: float = 110.0,
    cci14: float = 0.0,
    high: float | None = None,
    low: float | None = None,
    atr14: float = 20.0,
    n: int = _N,
) -> pd.DataFrame:
    """Build a synthetic candle DataFrame with pre-computed indicator columns."""
    if high is None:
        high = close + 2.0
    if low is None:
        low = close - 2.0
    idx = pd.date_range("2024-01-15 09:15", periods=n, freq="3min")
    df = pd.DataFrame(
        {
            "open":             [close - 0.5] * n,
            "high":             [high] * n,
            "low":              [low] * n,
            "close":            [close] * n,
            "volume":           [1000] * n,
            "supertrend_bias":  [bias] * n,
            "supertrend_slope": [slope] * n,
            "supertrend_line":  [st_line] * n,
            "cci14":            [cci14] * n,
            "atr14":            [atr14] * n,
        },
        index=idx,
    )
    return df


def _mk_15m(bias: str, st_line: float = 110.0, n: int = 10) -> pd.DataFrame:
    """Minimal 15m DataFrame."""
    idx = pd.date_range("2024-01-15 09:15", periods=n, freq="15min")
    return pd.DataFrame(
        {
            "open":             [100.0] * n,
            "high":             [102.0] * n,
            "low":              [98.0] * n,
            "close":            [100.0] * n,
            "supertrend_bias":  [bias] * n,
            "supertrend_slope": ["DOWN" if bias in ("BEARISH","DOWN") else "UP"] * n,
            "supertrend_line":  [st_line] * n,
            "atr14":            [20.0] * n,
        },
        index=idx,
    )


def _default_config(**kwargs) -> STEntryConfig:
    """STEntryConfig with log_diagnostics disabled to keep test output clean."""
    return STEntryConfig(log_diagnostics=False, **kwargs)


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestBiasConflict(unittest.TestCase):
    """T01–T03: 15m vs 3m bias conflicts must block entry with ST_CONFLICT."""

    def _assert_blocked(self, df3, df15, label: str):
        sig = check_entry_signal(df3, df15, symbol=label, config=_default_config())
        self.assertIsNone(sig, f"Expected None for {label}, got {sig}")

    def test_t01_15m_bullish_3m_bearish(self):
        df3  = _mk_df("BEARISH", "DOWN", close=100.0, st_line=110.0)
        df15 = _mk_15m("BULLISH")
        self._assert_blocked(df3, df15, "T01")

    def test_t02_15m_bearish_3m_bullish(self):
        df3  = _mk_df("BULLISH", "UP", close=115.0, st_line=110.0)
        df15 = _mk_15m("BEARISH")
        self._assert_blocked(df3, df15, "T02")

    def test_t03_15m_neutral(self):
        df3  = _mk_df("BEARISH", "DOWN", close=100.0, st_line=110.0)
        df15 = _mk_15m("NEUTRAL")
        self._assert_blocked(df3, df15, "T03")


class TestSlopeConflict(unittest.TestCase):
    """T04–T05: bias aligned but 3m slope wrong → ST_SLOPE_CONFLICT."""

    def test_t04_bearish_slope_up(self):
        """Both biases BEARISH but slope UP → blocked."""
        df3  = _mk_df("BEARISH", "UP", close=100.0, st_line=110.0)
        df15 = _mk_15m("BEARISH")
        sig  = check_entry_signal(df3, df15, symbol="T04", config=_default_config())
        self.assertIsNone(sig)

    def test_t05_bullish_slope_down(self):
        """Both biases BULLISH but slope DOWN → blocked."""
        df3  = _mk_df("BULLISH", "DOWN", close=115.0, st_line=110.0)
        df15 = _mk_15m("BULLISH")
        sig  = check_entry_signal(df3, df15, symbol="T05", config=_default_config())
        self.assertIsNone(sig)


class TestNoTrigger(unittest.TestCase):
    """T06: gate passes but neither trigger is active → NONE."""

    def test_t06_gate_ok_no_trigger(self):
        # BEARISH trend, price far from ST (away_mult satisfied) but not yet approaching
        # CCI is neutral (0.0)
        df3  = _mk_df("BEARISH", "DOWN", close=80.0, st_line=110.0, cci14=0.0, atr14=20.0)
        df15 = _mk_15m("BEARISH")
        # away_atr_mult=0.5 → need |st - close|=30 > 0.5*20=10 → was_away triggers on first call
        # but approaching has never been set, so pullback won't fire; CCI=0 won't fire
        tracker = PullbackTracker(away_atr_mult=0.5, touch_atr_mult=0.25)
        sig = check_entry_signal(
            df3, df15, symbol="T06",
            config=_default_config(), tracker=tracker
        )
        self.assertIsNone(sig)


class TestCCIRejection(unittest.TestCase):
    """T07–T08: CCI trigger fires when threshold crossed with aligned biases."""

    def test_t07_cci_rejection_put(self):
        """BEARISH bias aligned + CCI >= +100 → SELL signal, CCI_REJECTION trigger."""
        df3  = _mk_df(
            "BEARISH", "DOWN",
            close=100.0, st_line=110.0, cci14=105.0, atr14=20.0,
        )
        df15 = _mk_15m("BEARISH")
        config = _default_config(cci_put_thresh=100.0)
        sig = check_entry_signal(df3, df15, symbol="T07", config=config)
        self.assertIsNotNone(sig)
        self.assertEqual(sig["side"], "SELL")
        self.assertEqual(sig["option_type"], "PUT")
        self.assertEqual(sig["trigger"], "CCI_REJECTION")

    def test_t08_cci_rejection_call(self):
        """BULLISH bias aligned + CCI <= -100 → BUY signal, CCI_REJECTION trigger."""
        df3  = _mk_df(
            "BULLISH", "UP",
            close=115.0, st_line=110.0, cci14=-105.0, atr14=20.0,
        )
        df15 = _mk_15m("BULLISH", st_line=110.0)
        config = _default_config(cci_call_thresh=-100.0)
        sig = check_entry_signal(df3, df15, symbol="T08", config=config)
        self.assertIsNotNone(sig)
        self.assertEqual(sig["side"], "BUY")
        self.assertEqual(sig["option_type"], "CALL")
        self.assertEqual(sig["trigger"], "CCI_REJECTION")


class TestPullbackTrigger(unittest.TestCase):
    """T09–T10: Pullback state machine fires correctly.

    Sequence to trigger:
        Step 1 — candle far from ST_line → was_away = True
        Step 2 — candle approaches ST_line (within touch_atr_mult) → approaching = True
        Step 3 — candle wick touches ST_line and close confirms trend → PULLBACK
    """

    # ---- PUT (BEARISH) ----

    def test_t09_pullback_put(self):
        """Price moves away, approaches, then is rejected at ST line → SELL PULLBACK."""
        atr   = 20.0
        st    = 110.0   # ST line above price (BEARISH)
        config = _default_config(away_atr_mult=0.5, touch_atr_mult=0.5, cci_put_thresh=200.0)
        tracker = PullbackTracker(away_atr_mult=0.5, touch_atr_mult=0.5)
        df15 = _mk_15m("BEARISH", st_line=st)

        # Step 1: price far below ST (dist=30 > 0.5*20=10) → was_away
        df3 = _mk_df("BEARISH", "DOWN", close=80.0, high=81.0, low=79.0,
                     st_line=st, cci14=0.0, atr14=atr)
        sig = check_entry_signal(df3, df15, symbol="T09-step1", config=config, tracker=tracker)
        self.assertIsNone(sig)
        self.assertTrue(tracker._was_away)

        # Step 2: price approaches (dist=5 <= 0.5*20=10, dist>=0) → approaching
        df3 = _mk_df("BEARISH", "DOWN", close=105.0, high=106.0, low=104.0,
                     st_line=st, cci14=0.0, atr14=atr)
        sig = check_entry_signal(df3, df15, symbol="T09-step2", config=config, tracker=tracker)
        self.assertIsNone(sig)
        self.assertTrue(tracker._approaching)

        # Step 3: wick touches ST line (high=111 >= 110-0.1*20=108) and close still below
        df3 = _mk_df("BEARISH", "DOWN", close=107.0, high=111.0, low=106.5,
                     st_line=st, cci14=0.0, atr14=atr)
        sig = check_entry_signal(df3, df15, symbol="T09-step3", config=config, tracker=tracker)
        self.assertIsNotNone(sig, "Expected PULLBACK trigger for PUT")
        self.assertEqual(sig["side"], "SELL")
        self.assertEqual(sig["option_type"], "PUT")
        self.assertEqual(sig["trigger"], "PULLBACK")

    # ---- CALL (BULLISH) ----

    def test_t10_pullback_call(self):
        """Price moves away above, drops back, wick touches ST line → BUY PULLBACK."""
        atr   = 20.0
        st    = 100.0   # ST line below price (BULLISH)
        config = _default_config(away_atr_mult=0.5, touch_atr_mult=0.5, cci_call_thresh=-200.0)
        tracker = PullbackTracker(away_atr_mult=0.5, touch_atr_mult=0.5)
        df15 = _mk_15m("BULLISH", st_line=st)

        # Step 1: price far above ST (dist=30 > 0.5*20=10) → was_away
        df3 = _mk_df("BULLISH", "UP", close=130.0, high=131.0, low=129.0,
                     st_line=st, cci14=0.0, atr14=atr)
        sig = check_entry_signal(df3, df15, symbol="T10-step1", config=config, tracker=tracker)
        self.assertIsNone(sig)
        self.assertTrue(tracker._was_away)

        # Step 2: price drops back toward ST (dist=7 <= 0.5*20=10) → approaching
        df3 = _mk_df("BULLISH", "UP", close=107.0, high=108.0, low=106.0,
                     st_line=st, cci14=0.0, atr14=atr)
        sig = check_entry_signal(df3, df15, symbol="T10-step2", config=config, tracker=tracker)
        self.assertIsNone(sig)
        self.assertTrue(tracker._approaching)

        # Step 3: wick touches ST line (low=99 <= 100+0.1*20=102) and close above
        df3 = _mk_df("BULLISH", "UP", close=103.0, high=104.0, low=99.0,
                     st_line=st, cci14=0.0, atr14=atr)
        sig = check_entry_signal(df3, df15, symbol="T10-step3", config=config, tracker=tracker)
        self.assertIsNotNone(sig, "Expected PULLBACK trigger for CALL")
        self.assertEqual(sig["side"], "BUY")
        self.assertEqual(sig["option_type"], "CALL")
        self.assertEqual(sig["trigger"], "PULLBACK")


class TestRiskManagement(unittest.TestCase):
    """T11–T13: Stop loss and profit target computation."""

    def test_t11_sl_put_above_st_line(self):
        """PUT SL must be above ST line: sl = st_line + buffer_mult * ATR."""
        sl = compute_stop_loss(
            entry_price=100.0, st_line=110.0, side="PUT", atr=20.0, buffer_mult=0.25
        )
        self.assertAlmostEqual(sl, 110.0 + 0.25 * 20.0, places=2)
        self.assertGreater(sl, 110.0)

    def test_t12_sl_call_below_st_line(self):
        """CALL SL must be below ST line: sl = st_line - buffer_mult * ATR."""
        sl = compute_stop_loss(
            entry_price=115.0, st_line=110.0, side="CALL", atr=20.0, buffer_mult=0.25
        )
        self.assertAlmostEqual(sl, 110.0 - 0.25 * 20.0, places=2)
        self.assertLess(sl, 110.0)

    def test_t13_pt_rr_ratio(self):
        """PT should reflect the configured RR ratio."""
        entry = 100.0
        sl    = 115.0  # PUT: SL above entry
        rr    = 2.0
        pt = compute_profit_target(entry_price=entry, sl=sl, rr_ratio=rr)
        risk = abs(entry - sl)                # 15.0
        expected = entry - risk * rr          # 100 - 30 = 70
        self.assertAlmostEqual(pt, expected, places=2)

    def test_t13b_pt_rr_call(self):
        entry = 115.0
        sl    = 105.0   # CALL: SL below entry
        rr    = 1.5
        pt = compute_profit_target(entry_price=entry, sl=sl, rr_ratio=rr)
        risk = abs(entry - sl)               # 10.0
        expected = entry + risk * rr         # 115 + 15 = 130
        self.assertAlmostEqual(pt, expected, places=2)


class TestTrackerBehavior(unittest.TestCase):
    """T14–T15: PullbackTracker edge cases."""

    def test_t14_tracker_resets_on_bias_flip(self):
        tracker = PullbackTracker()
        tracker._was_away  = True
        tracker._approaching = True
        tracker._last_bias = "BEARISH"
        # Flip to BULLISH
        tracker.reset_on_bias_change("BULLISH")
        self.assertFalse(tracker._was_away)
        self.assertFalse(tracker._approaching)
        self.assertEqual(tracker._last_bias, "BULLISH")

    def test_t15_pullback_no_fire_if_never_away(self):
        """Approaching directly (was_away=False) must not fire."""
        tracker = PullbackTracker(away_atr_mult=0.5, touch_atr_mult=0.5)
        atr   = 20.0
        st    = 110.0
        # Price is already near ST from the start (never went away)
        result = tracker.update(
            close=107.0, high=111.0, low=106.5,
            st_line=st, atr=atr, side="PUT"
        )
        self.assertIsNone(result)

    def test_t15b_pullback_resets_after_trigger(self):
        """After PULLBACK trigger fires, tracker state is cleared."""
        tracker = PullbackTracker(away_atr_mult=0.5, touch_atr_mult=0.5)
        atr = 20.0
        st  = 110.0
        # Manually set state
        tracker._was_away  = True
        tracker._approaching = True
        result = tracker.update(
            close=107.0, high=111.0, low=106.5,
            st_line=st, atr=atr, side="PUT"
        )
        self.assertEqual(result, "PULLBACK")
        self.assertFalse(tracker._was_away)
        self.assertFalse(tracker._approaching)


class TestSignalSide(unittest.TestCase):
    """T16: signal_side() returns the correct plain string."""

    def test_t16_sell(self):
        df3  = _mk_df("BEARISH", "DOWN", close=100.0, st_line=110.0, cci14=110.0, atr14=20.0)
        df15 = _mk_15m("BEARISH")
        result = signal_side(df3, df15, config=_default_config())
        self.assertEqual(result, "SELL")

    def test_t16_buy(self):
        df3  = _mk_df("BULLISH", "UP", close=115.0, st_line=110.0, cci14=-110.0, atr14=20.0)
        df15 = _mk_15m("BULLISH", st_line=110.0)
        result = signal_side(df3, df15, config=_default_config())
        self.assertEqual(result, "BUY")

    def test_t16_none(self):
        # Conflict → NONE
        df3  = _mk_df("BEARISH", "DOWN", close=100.0, st_line=110.0)
        df15 = _mk_15m("BULLISH")
        result = signal_side(df3, df15, config=_default_config())
        self.assertEqual(result, "NONE")


class TestEmptyInputs(unittest.TestCase):
    """T17–T18: graceful handling of empty DataFrames."""

    def test_t17_empty_df3m(self):
        sig = check_entry_signal(
            pd.DataFrame(), _mk_15m("BEARISH"),
            config=_default_config()
        )
        self.assertIsNone(sig)

    def test_t18_empty_df15m(self):
        df3 = _mk_df("BEARISH", "DOWN", close=100.0, st_line=110.0)
        sig = check_entry_signal(
            df3, pd.DataFrame(),
            config=_default_config()
        )
        self.assertIsNone(sig)


class TestBrokerHooks(unittest.TestCase):
    """Paper simulation hooks return success without a real broker."""

    def test_paper_entry_no_broker_fn(self):
        ok, oid = place_st_pullback_entry(
            symbol="NSE:NIFTY24JAN22000CE",
            qty=50, side="BUY",
            entry_price=120.0, sl=105.0, pt=150.0,
            mode="PAPER",
        )
        self.assertTrue(ok)
        self.assertIsNotNone(oid)

    def test_paper_exit_no_broker_fn(self):
        ok, oid = place_st_pullback_exit(
            symbol="NSE:NIFTY24JAN22000CE",
            qty=50, reason="STOPLOSS",
            mode="PAPER",
        )
        self.assertTrue(ok)
        self.assertIsNotNone(oid)

    def test_entry_with_broker_fn_success(self):
        def fake_broker(sym, qty, side):
            return True, f"ORD_{sym}_{side}"

        ok, oid = place_st_pullback_entry(
            symbol="TEST", qty=25, side="SELL",
            entry_price=200.0, sl=215.0, pt=170.0,
            mode="LIVE", broker_fn=fake_broker,
        )
        self.assertTrue(ok)
        self.assertEqual(oid, "ORD_TEST_-1")

    def test_exit_with_broker_fn_failure(self):
        def fail_broker(sym, qty, reason):
            return False, None

        ok, oid = place_st_pullback_exit(
            symbol="TEST", qty=25, reason="TARGET",
            mode="LIVE", broker_fn=fail_broker,
        )
        self.assertFalse(ok)
        self.assertIsNone(oid)


class TestNormalisedBiasInput(unittest.TestCase):
    """Raw 'UP'/'DOWN' bias values (as stored in candle columns) are normalised."""

    def test_raw_up_down_treated_as_bullish_bearish(self):
        # Use raw "UP"/"DOWN" in both frames — should normalise to BULLISH/BEARISH
        df3  = _mk_df("UP", "UP", close=115.0, st_line=110.0, cci14=-110.0, atr14=20.0)
        df15 = _mk_15m("UP", st_line=110.0)
        sig  = check_entry_signal(df3, df15, config=_default_config())
        self.assertIsNotNone(sig)
        self.assertEqual(sig["side"], "BUY")

    def test_raw_up_down_conflict(self):
        df3  = _mk_df("DOWN", "DOWN", close=100.0, st_line=110.0, cci14=110.0, atr14=20.0)
        df15 = _mk_15m("UP", st_line=110.0)   # 15m BULLISH vs 3m BEARISH
        sig  = check_entry_signal(df3, df15, config=_default_config())
        self.assertIsNone(sig)


# ---------------------------------------------------------------------------
# T19–T20: Trailing goal (TG) — risk management extension
# ---------------------------------------------------------------------------

class TestTrailingTarget(unittest.TestCase):
    """T19–T20: TG computed at conservative tg_rr_ratio; present in signal."""

    def test_t19a_tg_put_less_than_pt(self):
        """TG must be between entry and PT for PUT (SL above entry)."""
        entry = 100.0
        sl    = 115.0   # PUT: SL above entry (risk = 15)
        tg = compute_trailing_target(entry, sl, tg_rr_ratio=1.0)  # 100 - 15 = 85
        pt = compute_profit_target(entry, sl, rr_ratio=2.0)        # 100 - 30 = 70
        self.assertLess(entry, sl)             # SL above entry for PUT
        self.assertLess(tg, entry)             # TG below entry (profit direction)
        self.assertGreater(tg, pt)             # TG closer to entry than PT
        self.assertAlmostEqual(tg, 85.0, places=2)

    def test_t19b_tg_call_between_entry_and_pt(self):
        """TG must be between entry and PT for CALL (SL below entry)."""
        entry = 115.0
        sl    = 105.0   # CALL: SL below entry (risk = 10)
        tg = compute_trailing_target(entry, sl, tg_rr_ratio=1.0)  # 115 + 10 = 125
        pt = compute_profit_target(entry, sl, rr_ratio=2.0)        # 115 + 20 = 135
        self.assertGreater(entry, sl)          # SL below entry for CALL
        self.assertGreater(tg, entry)          # TG above entry (profit direction)
        self.assertLess(tg, pt)                # TG closer to entry than PT
        self.assertAlmostEqual(tg, 125.0, places=2)

    def test_t20_signal_contains_tg_key(self):
        """Signal dict must contain 'tg' key when a signal is produced."""
        df3  = _mk_df("BEARISH", "DOWN", close=100.0, st_line=110.0, cci14=110.0, atr14=20.0)
        df15 = _mk_15m("BEARISH")
        config = _default_config(tg_rr_ratio=1.0, rr_ratio=2.0)
        sig = check_entry_signal(df3, df15, config=config)
        self.assertIsNotNone(sig)
        self.assertIn("tg", sig)
        self.assertIn("pt", sig)
        self.assertIn("sl", sig)
        # TG must be closer to entry than PT for a PUT
        self.assertGreater(sig["tg"], sig["pt"])   # tg=entry-risk*1.0, pt=entry-risk*2.0
        self.assertLess(sig["tg"], sig["sl"])       # both below SL for PUT


# ---------------------------------------------------------------------------
# T21–T24: BrokerAdapter dispatch
# ---------------------------------------------------------------------------

class _StubAdapter(BrokerAdapter):
    """Minimal concrete adapter for testing dispatch logic."""

    def __init__(self, entry_success=True, exit_success=True):
        self.entry_calls: list = []
        self.exit_calls:  list = []
        self._entry_ok = entry_success
        self._exit_ok  = exit_success

    def place_entry(self, symbol, qty, side_int, limit_price=0.0, stop=0.0, target=0.0):
        self.entry_calls.append((symbol, qty, side_int, limit_price))
        if self._entry_ok:
            return True, f"STUB_ENTRY_{symbol}"
        return False, None

    def place_exit(self, symbol, qty, reason):
        self.exit_calls.append((symbol, qty, reason))
        if self._exit_ok:
            return True, f"STUB_EXIT_{symbol}_{reason}"
        return False, None


class TestBrokerAdapterDispatch(unittest.TestCase):
    """T21–T24: BrokerAdapter subclasses dispatched correctly."""

    SYM = "NSE:NIFTY24JAN22000PE"

    def test_t21_adapter_entry_dispatched(self):
        """place_st_pullback_entry with adapter calls adapter.place_entry."""
        adapter = _StubAdapter()
        ok, oid = place_st_pullback_entry(
            symbol=self.SYM, qty=50, side="SELL",
            entry_price=120.0, sl=125.0, pt=110.0,
            mode="LIVE", broker_fn=adapter,
        )
        self.assertTrue(ok)
        self.assertEqual(oid, f"STUB_ENTRY_{self.SYM}")
        self.assertEqual(len(adapter.entry_calls), 1)
        sym, qty, side_int, lp = adapter.entry_calls[0]
        self.assertEqual(sym,      self.SYM)
        self.assertEqual(qty,      50)
        self.assertEqual(side_int, -1)         # SELL → -1

    def test_t22_adapter_exit_dispatched(self):
        """place_st_pullback_exit with adapter calls adapter.place_exit."""
        adapter = _StubAdapter()
        ok, oid = place_st_pullback_exit(
            symbol=self.SYM, qty=50, reason="STOPLOSS",
            mode="LIVE", broker_fn=adapter,
        )
        self.assertTrue(ok)
        self.assertIn("STUB_EXIT", oid)
        self.assertEqual(len(adapter.exit_calls), 1)
        sym, qty, reason = adapter.exit_calls[0]
        self.assertEqual(sym,    self.SYM)
        self.assertEqual(reason, "STOPLOSS")

    def test_t23_adapter_failure_propagated(self):
        """Adapter returning (False, None) is propagated cleanly."""
        adapter = _StubAdapter(entry_success=False, exit_success=False)
        ok_e, oid_e = place_st_pullback_entry(
            symbol=self.SYM, qty=50, side="BUY",
            entry_price=120.0, sl=110.0, pt=140.0,
            mode="LIVE", broker_fn=adapter,
        )
        self.assertFalse(ok_e)
        self.assertIsNone(oid_e)

        ok_x, oid_x = place_st_pullback_exit(
            symbol=self.SYM, qty=50, reason="TARGET",
            mode="LIVE", broker_fn=adapter,
        )
        self.assertFalse(ok_x)
        self.assertIsNone(oid_x)

    def test_t24_legacy_callable_still_works(self):
        """Raw callable broker_fn still dispatches via old interface."""
        calls = []

        def fake_entry(sym, qty, side_int):
            calls.append(("entry", sym, qty, side_int))
            return True, f"LEG_{sym}"

        ok, oid = place_st_pullback_entry(
            symbol=self.SYM, qty=25, side="BUY",
            entry_price=200.0, sl=190.0, pt=220.0,
            mode="LIVE", broker_fn=fake_entry,
        )
        self.assertTrue(ok)
        self.assertEqual(oid, f"LEG_{self.SYM}")
        self.assertEqual(calls[0], ("entry", self.SYM, 25, 1))  # BUY → 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.disable(logging.NOTSET)  # re-enable for standalone runs
    unittest.main(verbosity=2)
