# Phase 2 Implementation TODO

## Status: In Progress

## Completed Items:
- [x] 1. Scalp SL Tightening (ATR 0.6-0.8) - Already implemented
- [x] 2. Trend Trade Survivability Guard - Already implemented

## Remaining Items:
- [ ] 3. Bias Misalignment Filter - Add `BIAS_MISALIGN_BLOCKED` logging in `_trend_entry_quality_gate()`
- [ ] 4. Pulse Module Integration - Import and use pulse in execution.py for scalp entry signals
- [ ] 5. Dashboard Attribution - Add `ENTRY_ALLOWED_BUT_NOT_EXECUTED` logging
- [ ] 6. Dashboard Counters - Add counters for tick_bursts, scalp_trades_fired, trend_trades_confirmed, entry_allowed_not_executed, bias_misaligned_blocked

## Implementation Details:

### 3. Bias Misalignment Filter
- Location: `execution.py` - function `_trend_entry_quality_gate()`
- Add check: if bias is misaligned with allowed_side, log `BIAS_MISALIGN_BLOCKED` and return False

### 4. Pulse Module Integration  
- Location: `execution.py`
- Import: from pulse_module import get_pulse_module
- Use: Get pulse metrics in scalp entry flow to confirm burst direction

### 5. Dashboard Attribution
- Location: `execution.py`
- Add logging: When signal passes governance but fails final check (e.g., no option available), log `[ENTRY_ALLOWED_BUT_NOT_EXECUTED]`

### 6. Dashboard Counters
- Location: `dashboard.py`
- Add counters: tick_bursts_detected, scalp_trades_fired, trend_trades_confirmed, entry_allowed_not_executed, bias_misaligned_blocked

