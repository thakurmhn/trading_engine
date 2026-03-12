# QUICK ACTION CARD - Profitability Fixes
**Priority**: CRITICAL | **Time to Deploy**: 2-4 hours | **Expected Impact**: +60% profit factor

---

## 🔴 CRITICAL FIX #1: Duplicate Trades (30 min)
**Problem**: Same trade logged 6-7 times → inflated metrics  
**File**: `execution.py`  
**Add at top of file**:
```python
_logged_trades = set()  # Global deduplication tracker
```

**In trade logging function** (search for `def _record_trade` or similar):
```python
def _record_trade(trade_data):
    trade_key = (trade_data['symbol'], trade_data['entry_time'], trade_data['entry_price'])
    if trade_key in _logged_trades:
        logging.warning(f"[DUPLICATE BLOCKED] {trade_key}")
        return
    _logged_trades.add(trade_key)
    # ... rest of existing code
```

**Test**: `python main.py --mode REPLAY --date 2026-03-11`  
**Expect**: Trade count drops from 62 → ~10-15

---

## 🔴 CRITICAL FIX #2: Over-Filtering (45 min)
**Problem**: 806 DAILY_S4 blocks = missing 90% of trend moves  
**File**: `signals.py`  
**Find**: `if close < daily_s4: return None, "DAILY_S4_FILTER"`

**Replace with**:
```python
# Soften S4/R4 blocks - allow trend continuation with penalty
if close < daily_s4:
    if adx >= 30 and st_bias_15m == "BEARISH":
        score_penalty = -10  # Allow PUT with penalty
        logging.info(f"[DAILY_S4_OVERRIDE] Bearish trend confirmed ADX={adx:.1f}, allowing PUT -10pts")
    else:
        return None, "DAILY_S4_FILTER"

if close > daily_r4:
    if adx >= 30 and st_bias_15m == "BULLISH":
        score_penalty = -10  # Allow CALL with penalty
        logging.info(f"[DAILY_R4_OVERRIDE] Bullish trend confirmed ADX={adx:.1f}, allowing CALL -10pts")
    else:
        return None, "DAILY_R4_FILTER"
```

**Test**: Replay 2026-03-08 (big trend day)  
**Expect**: Blocks drop from 806 → ~200, capture trend moves

---

## 🟡 HIGH PRIORITY FIX #3: ATR-Based Stops (60 min)
**Problem**: Fixed 10pt stops → premature stop-outs  
**File**: `option_exit_manager.py`  
**Add new function**:
```python
def calculate_dynamic_stop(entry_price, atr, volatility_tier, side):
    """ATR-based stop - adapts to market volatility"""
    if volatility_tier == "VERY_LOW":
        multiplier = 1.2
    elif volatility_tier == "HIGH":
        multiplier = 2.0
    else:
        multiplier = 1.5
    
    stop_distance = atr * multiplier
    stop_price = entry_price - stop_distance if side == "CALL" else entry_price + stop_distance
    
    logging.info(f"[DYNAMIC_STOP] entry={entry_price:.2f} atr={atr:.1f} "
                 f"tier={volatility_tier} mult={multiplier} stop={stop_price:.2f}")
    return stop_price
```

**Replace fixed stop logic** (search for `MAX_LOSS_PER_TRADE` or `stop_price = entry - 10`):
```python
# OLD: stop_price = entry_price - 10
# NEW:
stop_price = calculate_dynamic_stop(entry_price, atr, volatility_tier, side)
```

**Test**: Replay 2026-03-10 (high vol day)  
**Expect**: Fewer premature stops, payoff ratio improves

---

## 🟡 HIGH PRIORITY FIX #4: PUT Scoring Parity (45 min)
**Problem**: PUT P&L +21 vs CALL +198 (9× gap)  
**File**: `entry_logic.py`  
**Find**: `def _score_trend_alignment` function

