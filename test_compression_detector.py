# ===== test_compression_detector.py =====
"""
Unit tests for compression_detector.py

Tests:
  detect_compression() — all 3 conditions + edge cases
  detect_expansion()   — LONG/SHORT breakout + failure cases
  _build_entry_signal() — level calculations LONG/SHORT
  CompressionState     — state machine transitions + consume_entry
"""

import math
import pytest
import pandas as pd
import numpy as np

from compression_detector import (
    detect_compression,
    detect_expansion,
    _build_entry_signal,
    CompressionState,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_df(rows, atr=100.0):
    """
    Build a minimal 15m-style DataFrame.
    rows: list of (open, high, low, close)
    atr: scalar atr14 value applied to all rows (simulates a pre-computed ATR column)
    """
    data = {
        "open":  [r[0] for r in rows],
        "high":  [r[1] for r in rows],
        "low":   [r[2] for r in rows],
        "close": [r[3] for r in rows],
        "atr14": [atr]  * len(rows),
    }
    return pd.DataFrame(data)


def _tight_bars(atr=100.0, n=3, base=25000.0, drift=0.0):
    """
    Generate n tight bars that pass all 3 compression conditions.
    Range = 10 pts (< 0.45*100=45), cluster ~ 15 pts (< 1.2*10=12? careful…)
    Let's use open=close (no drift) and range=10 per bar.
    """
    # Each bar: open=base, high=base+10, low=base-5 → range=15, but we want avg<45
    # Keep bars very tight: open=base, high=base+8, low=base-4 → range=12 (avg=12 < 45 ✓)
    # cluster_range = base+8 - (base-4) = 12 == max_single → 12 < 1.2*12=14.4 ✓
    # net_move = abs(close_last - open_first) ~ small
    rows = []
    for i in range(n):
        o = base + drift * i
        h = o + 8
        l = o - 4
        c = o + 1   # very slight upward drift but < 0.5*atr
        rows.append((o, h, l, c))
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# detect_compression
# ─────────────────────────────────────────────────────────────────────────────

class TestDetectCompression:

    def test_all_conditions_pass(self):
        rows = _tight_bars(atr=100.0)
        df = _make_df(rows, atr=100.0)
        zone = detect_compression(df)
        assert zone is not None
        assert zone["compression_active"] is True
        assert zone["compression_high"] == pytest.approx(25008.0)
        assert zone["compression_low"]  == pytest.approx(24996.0)
        assert zone["compression_strength"] > 1.0    # ATR / avg_range > 1

    def test_condition1_fails_range_too_wide(self):
        # avg range = 50, ATR = 100 → 50 >= 0.45*100=45 → FAIL
        rows = [(25000, 25050, 25000, 25025)] * 3   # range=50 each bar
        df = _make_df(rows, atr=100.0)
        assert detect_compression(df) is None

    def test_condition2_fails_no_overlap(self):
        # Three consecutive non-overlapping bars: cluster_range >> max_single
        rows = [
            (25000, 25010, 25000, 25005),   # range=10
            (25020, 25030, 25020, 25025),   # range=10, no overlap with bar1
            (25040, 25050, 25040, 25045),   # range=10, no overlap with bar2
        ]
        # avg_range=10 < 0.45*100=45 ✓
        # cluster_range = 25050-25000 = 50, max_single=10 → 50 >= 1.2*10=12 → FAIL
        df = _make_df(rows, atr=100.0)
        assert detect_compression(df) is None

    def test_condition3_fails_directional_bias(self):
        # net_move = abs(close_last - open_first) >= 0.5*ATR
        # open_first=25000, close_last=25060 → net_move=60 >= 50 → FAIL
        rows = [
            (25000, 25010, 24998, 25005),
            (25010, 25020, 25008, 25015),
            (25050, 25060, 25048, 25060),   # large jump → net_move=60
        ]
        df = _make_df(rows, atr=100.0)
        # avg_range ~ 12 < 45 ✓, cluster ~ 62, max_single=12 → 62 >= 14.4 → cond2 also fails
        # Let me make a case where only cond3 fails
        rows2 = [
            (25000, 25008, 24996, 25004),   # range=12, close≈open ✓
            (25004, 25012, 25000, 25008),   # range=12
            (25008, 25016, 25004, 25055),   # range=12, but close=25055 → net_move=55 ≥ 50
        ]
        # cluster_range = 25016 - 24996 = 20, max_single=12 → 20 >= 14.4 → cond2 already fails
        # Design a cleaner test: use all bars same position, last close drifts far
        rows3 = [
            (25000, 25008, 24996, 25001),
            (25001, 25009, 24997, 25002),
            (25002, 25010, 24998, 25050),   # close_last=25050, open_first=25000 → net=50 ≥ 50 → FAIL
        ]
        # cluster: 25010 - 24996 = 14; max_single=12; 14 < 14.4 ✓ cond2 passes
        df3 = _make_df(rows3, atr=100.0)
        assert detect_compression(df3) is None

    def test_nan_atr_returns_none(self):
        rows = _tight_bars(atr=100.0)
        df = _make_df(rows, atr=float("nan"))
        assert detect_compression(df) is None

    def test_zero_atr_returns_none(self):
        rows = _tight_bars(atr=100.0)
        df = _make_df(rows, atr=0.0)
        assert detect_compression(df) is None

    def test_fewer_than_3_bars_returns_none(self):
        rows = _tight_bars(atr=100.0, n=2)
        df = _make_df(rows, atr=100.0)
        assert detect_compression(df) is None

    def test_empty_df_returns_none(self):
        df = pd.DataFrame(columns=["open", "high", "low", "close", "atr14"])
        assert detect_compression(df) is None

    def test_missing_column_returns_none(self):
        rows = _tight_bars(atr=100.0)
        df = _make_df(rows, atr=100.0).drop(columns=["atr14"])
        assert detect_compression(df) is None

    def test_compression_strength_formula(self):
        # strength = ATR / avg_range
        rows = _tight_bars(atr=100.0)
        df = _make_df(rows, atr=100.0)
        zone = detect_compression(df)
        assert zone is not None
        expected_strength = 100.0 / zone["avg_range"]
        assert zone["compression_strength"] == pytest.approx(expected_strength, rel=0.01)

    def test_start_index_correct(self):
        # Add extra bars before the tight bars
        extra = [(25200, 25250, 25150, 25200)] * 5
        tight = _tight_bars(atr=100.0, n=3, base=25000.0)
        all_rows = extra + tight
        df = _make_df(all_rows, atr=100.0)
        zone = detect_compression(df)
        assert zone is not None
        assert zone["compression_start_index"] == 5   # iloc[-3] = index 5


# ─────────────────────────────────────────────────────────────────────────────
# detect_expansion
# ─────────────────────────────────────────────────────────────────────────────

class TestDetectExpansion:

    def _make_zone(self, high=25010.0, low=24990.0, atr=100.0):
        return {
            "compression_high":        high,
            "compression_low":         low,
            "compression_start_index": 0,
            "compression_strength":    8.0,
            "atr_15m":                 atr,
            "avg_range":               12.5,
            "compression_active":      True,
        }

    def test_long_breakout_confirmed(self):
        # close > compression_high, range > 1.3*ATR
        zone = self._make_zone(high=25010.0, low=24990.0, atr=100.0)
        rows = [(24990, 25150, 24985, 25100)]   # range=165 > 130, close=25100 > 25010
        df = _make_df(rows, atr=100.0)
        exp = detect_expansion(df, zone)
        assert exp is not None
        assert exp["expansion_confirmed"] is True
        assert exp["direction"] == "LONG"
        assert exp["breakout_close"] == 25100.0

    def test_short_breakout_confirmed(self):
        zone = self._make_zone(high=25010.0, low=24990.0, atr=100.0)
        rows = [(25010, 25015, 24820, 24880)]   # range=195 > 130, close=24880 < 24990
        df = _make_df(rows, atr=100.0)
        exp = detect_expansion(df, zone)
        assert exp is not None
        assert exp["direction"] == "SHORT"
        assert exp["breakout_close"] == 24880.0

    def test_close_inside_zone_returns_none(self):
        zone = self._make_zone(high=25010.0, low=24990.0, atr=100.0)
        rows = [(24990, 25150, 24985, 25005)]   # range=165 > 130 but close=25005 inside zone
        df = _make_df(rows, atr=100.0)
        assert detect_expansion(df, zone) is None

    def test_range_too_small_returns_none(self):
        zone = self._make_zone(high=25010.0, low=24990.0, atr=100.0)
        rows = [(24990, 25120, 24995, 25100)]   # range=125 ≤ 130
        df = _make_df(rows, atr=100.0)
        assert detect_expansion(df, zone) is None

    def test_empty_df_returns_none(self):
        zone = self._make_zone()
        df = pd.DataFrame(columns=["open", "high", "low", "close", "atr14"])
        assert detect_expansion(df, zone) is None

    def test_none_zone_returns_none(self):
        rows = [(24990, 25150, 24985, 25100)]
        df = _make_df(rows, atr=100.0)
        assert detect_expansion(df, None) is None


# ─────────────────────────────────────────────────────────────────────────────
# _build_entry_signal
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildEntrySignal:

    def _zone(self, atr=100.0, high=25010.0, low=24990.0):
        return {
            "compression_high":        high,
            "compression_low":         low,
            "compression_start_index": 3,
            "compression_strength":    8.0,
            "atr_15m":                 atr,
            "avg_range":               12.5,
            "compression_active":      True,
        }

    def test_long_entry_levels(self):
        zone = self._zone(atr=100.0, high=25010.0, low=24990.0)
        expansion = {"expansion_confirmed": True, "direction": "LONG",
                     "breakout_close": 25050.0, "candle_range": 150.0}
        sig = _build_entry_signal(zone, expansion)
        assert sig["side"] == "CALL"
        assert sig["sl"] == pytest.approx(24990.0)         # compression_low
        assert sig["tg"] == pytest.approx(25050.0 + 100.0) # entry + 1.0*ATR
        assert sig["pt"] == pytest.approx(25050.0 + 200.0) # entry + 2.0*ATR
        assert sig["source"] == "COMPRESSION_BREAKOUT"
        assert sig["strength"] == "HIGH"
        assert sig["score"] == 75

    def test_short_entry_levels(self):
        zone = self._zone(atr=100.0, high=25010.0, low=24990.0)
        expansion = {"expansion_confirmed": True, "direction": "SHORT",
                     "breakout_close": 24950.0, "candle_range": 150.0}
        sig = _build_entry_signal(zone, expansion)
        assert sig["side"] == "PUT"
        assert sig["sl"] == pytest.approx(25010.0)          # compression_high
        assert sig["tg"] == pytest.approx(24950.0 - 100.0)  # entry - 1.0*ATR
        assert sig["pt"] == pytest.approx(24950.0 - 200.0)  # entry - 2.0*ATR

    def test_signal_contains_compression_zone(self):
        zone = self._zone()
        expansion = {"expansion_confirmed": True, "direction": "LONG",
                     "breakout_close": 25050.0, "candle_range": 150.0}
        sig = _build_entry_signal(zone, expansion)
        assert sig["compression_zone"] is zone


# ─────────────────────────────────────────────────────────────────────────────
# CompressionState — state machine
# ─────────────────────────────────────────────────────────────────────────────

class TestCompressionState:

    def _tight_df(self, atr=100.0, n=3):
        rows = _tight_bars(atr=atr, n=n)
        return _make_df(rows, atr=atr)

    def _expansion_df(self, zone, direction="LONG", atr=100.0):
        """Single expansion bar that breaks out of zone."""
        if direction == "LONG":
            close = zone["compression_high"] + 10
            high  = zone["compression_high"] + 145
            low   = zone["compression_low"] - 5
        else:
            close = zone["compression_low"] - 10
            high  = zone["compression_high"] + 5
            low   = zone["compression_low"] - 145
        rows = [(zone["compression_low"], high, low, close)]
        return _make_df(rows, atr=atr)

    def test_initial_state_neutral(self):
        cs = CompressionState()
        assert cs.market_state == "NEUTRAL"
        assert cs.zone is None
        assert cs.entry_signal is None
        assert not cs.has_entry

    def test_neutral_to_energy_buildup(self):
        cs = CompressionState()
        df = self._tight_df(atr=100.0)
        cs.update(df)
        assert cs.market_state == "ENERGY_BUILDUP"
        assert cs.zone is not None
        assert not cs.has_entry

    def test_energy_buildup_to_volatility_expansion_long(self):
        cs = CompressionState()
        df = self._tight_df(atr=100.0)
        cs.update(df)
        assert cs.market_state == "ENERGY_BUILDUP"

        zone = cs.zone
        exp_df = self._expansion_df(zone, direction="LONG", atr=100.0)
        cs.update(exp_df)
        assert cs.market_state == "VOLATILITY_EXPANSION"
        assert cs.has_entry
        assert cs.entry_signal["side"] == "CALL"

    def test_energy_buildup_to_volatility_expansion_short(self):
        cs = CompressionState()
        df = self._tight_df(atr=100.0)
        cs.update(df)
        zone = cs.zone
        exp_df = self._expansion_df(zone, direction="SHORT", atr=100.0)
        cs.update(exp_df)
        assert cs.market_state == "VOLATILITY_EXPANSION"
        assert cs.entry_signal["side"] == "PUT"

    def test_zone_dissolves_reverts_to_neutral(self):
        cs = CompressionState()
        # First: enter ENERGY_BUILDUP
        tight_df = self._tight_df(atr=100.0)
        cs.update(tight_df)
        assert cs.market_state == "ENERGY_BUILDUP"

        # Wide bars — no compression, no expansion either (small range)
        wide_rows = [(25000, 25060, 24940, 25000)] * 3   # range=120 > 45, no compression
        # but range=120 < 1.3*100=130 → also no expansion
        wide_df = _make_df(wide_rows, atr=100.0)
        cs.update(wide_df)
        assert cs.market_state == "NEUTRAL"
        assert cs.zone is None
        assert not cs.has_entry

    def test_consume_entry_resets_state(self):
        cs = CompressionState()
        df = self._tight_df(atr=100.0)
        cs.update(df)
        zone = cs.zone
        exp_df = self._expansion_df(zone, atr=100.0)
        cs.update(exp_df)
        assert cs.has_entry

        cs.consume_entry()
        assert not cs.has_entry
        assert cs.market_state == "NEUTRAL"
        assert cs.zone is None

    def test_update_with_short_df_no_crash(self):
        cs = CompressionState()
        df = self._tight_df(atr=100.0, n=2)   # only 2 bars — too few
        cs.update(df)
        assert cs.market_state == "NEUTRAL"   # no change

    def test_update_with_none_no_crash(self):
        cs = CompressionState()
        cs.update(None)
        assert cs.market_state == "NEUTRAL"

    def test_compression_stays_active_on_continued_compression(self):
        cs = CompressionState()
        df1 = self._tight_df(atr=100.0)
        cs.update(df1)
        assert cs.market_state == "ENERGY_BUILDUP"

        # Another tight frame — stays ENERGY_BUILDUP
        df2 = self._tight_df(atr=100.0)
        cs.update(df2)
        assert cs.market_state == "ENERGY_BUILDUP"
        assert cs.zone is not None

    def test_volatility_expansion_state_persists_until_consumed(self):
        cs = CompressionState()
        df = self._tight_df(atr=100.0)
        cs.update(df)
        zone = cs.zone
        exp_df = self._expansion_df(zone, atr=100.0)
        cs.update(exp_df)
        assert cs.market_state == "VOLATILITY_EXPANSION"

        # Another update — still VOLATILITY_EXPANSION (not auto-reset)
        cs.update(exp_df)
        assert cs.market_state == "VOLATILITY_EXPANSION"
        assert cs.has_entry
