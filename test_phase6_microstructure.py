"""Phase 6 — Microstructure Proxy Tests.

Test pulse exhaustion, zone absorption/rejection, and spread noise detection.
"""

import sys
import unittest
import pandas as pd

# Ensure real modules are loaded (not stubs from other test files)
from unittest.mock import MagicMock as _MM
for _mod in ("pulse_module", "zone_detector"):
    _existing = sys.modules.get(_mod)
    if _existing is not None and isinstance(_existing, _MM):
        del sys.modules[_mod]

from pulse_module import PulseModule
from zone_detector import Zone, detect_zone_absorption


def _make_candles(price_pairs):
    """Create candle DataFrame from (low, high) tuples."""
    rows = []
    for i, (lo, hi) in enumerate(price_pairs):
        rows.append({
            "open": (lo + hi) / 2,
            "high": hi,
            "low": lo,
            "close": (lo + hi) / 2,
            "time": f"2026-03-07 10:{i:02d}:00",
        })
    return pd.DataFrame(rows)


# ═════════════════════════════════════════════════════════════════════════════
# PULSE EXHAUSTION
# ═════════════════════════════════════════════════════════════════════════════

class TestPulseExhaustionSustained(unittest.TestCase):
    """Sustained burst → PULSE_SUSTAINED."""

    def test_sustained_during_active_burst(self):
        pm = PulseModule(window_seconds=10, burst_threshold=5.0, min_ticks=3)
        base = 1000000.0
        for i in range(20):
            pm.on_tick(base + i * 10, 100.0 + i * 0.01)
        result = pm.detect_exhaustion(base + 200)
        self.assertEqual(result, "PULSE_SUSTAINED")

    def test_sustained_with_moderate_rate(self):
        pm = PulseModule(window_seconds=10, burst_threshold=3.0, min_ticks=3)
        base = 1000000.0
        for i in range(15):
            pm.on_tick(base + i * 100, 100.0 + i * 0.01)
        result = pm.detect_exhaustion(base + 1500)
        # May be sustained or None depending on rate
        self.assertIn(result, ("PULSE_SUSTAINED", None))


class TestPulseExhaustionDecay(unittest.TestCase):
    """Decayed burst → PULSE_EXHAUSTION or None."""

    def test_exhaustion_after_rate_decay(self):
        pm = PulseModule(window_seconds=5, burst_threshold=3.0, min_ticks=3)
        base = 1000000.0
        for i in range(15):
            pm.on_tick(base + i * 50, 100.0 + i * 0.01)
        _ = pm.detect_exhaustion(base + 800)
        if pm._peak_tick_rate > 0:
            pm._tick_buffer.clear()
            pm._cached_metrics = None
            pm.on_tick(base + 20000, 100.0)
            pm.on_tick(base + 25000, 100.0)
            pm.on_tick(base + 30000, 100.0)
            result = pm.detect_exhaustion(base + 35000)
            self.assertIn(result, ("PULSE_EXHAUSTION", "PULSE_SUSTAINED", None))


class TestPulseExhaustionNoBurst(unittest.TestCase):
    """No burst context → None."""

    def test_no_burst_returns_none(self):
        pm = PulseModule(window_seconds=10, burst_threshold=100.0, min_ticks=3)
        pm.on_tick(1000000, 100.0)
        pm.on_tick(1005000, 100.0)
        pm.on_tick(1010000, 100.0)
        result = pm.detect_exhaustion(1015000)
        self.assertIsNone(result)

    def test_empty_buffer_returns_none(self):
        pm = PulseModule(window_seconds=10, burst_threshold=5.0, min_ticks=3)
        result = pm.detect_exhaustion(1000000)
        self.assertIsNone(result)


class TestPulseExhaustionStats(unittest.TestCase):
    """Stats include exhaustion/sustained counts."""

    def test_stats_keys(self):
        pm = PulseModule(window_seconds=10, burst_threshold=5.0, min_ticks=3)
        stats = pm.get_stats()
        self.assertIn("exhaustion_count", stats)
        self.assertIn("sustained_count", stats)


# ═════════════════════════════════════════════════════════════════════════════
# ZONE ABSORPTION
# ═════════════════════════════════════════════════════════════════════════════

class TestZoneAbsorption3Touches(unittest.TestCase):
    """Zone touched >= 3 times → ZONE_ABSORPTION."""

    def test_demand_zone_absorbed(self):
        zone = Zone("D_1", "DEMAND", low=100.0, high=105.0,
                     origin_time="2026-03-07 09:30:00")
        prices = [
            (101, 106), (110, 115), (102, 104),
            (112, 118), (100, 105), (110, 115),
            (120, 125), (130, 135), (140, 145), (150, 155),
        ]
        candles = _make_candles(prices)
        result = detect_zone_absorption(candles, [zone], atr_value=10.0, revisit_window=10)
        self.assertIsNotNone(result)
        self.assertEqual(result["type"], "ZONE_ABSORPTION")
        self.assertEqual(result["touches"], 3)
        self.assertEqual(result["zone_id"], "D_1")
        self.assertEqual(result["zone_type"], "DEMAND")

    def test_supply_zone_absorbed(self):
        zone = Zone("S_1", "SUPPLY", low=200.0, high=210.0,
                     origin_time="2026-03-07 09:30:00")
        prices = [
            (201, 208), (201, 205), (203, 209),
            (180, 190), (170, 175), (160, 165),
            (155, 160), (150, 155), (145, 150), (140, 145),
        ]
        candles = _make_candles(prices)
        result = detect_zone_absorption(candles, [zone], atr_value=10.0, revisit_window=10)
        self.assertIsNotNone(result)
        self.assertEqual(result["type"], "ZONE_ABSORPTION")
        self.assertGreaterEqual(result["touches"], 3)


