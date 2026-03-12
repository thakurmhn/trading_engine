# Phase 2: Profitability Fixes Implementation
**Date**: 2026-03-12  
**Status**: Ready for Deployment  
**Scope**: 3 Critical Fixes + Validation

---

## Overview

Phase 2 addresses three critical profitability issues identified in the S4/R4 validation:

1. **Duplicate Trade Prevention** - Same entry logged multiple times
2. **ATR-Based Dynamic Stops** - Tighter stops for better payoff ratio
3. **PUT Scoring Parity** - Ensure PUT and CALL scoring is symmetric

---

## Fix 1: Duplicate Trade Prevention

### Problem
Same trade (symbol, entry_price, candle) logged multiple times, inflating trade count and metrics.

### Solution
Track (symbol, candle_time, entry_price) tuples. Block if already logged this candle.

### Code Changes

**File**: `execution.py`

**Location**: Line ~80 (DUPLICATE TRADE PREVENTION section)

```python
# BEFORE:
_logged_trades = set()  # Global deduplication tracker

# AFTER:
_logged_trades = set()  # Global deduplication tracker
_duplicate_blocks = 0   # Counter for audit trail
```

**Location**: Paper order scalp entry (~line 3200)

```python
# BEFORE:
trade_key = (opt_name, ct, entry_price)
if trade_key in _logged_trades:
    logging.warning(f"[DUPLICATE BLOCKED] {trade_key}")
    return
_logged_trades.add(trade_key)

# AFTER:
trade_key = (opt_name, last_candle_time, round(entry_price, 2))
if trade_key in _logged_trades:
    global _duplicate_blocks
    _duplicate_blocks += 1
    logging.warning(
        f"[DUPLICATE BLOCKED] {opt_name} @ {entry_price:.2f} "
        f"candle={last_candle_time} reason=Same entry already logged this candle"
    )
    return
_logged_trades.add(trade_key)
```

**Location**: Paper order trend entry (~line 3600)

```python
# BEFORE:
trade_key = (opt_name, ct, entry_price)
if trade_key in _logged_trades:
    logging.warning(f"[DUPLICATE BLOCKED] {trade_key}")
    return
_logged_trades.add(trade_key)

# AFTER:
trade_key = (opt_name, last_candle_time, round(entry_price, 2))
if trade_key in _logged_trades:
    global _duplicate_blocks
    _duplicate_blocks += 1
    logging.warning(
        f"[DUPLICATE BLOCKED] {opt_name} @ {entry_price:.2f} "
        f"candle={last_candle_time} reason=Same entry already logged this candle"
    )
    return
_logged_trades.add(trade_key)
```

### Validation
```bash
grep "DUPLICATE BLOCKED" options_trade_engine_*.log | wc -l
# Should show count of blocked duplicates (0 is ideal)
```

---

## Fix 2: ATR-Based Dynamic Stops (Tighter)

### Problem
Current stops are too wide, resulting in:
- Payoff ratio < 1.0 (losing money on average)
- Premature exits on normal volatility
- Profit factor 1.12 (target: 1.45+)

### Solution
Tighten ATR multipliers by 5-10% across all ADX tiers.

### Code Changes

**File**: `execution.py`

**Location**: `build_dynamic_levels()` function (~line 1850)

```python
# BEFORE:
if adx_val_f > 40:
    sl_mult  = 1.3
    sl_tier  = "ADX_STRONG_40"
elif adx_val_f > 0 and adx_val_f < 20:
    sl_mult  = 0.75
    sl_tier  = "ADX_WEAK_20"
else:
    sl_mult  = 1.0
    sl_tier  = "ADX_DEFAULT"

# AFTER:
if adx_val_f > 40:
    sl_mult  = 1.2  # 1.3 → 1.2 (tighter for strong trends)
    sl_tier  = "ADX_STRONG_40"
elif adx_val_f > 0 and adx_val_f < 20:
    sl_mult  = 0.7  # 0.75 → 0.7 (tighter in choppy)
    sl_tier  = "ADX_WEAK_20"
else:
    sl_mult  = 0.95  # 1.0 → 0.95 (tighter default)
    sl_tier  = "ADX_DEFAULT"
```

**Location**: ATR expansion logic (~line 1880)

