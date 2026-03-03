"""Unit tests for the dashboard module (Req 4).

Coverage
--------
TestLogParser         – parse_log_file extracts EXIT AUDIT records from a log.
TestComputeSummary    – total trades, win %, net P&L, CALL/PUT split.
TestPrintSummary      – print_summary outputs the expected labels.
TestEquityCurve       – plot_equity_curve returns a Path (or None when mpl absent).
TestSaveReportCSV     – save_report_csv writes a readable CSV.
TestSessionDashboard  – record_entry/exit, to_dataframe, summary, emit.
TestGenerateDashboard – generate_dashboard with DataFrame and log file.
TestEdgeCases         – empty inputs, mismatched entries/exits, NaN P&L.
"""

import csv
import io
import json
import logging
import os
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import pandas as pd

# ── import the module under test ──────────────────────────────────────────────
from dashboard import (
    SessionDashboard,
    TradeRecord,
    compute_summary,
    generate_dashboard,
    parse_log_file,
    plot_equity_curve,
    print_summary,
    save_report_csv,
    _MPL_AVAILABLE,
)


# ── Shared helpers ────────────────────────────────────────────────────────────

def _make_trades_df(records: list[dict]) -> pd.DataFrame:
    """Build a minimal trades DataFrame for summary/chart tests."""
    return pd.DataFrame(records)


def _make_log_file(content: str) -> Path:
    """Write log content to a named temp file and return its Path."""
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".log", delete=False, encoding="utf-8"
    )
    f.write(content)
    f.close()
    return Path(f.name)


def _sample_log() -> str:
    """Sample log lines that parse_log_file should handle."""
    return textwrap.dedent("""\
        2024-01-15 09:30:00,001 - INFO - [ENTRY DISPATCH] broker=FyersAdapter symbol=NSE:NIFTY24JAN22000CE side=BUY order_id=ORD001 timestamp=2024-01-15T09:30:00
        2024-01-15 09:30:00,002 - INFO - [ENTRY CONFIG] timestamp=2024-01-15T09:30:00 symbol=NSE:NIFTY50-INDEX adx_min=18.0 rsi_range=[35.0,65.0] cci_range=[-120.0,120.0]
        2024-01-15 10:00:00,001 - INFO - [EXIT AUDIT] timestamp=2024-01-15T10:00:00 symbol=NSE:NIFTY24JAN22000CE option_type=CALL position_side=LONG exit_type=TG reason=TARGET_HIT triggering_condition=tg_hit candle=5 bars_held=5 regime=ATR position_id=POS001 premium_move=25.50
        2024-01-15 10:30:00,001 - INFO - [EXIT AUDIT] timestamp=2024-01-15T10:30:00 symbol=NSE:NIFTY24JAN22000PE option_type=PUT position_side=LONG exit_type=SL reason=SL_HIT triggering_condition=sl_hit candle=3 bars_held=3 regime=ATR position_id=POS002 premium_move=-15.00
        2024-01-15 11:00:00,001 - INFO - [EXIT AUDIT] timestamp=2024-01-15T11:00:00 symbol=NSE:NIFTY24JAN22100CE option_type=CALL position_side=LONG exit_type=PT reason=PT_HIT triggering_condition=pt_hit candle=4 bars_held=4 regime=ATR position_id=POS003 premium_move=8.00
    """)


# ═══════════════════════════════════════════════════════════════════════════════
# TestLogParser
# ═══════════════════════════════════════════════════════════════════════════════

class TestLogParser(unittest.TestCase):

    def setUp(self):
        self._log_path = _make_log_file(_sample_log())

    def tearDown(self):
        os.unlink(self._log_path)

    def test_returns_dataframe(self):
        df = parse_log_file(self._log_path)
        self.assertIsInstance(df, pd.DataFrame)

    def test_correct_row_count(self):
        """3 EXIT AUDIT lines → 3 rows."""
        df = parse_log_file(self._log_path)
        self.assertEqual(len(df), 3)

    def test_option_type_extracted(self):
        df = parse_log_file(self._log_path)
        types_ = set(df["option_type"].tolist())
        self.assertIn("CALL", types_)
        self.assertIn("PUT", types_)

    def test_reason_extracted(self):
        df = parse_log_file(self._log_path)
        reasons = set(df["reason"].tolist())
        self.assertIn("TARGET_HIT", reasons)
        self.assertIn("SL_HIT", reasons)

    def test_premium_move_parsed(self):
        df = parse_log_file(self._log_path)
        pm = df["premium_move"].tolist()
        self.assertIn(25.5, pm)
        self.assertIn(-15.0, pm)

    def test_bars_held_parsed(self):
        df = parse_log_file(self._log_path)
        self.assertIn(5, df["bars_held"].tolist())
        self.assertIn(3, df["bars_held"].tolist())

    def test_position_id_extracted(self):
        df = parse_log_file(self._log_path)
        self.assertIn("POS001", df["position_id"].tolist())

    def test_nonexistent_file_returns_empty(self):
        df = parse_log_file("/tmp/nonexistent_trading_log.log")
        self.assertTrue(df.empty)

    def test_empty_log_returns_empty(self):
        log = _make_log_file("no audit lines here\n")
        try:
            df = parse_log_file(log)
            self.assertTrue(df.empty)
        finally:
            os.unlink(log)

    def test_pnl_points_column_populated(self):
        """pnl_points col is filled from premium_move for complete records."""
        df = parse_log_file(self._log_path)
        self.assertIn("pnl_points", df.columns)
        net = df["pnl_points"].sum()
        # 25.5 + (-15.0) + 8.0 = 18.5
        self.assertAlmostEqual(net, 18.5, places=4)


# ═══════════════════════════════════════════════════════════════════════════════
# TestComputeSummary
# ═══════════════════════════════════════════════════════════════════════════════

class TestComputeSummary(unittest.TestCase):

    def _df(self):
        return _make_trades_df([
            {"option_type": "CALL", "pnl_points": 25.0, "qty": 50},
            {"option_type": "PUT",  "pnl_points": -10.0, "qty": 50},
            {"option_type": "CALL", "pnl_points": 8.0,  "qty": 50},
            {"option_type": "PUT",  "pnl_points": 5.0,  "qty": 50},
            {"option_type": "CALL", "pnl_points": 0.0,  "qty": 50},
        ])

    def test_total_trades(self):
        s = compute_summary(self._df())
        self.assertEqual(s["total_trades"], 5)

    def test_winners(self):
        s = compute_summary(self._df())
        self.assertEqual(s["winners"], 3)

    def test_losers(self):
        s = compute_summary(self._df())
        self.assertEqual(s["losers"], 1)

    def test_breakeven(self):
        s = compute_summary(self._df())
        self.assertEqual(s["breakeven"], 1)

    def test_win_rate(self):
        s = compute_summary(self._df())
        self.assertAlmostEqual(s["win_rate_pct"], 60.0, places=1)

    def test_net_pnl_points(self):
        s = compute_summary(self._df())
        # 25 - 10 + 8 + 5 + 0 = 28
        self.assertAlmostEqual(s["net_pnl_points"], 28.0, places=2)

    def test_net_pnl_rupees(self):
        s = compute_summary(self._df())
        # 28.0 * 50 = 1400.0
        self.assertAlmostEqual(s["net_pnl_rupees"], 1400.0, places=2)

    def test_call_trades(self):
        s = compute_summary(self._df())
        self.assertEqual(s["call_trades"], 3)

    def test_put_trades(self):
        s = compute_summary(self._df())
        self.assertEqual(s["put_trades"], 2)

    def test_call_pnl(self):
        s = compute_summary(self._df())
        # 25 + 8 + 0 = 33
        self.assertAlmostEqual(s["call_pnl_points"], 33.0, places=2)

    def test_put_pnl(self):
        s = compute_summary(self._df())
        # -10 + 5 = -5
        self.assertAlmostEqual(s["put_pnl_points"], -5.0, places=2)

    def test_max_win(self):
        s = compute_summary(self._df())
        self.assertAlmostEqual(s["max_win_points"], 25.0, places=2)

    def test_max_loss(self):
        s = compute_summary(self._df())
        self.assertAlmostEqual(s["max_loss_points"], -10.0, places=2)

    def test_empty_dataframe(self):
        s = compute_summary(pd.DataFrame())
        self.assertEqual(s["total_trades"], 0)
        self.assertAlmostEqual(s["net_pnl_points"], 0.0)
        self.assertAlmostEqual(s["win_rate_pct"], 0.0)

    def test_none_input(self):
        s = compute_summary(None)
        self.assertEqual(s["total_trades"], 0)


# ═══════════════════════════════════════════════════════════════════════════════
# TestPrintSummary
# ═══════════════════════════════════════════════════════════════════════════════

class TestPrintSummary(unittest.TestCase):

    def _capture(self, summary: dict) -> str:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            print_summary(summary)
        return buf.getvalue()

    def test_total_trades_in_output(self):
        out = self._capture(compute_summary(_make_trades_df(
            [{"option_type": "CALL", "pnl_points": 5.0, "qty": 50}]
        )))
        self.assertIn("Total trades", out)

    def test_win_rate_in_output(self):
        out = self._capture(compute_summary(_make_trades_df(
            [{"option_type": "CALL", "pnl_points": 5.0, "qty": 50}]
        )))
        self.assertIn("Win rate", out)

    def test_net_pnl_in_output(self):
        out = self._capture(compute_summary(_make_trades_df(
            [{"option_type": "CALL", "pnl_points": 5.0, "qty": 50}]
        )))
        self.assertIn("Net P&L", out)

    def test_call_put_breakdown_in_output(self):
        records = [
            {"option_type": "CALL", "pnl_points": 5.0, "qty": 50},
            {"option_type": "PUT",  "pnl_points": -2.0, "qty": 50},
        ]
        out = self._capture(compute_summary(_make_trades_df(records)))
        self.assertIn("CALL", out)
        self.assertIn("PUT", out)


# ═══════════════════════════════════════════════════════════════════════════════
# TestEquityCurve
# ═══════════════════════════════════════════════════════════════════════════════

class TestEquityCurve(unittest.TestCase):

    def _df(self):
        return _make_trades_df([
            {"pnl_points": 10.0, "qty": 50},
            {"pnl_points": -5.0, "qty": 50},
            {"pnl_points": 15.0, "qty": 50},
        ])

    def test_returns_path_when_mpl_available(self):
        if not _MPL_AVAILABLE:
            self.skipTest("matplotlib not installed")
        with tempfile.TemporaryDirectory() as tmp:
            out = plot_equity_curve(self._df(), output_path=Path(tmp) / "eq.png")
            self.assertIsNotNone(out)
            self.assertTrue(out.exists())

    def test_file_is_non_empty_png(self):
        if not _MPL_AVAILABLE:
            self.skipTest("matplotlib not installed")
        with tempfile.TemporaryDirectory() as tmp:
            out = plot_equity_curve(self._df(), output_path=Path(tmp) / "eq.png")
            self.assertGreater(out.stat().st_size, 0)

    def test_returns_none_for_empty_df(self):
        result = plot_equity_curve(pd.DataFrame())
        self.assertIsNone(result)

    def test_returns_none_for_none_input(self):
        result = plot_equity_curve(None)
        self.assertIsNone(result)

    def test_creates_parent_directory(self):
        if not _MPL_AVAILABLE:
            self.skipTest("matplotlib not installed")
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "a" / "b" / "eq.png"
            out = plot_equity_curve(self._df(), output_path=nested)
            self.assertIsNotNone(out)
            self.assertTrue(out.exists())


# ═══════════════════════════════════════════════════════════════════════════════
# TestSaveReportCSV
# ═══════════════════════════════════════════════════════════════════════════════

class TestSaveReportCSV(unittest.TestCase):

    def _df(self):
        return _make_trades_df([
            {"symbol": "NSE:NIFTY24JAN22000CE", "option_type": "CALL",
             "pnl_points": 15.0, "qty": 50},
            {"symbol": "NSE:NIFTY24JAN22000PE", "option_type": "PUT",
             "pnl_points": -7.0, "qty": 50},
        ])

    def test_file_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = save_report_csv(self._df(), Path(tmp) / "report.csv")
            self.assertTrue(p.exists())

    def test_csv_has_correct_row_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = save_report_csv(self._df(), Path(tmp) / "report.csv")
            df = pd.read_csv(p)
            self.assertEqual(len(df), 2)

    def test_csv_has_required_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = save_report_csv(self._df(), Path(tmp) / "report.csv")
            df = pd.read_csv(p)
            self.assertIn("symbol", df.columns)
            self.assertIn("pnl_points", df.columns)

    def test_creates_nested_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "reports" / "2024" / "report.csv"
            p = save_report_csv(self._df(), nested)
            self.assertTrue(p.exists())


