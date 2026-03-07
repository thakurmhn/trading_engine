from __future__ import annotations

import ast
import unittest

import numpy as np
import pandas as pd

from failed_breakout_detector import detect_failed_breakout


def _mk_fb_df(closes, rsi=80.0, cci=180.0, atr=5.0):
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1.0 for c in closes],
            "low": [c - 1.0 for c in closes],
            "close": closes,
            "rsi14": [rsi] * len(closes),
            "cci20": [cci] * len(closes),
            "atr14": [atr] * len(closes),
        }
    )


class FailedBreakoutDetectorTests(unittest.TestCase):
    def test_r3_cross_reversal_detected(self):
        df = _mk_fb_df([90, 95, 100, 110, 120, 125, 131, 129], rsi=82.0, cci=220.0, atr=5.0)
        sig = detect_failed_breakout(df, {"r3": 130.0, "r4": 140.0, "s3": 90.0, "s4": 80.0})
        self.assertIsNotNone(sig)
        self.assertEqual(sig["side"], "PUT")

    def test_s3_cross_reversal_detected(self):
        df = _mk_fb_df([130, 125, 120, 115, 110, 105, 98, 101], rsi=18.0, cci=-240.0, atr=5.0)
        sig = detect_failed_breakout(df, {"r3": 150.0, "r4": 160.0, "s3": 100.0, "s4": 90.0})
        self.assertIsNotNone(sig)
        self.assertEqual(sig["side"], "CALL")

    def test_no_signal_when_no_cross(self):
        df = _mk_fb_df([100, 101, 102, 103, 104, 105, 106, 107], rsi=80.0, cci=220.0, atr=5.0)
        sig = detect_failed_breakout(df, {"r3": 130.0, "r4": 140.0, "s3": 90.0, "s4": 80.0})
        self.assertIsNone(sig)

    def test_no_signal_when_osc_not_extreme(self):
        df = _mk_fb_df([90, 95, 100, 110, 120, 125, 131, 129], rsi=52.0, cci=10.0, atr=5.0)
        sig = detect_failed_breakout(df, {"r3": 130.0, "r4": 140.0, "s3": 90.0, "s4": 80.0})
        self.assertIsNone(sig)


class FailedBreakoutGateIntegrationTests(unittest.TestCase):
    def _load_gate(self):
        with open("execution.py", "r", encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src)
        names = {"_supertrend_alignment_gate", "_trend_entry_quality_gate"}
        body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        ns = {
            "pd": pd,
            "np": np,
            "logging": type("L", (), {"info": lambda *a, **k: None, "debug": lambda *a, **k: None})(),
            "resolve_atr": lambda c: (float(c["atr14"].iloc[-1]), "atr14"),
        }
        exec(compile(ast.Module(body=body, type_ignores=[]), "<fb_gate>", "exec"), ns)
        return ns["_trend_entry_quality_gate"]

    def _mk_df(self, bias, slope):
        return pd.DataFrame(
            {
                "open": [100.0] * 8,
                "high": [101.0] * 8,
                "low": [99.0] * 8,
                "close": [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0],
                "supertrend_bias": [bias] * 8,
                "supertrend_slope": [slope] * 8,
                "supertrend_line": [99.0] * 8,
                "adx14": [30.0] * 8,
                "rsi14": [50.0] * 8,
                "cci20": [0.0] * 8,
                "atr14": [8.0] * 8,
            }
        )

    def test_gate_blocks_entry_in_failed_direction(self):
        gate = self._load_gate()
        c3 = self._mk_df("UP", "UP")
        c15 = self._mk_df("UP", "UP")
        ok, _side, reason, _details = gate(
            c3, c15, "2026-02-26T10:00:00", "NSE:NIFTY",
            failed_breakout_signal={"side": "PUT", "pivot": "R3", "tag": "FAILED_BREAKOUT_REVERSAL"},
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "Failed breakout opposite direction, entry suppressed.")

    def test_gate_allows_reversal_direction(self):
        gate = self._load_gate()
        c3 = self._mk_df("DOWN", "DOWN")
        c15 = self._mk_df("DOWN", "DOWN")
        ok, side, _reason, details = gate(
            c3, c15, "2026-02-26T10:00:00", "NSE:NIFTY",
            failed_breakout_signal={"side": "PUT", "pivot": "R3", "tag": "FAILED_BREAKOUT_REVERSAL"},
        )
        self.assertTrue(ok)
        self.assertEqual(side, "PUT")
        self.assertTrue(details.get("failed_breakout", False))


if __name__ == "__main__":
    unittest.main()