```python
# BEFORE:
if _atr_sl_ma > 0 and atr > 1.5 * _atr_sl_ma:
    _atr_sl_expand = 1.5
    _sl_atr_tier   = "ATR_HIGH"
elif _atr_sl_ma > 0 and atr > 1.2 * _atr_sl_ma:
    _atr_sl_expand = 1.2
    _sl_atr_tier   = "ATR_ELEVATED"
sl_mult = min(round(sl_mult * _atr_sl_expand, 3), 1.8)

# AFTER:
if _atr_sl_ma > 0 and atr > 1.5 * _atr_sl_ma:
    _atr_sl_expand = 1.4  # 1.5 → 1.4 (slightly tighter)
    _sl_atr_tier   = "ATR_HIGH"
elif _atr_sl_ma > 0 and atr > 1.3 * _atr_sl_ma:
    _atr_sl_expand = 1.15  # 1.2 → 1.15 (tighter)
    _sl_atr_tier   = "ATR_ELEVATED"
sl_mult = min(round(sl_mult * _atr_sl_expand, 3), 1.7)  # 1.8 → 1.7 (cap tighter)
```

**Location**: PT/TG multipliers (~line 1920)

```python
# BEFORE:
if atr <= 60:
    regime   = "VERY_LOW"
    pt_mult  = 1.7
    tg_mult  = 2.3
elif atr <= 100:
    regime   = "LOW"
    pt_mult  = 2.0
    tg_mult  = 2.8
elif atr <= 150:
    regime   = "MODERATE"
    pt_mult  = 2.2
    tg_mult  = 3.2
elif atr <= 250:
    regime   = "HIGH"
    pt_mult  = 2.7
    tg_mult  = 3.8

# AFTER:
if atr <= 60:
    regime   = "VERY_LOW"
    pt_mult  = 1.8  # 1.7 → 1.8 (wider targets)
    tg_mult  = 2.5  # 2.3 → 2.5
elif atr <= 100:
    regime   = "LOW"
    pt_mult  = 2.1  # 2.0 → 2.1
    tg_mult  = 2.9  # 2.8 → 2.9
elif atr <= 150:
    regime   = "MODERATE"
    pt_mult  = 2.3  # 2.2 → 2.3
    tg_mult  = 3.3  # 3.2 → 3.3
elif atr <= 250:
    regime   = "HIGH"
    pt_mult  = 2.8  # 2.7 → 2.8
    tg_mult  = 3.9  # 3.8 → 3.9
```

### Validation
```bash
# Check SL distances in logs
grep "LEVELS\[ATR_SL\]" options_trade_engine_*.log | head -5
# Should show tighter SL multipliers (1.2, 0.95, etc.)

# Verify payoff ratio improved
grep "Payoff Ratio" reports/dashboard_report_*.txt | tail -1
# Should be > 1.0 (target: 1.05+)
```

---

## Fix 3: PUT Scoring Parity

### Problem
PUT and CALL scoring is asymmetric:
- RSI thresholds: CALL >55, PUT <45 (asymmetric)
- CCI thresholds: CALL >100, PUT <-100 (symmetric but different ranges)
- Trend alignment: Different weighting for PUT vs CALL

### Solution
Ensure symmetric scoring across all dimensions.

### Code Changes

**File**: `entry_logic.py`

**Location**: `_score_rsi()` function (~line 280)

```python
# BEFORE:
if side == "CALL":
    base = w         if rsi > 55 else (w // 2 if rsi > 50 else 0)
else:
    base = w         if rsi < 45 else (w // 2 if rsi < 50 else 0)

# AFTER:
if side == "CALL":
    base = w         if rsi > 55 else (w // 2 if rsi > 50 else 0)
else:
    base = w         if rsi < 45 else (w // 2 if rsi < 50 else 0)
# Already symmetric - no change needed
```

**Location**: `_score_trend_alignment()` function (~line 200)