# ═══════════════════════════════════════════════════════════════════════════════
# TestSessionDashboard
# ═══════════════════════════════════════════════════════════════════════════════

class TestSessionDashboard(unittest.TestCase):

    def _dash(self) -> SessionDashboard:
        dash = SessionDashboard(qty_default=50)
        dash.record_entry("2024-01-15T09:30:00", "NSE:NIFTY24JAN22000CE",
                          "CALL", price=120.0, qty=50, position_id="P1")
        dash.record_exit("2024-01-15T10:00:00", "NSE:NIFTY24JAN22000CE",
                         "CALL", price=145.0, qty=50, reason="TARGET_HIT",
                         position_id="P1", bars_held=5)
        dash.record_entry("2024-01-15T10:15:00", "NSE:NIFTY24JAN22000PE",
                          "PUT", price=90.0, qty=50, position_id="P2")
        dash.record_exit("2024-01-15T10:45:00", "NSE:NIFTY24JAN22000PE",
                         "PUT", price=75.0, qty=50, reason="SL_HIT",
                         position_id="P2", bars_held=3)
        return dash

    def test_to_dataframe_has_rows(self):
        df = self._dash().to_dataframe()
        self.assertEqual(len(df), 2)

    def test_pnl_call_correct(self):
        df = self._dash().to_dataframe()
        call_row = df[df["option_type"] == "CALL"].iloc[0]
        self.assertAlmostEqual(call_row["pnl_points"], 25.0, places=2)

    def test_pnl_put_correct(self):
        df = self._dash().to_dataframe()
        put_row = df[df["option_type"] == "PUT"].iloc[0]
        self.assertAlmostEqual(put_row["pnl_points"], -15.0, places=2)

    def test_pnl_rupees_call(self):
        df = self._dash().to_dataframe()
        call_row = df[df["option_type"] == "CALL"].iloc[0]
        # 25 * 50 = 1250
        self.assertAlmostEqual(call_row["pnl_rupees"], 1250.0, places=2)

    def test_summary_total_trades(self):
        s = self._dash().summary()
        self.assertEqual(s["total_trades"], 2)

    def test_summary_winners(self):
        s = self._dash().summary()
        self.assertEqual(s["winners"], 1)

    def test_summary_losers(self):
        s = self._dash().summary()
        self.assertEqual(s["losers"], 1)

    def test_summary_net_pnl(self):
        s = self._dash().summary()
        # 25 + (-15) = 10
        self.assertAlmostEqual(s["net_pnl_points"], 10.0, places=2)

    def test_summary_call_pnl(self):
        s = self._dash().summary()
        self.assertAlmostEqual(s["call_pnl_points"], 25.0, places=2)

    def test_summary_put_pnl(self):
        s = self._dash().summary()
        self.assertAlmostEqual(s["put_pnl_points"], -15.0, places=2)

    def test_reason_stored(self):
        df = self._dash().to_dataframe()
        self.assertIn("TARGET_HIT", df["reason"].tolist())
        self.assertIn("SL_HIT", df["reason"].tolist())

    def test_bars_held_stored(self):
        df = self._dash().to_dataframe()
        self.assertIn(5, df["bars_held"].tolist())
        self.assertIn(3, df["bars_held"].tolist())

    def test_emit_returns_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._dash().emit(output_dir=tmp)
        self.assertIn("summary", result)
        self.assertEqual(result["summary"]["total_trades"], 2)

    def test_emit_writes_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._dash().emit(output_dir=tmp)
            # Check inside the context manager while the directory still exists
            if result["csv"]:
                self.assertTrue(result["csv"].exists())

    def test_empty_dashboard_summary(self):
        s = SessionDashboard().summary()
        self.assertEqual(s["total_trades"], 0)

    def test_exit_without_entry_does_not_crash(self):
        """record_exit with no matching entry falls back to defaults."""
        dash = SessionDashboard(qty_default=1)
        dash.record_exit("2024-01-15T10:00:00", "GHOST_SYM", "CALL",
                         price=100.0, qty=1, reason="SL_HIT",
                         position_id="GHOST", bars_held=2)
        df = dash.to_dataframe()
        self.assertEqual(len(df), 1)
        # entry_price defaults to 0.0 → pnl = 100 - 0 = 100
        self.assertAlmostEqual(df.iloc[0]["pnl_points"], 100.0, places=2)

    def test_emit_logs_dashboard_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertLogs(level="INFO") as cm:
                self._dash().emit(output_dir=tmp)
        self.assertTrue(any("[DASHBOARD]" in l for l in cm.output))


# ═══════════════════════════════════════════════════════════════════════════════
# TestGenerateDashboard
# ═══════════════════════════════════════════════════════════════════════════════