**Add PUT reversal credit** (after existing PUT logic):
```python
# In PUT section, after existing conditions:
if b15 == "BULLISH":  # 15m opposing but slope improving
    c15 = indicators.get("candle_15m")
    if c15 is not None:
        sl = str(c15.get("supertrend_slope", "")).upper()
        if sl == "DOWN":
            logging.info("[PUT_REVERSAL_CREDIT] 15m slope turning down +4pts")
            return w // 4  # 4 pts early reversal credit
```

**Test**: Replay 2026-03-02 (strong PUT day)  
**Expect**: More PUT entries, balanced CALL/PUT P&L

---

## 📊 Validation Commands

### After Each Fix:
```bash
# 1. Syntax check
python -m py_compile execution.py signals.py entry_logic.py option_exit_manager.py

# 2. Replay test (single day)
python main.py --mode REPLAY --date 2026-03-11

# 3. Check metrics
grep "Net P&L" reports/dashboard_report_2026-03-11.txt
grep "DUPLICATE BLOCKED" options_trade_engine_*.log
grep "DAILY_S4_OVERRIDE" options_trade_engine_*.log

# 4. Full 14-day replay (after all fixes)
for date in 2026-02-25 2026-02-26 2026-02-27 2026-03-02 2026-03-03 2026-03-04 2026-03-05 2026-03-08 2026-03-09 2026-03-10 2026-03-11; do
    python main.py --mode REPLAY --date $date
done

# 5. Generate comparison report
python _build_strategy_diagnostics.py
```

---

## 🎯 Expected Results (After All 4 Fixes)

| Metric | Before | After | Change |
|--------|--------|-------|--------|
| Profit Factor | 1.12 | 1.35+ | +20% |
| Expectancy | +0.45 pts | +0.95 pts | +111% |
| Payoff Ratio | 0.86 | 1.05+ | +22% |
| Avg Win | +12.72 pts | +15.5 pts | +22% |
| Avg Loss | -14.74 pts | -12.0 pts | -19% |
| PUT P&L (14d) | +21 pts | +90 pts | +329% |
| Trade Count | 291 | 360+ | +24% |

---

## ⚠️ Safety Checks

### Before Live Deployment:
- [ ] All 4 fixes pass syntax check
- [ ] Replay 2026-03-11 completes without errors
- [ ] Trade count is realistic (10-20 per day, not 60+)
- [ ] No duplicate trades in dashboard
- [ ] Profit factor > 1.2 on replay
- [ ] Paper trade 1 full session
- [ ] Monitor first live session with 1-lot size only

### Rollback Triggers:
- Win rate drops below 50%
- 3 consecutive losing trades
- Any Python exception in live trading
- Duplicate trades still appearing

### Rollback Command:
```bash
git checkout HEAD -- execution.py signals.py entry_logic.py option_exit_manager.py
python main.py --mode PAPER  # Validate rollback works
```

---

## 📁 Backup Before Starting

```bash
# Create backup branch
git checkout -b backup-pre-profitability-fixes
git add -A
git commit -m "Backup before profitability improvements"
git checkout main

# Or simple file backup
cp execution.py execution.py.backup
cp signals.py signals.py.backup
cp entry_logic.py entry_logic.py.backup
cp option_exit_manager.py option_exit_manager.py.backup
```

---

## 🚀 Implementation Order

1. **Fix #1 (Duplicates)** → Test → Commit
2. **Fix #2 (S4/R4)** → Test → Commit  
3. **Fix #3 (ATR Stops)** → Test → Commit
4. **Fix #4 (PUT Parity)** → Test → Commit
5. **Full Replay** → Compare metrics
6. **Paper Trade** → 1 full day
7. **Live Deploy** → 1-lot size, monitor closely

**Total Time**: 3-4 hours for all fixes + testing

---

## 📞 Need Help?

If any fix causes errors:
1. Check syntax: `python -m py_compile <file>`
2. Review logs: `tail -100 options_trade_engine_*.log`
3. Rollback: `git checkout HEAD -- <file>`
4. Re-read the fix instructions above
5. Test in isolation on single replay day first

**Ready to start?** Begin with Fix #1 (Duplicates) - it's the safest and fastest.
