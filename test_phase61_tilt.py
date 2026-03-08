"""Phase 6.1 — Tilt-Based Governance Tests.

Covers:
  - Tilt detector (signals.compute_tilt_state)
  - Governance relaxation in execution.py
  - Log parser parsing for [TILT_STATE], [GOVERNANCE_EASY], [GOVERNANCE_STRICT]
  - Dashboard tilt performance section
  - Backward compatibility
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# Ensure real modules are loaded for signals
from unittest.mock import MagicMock as _MM
for _mod in ("pulse_module", "zone_detector"):
    _existing = sys.modules.get(_mod)
    if _existing is not None and isinstance(_existing, _MM):
        del sys.modules[_mod]

# Stub heavy deps if not already
_ind = sys.modules.get("indicators")
if _ind is None or isinstance(_ind, _MM):
    _ind = MagicMock()
    _ind.calculate_atr = MagicMock(return_value=50.0)
    _ind.resolve_atr = MagicMock(return_value=(50.0, "ATR14"))
    _ind.daily_atr = MagicMock(return_value=100.0)
    _ind.momentum_ok = MagicMock(return_value=(True, 1.0))
    _ind.williams_r = MagicMock(return_value=-50.0)
    _ind.calculate_cci = MagicMock(return_value=0.0)
    _ind.compute_rsi = MagicMock(return_value=50.0)
    _ind.classify_cpr_width = MagicMock(return_value="NORMAL")
    sys.modules["indicators"] = _ind

_STUB_NAMES = [
    "setup", "orchestration", "position_manager",
    "fyers_apiv3", "fyers_apiv3.fyersModel",
    "contract_metadata", "expiry_manager",
    "failed_breakout_detector", "reversal_detector",
    "compression_detector",
    "day_type", "daily_sentiment",
    "volatility_context", "greeks_calculator",
]
for name in _STUB_NAMES:
    if name not in sys.modules:
        sys.modules[name] = MagicMock()

import numpy as np
from signals import compute_tilt_state
from log_parser import LogParser, SessionSummary, _P_TAGS


# ═════════════════════════════════════════════════════════════════════════════
# 1. TILT DETECTOR TESTS
# ═════════════════════════════════════════════════════════════════════════════

class TestComputeTiltState(unittest.TestCase):
    """Test compute_tilt_state from signals.py."""

    def test_bullish_tilt(self):
        """Price above TC and above R3 → BULLISH_TILT."""
        cpr = {"tc": 22000, "bc": 21900, "pivot": 21950}
        cam = {"r3": 22100, "r4": 22200, "s3": 21800, "s4": 21700}
        result = compute_tilt_state(22150.0, cpr, cam)
        self.assertEqual(result, "BULLISH_TILT")

    def test_bearish_tilt(self):
        """Price below BC and below S3 → BEARISH_TILT."""
        cpr = {"tc": 22000, "bc": 21900, "pivot": 21950}
        cam = {"r3": 22100, "r4": 22200, "s3": 21800, "s4": 21700}
        result = compute_tilt_state(21750.0, cpr, cam)
        self.assertEqual(result, "BEARISH_TILT")

    def test_neutral_between_levels(self):
        """Price between S3 and R3 → NEUTRAL."""
        cpr = {"tc": 22000, "bc": 21900, "pivot": 21950}
        cam = {"r3": 22100, "r4": 22200, "s3": 21800, "s4": 21700}
        result = compute_tilt_state(21950.0, cpr, cam)
        self.assertEqual(result, "NEUTRAL")

    def test_neutral_above_cpr_below_r3(self):
        """Price above CPR TC but below R3 → NEUTRAL (need both conditions)."""
        cpr = {"tc": 22000, "bc": 21900, "pivot": 21950}
        cam = {"r3": 22100, "r4": 22200, "s3": 21800, "s4": 21700}
        result = compute_tilt_state(22050.0, cpr, cam)
        self.assertEqual(result, "NEUTRAL")

    def test_neutral_below_cpr_above_s3(self):
        """Price below CPR BC but above S3 → NEUTRAL."""
        cpr = {"tc": 22000, "bc": 21900, "pivot": 21950}
        cam = {"r3": 22100, "r4": 22200, "s3": 21800, "s4": 21700}
        result = compute_tilt_state(21850.0, cpr, cam)
        self.assertEqual(result, "NEUTRAL")

    def test_missing_cpr_returns_neutral(self):
        """Missing CPR levels → NEUTRAL."""
        result = compute_tilt_state(22000.0, None, {"r3": 22100, "s3": 21800})
        self.assertEqual(result, "NEUTRAL")

    def test_missing_camarilla_returns_neutral(self):
        """Missing Camarilla levels → NEUTRAL."""
        result = compute_tilt_state(22000.0, {"tc": 22000, "bc": 21900}, None)
        self.assertEqual(result, "NEUTRAL")

    def test_nan_close_returns_neutral(self):
        """NaN close price → NEUTRAL."""
        cpr = {"tc": 22000, "bc": 21900, "pivot": 21950}
        cam = {"r3": 22100, "s3": 21800}
        result = compute_tilt_state(float("nan"), cpr, cam)
        self.assertEqual(result, "NEUTRAL")

    def test_none_close_returns_neutral(self):
        """None close price → NEUTRAL."""
        result = compute_tilt_state(None, {"tc": 22000, "bc": 21900}, {"r3": 22100, "s3": 21800})
        self.assertEqual(result, "NEUTRAL")

    def test_exact_boundary_r3(self):
        """Price exactly at R3 (not above) → NEUTRAL."""
        cpr = {"tc": 22000, "bc": 21900, "pivot": 21950}
        cam = {"r3": 22100, "s3": 21800}
        result = compute_tilt_state(22100.0, cpr, cam)
        self.assertEqual(result, "NEUTRAL")

    def test_exact_boundary_s3(self):
        """Price exactly at S3 (not below) → NEUTRAL."""
        cpr = {"tc": 22000, "bc": 21900, "pivot": 21950}
        cam = {"r3": 22100, "s3": 21800}
        result = compute_tilt_state(21800.0, cpr, cam)
        self.assertEqual(result, "NEUTRAL")


# ═════════════════════════════════════════════════════════════════════════════
# 2. LOG PARSER TESTS
# ═════════════════════════════════════════════════════════════════════════════

class _ParseHelper:
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


class TestTiltStateParsing(_ParseHelper, unittest.TestCase):
    """[TILT_STATE=...] tag parsing."""

    def test_bullish_tilt_counted(self):
        lines = [
            "2026-03-07 10:00:00 [TILT_STATE=BULLISH_TILT] side=CALL close=22150.00",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.tilt_state_count, 1)
        self.assertEqual(s.tag_counts.get("TILT_STATE", 0), 1)

    def test_bearish_tilt_counted(self):
        lines = [
            "2026-03-07 10:00:00 [TILT_STATE=BEARISH_TILT] side=PUT close=21750.00",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.tilt_state_count, 1)

    def test_neutral_tilt_counted(self):
        lines = [
            "2026-03-07 10:00:00 [TILT_STATE=NEUTRAL] side=CALL close=21950.00",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.tilt_state_count, 1)

    def test_multiple_tilt_states(self):
        lines = [
            "2026-03-07 10:00:00 [TILT_STATE=BULLISH_TILT] side=CALL close=22150.00",
            "2026-03-07 10:03:00 [TILT_STATE=NEUTRAL] side=CALL close=21950.00",
            "2026-03-07 10:06:00 [TILT_STATE=BEARISH_TILT] side=PUT close=21750.00",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.tilt_state_count, 3)


class TestGovernanceParsing(_ParseHelper, unittest.TestCase):
    """[GOVERNANCE_EASY] and [GOVERNANCE_STRICT] parsing."""

    def test_governance_easy_counted(self):
        lines = [
            "2026-03-07 10:00:00 [GOVERNANCE_EASY] timestamp=2026-03-07 symbol=NIFTY side=CALL tilt=BULLISH_TILT",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.governance_easy_count, 1)
        self.assertEqual(s.tag_counts.get("GOVERNANCE_EASY", 0), 1)

    def test_governance_strict_counted(self):
        lines = [
            "2026-03-07 10:00:00 [GOVERNANCE_STRICT] timestamp=2026-03-07 symbol=NIFTY side=CALL tilt=NEUTRAL",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.governance_strict_count, 1)
        self.assertEqual(s.tag_counts.get("GOVERNANCE_STRICT", 0), 1)

    def test_both_governance_tags(self):
        lines = [
            "2026-03-07 10:00:00 [GOVERNANCE_EASY] symbol=NIFTY side=CALL tilt=BULLISH_TILT",
            "2026-03-07 10:03:00 [GOVERNANCE_STRICT] symbol=NIFTY side=PUT tilt=NEUTRAL",
            "2026-03-07 10:06:00 [GOVERNANCE_EASY] symbol=BANKNIFTY side=PUT tilt=BEARISH_TILT",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.governance_easy_count, 2)
        self.assertEqual(s.governance_strict_count, 1)


class TestTiltTradeAttribution(_ParseHelper, unittest.TestCase):
    """Tilt state attached to trade records."""

    def test_tilt_on_trade(self):
        lines = [
            "2026-03-07 10:00:00 [TILT_STATE=BULLISH_TILT] side=CALL close=22150.00",
            "2026-03-07 10:01:00 [EXIT][PAPER TG_HIT] CALL NIFTY26MAR25000CE Entry=200.0 Exit=220.0 Qty=75 PnL=1500.00 (points=20.0) BarsHeld=5",
        ]
        s = self._parse_lines(lines)
        if s.trades:
            self.assertEqual(s.trades[0].get("tilt_state"), "BULLISH_TILT")

    def test_default_tilt_neutral(self):
        """No tilt tag before trade → default NEUTRAL."""
        lines = [
            "2026-03-07 10:01:00 [EXIT][PAPER SL_HIT] CALL NIFTY26MAR25000CE Entry=200.0 Exit=180.0 Qty=75 PnL=-1500.00 (points=-20.0) BarsHeld=3",
        ]
        s = self._parse_lines(lines)
        if s.trades:
            self.assertEqual(s.trades[0].get("tilt_state"), "NEUTRAL")


class TestTiltZeroCounts(_ParseHelper, unittest.TestCase):
    """Absent Phase 6.1 tags produce zero counts."""

    def test_all_zero(self):
        lines = ["2026-03-07 10:00:00 INFO Normal log"]
        s = self._parse_lines(lines)
        self.assertEqual(s.tilt_state_count, 0)
        self.assertEqual(s.governance_easy_count, 0)
        self.assertEqual(s.governance_strict_count, 0)


class TestToDictPhase61(_ParseHelper, unittest.TestCase):
    """to_dict includes Phase 6.1 fields."""

    def test_dict_keys(self):
        lines = [
            "2026-03-07 10:00:00 [TILT_STATE=BULLISH_TILT] side=CALL close=22150.00",
        ]
        s = self._parse_lines(lines)
        d = s.to_dict()
        self.assertIn("tilt_state_count", d)
        self.assertIn("governance_easy_count", d)
        self.assertIn("governance_strict_count", d)
        self.assertIn("tilt_performance", d)


class TestPTagsPhase61(unittest.TestCase):
    """Phase 6.1 tags in _P_TAGS list."""

    def test_all_present(self):
        for tag in ("TILT_STATE", "GOVERNANCE_EASY", "GOVERNANCE_STRICT"):
            self.assertIn(tag, _P_TAGS, f"{tag} missing from _P_TAGS")


# ═════════════════════════════════════════════════════════════════════════════
# 3. TILT PERFORMANCE PROPERTY
# ═════════════════════════════════════════════════════════════════════════════

class TestTiltPerformanceProperty(unittest.TestCase):

    def test_performance_breakdown(self):
        trades = [
            {"side": "CALL", "pnl_pts": 15.0, "tilt_state": "BULLISH_TILT"},
            {"side": "CALL", "pnl_pts": -5.0, "tilt_state": "BULLISH_TILT"},
            {"side": "PUT", "pnl_pts": 10.0, "tilt_state": "BEARISH_TILT"},
            {"side": "CALL", "pnl_pts": 3.0, "tilt_state": "NEUTRAL"},
        ]
        s = SessionSummary(
            log_path="test.log", session_type="PAPER",
            date_tag="2026-03-07", trades=trades,
        )
        perf = s.tilt_performance
        self.assertEqual(perf["BULLISH_TILT"]["trades"], 2)
        self.assertEqual(perf["BULLISH_TILT"]["winners"], 1)
        self.assertAlmostEqual(perf["BULLISH_TILT"]["net_pnl"], 10.0)
        self.assertEqual(perf["BEARISH_TILT"]["trades"], 1)
        self.assertEqual(perf["BEARISH_TILT"]["winners"], 1)
        self.assertEqual(perf["NEUTRAL"]["trades"], 1)

    def test_empty_trades(self):
        s = SessionSummary(
            log_path="test.log", session_type="PAPER", date_tag="2026-03-07",
        )
        self.assertEqual(s.tilt_performance, {})


# ═════════════════════════════════════════════════════════════════════════════
# 4. DASHBOARD TESTS
# ═════════════════════════════════════════════════════════════════════════════

class TestTiltDashboardSection(unittest.TestCase):
    """TILT PERFORMANCE section rendering."""

    def _render(self, session):
        from dashboard import _write_text_report
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            path = Path(f.name)
        try:
            _write_text_report(session, path)
            return path.read_text()
        finally:
            path.unlink(missing_ok=True)

    def test_section_appears_with_tilt_trades(self):
        trades = [
            {"side": "CALL", "pnl_pts": 10.0, "tilt_state": "BULLISH_TILT"},
            {"side": "PUT", "pnl_pts": -5.0, "tilt_state": "BEARISH_TILT"},
        ]
        session = SessionSummary(
            log_path="test.log", session_type="PAPER",
            date_tag="2026-03-07", trades=trades,
            tilt_state_count=2, governance_easy_count=1, governance_strict_count=1,
        )
        text = self._render(session)
        self.assertIn("TILT PERFORMANCE", text)
        self.assertIn("BULLISH_TILT", text)
        self.assertIn("BEARISH_TILT", text)
        self.assertIn("Governance EASY entries", text)
        self.assertIn("Governance STRICT entries", text)

    def test_section_absent_when_neutral_only(self):
        """Only NEUTRAL trades → no tilt section (needs BULLISH/BEARISH)."""
        trades = [
            {"side": "CALL", "pnl_pts": 5.0, "tilt_state": "NEUTRAL"},
        ]
        session = SessionSummary(
            log_path="test.log", session_type="PAPER",
            date_tag="2026-03-07", trades=trades,
        )
        text = self._render(session)
        self.assertNotIn("TILT PERFORMANCE", text)

    def test_section_absent_when_no_data(self):
        session = SessionSummary(
            log_path="test.log", session_type="PAPER", date_tag="2026-03-07",
        )
        text = self._render(session)
        self.assertNotIn("TILT PERFORMANCE", text)


# ═════════════════════════════════════════════════════════════════════════════
# 5. GOVERNANCE EXECUTION TESTS
# ═════════════════════════════════════════════════════════════════════════════

class TestGovernanceExecution(unittest.TestCase):
    """Verify governance tags exist in execution source."""

    def test_governance_easy_tag(self):
        import inspect
        import execution
        src = inspect.getsource(execution)
        self.assertIn("[GOVERNANCE_EASY]", src)

    def test_governance_strict_tag(self):
        import inspect
        import execution
        src = inspect.getsource(execution)
        self.assertIn("GOVERNANCE_STRICT", src)

    def test_tilt_state_in_st_details(self):
        """execution.py stores tilt_state in st_details."""
        import inspect
        import execution
        src = inspect.getsource(execution)
        self.assertIn("tilt_state", src)
        self.assertIn("tilt_aligned", src)

    def test_path_g_exists(self):
        """Path G tilt governance override exists."""
        import inspect
        import execution
        src = inspect.getsource(execution)
        self.assertIn("Path G", src)
        self.assertIn("TILT_GOVERNANCE_OVERRIDE", src)


# ═════════════════════════════════════════════════════════════════════════════
# 6. BACKWARD COMPATIBILITY
# ═════════════════════════════════════════════════════════════════════════════

class TestPhase61BackwardCompat(_ParseHelper, unittest.TestCase):

    def test_old_log_no_tilt(self):
        lines = [
            "2026-02-24 10:00:00 [SIGNAL FIRED] CALL source=PIVOT score=65",
            "2026-02-24 10:01:00 [ENTRY OK] CALL score=65/50 MODERATE HIGH",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.tilt_state_count, 0)
        self.assertEqual(s.governance_easy_count, 0)
        self.assertEqual(s.governance_strict_count, 0)
        self.assertEqual(s.tilt_performance, {})


# ═════════════════════════════════════════════════════════════════════════════
# 7. END-TO-END PIPELINE
# ═════════════════════════════════════════════════════════════════════════════

class TestEndToEndPhase61(_ParseHelper, unittest.TestCase):

    def test_full_pipeline(self):
        from dashboard import _write_text_report
        lines = [
            "2026-03-07 10:00:00 [TILT_STATE=BULLISH_TILT] side=CALL close=22150.00",
            "2026-03-07 10:00:30 [GOVERNANCE_EASY] timestamp=2026-03-07 symbol=NIFTY side=CALL tilt=BULLISH_TILT",
            "2026-03-07 10:01:00 [EXIT][PAPER TG_HIT] CALL NIFTY26MAR25000CE Entry=200.0 Exit=220.0 Qty=75 PnL=1500.00 (points=20.0) BarsHeld=5",
            "2026-03-07 10:02:00 [TILT_STATE=NEUTRAL] side=PUT close=21950.00",
            "2026-03-07 10:02:30 [GOVERNANCE_STRICT] timestamp=2026-03-07 symbol=NIFTY side=PUT tilt=NEUTRAL",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.tilt_state_count, 2)
        self.assertEqual(s.governance_easy_count, 1)
        self.assertEqual(s.governance_strict_count, 1)

        # Trade should have BULLISH_TILT
        if s.trades:
            self.assertEqual(s.trades[0].get("tilt_state"), "BULLISH_TILT")

        # Dashboard renders
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            txt_path = Path(f.name)
        try:
            _write_text_report(s, txt_path)
            text = txt_path.read_text()
            self.assertIn("TILT PERFORMANCE", text)
        finally:
            txt_path.unlink(missing_ok=True)


# ═════════════════════════════════════════════════════════════════════════════
# 8. PHASE 6.1.2 — REPLAY RELAXATION PATCH
# ═════════════════════════════════════════════════════════════════════════════

class TestBiasMisalignBypass(_ParseHelper, unittest.TestCase):
    """Verify [GOVERNANCE_EASY][BIAS_MISALIGN_BYPASSED] parsing."""

    def test_bias_misalign_bypassed_counted(self):
        lines = [
            "2026-03-07 10:00:00 [GOVERNANCE_EASY][BIAS_MISALIGN_BYPASSED] "
            "timestamp=2026-03-07 symbol=NIFTY side=CALL tilt=BULLISH_TILT "
            "bias=NEUTRAL RSI=72.0 CCI=160.0 "
            "reason=Tilt-aligned, bias misalignment block bypassed",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.governance_easy_count, 1)
        self.assertEqual(s.tilt_bias_override_count, 1)

    def test_normal_governance_easy_no_bias_override(self):
        lines = [
            "2026-03-07 10:00:00 [GOVERNANCE_EASY] timestamp=2026-03-07 "
            "symbol=NIFTY side=CALL tilt=BULLISH_TILT",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.governance_easy_count, 1)
        self.assertEqual(s.tilt_bias_override_count, 0)

    def test_governance_strict_no_bias_override(self):
        lines = [
            "2026-03-07 10:00:00 [GOVERNANCE_STRICT] timestamp=2026-03-07 "
            "symbol=NIFTY side=PUT tilt=NEUTRAL",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.governance_strict_count, 1)
        self.assertEqual(s.tilt_bias_override_count, 0)

    def test_mixed_governance_with_bias_bypass(self):
        lines = [
            "2026-03-07 10:00:00 [GOVERNANCE_EASY][BIAS_MISALIGN_BYPASSED] "
            "timestamp=2026-03-07 symbol=NIFTY side=CALL tilt=BULLISH_TILT "
            "bias=NEUTRAL RSI=72.0 CCI=160.0 "
            "reason=Tilt-aligned, bias misalignment block bypassed",
            "2026-03-07 10:05:00 [GOVERNANCE_EASY] timestamp=2026-03-07 "
            "symbol=NIFTY side=PUT tilt=BEARISH_TILT",
            "2026-03-07 10:10:00 [GOVERNANCE_STRICT] timestamp=2026-03-07 "
            "symbol=NIFTY side=CALL tilt=NEUTRAL",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.governance_easy_count, 2)
        self.assertEqual(s.governance_strict_count, 1)
        self.assertEqual(s.tilt_bias_override_count, 1)

    def test_to_dict_includes_bias_override(self):
        lines = [
            "2026-03-07 10:00:00 [GOVERNANCE_EASY][BIAS_MISALIGN_BYPASSED] "
            "timestamp=2026-03-07 symbol=NIFTY side=CALL tilt=BULLISH_TILT "
            "bias=NEUTRAL RSI=72.0 CCI=160.0 "
            "reason=Tilt-aligned, bias misalignment block bypassed",
        ]
        s = self._parse_lines(lines)
        d = s.to_dict()
        self.assertEqual(d["tilt_bias_override_count"], 1)


class TestDashboardBiasOverride(unittest.TestCase):
    """Verify dashboard shows bias misalignment bypass when present."""

    def test_dashboard_shows_bias_bypass(self):
        from dashboard import _write_text_report
        s = SessionSummary(
            log_path="test.log",
            session_type="REPLAY",
            date_tag="2026-03-07",
            tilt_state_count=3,
            governance_easy_count=2,
            governance_strict_count=1,
            tilt_bias_override_count=1,
        )
        # Need tilt_performance with at least one non-NEUTRAL entry
        s.trades = [
            {"side": "CALL", "pnl_pts": 10.0, "tilt_state": "BULLISH_TILT"},
        ]
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            txt_path = Path(f.name)
        try:
            _write_text_report(s, txt_path)
            text = txt_path.read_text()
            self.assertIn("Bias misalign bypassed", text)
            self.assertIn("1", text)
        finally:
            txt_path.unlink(missing_ok=True)

    def test_dashboard_hides_bias_bypass_when_zero(self):
        from dashboard import _write_text_report
        s = SessionSummary(
            log_path="test.log",
            session_type="REPLAY",
            date_tag="2026-03-07",
            tilt_state_count=2,
            governance_easy_count=1,
            governance_strict_count=1,
            tilt_bias_override_count=0,
        )
        s.trades = [
            {"side": "CALL", "pnl_pts": 10.0, "tilt_state": "BULLISH_TILT"},
        ]
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            txt_path = Path(f.name)
        try:
            _write_text_report(s, txt_path)
            text = txt_path.read_text()
            self.assertNotIn("Bias misalign bypassed", text)
        finally:
            txt_path.unlink(missing_ok=True)


class TestReplayGovernanceE2E(_ParseHelper, unittest.TestCase):
    """End-to-end: replay with tilt-aligned bias override + trade attribution."""

    def test_replay_bias_bypass_with_trade(self):
        lines = [
            "2026-03-07 09:30:00 [TILT_STATE=BULLISH_TILT] side=CALL close=22200.00",
            "2026-03-07 09:30:05 [GOVERNANCE_EASY][BIAS_MISALIGN_BYPASSED] "
            "timestamp=2026-03-07 symbol=NIFTY side=CALL tilt=BULLISH_TILT "
            "bias=NEUTRAL RSI=73.0 CCI=165.0 "
            "reason=Tilt-aligned, bias misalignment block bypassed",
            "2026-03-07 09:31:00 [EXIT][PAPER TG_HIT] CALL NIFTY26MAR25000CE "
            "Entry=200.0 Exit=225.0 Qty=75 PnL=1875.00 (points=25.0) BarsHeld=4",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.tilt_state_count, 1)
        self.assertEqual(s.governance_easy_count, 1)
        self.assertEqual(s.tilt_bias_override_count, 1)
        self.assertEqual(len(s.trades), 1)
        self.assertEqual(s.trades[0].get("tilt_state"), "BULLISH_TILT")

    def test_replay_neutral_tilt_strict(self):
        lines = [
            "2026-03-07 09:30:00 [TILT_STATE=NEUTRAL] side=CALL close=22000.00",
            "2026-03-07 09:30:05 [GOVERNANCE_STRICT] timestamp=2026-03-07 "
            "symbol=NIFTY side=CALL tilt=NEUTRAL",
        ]
        s = self._parse_lines(lines)
        self.assertEqual(s.governance_strict_count, 1)
        self.assertEqual(s.tilt_bias_override_count, 0)

    def test_compare_sessions_aggregates_bias_override(self):
        from dashboard import compare_sessions
        lines_a = [
            "2026-03-07 10:00:00 [GOVERNANCE_EASY][BIAS_MISALIGN_BYPASSED] "
            "timestamp=2026-03-07 symbol=NIFTY side=CALL tilt=BULLISH_TILT "
            "bias=NEUTRAL RSI=72.0 CCI=160.0 "
            "reason=Tilt-aligned, bias misalignment block bypassed",
        ]
        lines_b = [
            "2026-03-08 10:00:00 [GOVERNANCE_EASY][BIAS_MISALIGN_BYPASSED] "
            "timestamp=2026-03-08 symbol=NIFTY side=PUT tilt=BEARISH_TILT "
            "bias=NEUTRAL RSI=28.0 CCI=-160.0 "
            "reason=Tilt-aligned, bias misalignment block bypassed",
        ]
        import tempfile, os
        files = []
        for lines in (lines_a, lines_b):
            fd, path = tempfile.mkstemp(suffix=".log")
            with os.fdopen(fd, "w") as f:
                for l in lines:
                    f.write(l + "\n")
            files.append(path)
        out_dir = tempfile.mkdtemp()
        try:
            compare_sessions(files[:1], files[1:], output_dir=out_dir)
            report = Path(out_dir) / "comparison_report.txt"
            self.assertTrue(report.exists())
        finally:
            for p in files:
                os.unlink(p)


if __name__ == "__main__":
    unittest.main()
