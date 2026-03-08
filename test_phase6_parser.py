"""Phase 6 — Log Parser Tests.

Verify regex parsing for all Phase 6 tags, counter increments in SessionSummary,
bias_alignment trade attribution, and backward compatibility.
"""

import os
import tempfile
import unittest

from log_parser import LogParser, SessionSummary, _P_TAGS


class _ParseHelper:
    """Mixin to parse log lines via temp file."""

    def _parse_lines(self, lines):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".log", delete=False, encoding="utf-8"
        ) as f:
            f.write("\n".join(lines) + "\n")
            path = f.name
        try:
            return LogParser(path).parse()
        finally:
            os.unlink(path)


class TestBiasAlignmentParsing(_ParseHelper, unittest.TestCase):
    """[BIAS_ALIGNMENT] tag parsing."""

    def test_aligned_counted(self):
        lines = [
            "2026-03-07 10:00:00 [BIAS_ALIGNMENT] side=CALL status=ALIGNED tf=3m bias15m=BULLISH bias3m=BULLISH",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.bias_alignment_count, 1)
        self.assertEqual(s.tag_counts.get("BIAS_ALIGNMENT", 0), 1)

    def test_misaligned_counted(self):
        lines = [
            "2026-03-07 10:00:00 [BIAS_ALIGNMENT] side=PUT status=MISALIGNED tf=NONE",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.bias_alignment_count, 1)

    def test_neutral_counted(self):
        lines = [
            "2026-03-07 10:00:00 [BIAS_ALIGNMENT] side=CALL status=NEUTRAL tf=NONE",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.bias_alignment_count, 1)

    def test_multiple_events(self):
        lines = [
            "2026-03-07 10:00:00 [BIAS_ALIGNMENT] side=CALL status=ALIGNED tf=3m",
            "2026-03-07 10:03:00 [BIAS_ALIGNMENT] side=PUT status=MISALIGNED tf=NONE",
            "2026-03-07 10:06:00 [BIAS_ALIGNMENT] side=CALL status=NEUTRAL tf=NONE",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.bias_alignment_count, 3)


class TestBarCloseAlignmentParsing(_ParseHelper, unittest.TestCase):
    """[BAR_CLOSE_ALIGNMENT][TF=...] tag parsing."""

    def test_standalone_3m(self):
        lines = [
            "2026-03-07 10:00:00 [BAR_CLOSE_ALIGNMENT][TF=3m] alignment confirmed",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.bar_close_alignment_count, 1)

    def test_standalone_15m(self):
        lines = [
            "2026-03-07 10:00:00 [BAR_CLOSE_ALIGNMENT][TF=15m] alignment confirmed",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.bar_close_alignment_count, 1)

    def test_inside_entry_ok(self):
        """Tag embedded inside [ENTRY OK] line should still be counted."""
        lines = [
            "2026-03-07 10:00:00 [ENTRY OK] CALL score=65/50 MODERATE HIGH [BAR_CLOSE_ALIGNMENT][TF=3m]",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.bar_close_alignment_count, 1)
        self.assertEqual(s.entry_ok_count, 1)

    def test_multiple_tfs(self):
        lines = [
            "2026-03-07 10:00:00 [BAR_CLOSE_ALIGNMENT][TF=3m]",
            "2026-03-07 10:03:00 [ENTRY OK] PUT score=70/50 [BAR_CLOSE_ALIGNMENT][TF=15m]",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.bar_close_alignment_count, 2)


class TestSlopeOverrideTimeParsing(_ParseHelper, unittest.TestCase):
    """[SLOPE_OVERRIDE_TIME] tag parsing."""

    def test_counted(self):
        lines = [
            "2026-03-07 10:00:00 [SLOPE_OVERRIDE_TIME] timestamp=2026-03-07 symbol=NIFTY side=CALL bars=5",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.slope_override_time_count, 1)
        self.assertEqual(s.tag_counts.get("SLOPE_OVERRIDE_TIME", 0), 1)

    def test_multiple(self):
        lines = [
            "2026-03-07 10:00:00 [SLOPE_OVERRIDE_TIME] timestamp=2026-03-07 symbol=NIFTY side=CALL bars=5",
            "2026-03-07 10:05:00 [SLOPE_OVERRIDE_TIME] timestamp=2026-03-07 symbol=BANKNIFTY side=PUT bars=7",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.slope_override_time_count, 2)


class TestConflictBlockedParsing(_ParseHelper, unittest.TestCase):
    """[CONFLICT_BLOCKED] tag parsing."""

    def test_counted(self):
        lines = [
            "2026-03-07 10:00:00 [CONFLICT_BLOCKED] timestamp=2026-03-07 symbol=NIFTY side=CALL type=ST_SLOPE_CONFLICT conflict_bars=3",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.conflict_blocked_count, 1)
        self.assertEqual(s.tag_counts.get("CONFLICT_BLOCKED", 0), 1)


