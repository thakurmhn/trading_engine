"""Unit tests for entry quality filters and premature-exit safeguards."""

from __future__ import annotations

import ast
import unittest
from datetime import datetime

import numpy as np
import pandas as pd


class DummyLogger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def info(self, msg: str) -> None:
        self.messages.append(str(msg))

    def warning(self, msg: str) -> None:
        self.messages.append(str(msg))

    def error(self, msg: str) -> None:
        self.messages.append(str(msg))


class FakeHFManager:
    def __init__(self, should_exit: bool, reason: str) -> None:
        self._should_exit = should_exit
        self.last_reason = reason

    def check_exit(self, *_args, **_kwargs) -> bool:
        return self._should_exit


def _load_functions(*names: str):
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
        "np": np,
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
    }
    mod = ast.Module(body=[found[n] for n in names], type_ignores=[])
    exec(compile(mod, filename="<refinement_tests>", mode="exec"), ns)
    return {n: ns[n] for n in names}, logger


def _mk_df(st_bias: str, st_slope: str, adx: float, rsi: float, cci: float) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0, 103.0],
            "high": [101.0, 102.0, 103.0, 104.0],
            "low": [99.0, 100.0, 101.0, 102.0],
            "close": [100.5, 101.5, 102.5, 103.5],
            "supertrend_bias": [st_bias] * 4,
            "supertrend_slope": [st_slope] * 4,
            "supertrend_line": [100.0, 100.5, 101.0, 101.5],
            "adx14": [adx] * 4,
            "rsi14": [rsi] * 4,
            "cci20": [cci] * 4,
        }
    )


class EntryQualityGateTests(unittest.TestCase):
    def test_slope_mismatch_blocks_entry(self):
        funcs, _logger = _load_functions("_supertrend_alignment_gate", "_trend_entry_quality_gate")
        gate = funcs["_trend_entry_quality_gate"]
        c3 = _mk_df("UP", "DOWN", 30.0, 50.0, 0.0)
        c15 = _mk_df("UP", "DOWN", 30.0, 50.0, 0.0)
        ok, _side, reason, details = gate(c3, c15, "2026-02-26T10:00:00", "NSE:NIFTY")
        self.assertFalse(ok)
        self.assertEqual(reason, "Slope mismatch, entry suppressed.")
        self.assertTrue(np.isfinite(float(details["adx14"])))
        self.assertTrue(np.isfinite(float(details["rsi14"])))
        self.assertTrue(np.isfinite(float(details["cci20"])))

    def test_weak_adx_blocks_entry(self):
        funcs, _logger = _load_functions("_supertrend_alignment_gate", "_trend_entry_quality_gate")
        gate = funcs["_trend_entry_quality_gate"]
        c3 = _mk_df("UP", "UP", 20.0, 50.0, 0.0)
        c15 = _mk_df("UP", "UP", 30.0, 50.0, 0.0)
        ok, _side, reason, _details = gate(c3, c15, "2026-02-26T10:00:00", "NSE:NIFTY")
        self.assertFalse(ok)
        self.assertEqual(reason, "Weak trend strength, entry suppressed.")

    def test_osc_extreme_blocks_entry(self):
        funcs, _logger = _load_functions("_supertrend_alignment_gate", "_trend_entry_quality_gate")
        gate = funcs["_trend_entry_quality_gate"]
        c3 = _mk_df("UP", "UP", 30.0, 70.0, 0.0)
        c15 = _mk_df("UP", "UP", 30.0, 50.0, 0.0)
        ok, _side, reason, _details = gate(c3, c15, "2026-02-26T10:00:00", "NSE:NIFTY")
        self.assertFalse(ok)
        self.assertEqual(reason, "Oscillator extreme, entry suppressed.")


class ExitPrematureSafeguardTests(unittest.TestCase):
    def test_momentum_exhaustion_suppressed_before_two_bars(self):
        funcs, logger = _load_functions("check_exit_condition")
        fn = funcs["check_exit_condition"]
        df_slice = pd.DataFrame(
            {
                "open": [99.0, 100.0, 101.0],
                "high": [101.0, 102.0, 103.0],
                "low": [98.0, 99.0, 100.0],
                "close": [100.0, 101.0, 102.0],
                "supertrend_bias": ["UP"] * 3,
                "rsi14": [50.0] * 3,
            }
        )
        state = {
            "side": "CALL",
            "position_side": "LONG",
            "option_name": "NSE:NIFTY_TESTCE",
            "position_id": "POS1",
            "is_open": True,
            "buy_price": 200.0,
            "entry_candle": len(df_slice) - 2,  # bars_held=1
            "stop": 150.0,
            "pt": 210.0,
            "tg": 215.0,
            "trail_step": 5.0,
            "source": "X",
            "regime_context": "LOW",
            "hf_exit_manager": FakeHFManager(True, "MOMENTUM_EXHAUSTION"),
            "partial_booked": False,
        }
        triggered, reason = fn(df_slice, state, option_price=206.0, option_volume=0.0, timestamp="2026-02-26T10:00:00")
        self.assertFalse(triggered)
        self.assertIsNone(reason)
        self.assertTrue(any("Premature exit suppressed, minimum hold enforced." in m for m in logger.messages))


if __name__ == "__main__":
    unittest.main()
