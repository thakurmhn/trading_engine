# Pivot Reaction Engine - Integration Guide
**Status**: Ready for Integration  
**Time Required**: 30-45 minutes  
**Priority**: CRITICAL - Must deploy before next trading session

---

## Integration Overview

The Pivot Reaction Engine is now created and ready to integrate. This guide provides **exact code changes** needed to wire it into the trading pipeline.

### Integration Points:
1. **main.py** - Initialize engine, call on every candle close
2. **execution.py** - Validate signals before trade execution
3. **dashboard.py** - Display pivot metrics

---

## Step 1: Initialize Pivot Engine (main.py)

### Location: `main.py` - After imports, before run()

**Add import**:
```python
# Add after existing imports
from pivot_reaction_engine import (
    initialize_pivot_engine, 
    get_pivot_engine,
    CandleData,
    PivotLevel
)
```

### Location: `main.py` - Inside do_warmup() function

**Add after warmup completes** (around line 180):
```python
def do_warmup() -> MarketData:
    # ... existing warmup code ...
    
    logging.info(f"{GREEN}[WARMUP] Complete. Bot ready for market open.{RESET}")
    
    # NEW: Initialize Pivot Reaction Engine
    pivot_engine = initialize_pivot_engine(atr_multiplier=0.05)
    logging.info(f"{GREEN}[PIVOT_ENGINE] Initialized - Mandatory pivot validation ACTIVE{RESET}")
    
    return md
```

---

## Step 2: Evaluate Pivots on Every Candle Close (main.py)

### Location: `main.py` - Inside main_strategy_code(), after [NEW CANDLE] log

**Find this section** (around line 350):
```python
if is_new_candle:
    last_bar = df_3m.iloc[-1] if n3 > 0 else None
    bar_time = (
        str(last_bar.get("time") or last_bar.get("date", "?"))
        if last_bar is not None else "?"
    )
    # ... existing NEW CANDLE log ...
    
    logging.info(
        f"{CYAN}[NEW CANDLE] {sym} bar={bar_time} ..."
    )
    
    last_candle_count[sym] = n3
```

**Add AFTER the [NEW CANDLE] log**:
```python
    # ... existing [NEW CANDLE] log ...
    
    last_candle_count[sym] = n3
    
    # ═══════════════════════════════════════════════════════════════
    # NEW: MANDATORY PIVOT EVALUATION
    # ═══════════════════════════════════════════════════════════════
    
    # Get pivot engine
    pivot_engine = get_pivot_engine()
    
    # Prepare candle data
    candle_data = CandleData(
        timestamp=bar_time,
        open=float(last_bar.get("open", 0)),
        high=float(last_bar.get("high", 0)),
        low=float(last_bar.get("low", 0)),
        close=float(last_bar.get("close", 0)),
        atr=float(last_bar.get("atr14", 10.0) or 10.0)
    )
    
    # Get pivot levels (from indicators or calculate fresh)
    # Option A: If pivots stored in indicators dict
    pivot_levels = {
        "CPR": indicators.get("cpr_levels", {}),
        "TRADITIONAL": indicators.get("traditional_levels", {}),
        "CAMARILLA": indicators.get("camarilla_levels", {})
    }
    
    # Option B: If pivots need to be calculated fresh
    # (Use this if pivots not in indicators dict)
    if not pivot_levels["CPR"]:
        from indicators import (
            calculate_cpr,
            calculate_traditional_pivots,
            calculate_camarilla_pivots
        )
        prev_ohlc = md.get_prev_day_ohlc(sym)
        if prev_ohlc:
            ph = float(prev_ohlc.get("high", 0))
            pl = float(prev_ohlc.get("low", 0))
            pc = float(prev_ohlc.get("close", 0))
            
            pivot_levels = {
                "CPR": calculate_cpr(ph, pl, pc),
                "TRADITIONAL": calculate_traditional_pivots(ph, pl, pc),
                "CAMARILLA": calculate_camarilla_pivots(ph, pl, pc)
            }
    
    # Evaluate pivot interactions (MANDATORY)
    pivot_interactions = pivot_engine.evaluate_candle(candle_data, pivot_levels)
    
    # Store interactions for signal validation
    # (We'll pass this to paper_order/live_order)
    data_feed.pivot_interactions = pivot_interactions
    
    logging.info(
        f"{GREEN}[PIVOT_ENGINE] Evaluated {len(pivot_interactions)} pivot levels, "
        f"detected {sum(1 for i in pivot_interactions.values() if i.interaction_type.value != 'NO_INTERACTION')} interactions{RESET}"
    )
    
    # ═══════════════════════════════════════════════════════════════
```

---