```python
# BEFORE:
if side == "CALL":
    if b15 == "BULLISH" and b3 == "BULLISH":  return w
    if b15 == "BULLISH" and b3 == "NEUTRAL":   return w * 3 // 4
    if b15 == "BULLISH":                        return w // 2
    if b15 == "NEUTRAL" and b3 == "BULLISH":    return w // 4
    if b15 == "BEARISH":
        try:
            c15 = indicators.get("candle_15m")
            if c15 is not None:
                sl = str(c15.get("supertrend_slope", "")).upper()
                if sl == "UP": return w // 4
        except Exception:
            pass
    return 0
else:  # PUT
    if b15 == "BEARISH" and b3 == "BEARISH":  return w
    if b15 == "BEARISH" and b3 == "NEUTRAL":   return w * 3 // 4
    if b15 == "BEARISH":                        return w // 2
    if b15 == "NEUTRAL" and b3 == "BEARISH":    return w // 4
    if b15 == "BULLISH":
        try:
            c15 = indicators.get("candle_15m")
            if c15 is not None:
                sl = str(c15.get("supertrend_slope", "")).upper()
                if sl == "DOWN": return w // 4
        except Exception:
            pass
    return 0

# AFTER:
# Already symmetric - no change needed
# Both CALL and PUT follow same logic with opposite bias checks
```

**Location**: `_score_cci()` function (~line 320)

```python
# BEFORE:
if side == "CALL":
    if   cci3 >= 150:  score = 15
    elif cci3 >= 100:  score = 10
    elif cci3 >=  60:  score = 3
    else:              score = 0
else:  # PUT
    if   cci3 <= -150: score = 15
    elif cci3 <= -100: score = 10
    elif cci3 <=  -60: score = 3
    else:              score = 0

# AFTER:
# Already symmetric - no change needed
# Both CALL and PUT follow same logic with opposite thresholds
```

### Validation
```bash
# Check scoring symmetry in logs
grep "SCORE BREAKDOWN" options_trade_engine_*.log | grep "CALL" | head -3
grep "SCORE BREAKDOWN" options_trade_engine_*.log | grep "PUT" | head -3
# Should show similar score distributions for both sides
```

---

## Implementation Checklist

### Pre-Implementation
- [ ] Backup current code: `git checkout -b phase2-profitability-fixes`
- [ ] Verify current metrics: `grep "Profit Factor" reports/dashboard_report_*.txt | tail -1`
- [ ] Note baseline: Profit Factor, Payoff Ratio, Win Rate

### Implementation
- [ ] Apply Fix 1: Duplicate Prevention
- [ ] Apply Fix 2: ATR-Based Stops
- [ ] Apply Fix 3: PUT Scoring Parity (verify already symmetric)
- [ ] Syntax check: `python -m py_compile execution.py entry_logic.py`

### Validation
- [ ] Replay 2026-03-11: `python main.py --mode REPLAY --date 2026-03-11`
- [ ] Check logs for DUPLICATE BLOCKED count
- [ ] Verify SL multipliers tighter
- [ ] Compare metrics:
  - Profit Factor: should improve 1.12 → 1.25+
  - Payoff Ratio: should improve 0.86 → 1.0+
  - Win Rate: should remain stable 50-60%

### Post-Implementation
- [ ] Run multi-day replay (5 days)
- [ ] Verify no regressions
- [ ] Commit: `git commit -m "Phase 2: Profitability fixes - duplicate prevention, tighter stops, PUT parity"`
- [ ] Document results in PHASE2_RESULTS.md

---

## Expected Results

### Profit Factor
- **Before**: 1.12
- **After**: 1.25-1.35
- **Improvement**: +11-20%

### Payoff Ratio
- **Before**: 0.86
- **After**: 1.0-1.1
- **Improvement**: +16-28%

### Win Rate
- **Before**: 50-60%
- **After**: 50-60% (stable)
- **Change**: Minimal (expected)

### Trade Count
- **Before**: 62 trades (with duplicates)
- **After**: 50-55 trades (duplicates removed)
- **Reduction**: 10-15% (expected)

---

## Rollback Plan

If metrics degrade:

```bash
# Revert to baseline
git checkout HEAD -- execution.py entry_logic.py

# Verify revert
python -m py_compile execution.py entry_logic.py

# Re-run replay
python main.py --mode REPLAY --date 2026-03-11
```

---

## Next Steps

After Phase 2 validation:
1. **Phase 3**: Pivot Engine Integration (1 week)
2. **Phase 4**: Live Deployment (1 week)

---

**Status**: Ready for Implementation  
**Estimated Time**: 2-4 hours  
**Risk Level**: Low (targeted fixes, no architectural changes)
