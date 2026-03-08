"""Unit tests for Phase 5: Dashboard Regime Attribution.

Tests:
- LogParser parsing of [REGIME_CONTEXT] tags
- LogParser parsing of [EXIT AUDIT][REGIME_ADAPTIVE] tags
- Regime-at-entry attachment to trades
- SessionSummary.regime_performance property
- SessionSummary.to_dict() includes regime fields
- Dashboard _write_text_report() regime breakdown sections
"""

import tempfile
import textwrap
import unittest
from pathlib import Path

from log_parser import LogParser, SessionSummary


def _make_log(content: str) -> Path:
    """Write content to a temp log file and return its Path."""
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix="_2026-03-07.log", delete=False, encoding="utf-8"
    )
    f.write(textwrap.dedent(content))
    f.close()
    return Path(f.name)


# ═══════════════════════════════════════════════════════════════════════════════
# LogParser — [REGIME_CONTEXT] parsing
# ═══════════════════════════════════════════════════════════════════════════════

class TestRegimeContextParsing(unittest.TestCase):
    """Test parsing of [REGIME_CONTEXT] log lines."""

    def test_regime_context_counted(self):
        log = _make_log("""\
        2026-03-07 09:45:00,000 - INFO - [REGIME_CONTEXT] 2026-03-07 09:45:00 NSE:NIFTY ATR=120.5(ATR_MODERATE) ADX=28.3(ADX_DEFAULT) day=TREND_DAY cpr=NARROW ST=BULLISH/BULLISH slope=UP RSI=55.0 CCI=120.0 osc=ZoneC
        2026-03-07 09:48:00,000 - INFO - [REGIME_CONTEXT] 2026-03-07 09:48:00 NSE:NIFTY ATR=125.0(ATR_HIGH) ADX=42.1(ADX_STRONG_40) day=TREND_DAY cpr=NARROW ST=BULLISH/BULLISH slope=UP RSI=58.0 CCI=130.0 osc=ZoneC
        """)
        s = LogParser(log).parse()
        self.assertEqual(s.regime_context_count, 2)
        self.assertEqual(s.tag_counts.get("REGIME_CONTEXT", 0), 2)

    def test_regime_context_zero_when_absent(self):
        log = _make_log("2026-03-07 09:45:00,000 - INFO - Some normal log line\n")
        s = LogParser(log).parse()
        self.assertEqual(s.regime_context_count, 0)

    def test_regime_context_extracts_fields(self):
        """The last REGIME_CONTEXT should update _last_regime which gets attached to trades."""
        log = _make_log("""\
        2026-03-07 09:45:00,000 - INFO - [REGIME_CONTEXT] 2026-03-07 09:45:00 NSE:NIFTY ATR=120.5(ATR_MODERATE) ADX=28.3(ADX_DEFAULT) day=TREND_DAY cpr=NARROW
        2026-03-07 09:48:00,000 - INFO - [EXIT][PAPER SL_HIT] CALL NSE:NIFTY2630225000CE Entry=100.00 Exit=92.00 Qty=50 PnL=-400.00 (points=-8.00) BarsHeld=3
        """)
        s = LogParser(log).parse()
        self.assertEqual(s.total_trades, 1)
        t = s.trades[0]
        regime = t.get("regime_at_entry", {})
        self.assertEqual(regime.get("atr_regime"), "ATR_MODERATE")
        self.assertEqual(regime.get("adx_tier"), "ADX_DEFAULT")
        self.assertEqual(regime.get("day_type"), "TREND_DAY")
        self.assertEqual(regime.get("cpr_width"), "NARROW")


# ═══════════════════════════════════════════════════════════════════════════════
# LogParser — [EXIT AUDIT][REGIME_ADAPTIVE] parsing
# ═══════════════════════════════════════════════════════════════════════════════

