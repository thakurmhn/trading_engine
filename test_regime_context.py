"""Unit tests for regime_context.py — RegimeContext frozen dataclass and builder."""

import math
import unittest

from regime_context import (
    RegimeContext,
    compute_regime_context,
    compute_scalp_regime_context,
    log_regime_context,
    classify_atr_regime,
    classify_adx_tier,
)


class TestClassifyATRRegime(unittest.TestCase):

    def test_zero(self):
        self.assertEqual(classify_atr_regime(0), "ATR_UNKNOWN")

    def test_nan(self):
        self.assertEqual(classify_atr_regime(float("nan")), "ATR_UNKNOWN")

    def test_negative(self):
        self.assertEqual(classify_atr_regime(-10), "ATR_UNKNOWN")

    def test_very_low(self):
        self.assertEqual(classify_atr_regime(50), "VERY_LOW")
        self.assertEqual(classify_atr_regime(60), "VERY_LOW")

    def test_low(self):
        self.assertEqual(classify_atr_regime(61), "LOW")
        self.assertEqual(classify_atr_regime(100), "LOW")

    def test_moderate(self):
        self.assertEqual(classify_atr_regime(101), "MODERATE")
        self.assertEqual(classify_atr_regime(150), "MODERATE")

    def test_high(self):
        self.assertEqual(classify_atr_regime(200), "HIGH")
        self.assertEqual(classify_atr_regime(250), "HIGH")

    def test_extreme(self):
        self.assertEqual(classify_atr_regime(300), "EXTREME")


class TestClassifyADXTier(unittest.TestCase):

    def test_zero(self):
        self.assertEqual(classify_adx_tier(0), "ADX_DEFAULT")

    def test_nan(self):
        self.assertEqual(classify_adx_tier(float("nan")), "ADX_DEFAULT")

    def test_weak(self):
        self.assertEqual(classify_adx_tier(15), "ADX_WEAK_20")
        self.assertEqual(classify_adx_tier(19.9), "ADX_WEAK_20")

    def test_default(self):
        self.assertEqual(classify_adx_tier(20), "ADX_DEFAULT")
        self.assertEqual(classify_adx_tier(30), "ADX_DEFAULT")
        self.assertEqual(classify_adx_tier(40), "ADX_DEFAULT")

    def test_strong(self):
        self.assertEqual(classify_adx_tier(41), "ADX_STRONG_40")
        self.assertEqual(classify_adx_tier(60), "ADX_STRONG_40")


class TestRegimeContextFrozen(unittest.TestCase):

    def test_default_creation(self):
        rc = RegimeContext()
        self.assertEqual(rc.atr_regime, "ATR_UNKNOWN")
        self.assertEqual(rc.adx_tier, "ADX_DEFAULT")
        self.assertEqual(rc.day_type, "UNKNOWN")
        self.assertFalse(rc.st_aligned)

    def test_frozen_prevents_mutation(self):
        rc = RegimeContext(atr_value=120.0)
        with self.assertRaises(AttributeError):
            rc.atr_value = 999.0

    def test_regime_label(self):
        rc = RegimeContext(
            atr_regime="MODERATE",
            adx_tier="ADX_STRONG_40",
            day_type="TREND_DAY",
            cpr_width="NARROW",
        )
        self.assertEqual(rc.regime_label, "MODERATE|ADX_STRONG_40|TREND_DAY|NARROW")

    def test_has_reversal(self):
        rc1 = RegimeContext()
        self.assertFalse(rc1.has_reversal)
        rc2 = RegimeContext(reversal_signal={"side": "CALL", "score": 80})
        self.assertTrue(rc2.has_reversal)

    def test_has_failed_breakout(self):
        rc1 = RegimeContext()
        self.assertFalse(rc1.has_failed_breakout)
        rc2 = RegimeContext(failed_breakout_signal={"side": "PUT", "pivot": "R3"})
        self.assertTrue(rc2.has_failed_breakout)

    def test_has_zone_signal(self):
        rc = RegimeContext(zone_signal={"zone_type": "DEMAND", "action": "REVERSAL"})
        self.assertTrue(rc.has_zone_signal)

    def test_pulse_active(self):
        rc1 = RegimeContext(pulse_burst_flag=True, pulse_direction="UP")
        self.assertTrue(rc1.pulse_active)
        rc2 = RegimeContext(pulse_burst_flag=True, pulse_direction="NEUTRAL")
        self.assertFalse(rc2.pulse_active)
        rc3 = RegimeContext(pulse_burst_flag=False, pulse_direction="UP")
        self.assertFalse(rc3.pulse_active)


