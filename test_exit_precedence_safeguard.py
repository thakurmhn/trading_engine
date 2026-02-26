"""Unit tests for exit precedence and lifecycle safeguards.

These tests isolate `check_exit_condition` via AST extraction to avoid importing
the full `execution.py` module (which has runtime side effects in this project).
"""

from __future__ import annotations

import ast
import unittest
from datetime import datetime

import pandas as pd


class DummyLogger:
    """Simple logger stub that captures info/warning/error messages."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def info(self, msg: str) -> None:
        self.messages.append(str(msg))

    def warning(self, msg: str) -> None:
        self.messages.append(str(msg))

    def error(self, msg: str) -> None:
        self.messages.append(str(msg))


class FakeHFManager:
    """Stub HFT manager."""

    def __init__(self, should_exit: bool, reason: str = "DYNAMIC_TRAILING_STOP") -> None:
        self._should_exit = should_exit
        self.last_reason = reason

    def check_exit(self, *_args, **_kwargs) -> bool:
        return self._should_exit


def _load_functions(*names):
    """Compile selected functions from execution.py into a test namespace."""
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
        "time_zone": None,
        "logging": test_logger,
        "calculate_cci": lambda _df: pd.Series([0.0]),
        "williams_r": lambda _df: 0.0,
        "momentum_ok": lambda _df, _side: (True, 0.0),
        "OSCILLATOR_EXIT_MODE": "HARD",
        "YELLOW": "",
        "GREEN": "",
        "RED": "",
        "CYAN": "",
        "RESET": "",
        "SCALP_PT_POINTS": 7.0,
        "SCALP_SL_POINTS": 4.0,
    }
    mod = ast.Module(body=[found[name] for name in names], type_ignores=[])
    code = compile(mod, filename="<check_exit_condition>", mode="exec")
    exec(code, ns)
    return {name: ns[name] for name in names}, test_logger


def _mk_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0, 103.0],
            "close": [101.0, 102.0, 103.0, 104.0],
            "supertrend_bias": ["UP", "UP", "UP", "UP"],
            "rsi14": [50.0, 50.0, 50.0, 50.0],
        }
    )


class ExitPrecedenceSafeguardTests(unittest.TestCase):
    def test_hft_overrides_sl_when_both_true(self):
        """If HFT and SL are both true, HFT must win by precedence."""
        funcs, logger = _load_functions("check_exit_condition")
        fn = funcs["check_exit_condition"]
        state = {
            "side": "CALL",
            "option_name": "NSE:NIFTY_TESTCE",
            "position_id": "POS_1",
            "is_open": True,
            "buy_price": 200.0,
            "entry_candle": 0,
            "stop": 210.0,  # also true because current_ltp=100 <= 210
            "pt": 220.0,
            "tg": 230.0,
            "trail_step": 5.0,
            "source": "BREAKOUT_CPR_TC",
            "regime_context": "LOW",
            "hf_exit_manager": FakeHFManager(True, "DYNAMIC_TRAILING_STOP"),
        }
        triggered, reason = fn(
            _mk_df(),
            state,
            option_price=100.0,
            option_volume=0.0,
            timestamp="2026-02-26T10:00:00",
        )
        self.assertTrue(triggered)
        self.assertEqual(reason, "DYNAMIC_TRAILING_STOP")
        self.assertEqual(state.get("last_exit_type"), "HFT")
        self.assertTrue(
            any("position_id=POS_1" in m for m in logger.messages if "[EXIT AUDIT]" in m)
        )

    def test_closed_position_rejects_exit(self):
        """Duplicate/stale exits must be rejected when is_open is False."""
        funcs, _logger = _load_functions("check_exit_condition")
        fn = funcs["check_exit_condition"]
        state = {
            "side": "CALL",
            "option_name": "NSE:NIFTY_TESTCE",
            "position_id": "POS_2",
            "is_open": False,
            "buy_price": 200.0,
            "entry_candle": 0,
            "stop": 190.0,
            "pt": 220.0,
            "tg": 230.0,
            "trail_step": 5.0,
            "source": "BREAKOUT_CPR_TC",
            "regime_context": "LOW",
            "hf_exit_manager": FakeHFManager(True, "DYNAMIC_TRAILING_STOP"),
        }
        triggered, reason = fn(
            _mk_df(),
            state,
            option_price=205.0,
            option_volume=0.0,
            timestamp="2026-02-26T10:01:00",
        )
        self.assertFalse(triggered)
        self.assertIsNone(reason)

    def test_scalp_exit_fires_pt_without_min_bar(self):
        """Scalp trades must exit immediately on PT even before min bars."""
        funcs, _logger = _load_functions("check_exit_condition")
        fn = funcs["check_exit_condition"]
        state = {
            "side": "CALL",
            "option_name": "NSE:NIFTY_TESTCE",
            "position_id": "POS_3",
            "is_open": True,
            "buy_price": 200.0,
            "entry_candle": len(_mk_df()) - 1,  # bars_held=0
            "stop": 195.0,
            "pt": 207.0,
            "tg": 207.0,
            "trail_step": 0.0,
            "source": "MOMENTUM_SCALP",
            "regime_context": "SCALP",
            "scalp_mode": True,
            "scalp_pt_points": 5.0,
            "scalp_sl_points": 3.0,
            "hf_exit_manager": FakeHFManager(False),
        }
        triggered, reason = fn(
            _mk_df(),
            state,
            option_price=206.0,  # +6 points move
            option_volume=0.0,
            timestamp="2026-02-26T10:02:00",
        )
        self.assertTrue(triggered)
        self.assertEqual(reason, "SCALP_PT_HIT")
        self.assertEqual(state.get("last_exit_type"), "SCALP_PT_HIT")

    def test_scalp_cooldown_blocks_duplicate_burst(self):
        """Cooldown and duplicate burst key must prevent re-entry."""
        funcs, _logger = _load_functions("_can_enter_scalp")
        can_enter = funcs["_can_enter_scalp"]
        now = pd.Timestamp("2026-02-26T10:10:00")

        info_cd = {"scalp_cooldown_until": pd.Timestamp("2026-02-26T10:20:00")}
        ok, reason = can_enter(info_cd, "CALL:burst_1", now)
        self.assertFalse(ok)
        self.assertEqual(reason, "COOLDOWN")

        info_dup = {"scalp_cooldown_until": None, "scalp_last_burst_key": "CALL:burst_1"}
        ok, reason = can_enter(info_dup, "CALL:burst_1", now)
        self.assertFalse(ok)
        self.assertEqual(reason, "DUPLICATE_BURST")


if __name__ == "__main__":
    unittest.main()
