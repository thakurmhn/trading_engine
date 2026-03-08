"""Tests for Phase 6.2 — Trend Continuation Replay Validation.

Tests the replay integration of TrendContinuationState:
  - Activation/deactivation lifecycle during simulated replay bars
  - ATR spacing enforcement for re-entries
  - Daily cap enforcement across multiple re-entries
  - Governance override: quality gate + oscillator bypass when tilt active
  - Log tag attribution: TREND_CONTINUATION_OVERRIDE, REPLAY_TREND_REENTRY
  - Log parser integration: SessionSummary fields
  - Dashboard rendering of continuation stats
"""

import io
import logging
import re
import pytest
import numpy as np

from signals import TrendContinuationState


# ─── Simulated Replay Lifecycle ──────────────────────────────────────────────

class TestReplayLifecycle:
    """Simulate a replay-like bar loop with TrendContinuationState."""

    def _simulate_replay(self, prices, daily_s4, daily_r4, adx_values, atr=100.0):
        """Run a simulated replay with given prices and return trade events.

        Returns list of dicts: [{"bar": i, "type": "ACTIVATED"|"ENTRY"|"EXIT"|"DEACTIVATED"}, ...]
        """
        tc = TrendContinuationState(activation_bars=5, adx_min=20.0, max_continuation_trades=4, re_entry_atr_spacing=0.5)
        events = []
        position_open = False
        position_bars = 0
        exit_after_bars = 3  # simulate exits after 3 bars

        for i, (price, adx) in enumerate(zip(prices, adx_values)):
            tc.update(price, daily_s4, daily_r4, adx, i)

            if not tc.is_active and tc._activated is False and i > 0 and events and events[-1].get("type") != "DEACTIVATED":
                # Check if just deactivated
                pass

            if tc.is_active and not any(e["bar"] == i and e["type"] == "ACTIVATED" for e in events):
                if not any(e["type"] == "ACTIVATED" for e in events) or \
                   (events and events[-1].get("type") == "DEACTIVATED"):
                    events.append({"bar": i, "type": "ACTIVATED", "side": tc.active_side})

            # Simulate position management
            if position_open:
                position_bars += 1
                if position_bars >= exit_after_bars:
                    tc.record_exit(price)
                    events.append({"bar": i, "type": "EXIT", "price": price})
                    position_open = False
                    position_bars = 0
                continue

            # Try re-entry
            if tc.is_active and tc.can_re_enter(price, atr):
                tc.record_entry()
                events.append({"bar": i, "type": "ENTRY", "side": tc.active_side, "num": tc.continuation_count})
                position_open = True
                position_bars = 0

        return events, tc

    def test_bearish_tilt_activation_and_entries(self):
        """Simulates a bearish day: price stays below S4, multiple PUT entries fire."""
        daily_s4 = 22000.0
        daily_r4 = 23000.0
        # 20 bars all below S4, ADX starts low then rises
        prices = [21900.0] * 20
        adx_values = [15.0] * 4 + [22.0] * 16  # ADX >= 20 from bar 4

        events, tc = self._simulate_replay(prices, daily_s4, daily_r4, adx_values)

        # Should activate at bar=4 (5 bars with ADX >= 20)
        activations = [e for e in events if e["type"] == "ACTIVATED"]
        assert len(activations) >= 1
        assert activations[0]["side"] == "PUT"

        # Should have entries
        entries = [e for e in events if e["type"] == "ENTRY"]
        assert len(entries) >= 1
        assert all(e["side"] == "PUT" for e in entries)

    def test_bullish_tilt_activation(self):
        """Price stays above R4 with sufficient ADX."""
        daily_s4 = 22000.0
        daily_r4 = 23000.0
        prices = [23100.0] * 15
        adx_values = [25.0] * 15

        events, tc = self._simulate_replay(prices, daily_s4, daily_r4, adx_values, atr=50.0)

        activations = [e for e in events if e["type"] == "ACTIVATED"]
        assert len(activations) >= 1
        assert activations[0]["side"] == "CALL"

    def test_no_activation_in_range(self):
        """Price stays between S4 and R4 — no activation."""
        daily_s4 = 22000.0
        daily_r4 = 23000.0
        prices = [22500.0] * 20
        adx_values = [30.0] * 20

        events, tc = self._simulate_replay(prices, daily_s4, daily_r4, adx_values)

        activations = [e for e in events if e["type"] == "ACTIVATED"]
        assert len(activations) == 0
        assert not tc.is_active

    def test_deactivation_on_range_return(self):
        """Price goes below S4 then returns — deactivates."""
        daily_s4 = 22000.0
        daily_r4 = 23000.0
        prices = [21900.0] * 6 + [22500.0] * 5  # 6 bars below, then back inside
        adx_values = [25.0] * 11

        tc = TrendContinuationState(activation_bars=5, adx_min=20.0)
        for i, (price, adx) in enumerate(zip(prices, adx_values)):
            tc.update(price, daily_s4, daily_r4, adx, i)

        assert not tc.is_active  # Should have deactivated

    def test_daily_cap_enforced_in_replay(self):
        """Only max_continuation_trades entries allowed per session."""
        daily_s4 = 22000.0
        daily_r4 = 23000.0
        # Long bearish day with many opportunities
        prices = [21900.0 - i * 10 for i in range(60)]
        adx_values = [30.0] * 60

        events, tc = self._simulate_replay(prices, daily_s4, daily_r4, adx_values, atr=20.0)

        entries = [e for e in events if e["type"] == "ENTRY"]
        assert len(entries) <= 4  # max_continuation_trades=4


