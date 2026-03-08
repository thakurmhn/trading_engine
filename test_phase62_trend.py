"""Tests for Phase 6.2 — Trend Continuation Module.

Tests TrendContinuationState from signals.py:
  - Activation after N consecutive bars below S4 / above R4 with ADX >= threshold
  - Deactivation when price returns inside S4-R4
  - ATR-spaced re-entry gating
  - Daily cap enforcement
  - Record entry / exit lifecycle
"""

import pytest
import numpy as np


# ── Import TrendContinuationState ─────────────────────────────────────────────
from signals import TrendContinuationState


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tc():
    """Default TrendContinuationState with activation_bars=15, adx_min=25."""
    return TrendContinuationState(
        activation_bars=15,
        adx_min=25.0,
        max_continuation_trades=6,
        re_entry_atr_spacing=0.5,
    )


@pytest.fixture
def tc_fast():
    """Fast activation: only 3 bars needed, low ADX threshold."""
    return TrendContinuationState(
        activation_bars=3,
        adx_min=10.0,
        max_continuation_trades=4,
        re_entry_atr_spacing=0.5,
    )


# ── Basic State Tests ────────────────────────────────────────────────────────

class TestTrendContinuationBasic:

    def test_initial_state(self, tc):
        assert not tc.is_active
        assert tc.active_side is None
        assert tc.continuation_count == 0

    def test_no_activation_without_enough_bars(self, tc):
        daily_s4, daily_r4 = 22000.0, 23000.0
        # 14 bars below S4 — not enough (need 15)
        for i in range(14):
            tc.update(21900.0, daily_s4, daily_r4, adx=30.0, bar_idx=i)
        assert not tc.is_active

    def test_activation_bearish_after_threshold_bars(self, tc):
        daily_s4, daily_r4 = 22000.0, 23000.0
        for i in range(15):
            tc.update(21900.0, daily_s4, daily_r4, adx=30.0, bar_idx=i)
        assert tc.is_active
        assert tc.active_side == "PUT"

    def test_activation_bullish_after_threshold_bars(self, tc):
        daily_s4, daily_r4 = 22000.0, 23000.0
        for i in range(15):
            tc.update(23100.0, daily_s4, daily_r4, adx=28.0, bar_idx=i)
        assert tc.is_active
        assert tc.active_side == "CALL"

    def test_no_activation_with_low_adx(self, tc):
        daily_s4, daily_r4 = 22000.0, 23000.0
        for i in range(20):
            tc.update(21900.0, daily_s4, daily_r4, adx=20.0, bar_idx=i)
        assert not tc.is_active

    def test_activation_requires_adx_at_threshold(self, tc):
        daily_s4, daily_r4 = 22000.0, 23000.0
        # 14 bars with low ADX
        for i in range(14):
            tc.update(21900.0, daily_s4, daily_r4, adx=20.0, bar_idx=i)
        # 15th bar with high ADX — should activate
        tc.update(21900.0, daily_s4, daily_r4, adx=25.0, bar_idx=14)
        assert tc.is_active

    def test_deactivation_on_return_to_range(self, tc_fast):
        daily_s4, daily_r4 = 22000.0, 23000.0
        for i in range(3):
            tc_fast.update(21900.0, daily_s4, daily_r4, adx=15.0, bar_idx=i)
        assert tc_fast.is_active
        # Price returns inside S4-R4
        tc_fast.update(22500.0, daily_s4, daily_r4, adx=15.0, bar_idx=3)
        assert not tc_fast.is_active
        assert tc_fast.active_side is None

    def test_reset(self, tc_fast):
        daily_s4, daily_r4 = 22000.0, 23000.0
        for i in range(3):
            tc_fast.update(21900.0, daily_s4, daily_r4, adx=15.0, bar_idx=i)
        assert tc_fast.is_active
        tc_fast.reset()
        assert not tc_fast.is_active
        assert tc_fast.continuation_count == 0

    def test_nan_values_handled(self, tc):
        tc.update(float("nan"), 22000.0, 23000.0, adx=30.0, bar_idx=0)
        assert not tc.is_active
        tc.update(22500.0, float("nan"), 23000.0, adx=30.0, bar_idx=1)
        assert not tc.is_active

    def test_consecutive_reset_on_mid_range(self, tc_fast):
        daily_s4, daily_r4 = 22000.0, 23000.0
        # 2 bars below S4
        tc_fast.update(21900.0, daily_s4, daily_r4, adx=15.0, bar_idx=0)
        tc_fast.update(21900.0, daily_s4, daily_r4, adx=15.0, bar_idx=1)
        # 1 bar inside range — resets count
        tc_fast.update(22500.0, daily_s4, daily_r4, adx=15.0, bar_idx=2)
        # 2 more below — total only 2, not 4
        tc_fast.update(21900.0, daily_s4, daily_r4, adx=15.0, bar_idx=3)
        tc_fast.update(21900.0, daily_s4, daily_r4, adx=15.0, bar_idx=4)
        assert not tc_fast.is_active  # need 3 consecutive