class TestRegimeAdaptiveParsing(unittest.TestCase):
    """Test parsing of [EXIT AUDIT][REGIME_ADAPTIVE] log lines."""

    def test_regime_adaptive_counted(self):
        log = _make_log("""\
        2026-03-07 10:00:00,000 - INFO - [EXIT AUDIT][REGIME_ADAPTIVE] day_type=TREND_DAY adx_tier=ADX_STRONG_40 gap_tag=NO_GAP min_hold_adj=+1 trail_step=8 time_exit=18 gap_suppress=OFF
        """)
        s = LogParser(log).parse()
        self.assertEqual(s.regime_adaptive_count, 1)
        self.assertEqual(s.tag_counts.get("REGIME_ADAPTIVE", 0), 1)

    def test_regime_adaptive_zero_when_absent(self):
        log = _make_log("2026-03-07 09:45:00,000 - INFO - Some normal log line\n")
        s = LogParser(log).parse()
        self.assertEqual(s.regime_adaptive_count, 0)


# ═══════════════════════════════════════════════════════════════════════════════
# Regime trade breakdown + regime_performance property
# ═══════════════════════════════════════════════════════════════════════════════

class TestRegimeTradeBreakdown(unittest.TestCase):
    """Test regime_trade_breakdown construction and regime_performance."""

    def _make_session_with_trades(self):
        """Create a log with 2 trades under different regimes."""
        log = _make_log("""\
        2026-03-07 09:45:00,000 - INFO - [REGIME_CONTEXT] 2026-03-07 09:45:00 NSE:NIFTY ATR=120.5(ATR_MODERATE) ADX=28.3(ADX_DEFAULT) day=TREND_DAY cpr=NARROW
        2026-03-07 09:48:00,000 - INFO - [EXIT][PAPER SL_HIT] CALL NSE:NIFTY2630225000CE Entry=100.00 Exit=92.00 Qty=50 PnL=-400.00 (points=-8.00) BarsHeld=3
        2026-03-07 10:00:00,000 - INFO - [REGIME_CONTEXT] 2026-03-07 10:00:00 NSE:NIFTY ATR=80.0(ATR_LOW) ADX=15.0(ADX_WEAK_20) day=RANGE_DAY cpr=WIDE
        2026-03-07 10:05:00,000 - INFO - [EXIT][PAPER TARGET_HIT] PUT NSE:NIFTY2630224500PE Entry=80.00 Exit=95.00 Qty=50 PnL=750.00 (points=+15.00) BarsHeld=5
        """)
        return LogParser(log).parse()

    def test_breakdown_has_both_regimes(self):
        s = self._make_session_with_trades()
        breakdown = s.regime_trade_breakdown
        self.assertIn("day_type", breakdown)
        self.assertIn("TREND_DAY", breakdown["day_type"])
        self.assertIn("RANGE_DAY", breakdown["day_type"])

    def test_breakdown_pnl_values(self):
        s = self._make_session_with_trades()
        breakdown = s.regime_trade_breakdown
        # First trade: TREND_DAY, -8.0 pts
        self.assertEqual(breakdown["day_type"]["TREND_DAY"], [-8.0])
        # Second trade: RANGE_DAY, +15.0 pts
        self.assertEqual(breakdown["day_type"]["RANGE_DAY"], [15.0])

    def test_breakdown_adx_tier(self):
        s = self._make_session_with_trades()
        breakdown = s.regime_trade_breakdown
        self.assertIn("ADX_DEFAULT", breakdown["adx_tier"])
        self.assertIn("ADX_WEAK_20", breakdown["adx_tier"])

    def test_breakdown_atr_regime(self):
        s = self._make_session_with_trades()
        breakdown = s.regime_trade_breakdown
        self.assertIn("ATR_MODERATE", breakdown["atr_regime"])
        self.assertIn("ATR_LOW", breakdown["atr_regime"])

    def test_breakdown_cpr_width(self):
        s = self._make_session_with_trades()
        breakdown = s.regime_trade_breakdown
        self.assertIn("NARROW", breakdown["cpr_width"])
        self.assertIn("WIDE", breakdown["cpr_width"])

    def test_regime_performance_property(self):
        s = self._make_session_with_trades()
        perf = s.regime_performance
        # Day type performance
        self.assertEqual(perf["day_type"]["TREND_DAY"]["trades"], 1)
        self.assertEqual(perf["day_type"]["TREND_DAY"]["winners"], 0)
        self.assertEqual(perf["day_type"]["TREND_DAY"]["win_rate"], 0.0)
        self.assertEqual(perf["day_type"]["TREND_DAY"]["net_pnl"], -8.0)

        self.assertEqual(perf["day_type"]["RANGE_DAY"]["trades"], 1)
        self.assertEqual(perf["day_type"]["RANGE_DAY"]["winners"], 1)
        self.assertEqual(perf["day_type"]["RANGE_DAY"]["win_rate"], 100.0)
        self.assertEqual(perf["day_type"]["RANGE_DAY"]["net_pnl"], 15.0)