class TestRegimeContextExport(unittest.TestCase):

    def setUp(self):
        self.rc = RegimeContext(
            atr_value=120.0,
            atr_regime="MODERATE",
            adx_value=28.0,
            adx_tier="ADX_DEFAULT",
            day_type="TREND_DAY",
            cpr_width="NARROW",
            open_bias="OPEN_HIGH",
            osc_context="ZoneC-Continuation",
            osc_rsi_range=(25.0, 75.0),
            osc_cci_range=(-180.0, 180.0),
            ema_stretch_tagged=True,
            ema_stretch_mult=2.1,
            failed_breakout_signal={"side": "PUT"},
            gap_tag="GAP_UP",
            compression_state="ENERGY_BUILDUP",
            bar_timestamp="2026-03-06 10:30:00",
            symbol="NSE:NIFTY50-INDEX",
        )

    def test_to_state_keys_backward_compat(self):
        sk = self.rc.to_state_keys()
        self.assertEqual(sk["regime_context"], "MODERATE")
        self.assertEqual(sk["atr_value"], 120.0)
        self.assertEqual(sk["osc_context"], "ZoneC-Continuation")
        self.assertEqual(sk["day_type"], "TREND_DAY")
        self.assertEqual(sk["open_bias"], "OPEN_HIGH")
        self.assertTrue(sk["failed_breakout"])
        self.assertTrue(sk["ema_stretch"])
        self.assertAlmostEqual(sk["ema_stretch_mult"], 2.1)

    def test_to_state_keys_new_fields(self):
        sk = self.rc.to_state_keys()
        self.assertIs(sk["entry_regime_context"], self.rc)
        self.assertEqual(sk["adx_tier"], "ADX_DEFAULT")
        self.assertEqual(sk["cpr_width_at_entry"], "NARROW")
        self.assertEqual(sk["gap_tag"], "GAP_UP")
        self.assertEqual(sk["compression_state_at_entry"], "ENERGY_BUILDUP")
        self.assertAlmostEqual(sk["osc_rsi_call"], 75.0)
        self.assertAlmostEqual(sk["osc_rsi_put"], 25.0)
        self.assertAlmostEqual(sk["osc_cci_call"], 180.0)
        self.assertAlmostEqual(sk["osc_cci_put"], -180.0)

    def test_to_log_tag_contains_key_fields(self):
        tag = self.rc.to_log_tag()
        self.assertIn("ATR=120.0(MODERATE)", tag)
        self.assertIn("ADX=28.0(ADX_DEFAULT)", tag)
        self.assertIn("day=TREND_DAY", tag)
        self.assertIn("cpr=NARROW", tag)
        self.assertIn("osc=ZoneC-Continuation", tag)
        self.assertIn("fb=PUT", tag)
        self.assertIn("comp=ENERGY_BUILDUP", tag)

    def test_to_dict_serializable(self):
        d = self.rc.to_dict()
        self.assertIsInstance(d, dict)
        self.assertEqual(d["atr_regime"], "MODERATE")
        self.assertEqual(d["day_type"], "TREND_DAY")
        # Should not contain non-serializable objects
        for k, v in d.items():
            self.assertNotIsInstance(v, RegimeContext)


