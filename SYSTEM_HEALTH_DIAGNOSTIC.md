# Trading System Health Diagnostic Checklist
**Purpose**: Quick health check before/after improvements  
**Frequency**: Run after each code change, daily in live trading

---

## 🔍 Quick Health Check (5 minutes)

### Run These Commands:
```bash
# 1. Latest dashboard report
cat reports/dashboard_report_$(date +%Y-%m-%d).txt | head -50

# 2. Check for critical errors
grep -i "error\|exception\|failed" options_trade_engine_*.log | tail -20

# 3. Duplicate trade check
grep "DUPLICATE" options_trade_engine_*.log | wc -l

# 4. Over-filtering check
grep "DAILY_S4_FILTER\|DAILY_R4_FILTER" options_trade_engine_*.log | wc -l
```

---

## ✅ System Health Indicators

### 🟢 HEALTHY System Shows:
- [ ] **Trade Count**: 10-20 trades per day (not 60+)
- [ ] **Win Rate**: 55-65%
- [ ] **Profit Factor**: > 1.3
- [ ] **Payoff Ratio**: > 1.0 (avg win > avg loss)
- [ ] **CALL/PUT Balance**: Both sides contribute (not 9:1 ratio)
- [ ] **Block Rate**: < 300 blocks per day
- [ ] **No Duplicates**: Zero `[DUPLICATE BLOCKED]` logs
- [ ] **S4/R4 Overrides**: Present on trend days
- [ ] **ATR Stops**: Dynamic stop distances logged
- [ ] **Signal Logs**: `[SIGNAL FIRED]` present before `[ENTRY OK]`

### 🟡 WARNING Signs:
- [ ] **Trade Count**: 25-40 trades per day (overtrading)
- [ ] **Win Rate**: 50-54% (marginal edge)
- [ ] **Profit Factor**: 1.1-1.3 (fragile)
- [ ] **Payoff Ratio**: 0.85-0.95 (losses approaching wins)
- [ ] **CALL/PUT Imbalance**: One side > 70% of P&L
- [ ] **Block Rate**: 300-600 blocks per day
- [ ] **Few Overrides**: S4/R4 overrides rare on trend days
- [ ] **Static Stops**: All stops same distance (10-12 pts)

### 🔴 CRITICAL Issues:
- [ ] **Trade Count**: > 40 trades per day (likely duplicates)
- [ ] **Win Rate**: < 50% (losing edge)
- [ ] **Profit Factor**: < 1.1 (barely profitable)
- [ ] **Payoff Ratio**: < 0.85 (losses exceed wins significantly)
- [ ] **One-Sided**: CALL or PUT contributes < 10% of P&L
- [ ] **Block Rate**: > 600 blocks per day (over-filtering)
- [ ] **Duplicates Present**: `[DUPLICATE BLOCKED]` logs found
- [ ] **No Overrides**: Zero S4/R4 overrides on trend days
- [ ] **No Signal Logs**: `[SIGNAL FIRED]` missing

---

## 📊 Metric Extraction Commands

### Daily Performance:
```bash
# Net P&L
grep "Net P&L" reports/dashboard_report_$(date +%Y-%m-%d).txt

# Win rate
grep "Win rate" reports/dashboard_report_$(date +%Y-%m-%d).txt

# Trade count
grep "Total trades" reports/dashboard_report_$(date +%Y-%m-%d).txt

# CALL vs PUT
grep "CALL trades\|PUT trades" reports/dashboard_report_$(date +%Y-%m-%d).txt
```

### Signal Pipeline Health:
```bash
# Signals fired vs blocked
grep "Signals fired\|Total blocked" reports/dashboard_report_$(date +%Y-%m-%d).txt

# Top blockers
grep -A 10 "BLOCKED BREAKDOWN" reports/dashboard_report_$(date +%Y-%m-%d).txt
```

### Exit Quality:
```bash
# Exit reason distribution
grep -A 10 "EXIT REASONS" reports/dashboard_report_$(date +%Y-%m-%d).txt

# Average bars held
grep "Avg hold\|Average P&L" reports/dashboard_report_$(date +%Y-%m-%d).txt
```

---

## 🔬 Deep Dive Diagnostics (15 minutes)

### 1. Duplicate Trade Analysis
```bash
# Count unique vs total trades
echo "Total trade logs:"
grep "\[ENTRY\]" options_trade_engine_*.log | wc -l

echo "Unique entry prices:"
grep "\[ENTRY\]" options_trade_engine_*.log | awk '{print $NF}' | sort -u | wc -l

# If total >> unique → duplicates present
```

