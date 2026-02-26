"""Comprehensive unit tests for MomentumScalpExit behavior.

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
        "time_zone": None,
        "logging": test_logger,
        "calculate_cci": lambda _df: pd.Series([0.0]),
        "williams_r": lambda _df: 0.0,
        "momentum_ok": lambda _df, _side: (True, 0.0),
        "get_option_by_moneyness": lambda *_args, **_kwargs: ("NSE:TESTCE", 0),
        "CALL_MONEYNESS": 0,
        "PUT_MONEYNESS": 0,
        "SCALP_PT_POINTS": 7.0,
        "SCALP_SL_POINTS": 4.0,
        "SCALP_COOLDOWN_MINUTES": 20,
        "SCALP_HISTORY_MAXLEN": 120,
        "OSCILLATOR_EXIT_MODE": "HARD",
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
    if extra_ns:
        ns.update(extra_ns)

    mod = ast.Module(body=[found[name] for name in names], type_ignores=[])
    code = compile(mod, filename="<execution_funcs>", mode="exec")
    exec(code, ns)
    return {name: ns[name] for name in names}, test_logger, ns


def _mk_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0, 103.0],
            "close": [101.0, 102.0, 103.0, 104.0],
            "supertrend_bias": ["UP", "UP", "UP", "UP"],
            "rsi14": [50.0, 50.0, 50.0, 50.0],
            "time": pd.date_range("2026-02-26 10:00:00", periods=4, freq="3min"),
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
        "source": "MOMENTUM_SCALP",
        "regime_context": "SCALP",
        "scalp_mode": True,
        "scalp_pt_points": 5.0,
        "scalp_sl_points": 3.0,
        "hf_exit_manager": FakeHFManager(False),
        "quantity": 25,
        "trade_flag": 1,
    }
    state.update(overrides)
    return state


class MomentumScalpDetectionTests(unittest.TestCase):
    def test_momentum_signal_detects_burst(self):
        funcs, _logger, ns = _load_functions(
            "_update_scalp_premium_history",
            "_rsi_series",
            "_detect_scalp_momentum_signal",
        )
        detect = funcs["_detect_scalp_momentum_signal"]

        info = {"scalp_hist": {"CALL": [], "PUT": []}}
        for px in [200.0, 200.2, 200.1, 200.3, 200.2, 200.4, 200.3, 200.5, 200.4]:
            info["scalp_hist"]["CALL"].append({"ts": pd.Timestamp("2026-02-26 10:00:00"), "price": px})
        ns["df"].loc["NSE:TESTCE", "ltp"] = 209.0

        sig = detect(info, 22500.0, pd.Timestamp("2026-02-26 10:12:00"))
        self.assertIsNotNone(sig)
        self.assertEqual(sig["side"], "CALL")
        self.assertIn("reason", sig)

    def test_momentum_signal_suppresses_noise(self):
        funcs, _logger, ns = _load_functions(
            "_update_scalp_premium_history",
            "_rsi_series",
            "_detect_scalp_momentum_signal",
        )
        detect = funcs["_detect_scalp_momentum_signal"]

        info = {"scalp_hist": {"CALL": [], "PUT": []}}
        noisy = [200.00, 200.05, 199.98, 200.02, 200.01, 200.04, 200.00, 200.03, 199.99]
        for px in noisy:
            info["scalp_hist"]["CALL"].append({"ts": pd.Timestamp("2026-02-26 10:00:00"), "price": px})
        ns["df"].loc["NSE:TESTCE", "ltp"] = 200.01

        sig = detect(info, 22500.0, pd.Timestamp("2026-02-26 10:12:00"))
        self.assertIsNone(sig)


class MomentumScalpControlTests(unittest.TestCase):
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


class MomentumScalpExitTests(unittest.TestCase):
    def test_scalp_pt_exit_immediate_without_min_bars(self):
        funcs, logger, _ns = _load_functions("check_exit_condition")
        fn = funcs["check_exit_condition"]
        state = _base_state()

        triggered, reason = fn(
            _mk_df(),
            state,
            option_price=206.5,
            option_volume=100.0,
            timestamp="2026-02-26T10:09:00",
        )
        self.assertTrue(triggered)
        self.assertEqual(reason, "SCALP_PT_HIT")
        self.assertEqual(state.get("last_exit_type"), "SCALP_PT_HIT")
        self.assertTrue(any("premium_move=" in m for m in logger.messages if "[EXIT AUDIT]" in m))

    def test_scalp_sl_exit_immediate(self):
        funcs, _logger, _ns = _load_functions("check_exit_condition")
        fn = funcs["check_exit_condition"]
        state = _base_state()

        triggered, reason = fn(
            _mk_df(),
            state,
            option_price=196.5,
            option_volume=100.0,
            timestamp="2026-02-26T10:09:00",
        )
        self.assertTrue(triggered)
        self.assertEqual(reason, "SCALP_SL_HIT")
        self.assertEqual(state.get("last_exit_type"), "SCALP_SL_HIT")

    def test_sl_backstop_active_in_scalp_mode(self):
        funcs, _logger, _ns = _load_functions("check_exit_condition")
        fn = funcs["check_exit_condition"]
        state = _base_state(stop=198.0, scalp_pt_points=10.0, scalp_sl_points=10.0)

        triggered, reason = fn(
            _mk_df(),
            state,
            option_price=197.9,
            option_volume=10.0,
            timestamp="2026-02-26T10:09:00",
        )
        self.assertTrue(triggered)
        self.assertEqual(reason, "SL_HIT")
        self.assertEqual(state.get("last_exit_type"), "SL")

    def test_hft_override_wins_over_scalp_pt(self):
        funcs, _logger, _ns = _load_functions("check_exit_condition")
        fn = funcs["check_exit_condition"]
        state = _base_state(
            hf_exit_manager=FakeHFManager(True, "DYNAMIC_TRAILING_STOP"),
            stop=180.0,
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

    def test_trend_exits_not_applied_for_scalp_trade(self):
        funcs, _logger, _ns = _load_functions("check_exit_condition")
        fn = funcs["check_exit_condition"]
        state = _base_state(
            scalp_pt_points=20.0,
            scalp_sl_points=20.0,
            pt=202.0,
            tg=202.0,
        )

        triggered, reason = fn(
            _mk_df(),
            state,
            option_price=202.5,  # would hit PT/TG for trend but not scalp threshold
            option_volume=5.0,
            timestamp="2026-02-26T10:09:00",
        )
        self.assertFalse(triggered)
        self.assertIsNone(reason)

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
        state = _base_state(position_id="POS_AUDIT_1")

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


class MomentumScalpLifecycleTests(unittest.TestCase):
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