# ─── ATR Spacing Tests ───────────────────────────────────────────────────────

class TestATRSpacingReplay:

    def test_spacing_prevents_immediate_reentry(self):
        """After exit, must wait for price to move 0.5x ATR before re-entry."""
        tc = TrendContinuationState(activation_bars=3, adx_min=10.0, re_entry_atr_spacing=0.5)
        # Activate
        for i in range(3):
            tc.update(21900.0, 22000.0, 23000.0, 15.0, i)
        assert tc.is_active

        # Record exit at 21900
        tc.record_exit(21900.0)

        # Price hasn't moved enough (need 21900 - 50 = 21850)
        assert not tc.can_re_enter(21870.0, 100.0)

        # Price moved enough
        assert tc.can_re_enter(21840.0, 100.0)

    def test_spacing_resets_on_each_exit(self):
        """Each exit resets the spacing reference price."""
        tc = TrendContinuationState(activation_bars=3, adx_min=10.0, re_entry_atr_spacing=0.5)
        for i in range(3):
            tc.update(21900.0, 22000.0, 23000.0, 15.0, i)

        # First exit at 21900
        tc.record_exit(21900.0)
        tc.record_entry()

        # Second exit at 21800
        tc.record_exit(21800.0)
        # Now spacing is from 21800, need 21800 - 50 = 21750
        assert not tc.can_re_enter(21780.0, 100.0)
        assert tc.can_re_enter(21750.0, 100.0)


# ─── Log Tag Tests ────────────────────────────────────────────────────────────