# ── Re-entry Logic Tests ─────────────────────────────────────────────────────

class TestTrendContinuationReEntry:

    def _activate_bearish(self, tc):
        """Helper: activate trend continuation in PUT direction."""
        for i in range(tc.activation_bars):
            tc.update(21900.0, 22000.0, 23000.0, adx=30.0, bar_idx=i)
        assert tc.is_active

    def _activate_bullish(self, tc):
        for i in range(tc.activation_bars):
            tc.update(23100.0, 22000.0, 23000.0, adx=30.0, bar_idx=i)
        assert tc.is_active

    def test_can_re_enter_when_active_no_prior_exit(self, tc_fast):
        for i in range(3):
            tc_fast.update(21900.0, 22000.0, 23000.0, adx=15.0, bar_idx=i)
        assert tc_fast.can_re_enter(21900.0, atr=100.0)

    def test_cannot_re_enter_when_inactive(self, tc):
        assert not tc.can_re_enter(21900.0, atr=100.0)

    def test_atr_spacing_bearish(self, tc_fast):
        for i in range(3):
            tc_fast.update(21900.0, 22000.0, 23000.0, adx=15.0, bar_idx=i)
        # Record exit at 21900
        tc_fast.record_exit(21900.0)
        # Spacing = 0.5 * 100 = 50. Need close <= 21900 - 50 = 21850
        assert not tc_fast.can_re_enter(21870.0, atr=100.0)  # 21870 > 21850
        assert tc_fast.can_re_enter(21850.0, atr=100.0)      # exactly at threshold
        assert tc_fast.can_re_enter(21800.0, atr=100.0)      # below threshold

    def test_atr_spacing_bullish(self, tc_fast):
        for i in range(3):
            tc_fast.update(23100.0, 22000.0, 23000.0, adx=15.0, bar_idx=i)
        tc_fast.record_exit(23100.0)
        # Spacing = 0.5 * 100 = 50. Need close >= 23100 + 50 = 23150
        assert not tc_fast.can_re_enter(23130.0, atr=100.0)  # too close
        assert tc_fast.can_re_enter(23150.0, atr=100.0)

    def test_daily_cap_enforced(self, tc_fast):
        for i in range(3):
            tc_fast.update(21900.0, 22000.0, 23000.0, adx=15.0, bar_idx=i)
        # Take max_continuation_trades=4 entries
        for _ in range(4):
            assert tc_fast.can_re_enter(21000.0, atr=100.0)
            tc_fast.record_entry()
        # 5th should be blocked
        assert not tc_fast.can_re_enter(21000.0, atr=100.0)
        assert tc_fast.continuation_count == 4

    def test_record_entry_increments_count(self, tc_fast):
        for i in range(3):
            tc_fast.update(21900.0, 22000.0, 23000.0, adx=15.0, bar_idx=i)
        tc_fast.record_entry()
        assert tc_fast.continuation_count == 1
        tc_fast.record_entry()
        assert tc_fast.continuation_count == 2


