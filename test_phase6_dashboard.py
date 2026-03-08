"""Phase 6 — Dashboard Tests.

Verify Bias Alignment Performance and Microstructure Attribution sections
render correctly, skip when empty, and aggregate in compare_sessions().
"""

import tempfile
import unittest
from pathlib import Path

from log_parser import SessionSummary
from dashboard import _write_text_report, compare_sessions


def _make_session(**kwargs):
    defaults = {
        "log_path": "test.log",
        "session_type": "PAPER",
        "date_tag": "2026-03-07",
    }
    defaults.update(kwargs)
    return SessionSummary(**defaults)


class TestBiasAlignmentSection(unittest.TestCase):
    """BIAS ALIGNMENT PERFORMANCE section rendering."""

    def _render(self, session):
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            path = Path(f.name)
        try:
            _write_text_report(session, path)
            return path.read_text()
        finally:
            path.unlink(missing_ok=True)

    def test_section_appears_with_aligned_trades(self):
        trades = [
            {"side": "CALL", "pnl_pts": 10.0, "bias_alignment": "ALIGNED"},
            {"side": "PUT", "pnl_pts": -5.0, "bias_alignment": "MISALIGNED"},
            {"side": "CALL", "pnl_pts": 8.0, "bias_alignment": "ALIGNED"},
        ]
        session = _make_session(trades=trades, bias_alignment_count=3)
        text = self._render(session)
        self.assertIn("BIAS ALIGNMENT PERFORMANCE", text)
        self.assertIn("ALIGNED", text)
        self.assertIn("MISALIGNED", text)

    def test_section_absent_when_no_alignment_data(self):
        session = _make_session()
        text = self._render(session)
        self.assertNotIn("BIAS ALIGNMENT PERFORMANCE", text)

    def test_shows_win_rate_and_pnl(self):
        trades = [
            {"side": "CALL", "pnl_pts": 15.0, "bias_alignment": "ALIGNED"},
            {"side": "CALL", "pnl_pts": -3.0, "bias_alignment": "ALIGNED"},
        ]
        session = _make_session(trades=trades, bias_alignment_count=2)
        text = self._render(session)
        self.assertIn("50.0%", text)
        self.assertIn("+12.00", text)

    def test_bar_close_count_shown(self):
        trades = [
            {"side": "CALL", "pnl_pts": 10.0, "bias_alignment": "ALIGNED"},
        ]
        session = _make_session(
            trades=trades,
            bias_alignment_count=2,
            bar_close_alignment_count=3,
        )
        text = self._render(session)
        self.assertIn("Bar-close alignment logs", text)
        self.assertIn("3", text)


class TestMicrostructureSection(unittest.TestCase):
    """MICROSTRUCTURE ATTRIBUTION section rendering."""

    def _render(self, session):
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            path = Path(f.name)
        try:
            _write_text_report(session, path)
            return path.read_text()
        finally:
            path.unlink(missing_ok=True)

    def test_section_appears_with_pulse(self):
        session = _make_session(pulse_exhaustion_count=3)
        text = self._render(session)
        self.assertIn("MICROSTRUCTURE ATTRIBUTION", text)
        self.assertIn("Pulse exhaustion events", text)

    def test_section_appears_with_zone_absorption(self):
        session = _make_session(zone_absorption_count=2)
        text = self._render(session)
        self.assertIn("MICROSTRUCTURE ATTRIBUTION", text)
        self.assertIn("Zone absorption events", text)

    def test_section_appears_with_spread_noise(self):
        session = _make_session(spread_noise_count=5)
        text = self._render(session)
        self.assertIn("MICROSTRUCTURE ATTRIBUTION", text)
        self.assertIn("Spread noise entries", text)

    def test_section_appears_with_slope_time(self):
        session = _make_session(slope_override_time_count=1)
        text = self._render(session)
        self.assertIn("MICROSTRUCTURE ATTRIBUTION", text)
        self.assertIn("Slope time overrides", text)

    def test_section_appears_with_conflict_blocked(self):
        session = _make_session(conflict_blocked_count=4)
        text = self._render(session)
        self.assertIn("MICROSTRUCTURE ATTRIBUTION", text)
        self.assertIn("Conflict blocked", text)

    def test_section_absent_when_all_zero(self):
        session = _make_session()
        text = self._render(session)
        self.assertNotIn("MICROSTRUCTURE ATTRIBUTION", text)

    def test_all_fields_shown(self):
        session = _make_session(
            pulse_exhaustion_count=2,
            zone_absorption_count=3,
            spread_noise_count=1,
            slope_override_time_count=4,
            conflict_blocked_count=5,
        )
        text = self._render(session)
        self.assertIn("Pulse exhaustion events", text)
        self.assertIn("Zone absorption events", text)
        self.assertIn("Spread noise entries", text)
        self.assertIn("Slope time overrides", text)
        self.assertIn("Conflict blocked", text)


