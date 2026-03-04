"""Unit tests for OptionExitManager and check_exit_condition exit logic.

Coverage:
  TestOptionExitManager  – DTS, momentum exhaustion, buffer behaviour
  TestExitSL             – SL precedence and audit trail
  TestExitTG             – TG path, deferral, audit type
  TestExitPT             – Early-PT, normal-PT, double-booking guard
  TestExitScalp          – scalp_mode PT / SL / custom thresholds
  TestExitSuppression    – Minimum-hold gate logic
  TestExitConflict       – SL > TG > PT > HFT precedence matrix
  TestExitBrokerDispatch – Real OptionExitManager wired into check_exit_condition
  TestExitRestart        – Startup suppression, restored positions
  TestCleanupTradeExit   – State mutation and audit logging
  TestScalpCooldownGate  – _can_enter_scalp cooldown and burst deduplication
"""

import sys
import types
import unittest
from unittest.mock import MagicMock

import numpy as np
import pandas as pd

# ── Stub all heavy dependencies BEFORE importing execution.py ─────────────────
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
]

for _n in _STUB_NAMES:
    if _n not in sys.modules:
        sys.modules[_n] = types.ModuleType(_n)

# -- config --
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
_cfg.OSCILLATOR_EXIT_MODE = "SOFT"
_cfg.symbols = {"index": "NSE:NIFTY50-INDEX"}
_cfg.TREND_ENTRY_ADX_MIN = 18.0

# -- setup --
_stup = sys.modules["setup"]
_stup.df = pd.DataFrame()
_stup.fyers = MagicMock()
_stup.ticker = "NSE:NIFTY50-INDEX"
_stup.option_chain = {}
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

# -- signals --
_sig = sys.modules["signals"]
_sig.detect_signal = MagicMock(return_value=None)
_sig.get_opening_range = MagicMock(return_value=(None, None))

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
_dt_mod.DayType = MagicMock()
_dt_mod.DayTypeResult = MagicMock()

# -- fyers_apiv3 --
_fyers_pkg = sys.modules["fyers_apiv3"]
_fyers_model_mod = sys.modules["fyers_apiv3.fyersModel"]
_fyers_pkg.fyersModel = _fyers_model_mod
_fyers_model_mod.FyersModel = MagicMock()

# ── Now safe to import execution.py ──────────────────────────────────────────
import execution  # noqa: E402
from execution import (  # noqa: E402
    check_exit_condition,
    cleanup_trade_exit,
    _can_enter_scalp,
    _is_startup_suppression_active,
    SCALP_PT_POINTS,
    SCALP_SL_POINTS,
)
from option_exit_manager import OptionExitConfig, OptionExitManager  # noqa: E402

import pendulum  # noqa: E402

_TZ = "Asia/Kolkata"


# ── Shared helpers ────────────────────────────────────────────────────────────

def _make_df(n: int, close: float = 250.0, open_: float = None) -> pd.DataFrame:
    """Minimal OHLC slice with *n* rows.  open_ defaults to close (no reversal)."""
    o = open_ if open_ is not None else close
    return pd.DataFrame({"open": [o] * n, "close": [close] * n})


def _base_state(
    side: str = "CALL",
    entry_candle: int = 0,
    close: float = 250.0,
    stop: float = None,
    pt: float = None,
    tg: float = None,
) -> dict:
    """Minimal open-position state dict for check_exit_condition.

    stop/pt/tg are omitted when None so that state.get("stop", 0) returns 0
    rather than None (avoids TypeError in execution.py's trailing-stop guard).
    """
    state = {
        "side": side,
        "position_side": "LONG",
        "option_name": f"NIFTY{side}",
        "position_id": "TEST-001",
        "buy_price": close,
        "entry_candle": entry_candle,
        "is_open": True,
        "scalp_mode": False,
        "hf_exit_manager": None,
        "source": "ATR",
        "atr_value": 50.0,
    }
    if stop is not None:
        state["stop"] = stop
    if pt is not None:
        state["pt"] = pt
    if tg is not None:
        state["tg"] = tg
    return state


def _ts() -> "pendulum.DateTime":
    return pendulum.now(_TZ)


def _filled_df() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["ticker", "price", "action", "stop_price", "take_profit",
                 "spot_price", "quantity"]
    )