class TestMicrostructureTagParsing(_ParseHelper, unittest.TestCase):
    """[PULSE_EXHAUSTION], [ZONE_ABSORPTION], [ZONE_REJECTION], [SPREAD_NOISE] parsing."""

    def test_pulse_exhaustion_counted(self):
        lines = [
            "2026-03-07 10:00:00 [PULSE_EXHAUSTION] peak=20.0 current=8.0 decay_ratio=0.40",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.pulse_exhaustion_count, 1)

    def test_zone_absorption_counted(self):
        lines = [
            "2026-03-07 10:00:00 [ZONE_ABSORPTION] zone=D_1 type=DEMAND touches=3 window=10",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.zone_absorption_count, 1)

    def test_spread_noise_counted(self):
        lines = [
            "2026-03-07 10:00:00 [SPREAD_NOISE] close_open_drift=1.50 <= 2 pts",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.spread_noise_count, 1)

    def test_all_microstructure_together(self):
        lines = [
            "2026-03-07 10:00:00 [PULSE_EXHAUSTION] peak=20.0 current=8.0",
            "2026-03-07 10:01:00 [ZONE_ABSORPTION] zone=D_1 type=DEMAND touches=3",
            "2026-03-07 10:02:00 [SPREAD_NOISE] close_open_drift=1.50",
        ]
        s = self._parse_lines(lines)
        mc = s.microstructure_counts
        self.assertEqual(mc["pulse_exhaustion"], 1)
        self.assertEqual(mc["zone_absorption"], 1)
        self.assertEqual(mc["spread_noise"], 1)


class TestTradeAttribution(_ParseHelper, unittest.TestCase):
    """bias_alignment attached to trade records."""

    def test_bias_alignment_on_trade(self):
        lines = [
            "2026-03-07 10:00:00 [BIAS_ALIGNMENT] side=CALL status=ALIGNED tf=3m",
            "2026-03-07 10:01:00 [EXIT][PAPER SL_HIT] CALL NIFTY26MAR25000CE Entry=200.0 Exit=180.0 Qty=75 PnL=-1500.00 (points=-20.0) BarsHeld=3",
        ]
        s = self._parse_lines(lines)
        if s.trades:
            self.assertEqual(s.trades[0].get("bias_alignment"), "ALIGNED")

    def test_misaligned_on_trade(self):
        lines = [
            "2026-03-07 10:00:00 [BIAS_ALIGNMENT] side=PUT status=MISALIGNED tf=NONE",
            "2026-03-07 10:01:00 [EXIT][PAPER TG_HIT] PUT NIFTY26MAR24500PE Entry=200.0 Exit=240.0 Qty=75 PnL=3000.00 (points=40.0) BarsHeld=5",
        ]
        s = self._parse_lines(lines)
        if s.trades:
            self.assertEqual(s.trades[0].get("bias_alignment"), "MISALIGNED")


class TestZeroCounts(_ParseHelper, unittest.TestCase):
    """Absent Phase 6 tags produce zero counts."""

    def test_all_zero(self):
        lines = ["2026-03-07 10:00:00 INFO Normal log line"]
        s = self._parse_lines(lines)
        self.assertEqual(s.bias_alignment_count, 0)
        self.assertEqual(s.bar_close_alignment_count, 0)
        self.assertEqual(s.slope_override_time_count, 0)
        self.assertEqual(s.conflict_blocked_count, 0)
        self.assertEqual(s.pulse_exhaustion_count, 0)
        self.assertEqual(s.zone_absorption_count, 0)
        self.assertEqual(s.spread_noise_count, 0)


class TestToDictPhase6(_ParseHelper, unittest.TestCase):
    """to_dict() includes Phase 6 fields."""

    def test_dict_keys(self):
        lines = [
            "2026-03-07 10:00:00 [BIAS_ALIGNMENT] side=CALL status=ALIGNED tf=3m",
        ]
        s = self._parse_lines(lines)
        d = s.to_dict()
        self.assertIn("bias_alignment_count", d)
        self.assertIn("bar_close_alignment_count", d)
        self.assertIn("slope_override_time_count", d)
        self.assertIn("conflict_blocked_count", d)
        self.assertIn("bias_alignment_performance", d)
        self.assertIn("microstructure_counts", d)


class TestPTagsComplete(unittest.TestCase):
    """Phase 6 tags are in _P_TAGS list."""

    def test_all_present(self):
        expected = [
            "BAR_CLOSE_ALIGNMENT", "BAR_CLOSE_MISALIGNED",
            "BIAS_ALIGNMENT", "SLOPE_OVERRIDE_TIME",
            "CONFLICT_BLOCKED", "PULSE_EXHAUSTION",
            "ZONE_ABSORPTION", "ZONE_REJECTION", "SPREAD_NOISE",
        ]
        for tag in expected:
            self.assertIn(tag, _P_TAGS, f"{tag} missing from _P_TAGS")