class TestRegimePerformanceEdgeCases(unittest.TestCase):
    """Edge cases for regime_performance."""

    def test_empty_session(self):
        s = SessionSummary(log_path="test.log", session_type="REPLAY", date_tag="2026-03-07")
        perf = s.regime_performance
        for dim in ("day_type", "adx_tier", "atr_regime", "cpr_width"):
            self.assertEqual(perf[dim], {})

    def test_no_regime_context_defaults_unknown(self):
        """Trades without REGIME_CONTEXT logged get UNKNOWN labels."""
        log = _make_log("""\
        2026-03-07 09:48:00,000 - INFO - [EXIT][PAPER SL_HIT] CALL NSE:NIFTY2630225000CE Entry=100.00 Exit=92.00 Qty=50 PnL=-400.00 (points=-8.00) BarsHeld=3
        """)
        s = LogParser(log).parse()
        perf = s.regime_performance
        self.assertIn("UNKNOWN", perf["day_type"])
        self.assertEqual(perf["day_type"]["UNKNOWN"]["trades"], 1)


# ═══════════════════════════════════════════════════════════════════════════════
# SessionSummary.to_dict() includes regime fields
# ═══════════════════════════════════════════════════════════════════════════════

class TestToDictRegimeFields(unittest.TestCase):
    """Verify to_dict() includes regime attribution fields."""

    def test_to_dict_has_regime_fields(self):
        s = SessionSummary(
            log_path="test.log", session_type="REPLAY", date_tag="2026-03-07",
            regime_context_count=5, regime_adaptive_count=3,
        )
        d = s.to_dict()
        self.assertIn("regime_context_count", d)
        self.assertIn("regime_adaptive_count", d)
        self.assertIn("regime_performance", d)
        self.assertEqual(d["regime_context_count"], 5)
        self.assertEqual(d["regime_adaptive_count"], 3)


# ═══════════════════════════════════════════════════════════════════════════════
# Dashboard text report — regime breakdown sections
# ═══════════════════════════════════════════════════════════════════════════════