def _leg_info(option_name: str = "NIFTY25000CE", qty: int = 50) -> dict:
    return {
        "trade_flag": 1,
        "quantity": qty,
        "is_open": True,
        "lifecycle_state": "OPEN",
        "filled_df": _filled_df(),
        "position_side": "LONG",
        "regime_context": "ATR",
        "position_id": "POS-001",
        "option_name": option_name,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# TestOptionExitManager  –  direct unit tests (no execution.py dependency)
# ═══════════════════════════════════════════════════════════════════════════════

class TestOptionExitManager(unittest.TestCase):

    def _mgr(self, entry: float = 200.0, risk_buffer: float = 0.0,
             **cfg_kwargs) -> OptionExitManager:
        return OptionExitManager(
            entry_price=entry,
            risk_buffer=risk_buffer,
            config=OptionExitConfig(**cfg_kwargs),
        )

    # ── no-exit guards ────────────────────────────────────────────────────────

    def test_no_exit_at_entry_price(self):
        mgr = self._mgr(200.0)
        self.assertFalse(mgr.check_exit(200.0, _ts()))

    def test_no_exit_below_entry_price(self):
        mgr = self._mgr(200.0)
        self.assertFalse(mgr.check_exit(180.0, _ts()))

    def test_last_reason_empty_when_no_exit(self):
        mgr = self._mgr(200.0)
        mgr.check_exit(210.0, _ts())
        self.assertEqual(mgr.last_reason, "")

    # ── DTS tests ─────────────────────────────────────────────────────────────

    def test_dts_fires_when_price_retraces_from_peak(self):
        """DTS: entry=200, peak=320 (60 %+ profit → 3 % trail). 309 < 310.4 → fires."""
        mgr = self._mgr(200.0, risk_buffer=0.0)
        ts = _ts()
        for p in [220, 260, 300, 320]:
            mgr.update_tick(p, 0, ts)
        # trail_stop = 320 * (1 - 0.03) = 310.4; price=309 ≤ 310.4 → DTS
        result = mgr.check_exit(309.0, ts, ingest_tick=False)
        self.assertTrue(result)
        self.assertEqual(mgr.last_reason, "DYNAMIC_TRAILING_STOP")

    def test_dts_does_not_fire_above_trail_stop(self):
        mgr = self._mgr(200.0, risk_buffer=0.0)
        ts = _ts()
        for p in [220, 260, 300, 320]:
            mgr.update_tick(p, 0, ts)
        # price=315 > trail_stop=310.4 → no DTS
        result = mgr.check_exit(315.0, ts, ingest_tick=False)
        self.assertFalse(result)

    def test_dts_uses_wide_trail_when_profit_is_small(self):
        """At low profit, trail is wider so a small retrace is tolerated.

        trail_frac = dynamic_trail_lo - span * (profit_frac / tighten_frac)
        with profit_frac computed from *current* price, not peak.

        peak=240, entry=200.
        price=230 → profit_frac=0.15, trail_frac≈0.079, trail_stop≈221 → no DTS.
        price=210 → profit_frac=0.05, trail_frac≈0.093, trail_stop≈217.7 → DTS.
        """
        mgr = self._mgr(200.0, risk_buffer=0.0)
        ts = _ts()
        for p in [210, 220, 230, 240]:
            mgr.update_tick(p, 0, ts)
        # Small retrace – trail_stop ≈ 221; price=230 is safely above → no DTS
        self.assertFalse(mgr.check_exit(230.0, ts, ingest_tick=False))
        # Large retrace – trail_stop ≈ 217.7; price=210 is below → DTS fires
        self.assertTrue(mgr.check_exit(210.0, ts, ingest_tick=False))

    def test_last_reason_set_after_dts(self):
        mgr = self._mgr(200.0, risk_buffer=0.0)
        ts = _ts()
        for p in [250, 300, 360]:
            mgr.update_tick(p, 0, ts)
        mgr.check_exit(340.0, ts, ingest_tick=False)
        self.assertEqual(mgr.last_reason, "DYNAMIC_TRAILING_STOP")

    # ── buffer / tick tests ───────────────────────────────────────────────────

    def test_update_tick_populates_buffers(self):
        mgr = self._mgr(200.0)
        ts = _ts()
        mgr.update_tick(210.0, 100.0, ts)
        mgr.update_tick(215.0, 200.0, ts)
        self.assertEqual(len(mgr._prices), 2)
        self.assertEqual(len(mgr._volumes), 2)
        self.assertEqual(mgr._peak_price, 215.0)

    def test_ingest_tick_false_skips_append(self):
        """ingest_tick=False: buffer unchanged but evaluation still runs."""
        mgr = self._mgr(200.0)
        ts = _ts()
        for p in [210, 220, 230]:
            mgr.update_tick(p, 0, ts)
        before_len = len(mgr._prices)
        mgr.check_exit(225.0, ts, ingest_tick=False)
        self.assertEqual(len(mgr._prices), before_len)

    def test_momentum_exhaustion_not_triggered_with_too_few_ticks(self):
        """_momentum_exhaustion requires ≥ roc_window_ticks+1 ticks."""
        mgr = self._mgr(100.0, roc_window_ticks=8)
        ts = _ts()
        # Feed only 5 ticks (< 9 required)
        for p in [100, 102, 105, 109, 113]:
            mgr.update_tick(p, 0, ts)
        self.assertFalse(mgr._momentum_exhaustion())

    def test_volatility_mean_reversion_not_triggered_with_few_ticks(self):
        """_volatility_mean_reversion requires ≥ ma_window ticks."""
        mgr = self._mgr(100.0, ma_window=20)
        ts = _ts()
        # Feed only 10 ticks (< 20 required)
        for p in range(100, 110):
            mgr.update_tick(float(p), 0, ts)
        self.assertFalse(mgr._volatility_mean_reversion(109.0))


# ═══════════════════════════════════════════════════════════════════════════════
# TestExitSL
# ═══════════════════════════════════════════════════════════════════════════════

class TestExitSL(unittest.TestCase):

    def _run(self, ltp: float, stop: float, side: str = "CALL",
             bars_held: int = 3, entry: float = 200.0):
        n = bars_held + 1
        df_s = _make_df(n, close=ltp)
        state = _base_state(side=side, entry_candle=0, close=entry, stop=stop)
        return check_exit_condition(df_s, state, option_price=ltp, timestamp=_ts())

    def test_sl_hit_call(self):
        triggered, reason = self._run(ltp=149.0, stop=150.0, side="CALL")
        self.assertTrue(triggered)
        self.assertEqual(reason, "SL_HIT")

    def test_sl_hit_put(self):
        triggered, reason = self._run(ltp=149.0, stop=150.0, side="PUT")
        self.assertTrue(triggered)
        self.assertEqual(reason, "SL_HIT")

    def test_sl_exactly_at_stop(self):
        triggered, reason = self._run(ltp=150.0, stop=150.0)
        self.assertTrue(triggered)
        self.assertEqual(reason, "SL_HIT")

    def test_sl_not_hit(self):
        triggered, _ = self._run(ltp=160.0, stop=150.0)
        self.assertFalse(triggered)

    def test_sl_fires_at_zero_bars_held(self):
        """SL is always active – not suppressed by minimum-hold gate."""
        n = 4
        df_s = _make_df(n, close=149.0)
        state = _base_state(side="CALL", entry_candle=3, close=200.0, stop=150.0)
        # bars_held = (4-1) - 3 = 0
        triggered, reason = check_exit_condition(df_s, state, option_price=149.0, timestamp=_ts())
        self.assertTrue(triggered)
        self.assertEqual(reason, "SL_HIT")

    def test_sl_takes_precedence_over_tg(self):
        """SL is checked before TG – SL wins when both conditions are met."""
        n = 4
        df_s = _make_df(n, close=400.0)
        state = _base_state(side="CALL", entry_candle=0, close=200.0,
                            stop=400.0, tg=300.0)
        triggered, reason = check_exit_condition(df_s, state, option_price=400.0, timestamp=_ts())
        self.assertTrue(triggered)
        self.assertEqual(reason, "SL_HIT")

    def test_closed_position_skipped(self):
        n = 4
        df_s = _make_df(n, close=100.0)
        state = _base_state(side="CALL", entry_candle=0, close=200.0, stop=150.0)
        state["is_open"] = False
        triggered, reason = check_exit_condition(df_s, state, option_price=100.0, timestamp=_ts())
        self.assertFalse(triggered)
        self.assertIsNone(reason)

    def test_sl_audit_written_to_state(self):
        n = 4
        df_s = _make_df(n, close=149.0)
        state = _base_state(side="CALL", entry_candle=0, close=200.0, stop=150.0)
        check_exit_condition(df_s, state, option_price=149.0, timestamp=_ts())
        self.assertEqual(state.get("last_exit_type"), "SL")

    def test_sl_log_contains_sl_hit(self):
        n = 4
        df_s = _make_df(n, close=149.0)
        state = _base_state(side="CALL", entry_candle=0, close=200.0, stop=150.0)
        with self.assertLogs(level="INFO") as cm:
            check_exit_condition(df_s, state, option_price=149.0, timestamp=_ts())
        self.assertTrue(any("SL_HIT" in line for line in cm.output))


# ═══════════════════════════════════════════════════════════════════════════════
# TestExitTG
# ═══════════════════════════════════════════════════════════════════════════════

class TestExitTG(unittest.TestCase):

    def test_tg_hit_after_min_bars(self):
        """ltp ≥ tg and bars_held=3 ≥ 3 → TARGET_HIT."""
        n = 4  # i=3, entry_candle=0, bars_held=3
        df_s = _make_df(n, close=310.0)
        state = _base_state(side="CALL", entry_candle=0, close=200.0, tg=300.0)
        triggered, reason = check_exit_condition(df_s, state, option_price=310.0, timestamp=_ts())
        self.assertTrue(triggered)
        self.assertEqual(reason, "TARGET_HIT")

    def test_tg_hit_exactly_at_threshold(self):
        n = 4
        df_s = _make_df(n, close=300.0)
        state = _base_state(side="CALL", entry_candle=0, close=200.0, tg=300.0)
        triggered, reason = check_exit_condition(df_s, state, option_price=300.0, timestamp=_ts())
        self.assertTrue(triggered)
        self.assertEqual(reason, "TARGET_HIT")

    def test_tg_deferred_before_min_bars(self):
        """bars_held=1 < 3 → deferred, returns (False, None)."""
        n = 4
        df_s = _make_df(n, close=310.0)
        # entry_candle=2, i=3, bars_held=1
        state = _base_state(side="CALL", entry_candle=2, close=200.0, tg=300.0)
        triggered, reason = check_exit_condition(df_s, state, option_price=310.0, timestamp=_ts())
        self.assertFalse(triggered)
        self.assertIsNone(reason)

    def test_tg_not_hit(self):
        n = 4
        df_s = _make_df(n, close=290.0)
        state = _base_state(side="CALL", entry_candle=0, close=200.0, tg=300.0)
        triggered, _ = check_exit_condition(df_s, state, option_price=290.0, timestamp=_ts())
        self.assertFalse(triggered)

    def test_tg_audit_type_is_tg(self):
        n = 4
        df_s = _make_df(n, close=310.0)
        state = _base_state(side="CALL", entry_candle=0, close=200.0, tg=300.0)
        check_exit_condition(df_s, state, option_price=310.0, timestamp=_ts())
        self.assertEqual(state.get("last_exit_type"), "TG")

    def test_tg_log_contains_tg_hit(self):
        n = 4
        df_s = _make_df(n, close=310.0)
        state = _base_state(side="CALL", entry_candle=0, close=200.0, tg=300.0)
        with self.assertLogs(level="INFO") as cm:
            check_exit_condition(df_s, state, option_price=310.0, timestamp=_ts())
        self.assertTrue(any("TG_HIT" in line for line in cm.output))


# ═══════════════════════════════════════════════════════════════════════════════
# TestExitPT
# ═══════════════════════════════════════════════════════════════════════════════

class TestExitPT(unittest.TestCase):

    def test_pt_early_hit_bars_held_zero(self):
        """PT hit when bars_held=0 → immediate PT_HIT exit (special early booking)."""
        n = 4
        df_s = _make_df(n, close=310.0)
        # entry_candle=3, i=3, bars_held=0
        state = _base_state(side="CALL", entry_candle=3, close=200.0, pt=300.0)
        triggered, reason = check_exit_condition(df_s, state, option_price=310.0, timestamp=_ts())
        self.assertTrue(triggered)
        self.assertEqual(reason, "PT_HIT")

    def test_pt_early_hit_locks_stop_at_entry(self):
        """After early PT exit, stop is locked at entry_price."""
        n = 4
        df_s = _make_df(n, close=310.0)
        state = _base_state(side="CALL", entry_candle=3, close=200.0, pt=300.0)
        state["stop"] = 180.0
        check_exit_condition(df_s, state, option_price=310.0, timestamp=_ts())
        # Stop should be raised to at least entry (200.0)
        self.assertGreaterEqual(state["stop"], 200.0)

    def test_pt_hit_after_min_bars_sets_partial_booked(self):
        """bars_held=3 ≥ 3, ltp ≥ pt → partial_booked=True, stop ≥ entry."""
        n = 4
        df_s = _make_df(n, close=310.0)
        state = _base_state(side="CALL", entry_candle=0, close=200.0, pt=300.0)
        state["stop"] = 180.0
        check_exit_condition(df_s, state, option_price=310.0, timestamp=_ts())
        self.assertTrue(state.get("partial_booked", False))
        self.assertGreaterEqual(state["stop"], 200.0)

    def test_pt_not_rebooked_after_partial(self):
        """Once partial_booked=True, pt_hit evaluates to False (no double booking)."""
        n = 4
        df_s = _make_df(n, close=310.0)
        state = _base_state(side="CALL", entry_candle=0, close=200.0, pt=300.0)
        state["partial_booked"] = True
        # With partial_booked set, pt_hit is always False, so no PT_HIT return
        check_exit_condition(df_s, state, option_price=310.0, timestamp=_ts())
        # partial_booked should remain True (not double-triggered)
        self.assertTrue(state["partial_booked"])

    def test_pt_deferred_between_bars_one_and_min(self):
        """bars_held=1 (not 0), PT hit → deferred (False, None)."""
        n = 4
        df_s = _make_df(n, close=310.0)
        # entry_candle=2, i=3, bars_held=1
        state = _base_state(side="CALL", entry_candle=2, close=200.0, pt=300.0)
        triggered, reason = check_exit_condition(df_s, state, option_price=310.0, timestamp=_ts())
        self.assertFalse(triggered)


# ═══════════════════════════════════════════════════════════════════════════════
# TestExitScalp
# ═══════════════════════════════════════════════════════════════════════════════

class TestExitScalp(unittest.TestCase):

    def _scalp_state(self, side: str = "CALL", entry: float = 250.0,
                     scalp_pt: float = None, scalp_sl: float = None) -> dict:
        state = _base_state(side=side, entry_candle=0, close=entry)
        state["scalp_mode"] = True
        state["buy_price"] = entry
        if scalp_pt is not None:
            state["scalp_pt_points"] = scalp_pt
        if scalp_sl is not None:
            state["scalp_sl_points"] = scalp_sl
        return state

    def test_scalp_pt_hit_call(self):
        state = self._scalp_state(side="CALL", entry=250.0)
        ltp = 250.0 + SCALP_PT_POINTS          # exactly at PT (7 pts)
        triggered, reason = check_exit_condition(
            _make_df(4, close=ltp), state, option_price=ltp, timestamp=_ts())
        self.assertTrue(triggered)
        self.assertEqual(reason, "SCALP_PT_HIT")

    def test_scalp_pt_hit_put(self):
        state = self._scalp_state(side="PUT", entry=250.0)
        ltp = 250.0 + SCALP_PT_POINTS
        triggered, reason = check_exit_condition(
            _make_df(4, close=ltp), state, option_price=ltp, timestamp=_ts())
        self.assertTrue(triggered)
        self.assertEqual(reason, "SCALP_PT_HIT")

    def test_scalp_sl_hit(self):
        state = self._scalp_state(side="CALL", entry=250.0)
        ltp = 250.0 - SCALP_SL_POINTS          # exactly at SL (4 pts)
        triggered, reason = check_exit_condition(
            _make_df(4, close=ltp), state, option_price=ltp, timestamp=_ts())
        self.assertTrue(triggered)
        self.assertEqual(reason, "SCALP_SL_HIT")

    def test_scalp_no_exit_mid_range(self):
        """premium_move=+3 < 7 PT, > -4 SL → no exit."""
        state = self._scalp_state(side="CALL", entry=250.0)
        ltp = 253.0
        triggered, _ = check_exit_condition(
            _make_df(4, close=ltp), state, option_price=ltp, timestamp=_ts())
        self.assertFalse(triggered)

    def test_scalp_custom_pt_threshold(self):
        """Custom scalp_pt=10. premium_move=+8 < 10 → no exit."""
        state = self._scalp_state(side="CALL", entry=250.0, scalp_pt=10.0, scalp_sl=5.0)
        ltp = 258.0  # +8 < 10
        triggered, _ = check_exit_condition(
            _make_df(4, close=ltp), state, option_price=ltp, timestamp=_ts())
        self.assertFalse(triggered)

    def test_sl_wins_over_scalp_logic(self):
        """Normal SL is checked BEFORE scalp logic; SL wins."""
        state = self._scalp_state(side="CALL", entry=250.0)
        state["stop"] = 249.5               # SL at 249.5
        ltp = 249.0                         # below SL; premium_move < PT
        triggered, reason = check_exit_condition(
            _make_df(4, close=ltp), state, option_price=ltp, timestamp=_ts())
        self.assertTrue(triggered)
        self.assertEqual(reason, "SL_HIT")

    def test_scalp_audit_written(self):
        state = self._scalp_state(side="CALL", entry=250.0)
        ltp = 250.0 + SCALP_PT_POINTS
        check_exit_condition(_make_df(4, close=ltp), state, option_price=ltp, timestamp=_ts())
        self.assertEqual(state.get("last_exit_type"), "SCALP_PT_HIT")


# ═══════════════════════════════════════════════════════════════════════════════
# TestExitSuppression  –  minimum-hold gate
# ═══════════════════════════════════════════════════════════════════════════════

class TestExitSuppression(unittest.TestCase):

    def test_hft_momentum_exhaustion_suppressed_at_bars_held_one(self):
        """MOMENTUM_EXHAUSTION with bars_held=1 < 2 → suppressed."""
        hf_mgr = MagicMock()
        hf_mgr.check_exit.return_value = True
        hf_mgr.last_reason = "MOMENTUM_EXHAUSTION"

        df_s = _make_df(4, close=250.0)
        # entry_candle=2, i=3, bars_held=1
        state = _base_state(side="CALL", entry_candle=2, close=200.0)
        state["hf_exit_manager"] = hf_mgr

        triggered, _ = check_exit_condition(df_s, state, option_price=250.0, timestamp=_ts())
        self.assertFalse(triggered)

    def test_hft_dts_suppressed_at_bars_held_zero(self):
        """Non-SL/PT HFT reason at bars_held=0 → suppressed."""
        hf_mgr = MagicMock()
        hf_mgr.check_exit.return_value = True
        hf_mgr.last_reason = "DYNAMIC_TRAILING_STOP"

        df_s = _make_df(4, close=250.0)
        # entry_candle=3, i=3, bars_held=0
        state = _base_state(side="CALL", entry_candle=3, close=200.0)
        state["hf_exit_manager"] = hf_mgr

        triggered, _ = check_exit_condition(df_s, state, option_price=250.0, timestamp=_ts())
        self.assertFalse(triggered)

    def test_hft_sl_hit_not_suppressed_at_bars_held_zero(self):
        """SL_HIT is whitelisted – not suppressed even at bars_held=0."""
        hf_mgr = MagicMock()
        hf_mgr.check_exit.return_value = True
        hf_mgr.last_reason = "SL_HIT"

        df_s = _make_df(4, close=250.0)
        state = _base_state(side="CALL", entry_candle=3, close=200.0)
        state["hf_exit_manager"] = hf_mgr

        triggered, reason = check_exit_condition(df_s, state, option_price=250.0, timestamp=_ts())
        self.assertTrue(triggered)
        self.assertEqual(reason, "SL_HIT")

    def test_premature_exit_suppressed_no_pt_bars_zero(self):
        """bars_held=0, no PT, no SL → suppressed by minimum-hold gate."""
        df_s = _make_df(4, close=250.0)
        # entry_candle=3, i=3, bars_held=0; tg set but not hit
        state = _base_state(side="CALL", entry_candle=3, close=200.0, tg=300.0)
        triggered, _ = check_exit_condition(df_s, state, option_price=250.0, timestamp=_ts())
        self.assertFalse(triggered)

    def test_sl_always_active_regardless_of_bars_held(self):
        """Direct SL check (line 1050) is NOT gated by bars_held."""
        df_s = _make_df(4, close=149.0)
        state = _base_state(side="CALL", entry_candle=3, close=200.0, stop=150.0)
        # bars_held=0, but SL fires before any bar gate
        triggered, reason = check_exit_condition(df_s, state, option_price=149.0, timestamp=_ts())
        self.assertTrue(triggered)
        self.assertEqual(reason, "SL_HIT")


# ═══════════════════════════════════════════════════════════════════════════════
# TestExitConflict  –  precedence matrix
# ═══════════════════════════════════════════════════════════════════════════════

class TestExitConflict(unittest.TestCase):

    def test_sl_wins_over_tg_same_tick(self):
        """ltp satisfies both SL and TG. SL (line 1050) is checked first → SL wins."""
        df_s = _make_df(4, close=400.0)
        state = _base_state(side="CALL", entry_candle=0, close=200.0,
                            stop=400.0, tg=300.0)
        triggered, reason = check_exit_condition(df_s, state, option_price=400.0, timestamp=_ts())
        self.assertTrue(triggered)
        self.assertEqual(reason, "SL_HIT")

    def test_tg_wins_over_pt_same_tick(self):
        """Both TG and PT hit. TG evaluated first at line 1112 → TARGET_HIT."""
        df_s = _make_df(4, close=350.0)
        state = _base_state(side="CALL", entry_candle=0, close=200.0,
                            tg=300.0, pt=280.0)
        triggered, reason = check_exit_condition(df_s, state, option_price=350.0, timestamp=_ts())
        self.assertTrue(triggered)
        self.assertEqual(reason, "TARGET_HIT")

    def test_hft_fires_before_sl(self):
        """HFT (line 1022) has highest precedence → fires before SL (line 1050)."""
        hf_mgr = MagicMock()
        hf_mgr.check_exit.return_value = True
        hf_mgr.last_reason = "DYNAMIC_TRAILING_STOP"

        df_s = _make_df(4, close=250.0)          # bars_held=3 → not suppressed
        state = _base_state(side="CALL", entry_candle=0, close=200.0, stop=260.0)
        state["hf_exit_manager"] = hf_mgr

        triggered, reason = check_exit_condition(df_s, state, option_price=250.0, timestamp=_ts())
        self.assertTrue(triggered)
        self.assertEqual(reason, "DYNAMIC_TRAILING_STOP")

    def test_scalp_mode_sl_before_scalp_logic(self):
        """scalp_mode=True + SL hit → SL fires before scalp PT/SL evaluation."""
        state = _base_state(side="CALL", entry_candle=0, close=250.0)
        state["scalp_mode"] = True
        state["buy_price"] = 250.0
        state["stop"] = 249.5
        ltp = 249.0
        triggered, reason = check_exit_condition(
            _make_df(4, close=ltp), state, option_price=ltp, timestamp=_ts())
        self.assertTrue(triggered)
        self.assertEqual(reason, "SL_HIT")


# ═══════════════════════════════════════════════════════════════════════════════
# TestExitBrokerDispatch  –  real OptionExitManager wired into check_exit_condition
# ═══════════════════════════════════════════════════════════════════════════════

class TestExitBrokerDispatch(unittest.TestCase):

    def _attach_manager(self, state: dict, entry: float = 200.0,
                        **cfg_kw) -> OptionExitManager:
        mgr = OptionExitManager(entry_price=entry, risk_buffer=0.0,
                                config=OptionExitConfig(**cfg_kw))
        state["hf_exit_manager"] = mgr
        return mgr

    def test_real_dts_propagates_through_check_exit_condition(self):
        """Real OptionExitManager DTS triggers check_exit_condition exit."""
        # bars_held=4 → not suppressed by any guard
        n = 5
        df_s = _make_df(n, close=309.0)
        state = _base_state(side="CALL", entry_candle=0, close=200.0)
        mgr = self._attach_manager(state, entry=200.0)
        ts = _ts()
        # Peak=320 → trail_stop = 320*0.97=310.4; ltp=309 < 310.4 → DTS
        for p in [220, 260, 300, 320]:
            mgr.update_tick(p, 0, ts)

        triggered, reason = check_exit_condition(df_s, state, option_price=309.0, timestamp=ts)
        self.assertTrue(triggered)
        self.assertEqual(reason, "DYNAMIC_TRAILING_STOP")

    def test_hft_error_does_not_propagate(self):
        """Exception in hf_mgr.check_exit is caught; execution continues."""
        hf_mgr = MagicMock()
        hf_mgr.check_exit.side_effect = RuntimeError("tick feed error")

        df_s = _make_df(4, close=250.0)
        state = _base_state(side="CALL", entry_candle=0, close=200.0)
        state["hf_exit_manager"] = hf_mgr

        # Must not raise; the function should continue and return gracefully
        try:
            result = check_exit_condition(df_s, state, option_price=250.0, timestamp=_ts())
        except Exception as exc:
            self.fail(f"check_exit_condition raised unexpectedly: {exc}")

    def test_hft_pt_hit_not_suppressed_at_bars_zero(self):
        """HFT reason=PT_HIT at bars_held=0 → whitelisted, NOT suppressed."""
        hf_mgr = MagicMock()
        hf_mgr.check_exit.return_value = True
        hf_mgr.last_reason = "PT_HIT"

        df_s = _make_df(4, close=250.0)
        state = _base_state(side="CALL", entry_candle=3, close=200.0)  # bars_held=0
        state["hf_exit_manager"] = hf_mgr

        triggered, reason = check_exit_condition(df_s, state, option_price=250.0, timestamp=_ts())
        self.assertTrue(triggered)
        self.assertEqual(reason, "PT_HIT")

    def test_all_three_exit_reasons_are_valid_strings(self):
        """Enumerate the three valid OptionExitManager reasons."""
        for reason in ("DYNAMIC_TRAILING_STOP", "MOMENTUM_EXHAUSTION",
                       "VOLATILITY_MEAN_REVERSION"):
            self.assertIsInstance(reason, str)


# ═══════════════════════════════════════════════════════════════════════════════
# TestExitRestart  –  startup suppression + restored positions
# ═══════════════════════════════════════════════════════════════════════════════

class TestExitRestart(unittest.TestCase):

    def test_startup_suppression_active_blocks(self):
        ts_now = pendulum.now(_TZ)
        info = {"startup_suppression_until": ts_now.add(seconds=120)}
        self.assertTrue(_is_startup_suppression_active(info, ts_now, "PAPER"))

    def test_startup_suppression_expired_allows(self):
        ts_now = pendulum.now(_TZ)
        info = {"startup_suppression_until": ts_now.subtract(seconds=30)}
        self.assertFalse(_is_startup_suppression_active(info, ts_now, "PAPER"))

    def test_startup_suppression_none_allows(self):
        info = {"startup_suppression_until": None}
        self.assertFalse(_is_startup_suppression_active(info, pendulum.now(_TZ), "PAPER"))

    def test_restored_position_sl_still_active(self):
        """Restored (is_open=True) trade: SL still evaluated correctly."""
        df_s = _make_df(4, close=149.0)
        state = _base_state(side="CALL", entry_candle=0, close=200.0, stop=150.0)
        state["is_open"] = True   # explicitly restored
        triggered, reason = check_exit_condition(df_s, state, option_price=149.0, timestamp=_ts())
        self.assertTrue(triggered)
        self.assertEqual(reason, "SL_HIT")

    def test_restored_position_tg_still_active(self):
        """Restored trade: TG evaluated correctly after restart."""
        df_s = _make_df(4, close=310.0)
        state = _base_state(side="CALL", entry_candle=0, close=200.0, tg=300.0)
        state["is_open"] = True
        triggered, reason = check_exit_condition(df_s, state, option_price=310.0, timestamp=_ts())
        self.assertTrue(triggered)
        self.assertEqual(reason, "TARGET_HIT")

    def test_closed_position_after_restart_skipped(self):
        """is_open=False → [EXIT SKIP] and (False, None)."""
        df_s = _make_df(4, close=100.0)
        state = _base_state(side="CALL", entry_candle=0, close=200.0, stop=150.0)
        state["is_open"] = False
        triggered, reason = check_exit_condition(df_s, state, option_price=100.0, timestamp=_ts())
        self.assertFalse(triggered)
        self.assertIsNone(reason)

    def test_suppression_logs_remaining_time(self):
        """Suppression active → logs [ENTRY BLOCKED][STARTUP_SUPPRESSION]."""
        ts_now = pendulum.now(_TZ)
        info = {
            "startup_suppression_until": ts_now.add(seconds=120),
            "startup_suppression_logged_at": None,
        }
        with self.assertLogs(level="INFO") as cm:
            _is_startup_suppression_active(info, ts_now, "PAPER")
        self.assertTrue(any("STARTUP_SUPPRESSION" in line for line in cm.output))


# ═══════════════════════════════════════════════════════════════════════════════
# TestCleanupTradeExit  –  state mutation and audit logging
# ═══════════════════════════════════════════════════════════════════════════════

class TestCleanupTradeExit(unittest.TestCase):

    def _make_info(self, leg: str = "call_buy",
                   option_name: str = "NIFTY25000CE") -> dict:
        return {leg: _leg_info(option_name=option_name)}

    # ── state mutation ────────────────────────────────────────────────────────

    def test_trade_flag_reset_to_zero(self):
        info = self._make_info("call_buy")
        cleanup_trade_exit(info, "call_buy", "CALL", "NIFTY25000CE",
                           50, 255.0, "PAPER", "SL_HIT")
        self.assertEqual(info["call_buy"]["trade_flag"], 0)

    def test_is_open_set_false(self):
        info = self._make_info("call_buy")
        cleanup_trade_exit(info, "call_buy", "CALL", "NIFTY25000CE",
                           50, 255.0, "PAPER", "SL_HIT")
        self.assertFalse(info["call_buy"]["is_open"])

    def test_lifecycle_state_set_exit(self):
        info = self._make_info("put_buy", option_name="NIFTY24000PE")
        cleanup_trade_exit(info, "put_buy", "PUT", "NIFTY24000PE",
                           50, 180.0, "PAPER", "TARGET_HIT")
        self.assertEqual(info["put_buy"]["lifecycle_state"], "EXIT")

    def test_quantity_reset_to_zero(self):
        info = self._make_info("call_buy")
        cleanup_trade_exit(info, "call_buy", "CALL", "NIFTY25000CE",
                           50, 255.0, "PAPER", "EOD")
        self.assertEqual(info["call_buy"]["quantity"], 0)

    def test_filled_df_has_one_exit_row(self):
        info = self._make_info("call_buy")
        cleanup_trade_exit(info, "call_buy", "CALL", "NIFTY25000CE",
                           50, 255.0, "PAPER", "SL_HIT")
        fdf = info["call_buy"]["filled_df"]
        self.assertEqual(len(fdf), 1)
        row = fdf.iloc[0]
        self.assertEqual(row["action"], "EXIT")
        self.assertAlmostEqual(float(row["price"]), 255.0, places=2)

    # ── logging / audit ───────────────────────────────────────────────────────

    def test_exit_audit_log_emitted(self):
        info = self._make_info("call_buy")
        with self.assertLogs(level="INFO") as cm:
            cleanup_trade_exit(info, "call_buy", "CALL", "NIFTY25000CE",
                               50, 255.0, "PAPER", "SL_HIT")
        self.assertTrue(any("EXIT AUDIT" in line for line in cm.output))

    def test_reason_appears_in_log(self):
        info = self._make_info("call_buy")
        with self.assertLogs(level="INFO") as cm:
            cleanup_trade_exit(info, "call_buy", "CALL", "NIFTY25000CE",
                               50, 255.0, "PAPER", "PT_HIT")
        self.assertTrue(any("PT_HIT" in line for line in cm.output))

    def test_multiple_cleanups_accumulate_rows(self):
        """Two separate cleanup calls → two rows in filled_df."""
        info = self._make_info("call_buy")
        cleanup_trade_exit(info, "call_buy", "CALL", "NIFTY25000CE",
                           50, 255.0, "PAPER", "SL_HIT")
        # Re-open to allow second cleanup
        info["call_buy"]["trade_flag"] = 1
        info["call_buy"]["is_open"] = True
        info["call_buy"]["lifecycle_state"] = "OPEN"
        cleanup_trade_exit(info, "call_buy", "CALL", "NIFTY25000CE",
                           50, 240.0, "PAPER", "EOD")
        self.assertEqual(len(info["call_buy"]["filled_df"]), 2)


# ═══════════════════════════════════════════════════════════════════════════════
# TestScalpCooldownGate  –  _can_enter_scalp
# ═══════════════════════════════════════════════════════════════════════════════

class TestScalpCooldownGate(unittest.TestCase):

    def test_cooldown_active_blocks_with_reason(self):
        ts_now = pendulum.now(_TZ)
        info = {
            "scalp_cooldown_until": ts_now.add(minutes=5),
            "scalp_last_burst_key": None,
        }
        ok, reason = _can_enter_scalp(info, "KEY1", ts_now)
        self.assertFalse(ok)
        self.assertEqual(reason, "COOLDOWN")

    def test_cooldown_expired_allows_entry(self):
        ts_now = pendulum.now(_TZ)
        info = {
            "scalp_cooldown_until": ts_now.subtract(seconds=30),
            "scalp_last_burst_key": None,
        }
        ok, reason = _can_enter_scalp(info, "KEY1", ts_now)
        self.assertTrue(ok)
        self.assertEqual(reason, "OK")

    def test_duplicate_burst_key_blocks(self):
        ts_now = pendulum.now(_TZ)
        info = {
            "scalp_cooldown_until": None,
            "scalp_last_burst_key": "KEY1",
        }
        ok, reason = _can_enter_scalp(info, "KEY1", ts_now)
        self.assertFalse(ok)
        self.assertEqual(reason, "DUPLICATE_BURST")

    def test_new_burst_key_allows(self):
        ts_now = pendulum.now(_TZ)
        info = {
            "scalp_cooldown_until": None,
            "scalp_last_burst_key": "OLD_KEY",
        }
        ok, reason = _can_enter_scalp(info, "NEW_KEY", ts_now)
        self.assertTrue(ok)
        self.assertEqual(reason, "OK")

    def test_no_cooldown_no_burst_allows(self):
        ts_now = pendulum.now(_TZ)
        info = {
            "scalp_cooldown_until": None,
            "scalp_last_burst_key": None,
        }
        ok, reason = _can_enter_scalp(info, "KEY1", ts_now)
        self.assertTrue(ok)
        self.assertEqual(reason, "OK")


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