class TestLogTags:
    """Verify log tags emitted by TrendContinuationState."""

    def _capture_logs(self):
        """Setup log capture at INFO level, return (stream, handler, old_level)."""
        log_stream = io.StringIO()
        handler = logging.StreamHandler(log_stream)
        handler.setLevel(logging.INFO)
        old_level = logging.root.level
        logging.root.setLevel(logging.INFO)
        logging.root.addHandler(handler)
        return log_stream, handler, old_level

    def _cleanup_logs(self, handler, old_level):
        logging.root.removeHandler(handler)
        logging.root.setLevel(old_level)

    def test_activation_log(self):
        log_stream, handler, old_level = self._capture_logs()
        try:
            tc = TrendContinuationState(activation_bars=3, adx_min=10.0)
            for i in range(3):
                tc.update(21900.0, 22000.0, 23000.0, 15.0, i)
            log_output = log_stream.getvalue()
            assert "[TREND_CONTINUATION][ACTIVATED]" in log_output
            assert "side=PUT" in log_output
        finally:
            self._cleanup_logs(handler, old_level)

    def test_deactivation_log(self):
        log_stream, handler, old_level = self._capture_logs()
        try:
            tc = TrendContinuationState(activation_bars=3, adx_min=10.0)
            for i in range(3):
                tc.update(21900.0, 22000.0, 23000.0, 15.0, i)
            tc.update(22500.0, 22000.0, 23000.0, 15.0, 3)
            log_output = log_stream.getvalue()
            assert "[TREND_CONTINUATION][DEACTIVATED]" in log_output
        finally:
            self._cleanup_logs(handler, old_level)

    def test_re_entry_log(self):
        log_stream, handler, old_level = self._capture_logs()
        try:
            tc = TrendContinuationState(activation_bars=3, adx_min=10.0)
            for i in range(3):
                tc.update(21900.0, 22000.0, 23000.0, 15.0, i)
            tc.record_entry()
            log_output = log_stream.getvalue()
            assert "[TREND_CONTINUATION][RE_ENTRY]" in log_output
            assert "#1" in log_output
        finally:
            self._cleanup_logs(handler, old_level)

    def test_exit_recorded_log(self):
        log_stream, handler, old_level = self._capture_logs()
        try:
            tc = TrendContinuationState(activation_bars=3, adx_min=10.0)
            for i in range(3):
                tc.update(21900.0, 22000.0, 23000.0, 15.0, i)
            tc.record_exit(21850.0)
            log_output = log_stream.getvalue()
            assert "[TREND_CONTINUATION][EXIT_RECORDED]" in log_output
            assert "exit_price=21850.00" in log_output
        finally:
            self._cleanup_logs(handler, old_level)


# ─── Log Parser Integration ──────────────────────────────────────────────────

class TestLogParserIntegration:

    def test_regex_matches_activated(self):
        from log_parser import _RE_TREND_CONT_ACTIVATED
        line = "[TREND_CONTINUATION][ACTIVATED] bar=505 side=PUT consec_bars=52 ADX=25.0"
        m = _RE_TREND_CONT_ACTIVATED.search(line)
        assert m is not None
        assert m.group("side") == "PUT"

    def test_regex_matches_entry(self):
        from log_parser import _RE_TREND_CONT_ENTRY
        line = "[TREND_CONTINUATION][ENTRY] bar=513 2026-03-02 12:00:00 | PUT #2 close=24770.65"
        m = _RE_TREND_CONT_ENTRY.search(line)
        assert m is not None
        assert m.group("side") == "PUT"
        assert m.group("num") == "2"

    def test_regex_matches_deactivated(self):
        from log_parser import _RE_TREND_CONT_DEACTIVATED
        line = "[TREND_CONTINUATION][DEACTIVATED] bar=483 close=25662.15 returned inside S4-R4 range"
        m = _RE_TREND_CONT_DEACTIVATED.search(line)
        assert m is not None

    def test_session_summary_fields_default(self):
        from log_parser import SessionSummary
        s = SessionSummary(log_path="test.log", session_type="REPLAY", date_tag="2026-03-02")
        assert s.trend_continuation_activations == 0
        assert s.trend_continuation_entries == 0
        assert s.trend_continuation_deactivations == 0
        assert s.trend_continuation_side == ""

    def test_to_dict_includes_continuation(self):
        from log_parser import SessionSummary
        s = SessionSummary(
            log_path="test.log",
            session_type="REPLAY",
            date_tag="2026-03-02",
            trend_continuation_activations=1,
            trend_continuation_entries=5,
            trend_continuation_deactivations=0,
            trend_continuation_side="PUT",
        )
        d = s.to_dict()
        assert d["trend_continuation_activations"] == 1
        assert d["trend_continuation_entries"] == 5
        assert d["trend_continuation_side"] == "PUT"


# ─── P-Tag Parsing ───────────────────────────────────────────────────────────