class TestCompareSessionsAggregation(unittest.TestCase):
    """compare_sessions() aggregates Phase 6 fields."""

    def test_aggregate_phase6_fields(self):
        """Create two log files with Phase 6 tags and verify aggregation."""
        log1_lines = [
            "2026-03-05 10:00:00 [BIAS_ALIGNMENT] side=CALL status=ALIGNED tf=3m",
            "2026-03-05 10:01:00 [BIAS_ALIGNMENT] side=PUT status=MISALIGNED tf=NONE",
            "2026-03-05 10:02:00 [BAR_CLOSE_ALIGNMENT][TF=3m]",
            "2026-03-05 10:03:00 [SLOPE_OVERRIDE_TIME] timestamp=2026-03-05 symbol=NIFTY side=CALL bars=5",
            "2026-03-05 10:04:00 [CONFLICT_BLOCKED] timestamp=2026-03-05 symbol=NIFTY side=PUT type=ST_SLOPE_CONFLICT conflict_bars=2",
            "2026-03-05 10:05:00 [PULSE_EXHAUSTION] peak=20.0 current=8.0",
            "2026-03-05 10:06:00 [ZONE_ABSORPTION] zone=D_1 type=DEMAND touches=3",
            "2026-03-05 10:07:00 [SPREAD_NOISE] close_open_drift=1.50",
        ]
        log2_lines = [
            "2026-03-06 10:00:00 [BIAS_ALIGNMENT] side=CALL status=NEUTRAL tf=NONE",
            "2026-03-06 10:01:00 [CONFLICT_BLOCKED] timestamp=2026-03-06 symbol=NIFTY side=CALL type=ST_SLOPE_CONFLICT conflict_bars=1",
            "2026-03-06 10:02:00 [PULSE_EXHAUSTION] peak=15.0 current=6.0",
            "2026-03-06 10:03:00 [SPREAD_NOISE] close_open_drift=0.80",
        ]

        import os
        paths = []
        for lines in [log1_lines, log2_lines]:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".log", delete=False, encoding="utf-8"
            ) as f:
                f.write("\n".join(lines) + "\n")
                paths.append(f.name)

        with tempfile.TemporaryDirectory() as out_dir:
            try:
                result = compare_sessions(paths[:1], paths[1:], output_dir=out_dir)
                baseline = result["baseline_summary"]
                fixed = result["fixed_summary"]
                # Baseline should have Phase 6 counts from log1
                self.assertEqual(baseline["bias_alignment_count"], 2)
                self.assertEqual(baseline["bar_close_alignment_count"], 1)
                self.assertEqual(baseline["slope_override_time_count"], 1)
                self.assertEqual(baseline["pulse_exhaustion_count"], 1)
                # Fixed should have Phase 6 counts from log2
                self.assertEqual(fixed["bias_alignment_count"], 1)
                self.assertEqual(fixed["conflict_blocked_count"], 1)
                self.assertEqual(fixed["pulse_exhaustion_count"], 1)
                self.assertEqual(fixed["spread_noise_count"], 1)
            finally:
                for p in paths:
                    os.unlink(p)


class TestBackwardCompatDashboard(unittest.TestCase):
    """Dashboard renders old sessions without Phase 6 fields."""

    def test_old_session_renders(self):
        session = _make_session(
            trades=[{"side": "CALL", "pnl_pts": 10.0}],
        )
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            path = Path(f.name)
        try:
            _write_text_report(session, path)
            text = path.read_text()
            self.assertIn("TRADE SUMMARY", text)
            self.assertNotIn("BIAS ALIGNMENT PERFORMANCE", text)
            self.assertNotIn("MICROSTRUCTURE ATTRIBUTION", text)
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