class TestDashboardRegimeSection(unittest.TestCase):
    """Test that _write_text_report includes regime breakdown."""

    def test_regime_section_appears_when_data_present(self):
        from dashboard import _write_text_report
        import tempfile

        s = SessionSummary(
            log_path="test.log", session_type="REPLAY", date_tag="2026-03-07",
            trades=[
                {"side": "CALL", "pnl_pts": 10.0, "pnl_rs": 500.0, "bars_held": 5,
                 "regime_at_entry": {"day_type": "TREND_DAY", "adx_tier": "ADX_STRONG_40",
                                     "atr_regime": "ATR_MODERATE", "cpr_width": "NARROW"}},
                {"side": "PUT", "pnl_pts": -5.0, "pnl_rs": -250.0, "bars_held": 2,
                 "regime_at_entry": {"day_type": "RANGE_DAY", "adx_tier": "ADX_WEAK_20",
                                     "atr_regime": "ATR_LOW", "cpr_width": "WIDE"}},
            ],
            regime_context_count=10,
            regime_adaptive_count=2,
            regime_trade_breakdown={
                "day_type": {"TREND_DAY": [10.0], "RANGE_DAY": [-5.0]},
                "adx_tier": {"ADX_STRONG_40": [10.0], "ADX_WEAK_20": [-5.0]},
                "atr_regime": {"ATR_MODERATE": [10.0], "ATR_LOW": [-5.0]},
                "cpr_width": {"NARROW": [10.0], "WIDE": [-5.0]},
            },
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "report.txt"
            _write_text_report(s, out)
            text = out.read_text(encoding="utf-8")

        self.assertIn("DAY TYPE PERFORMANCE", text)
        self.assertIn("ADX TIER PERFORMANCE", text)
        self.assertIn("ATR REGIME PERFORMANCE", text)
        self.assertIn("CPR WIDTH PERFORMANCE", text)
        self.assertIn("TREND_DAY", text)
        self.assertIn("RANGE_DAY", text)
        self.assertIn("Regime context logs", text)
        self.assertIn("Regime adaptive logs", text)

    def test_regime_section_absent_when_no_data(self):
        from dashboard import _write_text_report
        import tempfile

        s = SessionSummary(
            log_path="test.log", session_type="REPLAY", date_tag="2026-03-07",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "report.txt"
            _write_text_report(s, out)
            text = out.read_text(encoding="utf-8")

        self.assertNotIn("DAY TYPE PERFORMANCE", text)
        self.assertNotIn("ATR REGIME PERFORMANCE", text)

    def test_regime_section_skips_unknown_only(self):
        from dashboard import _write_text_report
        import tempfile

        s = SessionSummary(
            log_path="test.log", session_type="REPLAY", date_tag="2026-03-07",
            trades=[{"side": "CALL", "pnl_pts": 5.0, "pnl_rs": 250.0, "bars_held": 3}],
            regime_trade_breakdown={
                "day_type": {"UNKNOWN": [5.0]},
                "adx_tier": {"UNKNOWN": [5.0]},
                "atr_regime": {"UNKNOWN": [5.0]},
                "cpr_width": {"UNKNOWN": [5.0]},
            },
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "report.txt"
            _write_text_report(s, out)
            text = out.read_text(encoding="utf-8")

        self.assertNotIn("DAY TYPE PERFORMANCE", text)


# ═══════════════════════════════════════════════════════════════════════════════
# End-to-end: parse log with regime context + verify dashboard output
# ═══════════════════════════════════════════════════════════════════════════════

class TestEndToEndRegimeAttribution(unittest.TestCase):
    """Full pipeline: log -> parser -> summary -> dashboard."""

    def test_full_pipeline(self):
        from dashboard import _write_text_report
        import tempfile

        log = _make_log("""\
        2026-03-07 09:45:00,000 - INFO - [REGIME_CONTEXT] 2026-03-07 09:45:00 NSE:NIFTY ATR=120.5(ATR_MODERATE) ADX=35.0(ADX_DEFAULT) day=TREND_DAY cpr=NARROW
        2026-03-07 09:48:00,000 - INFO - [EXIT][PAPER TARGET_HIT] CALL NSE:NIFTY2630225000CE Entry=100.00 Exit=115.00 Qty=50 PnL=750.00 (points=+15.00) BarsHeld=5
        2026-03-07 09:50:00,000 - INFO - [EXIT AUDIT][REGIME_ADAPTIVE] day_type=TREND_DAY adx_tier=ADX_DEFAULT gap_tag=NO_GAP min_hold_adj=+1 trail_step=8 time_exit=18 gap_suppress=OFF
        2026-03-07 10:00:00,000 - INFO - [REGIME_CONTEXT] 2026-03-07 10:00:00 NSE:NIFTY ATR=90.0(ATR_LOW) ADX=18.0(ADX_WEAK_20) day=RANGE_DAY cpr=WIDE
        2026-03-07 10:05:00,000 - INFO - [EXIT][PAPER SL_HIT] PUT NSE:NIFTY2630224500PE Entry=80.00 Exit=72.00 Qty=50 PnL=-400.00 (points=-8.00) BarsHeld=2
        """)
        s = LogParser(log).parse()

        # Verify counts
        self.assertEqual(s.regime_context_count, 2)
        self.assertEqual(s.regime_adaptive_count, 1)
        self.assertEqual(s.total_trades, 2)

        # Verify trade 1 regime
        t1 = s.trades[0]
        self.assertEqual(t1["regime_at_entry"]["day_type"], "TREND_DAY")
        self.assertEqual(t1["regime_at_entry"]["atr_regime"], "ATR_MODERATE")

        # Verify trade 2 regime
        t2 = s.trades[1]
        self.assertEqual(t2["regime_at_entry"]["day_type"], "RANGE_DAY")
        self.assertEqual(t2["regime_at_entry"]["atr_regime"], "ATR_LOW")

        # Verify regime_performance
        perf = s.regime_performance
        self.assertEqual(perf["day_type"]["TREND_DAY"]["trades"], 1)
        self.assertEqual(perf["day_type"]["TREND_DAY"]["winners"], 1)
        self.assertEqual(perf["day_type"]["RANGE_DAY"]["trades"], 1)
        self.assertEqual(perf["day_type"]["RANGE_DAY"]["winners"], 0)

        # Verify dashboard output
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "report.txt"
            from dashboard import _write_text_report
            _write_text_report(s, out)
            text = out.read_text(encoding="utf-8")

        self.assertIn("DAY TYPE PERFORMANCE", text)
        self.assertIn("TREND_DAY", text)
        self.assertIn("RANGE_DAY", text)


# ═══════════════════════════════════════════════════════════════════════════════
# Backward compatibility
# ═══════════════════════════════════════════════════════════════════════════════

class TestBackwardCompatibility(unittest.TestCase):
    """Ensure old logs without regime tags still parse correctly."""

    def test_old_log_no_regime_tags(self):
        log = _make_log("""\
        2026-03-07 09:45:00,000 - INFO - [SIGNAL FIRED] CALL score=83 strength=HIGH
        2026-03-07 09:45:00,000 - INFO - [ENTRY OK] CALL score=83/50 MODERATE HIGH
        """)
        s = LogParser(log).parse()
        self.assertEqual(s.regime_context_count, 0)
        self.assertEqual(s.regime_adaptive_count, 0)
        perf = s.regime_performance
        for dim in ("day_type", "adx_tier", "atr_regime", "cpr_width"):
            self.assertEqual(perf[dim], {})

    def test_session_summary_default_regime_fields(self):
        s = SessionSummary(log_path="x.log", session_type="REPLAY", date_tag="2026-03-07")
        self.assertEqual(s.regime_context_count, 0)
        self.assertEqual(s.regime_adaptive_count, 0)
        self.assertEqual(s.regime_trade_breakdown, {})


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 4 tag counting (ZONE / PULSE)
# ═══════════════════════════════════════════════════════════════════════════════

class TestZonePulseTagCounting(unittest.TestCase):
    """Test that [ZONE] and [PULSE] tags are counted in tag_counts."""

    def test_zone_tag_counted_standalone(self):
        """[ZONE] on a standalone line (not inside [ENTRY OK]) is counted."""
        log = _make_log("""\
        2026-03-07 09:45:00,000 - INFO - [ZONE][SCORE][CALL] breakout aligned +10 zone_type=DEMAND
        """)
        s = LogParser(log).parse()
        self.assertGreaterEqual(s.tag_counts.get("ZONE", 0), 1)

    def test_pulse_tag_counted_standalone(self):
        """[PULSE] on a standalone line (not inside [ENTRY OK]) is counted."""
        log = _make_log("""\
        2026-03-07 09:45:00,000 - INFO - [PULSE][SCORE][CALL] burst+drift=UP aligned +8 tick_rate=20.0
        """)
        s = LogParser(log).parse()
        self.assertGreaterEqual(s.tag_counts.get("PULSE", 0), 1)

    def test_zone_pulse_in_p_tags_list(self):
        """Verify ZONE and PULSE are in the _P_TAGS detection list."""
        from log_parser import _P_TAGS
        self.assertIn("ZONE", _P_TAGS)
        self.assertIn("PULSE", _P_TAGS)


if __name__ == "__main__":
    unittest.main()