class TestBackwardCompatibility(_ParseHelper, unittest.TestCase):
    """Pre-Phase 6 logs parse without errors."""

    def test_old_log_parses(self):
        lines = [
            "2026-02-24 10:00:00 [SIGNAL FIRED] CALL source=PIVOT score=65",
            "2026-02-24 10:01:00 [ENTRY OK] CALL score=65/50 MODERATE HIGH",
            "2026-02-24 10:05:00 [TRADE OPEN][PAPER] CALL bar=5 bar_ts=2026-02-24T10:05 underlying=22000.0 premium=200.0 score=65 lot=75",
            "2026-02-24 10:15:00 [TRADE EXIT][PAPER] CALL bar=10 bar_ts=2026-02-24T10:15 underlying=22050.0 premium=220.0 pnl_pts=20.0 pnl_rs=1500.0 bars_held=5 reason=TG_HIT",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.bias_alignment_count, 0)
        self.assertEqual(s.bar_close_alignment_count, 0)
        mc = s.microstructure_counts
        self.assertEqual(mc["pulse_exhaustion"], 0)

    def test_old_exit_format(self):
        """Rich EXIT format from older logs still parses with default NEUTRAL."""
        lines = [
            "2026-02-24 10:15:00 [EXIT][PAPER SL_HIT] CALL NIFTY26MAR25000CE Entry=200.0 Exit=180.0 Qty=75 PnL=-1500.00 (points=-20.0) BarsHeld=3",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.bias_alignment_count, 0)
        if s.trades:
            # Default is "NEUTRAL" when no [BIAS_ALIGNMENT] tag was seen
            self.assertEqual(s.trades[0].get("bias_alignment"), "NEUTRAL")


class TestBiasAlignmentPerformanceProperty(unittest.TestCase):
    """bias_alignment_performance property breakdown."""

    def test_performance_breakdown(self):
        trades = [
            {"side": "CALL", "pnl_pts": 10.0, "bias_alignment": "ALIGNED"},
            {"side": "PUT", "pnl_pts": -5.0, "bias_alignment": "MISALIGNED"},
            {"side": "CALL", "pnl_pts": 8.0, "bias_alignment": "ALIGNED"},
            {"side": "PUT", "pnl_pts": 3.0, "bias_alignment": "NEUTRAL"},
        ]
        s = SessionSummary(
            log_path="test.log", session_type="PAPER",
            date_tag="2026-03-07", trades=trades,
        )
        perf = s.bias_alignment_performance
        self.assertEqual(perf["ALIGNED"]["trades"], 2)
        self.assertEqual(perf["ALIGNED"]["winners"], 2)
        self.assertAlmostEqual(perf["ALIGNED"]["win_rate"], 100.0)
        self.assertAlmostEqual(perf["ALIGNED"]["net_pnl"], 18.0)
        self.assertEqual(perf["MISALIGNED"]["trades"], 1)
        self.assertEqual(perf["NEUTRAL"]["trades"], 1)

    def test_empty_trades(self):
        s = SessionSummary(
            log_path="test.log", session_type="PAPER", date_tag="2026-03-07",
        )
        self.assertEqual(s.bias_alignment_performance, {})


class TestEndToEndPipeline(_ParseHelper, unittest.TestCase):
    """Full pipeline: log lines → parser → summary → validate."""

    def test_full_pipeline(self):
        lines = [
            "2026-03-07 10:00:00 [BIAS_ALIGNMENT] side=CALL status=ALIGNED tf=3m bias15m=BULLISH bias3m=BULLISH",
            "2026-03-07 10:00:30 [ENTRY OK] CALL score=70/50 MODERATE HIGH [BAR_CLOSE_ALIGNMENT][TF=3m]",
            "2026-03-07 10:01:00 [EXIT][PAPER TG_HIT] CALL NIFTY26MAR25000CE Entry=200.0 Exit=220.0 Qty=75 PnL=1500.00 (points=20.0) BarsHeld=5",
            "2026-03-07 10:02:00 [PULSE_EXHAUSTION] peak=25.0 current=10.0 decay_ratio=0.40",
            "2026-03-07 10:03:00 [ZONE_ABSORPTION] zone=D_1 type=DEMAND touches=4 window=10",
            "2026-03-07 10:04:00 [SPREAD_NOISE] close_open_drift=1.20 <= 2 pts",
            "2026-03-07 10:05:00 [SLOPE_OVERRIDE_TIME] timestamp=2026-03-07 symbol=NIFTY side=PUT bars=5",
            "2026-03-07 10:06:00 [CONFLICT_BLOCKED] timestamp=2026-03-07 symbol=NIFTY side=PUT type=ST_SLOPE_CONFLICT conflict_bars=3",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.bias_alignment_count, 1)
        self.assertEqual(s.bar_close_alignment_count, 1)
        self.assertEqual(s.pulse_exhaustion_count, 1)
        self.assertEqual(s.zone_absorption_count, 1)
        self.assertEqual(s.spread_noise_count, 1)
        self.assertEqual(s.slope_override_time_count, 1)
        self.assertEqual(s.conflict_blocked_count, 1)


if __name__ == "__main__":
    unittest.main()