class TestComputeRegimeContext(unittest.TestCase):

    def _make_st_details(self, **overrides):
        base = {
            "ST3m_bias": "BULLISH",
            "ST15m_bias": "BULLISH",
            "ST3m_slope": "UP",
            "ST15m_slope": "UP",
            "adx14": 28.0,
            "rsi14": 55.0,
            "cci20": 120.0,
            "cpr_width": "NARROW",
            "osc_context": "ZoneC-Continuation",
            "osc_zone": "ZoneC",
            "day_type_tag": "TREND_DAY",
            "open_bias": "OPEN_HIGH",
            "bias": "Bullish",
            "gap_tag": "NO_GAP",
            "bias_aligned": True,
            "ema_stretch_mult": 1.5,
            "ema_stretch_tagged": False,
            "osc_relief_override": False,
            "osc_trend_override": False,
            "atr_expand_tier": "ATR_DEFAULT",
            "eff_rsi_range": [25.0, 75.0],
            "eff_cci_range": [-180.0, 180.0],
        }
        base.update(overrides)
        return base

    def test_basic_build(self):
        rc = compute_regime_context(
            st_details=self._make_st_details(),
            atr=120.0,
        )
        self.assertIsInstance(rc, RegimeContext)
        self.assertEqual(rc.atr_regime, "MODERATE")
        self.assertEqual(rc.adx_tier, "ADX_DEFAULT")
        self.assertEqual(rc.day_type, "TREND_DAY")
        self.assertTrue(rc.st_aligned)
        self.assertEqual(rc.st_bias_3m, "BULLISH")
        self.assertAlmostEqual(rc.rsi14, 55.0)

    def test_adx_tiers(self):
        rc_weak = compute_regime_context(
            st_details=self._make_st_details(adx14=15.0), atr=80.0,
        )
        self.assertEqual(rc_weak.adx_tier, "ADX_WEAK_20")

        rc_strong = compute_regime_context(
            st_details=self._make_st_details(adx14=45.0), atr=80.0,
        )
        self.assertEqual(rc_strong.adx_tier, "ADX_STRONG_40")

    def test_atr_regimes(self):
        for atr, expected in [(50, "VERY_LOW"), (80, "LOW"), (120, "MODERATE"), (200, "HIGH"), (300, "EXTREME")]:
            rc = compute_regime_context(st_details=self._make_st_details(), atr=atr)
            self.assertEqual(rc.atr_regime, expected, f"ATR={atr}")

    def test_detector_signals_attached(self):
        rev = {"side": "CALL", "score": 80}
        fb = {"side": "PUT", "pivot": "R3"}
        zone = {"zone_type": "DEMAND", "action": "REVERSAL"}
        rc = compute_regime_context(
            st_details=self._make_st_details(),
            atr=100.0,
            reversal_signal=rev,
            failed_breakout_signal=fb,
            zone_signal=zone,
        )
        self.assertTrue(rc.has_reversal)
        self.assertEqual(rc.reversal_signal["side"], "CALL")
        self.assertTrue(rc.has_failed_breakout)
        self.assertTrue(rc.has_zone_signal)

    def test_pulse_metrics(self):
        rc = compute_regime_context(
            st_details=self._make_st_details(),
            atr=100.0,
            pulse_tick_rate=20.0,
            pulse_burst_flag=True,
            pulse_direction="UP",
        )
        self.assertTrue(rc.pulse_active)
        self.assertAlmostEqual(rc.pulse_tick_rate, 20.0)

    def test_compression_state(self):
        rc = compute_regime_context(
            st_details=self._make_st_details(),
            atr=100.0,
            compression_state_str="ENERGY_BUILDUP",
        )
        self.assertEqual(rc.compression_state, "ENERGY_BUILDUP")

    def test_empty_st_details_safe(self):
        rc = compute_regime_context(st_details={}, atr=80.0)
        self.assertEqual(rc.atr_regime, "LOW")
        self.assertEqual(rc.adx_tier, "ADX_DEFAULT")
        self.assertEqual(rc.day_type, "UNKNOWN")

    def test_none_st_details_safe(self):
        rc = compute_regime_context(st_details=None, atr=80.0)
        self.assertEqual(rc.atr_regime, "LOW")

    def test_st_alignment_detection(self):
        rc_aligned = compute_regime_context(
            st_details=self._make_st_details(ST3m_bias="BEARISH", ST15m_bias="BEARISH"),
            atr=80.0,
        )
        self.assertTrue(rc_aligned.st_aligned)

        rc_misaligned = compute_regime_context(
            st_details=self._make_st_details(ST3m_bias="BULLISH", ST15m_bias="BEARISH"),
            atr=80.0,
        )
        self.assertFalse(rc_misaligned.st_aligned)

    def test_osc_range_extraction(self):
        rc = compute_regime_context(
            st_details=self._make_st_details(
                eff_rsi_range=[20.0, 80.0],
                eff_cci_range=[-200.0, 200.0],
            ),
            atr=100.0,
        )
        self.assertEqual(rc.osc_rsi_range, (20.0, 80.0))
        self.assertEqual(rc.osc_cci_range, (-200.0, 200.0))


class TestComputeScalpRegimeContext(unittest.TestCase):

    def test_basic_scalp(self):
        rc = compute_scalp_regime_context(
            atr=80.0,
            adx=25.0,
            pulse_tick_rate=20.0,
            pulse_burst_flag=True,
            pulse_direction="UP",
        )
        self.assertEqual(rc.atr_regime, "LOW")
        self.assertEqual(rc.adx_tier, "ADX_DEFAULT")
        self.assertTrue(rc.pulse_active)
        # Scalp RC should have defaults for non-gate fields
        self.assertEqual(rc.day_type, "UNKNOWN")
        self.assertEqual(rc.st_bias_3m, "NEUTRAL")


class TestLogRegimeContext(unittest.TestCase):

    def test_log_emits_tag(self):
        rc = RegimeContext(
            atr_value=120.0, atr_regime="MODERATE",
            adx_value=28.0, adx_tier="ADX_DEFAULT",
            day_type="TREND_DAY",
            bar_timestamp="2026-03-06 10:30:00",
            symbol="NIFTY",
        )
        import logging
        with self.assertLogs("root", level="INFO") as cm:
            log_regime_context(rc)
        self.assertTrue(any("[REGIME_CONTEXT]" in line for line in cm.output))
        self.assertTrue(any("MODERATE" in line for line in cm.output))


if __name__ == "__main__":
    unittest.main()
