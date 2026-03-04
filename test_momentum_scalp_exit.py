"""Comprehensive unit tests for dip-buying scalp logic.

These tests compile selected functions from ``execution.py`` via AST extraction
to avoid importing the full runtime module with broker/session side effects.
"""

from __future__ import annotations

import ast
import unittest
from datetime import datetime, timedelta

import pandas as pd


class DummyLogger:
    """Simple logger stub that captures messages for audit assertions."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        self.messages.append(str(msg))

    def warning(self, msg: str) -> None:
        self.messages.append(str(msg))

    def error(self, msg: str) -> None:
        self.messages.append(str(msg))


class FakeHFManager:
    """Stub HFT manager for precedence tests."""

    def __init__(self, should_exit: bool, reason: str = "DYNAMIC_TRAILING_STOP") -> None:
        self._should_exit = should_exit
        self.last_reason = reason

    def check_exit(self, *_args, **_kwargs) -> bool:
        return self._should_exit


def _load_functions(*names: str, extra_ns: dict | None = None):
    """Compile selected functions from execution.py into isolated namespace."""
    with open("execution.py", "r", encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    found = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            found[node.name] = node
    for name in names:
        assert name in found, f"{name} not found"

    test_logger = DummyLogger()
    ns = {
        "pd": pd,
        "dt": datetime,
        "timedelta": timedelta,
        "logging": test_logger,
        "calculate_cci": lambda _df: pd.Series([0.0]),
        "williams_r": lambda _df: 0.0,
        "momentum_ok": lambda _df, _side: (True, 0.0),
        "get_option_by_moneyness": lambda *_args, **_kwargs: ("NSE:TESTCE", 0),
        "time_zone": None,
        # Mock dependencies for the new scalp logic
        "calculate_traditional_pivots": lambda *args: {"s1": 22400, "r1": 22600, "pivot": 22500},
        "CALL_MONEYNESS": 0,
        "PUT_MONEYNESS": 0,
        "SCALP_PT_POINTS": 7.0,
        "SCALP_SL_POINTS": 4.0,
        "SCALP_COOLDOWN_MINUTES": 20,
        "SCALP_HISTORY_MAXLEN": 120,
        "OSCILLATOR_EXIT_MODE": "HARD",
        "SCALP_MIN_HOLD_BARS": 2,
        "SCALP_EXTREME_MOVE_ATR_MULT": 0.90,
        "PAPER_SLIPPAGE_POINTS": 4.0,
        "YELLOW": "",
        "GREEN": "",
        "RED": "",
        "CYAN": "",
        "RESET": "",
        "send_paper_exit_order": lambda *_args, **_kwargs: (True, "PAPER_ORDER"),
        "send_live_exit_order": lambda *_args, **_kwargs: (True, "LIVE_ORDER"),
        "update_order_status": lambda *_args, **_kwargs: None,
        "df": pd.DataFrame(
            {"ltp": [210.0], "volume": [1000.0]},
            index=["NSE:TESTCE"],
        ),
    }
    mod = ast.Module(body=[found[name] for name in names], type_ignores=[])
    code = compile(mod, filename="<execution_funcs>", mode="exec")
    exec(code, ns)
    if extra_ns:
        ns.update(extra_ns)
    return {name: ns[name] for name in names}, test_logger, ns


def _mk_df(rows: int = 4) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [100.0] * rows,
            "high": [101.0] * rows,
            "low": [99.0] * rows,
            "close": [100.5] * rows,
            "supertrend_bias": ["UP"] * rows,
            "rsi14": [50.0] * rows,
            "time": pd.to_datetime(pd.date_range("2026-02-26 10:00:00", periods=rows, freq="3min")),
        }
    )


def _base_state(**overrides) -> dict:
    state = {
        "side": "CALL",
        "option_name": "NSE:TESTCE",
        "position_id": "POS_SCALP_1",
        "is_open": True,
        "buy_price": 200.0,
        "entry_candle": len(_mk_df()) - 1,
        "stop": 195.0,
        "pt": 208.0,
        "tg": 210.0,
        "trail_step": 5.0,
        "source": "SCALP_BUY_DIP",
        "regime_context": "SCALP",
        "scalp_mode": True,
        "scalp_pt_points": 5.0,
        "hf_exit_manager": FakeHFManager(False),
        "quantity": 25,
        "trade_flag": 1,
    }
    state.update(overrides)
    return state


class ScalpDipBuyDetectionTests(unittest.TestCase):
    def test_buy_on_dip_at_s1(self):
        funcs, logger, _ns = _load_functions("_detect_scalp_dip_rally_signal")
        detect = funcs["_detect_scalp_dip_rally_signal"]
        candles = _mk_df()
        # Make last candle dip to S1 and reject
        candles.loc[candles.index[-1], "low"] = 22398.0  # S1 is 22400
        candles.loc[candles.index[-1], "close"] = 22405.0
        candles.loc[candles.index[-1], "high"] = 22408.0
        trad_levels = {"s1": 22400.0, "r1": 22600.0, "pivot": 22500.0}

        sig = detect(candles, trad_levels, atr=20.0)
        self.assertIsNotNone(sig)
        self.assertEqual(sig["side"], "CALL")
        self.assertIn("reason", sig)
        self.assertEqual(sig["reason"], "SCALP_BUY_DIP_S1")
        self.assertIn("stop", sig)
        self.assertLess(sig["stop"], 22400.0)  # SL must be below S1
        self.assertTrue(any("[SCALP_BUY_DIP]" in m for m in logger.messages))

    def test_sell_on_rally_at_r1(self):
        funcs, logger, _ns = _load_functions("_detect_scalp_dip_rally_signal")
        detect = funcs["_detect_scalp_dip_rally_signal"]
        candles = _mk_df()
        # Make last candle rally to R1 and reject
        candles.loc[candles.index[-1], "high"] = 22602.0  # R1 is 22600
        candles.loc[candles.index[-1], "close"] = 22595.0
        candles.loc[candles.index[-1], "low"] = 22592.0
        trad_levels = {"s1": 22400.0, "r1": 22600.0, "pivot": 22500.0}

        info = {"scalp_hist": {"CALL": [], "PUT": []}}
        noisy = [200.00, 200.05, 199.98, 200.02, 200.01, 200.04, 200.00, 200.03, 199.99]
        for px in noisy:
            info["scalp_hist"]["CALL"].append({"ts": pd.Timestamp("2026-02-26 10:00:00"), "price": px})
        _ns["df"].loc["NSE:TESTCE", "ltp"] = 200.01
        sig = detect(candles, trad_levels, atr=20.0)
        self.assertIsNotNone(sig)
        self.assertEqual(sig["side"], "PUT")
        self.assertEqual(sig["reason"], "SCALP_SELL_RALLY_R1")
        self.assertIn("stop", sig)
        self.assertGreater(sig["stop"], 22600.0)  # SL must be above R1
        self.assertTrue(any("[SCALP_SELL_RALLY]" in m for m in logger.messages))
    def test_no_signal_in_mid_range(self):
        funcs, _logger, _ns = _load_functions("_detect_scalp_dip_rally_signal")
        detect = funcs["_detect_scalp_dip_rally_signal"]
        candles = _mk_df()
        # Price is well inside S1/R1 — low stays above S1+atr_buf, high stays below R1-atr_buf
        candles.loc[candles.index[-1], "low"] = 22420.0
        candles.loc[candles.index[-1], "high"] = 22480.0
        candles.loc[candles.index[-1], "close"] = 22450.0
        trad_levels = {"s1": 22400.0, "r1": 22600.0, "pivot": 22500.0}
        sig = detect(candles, trad_levels, atr=20.0)
        self.assertIsNone(sig)


class ScalpControlTests(unittest.TestCase):
    def test_cooldown_blocks_duplicate_entries(self):
        funcs, _logger, _ns = _load_functions("_can_enter_scalp")
        can_enter = funcs["_can_enter_scalp"]
        now = pd.Timestamp("2026-02-26 10:15:00")
        info = {"scalp_cooldown_until": pd.Timestamp("2026-02-26 10:35:00")}

        ok, reason = can_enter(info, "CALL:burst", now)
        self.assertFalse(ok)
        self.assertEqual(reason, "COOLDOWN")

    def test_reentry_allowed_after_cooldown(self):
        funcs, _logger, _ns = _load_functions("_can_enter_scalp")
        can_enter = funcs["_can_enter_scalp"]
        now = pd.Timestamp("2026-02-26 10:36:00")
        info = {"scalp_cooldown_until": pd.Timestamp("2026-02-26 10:35:00")}

        ok, reason = can_enter(info, "CALL:burst_new", now)
        self.assertTrue(ok)
        self.assertEqual(reason, "OK")


class ScalpExitLogicTests(unittest.TestCase):
    def test_scalp_pt_suppressed_before_min_bars(self):
        funcs, logger, _ns = _load_functions("check_exit_condition")
        fn = funcs["check_exit_condition"]
        state = _base_state(entry_candle=len(_mk_df()) - 1)  # bars_held=0

        triggered, reason = fn(
            _mk_df(),
            state,
            option_price=206.5,
            option_volume=100.0,
            timestamp="2026-02-26T10:09:00",
        )
        self.assertFalse(triggered)
        self.assertIsNone(reason)

    def test_scalp_pt_fires_after_min_bars(self):
        funcs, _logger, _ns = _load_functions("check_exit_condition")
        fn = funcs["check_exit_condition"]
        state = _base_state(entry_candle=0)  # bars_held=3 >= 2

        triggered, reason = fn(
            _mk_df(),
            state,
            option_price=206.0,  # PT hit
            option_volume=100.0,
            timestamp="2026-02-26T10:09:00",
        )
        self.assertTrue(triggered)
        self.assertEqual(reason, "SCALP_PT_HIT")

    def test_scalp_sl_points_logic_is_removed(self):
        """The old SCALP_SL_HIT based on fixed points should no longer fire."""
        funcs, _logger, _ns = _load_functions("check_exit_condition")
        fn = funcs["check_exit_condition"]
        state = _base_state(stop=180.0)  # Main SL is far away

        # This would have triggered the old SCALP_SL_HIT
        triggered, _reason = fn(
            _mk_df(), state, option_price=196.5, option_volume=100.0, timestamp="2026-02-26T10:09:00"
        )
        self.assertFalse(triggered)

    def test_sl_backstop_active_in_scalp_mode(self):
        """The main ATR-based SL should still fire for scalp trades."""
        funcs, _logger, _ns = _load_functions("check_exit_condition")
        fn = funcs["check_exit_condition"]
        state = _base_state(stop=198.0, scalp_pt_points=10.0, entry_candle=0)

        triggered, reason = fn(
            _mk_df(), state, option_price=197.9, option_volume=10.0,
            timestamp="2026-02-26T10:09:00"
        )
        self.assertTrue(triggered)
        self.assertEqual(reason, "SL_HIT")
        self.assertEqual(state.get("last_exit_type"), "SL")

    def test_hft_override_wins_over_scalp_pt(self):
        funcs, _logger, _ns = _load_functions("check_exit_condition")
        fn = funcs["check_exit_condition"]
        state = _base_state(
            hf_exit_manager=FakeHFManager(True, "DYNAMIC_TRAILING_STOP"),
            stop=180.0,  # Main SL is not hit
            entry_candle=1,  # bars_held=2, so premature-exit gate does not suppress HFT
        )

        triggered, reason = fn(
            _mk_df(),
            state,
            option_price=206.0,
            option_volume=100.0,
            timestamp="2026-02-26T10:09:00",
        )
        self.assertTrue(triggered)
        self.assertEqual(reason, "DYNAMIC_TRAILING_STOP")
        self.assertEqual(state.get("last_exit_type"), "HFT")

    def test_hft_can_fire_for_scalp_trade(self):
        """Verify HFT logic can fire for scalp trades (fall-through)."""
        funcs, _logger, _ns = _load_functions("check_exit_condition")
        fn = funcs["check_exit_condition"]
        state = _base_state(
            scalp_pt_points=20.0,  # PT not hit
            hf_exit_manager=FakeHFManager(True, "MOMENTUM_EXHAUSTION"),
            entry_candle=0,  # bars_held=3, so HFT is active
        )

        triggered, reason = fn(
            _mk_df(), state, option_price=202.5, option_volume=5.0,
            timestamp="2026-02-26T10:09:00"
        )
        self.assertTrue(triggered)
        self.assertEqual(reason, "MOMENTUM_EXHAUSTION")

    def test_duplicate_exit_rejected_when_closed(self):
        funcs, _logger, _ns = _load_functions("check_exit_condition")
        fn = funcs["check_exit_condition"]
        state = _base_state(is_open=False)

        triggered, reason = fn(
            _mk_df(),
            state,
            option_price=210.0,
            option_volume=0.0,
            timestamp="2026-02-26T10:09:00",
        )
        self.assertFalse(triggered)
        self.assertIsNone(reason)

    def test_audit_log_contains_scalp_exit_fields(self):
        funcs, logger, _ns = _load_functions("check_exit_condition")
        fn = funcs["check_exit_condition"]
        state = _base_state(position_id="POS_AUDIT_1", entry_candle=0)

        triggered, reason = fn(
            _mk_df(),
            state,
            option_price=206.0,
            option_volume=10.0,
            timestamp="2026-02-26T10:09:00",
        )
        self.assertTrue(triggered)
        self.assertEqual(reason, "SCALP_PT_HIT")
        audit_lines = [m for m in logger.messages if "[EXIT AUDIT]" in m]
        self.assertTrue(any("exit_type=SCALP_PT_HIT" in m for m in audit_lines))
        self.assertTrue(any("position_id=POS_AUDIT_1" in m for m in audit_lines))
        self.assertTrue(any("premium_move=" in m for m in audit_lines))


class ScalpLifecycleTests(unittest.TestCase):
    def test_process_order_transitions_open_hold_exit_and_sets_cooldown(self):
        df_market = pd.DataFrame({"ltp": [207.5], "volume": [250.0]}, index=["NSE:TESTCE"])
        funcs, _logger, _ns = _load_functions(
            "_get_option_market_snapshot",
            "check_exit_condition",
            "process_order",
            extra_ns={
                "df": df_market,
                "check_exit_condition": lambda *_args, **_kwargs: (True, "SCALP_PT_HIT"),
            },
        )
        process_order = funcs["process_order"]

        state = _base_state(
            option_name="NSE:TESTCE",
            position_id="POS_FLOW_1",
            scalp_mode=True,
        )
        info = {
            "call_buy": {"pnl": 0.0, "filled_df": pd.DataFrame()},
            "put_buy": {"pnl": 0.0, "filled_df": pd.DataFrame()},
            "total_pnl": 0.0,
        }
        info["call_buy"].update(state)
        info["call_buy"]["filled_df"] = pd.DataFrame(
            columns=["ticker", "price", "action", "stop_price", "take_profit", "spot_price", "quantity"]
        )

        triggered, reason = process_order(
            state=info["call_buy"],
            df_slice=_mk_df(),
            info=info,
            spot_price=22500.0,
            account_type="paper",
            mode="REPLAY",
        )

        self.assertTrue(triggered)
        self.assertEqual(reason, "SCALP_PT_HIT")
        self.assertFalse(info["call_buy"]["is_open"])
        self.assertEqual(info["call_buy"]["lifecycle_state"], "EXIT")
        self.assertIsNotNone(info.get("scalp_cooldown_until"))

    def test_process_order_rejects_stale_exit_when_closed(self):
        funcs, _logger, _ns = _load_functions(
            "_get_option_market_snapshot",
            "check_exit_condition",
            "process_order",
            extra_ns={"df": pd.DataFrame({"ltp": [210.0]}, index=["NSE:TESTCE"])},
        )
        process_order = funcs["process_order"]
        closed_state = _base_state(is_open=False, option_name="NSE:TESTCE")
        info = {
            "call_buy": {"pnl": 0.0, "filled_df": pd.DataFrame()},
            "put_buy": {"pnl": 0.0, "filled_df": pd.DataFrame()},
            "total_pnl": 0.0,
        }
        info["call_buy"].update(closed_state)
        info["call_buy"]["filled_df"] = pd.DataFrame(
            columns=["ticker", "price", "action", "stop_price", "take_profit", "spot_price", "quantity"]
        )

        triggered, reason = process_order(
            state=info["call_buy"],
            df_slice=_mk_df(),
            info=info,
            spot_price=22500.0,
            account_type="paper",
            mode="REPLAY",
        )
        self.assertFalse(triggered)
        self.assertIsNone(reason)


if __name__ == "__main__":
    unittest.main()