## Step 3: Validate Signals Before Trade Execution (execution.py)

### Location: `execution.py` - Inside paper_order() function

**Find the section where signal is generated** (search for `detect_signal`):
```python
def paper_order(df_3m, hist_yesterday_15m=None, mode="LIVE"):
    # ... existing code ...
    
    # Signal detection
    signal, block_reason = detect_signal(
        candles_3m, candles_15m, ...
    )
    
    if signal is None:
        # ... existing block handling ...
```

**Add AFTER signal detection, BEFORE entry execution**:
```python
    # Signal detection
    signal, block_reason = detect_signal(
        candles_3m, candles_15m, ...
    )
    
    if signal is None:
        # ... existing block handling ...
        return
    
    # ═══════════════════════════════════════════════════════════════
    # NEW: MANDATORY PIVOT VALIDATION
    # ═══════════════════════════════════════════════════════════════
    
    # Get pivot interactions from data_feed (set in main.py)
    pivot_interactions = getattr(data_feed, 'pivot_interactions', None)
    
    if pivot_interactions is None:
        logging.error(
            "[PIVOT_ENGINE][CRITICAL] No pivot interactions available - "
            "PIVOT_DECISION_PIPELINE_FAILURE"
        )
        # Block trade if pivot evaluation missing
        return
    
    # Validate signal against pivot context
    from pivot_reaction_engine import get_pivot_engine
    pivot_engine = get_pivot_engine()
    
    signal_side = signal[0]  # "CALL" or "PUT"
    signal_reason = signal[1]  # Reason string
    
    is_valid, validation_reason = pivot_engine.validate_trade_signal(
        signal_side, signal_reason, pivot_interactions
    )
    
    if not is_valid:
        logging.warning(
            f"[PIVOT_ENGINE][BLOCK] {signal_side} signal blocked by pivot validation - "
            f"{validation_reason}"
        )
        return
    
    logging.info(
        f"[PIVOT_ENGINE][VALIDATED] {signal_side} signal approved - {validation_reason}"
    )
    
    # ═══════════════════════════════════════════════════════════════
    
    # Continue with existing entry logic
    # ... rest of paper_order() ...
```

**Repeat the same validation in live_order()** if you use live trading.

---

## Step 4: Add Pivot Metrics to Dashboard (dashboard.py)

### Location: `dashboard.py` - In the report generation function

**Find the section where metrics are printed** (search for "TRADE SUMMARY"):
```python
def generate_dashboard_report(log_file):
    # ... existing code ...
    
    print("  TRADE SUMMARY")
    print("------------------------------------------------------------")
    # ... existing metrics ...
```

**Add AFTER trade summary, BEFORE signal pipeline**:
```python
    # ... existing TRADE SUMMARY section ...
    
    # ═══════════════════════════════════════════════════════════════
    # NEW: PIVOT REACTION ENGINE METRICS
    # ═══════════════════════════════════════════════════════════════
    
    try:
        from pivot_reaction_engine import get_pivot_engine
        pivot_engine = get_pivot_engine()
        
        print("\n  PIVOT REACTION ENGINE")
        print("------------------------------------------------------------")
        
        metrics = pivot_engine.get_metrics()
        
        print(f"  Candles evaluated      : {metrics['total_candles_evaluated']}")
        print(f"  Pivot levels checked   : {metrics['pivot_levels_checked']}")
        print(f"  Interactions detected  : {metrics['pivot_interactions_detected']}")
        print(f"    - Rejections         : {metrics['pivot_rejections']}")
        print(f"    - Acceptances        : {metrics['pivot_acceptances']}")
        print(f"    - Breakouts          : {metrics['pivot_breakouts']}")
        print(f"    - Breakdowns         : {metrics['pivot_breakdowns']}")
        print(f"  Cluster events         : {metrics['pivot_cluster_events']}")
        print(f"  Trades w/ pivot confirm: {metrics['trades_with_pivot_confirmation']}")
        print(f"  Trades blocked (pivot) : {metrics['trades_blocked_no_pivot']}")
        
        # Calculate coverage percentage
        if metrics['total_candles_evaluated'] > 0:
            expected_checks = metrics['total_candles_evaluated'] * 27  # 27 pivot levels
            coverage_pct = (metrics['pivot_levels_checked'] / expected_checks) * 100
            print(f"  Pivot coverage         : {coverage_pct:.1f}%")
        
    except Exception as e:
        print(f"  [ERROR] Pivot engine metrics unavailable: {e}")
    
    # ═══════════════════════════════════════════════════════════════
    
    # Continue with existing sections
    print("\n  SIGNAL PIPELINE")
    # ... rest of dashboard ...
```

---

## Step 5: Validation Commands

