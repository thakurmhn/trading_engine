#!/usr/bin/env python3
"""
Diagnostic tool to identify missing indicators in the scoring engine.
Run this to validate which keys are missing from the indicators dict.
"""

import sys
import logging

# Expected indicator keys by scoring function
EXPECTED_INDICATORS = {
    "_score_trend_alignment": [
        "st_bias_3m",
        "candle_15m (contains supertrend_slope)"
    ],
    "_score_rsi": [
        "rsi_prev (for slope calculation)"
    ],
    "_score_cci": [
        "candle_15m (contains cci20/cci)"
    ],
    "_score_vwap": [
        "vwap",
        "atr (for tolerance)"
    ],
    "_score_pivot": [
        "pivot_signal (passed separately, not in indicators)"
    ],
    "_score_momentum_ok": [
        "momentum_ok_call",
        "momentum_ok_put"
    ],
    "_score_cpr_width": [
        "cpr_width"
    ],
    "_score_entry_type": [
        "entry_type"
    ],
}

# Current indicators dict being built in signals.py (lines 520-530)
CURRENT_INDICATORS = {
    "atr": "present",
    "supertrend_line_3m": "present",
    "supertrend_line_15m": "present",
    "ema_fast": "present",
    "ema_slow": "present",
    "adx": "present",
    "cci": "present (but entry_logic looks for candle.cci, not indicators.cci)",
    "candle_15m": "present",
    "st_bias_3m": "present",
    "vwap": "present",
}

MISSING_INDICATORS = {
    "momentum_ok_call": "CRITICAL - returns 0 from _score_momentum_ok for CALL",
    "momentum_ok_put": "CRITICAL - returns 0 from _score_momentum_ok for PUT",
    "cpr_width": "CRITICAL - returns 0 from _score_cpr_width",
    "entry_type": "CRITICAL - returns 0 from _score_entry_type",
    "rsi_prev": "IMPORTANT - skips RSI slope bonus in _score_rsi",
}

print("=" * 80)
print("INDICATOR SCORING DIAGNOSIS — v5 Entry Logic")
print("=" * 80)

print("\n[CURRENT INDICATORS IN DICT]")
for k, v in CURRENT_INDICATORS.items():
    print(f"  [OK] {k:25} -- {v}")

print("\n[MISSING INDICATORS]")
missing_critical = 0
missing_important = 0
for k, v in MISSING_INDICATORS.items():
    label = "[CRITICAL]" if "CRITICAL" in v else "[IMPORTANT]"
    print(f"  {label:15} {k:25} -- {v}")
    if "CRITICAL" in v:
        missing_critical += 1
    else:
        missing_important += 1

print("\n[EXPECTED EFFECT ON SCORING]")
print("""
1. momentum_ok_call MISSING:
   - _score_momentum_ok(indicators, "CALL") returns 0
   - Expected: 15 pts if momentum is OK, 0 if not
   - Impact: CALLS lose 15 pts (very significant)

2. momentum_ok_put MISSING:
   - _score_momentum_ok(indicators, "PUT") returns 0
   - Expected: 15 pts if momentum is OK, 0 if not
   - Impact: PUTS lose 15 pts (very significant)

3. cpr_width MISSING:
   - _score_cpr_width() always returns 0
   - Expected: +5 pts if CPR narrow, 0 otherwise
   - Impact: Loses +5 pts bonus on trending days

4. entry_type MISSING:
   - _score_entry_type() always returns 0
   - Expected: +5 pts if PULLBACK/REJECTION, 0 otherwise
   - Impact: Loses +5 pts quality bonus

5. rsi_prev MISSING:
   - _score_rsi() skips +2 slope bonus
   - Expected: +2 pts if RSI moving in trade direction
   - Impact: Loses +2 pts slope confirmation

CUMULATIVE EFFECT:
  Best case scenario WITHOUT these indicators:
    Max score = 95 - 15 (MOM) - 5 (CPR) - 5 (ET) = 70 pts
    Threshold = 50 pts -> CAN still pass (OK)

  But this assumes ALL other scoring functions return max:
    Trend(20) + RSI(10) + CCI(15) + VWAP(10) + Pivot(15) + MOM(0) + CPR(0) + ET(0)
    = 70 pts / 50 threshold -> technically should fire

  ACTUAL PROBLEM:
    If ANY gating condition blocks a dimension (RSI hard filter, ATR, etc.),
    the score drops below threshold and entry is blocked with reason="Score too low"

    Example: side_threshold = 70 (due to counter-3m surcharge)
    But score = 65 max (without MOM/CPR/ET)
    -> Entry blocked (FAIL)

DIAGNOSIS:
  The issue is NOT just missing indicators -- it's compounded by:
  1. Missing 15-25 pts (MOM + CPR + ET)
  2. Threshold surcharges making requirements too high
  3. No debug logging to show which scorer returned 0

SOLUTION:
  Add missing indicators to the dict before calling check_entry_condition()
  This should restore 15-25 pts and allow entries to fire again.
""")

print("\n" + "=" * 80)
print("NEXT STEP: Fix signals.py indicators dict building (lines 520-540)")
print("=" * 80)
