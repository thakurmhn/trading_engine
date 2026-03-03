from __future__ import annotations

import unittest

import pandas as pd

from zone_detector import (
    Zone,
    detect_zones,
    update_zone_activity,
    detect_zone_revisit,
)


def _mk_15m_for_demand():
    # consolidation then bullish impulse
    rows = []
    t0 = pd.Timestamp("2026-02-20 09:15:00")
    for i in range(10):
        rows.append([t0 + pd.Timedelta(minutes=15 * i), 100, 101, 99, 100, 1000])
    rows.append([t0 + pd.Timedelta(minutes=150), 100, 122, 99, 121, 5000])
    return pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])


def _mk_15m_for_supply():
    rows = []
    t0 = pd.Timestamp("2026-02-20 09:15:00")
    for i in range(10):
        rows.append([t0 + pd.Timedelta(minutes=15 * i), 100, 101, 99, 100, 1000])
    rows.append([t0 + pd.Timedelta(minutes=150), 100, 101, 78, 79, 5000])
    return pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])


class ZoneDetectorTests(unittest.TestCase):
    def test_detect_demand_zone(self):
        zones = detect_zones(_mk_15m_for_demand())
        self.assertTrue(any(z.zone_type == "DEMAND" for z in zones))

    def test_detect_supply_zone(self):
        zones = detect_zones(_mk_15m_for_supply())
        self.assertTrue(any(z.zone_type == "SUPPLY" for z in zones))

    def test_zone_inactive_after_breach(self):
        z = Zone(zone_id="D1", zone_type="DEMAND", low=95.0, high=100.0, origin_time="2026-02-20 10:30:00")
        zones = [z]
        update_zone_activity(zones, close_price=93.0, atr_value=4.0, bar_time="2026-02-21 11:00:00")
        self.assertFalse(zones[0].active)

    def test_revisit_breakout(self):
        z = Zone(zone_id="D1", zone_type="DEMAND", low=95.0, high=100.0, origin_time="2026-02-20 10:30:00")
        df = pd.DataFrame(
            {
                "open": [103, 102, 101, 99, 98, 94],
                "high": [104, 103, 102, 100, 99, 95],
                "low": [101, 100, 99, 97, 96, 93],
                "close": [103, 102, 101, 99, 98, 94],
            }
        )
        sig = detect_zone_revisit(df, [z], atr_value=4.0)
        self.assertIsNotNone(sig)
        self.assertEqual(sig["action"], "BREAKOUT")

    def test_revisit_reversal(self):
        z = Zone(zone_id="S1", zone_type="SUPPLY", low=100.0, high=105.0, origin_time="2026-02-20 10:30:00")
        df = pd.DataFrame(
            {
                "open": [104, 105, 104, 103, 101, 99],
                "high": [106, 107, 106, 105, 103, 101],
                "low": [102, 104, 103, 101, 100, 98],
                "close": [105, 106, 105, 103, 101, 99],
            }
        )
        sig = detect_zone_revisit(df, [z], atr_value=4.0)
        self.assertIsNotNone(sig)
        self.assertEqual(sig["action"], "REVERSAL")

    def test_no_revisit_when_zone_inactive(self):
        z = Zone(zone_id="S1", zone_type="SUPPLY", low=100.0, high=105.0, origin_time="2026-02-20 10:30:00", active=False)
        df = pd.DataFrame(
            {
                "open": [104, 105, 104, 103, 101, 99],
                "high": [106, 107, 106, 105, 103, 101],
                "low": [102, 104, 103, 101, 100, 98],
                "close": [105, 106, 105, 103, 101, 99],
            }
        )
        sig = detect_zone_revisit(df, [z], atr_value=4.0)
        self.assertIsNone(sig)


if __name__ == "__main__":
    unittest.main()
