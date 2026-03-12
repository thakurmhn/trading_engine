# Replay Validation Agent - Framework Documentation

**Date**: 2026-03-12  
**Status**: READY FOR DEPLOYMENT  
**Purpose**: Validate Pivot Reaction Engine + Liquidity Event Detection on historical data

---

## OVERVIEW

The Replay Validation Agent is a comprehensive testing framework that validates two critical trading system modules on historical tick data:

1. **Pivot Reaction Engine** - Mandatory pivot evaluation on every candle close
2. **Liquidity Event Detection Engine** - Sweep/trap detection at structural levels

The agent processes historical tick data sequentially (no lookahead bias), confirms pivot interactions and liquidity events are detected correctly, validates trade signals reference pivot context, and generates detailed validation reports.

---

## VALIDATION OBJECTIVES

### Objective 1: Pivot Levels Evaluated on Every Candle Close
- **Requirement**: Every candle must have pivot evaluation completed
- **Validation**: Count candles processed vs pivot interactions detected
- **Success Criteria**: Coverage >= 99%

### Objective 2: Pivot Interactions Correctly Classified
- **Requirement**: Interactions must be classified as touch, rejection, acceptance, breakout, breakdown, etc.
- **Validation**: Verify classification logic matches market structure
- **Success Criteria**: All interaction types detected and logged

### Objective 3: Liquidity Sweeps Detected at Structural Levels
- **Requirement**: Liquidity events must be detected when price sweeps through pivot levels
- **Validation**: Count sweep events and verify they occur near pivots
- **Success Criteria**: Sweeps detected > 0, correlation with pivots confirmed

### Objective 4: Pivot Clusters Recognized
- **Requirement**: Multiple pivots in close proximity must be grouped as clusters
- **Validation**: Verify cluster detection algorithm works correctly
- **Success Criteria**: Cluster events logged and counted

### Objective 5: Trade Signals Reference Pivot and Liquidity Context
- **Requirement**: Every trade signal must include pivot/liquidity context
- **Validation**: Verify signal reason strings include pivot keywords
- **Success Criteria**: Signal confirmation rate > 90%

### Objective 6: No Trade Signal Bypasses Pivot Validation
- **Requirement**: No trade may execute without pivot validation
- **Validation**: Verify all signals pass through pivot_engine.validate_trade_signal()
- **Success Criteria**: Zero bypass events detected

---

## VALIDATION PROCEDURE

### Step 1: Load Replay Data
```
Input: Historical tick databases (C:\SQLite\ticks\ticks_*.db)
Process:
  - Locate all tick database files
  - Sort chronologically
  - Select most recent 14 days
Output: List of tick files ready for processing
```

### Step 2: Initialize Session Context
```
Input: Tick files
Process:
  - Calculate pivot levels (CPR, Traditional, Camarilla)
  - Initialize Pivot Reaction Engine
  - Initialize Liquidity Event Detection Engine
Output: Engines ready for candle processing
```

### Step 3: Candle Close Processing
```
For each candle:
  1. Run Pivot Reaction Engine
     - Evaluate candle interaction with ALL pivot levels
     - Classify interaction type
     - Detect pivot clusters
     - Record metrics
  
  2. Run Liquidity Event Detection
     - Check for sweep patterns
     - Check for trap patterns
     - Verify correlation with pivots
     - Record metrics
  
  3. Log results
     - [PIVOT_INTERACTION] logs
     - [LIQ_SWEEP] logs
     - [LIQ_TRAP] logs
```

### Step 4: Liquidity Sweep Detection
```
For each candle:
  - Detect LIQ_SWEEP_HIGH (price sweeps above resistance)
  - Detect LIQ_SWEEP_LOW (price sweeps below support)
  - Detect STOP_HUNT (extreme wicks)
  - Detect FALSE_BREAKOUT (break + reversal)
  - Detect BULL_TRAP (false breakout up)
  - Detect BEAR_TRAP (false breakdown down)
  
  Verify: Each event references nearby pivot level
```

### Step 5: Signal Validation
```
For each trade signal:
  1. Confirm pivot interaction detected
  2. Confirm liquidity context evaluated
  3. Confirm signal direction aligns with pivot reaction
  4. If any check fails: BLOCK signal and record failure
  
  Success: Signal executed with full pivot/liquidity context
```

---

## VALIDATION METRICS

### Pivot Metrics
- `total_candles_processed` - Total candles evaluated
- `pivot_levels_checked` - Total pivot levels evaluated
- `pivot_interactions_detected` - Interactions found
- `pivot_rejections` - Rejection events
- `pivot_acceptances` - Acceptance events
- `pivot_breakouts` - Breakout events
- `pivot_breakdowns` - Breakdown events
- `pivot_clusters_detected` - Cluster events

### Liquidity Metrics
- `liquidity_events_detected` - Total sweep/trap events
- `sweeps_at_pivots` - Sweeps near pivot levels
- `false_breakouts_detected` - Failed breakout events
- `trap_events_detected` - Bull/bear trap events

### Signal Integrity Metrics
- `signals_generated` - Total signals fired
- `signals_with_pivot_confirmation` - Signals with pivot context
- `signals_with_liquidity_confirmation` - Signals with liquidity context
- `signals_blocked_due_to_trap` - Signals blocked by trap detection

---

## FAILURE CONDITIONS

The validation FAILS if any of these conditions occur:

1. **Pivot Coverage < 99%**
   - Indicates: Pivot evaluation not running on every candle
   - Action: Investigate pivot_engine.evaluate_candle() calls