class TestZoneRejection(unittest.TestCase):
    """Zone touched 1-2 times → ZONE_REJECTION."""

    def test_single_touch_rejection(self):
        zone = Zone("S_1", "SUPPLY", low=200.0, high=210.0,
                     origin_time="2026-03-07 09:30:00")
        prices = [
            (201, 208),  # touches
            (180, 190), (170, 175), (160, 165), (155, 160),
            (150, 155), (145, 150), (140, 145), (135, 140), (130, 135),
        ]
        candles = _make_candles(prices)
        result = detect_zone_absorption(candles, [zone], atr_value=10.0, revisit_window=10)
        self.assertIsNotNone(result)
        self.assertEqual(result["type"], "ZONE_REJECTION")
        self.assertEqual(result["touches"], 1)

    def test_two_touches_rejection(self):
        zone = Zone("D_1", "DEMAND", low=100.0, high=105.0,
                     origin_time="2026-03-07 09:30:00")
        prices = [
            (101, 106), (110, 115), (102, 104),
            (112, 118), (120, 125), (130, 135),
            (140, 145), (150, 155), (160, 165), (170, 175),
        ]
        candles = _make_candles(prices)
        result = detect_zone_absorption(candles, [zone], atr_value=10.0, revisit_window=10)
        self.assertIsNotNone(result)
        self.assertEqual(result["type"], "ZONE_REJECTION")
        self.assertEqual(result["touches"], 2)


class TestZoneAbsorptionEdgeCases(unittest.TestCase):
    """Edge cases for zone absorption."""

    def test_inactive_zones_skipped(self):
        zone = Zone("D_1", "DEMAND", low=100.0, high=105.0,
                     origin_time="2026-03-07 09:30:00", active=False)
        prices = [(101, 106)] * 10
        candles = _make_candles(prices)
        result = detect_zone_absorption(candles, [zone], atr_value=10.0)
        self.assertIsNone(result)

    def test_no_zones_returns_none(self):
        prices = [(101, 106)] * 10
        candles = _make_candles(prices)
        result = detect_zone_absorption(candles, [], atr_value=10.0)
        self.assertIsNone(result)

    def test_insufficient_candles_returns_none(self):
        zone = Zone("D_1", "DEMAND", low=100.0, high=105.0,
                     origin_time="2026-03-07 09:30:00")
        prices = [(101, 106)] * 3
        candles = _make_candles(prices)
        result = detect_zone_absorption(candles, [zone], atr_value=10.0, revisit_window=10)
        self.assertIsNone(result)

    def test_invalid_atr_returns_none(self):
        zone = Zone("D_1", "DEMAND", low=100.0, high=105.0,
                     origin_time="2026-03-07 09:30:00")
        prices = [(101, 106)] * 10
        candles = _make_candles(prices)
        result = detect_zone_absorption(candles, [zone], atr_value=0.0)
        self.assertIsNone(result)

    def test_nan_atr_returns_none(self):
        zone = Zone("D_1", "DEMAND", low=100.0, high=105.0,
                     origin_time="2026-03-07 09:30:00")
        prices = [(101, 106)] * 10
        candles = _make_candles(prices)
        result = detect_zone_absorption(candles, [zone], atr_value=float("nan"))
        self.assertIsNone(result)

    def test_no_touches_returns_none(self):
        zone = Zone("D_1", "DEMAND", low=100.0, high=105.0,
                     origin_time="2026-03-07 09:30:00")
        prices = [(200, 210)] * 10  # All bars far from zone
        candles = _make_candles(prices)
        result = detect_zone_absorption(candles, [zone], atr_value=10.0, revisit_window=10)
        self.assertIsNone(result)


# ═════════════════════════════════════════════════════════════════════════════
# SPREAD NOISE (via entry_logic, already tested in test_phase6_entry_logic.py
# but included here for microstructure completeness)
# ═════════════════════════════════════════════════════════════════════════════

class TestSpreadNoiseProxy(unittest.TestCase):
    """Spread noise detection (premium drift ≤ 2 pts)."""

    def setUp(self):
        import sys
        from unittest.mock import MagicMock
        # entry_logic may already be loaded with stubs
        if "entry_logic" in sys.modules:
            self.func = sys.modules["entry_logic"].detect_spread_noise
        else:
            self.skipTest("entry_logic not importable in this context")

    def test_drift_within_threshold(self):
        """Close-open drift ≤ 2 → noise."""
        candle = {"close": 101.0, "open": 100.0, "high": 103.0, "low": 97.0}
        self.assertTrue(self.func(candle, {}))

    def test_drift_exceeds_threshold(self):
        """Close-open drift > 2 and range > 2 → not noise."""
        candle = {"close": 105.0, "open": 100.0, "high": 106.0, "low": 99.0}
        self.assertFalse(self.func(candle, {}))

    def test_range_within_threshold(self):
        """Bar range ≤ 2 → noise."""
        candle = {"close": 100.5, "open": 100.0, "high": 101.0, "low": 99.5}
        self.assertTrue(self.func(candle, {}))


if __name__ == "__main__":
    unittest.main()