### 2. Over-Filtering Analysis
```bash
# S4/R4 block count by day
for log in options_trade_engine_2026-*.log; do
    date=$(echo $log | grep -oP '\d{4}-\d{2}-\d{2}')
    s4_blocks=$(grep -c "DAILY_S4_FILTER" $log)
    r4_blocks=$(grep -c "DAILY_R4_FILTER" $log)
    echo "$date: S4=$s4_blocks R4=$r4_blocks Total=$((s4_blocks + r4_blocks))"
done | sort

# Days with > 500 blocks → severe over-filtering
```

### 3. PUT Side Health Check
```bash
# PUT vs CALL entry count
echo "CALL entries:"
grep "\[ENTRY\].*CALL" options_trade_engine_*.log | wc -l

echo "PUT entries:"
grep "\[ENTRY\].*PUT" options_trade_engine_*.log | wc -l

# PUT vs CALL P&L (from dashboard)
grep "CALL trades\|PUT trades" reports/dashboard_report_*.txt | tail -20
```

### 4. Stop Loss Effectiveness
```bash
# Count SL hits
grep "SL_HIT\|LOSS" reports/dashboard_report_*.txt | wc -l

# Average loss size
grep "Avg loss" reports/dashboard_report_*.txt | tail -5

# Check if stops are dynamic
grep "DYNAMIC_STOP" options_trade_engine_*.log | head -10
# If empty → stops are still static
```

### 5. Trend Day Capture
```bash
# Check 2026-03-08 (known big trend day)
echo "2026-03-08 Performance:"
grep "Net P&L\|Total trades\|Win rate" reports/dashboard_report_2026-03-08.txt

echo "S4/R4 Overrides on 2026-03-08:"
grep "DAILY_S4_OVERRIDE\|DAILY_R4_OVERRIDE" options_trade_engine_2026-03-08.log | wc -l
# Should be > 0 after Fix #2
```

---

## 📈 Before/After Comparison Template

### Baseline (Before Fixes):
```
Date Range: 2026-02-25 to 2026-03-11 (14 days)
─────────────────────────────────────────────────
Total Trades:        291
Win Rate:            55.3%
Profit Factor:       1.12
Expectancy:          +0.45 pts/trade
Payoff Ratio:        0.86
Avg Win:             +12.72 pts
Avg Loss:            -14.74 pts
CALL P&L:            +198 pts
PUT P&L:             +21 pts
Max Daily Loss:      -72 pts (2026-03-03)
Best Day:            +570 pts (2026-03-08)
Avg Blocks/Day:      ~400
Duplicate Trades:    YES (62 on 2026-03-11)
```

### After Fixes (Fill in after replay):
```
Date Range: 2026-02-25 to 2026-03-11 (14 days)
─────────────────────────────────────────────────
Total Trades:        _____
Win Rate:            _____%
Profit Factor:       ____
Expectancy:          _____ pts/trade
Payoff Ratio:        ____
Avg Win:             _____ pts
Avg Loss:            _____ pts
CALL P&L:            _____ pts
PUT P&L:             _____ pts
Max Daily Loss:      _____ pts
Best Day:            _____ pts
Avg Blocks/Day:      ~____
Duplicate Trades:    NO (verified)
```

### Improvement Calculation:
```bash
# Profit factor improvement
echo "scale=2; (NEW_PF - 1.12) / 1.12 * 100" | bc
# Example: (1.35 - 1.12) / 1.12 * 100 = 20.5% improvement

# Expectancy improvement
echo "scale=2; (NEW_EXP - 0.45) / 0.45 * 100" | bc

# PUT P&L improvement
echo "scale=2; (NEW_PUT_PNL - 21) / 21 * 100" | bc
```

---

## 🎯 Target Metrics (Post-Improvement)

### Minimum Acceptable:
- Profit Factor: > 1.25
- Win Rate: > 53%
- Payoff Ratio: > 0.95
- PUT P&L: > 80 pts (14 days)
- No duplicate trades

### Good Performance:
- Profit Factor: > 1.35
- Win Rate: > 57%
- Payoff Ratio: > 1.05
- PUT P&L: > 120 pts (14 days)
- Blocks/day: < 250

### Excellent Performance:
- Profit Factor: > 1.50
- Win Rate: > 60%
- Payoff Ratio: > 1.20
- PUT P&L: > 150 pts (14 days)
- Blocks/day: < 200

---

## 🚨 Red Flags to Watch