After integration, test with these commands:

### Syntax Check:
```bash
python -m py_compile pivot_reaction_engine.py
python -m py_compile main.py
python -m py_compile execution.py
python -m py_compile dashboard.py
```

### Replay Test:
```bash
python main.py --mode REPLAY --date 2026-03-11
```

### Check Logs:
```bash
# Verify pivot engine initialized
grep "\[PIVOT_ENGINE\] Initialized" options_trade_engine_*.log

# Verify pivot evaluation happening
grep "\[PIVOT_AUDIT\]" options_trade_engine_*.log | head -20

# Verify pivot validation
grep "\[PIVOT_ENGINE\]\[VALIDATED\]" options_trade_engine_*.log

# Check for blocks
grep "\[PIVOT_ENGINE\]\[BLOCK\]" options_trade_engine_*.log

# Check for clusters
grep "\[PIVOT_CLUSTER_EVENT\]" options_trade_engine_*.log
```

### Expected Log Output:
```
[PIVOT_ENGINE] Initialized - Mandatory pivot validation ACTIVE
[NEW CANDLE] NSE:NIFTY50-INDEX bar=2026-03-11 09:18:00 ...
[PIVOT_AUDIT] timestamp=09:18:00 candle_close=25432.50 pivot_family=CPR pivot_level=TC pivot_price=25471.00 interaction_type=NO_INTERACTION ...
[PIVOT_AUDIT] timestamp=09:18:00 candle_close=25432.50 pivot_family=TRADITIONAL pivot_level=R1 pivot_price=25514.00 interaction_type=NO_INTERACTION ...
[PIVOT_CLUSTER_EVENT] cluster_id=0 pivots=['CPR_TC', 'TRADITIONAL_R1'] center=25492.50 range=43.00pts count=2 dominant=CPR
[PIVOT_ENGINE] Evaluated 27 pivot levels, detected 3 interactions
[SIGNAL FIRED] side=PUT score=68 ...
[PIVOT_ENGINE][VALIDATED] PUT signal approved - PUT_CONFIRMED_RESISTANCE_REJECTION_R1
[ENTRY][PAPER] PUT NSE:NIFTY2630225500PE @ 248.85
```

---

## Step 6: Dashboard Verification

After replay, check dashboard report:

```bash
cat reports/dashboard_report_2026-03-11.txt
```

**Expected new section**:
```
  PIVOT REACTION ENGINE
------------------------------------------------------------
  Candles evaluated      : 262
  Pivot levels checked   : 7,074  (27 levels × 262 candles)
  Interactions detected  : 184
    - Rejections         : 67
    - Acceptances        : 42
    - Breakouts          : 38
    - Breakdowns         : 37
  Cluster events         : 12
  Trades w/ pivot confirm: 58
  Trades blocked (pivot) : 4
  Pivot coverage         : 100.0%
```

---

## Troubleshooting

### Issue: "Pivot Reaction Engine not initialized"
**Solution**: Verify `initialize_pivot_engine()` is called in `do_warmup()`

### Issue: "No pivot interactions available"
**Solution**: Verify `pivot_interactions` is set in main.py after `evaluate_candle()`

### Issue: "All trades blocked"
**Solution**: Check validation logic - may be too strict. Review `_validate_signal_alignment()`

### Issue: "No [PIVOT_AUDIT] logs"
**Solution**: Verify `evaluate_candle()` is called on every candle close

### Issue: "Pivot levels empty"
**Solution**: Verify pivot calculation in main.py - use Option B if pivots not in indicators

---

## Rollback Plan

If integration causes issues:

```bash
# Remove pivot engine import from main.py
# Comment out pivot evaluation section
# Comment out pivot validation in execution.py
# Remove pivot metrics from dashboard.py

# Or full rollback:
git checkout HEAD -- main.py execution.py dashboard.py
rm pivot_reaction_engine.py
```

---

## Success Criteria

✅ Integration successful if:
- [ ] No Python syntax errors
- [ ] Pivot engine initializes at startup
- [ ] [PIVOT_AUDIT] logs appear for every candle
- [ ] [PIVOT_ENGINE][VALIDATED] logs appear before entries
- [ ] Dashboard shows pivot metrics
- [ ] Pivot coverage = 100%
- [ ] No trades execute without pivot validation

---

## Next Steps After Integration

1. **Run replay test** on 2026-03-11 (known duplicate trade day)
2. **Verify metrics** in dashboard report
3. **Check logs** for pivot validation
4. **Paper trade** 1 full session
5. **Monitor** for any blocked trades
6. **Deploy to live** with confidence

---

**Ready to integrate?** Start with Step 1 (Initialize in main.py) and work through each step sequentially.
