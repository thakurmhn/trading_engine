"""Unit tests for consistent long-premium option position handling."""

from __future__ import annotations

import ast
import unittest
from datetime import datetime

import pandas as pd


class DummyLogger:
    """Logger stub that stores emitted messages."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def info(self, msg: str) -> None:
        self.messages.append(str(msg))

    def warning(self, msg: str) -> None:
        self.messages.append(str(msg))

    def error(self, msg: str) -> None:
        self.messages.append(str(msg))


def _load_functions(*names: str, extra_ns: dict | None = None):
    """Compile selected execution.py functions into an isolated namespace."""
    with open("execution.py", "r", encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    found = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            found[node.name] = node
    for name in names:
        assert name in found, f"{name} not found"

    logger = DummyLogger()
    ns = {
        "pd": pd,
        "dt": datetime,
        "time_zone": None,
        "logging": logger,
        "calculate_cci": lambda _df: pd.Series([0.0]),
        "williams_r": lambda _df: 0.0,
        "momentum_ok": lambda _df, _side: (True, 0.0),
        "OSCILLATOR_EXIT_MODE": "HARD",
        "SCALP_PT_POINTS": 7.0,
        "SCALP_SL_POINTS": 4.0,
        "YELLOW": "",
        "GREEN": "",
        "RED": "",
        "CYAN": "",
        "RESET": "",
        "send_paper_exit_order": lambda *_args, **_kwargs: (True, "PAPER_EXIT"),
        "send_live_exit_order": lambda *_args, **_kwargs: (True, "LIVE_EXIT"),
        "update_order_status": lambda *_args, **_kwargs: None,
    }
    if extra_ns:
        ns.update(extra_ns)

    mod = ast.Module(body=[found[name] for name in names], type_ignores=[])
    exec(compile(mod, filename="<execution_tests>", mode="exec"), ns)
    return {name: ns[name] for name in names}, logger, ns


def _mk_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0, 103.0],
            "close": [101.0, 102.0, 103.0, 104.0],
            "supertrend_bias": ["DOWN", "DOWN", "DOWN", "DOWN"],
            "rsi14": [50.0, 50.0, 50.0, 50.0],
            "time": pd.date_range("2026-02-26 10:00:00", periods=4, freq="3min"),
        }
    )


class OptionPositionHandlingTests(unittest.TestCase):
    def test_call_and_put_position_side_are_long(self):
        funcs, _logger, _ns = _load_functions("_long_position_side")
        side_fn = funcs["_long_position_side"]
        self.assertEqual(side_fn("CALL"), "LONG")
        self.assertEqual(side_fn("PUT"), "LONG")

    def test_sl_exit_audit_logs_long_for_put(self):
        funcs, logger, _ns = _load_functions("check_exit_condition")
        fn = funcs["check_exit_condition"]
        state = {
            "side": "PUT",
            "position_side": "LONG",
            "option_name": "NSE:NIFTY_TESTPE",
            "position_id": "POS_PUT_1",
            "is_open": True,
            "buy_price": 200.0,
            "entry_candle": 0,
            "stop": 195.0,
            "pt": 210.0,
            "tg": 215.0,
            "trail_step": 5.0,
            "source": "BREAKOUT_CPR_BC",
            "regime_context": "LOW",
            "hf_exit_manager": None,
            "partial_booked": False,
        }

        triggered, reason = fn(
            _mk_df(),
            state,
            option_price=190.0,
            option_volume=0.0,
            timestamp="2026-02-26T10:09:00",
        )
        self.assertTrue(triggered)
        self.assertEqual(reason, "SL_HIT")
        audit = [m for m in logger.messages if "[EXIT AUDIT]" in m]
        self.assertTrue(any("option_type=PUT" in m for m in audit))
        self.assertTrue(any("position_side=LONG" in m for m in audit))
        self.assertTrue(any("exit_type=SL" in m for m in audit))

    def test_put_exit_pnl_uses_long_premium_math(self):
        market_df = pd.DataFrame({"ltp": [190.0], "volume": [100.0]}, index=["NSE:NIFTY_TESTPE"])
        funcs, logger, _ns = _load_functions(
            "_get_option_market_snapshot",
            "process_order",
            extra_ns={
                "df": market_df,
                "check_exit_condition": lambda *_args, **_kwargs: (True, "SL_HIT"),
            },
        )
        process_order = funcs["process_order"]

        state = {
            "side": "PUT",
            "position_side": "LONG",
            "option_name": "NSE:NIFTY_TESTPE",
            "position_id": "POS_PUT_2",
            "is_open": True,
            "trade_flag": 1,
            "buy_price": 200.0,
            "quantity": 1,
            "entry_candle": 0,
            "stop": 195.0,
            "pt": 210.0,
            "tg": 215.0,
            "trail_step": 5.0,
            "scalp_mode": False,
            "partial_booked": False,
        }
        put_trade = dict(state)
        put_trade["pnl"] = 0.0
        put_trade["filled_df"] = pd.DataFrame(
            columns=["ticker", "price", "action", "stop_price", "take_profit", "spot_price", "quantity"]
        )
        info = {
            "call_buy": {
                "pnl": 0.0,
                "filled_df": pd.DataFrame(
                    columns=["ticker", "price", "action", "stop_price", "take_profit", "spot_price", "quantity"]
                ),
            },
            "put_buy": put_trade,
            "total_pnl": 0.0,
        }

        triggered, reason = process_order(
            state=info["put_buy"],
            df_slice=_mk_df(),
            info=info,
            spot_price=25000.0,
            account_type="paper",
            mode="REPLAY",
        )

        self.assertTrue(triggered)
        self.assertEqual(reason, "SL_HIT")
        self.assertAlmostEqual(float(info["put_buy"]["pnl"]), -10.0, places=4)
        self.assertTrue(any("PositionSide=LONG" in m for m in logger.messages))


if __name__ == "__main__":
    unittest.main()
