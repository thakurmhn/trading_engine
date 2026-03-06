# Phase 2 Implementation Plan

## Objectives Overview

1. **Scalp SL Tightening**: ATR × 0.6–0.8 (currently ATR × 0.8–1.0)
2. **Trend Trade Survivability Guard**: PT/TG scaling up to ATR × 2.0 in strong ADX sessions
3. **Bias Misalignment Filter**: Reduce misaligned trades
4. **Tick-Rate Momentum Module (Pulse)**: Calculate tick inter-arrival times, tick rate, detect bursts and direction drift
5. **Dashboard Attribution Clarity**: `[ENTRY_ALLOWED_BUT_NOT_EXECUTED]` tag

---

## Implementation Details

### 1. Scalp SL Tightening (ATR × 0.6–0.8)

**Current**: `SCALP_ATR_SL_MIN_MULT = 0.80`, `SCALP_ATR_SL_MAX_MULT = 1.00`
**New**: `SCALP_ATR_SL_MIN_MULT = 0.60`, `SCALP_ATR_SL_MAX_MULT = 0.80`

**Files to modify**:
- `execution.py`: Update `SCALP_ATR_SL_MIN_MULT` and `SCALP_ATR_SL_MAX_MULT` constants

### 2. Trend Trade Survivability Guard (PT/TG Scaling)

**Current**: PT/TG based on regime (ATR × 1.2–2.0)
**New**: In strong ADX (>40), scale PT/TG to ATR × 2.0 for enhanced survivability

**Files to modify**:
- `execution.py`: Update `build_dynamic_levels()` to scale PT/TG based on ADX

### 3. Bias Misalignment Filter

**Implementation**: Add filter in `_trend_entry_quality_gate()` to reduce trades when day bias is misaligned with signal direction

**Files to modify**:
- `execution.py`: Add bias alignment check in entry quality gate

### 4. Tick-Rate Momentum Module (Pulse)

**New file**: `pulse_module.py`
- Calculate tick inter-arrival times (ms)
- Derive tick rate (ticks/sec)
- Detect bursts (tick_rate > threshold)
- Detect direction drift (UP/DOWN/NEUTRAL based on price movement)
- Tag signals: `[MOMENTUM_TICK_RATE][UP/DOWN]`

**Integration**:
- `data_feed.py`: Integrate Pulse calculation in tick processing
- `execution.py`: Use Pulse for scalp entry signals

### 5. Dashboard Attribution Clarity

**Implementation**: Add `[ENTRY_ALLOWED_BUT_NOT_EXECUTED]` tag when trades pass governance but fail final signal check

**Files to modify**:
- `execution.py`: Add logging for entry allowed but not executed scenarios

---

## Files to Modify

1. **execution.py**
   - Update SCALP_ATR_SL constants (0.6-0.8)
   - Update build_dynamic_levels() for PT/TG scaling
   - Add bias misalignment filter
   - Add `[ENTRY_ALLOWED_BUT_NOT_EXECUTED]` logging

2. **data_feed.py**
   - Integrate Pulse module

3. **pulse_module.py** (NEW)
   - Tick-rate momentum calculation
   - Burst detection
   - Direction drift detection

---

## Dashboard Counters to Add

- `tick_bursts_detected`: Number of tick bursts detected
- `scalp_trades_fired`: Number of scalp trades executed
- `trend_trades_confirmed`: Number of trend trades confirmed
- `entry_allowed_not_executed`: Trades passing governance but not executed
- `bias_misaligned_blocked`: Bias misalignment blocks

---

## Validation Requirements

1. Smaller average losses (via tighter SL)
2. Longer survivability (via PT/TG scaling in strong ADX)
3. Positive net P&L (via bias alignment filter)