# ── Edge Case Tests ───────────────────────────────────────────────────────────

class TestTrendContinuationEdgeCases:

    def test_switch_from_bearish_to_bullish(self, tc_fast):
        """Price crosses from below S4 to above R4."""
        # Activate bearish
        for i in range(3):
            tc_fast.update(21900.0, 22000.0, 23000.0, adx=15.0, bar_idx=i)
        assert tc_fast.is_active
        assert tc_fast.active_side == "PUT"
        # Price returns to mid-range — deactivates
        tc_fast.update(22500.0, 22000.0, 23000.0, adx=15.0, bar_idx=3)
        assert not tc_fast.is_active
        # Now go above R4 — activate bullish
        for i in range(3):
            tc_fast.update(23100.0, 22000.0, 23000.0, adx=15.0, bar_idx=4 + i)
        assert tc_fast.is_active
        assert tc_fast.active_side == "CALL"

    def test_zero_atr_allows_entry(self, tc_fast):
        for i in range(3):
            tc_fast.update(21900.0, 22000.0, 23000.0, adx=15.0, bar_idx=i)
        tc_fast.record_exit(21900.0)
        # With zero ATR, spacing check should pass (no spacing calc possible)
        assert tc_fast.can_re_enter(21900.0, atr=0.0)

    def test_continuation_count_preserved_across_reactivation(self, tc_fast):
        """Deactivation + reactivation resets activation but NOT via reset()."""
        for i in range(3):
            tc_fast.update(21900.0, 22000.0, 23000.0, adx=15.0, bar_idx=i)
        tc_fast.record_entry()
        # Deactivate
        tc_fast.update(22500.0, 22000.0, 23000.0, adx=15.0, bar_idx=3)
        assert not tc_fast.is_active
        # Reactivate
        for i in range(3):
            tc_fast.update(21900.0, 22000.0, 23000.0, adx=15.0, bar_idx=4 + i)
        assert tc_fast.is_active
        # Count should persist (feature: deactivation doesn't reset trades)
        assert tc_fast.continuation_count == 1

    def test_exactly_at_s4_boundary(self, tc_fast):
        """Price exactly equal to S4 — should NOT count as below S4."""
        for i in range(5):
            tc_fast.update(22000.0, 22000.0, 23000.0, adx=15.0, bar_idx=i)
        # Not below S4 and not above R4 — mid-range
        assert not tc_fast.is_active

    def test_exactly_at_r4_boundary(self, tc_fast):
        """Price exactly equal to R4 — should NOT count as above R4."""
        for i in range(5):
            tc_fast.update(23000.0, 22000.0, 23000.0, adx=15.0, bar_idx=i)
        assert not tc_fast.is_active


# ── Log Parser Integration Tests ─────────────────────────────────────────────

class TestLogParserTrendContinuation:

    def test_session_summary_has_fields(self):
        from log_parser import SessionSummary
        s = SessionSummary(
            log_path="test.log",
            session_type="REPLAY",
            date_tag="2026-03-02",
        )
        assert s.trend_continuation_activations == 0
        assert s.trend_continuation_entries == 0
        assert s.trend_continuation_deactivations == 0
        assert s.trend_continuation_side == ""

    def test_to_dict_includes_fields(self):
        from log_parser import SessionSummary
        s = SessionSummary(
            log_path="test.log",
            session_type="REPLAY",
            date_tag="2026-03-02",
            trend_continuation_activations=2,
            trend_continuation_entries=5,
            trend_continuation_deactivations=1,
            trend_continuation_side="PUT",
        )
        d = s.to_dict()
        assert d["trend_continuation_activations"] == 2
        assert d["trend_continuation_entries"] == 5
        assert d["trend_continuation_deactivations"] == 1
        assert d["trend_continuation_side"] == "PUT"
