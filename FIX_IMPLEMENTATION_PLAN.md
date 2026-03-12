# Fix Implementation Plan - 4 Critical Issues

## Issue 1: Reversal Detector Too Aggressive on Trending Days
**Problem**: REVERSAL_EXIT fires on normal pullbacks during trends, exiting winners prematurely.
**Root Cause**: No ADX check before reversal exit; reversal detector treats all oscillator extremes equally.
**Solution**: Add ADX threshold gate to reversal exit logic.

**File**: `option_exit_manager.py`
**Changes**:
- Modify `_check_composite_exit_score()` to suppress reversal exits when ADX > 30 (strong trend)
- Add ADX-aware reversal suppression: if ADX >= 35, require 2x momentum collapse (80% vs 60%)
- Log suppression reason for audit trail

---

## Issue 2: Composite Score Exit Too Sensitive
**Problem**: Exiting winners after 1 bar (trades #1, #5, #6 on 2026-03-09).
**Root Cause**: Dynamic threshold too low on trending days; no day-type awareness.
**Solution**: Increase threshold on TRENDING days; add bars_held guard.

**File**: `option_exit_manager.py`
**Changes**:
- Add `bars_held` parameter to `_check_composite_exit_score()`
- Suppress exit if bars_held < 3 (minimum hold before composite exit)
- On TRENDING days: raise threshold by +15 pts (from 45 to 60)
- Log threshold adjustment for audit

---

## Issue 3: Stop Loss Placement Too Tight
**Problem**: Trade #4 lost -52.50 pts on first bar (SL_HIT).
**Root Cause**: Fixed stop loss (10-12 pts) doesn't scale with ATR on trending days.
**Solution**: Use ATR-based dynamic stops; widen on trending days.

**File**: `position_manager.py` (or wherever SL is set)
**Changes**:
- Change SL from fixed 10-12 pts to 1.5x ATR on TRENDING days
- On RANGE days: keep 1.0x ATR
- On NORMAL days: 1.2x ATR
- Log SL calculation for audit

---

## Issue 4: Signal Generation Suppressed (Only 15 Entry OK vs 273 Blocked)
**Problem**: 108 blocked by ST_CONFLICT, 47 by WEAK_ADX on trending day.
**Root Cause**: ST_CONFLICT filter too strict; WEAK_ADX gate doesn't account for trend confirmation.
**Solution**: Relax ST_CONFLICT on strong trends; bypass WEAK_ADX when ADX confirms.

**File**: `entry_logic.py` (in `check_entry_condition()`)
**Changes**:
- Add ST_CONFLICT_OVERRIDE when ADX >= 35 (strong trend overrides ST disagreement)
- Bypass WEAK_ADX gate when ADX >= 25 AND price is trending (ST aligned)
- Log override reason for audit

---

## Implementation Order
1. **Fix #4 first** (signal generation) - enables more entries
2. **Fix #1 next** (reversal detector) - prevents premature exits
3. **Fix #2 then** (composite score) - tightens exit timing
4. **Fix #3 last** (stop loss) - protects capital

---

## Validation Strategy
- Test on 2026-03-09 replay (TRENDING day with ADX=50+)
- Expected: 15+ Entry OK signals (vs 15 currently)
- Expected: Win rate 50%+ (vs 37.5% currently)
- Expected: Avg hold 5+ bars (vs 1-3 bars currently)

---

## Rollback Plan
- Each fix is isolated to one function
- Can disable individually via config flags
- No breaking changes to existing APIs