2. **Pivot Interaction Not Classified**
   - Indicates: Classification logic broken
   - Action: Review _classify_interaction() method

3. **Liquidity Sweep Not Detected**
   - Indicates: Sweep detection algorithm failed
   - Action: Review sweep detection thresholds

4. **Trade Signal Bypasses Pivot Validation**
   - Indicates: Signal executed without pivot_engine.validate_trade_signal()
   - Action: Verify execution layer calls validation

5. **Trade Signal Conflicts with Liquidity Trap**
   - Indicates: Signal allowed despite trap detection
   - Action: Review trap blocking logic

6. **Failure Count > Acceptable Threshold**
   - Indicates: Multiple validation failures
   - Action: Investigate root causes

---

## SUCCESS CRITERIA

Replay validation PASSES when ALL of these are true:

✓ **Pivot Evaluation Coverage >= 99%**
  - At least 99% of candles have pivot evaluation completed

✓ **Liquidity Sweeps Detected at Structural Levels**
  - Sweep events detected > 0
  - Sweeps correlate with pivot levels

✓ **No Trade Signal Bypasses Pivot Validation**
  - Zero bypass events in failure log
  - All signals reference pivot context

✓ **Signal Confirmation Rate > 90%**
  - At least 90% of signals have pivot/liquidity confirmation

✓ **Failure Events Below Threshold**
  - Fewer than 5 critical failures
  - No repeated failure patterns

---

## RUNNING THE VALIDATION

### Command Line
```bash
python replay_validation_agent.py
```

### Output Files
- `replay_validation_agent.log` - Detailed execution log
- `replay_validation_report.json` - Structured validation report

### Expected Output
```
================================================================================
REPLAY VALIDATION REPORT
================================================================================
Status: PASSED
Timestamp: 2026-03-12T10:30:45.123456

SUMMARY
────────────────────────────────────────────────────────────────────────────
  total_replay_days............................ 14
  total_candles_processed..................... 2847
  pivot_evaluation_coverage................... 99.87%
  signal_confirmation_rate................... 94.2%
  failure_count............................... 0

PIVOT METRICS
────────────────────────────────────────────────────────────────────────────
  pivot_levels_checked........................ 85410
  pivot_interactions_detected................. 2834
  pivot_rejections............................ 1247
  pivot_acceptances........................... 892
  pivot_breakouts............................. 456
  pivot_breakdowns............................ 239
  pivot_clusters_detected..................... 156

LIQUIDITY METRICS
────────────────────────────────────────────────────────────────────────────
  liquidity_events_detected................... 487
  sweeps_at_pivots............................ 412
  false_breakouts_detected.................... 45
  trap_events_detected........................ 30

SIGNAL METRICS
────────────────────────────────────────────────────────────────────────────
  signals_generated........................... 127
  signals_with_pivot_confirmation............ 120
  signals_with_liquidity_confirmation........ 115
  signals_blocked_due_to_trap................. 8

SUCCESS CRITERIA
────────────────────────────────────────────────────────────────────────────
  pivot_coverage_gte_99....................... ✓ PASS
  liquidity_sweeps_detected................... ✓ PASS
  no_signal_bypass............................ ✓ PASS
  signal_confirmation_gt_90................... ✓ PASS
  failures_below_threshold.................... ✓ PASS

================================================================================
```

---

## INTEGRATION WITH LIVE TRADING

After validation passes, the Pivot Reaction Engine and Liquidity Event Detection are ready for live deployment:

1. **Pivot Reaction Engine** is initialized in `main.py` startup
2. **Every candle close** triggers `pivot_engine.evaluate_candle()`
3. **Every trade signal** passes through `pivot_engine.validate_trade_signal()`
4. **Liquidity events** are logged and referenced in trade decisions

---

## TROUBLESHOOTING

### Issue: Pivot Coverage < 99%
**Cause**: Pivot evaluation not running on every candle  
**Solution**: 
- Verify `pivot_engine.evaluate_candle()` is called in candle processing loop
- Check for exceptions in pivot evaluation
- Verify pivot levels are calculated correctly

### Issue: No Liquidity Events Detected
**Cause**: Sweep/trap detection thresholds too strict  
**Solution**:
- Review sweep detection threshold (currently 1.5x ATR)
- Review trap detection threshold (currently 0.8x ATR)
- Verify ATR calculation is correct

### Issue: Trade Signals Bypass Pivot Validation
**Cause**: Execution layer not calling `validate_trade_signal()`  
**Solution**:
- Add validation call before trade execution
- Verify validation result is checked before order placement
- Add audit logs for all validation calls

### Issue: High Failure Count
**Cause**: Multiple validation failures  
**Solution**:
- Review failure log for patterns
- Identify root cause of each failure
- Fix issues one at a time
- Re-run validation after each fix

---

## NEXT STEPS

1. ✅ Run replay validation: `python replay_validation_agent.py`
2. ⏳ Review validation report
3. ⏳ If PASSED: Deploy to live trading
4. ⏳ If FAILED: Fix issues and re-run validation
5. ⏳ Monitor live trading for pivot/liquidity events

---

## DOCUMENTATION REFERENCES

- **Pivot Reaction Engine**: `pivot_reaction_engine.py`
- **Execution Layer**: `execution.py`
- **Main Entry Point**: `main.py`
- **Validation Agent**: `replay_validation_agent.py`

---

## CONTACT & SUPPORT

For validation issues or questions:
1. Review this documentation
2. Check `replay_validation_agent.log` for detailed error messages
3. Examine `replay_validation_report.json` for structured results
4. Review relevant source code files