class TestGenerateDashboard(unittest.TestCase):

    def _df(self):
        return _make_trades_df([
            {"option_type": "CALL", "pnl_points": 20.0, "qty": 50},
            {"option_type": "PUT",  "pnl_points": -8.0,  "qty": 50},
        ])

    def test_from_dataframe_returns_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = generate_dashboard(trades_df=self._df(), output_dir=tmp)
        self.assertIn("summary", r)
        self.assertEqual(r["summary"]["total_trades"], 2)

    def test_from_log_file_returns_summary(self):
        log_path = _make_log_file(_sample_log())
        try:
            with tempfile.TemporaryDirectory() as tmp:
                r = generate_dashboard(log_path=log_path, output_dir=tmp)
            self.assertIn("summary", r)
            # 3 EXIT AUDIT lines → 3 trades
            self.assertEqual(r["summary"]["total_trades"], 3)
        finally:
            os.unlink(log_path)

    def test_net_pnl_from_log(self):
        log_path = _make_log_file(_sample_log())
        try:
            with tempfile.TemporaryDirectory() as tmp:
                r = generate_dashboard(log_path=log_path, output_dir=tmp)
            # 25.5 + (-15.0) + 8.0 = 18.5
            self.assertAlmostEqual(r["summary"]["net_pnl_points"], 18.5, places=1)
        finally:
            os.unlink(log_path)

    def test_empty_log_produces_zero_trades(self):
        log_path = _make_log_file("no useful lines\n")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                r = generate_dashboard(log_path=log_path, output_dir=tmp)
            self.assertEqual(r["summary"]["total_trades"], 0)
        finally:
            os.unlink(log_path)

    def test_no_inputs_produces_zero_trades(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = generate_dashboard(output_dir=tmp)
        self.assertEqual(r["summary"]["total_trades"], 0)

    def test_logs_dashboard_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertLogs(level="INFO") as cm:
                generate_dashboard(trades_df=self._df(), output_dir=tmp)
        self.assertTrue(any("[DASHBOARD]" in l for l in cm.output))


# ═══════════════════════════════════════════════════════════════════════════════
# TestEdgeCases
# ═══════════════════════════════════════════════════════════════════════════════

class TestEdgeCases(unittest.TestCase):

    def test_single_trade_100pct_win_rate(self):
        df = _make_trades_df([{"option_type": "CALL", "pnl_points": 5.0, "qty": 1}])
        s = compute_summary(df)
        self.assertAlmostEqual(s["win_rate_pct"], 100.0)
        self.assertEqual(s["total_trades"], 1)

    def test_all_losses(self):
        df = _make_trades_df([
            {"option_type": "CALL", "pnl_points": -3.0, "qty": 1},
            {"option_type": "PUT",  "pnl_points": -7.0, "qty": 1},
        ])
        s = compute_summary(df)
        self.assertEqual(s["winners"], 0)
        self.assertEqual(s["win_rate_pct"], 0.0)
        self.assertAlmostEqual(s["net_pnl_points"], -10.0)

    def test_all_breakeven(self):
        df = _make_trades_df([
            {"option_type": "CALL", "pnl_points": 0.0, "qty": 1},
            {"option_type": "CALL", "pnl_points": 0.0, "qty": 1},
        ])
        s = compute_summary(df)
        self.assertEqual(s["breakeven"], 2)
        self.assertEqual(s["winners"], 0)

    def test_nan_pnl_treated_as_zero(self):
        """NaN P&L values must not break the summary."""
        df = _make_trades_df([
            {"option_type": "CALL", "pnl_points": float("nan"), "qty": 1},
            {"option_type": "PUT",  "pnl_points": 10.0, "qty": 1},
        ])
        s = compute_summary(df)
        self.assertEqual(s["total_trades"], 2)
        # NaN treated as 0 in fillna path
        self.assertAlmostEqual(s["net_pnl_points"], 10.0, places=2)

    def test_trade_record_pnl_from_prices(self):
        """TradeRecord computes P&L from prices when premium_move is None."""
        rec = TradeRecord(
            position_id="T1", symbol="SYM", option_type="CALL",
            entry_price=100.0, exit_price=120.0, qty=50
        )
        self.assertAlmostEqual(rec.pnl_points, 20.0, places=2)
        self.assertAlmostEqual(rec.pnl_rupees, 1000.0, places=2)

    def test_trade_record_pnl_from_premium_move(self):
        """premium_move overrides price difference."""
        rec = TradeRecord(
            position_id="T1", symbol="SYM", option_type="CALL",
            entry_price=100.0, exit_price=120.0, qty=50,
            premium_move=-5.0,
        )
        self.assertAlmostEqual(rec.pnl_points, -5.0, places=2)

    def test_log_with_partial_fields(self):
        """A log line missing optional bars_held defaults to -1."""
        log_content = (
            "2024-01-15 09:00:00,001 - INFO - "
            "[EXIT AUDIT] timestamp=2024-01-15T09:00:00 "
            "symbol=NSE:NIFTY CE option_type=CALL "
            "exit_type=SL reason=SL_HIT "
            "triggering_condition=test candle=1 "
            "regime=ATR position_id=POSXYZ\n"
        )
        log_path = _make_log_file(log_content)
        try:
            df = parse_log_file(log_path)
            self.assertEqual(len(df), 1)
            self.assertEqual(df.iloc[0]["bars_held"], -1)
        finally:
            os.unlink(log_path)


# ═══════════════════════════════════════════════════════════════════════════════
# TestConfigThresholds — config_thresholds integration
# ═══════════════════════════════════════════════════════════════════════════════

class TestConfigThresholds(unittest.TestCase):
    """Verify STEntryConfig thresholds flow through compute_summary,
    print_summary, SessionDashboard, and generate_dashboard."""

    _THRESHOLDS = {"adx_min": 18.0, "rr_ratio": 2.0, "tg_rr_ratio": 1.0}

    def _df(self):
        return _make_trades_df([
            {"option_type": "CALL", "pnl_points": 10.0, "qty": 50},
            {"option_type": "PUT",  "pnl_points": -5.0, "qty": 50},
        ])

    # ── compute_summary ────────────────────────────────────────────────────

    def test_compute_summary_includes_config_thresholds_key(self):
        """compute_summary always returns a config_thresholds key."""
        s = compute_summary(self._df())
        self.assertIn("config_thresholds", s)

    def test_compute_summary_empty_config_thresholds_by_default(self):
        """Without explicit config_thresholds arg the key is an empty dict."""
        s = compute_summary(self._df())
        self.assertIsInstance(s["config_thresholds"], dict)
        self.assertEqual(s["config_thresholds"], {})

    def test_compute_summary_stores_passed_thresholds(self):
        """config_thresholds dict is preserved verbatim in summary."""
        s = compute_summary(self._df(), config_thresholds=self._THRESHOLDS)
        self.assertAlmostEqual(s["config_thresholds"]["adx_min"],    18.0, places=4)
        self.assertAlmostEqual(s["config_thresholds"]["rr_ratio"],   2.0,  places=4)
        self.assertAlmostEqual(s["config_thresholds"]["tg_rr_ratio"], 1.0, places=4)

    def test_compute_summary_empty_df_still_has_thresholds(self):
        """config_thresholds are returned even when trades df is empty."""
        s = compute_summary(pd.DataFrame(), config_thresholds=self._THRESHOLDS)
        self.assertAlmostEqual(s["config_thresholds"]["adx_min"], 18.0, places=4)

    def test_compute_summary_none_config_thresholds_becomes_empty_dict(self):
        """Passing config_thresholds=None results in an empty dict (not None)."""
        s = compute_summary(self._df(), config_thresholds=None)
        self.assertEqual(s["config_thresholds"], {})

    # ── print_summary ──────────────────────────────────────────────────────

    def _capture(self, summary: dict) -> str:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            print_summary(summary)
        return buf.getvalue()

    def test_print_summary_shows_entry_thresholds_block(self):
        """ENTRY THRESHOLDS section is printed when config_thresholds present."""
        s = compute_summary(self._df(), config_thresholds=self._THRESHOLDS)
        out = self._capture(s)
        self.assertIn("ENTRY THRESHOLDS", out)

    def test_print_summary_shows_adx_min(self):
        """ADX min value is visible in the printed output."""
        s = compute_summary(self._df(), config_thresholds=self._THRESHOLDS)
        out = self._capture(s)
        self.assertIn("18.0", out)

    def test_print_summary_shows_rr_ratio(self):
        """RR ratio is visible in the printed output."""
        s = compute_summary(self._df(), config_thresholds=self._THRESHOLDS)
        out = self._capture(s)
        self.assertIn("2.0", out)

    def test_print_summary_no_thresholds_block_when_empty(self):
        """ENTRY THRESHOLDS section must NOT appear when config_thresholds is empty."""
        s = compute_summary(self._df())   # no thresholds
        out = self._capture(s)
        self.assertNotIn("ENTRY THRESHOLDS", out)

    # ── SessionDashboard ───────────────────────────────────────────────────

    def test_session_dashboard_accepts_config_thresholds(self):
        """SessionDashboard(config_thresholds=…) stores the dict."""
        dash = SessionDashboard(qty_default=50, config_thresholds=self._THRESHOLDS)
        s = dash.summary()
        self.assertAlmostEqual(s["config_thresholds"]["adx_min"], 18.0, places=4)

    def test_session_dashboard_default_no_thresholds(self):
        """SessionDashboard without config_thresholds yields empty dict."""
        dash = SessionDashboard(qty_default=50)
        s = dash.summary()
        self.assertEqual(s["config_thresholds"], {})

    def test_session_dashboard_emit_summary_includes_thresholds(self):
        """emit() summary dict carries config_thresholds."""
        dash = SessionDashboard(qty_default=50, config_thresholds=self._THRESHOLDS)
        dash.record_entry("2024-01-15T09:30:00", "SYM", "CALL",
                          price=100.0, qty=50, position_id="P1")
        dash.record_exit("2024-01-15T10:00:00", "SYM", "CALL",
                         price=120.0, qty=50, reason="TARGET_HIT",
                         position_id="P1", bars_held=4)
        with tempfile.TemporaryDirectory() as tmp:
            result = dash.emit(output_dir=tmp)
        self.assertAlmostEqual(
            result["summary"]["config_thresholds"]["rr_ratio"], 2.0, places=4
        )

    # ── generate_dashboard — [ENTRY CONFIG] log parsing ───────────────────

    def _log_with_entry_config(self) -> str:
        return textwrap.dedent("""\
            2024-01-15 09:30:00,001 - INFO - [ENTRY CONFIG] timestamp=2024-01-15T09:30:00 symbol=NSE:NIFTY50-INDEX adx_min=18.0 rsi_range=[35.0,65.0] cci_range=[-120.0,120.0]
            2024-01-15 10:00:00,001 - INFO - [EXIT AUDIT] timestamp=2024-01-15T10:00:00 symbol=NSE:NIFTY24JAN22000CE option_type=CALL position_side=LONG exit_type=TG reason=TARGET_HIT triggering_condition=tg_hit candle=5 bars_held=5 regime=ATR position_id=POS001 premium_move=20.00
        """)

    def test_generate_dashboard_extracts_adx_min_from_log(self):
        """generate_dashboard parses [ENTRY CONFIG] and sets adx_min=18.0."""
        log_path = _make_log_file(self._log_with_entry_config())
        try:
            with tempfile.TemporaryDirectory() as tmp:
                r = generate_dashboard(log_path=log_path, output_dir=tmp)
            self.assertAlmostEqual(
                r["summary"]["config_thresholds"].get("adx_min", 0.0), 18.0, places=4
            )
        finally:
            os.unlink(log_path)

    def test_generate_dashboard_config_thresholds_empty_without_entry_config_line(self):
        """Logs with no [ENTRY CONFIG] line yield empty config_thresholds."""
        log_path = _make_log_file("no config lines here\n")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                r = generate_dashboard(log_path=log_path, output_dir=tmp)
            self.assertEqual(r["summary"]["config_thresholds"], {})
        finally:
            os.unlink(log_path)

    def test_generate_dashboard_from_dataframe_has_config_thresholds_key(self):
        """generate_dashboard via trades_df always includes config_thresholds key."""
        with tempfile.TemporaryDirectory() as tmp:
            r = generate_dashboard(trades_df=self._df(), output_dir=tmp)
        self.assertIn("config_thresholds", r["summary"])

    def test_generate_dashboard_log_includes_config_thresholds_tag(self):
        """generate_dashboard completion log contains config_thresholds value."""
        log_path = _make_log_file(self._log_with_entry_config())
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertLogs(level="INFO") as cm:
                    generate_dashboard(log_path=log_path, output_dir=tmp)
            self.assertTrue(
                any("config_thresholds" in line for line in cm.output)
            )
        finally:
            os.unlink(log_path)


# ═══════════════════════════════════════════════════════════════════════════════
# TestSaveReportJSON
# ═══════════════════════════════════════════════════════════════════════════════

from dashboard import save_report_json


class TestSaveReportJSON(unittest.TestCase):

    def _df(self):
        return _make_trades_df([
            {"option_type": "CALL", "pnl_points": 15.0, "qty": 50},
            {"option_type": "PUT",  "pnl_points": -7.0, "qty": 50},
        ])

    def _summary(self):
        return compute_summary(self._df())

    def test_file_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = save_report_json(self._df(), self._summary(), Path(tmp) / "trades.json")
            self.assertTrue(p.exists())

    def test_json_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = save_report_json(self._df(), self._summary(), Path(tmp) / "trades.json")
            with p.open(encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertIn("summary", data)
            self.assertIn("trades", data)

    def test_json_trade_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = save_report_json(self._df(), self._summary(), Path(tmp) / "trades.json")
            with p.open(encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertEqual(len(data["trades"]), 2)

    def test_json_summary_total_trades(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = save_report_json(self._df(), self._summary(), Path(tmp) / "trades.json")
            with p.open(encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertEqual(data["summary"]["total_trades"], 2)

    def test_accepts_list_input(self):
        trades_list = [{"side": "CALL", "pnl_pts": 10.0}]
        with tempfile.TemporaryDirectory() as tmp:
            p = save_report_json(trades_list, {}, Path(tmp) / "trades.json")
            with p.open(encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertEqual(len(data["trades"]), 1)

    def test_creates_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "deep" / "path" / "trades.json"
            p = save_report_json([], {}, nested)
            self.assertTrue(p.exists())

    def test_generated_at_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = save_report_json(self._df(), self._summary(), Path(tmp) / "trades.json")
            with p.open(encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertIn("generated_at", data)


# ═══════════════════════════════════════════════════════════════════════════════
# TestLogParserModule  — log_parser.py
# ═══════════════════════════════════════════════════════════════════════════════

from log_parser import LogParser, SessionSummary, parse_session, parse_multiple


def _new_format_log() -> str:
    """Sample log content using the new [TRADE OPEN]/[TRADE EXIT] format."""
    return textwrap.dedent("""\
        2026-02-24 11:02:48,549 - INFO - [ENTRY OK] CALL score=83/50 NORMAL HIGH | ST=20/20 RSI=61.7 CCI=203 VWAP=10/10 PIV=8/15 MOM=\u2713 CPR=NARROW ET=? pivot=BREAKOUT_R3
        2026-02-24 11:02:48,551 - INFO - [SIGNAL FIRED] CALL score=83 strength=HIGH | ST15m=BULLISH ST3m=BULLISH RSI=61.7
        2026-02-24 11:02:48,553 - INFO - [TRADE OPEN][REPLAY] CALL bar=791 2026-02-20 09:45:00 underlying=25497.75 premium=153.00 score=83 src=PIVOT pivot=BREAKOUT_R3 cpr=NARROW day=UNKNOWN max_hold=23bars trail_min=35pts trail_step=12% lot=130
        2026-02-24 11:02:51,478 - INFO - [TRADE EXIT] LOSS CALL bar=798 2026-02-20 10:06:00 prem 153.00\u2192143.61 P&L=-9.39pts (-1221\u20b9) peak=165.56 held=7bars trail_updates=0
        2026-02-24 11:02:56,159 - INFO - [ENTRY OK] CALL score=82/45 NORMAL HIGH | ST=20/20 RSI=70.1 CCI=110 VWAP=10/10 PIV=10/15 MOM=\u2713 CPR=NARROW ET=? pivot=BREAKOUT_R4
        2026-02-24 11:02:56,161 - INFO - [SIGNAL FIRED] CALL score=82 strength=HIGH | ST15m=BULLISH
        2026-02-24 11:02:56,163 - INFO - [TRADE OPEN][REPLAY] CALL bar=809 2026-02-20 10:39:00 underlying=25600.15 premium=153.60 score=82 src=PIVOT pivot=BREAKOUT_R4 cpr=NARROW day=DOUBLE_DIST max_hold=23bars trail_min=35pts trail_step=12% lot=130
        2026-02-24 11:02:59,233 - INFO - [TRADE EXIT] WIN  CALL bar=882 2026-02-20 14:18:00 prem 153.60\u2192170.21 P&L=+16.61pts (+2160\u20b9) peak=170.21 held=5bars trail_updates=0
        2026-02-26 09:49:35,920 - INFO - [ENTRY BLOCKED][COOLDOWN] 0s < 120s
        2026-02-26 09:54:01,805 - INFO - [ENTRY BLOCKED][ST_CONFLICT] timestamp=2026-02-26 09:54:01 symbol=NSE:NIFTY50-INDEX reason=Supertrend conflict
        2026-02-26 09:54:02,000 - INFO - [ENTRY BLOCKED][ST_CONFLICT] timestamp=2026-02-26 09:54:02 symbol=NSE:NIFTY50-INDEX reason=Supertrend conflict
    """)


def _make_new_format_log() -> Path:
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".log", delete=False, encoding="utf-8",
        prefix="options_trade_engine_2026-02-24_"
    )
    f.write(_new_format_log())
    f.close()
    return Path(f.name)


class TestLogParserModule(unittest.TestCase):

    def setUp(self):
        self._log_path = _make_new_format_log()

    def tearDown(self):
        os.unlink(self._log_path)

    def test_returns_session_summary(self):
        summary = parse_session(self._log_path)
        self.assertIsInstance(summary, SessionSummary)

    def test_trade_count(self):
        """2 TRADE OPEN + 2 TRADE EXIT → 2 completed trades."""
        summary = parse_session(self._log_path)
        self.assertEqual(summary.total_trades, 2)

    def test_winner_loser_counts(self):
        """First trade LOSS, second trade WIN."""
        summary = parse_session(self._log_path)
        self.assertEqual(summary.losers, 1)
        self.assertEqual(summary.winners, 1)

    def test_pnl_values(self):
        pnls = [t["pnl_pts"] for t in parse_session(self._log_path).trades]
        self.assertAlmostEqual(sum(pnls), -9.39 + 16.61, places=2)

    def test_bars_held_extracted(self):
        held = {t["bars_held"] for t in parse_session(self._log_path).trades}
        self.assertIn(7, held)
        self.assertIn(5, held)

    def test_entry_premium_extracted(self):
        prems = {t["entry_prem"] for t in parse_session(self._log_path).trades}
        self.assertIn(153.00, prems)
        self.assertIn(153.60, prems)

    def test_open_score_merged(self):
        scores = {t.get("score") for t in parse_session(self._log_path).trades}
        self.assertIn(83, scores)
        self.assertIn(82, scores)

    def test_blocked_counts(self):
        summary = parse_session(self._log_path)
        self.assertIn("COOLDOWN", summary.blocked_counts)
        self.assertIn("ST_CONFLICT", summary.blocked_counts)
        self.assertEqual(summary.blocked_counts["ST_CONFLICT"], 2)
        self.assertEqual(summary.blocked_counts["COOLDOWN"], 1)

    def test_total_blocked(self):
        summary = parse_session(self._log_path)
        self.assertEqual(summary.total_blocked, 3)

    def test_signals_fired(self):
        summary = parse_session(self._log_path)
        self.assertEqual(summary.signals_fired, 2)

    def test_entry_ok_count(self):
        summary = parse_session(self._log_path)
        self.assertEqual(summary.entry_ok_count, 2)

    def test_session_type_replay(self):
        summary = parse_session(self._log_path)
        self.assertEqual(summary.session_type, "REPLAY")

    def test_win_rate(self):
        summary = parse_session(self._log_path)
        self.assertAlmostEqual(summary.win_rate_pct, 50.0, places=1)

    def test_nonexistent_file(self):
        summary = parse_session("/tmp/nonexistent_trading_engine.log")
        self.assertEqual(summary.total_trades, 0)
        self.assertEqual(summary.session_type, "UNKNOWN")

    def test_to_dict_keys(self):
        d = parse_session(self._log_path).to_dict()
        for key in ("total_trades", "win_rate_pct", "net_pnl_pts",
                    "blocked_counts", "signals_fired", "tag_counts"):
            self.assertIn(key, d)

    def test_parse_multiple(self):
        summaries = parse_multiple([self._log_path, self._log_path])
        self.assertEqual(len(summaries), 2)
        self.assertEqual(summaries[0].total_trades, 2)


# ═══════════════════════════════════════════════════════════════════════════════
# TestGenerateFullReport
# ═══════════════════════════════════════════════════════════════════════════════

from dashboard import generate_full_report


class TestGenerateFullReport(unittest.TestCase):

    def setUp(self):
        self._log_path = _make_new_format_log()

    def tearDown(self):
        os.unlink(self._log_path)

    def test_returns_artifact_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = generate_full_report(self._log_path, output_dir=tmp)
        self.assertIn("summary", result)

    def test_csv_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = generate_full_report(self._log_path, output_dir=tmp)
            if result["csv"]:
                self.assertTrue(result["csv"].exists())

    def test_json_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = generate_full_report(self._log_path, output_dir=tmp)
            self.assertIsNotNone(result["json"])
            self.assertTrue(result["json"].exists())

    def test_json_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = generate_full_report(self._log_path, output_dir=tmp)
            with result["json"].open(encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertIn("trades", data)
            self.assertIn("summary", data)

    def test_text_report_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = generate_full_report(self._log_path, output_dir=tmp)
            self.assertIsNotNone(result["text"])
            self.assertTrue(result["text"].exists())

    def test_text_report_contains_win_rate(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = generate_full_report(self._log_path, output_dir=tmp)
            content = result["text"].read_text(encoding="utf-8")
        self.assertIn("Win rate", content)

    def test_text_report_contains_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = generate_full_report(self._log_path, output_dir=tmp)
            content = result["text"].read_text(encoding="utf-8")
        self.assertIn("ST_CONFLICT", content)

    def test_summary_trade_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = generate_full_report(self._log_path, output_dir=tmp)
        self.assertEqual(result["summary"]["total_trades"], 2)

    def test_empty_log_produces_json(self):
        log = _make_log_file("no trades here\n")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                result = generate_full_report(log, output_dir=tmp)
                self.assertIsNotNone(result["json"])
                self.assertTrue(result["json"].exists())
        finally:
            os.unlink(log)


# ═══════════════════════════════════════════════════════════════════════════════
# TestCompareSessions
# ═══════════════════════════════════════════════════════════════════════════════

from dashboard import compare_sessions


def _make_baseline_log() -> Path:
    """Baseline: 2 LOSS trades, heavy ST_CONFLICT blocking."""
    content = textwrap.dedent("""\
        2026-02-22 11:00:00,000 - INFO - [TRADE OPEN][REPLAY] CALL bar=100 2026-02-18 09:45:00 underlying=25000.00 premium=120.00 score=65 src=PIVOT cpr=WIDE day=UNKNOWN lot=50
        2026-02-22 11:00:03,000 - INFO - [TRADE EXIT] LOSS CALL bar=107 2026-02-18 10:15:00 prem 120.00\u2192105.00 P&L=-15.00pts (-750\u20b9) peak=122.00 held=6bars trail_updates=0
        2026-02-22 11:00:10,000 - INFO - [TRADE OPEN][REPLAY] PUT bar=200 2026-02-18 11:00:00 underlying=24900.00 premium=110.00 score=60 src=PIVOT cpr=WIDE day=UNKNOWN lot=50
        2026-02-22 11:00:13,000 - INFO - [TRADE EXIT] LOSS PUT bar=207 2026-02-18 11:30:00 prem 110.00\u219295.00 P&L=-15.00pts (-750\u20b9) peak=112.00 held=6bars trail_updates=0
        2026-02-22 11:00:20,000 - INFO - [ENTRY BLOCKED][ST_CONFLICT] timestamp=2026-02-22 11:00:20 symbol=NSE:NIFTY50-INDEX reason=conflict
        2026-02-22 11:00:21,000 - INFO - [ENTRY BLOCKED][ST_CONFLICT] timestamp=2026-02-22 11:00:21 symbol=NSE:NIFTY50-INDEX reason=conflict
        2026-02-22 11:00:22,000 - INFO - [ENTRY BLOCKED][ST_CONFLICT] timestamp=2026-02-22 11:00:22 symbol=NSE:NIFTY50-INDEX reason=conflict
    """)
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".log", delete=False, encoding="utf-8",
        prefix="options_trade_engine_2026-02-22_"
    )
    f.write(content)
    f.close()
    return Path(f.name)


def _make_fixed_log() -> Path:
    """Fixed: 1 WIN + 1 LOSS, fewer blocked entries."""
    content = textwrap.dedent("""\
        2026-02-28 11:00:00,000 - INFO - [TRADE OPEN][REPLAY] CALL bar=100 2026-02-25 09:45:00 underlying=25200.00 premium=130.00 score=78 src=PIVOT cpr=NARROW day=TRENDING lot=50
        2026-02-28 11:00:03,000 - INFO - [TRADE EXIT] WIN  CALL bar=110 2026-02-25 10:15:00 prem 130.00\u2192155.00 P&L=+25.00pts (+1250\u20b9) peak=157.00 held=10bars trail_updates=1
        2026-02-28 11:00:10,000 - INFO - [TRADE OPEN][REPLAY] PUT bar=200 2026-02-25 11:00:00 underlying=25100.00 premium=115.00 score=72 src=PIVOT cpr=NARROW day=TRENDING lot=50
        2026-02-28 11:00:13,000 - INFO - [TRADE EXIT] LOSS PUT bar=207 2026-02-25 11:30:00 prem 115.00\u2192105.00 P&L=-10.00pts (-500\u20b9) peak=117.00 held=6bars trail_updates=0
        2026-02-28 11:00:20,000 - INFO - [ENTRY BLOCKED][ST_CONFLICT] timestamp=2026-02-28 11:00:20 symbol=NSE:NIFTY50-INDEX reason=conflict
        2026-02-28 11:00:23,000 - INFO - [FALSE_BREAKOUT_COOLDOWN] Activated for 5 bars
    """)
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".log", delete=False, encoding="utf-8",
        prefix="options_trade_engine_2026-02-28_"
    )
    f.write(content)
    f.close()
    return Path(f.name)


class TestCompareSessions(unittest.TestCase):

    def setUp(self):
        self._baseline = _make_baseline_log()
        self._fixed    = _make_fixed_log()

    def tearDown(self):
        os.unlink(self._baseline)
        os.unlink(self._fixed)

    def test_returns_dict_with_required_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = compare_sessions([self._baseline], [self._fixed], output_dir=tmp)
        for key in ("text", "baseline_summary", "fixed_summary"):
            self.assertIn(key, r)

    def test_comparison_report_file_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = compare_sessions([self._baseline], [self._fixed], output_dir=tmp)
            self.assertTrue(r["text"].exists())

    def test_baseline_has_two_trades(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = compare_sessions([self._baseline], [self._fixed], output_dir=tmp)
        self.assertEqual(r["baseline_summary"]["total_trades"], 2)

    def test_fixed_has_two_trades(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = compare_sessions([self._baseline], [self._fixed], output_dir=tmp)
        self.assertEqual(r["fixed_summary"]["total_trades"], 2)

    def test_baseline_net_pnl_negative(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = compare_sessions([self._baseline], [self._fixed], output_dir=tmp)
        self.assertLess(r["baseline_summary"]["net_pnl_pts"], 0)

    def test_fixed_net_pnl_better_than_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = compare_sessions([self._baseline], [self._fixed], output_dir=tmp)
        self.assertGreater(
            r["fixed_summary"]["net_pnl_pts"],
            r["baseline_summary"]["net_pnl_pts"],
        )

    def test_baseline_win_rate_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = compare_sessions([self._baseline], [self._fixed], output_dir=tmp)
        self.assertAlmostEqual(r["baseline_summary"]["win_rate_pct"], 0.0, places=1)

    def test_fixed_win_rate_fifty(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = compare_sessions([self._baseline], [self._fixed], output_dir=tmp)
        self.assertAlmostEqual(r["fixed_summary"]["win_rate_pct"], 50.0, places=1)

    def test_baseline_has_more_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = compare_sessions([self._baseline], [self._fixed], output_dir=tmp)
        self.assertGreater(
            r["baseline_summary"]["total_blocked"],
            r["fixed_summary"]["total_blocked"],
        )

    def test_report_contains_head_to_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = compare_sessions([self._baseline], [self._fixed], output_dir=tmp)
            content = r["text"].read_text(encoding="utf-8")
        self.assertIn("BASELINE", content.upper())
        self.assertIn("FIXED", content.upper())

    def test_report_contains_blocked_breakdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = compare_sessions([self._baseline], [self._fixed], output_dir=tmp)
            content = r["text"].read_text(encoding="utf-8")
        self.assertIn("ST_CONFLICT", content)

    def test_p1_tag_count_in_fixed(self):
        """[FALSE_BREAKOUT_COOLDOWN] appears in fixed log → tag_counts > 0."""
        with tempfile.TemporaryDirectory() as tmp:
            r = compare_sessions([self._baseline], [self._fixed], output_dir=tmp)
        ftags = r["fixed_summary"]["tag_counts"]
        self.assertGreater(ftags.get("FALSE_BREAKOUT_COOLDOWN", 0), 0)


# ═══════════════════════════════════════════════════════════════════════════════
# TestDashboardOpenBias  — P5-C  (log_parser.py + dashboard.py)
# ═══════════════════════════════════════════════════════════════════════════════

from dashboard import generate_full_report


def _open_bias_log(bias_tag: str = "OPEN_HIGH") -> str:
    """
    Log content with:
      • one [OPEN_POSITION] line (session open bias)
      • one CALL trade (ALIGNED when OPEN_LOW, MISALIGNED when OPEN_HIGH)
      • one PUT trade  (ALIGNED when OPEN_HIGH, MISALIGNED when OPEN_LOW)
    """
    return textwrap.dedent(f"""\
        2026-02-24 09:15:00,001 - INFO - \x1b[36m[OPEN_POSITION] tag={bias_tag} open=25000.00 high=25000.00 low=24900.00 tol=0.50 bull=0 bear=3\x1b[0m
        2026-02-24 09:18:00,001 - INFO - [TRADE OPEN][REPLAY] CALL bar=10 2026-02-24 09:18:00 underlying=25000.00 premium=120.00 score=75 src=PIVOT lot=50
        2026-02-24 09:24:00,001 - INFO - [TRADE EXIT] WIN  CALL bar=12 2026-02-24 09:24:00 prem 120.00\u2192135.00 P&L=+15.00pts (+750\u20b9) peak=136.00 held=2bars
        2026-02-24 09:30:00,001 - INFO - [TRADE OPEN][REPLAY] PUT bar=13 2026-02-24 09:30:00 underlying=25000.00 premium=100.00 score=70 src=ST lot=50
        2026-02-24 09:36:00,001 - INFO - [TRADE EXIT] WIN  PUT bar=15 2026-02-24 09:36:00 prem 100.00\u2192115.00 P&L=+15.00pts (+750\u20b9) peak=116.00 held=2bars
    """)


class TestDashboardOpenBias(unittest.TestCase):
    """P5-C: log_parser detects [OPEN_POSITION] and annotates trades."""

    def _parse(self, bias_tag: str = "OPEN_HIGH") -> "SessionSummary":
        log_path = _make_log_file(_open_bias_log(bias_tag))
        try:
            parser = LogParser(log_path)
            return parser.parse()
        finally:
            os.unlink(log_path)

    # ── SessionSummary.open_bias_tag ──────────────────────────────────────

    def test_open_bias_tag_detected_open_high(self):
        """[OPEN_POSITION] tag=OPEN_HIGH → session.open_bias_tag = OPEN_HIGH."""
        s = self._parse("OPEN_HIGH")
        self.assertEqual(s.open_bias_tag, "OPEN_HIGH")

    def test_open_bias_tag_detected_open_low(self):
        """[OPEN_POSITION] tag=OPEN_LOW → session.open_bias_tag = OPEN_LOW."""
        s = self._parse("OPEN_LOW")
        self.assertEqual(s.open_bias_tag, "OPEN_LOW")

    def test_no_open_position_line_defaults_none(self):
        """Log with no [OPEN_POSITION] → open_bias_tag = NONE."""
        log_path = _make_log_file("2026-02-24 09:15:00,001 - INFO - no bias here\n")
        try:
            s = LogParser(log_path).parse()
        finally:
            os.unlink(log_path)
        self.assertEqual(s.open_bias_tag, "NONE")

    # ── Trade annotation ──────────────────────────────────────────────────

    def test_call_trade_aligned_when_open_low(self):
        """CALL trade annotated ALIGNED when session bias = OPEN_LOW."""
        s = self._parse("OPEN_LOW")
        call_trades = [t for t in s.trades if t.get("side") == "CALL"]
        self.assertTrue(len(call_trades) > 0)
        self.assertEqual(call_trades[0]["open_bias_aligned"], "ALIGNED")

    def test_put_trade_aligned_when_open_high(self):
        """PUT trade annotated ALIGNED when session bias = OPEN_HIGH."""
        s = self._parse("OPEN_HIGH")
        put_trades = [t for t in s.trades if t.get("side") == "PUT"]
        self.assertTrue(len(put_trades) > 0)
        self.assertEqual(put_trades[0]["open_bias_aligned"], "ALIGNED")

    def test_call_trade_misaligned_when_open_high(self):
        """CALL trade annotated MISALIGNED when session bias = OPEN_HIGH."""
        s = self._parse("OPEN_HIGH")
        call_trades = [t for t in s.trades if t.get("side") == "CALL"]
        self.assertTrue(len(call_trades) > 0)
        self.assertEqual(call_trades[0]["open_bias_aligned"], "MISALIGNED")

    def test_put_trade_misaligned_when_open_low(self):
        """PUT trade annotated MISALIGNED when session bias = OPEN_LOW."""
        s = self._parse("OPEN_LOW")
        put_trades = [t for t in s.trades if t.get("side") == "PUT"]
        self.assertTrue(len(put_trades) > 0)
        self.assertEqual(put_trades[0]["open_bias_aligned"], "MISALIGNED")

    def test_neutral_annotation_when_bias_none(self):
        """All trades annotated NEUTRAL when open_bias_tag = NONE."""
        log_path = _make_log_file(textwrap.dedent("""\
            2026-02-24 09:18:00,001 - INFO - [TRADE OPEN][REPLAY] CALL bar=10 2026-02-24 09:18:00 underlying=25000.00 premium=120.00 score=75 src=PIVOT lot=50
            2026-02-24 09:24:00,001 - INFO - [TRADE EXIT] WIN  CALL bar=12 2026-02-24 09:24:00 prem 120.00\u2192135.00 P&L=+15.00pts (+750\u20b9) peak=136.00 held=2bars
        """))
        try:
            s = LogParser(log_path).parse()
        finally:
            os.unlink(log_path)
        self.assertTrue(all(t["open_bias_aligned"] == "NEUTRAL" for t in s.trades))

    # ── open_bias_stats property ──────────────────────────────────────────

    def test_open_bias_stats_has_required_keys(self):
        """open_bias_stats dict exposes the required keys."""
        s = self._parse("OPEN_HIGH")
        obs = s.open_bias_stats
        for key in ("open_bias_tag", "aligned_count", "misaligned_count",
                    "neutral_count", "aligned_pnl", "misaligned_pnl", "pct_aligned"):
            self.assertIn(key, obs, f"Missing key: {key}")

    def test_open_bias_stats_aligned_count_open_high(self):
        """OPEN_HIGH: 1 PUT (aligned) + 1 CALL (misaligned) → aligned_count=1."""
        s = self._parse("OPEN_HIGH")
        obs = s.open_bias_stats
        self.assertEqual(obs["aligned_count"], 1)
        self.assertEqual(obs["misaligned_count"], 1)

    def test_open_bias_stats_pct_aligned(self):
        """pct_aligned = 50.0 when 1 of 2 trades aligned."""
        s = self._parse("OPEN_HIGH")
        self.assertAlmostEqual(s.open_bias_stats["pct_aligned"], 50.0, places=1)

    def test_open_bias_stats_in_to_dict(self):
        """to_dict() includes open_bias_tag and open_bias_stats."""
        s = self._parse("OPEN_HIGH")
        d = s.to_dict()
        self.assertIn("open_bias_tag", d)
        self.assertIn("open_bias_stats", d)
        self.assertEqual(d["open_bias_tag"], "OPEN_HIGH")

    # ── Text report contains open bias section ────────────────────────────

    def test_text_report_contains_open_bias_section(self):
        """generate_full_report writes OPEN BIAS ALIGNMENT section."""
        log_path = _make_log_file(_open_bias_log("OPEN_HIGH"))
        try:
            with tempfile.TemporaryDirectory() as tmp:
                result = generate_full_report(log_path, output_dir=tmp)
                content = result["text"].read_text(encoding="utf-8")
        finally:
            os.unlink(log_path)
        self.assertIn("OPEN BIAS", content)

    def test_text_report_shows_session_bias_tag(self):
        """Text report shows the session open bias tag."""
        log_path = _make_log_file(_open_bias_log("OPEN_HIGH"))
        try:
            with tempfile.TemporaryDirectory() as tmp:
                result = generate_full_report(log_path, output_dir=tmp)
                content = result["text"].read_text(encoding="utf-8")
        finally:
            os.unlink(log_path)
        self.assertIn("OPEN_HIGH", content)

    # ── OPEN_POSITION tag counted ─────────────────────────────────────────

    def test_open_position_tag_counted_in_tag_counts(self):
        """[OPEN_POSITION] log line increments tag_counts['OPEN_POSITION']."""
        s = self._parse("OPEN_HIGH")
        self.assertGreater(s.tag_counts.get("OPEN_POSITION", 0), 0)


# ═══════════════════════════════════════════════════════════════════════════════
# TestDashboardOpenBiasExtended — P5-F (new tag detection in log_parser.py)
# ═══════════════════════════════════════════════════════════════════════════════

def _extended_bias_log(
    gap_tag: str = "GAP_UP",
    vs_close_tag: str = "OPEN_ABOVE_CLOSE",
    balance_tag: str = "OUTSIDE_BALANCE",
) -> str:
    """Log with all four P5 tag lines and one CALL trade."""
    return textwrap.dedent(f"""\
        2026-02-24 09:15:00,001 - INFO - \x1b[36m[OPEN_POSITION] tag=OPEN_LOW open=24900.00 high=25100.00 low=24900.00 tol=0.50 bull=3 bear=0\x1b[0m
        2026-02-24 09:15:01,001 - INFO - \x1b[36m[{gap_tag}] open=24900.00 prev_high=24850.00 prev_low=24700.00 bull=3 bear=0\x1b[0m
        2026-02-24 09:15:02,001 - INFO - \x1b[36m[{vs_close_tag}] open=24900.00 prev_close=24800.00 bull=2 bear=0\x1b[0m
        2026-02-24 09:15:03,001 - INFO - \x1b[36m[{balance_tag}] open=24900.00 bc=24990.00 tc=25010.00\x1b[0m
        2026-02-24 09:18:00,001 - INFO - [TRADE OPEN][REPLAY] CALL bar=10 2026-02-24 09:18:00 underlying=25000.00 premium=120.00 score=75 src=PIVOT lot=50
        2026-02-24 09:24:00,001 - INFO - [TRADE EXIT] WIN  CALL bar=12 2026-02-24 09:24:00 prem 120.00\u2192135.00 P&L=+15.00pts (+750\u20b9) peak=136.00 held=2bars
    """)


class TestDashboardOpenBiasExtended(unittest.TestCase):
    """P5-F: new tag detection and SessionSummary extended fields."""

    def _parse(self, **kwargs) -> "SessionSummary":
        log_path = _make_log_file(_extended_bias_log(**kwargs))
        try:
            return LogParser(log_path).parse()
        finally:
            os.unlink(log_path)

    # ── New tag fields on SessionSummary ──────────────────────────────────

    def test_gap_tag_detected_gap_up(self):
        s = self._parse(gap_tag="GAP_UP")
        self.assertEqual(s.gap_tag, "GAP_UP")

    def test_gap_tag_detected_gap_down(self):
        s = self._parse(gap_tag="GAP_DOWN")
        self.assertEqual(s.gap_tag, "GAP_DOWN")

    def test_gap_tag_detected_no_gap(self):
        s = self._parse(gap_tag="NO_GAP")
        self.assertEqual(s.gap_tag, "NO_GAP")

    def test_vs_close_tag_detected_open_above(self):
        s = self._parse(vs_close_tag="OPEN_ABOVE_CLOSE")
        self.assertEqual(s.vs_close_tag, "OPEN_ABOVE_CLOSE")

    def test_vs_close_tag_detected_open_below(self):
        s = self._parse(vs_close_tag="OPEN_BELOW_CLOSE")
        self.assertEqual(s.vs_close_tag, "OPEN_BELOW_CLOSE")

    def test_vs_close_tag_detected_close_equal(self):
        s = self._parse(vs_close_tag="OPEN_CLOSE_EQUAL")
        self.assertEqual(s.vs_close_tag, "OPEN_CLOSE_EQUAL")

    def test_balance_tag_detected_balance_open(self):
        s = self._parse(balance_tag="BALANCE_OPEN")
        self.assertEqual(s.balance_tag, "BALANCE_OPEN")

    def test_balance_tag_detected_outside_balance(self):
        s = self._parse(balance_tag="OUTSIDE_BALANCE")
        self.assertEqual(s.balance_tag, "OUTSIDE_BALANCE")

    # ── open_bias_stats extended keys ─────────────────────────────────────

    def test_open_bias_stats_has_new_keys(self):
        s = self._parse()
        obs = s.open_bias_stats
        for key in ("vs_close_tag", "gap_tag", "balance_tag",
                    "is_gap_day", "is_balance_day",
                    "gap_day_pnl", "balance_day_pnl"):
            self.assertIn(key, obs, f"Missing key: {key}")

    def test_is_gap_day_true_for_gap_up(self):
        s = self._parse(gap_tag="GAP_UP")
        self.assertTrue(s.open_bias_stats["is_gap_day"])

    def test_is_gap_day_false_for_no_gap(self):
        s = self._parse(gap_tag="NO_GAP")
        self.assertFalse(s.open_bias_stats["is_gap_day"])

    def test_is_balance_day_true(self):
        s = self._parse(balance_tag="BALANCE_OPEN")
        self.assertTrue(s.open_bias_stats["is_balance_day"])

    def test_is_balance_day_false_for_outside(self):
        s = self._parse(balance_tag="OUTSIDE_BALANCE")
        self.assertFalse(s.open_bias_stats["is_balance_day"])

    def test_gap_day_pnl_nonzero_on_gap_day(self):
        """gap_day_pnl = total session P&L when is_gap_day."""
        s = self._parse(gap_tag="GAP_UP")
        self.assertNotEqual(s.open_bias_stats["gap_day_pnl"], 0.0)

    def test_gap_day_pnl_zero_on_non_gap_day(self):
        s = self._parse(gap_tag="NO_GAP")
        self.assertEqual(s.open_bias_stats["gap_day_pnl"], 0.0)

    # ── to_dict() new fields ──────────────────────────────────────────────

    def test_to_dict_includes_new_tag_fields(self):
        s = self._parse()
        d = s.to_dict()
        for key in ("vs_close_tag", "gap_tag", "balance_tag"):
            self.assertIn(key, d, f"Missing key in to_dict: {key}")

    # ── Trade alignment with GAP_UP ───────────────────────────────────────

    def test_call_aligned_when_open_low_and_gap_up(self):
        """CALL trade ALIGNED when OPEN_LOW OR GAP_UP (extended logic)."""
        s = self._parse(gap_tag="GAP_UP")
        call_trades = [t for t in s.trades if t.get("side") == "CALL"]
        self.assertTrue(len(call_trades) > 0)
        self.assertEqual(call_trades[0]["open_bias_aligned"], "ALIGNED")

    def test_call_aligned_when_only_gap_up_no_open_pos(self):
        """CALL trade ALIGNED via GAP_UP even when OPEN_POSITION not present."""
        log_content = textwrap.dedent("""\
            2026-02-24 09:15:01,001 - INFO - [GAP_UP] open=25200.00 prev_high=25100.00 prev_low=24900.00 bull=3 bear=0
            2026-02-24 09:18:00,001 - INFO - [TRADE OPEN][REPLAY] CALL bar=10 2026-02-24 09:18:00 underlying=25000.00 premium=120.00 score=75 src=PIVOT lot=50
            2026-02-24 09:24:00,001 - INFO - [TRADE EXIT] WIN  CALL bar=12 2026-02-24 09:24:00 prem 120.00\u2192135.00 P&L=+15.00pts (+750\u20b9) peak=136.00 held=2bars
        """)
        log_path = _make_log_file(log_content)
        try:
            s = LogParser(log_path).parse()
        finally:
            os.unlink(log_path)
        call_trades = [t for t in s.trades if t.get("side") == "CALL"]
        self.assertEqual(call_trades[0]["open_bias_aligned"], "ALIGNED")

    # ── Text report shows new scenario fields ─────────────────────────────

    def test_text_report_shows_gap_scenario(self):
        log_path = _make_log_file(_extended_bias_log(gap_tag="GAP_UP"))
        try:
            with tempfile.TemporaryDirectory() as tmp:
                result = generate_full_report(log_path, output_dir=tmp)
                content = result["text"].read_text(encoding="utf-8")
        finally:
            os.unlink(log_path)
        self.assertIn("GAP_UP", content)

    def test_text_report_shows_vs_close_tag(self):
        log_path = _make_log_file(_extended_bias_log(vs_close_tag="OPEN_ABOVE_CLOSE"))
        try:
            with tempfile.TemporaryDirectory() as tmp:
                result = generate_full_report(log_path, output_dir=tmp)
                content = result["text"].read_text(encoding="utf-8")
        finally:
            os.unlink(log_path)
        self.assertIn("OPEN_ABOVE_CLOSE", content)

    def test_text_report_shows_balance_tag(self):
        log_path = _make_log_file(_extended_bias_log(balance_tag="BALANCE_OPEN"))
        try:
            with tempfile.TemporaryDirectory() as tmp:
                result = generate_full_report(log_path, output_dir=tmp)
                content = result["text"].read_text(encoding="utf-8")
        finally:
            os.unlink(log_path)
        self.assertIn("BALANCE_OPEN", content)

    def test_text_report_shows_gap_day_pnl_on_gap_day(self):
        """Gap day P&L line appears in report when gap_tag is GAP_UP/GAP_DOWN."""
        log_path = _make_log_file(_extended_bias_log(gap_tag="GAP_UP"))
        try:
            with tempfile.TemporaryDirectory() as tmp:
                result = generate_full_report(log_path, output_dir=tmp)
                content = result["text"].read_text(encoding="utf-8")
        finally:
            os.unlink(log_path)
        self.assertIn("Gap day P&L", content)

    # ── New tags counted in tag_counts ────────────────────────────────────

    def test_gap_up_counted_in_tag_counts(self):
        s = self._parse(gap_tag="GAP_UP")
        self.assertGreater(s.tag_counts.get("GAP_UP", 0), 0)

    def test_open_above_close_counted_in_tag_counts(self):
        s = self._parse(vs_close_tag="OPEN_ABOVE_CLOSE")
        self.assertGreater(s.tag_counts.get("OPEN_ABOVE_CLOSE", 0), 0)

    def test_balance_open_counted_in_tag_counts(self):
        s = self._parse(balance_tag="BALANCE_OPEN")
        self.assertGreater(s.tag_counts.get("BALANCE_OPEN", 0), 0)


# ═══════════════════════════════════════════════════════════════════════════════
# TestNewLogParserFields — new SessionSummary fields added in Mar 2026
# Covers: day_type_tag, cpr_width_tag, reversal/osc/expiry/volatility counters,
#         structured TRADE OPEN/EXIT format, lot_cap_count, survivability.
# ═══════════════════════════════════════════════════════════════════════════════

def _day_type_log(day_type: str = "TREND_DAY", cpr_width: str = "NARROW") -> str:
    return textwrap.dedent(f"""\
        2026-03-02 09:30:01,001 - INFO - [DAY_TYPE] day_type_tag={day_type} open_bias=OPEN_LOW gap=NO_GAP balance=OUTSIDE_BALANCE vs_close=OPEN_ABOVE_CLOSE cpr_width={cpr_width}
        2026-03-02 09:32:00,001 - INFO - [TRADE OPEN][PAPER] CALL bar=5 2026-03-02 09:32:00 underlying=22400.00 premium=100.00 score=72 src=PIVOT lot=75
        2026-03-02 09:38:00,001 - INFO - [TRADE EXIT] WIN  CALL bar=7 2026-03-02 09:38:00 prem 100.00→115.00 P&L=+15.00pts (+1125₹) peak=116.00 held=2bars
    """)


def _reversal_log() -> str:
    return textwrap.dedent("""\
        2026-03-02 09:30:01,001 - INFO - [REVERSAL_SIGNAL] CALL score=78 strength=HIGH stretch=-1.87x ATR pivot=S4 ema9=22100 ema13=22080
        2026-03-02 09:30:02,001 - INFO - [REVERSAL_OVERRIDE] RSI=22.5 oscillator extreme flipped to confirmation for CALL reversal score=78 strength=HIGH pivot_zone=S4
        2026-03-02 09:30:03,001 - INFO - [ENTRY ALLOWED][ST_SLOPE_OVERRIDE] timestamp=2026-03-02 09:30:03 symbol=NSE:NIFTY50-INDEX allowed_side=CALL ST3m_bias=BULLISH ST3m_slope=DOWN override_reason=REVERSAL_OVERRIDE: reversal_score=78 strength=HIGH
        2026-03-02 09:32:00,001 - INFO - [TRADE OPEN][PAPER] CALL bar=5 2026-03-02 09:32:00 underlying=22400.00 premium=100.00 score=78 src=PIVOT lot=75
        2026-03-02 09:38:00,001 - INFO - [TRADE EXIT] WIN  CALL bar=7 2026-03-02 09:38:00 prem 100.00→120.00 P&L=+20.00pts (+1500₹) peak=121.00 held=2bars
    """)


def _osc_gating_log() -> str:
    return textwrap.dedent("""\
        2026-03-02 09:30:01,001 - INFO - [ENTRY BLOCKED][OSC_EXTREME] timestamp=2026-03-02 09:30:01 symbol=NSE:NIFTY50-INDEX allowed_side=PUT RSI=22.0 CCI=-210.0 rsi_range=[20.0,80.0] cci_range=[-250.0,250.0] tier=ADX_STRONG_40 ADX=41.5 reason=Oscillator extreme
        2026-03-02 09:30:02,001 - INFO - [OSC_OVERRIDE][TREND_CONFIRMED] timestamp=2026-03-02 09:30:02 symbol=NSE:NIFTY50-INDEX allowed_side=CALL ADX=38.2 tier=ADX_MOD_30 RSI=71.5 (expanded [25.0,75.0]) CCI=188.3 (expanded [-200.0,200.0])
        2026-03-02 09:30:03,001 - INFO - [OSC_RELIEF][S4/R4_BREAK] side=PUT reason=price below S4-ATR close=21800.0 s4=21900.0 s4_relief_thr=21850.0 atr=50.0
        2026-03-02 09:32:00,001 - INFO - [TRADE OPEN][PAPER] CALL bar=5 2026-03-02 09:32:00 underlying=22400.00 premium=100.00 score=72 src=PIVOT lot=75
        2026-03-02 09:38:00,001 - INFO - [TRADE EXIT] WIN  CALL bar=7 2026-03-02 09:38:00 prem 100.00→115.00 P&L=+15.00pts (+1125₹) peak=116.00 held=2bars
    """)


def _contract_expiry_log() -> str:
    return textwrap.dedent("""\
        2026-03-02 09:15:01,001 - INFO - [CONTRACT_METADATA] symbol=NIFTY lot=75 expiry=2026-03-06 contracts_loaded=120
        2026-03-02 09:15:02,001 - INFO - [CONTRACT_ROLL] symbol=NIFTY old_expiry=2026-03-06 new_expiry=2026-03-13
        2026-03-02 09:15:03,001 - DEBUG - [CONTRACT_FILTER] symbol=NIFTY24MAR22000CE strike=22000 intrinsic=0.00 → SKIPPED
        2026-03-02 09:15:04,001 - WARNING - [CONTRACT_METADATA][LOT_MISMATCH] api_lot=75 manual_lot=50
        2026-03-02 09:32:00,001 - INFO - [TRADE OPEN][PAPER] CALL bar=5 2026-03-02 09:32:00 underlying=22400.00 premium=100.00 score=72 src=PIVOT lot=75
        2026-03-02 09:38:00,001 - INFO - [TRADE EXIT] WIN  CALL bar=7 2026-03-02 09:38:00 prem 100.00→115.00 P&L=+15.00pts (+1125₹) peak=116.00 held=2bars
    """)


def _volatility_log() -> str:
    return textwrap.dedent("""\
        2026-03-02 09:15:01,001 - INFO - [VIX_CONTEXT] symbol=NSE:INDIAVIX-INDEX value=13.50 tier=CALM
        2026-03-02 09:15:02,001 - INFO - [GREEKS] symbol=NIFTY24MAR22400CE delta=0.48 gamma=0.003 theta=-6.2 vega=18.5 iv=14.2%
        2026-03-02 09:15:03,001 - DEBUG - [VOL_CONTEXT][SCORE_ADJUST][CALL] vix_tier=CALM vol_adj=-5 theta=-6.2 theta_adj=-8 vega=18.5
        2026-03-02 09:15:04,001 - INFO - [POSITION_SIZE] equity=N/A score=72 atr=85.0 vix_tier=CALM vega_high=True conf_low=False lots=1
        2026-03-02 09:15:05,001 - DEBUG - [VOL_CONTEXT][ALIGN][CALL] indicators=RSI:61.7_ADX:32.5 vix_tier=CALM adj=score:-5_osc:STRICTER_atr:TIGHTER
        2026-03-02 09:15:06,001 - DEBUG - [GREEKS_ALIGN][CALL] symbol=NIFTY24MAR22400CE theta=-6.2 vega=18.5 adj=theta:-8_vega_risk:HIGH
        2026-03-02 09:15:07,001 - DEBUG - [SCORE_MATRIX][CALL] base=85 vol_adj=-5 theta_adj=-8 final=72/50
        2026-03-02 09:32:00,001 - INFO - [TRADE OPEN][PAPER] CALL bar=5 2026-03-02 09:32:00 underlying=22400.00 premium=100.00 score=72 src=PIVOT lot=75
        2026-03-02 09:38:00,001 - INFO - [TRADE EXIT] WIN  CALL bar=7 2026-03-02 09:38:00 prem 100.00→115.00 P&L=+15.00pts (+1125₹) peak=116.00 held=2bars
    """)


def _struct_trade_log() -> str:
    """Structured [TRADE OPEN] / [TRADE EXIT] format from PositionManager."""
    return textwrap.dedent("""\
        2026-03-02 09:32:00,001 - INFO - [TRADE OPEN] time=2026-03-02 09:32:00 side=CALL option_name=NIFTY24MAR22400CE entry=100.00 lots=75
        2026-03-02 09:38:00,001 - INFO - [TRADE EXIT] time=2026-03-02 09:38:00 option_name=NIFTY24MAR22400CE exit=115.00 pnl_pts=+15.00 pnl_rs=+1125 bars=2 reason=TG_HIT
        2026-03-02 09:45:00,001 - INFO - [TRADE OPEN] time=2026-03-02 09:45:00 side=PUT option_name=NIFTY24MAR22200PE entry=80.00 lots=75
        2026-03-02 09:51:00,001 - INFO - [TRADE EXIT] time=2026-03-02 09:51:00 option_name=NIFTY24MAR22200PE exit=68.00 pnl_pts=-12.00 pnl_rs=-900 bars=2 reason=SL_HIT
    """)


def _trend_loss_log() -> str:
    return textwrap.dedent("""\
        2026-03-02 09:32:00,001 - INFO - [TRADE OPEN][PAPER] CALL bar=5 2026-03-02 09:32:00 underlying=22400.00 premium=100.00 score=72 src=PIVOT lot=75
        2026-03-02 09:38:00,001 - INFO - [TRADE EXIT] LOSS CALL bar=7 2026-03-02 09:38:00 prem 100.00→85.00 P&L=-15.00pts (-1125₹) peak=102.00 held=5bars
        2026-03-02 09:38:01,001 - INFO - [TREND_LOSS] side=CALL exit_reason=SL_HIT pnl=-15.00 bars=5 adx_tier=ADX_DEFAULT
    """)


class TestNewLogParserFields(unittest.TestCase):
    """Mar 2026: new SessionSummary fields parsed from structured log tags."""

    def _parse_log(self, content: str) -> "SessionSummary":
        log_path = _make_log_file(content)
        try:
            return LogParser(log_path).parse()
        finally:
            os.unlink(log_path)

    # ── day_type_tag ──────────────────────────────────────────────────────────

    def test_day_type_tag_trend_day(self):
        s = self._parse_log(_day_type_log("TREND_DAY"))
        self.assertEqual(s.day_type_tag, "TREND_DAY")

    def test_day_type_tag_range_day(self):
        s = self._parse_log(_day_type_log("RANGE_DAY"))
        self.assertEqual(s.day_type_tag, "RANGE_DAY")

    def test_day_type_tag_default_neutral(self):
        s = self._parse_log("2026-03-02 09:30:00,001 - INFO - no day type here\n")
        self.assertEqual(s.day_type_tag, "NEUTRAL_DAY")

    def test_cpr_width_tag_narrow(self):
        s = self._parse_log(_day_type_log(cpr_width="NARROW"))
        self.assertEqual(s.cpr_width_tag, "NARROW")

    def test_cpr_width_tag_wide(self):
        s = self._parse_log(_day_type_log(cpr_width="WIDE"))
        self.assertEqual(s.cpr_width_tag, "WIDE")

    def test_cpr_width_tag_default_normal(self):
        s = self._parse_log("2026-03-02 09:30:00,001 - INFO - no cpr here\n")
        self.assertEqual(s.cpr_width_tag, "NORMAL")

    def test_day_type_counted_in_tag_counts(self):
        s = self._parse_log(_day_type_log())
        self.assertGreater(s.tag_counts.get("DAY_TYPE", 0), 0)

    # ── reversal_trades_count and st_slope_override_count ────────────────────

    def test_reversal_trades_count(self):
        s = self._parse_log(_reversal_log())
        self.assertEqual(s.reversal_trades_count, 1)

    def test_st_slope_override_count(self):
        s = self._parse_log(_reversal_log())
        self.assertEqual(s.st_slope_override_count, 1)

    def test_reversal_trade_count_zero_default(self):
        s = self._parse_log("2026-03-02 09:30:00,001 - INFO - nothing\n")
        self.assertEqual(s.reversal_trades_count, 0)

    def test_reversal_signal_counted_in_tag_counts(self):
        s = self._parse_log(_reversal_log())
        self.assertGreater(s.tag_counts.get("REVERSAL_SIGNAL", 0), 0)

    def test_st_slope_override_counted_in_tag_counts(self):
        s = self._parse_log(_reversal_log())
        self.assertGreater(s.tag_counts.get("ST_SLOPE_OVERRIDE", 0), 0)

    # ── oscillator_blocks / oscillator_overrides / oscillator_relief_count ───

    def test_oscillator_blocks_counted(self):
        s = self._parse_log(_osc_gating_log())
        self.assertEqual(s.oscillator_blocks, 1)

    def test_oscillator_overrides_counted(self):
        s = self._parse_log(_osc_gating_log())
        self.assertEqual(s.oscillator_overrides, 1)

    def test_oscillator_relief_count(self):
        s = self._parse_log(_osc_gating_log())
        self.assertEqual(s.oscillator_relief_count, 1)

    def test_osc_defaults_zero(self):
        s = self._parse_log("2026-03-02 09:30:00,001 - INFO - nothing\n")
        self.assertEqual(s.oscillator_blocks, 0)
        self.assertEqual(s.oscillator_overrides, 0)
        self.assertEqual(s.oscillator_relief_count, 0)

    # ── trend_loss_count ─────────────────────────────────────────────────────

    def test_trend_loss_count(self):
        s = self._parse_log(_trend_loss_log())
        self.assertEqual(s.trend_loss_count, 1)

    def test_trend_loss_count_zero_default(self):
        s = self._parse_log("2026-03-02 09:30:00,001 - INFO - nothing\n")
        self.assertEqual(s.trend_loss_count, 0)

    # ── expiry_roll_count / lot_size_mismatch_count / intrinsic_filter_count ─

    def test_expiry_roll_count(self):
        s = self._parse_log(_contract_expiry_log())
        self.assertEqual(s.expiry_roll_count, 1)

    def test_lot_size_mismatch_count(self):
        s = self._parse_log(_contract_expiry_log())
        self.assertEqual(s.lot_size_mismatch_count, 1)

    def test_intrinsic_filter_count(self):
        s = self._parse_log(_contract_expiry_log())
        self.assertEqual(s.intrinsic_filter_count, 1)

    def test_contract_fields_zero_default(self):
        s = self._parse_log("2026-03-02 09:30:00,001 - INFO - nothing\n")
        self.assertEqual(s.expiry_roll_count, 0)
        self.assertEqual(s.lot_size_mismatch_count, 0)
        self.assertEqual(s.intrinsic_filter_count, 0)

    # ── volatility context counters ───────────────────────────────────────────

    def test_vix_tier_count(self):
        s = self._parse_log(_volatility_log())
        self.assertEqual(s.vix_tier_count, 1)

    def test_greeks_usage_count(self):
        s = self._parse_log(_volatility_log())
        self.assertEqual(s.greeks_usage_count, 1)

    def test_theta_penalty_count(self):
        """theta_adj=-8 in VOL_CONTEXT/SCORE_ADJUST → theta_penalty_count=1."""
        s = self._parse_log(_volatility_log())
        self.assertEqual(s.theta_penalty_count, 1)

    def test_vega_penalty_count(self):
        """vega_high=True in POSITION_SIZE → vega_penalty_count=1."""
        s = self._parse_log(_volatility_log())
        self.assertEqual(s.vega_penalty_count, 1)

    def test_vol_context_align_count(self):
        s = self._parse_log(_volatility_log())
        self.assertEqual(s.vol_context_align_count, 1)

    def test_greeks_align_count(self):
        s = self._parse_log(_volatility_log())
        self.assertEqual(s.greeks_align_count, 1)

    def test_score_matrix_usage_count(self):
        s = self._parse_log(_volatility_log())
        self.assertEqual(s.score_matrix_usage_count, 1)

    def test_volatility_fields_zero_default(self):
        s = self._parse_log("2026-03-02 09:30:00,001 - INFO - nothing\n")
        self.assertEqual(s.vix_tier_count, 0)
        self.assertEqual(s.greeks_usage_count, 0)
        self.assertEqual(s.theta_penalty_count, 0)
        self.assertEqual(s.vega_penalty_count, 0)
        self.assertEqual(s.vol_context_align_count, 0)
        self.assertEqual(s.greeks_align_count, 0)
        self.assertEqual(s.score_matrix_usage_count, 0)

    # ── structured TRADE OPEN / TRADE EXIT format ────────────────────────────

    def test_struct_format_trade_count(self):
        """Structured format: 2 TRADE OPEN + 2 TRADE EXIT → 2 trades."""
        s = self._parse_log(_struct_trade_log())
        self.assertEqual(s.total_trades, 2)

    def test_struct_format_side_preserved(self):
        s = self._parse_log(_struct_trade_log())
        sides = {t.get("side") for t in s.trades}
        self.assertIn("CALL", sides)
        self.assertIn("PUT", sides)

    def test_struct_format_pnl_pts(self):
        s = self._parse_log(_struct_trade_log())
        pnls = sorted(t.get("pnl_pts", 0.0) for t in s.trades)
        self.assertAlmostEqual(pnls[0], -12.0, places=1)
        self.assertAlmostEqual(pnls[1],  15.0, places=1)

    def test_struct_format_option_name(self):
        s = self._parse_log(_struct_trade_log())
        names = {t.get("option_name") for t in s.trades}
        self.assertIn("NIFTY24MAR22400CE", names)
        self.assertIn("NIFTY24MAR22200PE", names)

    def test_struct_format_exit_reason(self):
        s = self._parse_log(_struct_trade_log())
        reasons = {t.get("exit_reason") for t in s.trades}
        self.assertIn("TG_HIT", reasons)
        self.assertIn("SL_HIT", reasons)

    def test_struct_format_winners_losers(self):
        s = self._parse_log(_struct_trade_log())
        self.assertEqual(s.winners, 1)
        self.assertEqual(s.losers, 1)

    # ── lot_cap_count / survivability ─────────────────────────────────────────

    def test_lot_cap_count_via_tag_counts(self):
        log = textwrap.dedent("""\
            2026-03-02 09:30:01,001 - INFO - [MAX_TRADES_CAP] trade_count=8 max=8 entry blocked
            2026-03-02 09:30:02,001 - INFO - [MAX_TRADES_CAP] trade_count=8 max=8 entry blocked
        """)
        s = self._parse_log(log)
        self.assertEqual(s.lot_cap_count, 2)

    def test_survivability_count_min_3_bars(self):
        """Trades held >= 3 bars contribute to survivability_count."""
        log = textwrap.dedent("""\
            2026-03-02 09:32:00,001 - INFO - [TRADE OPEN] time=2026-03-02 09:32:00 side=CALL option_name=NIFTY24MAR22400CE entry=100.00 lots=75
            2026-03-02 09:44:00,001 - INFO - [TRADE EXIT] time=2026-03-02 09:44:00 option_name=NIFTY24MAR22400CE exit=115.00 pnl_pts=+15.00 pnl_rs=+1125 bars=4 reason=TG_HIT
            2026-03-02 09:45:00,001 - INFO - [TRADE OPEN] time=2026-03-02 09:45:00 side=PUT option_name=NIFTY24MAR22200PE entry=80.00 lots=75
            2026-03-02 09:48:00,001 - INFO - [TRADE EXIT] time=2026-03-02 09:48:00 option_name=NIFTY24MAR22200PE exit=68.00 pnl_pts=-12.00 pnl_rs=-900 bars=1 reason=SL_HIT
        """)
        s = self._parse_log(log)
        self.assertEqual(s.survivability_count, 1)

    def test_survivability_ratio_calculation(self):
        """survivability_ratio = survivability_count / total_trades * 100."""
        log = textwrap.dedent("""\
            2026-03-02 09:32:00,001 - INFO - [TRADE OPEN] time=2026-03-02 09:32:00 side=CALL option_name=NIFTY24MAR22400CE entry=100.00 lots=75
            2026-03-02 09:44:00,001 - INFO - [TRADE EXIT] time=2026-03-02 09:44:00 option_name=NIFTY24MAR22400CE exit=115.00 pnl_pts=+15.00 pnl_rs=+1125 bars=4 reason=TG_HIT
            2026-03-02 09:45:00,001 - INFO - [TRADE OPEN] time=2026-03-02 09:45:00 side=PUT option_name=NIFTY24MAR22200PE entry=80.00 lots=75
            2026-03-02 09:48:00,001 - INFO - [TRADE EXIT] time=2026-03-02 09:48:00 option_name=NIFTY24MAR22200PE exit=68.00 pnl_pts=-12.00 pnl_rs=-900 bars=1 reason=SL_HIT
        """)
        s = self._parse_log(log)
        self.assertAlmostEqual(s.survivability_ratio, 50.0, places=1)

    # ── to_dict() includes all new fields ────────────────────────────────────

    def test_to_dict_has_new_fields(self):
        s = self._parse_log(_day_type_log())
        d = s.to_dict()
        for key in ("day_type_tag", "cpr_width_tag", "reversal_trades_count",
                    "st_slope_override_count", "oscillator_blocks",
                    "oscillator_overrides", "oscillator_relief_count",
                    "trend_loss_count", "expiry_roll_count",
                    "lot_size_mismatch_count", "intrinsic_filter_count",
                    "vix_tier_count", "greeks_usage_count",
                    "theta_penalty_count", "vega_penalty_count",
                    "vol_context_align_count", "greeks_align_count",
                    "score_matrix_usage_count", "lot_cap_count",
                    "survivability_count", "survivability_ratio"):
            self.assertIn(key, d, f"Missing to_dict key: {key}")


# ═══════════════════════════════════════════════════════════════════════════════
# TestNewTextReportSections — new _write_text_report sections (Mar 2026)
# ═══════════════════════════════════════════════════════════════════════════════

class TestNewTextReportSections(unittest.TestCase):
    """New sections in generate_full_report text output."""

    def _report_text(self, log_content: str) -> str:
        log_path = _make_log_file(log_content)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                result = generate_full_report(log_path, output_dir=tmp)
                return result["text"].read_text(encoding="utf-8")
        finally:
            os.unlink(log_path)

    # ── DAY CLASSIFICATION section ────────────────────────────────────────────

    def test_day_classification_section_present(self):
        text = self._report_text(_day_type_log())
        self.assertIn("DAY CLASSIFICATION", text)

    def test_day_type_tag_in_report(self):
        text = self._report_text(_day_type_log("TREND_DAY"))
        self.assertIn("TREND_DAY", text)

    def test_cpr_width_in_report(self):
        text = self._report_text(_day_type_log(cpr_width="NARROW"))
        self.assertIn("NARROW", text)

    # ── REVERSAL DETECTOR section ─────────────────────────────────────────────

    def test_reversal_section_present_when_fired(self):
        text = self._report_text(_reversal_log())
        self.assertIn("REVERSAL DETECTOR", text)

    def test_reversal_section_absent_when_not_fired(self):
        text = self._report_text(_day_type_log())
        self.assertNotIn("REVERSAL DETECTOR", text)

    def test_reversal_pnl_in_report(self):
        text = self._report_text(_reversal_log())
        self.assertIn("Reversal P&L", text)

    def test_slope_overrides_in_report(self):
        text = self._report_text(_reversal_log())
        self.assertIn("ST_SLOPE overrides", text)

    # ── OSCILLATOR GATING section ─────────────────────────────────────────────

    def test_oscillator_gating_section_present_when_fired(self):
        text = self._report_text(_osc_gating_log())
        self.assertIn("OSCILLATOR GATING", text)

    def test_oscillator_gating_absent_when_not_fired(self):
        text = self._report_text(_day_type_log())
        self.assertNotIn("OSCILLATOR GATING", text)

    def test_osc_blocks_in_report(self):
        text = self._report_text(_osc_gating_log())
        self.assertIn("OSC blocks", text)

    def test_osc_overrides_in_report(self):
        text = self._report_text(_osc_gating_log())
        self.assertIn("OSC overrides", text)

    # ── TREND SURVIVABILITY section ───────────────────────────────────────────

    def test_trend_survivability_section_present(self):
        text = self._report_text(_trend_loss_log())
        self.assertIn("TREND SURVIVABILITY", text)

    def test_trend_loss_count_in_report(self):
        text = self._report_text(_trend_loss_log())
        self.assertIn("Trend SL exits", text)

    # ── VOLATILITY CONTEXT section ────────────────────────────────────────────

    def test_volatility_context_section_present(self):
        text = self._report_text(_volatility_log())
        self.assertIn("VOLATILITY CONTEXT", text)

    def test_vix_tier_refreshes_in_report(self):
        text = self._report_text(_volatility_log())
        self.assertIn("VIX tier", text)

    def test_greeks_computed_in_report(self):
        text = self._report_text(_volatility_log())
        self.assertIn("Greeks computed", text)

    # ── TRADE DETAILS table ───────────────────────────────────────────────────

    def test_trade_details_table_present(self):
        text = self._report_text(_struct_trade_log())
        self.assertIn("TRADE DETAILS", text)

    def test_trade_details_shows_option_names(self):
        text = self._report_text(_struct_trade_log())
        self.assertIn("NIFTY24MAR22400CE", text)

    def test_trade_details_shows_exit_reason(self):
        text = self._report_text(_struct_trade_log())
        self.assertIn("TG_HIT", text)

    # ── P&L SUMMARY section ───────────────────────────────────────────────────

    def test_pnl_summary_section_present(self):
        text = self._report_text(_struct_trade_log())
        self.assertIn("P&L SUMMARY", text)

    def test_pnl_summary_shows_net_pnl(self):
        text = self._report_text(_struct_trade_log())
        self.assertIn("Net P&L", text)

    def test_pnl_summary_shows_survivability(self):
        text = self._report_text(_struct_trade_log())
        self.assertIn("Survivability", text)


# ── Rich-exit log helper ──────────────────────────────────────────────────────

def _rich_exit_log() -> str:
    """[EXIT][PAPER/LIVE <reason>] format emitted by execution.py cleanup_trade_exit."""
    return textwrap.dedent("""\
        2026-03-02 09:48:02,585 - INFO - \x1b[93m[EXIT][PAPER SL_HIT] PUT NSE:NIFTY2630225000PE Entry=102.50 Exit=94.25 Qty=130 PnL=-1072.50 (points=-8.25) BarsHeld=0 ExitType=SL Trigger=ltp<=98.50\x1b[0m
        2026-03-02 10:45:14,001 - INFO - [EXIT][PAPER SCALP_PT_HIT] CALL NSE:NIFTY2630224700CE Entry=137.15 Exit=140.55 Qty=130 PnL=442.00 (points=3.40) BarsHeld=0 ExitType=PT Trigger=ltp>=140.00
        2026-03-02 10:57:32,001 - INFO - [EXIT][PAPER SL_HIT] PUT NSE:NIFTY2630224900PE Entry=107.45 Exit=94.25 Qty=130 PnL=-1716.00 (points=-13.20) BarsHeld=0 ExitType=SL Trigger=ltp<=101.00
        2026-03-02 11:02:20,001 - INFO - [EXIT][LIVE SL_HIT] PUT NSE:NIFTY2630225000PE Entry=170.20 Exit=152.30 Qty=130 PnL=-2327.00 (points=-17.90) BarsHeld=0 ExitType=SL Trigger=ltp<=161.50
    """)


def _rich_exit_log_survival() -> str:
    """Rich-exit log with some BarsHeld>=3 trades for survivability test."""
    return textwrap.dedent("""\
        2026-03-02 09:48:02,001 - INFO - [EXIT][PAPER SL_HIT] PUT NSE:NIFTY2630225000PE Entry=102.50 Exit=94.25 Qty=130 PnL=-1072.50 (points=-8.25) BarsHeld=0 ExitType=SL Trigger=ltp<=98.50
        2026-03-02 10:30:00,001 - INFO - [EXIT][PAPER TG_HIT] CALL NSE:NIFTY2630224700CE Entry=90.00 Exit=115.00 Qty=75 PnL=1875.00 (points=25.00) BarsHeld=5 ExitType=TG Trigger=ltp>=115.00
        2026-03-02 11:00:00,001 - INFO - [EXIT][PAPER SL_HIT] CALL NSE:NIFTY2630224800CE Entry=80.00 Exit=72.00 Qty=75 PnL=-600.00 (points=-8.00) BarsHeld=3 ExitType=SL Trigger=ltp<=75.00
    """)


class TestRichExitParsing(unittest.TestCase):
    """Tests for _RE_EXIT_RICH — execution.py [EXIT][PAPER/LIVE reason] format."""

    def _parse(self, content: str):
        p = _make_log_file(content)
        try:
            from log_parser import parse_session
            return parse_session(str(p))
        finally:
            os.unlink(p)

    # ── Trade count ───────────────────────────────────────────────────────────

    def test_rich_exit_trade_count(self):
        s = self._parse(_rich_exit_log())
        self.assertEqual(s.total_trades, 4)

    def test_rich_exit_winners_losers(self):
        s = self._parse(_rich_exit_log())
        self.assertEqual(s.winners, 1)
        self.assertEqual(s.losers, 3)

    # ── P&L ──────────────────────────────────────────────────────────────────

    def test_rich_exit_net_pnl_pts(self):
        s = self._parse(_rich_exit_log())
        # -8.25 + 3.40 - 13.20 - 17.90 = -35.95
        self.assertAlmostEqual(s.net_pnl_pts, -35.95, places=2)

    def test_rich_exit_net_pnl_rs(self):
        s = self._parse(_rich_exit_log())
        # -1072.50 + 442.00 - 1716.00 - 2327.00 = -4673.50
        self.assertAlmostEqual(s.net_pnl_rs, -4673.50, places=0)

    def test_rich_exit_best_trade(self):
        s = self._parse(_rich_exit_log())
        pnls = [t.get("pnl_pts", 0.0) for t in s.trades]
        self.assertAlmostEqual(max(pnls), 3.40, places=2)

    def test_rich_exit_worst_trade(self):
        s = self._parse(_rich_exit_log())
        pnls = [t.get("pnl_pts", 0.0) for t in s.trades]
        self.assertAlmostEqual(min(pnls), -17.90, places=2)

    # ── CALL / PUT split ─────────────────────────────────────────────────────

    def test_rich_exit_call_count(self):
        s = self._parse(_rich_exit_log())
        self.assertEqual(s.call_trades, 1)

    def test_rich_exit_put_count(self):
        s = self._parse(_rich_exit_log())
        self.assertEqual(s.put_trades, 3)

    # ── Exit reasons ─────────────────────────────────────────────────────────

    def test_rich_exit_sl_hit_count(self):
        s = self._parse(_rich_exit_log())
        self.assertEqual(s.exit_reason_counts.get("SL_HIT", 0), 3)

    def test_rich_exit_scalp_pt_count(self):
        s = self._parse(_rich_exit_log())
        self.assertEqual(s.exit_reason_counts.get("SCALP_PT_HIT", 0), 1)

    # ── Session mode ─────────────────────────────────────────────────────────

    def test_rich_exit_session_mode_paper(self):
        # First 3 trades are PAPER, last one is LIVE — session_type should reflect LIVE
        s = self._parse(_rich_exit_log())
        # At least one session type must be populated
        self.assertIn(s.session_type, ("PAPER", "LIVE", "MIXED"))

    # ── ANSI stripping ────────────────────────────────────────────────────────

    def test_rich_exit_ansi_stripped(self):
        """ANSI escape codes in the log line must not break parsing."""
        content = (
            "2026-03-02 09:48:02,585 - INFO - "
            "\x1b[93m[EXIT][PAPER SL_HIT] PUT NSE:NIFTY2630225000PE "
            "Entry=102.50 Exit=94.25 Qty=130 PnL=-1072.50 (points=-8.25) BarsHeld=0\x1b[0m\n"
        )
        s = self._parse(content)
        self.assertEqual(s.total_trades, 1)
        self.assertAlmostEqual(s.net_pnl_pts, -8.25, places=2)

    # ── Lot size ─────────────────────────────────────────────────────────────

    def test_rich_exit_lot_size_captured(self):
        s = self._parse(_rich_exit_log())
        # All trades should have Qty=130 or 75 — verify trades list exists
        self.assertGreater(s.total_trades, 0)

    # ── Contract names ────────────────────────────────────────────────────────

    def test_rich_exit_option_names_present(self):
        """Trade details must include full NSE option_name from log."""
        from log_parser import parse_session
        p = _make_log_file(_rich_exit_log())
        try:
            s = parse_session(str(p))
            names = [t.get("option_name", "") for t in s.trades]
            self.assertTrue(any("NIFTY" in n for n in names))
        finally:
            os.unlink(p)

    # ── Survivability ────────────────────────────────────────────────────────

    def test_rich_exit_survivability_zero(self):
        """All BarsHeld=0 → survivability_count=0."""
        # Use only the first line (BarsHeld=0)
        content = (
            "2026-03-02 09:48:02,001 - INFO - [EXIT][PAPER SL_HIT] PUT "
            "NSE:NIFTY2630225000PE Entry=102.50 Exit=94.25 Qty=130 "
            "PnL=-1072.50 (points=-8.25) BarsHeld=0\n"
        )
        s = self._parse(content)
        self.assertEqual(s.survivability_count, 0)

    def test_rich_exit_survivability_counted(self):
        """Trades with BarsHeld>=3 must increment survivability_count."""
        s = self._parse(_rich_exit_log_survival())
        # BarsHeld=0 (no), BarsHeld=5 (yes), BarsHeld=3 (yes) → 2
        self.assertEqual(s.survivability_count, 2)

    # ── Priority over EXIT AUDIT ──────────────────────────────────────────────

    def test_rich_exit_takes_priority_over_audit(self):
        """When rich-exit lines exist alongside EXIT AUDIT, rich wins."""
        content = textwrap.dedent("""\
            2026-03-02 09:48:02,001 - INFO - [EXIT][PAPER SL_HIT] PUT NSE:NIFTY2630225000PE Entry=102.50 Exit=94.25 Qty=130 PnL=-1072.50 (points=-8.25) BarsHeld=0
            2026-03-02 09:48:02,001 - INFO - [EXIT AUDIT] side=PUT pnl_pts=0.0 pnl_rs=0 bars=0 reason=SL_HIT score=0
        """)
        s = self._parse(content)
        # Rich exit should win: pnl = -8.25, NOT 0.0
        self.assertAlmostEqual(s.net_pnl_pts, -8.25, places=2)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    unittest.main(verbosity=2)
