"""Unit tests for multi-timeframe Supertrend alignment gate."""

from __future__ import annotations

import ast
import unittest

import pandas as pd


class DummyLogger:
    """Simple logger stub for audit-log assertions."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def info(self, msg: str) -> None:
        self.messages.append(str(msg))

    def warning(self, msg: str) -> None:
        self.messages.append(str(msg))

    def error(self, msg: str) -> None:
        self.messages.append(str(msg))


def _load_gate():
    with open("execution.py", "r", encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    fn = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_supertrend_alignment_gate":
            fn = node
            break
    assert fn is not None, "_supertrend_alignment_gate not found"

    logger = DummyLogger()
    ns = {"pd": pd, "logging": logger}
    mod = ast.Module(body=[fn], type_ignores=[])
    exec(compile(mod, filename="<st_gate>", mode="exec"), ns)
    return ns["_supertrend_alignment_gate"], logger


def _mk_3m(bias: str) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "supertrend_bias": [bias],
            "supertrend_slope": ["UP" if bias in {"UP", "BULLISH"} else "DOWN"],
            "supertrend_line": [25000.0],
        }
    )


def _mk_15m(bias: str) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "supertrend_bias": [bias],
            "supertrend_slope": ["UP" if bias in {"UP", "BULLISH"} else "DOWN"],
            "supertrend_line": [25010.0],
        }
    )


class SupertrendAlignmentTests(unittest.TestCase):
    def test_mixed_bias_blocks_entry(self):
        gate, logger = _load_gate()
        aligned, allowed_side, details = gate(
            candles_3m=_mk_3m("UP"),
            candles_15m=_mk_15m("DOWN"),
            timestamp="2026-02-26T10:00:00",
            symbol="NSE:NIFTY50-INDEX",
        )
        self.assertFalse(aligned)
        self.assertIsNone(allowed_side)
        self.assertEqual(details["ST3m_bias"], "BULLISH")
        self.assertEqual(details["ST15m_bias"], "BEARISH")
        self.assertTrue(any("alignment_status=False" in m for m in logger.messages))

    def test_both_bullish_allows_long(self):
        gate, _logger = _load_gate()
        aligned, allowed_side, details = gate(
            candles_3m=_mk_3m("UP"),
            candles_15m=_mk_15m("UP"),
            timestamp="2026-02-26T10:03:00",
            symbol="NSE:NIFTY50-INDEX",
        )
        self.assertTrue(aligned)
        self.assertEqual(allowed_side, "CALL")
        self.assertTrue(details["alignment_status"])

    def test_both_bearish_allows_short(self):
        gate, _logger = _load_gate()
        aligned, allowed_side, details = gate(
            candles_3m=_mk_3m("DOWN"),
            candles_15m=_mk_15m("DOWN"),
            timestamp="2026-02-26T10:06:00",
            symbol="NSE:NIFTY50-INDEX",
        )
        self.assertTrue(aligned)
        self.assertEqual(allowed_side, "PUT")
        self.assertTrue(details["alignment_status"])


if __name__ == "__main__":
    unittest.main()