### During Live Trading:
1. **Consecutive Losses**: 3+ in a row → pause trading, review logs
2. **Rapid Drawdown**: -₹5,000 in < 30 min → circuit breaker should trigger
3. **No Entries**: 0 trades in first 2 hours → check filters
4. **Duplicate Alerts**: Any `[DUPLICATE BLOCKED]` → stop immediately
5. **Exception Errors**: Any Python traceback → stop immediately

### During Replay Testing:
1. **Trade Count Spike**: > 40 trades/day → likely duplicates
2. **Zero Overrides**: No S4/R4 overrides on 2026-03-08 → Fix #2 not working
3. **All Stops Same**: All stop distances = 10-12 pts → Fix #3 not working
4. **PUT Still Weak**: PUT P&L < 50 pts → Fix #4 not working
5. **Syntax Errors**: Any import/compile errors → rollback immediately

---

## 📋 Daily Checklist (Live Trading)

### Pre-Market (9:00-9:15 AM):
- [ ] Check overnight news/gaps
- [ ] Verify warmup completed (225+ bars)
- [ ] Review yesterday's P&L
- [ ] Confirm no duplicate trades yesterday
- [ ] Check circuit breaker reset

### During Market (9:15-3:15 PM):
- [ ] Monitor first trade (verify no duplicates)
- [ ] Check P&L every hour
- [ ] Watch for 3 consecutive losses
- [ ] Verify entries happening (not over-filtered)
- [ ] Monitor log for exceptions

### Post-Market (3:30-4:00 PM):
- [ ] Generate dashboard report
- [ ] Review all trades
- [ ] Check for duplicates
- [ ] Analyze exit reasons
- [ ] Calculate daily metrics
- [ ] Update performance log

---

## 🔧 Troubleshooting Guide

### Issue: No Trades Happening
**Check**:
```bash
grep "SIGNAL FIRED" options_trade_engine_*.log | tail -10
grep "SIGNAL BLOCKED" options_trade_engine_*.log | tail -10
```
**Likely Cause**: Over-filtering (S4/R4 blocks)  
**Fix**: Verify Fix #2 deployed correctly

### Issue: All Trades Losing
**Check**:
```bash
grep "SL_HIT\|LOSS" reports/dashboard_report_*.txt
grep "DYNAMIC_STOP" options_trade_engine_*.log | head -5
```
**Likely Cause**: Stops too tight  
**Fix**: Verify Fix #3 (ATR stops) deployed

### Issue: Only CALL Trades
**Check**:
```bash
grep "\[ENTRY\].*PUT" options_trade_engine_*.log | wc -l
grep "PUT_REVERSAL_CREDIT" options_trade_engine_*.log | wc -l
```
**Likely Cause**: PUT scoring still asymmetric  
**Fix**: Verify Fix #4 deployed correctly

### Issue: Duplicate Trades
**Check**:
```bash
grep "DUPLICATE BLOCKED" options_trade_engine_*.log
```
**Likely Cause**: Fix #1 not deployed  
**Fix**: Add deduplication guard to execution.py

---

## 📊 Weekly Performance Review Template

```
Week of: ___________
─────────────────────────────────────────────────
Trading Days:        _____
Total Trades:        _____
Win Rate:            _____%
Net P&L:             ₹_____
Profit Factor:       _____
Best Day:            ₹_____ (date: _____)
Worst Day:           ₹_____ (date: _____)
Avg Trades/Day:      _____
CALL Trades:         _____
PUT Trades:          _____
CALL P&L:            ₹_____
PUT P&L:             ₹_____

Issues Encountered:
- [ ] Duplicates: _____
- [ ] Over-filtering: _____
- [ ] Exceptions: _____
- [ ] Circuit breakers: _____

Improvements Needed:
1. _____________________
2. _____________________
3. _____________________
```

---

## 🎓 Learning from Losses

### After Each Losing Trade:
1. **Entry Quality**: Was score > threshold + 10? (marginal entries lose more)
2. **Stop Placement**: Was stop < 1.5 × ATR? (too tight)
3. **Exit Timing**: Did we hold < 3 bars? (premature exit)
4. **Trend Alignment**: Were 15m and 3m aligned? (counter-trend trades risky)
5. **Time of Day**: Was it 12:00-12:20 or 14:45+? (avoid these windows)

### After Each Losing Day:
1. **Day Type**: Was CPR wide? (range days are choppy)
2. **Block Rate**: Were > 500 signals blocked? (over-filtering)
3. **Trade Count**: Were > 25 trades taken? (overtrading)
4. **Side Balance**: Was one side > 80% of trades? (bias error)
5. **Circuit Breaker**: Did it trigger? (if not, should it have?)

---

**Use this checklist daily to catch issues early and track improvement progress.**