class TestPTagParsing:
    """Verify _P_TAGS includes trend continuation tags."""

    def test_trend_continuation_in_ptags(self):
        from log_parser import _P_TAGS
        assert "TREND_CONTINUATION" in _P_TAGS

    def test_trend_continuation_override_in_ptags(self):
        from log_parser import _P_TAGS
        assert "TREND_CONTINUATION_OVERRIDE" in _P_TAGS

    def test_replay_trend_reentry_in_ptags(self):
        from log_parser import _P_TAGS
        assert "REPLAY_TREND_REENTRY" in _P_TAGS

    def test_tag_regex_matches(self):
        from log_parser import _RE_TAG_ANY
        line = "[TREND_CONTINUATION_OVERRIDE] bar=505 side=PUT"
        m = _RE_TAG_ANY.search(line)
        assert m is not None
        assert m.group(1) == "TREND_CONTINUATION_OVERRIDE"

        line2 = "[REPLAY_TREND_REENTRY] bar=505 side=PUT"
        m2 = _RE_TAG_ANY.search(line2)
        assert m2 is not None
        assert m2.group(1) == "REPLAY_TREND_REENTRY"


# ─── Dashboard Rendering ─────────────────────────────────────────────────────

class TestDashboardRendering:

    def test_dashboard_renders_continuation_section(self):
        """Verify dashboard text report includes continuation section when active."""
        from log_parser import SessionSummary

        session = SessionSummary(
            log_path="test.log",
            session_type="REPLAY",
            date_tag="2026-03-02",
            trend_continuation_activations=1,
            trend_continuation_entries=5,
            trend_continuation_deactivations=0,
            trend_continuation_side="PUT",
            trades=[
                {"side": "PUT", "pnl_pts": 6.8, "reason": "TREND_CONTINUATION", "source": "TREND_CONTINUATION"},
                {"side": "PUT", "pnl_pts": 14.1, "reason": "TREND_CONTINUATION", "source": "TREND_CONTINUATION"},
                {"side": "PUT", "pnl_pts": -5.0, "reason": "TREND_CONTINUATION", "source": "TREND_CONTINUATION"},
            ],
        )

        # Call dashboard text report writer
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            from dashboard import _write_text_report
            out_path = Path(tmpdir) / "report.txt"
            _write_text_report(session, out_path)
            report = out_path.read_text(encoding="utf-8")

        assert "TREND CONTINUATION (Phase 6.2)" in report
        assert "Activations" in report
        assert "Continuation entries" in report
        assert "PUT" in report


# ─── Edge Cases ──────────────────────────────────────────────────────────────

class TestEdgeCases:

    def test_activation_with_exact_adx_threshold(self):
        tc = TrendContinuationState(activation_bars=3, adx_min=25.0)
        for i in range(3):
            tc.update(21900.0, 22000.0, 23000.0, 25.0, i)
        assert tc.is_active

    def test_activation_just_below_adx_threshold(self):
        tc = TrendContinuationState(activation_bars=3, adx_min=25.0)
        for i in range(3):
            tc.update(21900.0, 22000.0, 23000.0, 24.9, i)
        assert not tc.is_active

    def test_continuation_after_full_cap_then_deactivate_reactivate(self):
        """After hitting cap, deactivation and reactivation should preserve cap."""
        tc = TrendContinuationState(activation_bars=3, adx_min=10.0, max_continuation_trades=2)
        for i in range(3):
            tc.update(21900.0, 22000.0, 23000.0, 15.0, i)
        tc.record_entry()
        tc.record_entry()
        assert not tc.can_re_enter(21000.0, 100.0)  # cap reached

        # Deactivate
        tc.update(22500.0, 22000.0, 23000.0, 15.0, 3)
        assert not tc.is_active

        # Reactivate
        for i in range(3):
            tc.update(21900.0, 22000.0, 23000.0, 15.0, 4 + i)
        assert tc.is_active
        # Cap should still be reached
        assert not tc.can_re_enter(21000.0, 100.0)

    def test_price_exactly_at_s4(self):
        """Price == S4 is inside range, not below."""
        tc = TrendContinuationState(activation_bars=3, adx_min=10.0)
        for i in range(5):
            tc.update(22000.0, 22000.0, 23000.0, 15.0, i)
        assert not tc.is_active

    def test_nan_price_skipped(self):
        tc = TrendContinuationState(activation_bars=3, adx_min=10.0)
        tc.update(float("nan"), 22000.0, 23000.0, 15.0, 0)
        tc.update(21900.0, float("nan"), 23000.0, 15.0, 1)
        tc.update(21900.0, 22000.0, float("nan"), 15.0, 2)
        assert not tc.is_active  # all skipped
