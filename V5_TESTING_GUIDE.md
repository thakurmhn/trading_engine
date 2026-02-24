# v5 Scoring Fix - Testing & Validation Guide

## Overview
This guide walks through validating that the scoring engine fix is working correctly by deploying in REPLAY mode first, then PAPER, then LIVE.

---

## PHASE 1: IMPORT VALIDATION (5 minutes)

### Step 1: Verify Imports Work
```bash
python -c "from signals import detect_signal; from entry_logic import check_entry_condition; from indicators import classify_cpr_width, momentum_ok; print('SUCCESS')"
```

Expected Output:
```
SUCCESS
```

If this fails:
- Check Python venv is active
- Check indicators.py exists and has classify_cpr_width, momentum_ok functions
- Check signals.py imports are at top of file

### Step 2: Check Code Has Fix
```bash
grep "momentum_ok_call" signals.py | wc -l
```

Expected: 2 (one for computation, one for dict)

```bash
grep '"momentum_ok_call": mom_ok_call' signals.py
```

Expected: Should find the line in indicators dict

---

## PHASE 2: REPLAY MODE VALIDATION (30 minutes)

### Step 1: Run REPLAY on Known Good Day
```bash
python main.py --mode=REPLAY --symbol=NSE:NIFTY50-INDEX --date=2026-02-20 --start_time=09:30:00 --end_time=16:00:00
```

### Step 2: Monitor Console Output

**Good Indicators (You should see):**
```
[INDICATORS BUILT] MOM_CALL=True MOM_PUT=False CPR=NARROW ET=PULLBACK RSI_prev=52.34
[SCORE BREAKDOWN v5][CALL] 95/50 | Indicators: MOM=OK CPR=NARROW ET=PULLBACK RSI_prev=AVAIL
[ENTRY OK] CALL score=95/50 [NORMAL] HIGH
[TRADE OPENED] CALL premium=150 qty=50
[TRADE CLOSED] CALL +2300 profit
```

**Bad Indicators (You should NOT see if fix is working):**
```
[SIGNAL CHECK] score=0/50 side=None  <-- THIS MEANS FIX NOT WORKING
[SIGNAL BLOCKED] Score too low
```

### Step 3: Analyze Logs

Extract indicator build logs:
```bash
tail -200 logs/trading_engine_replay_2026-02-20.log | grep "\[INDICATORS BUILT\]"
```

Expected: Multiple lines showing indicators populated

Extract score breakdown:
```bash
tail -200 logs/trading_engine_replay_2026-02-20.log | grep "\[SCORE BREAKDOWN v5\]" | head -3
```

Expected: Shows all 8 scorers contributing, indicators marked as OK/NARROW/etc

Extract entry firing:
```bash
tail -200 logs/trading_engine_replay_2026-02-20.log | grep "\[ENTRY OK\]"
```

Expected: Multiple entries, score values > 50, different sides (CALL/PUT)

### Step 4: Check Trade Performance

Expected metrics after 1 hour replay:
- Number of trades: 3-5
- Win rate: ≥ 50%
- Avg winning trade: +300-500
- Avg losing trade: -200-400

Example log entries:
```
14:15:30 [ENTRY OK] CALL score=92/50 zone=NORMAL strength=HIGH
14:15:31 [TRADE OPENED] CALL entry=150 stop=120 target=190
14:16:45 [TRADE CLOSED] CALL result=+40 reason=TARGET_HIT
     ↓ Profit = +40 * 50 qty = +2000
```

---

## PHASE 3: PAPER MODE DEPLOYMENT (2-4 hours)

### Step 1: Enable PAPER Mode
Edit config.py:
```python
MODE = "PAPER"  # Set from REPLAY to PAPER
```

### Step 2: Start Trading Engine
```bash
python main.py
```

### Step 3: Monitor First 30 Minutes
Every 5 minutes, check logs:
```bash
tail -50 logs/trading_engine_paper_$(date +%Y-%m-%d).log
```

Watch for:
1. `[INDICATORS BUILT]` messages (appears every bar)
2. `[ENTRY OK]` messages (should appear multiple times per hour)
3. `[TRADE OPENED]` messages (signal → execution)
4. `[TRADE CLOSED]` messages (exit execution)

### Step 4: Check for Errors

Search for errors:
```bash
grep -i "error\|exception\|traceback" logs/trading_engine_paper_*.log
```

Should return: Nothing (or only non-critical warnings)

Search for blocked signals:
```bash
grep "SCORE\|BLOCKED" logs/trading_engine_paper_*.log | tail -10
```

Should show: Score values > 50, not "Score too low"

### Step 5: Exit After 2 Hours (minimum) or 4 Hours (ideal)

Stop trading engine (Ctrl+C)

### Step 6: Analyze PAPER Results

```bash
# Count total entries
grep "\[ENTRY OK\]" logs/trading_engine_paper_*.log | wc -l

# Count wins
grep "CLOSED.*result=+\|CLOSED.*PROFIT" logs/trading_engine_paper_*.log | wc -l

# Count losses  
grep "CLOSED.*result=-\|CLOSED.*LOSS" logs/trading_engine_paper_*.log | wc -l

# Average entry score
grep "\[ENTRY OK\]" logs/trading_engine_paper_*.log | grep -oP 'score=\K[0-9.]+' | awk '{s+=$1; count++} END {print "Avg:", s/count}'
```

Expected Results:
- Entries: 3-8 in 2 hours
- Win rate: ≥ 50%
- Entry score: 60-95 (not 0)
- Side determination: Mix of CALL and PUT (not all None)

---

## PHASE 4: LIVE MODE READINESS (Optional)

### Checklist Before Going LIVE:

- [ ] REPLAY mode: ≥ 3 trades/hour with ≥ 50% win rate
- [ ] PAPER mode: Minimum 2 hours, ≥ 50% win rate, no errors
- [ ] Logs show: `[INDICATORS BUILT]` every bar
- [ ] Logs show: `[SCORE BREAKDOWN v5]` with 8 scorers
- [ ] Logs show: `[SCORE BREAKDOWN v5]` indicators marked as OK/NARROW/WIDE/etc (not empty)
- [ ] Entries show: score > 50 (not score=0)
- [ ] Entries show: side=CALL or PUT (not side=None)
- [ ] Max profit/loss per trade: Within risk parameters

### Deploy to LIVE:
```python
MODE = "LIVE"  # Set from PAPER to LIVE
```

### LIVE Monitoring (First Hour):
```bash
tail -f logs/trading_engine_live_$(date +%Y-%m-%d).log | grep "\[INDICATORS BUILT\]\|\[ENTRY OK\]\|\[TRADE"
```

Expected to see:
- Entry signals firing
- Trades opening
- Trades closing with profit/loss
- No errors or exceptions

---

## Diagnostic: If Score Still Shows as 0

### Check 1: Verify Missing Indicator NOT THERE

Run this Python snippet:
```python
of signals import detect_signal
import json

# Get a signal (this will call detect_signal internally)
# Check what's in the indicators dict by looking at raw function output

# Alternative: Check signals.py directly
with open("signals.py") as f:
    content = f.read()
    if "momentum_ok_call" in content:
        print("Fix is present")
    else:
        print("Fix NOT found - re-run implement")
```

### Check 2: ATR Regime Gate

If score=0 and indicators show OK, might be ATR blocking:
```bash
grep "ATR regime" logs/trading_engine_*.log | tail -5
```

Expected: "ATR regime: OK" or "ATR regime: HIGH"
Problematic: "ATR regime: LOW" (blocks entry)

Solution: ATR is ≤15, wait for market volatility to increase

### Check 3: RSI Hard Gate

```bash
grep "RSI hard\|RSI directional" logs/trading_engine_*.log | tail -5
```

Expected: "RSI check: PASS" or indicator_name present
Problematic: "RSI check: FAIL" (oversold/overbought)

Solution: Wait for RSI to normalize (30 < RSI < 75)

### Check 4: Time Gate

```bash
grep "\[TIME GATE\]" logs/trading_engine_*.log | tail -5
```

Expected: Should NOT see this log
Problematic: See this log = time-restricted zone

Solution: Wait until allowed trading hours (9:45 - 14:55)

### Check 5: Missing Candle Data

```bash
grep "candles =" logs/trading_engine_*.log | tail -3
```

Expected: "candles = 20" (min 20 bars for momentum calc)
Problematic: "candles = 2" or "candles < 5"

Solution: Wait for enough historical data

---

## Common Issues & Fixes

| Issue | Log Message | Fix |
|-------|-------------|-----|
| Fix not deployed | score=0/50, side=None | Check grep "momentum_ok_call" signals.py |
| ATR too low | "[ENTRY GATE] ATR=12 regime=LOW" | Wait for volatility |
| RSI oversold | "[ENTRY GATE] RSI=28 gate=OVERSOLD" | Wait for RSI normalization |
| Time restricted | "[TIME GATE] hour=12 zone=LUNCH" | Wait for 12:20 or 14:00 |
| Insufficient data | "candles=3 insufficient" | Wait for 20+ bars |
| Cpr_width failing | "[Indicator Failed] CPR error" | Check cpr_levels computing |
| Momentum_ok failing | "Momentum check error" | Requires ≥20 candles + volume |

---

## Success Indicators

### You'll Know It's Working When:

✓ See `[INDICATORS BUILT]` every bar showing 5 new indicators

✓ See `[SCORE BREAKDOWN v5]` with all 8 scorers listed

✓ See `[ENTRY OK]` messages with score > 50

✓ See side determination shows CALL or PUT (not None)

✓ See multiple trades per hour (3-5 target)

✓ See win rate ≥ 50% consistently

### You'll Know There's Still a Problem If:

✗ See score=0/50 side=None

✗ See "[SIGNAL BLOCKED] Score too low"

✗ See no `[INDICATORS BUILT]` logs

✗ See `[SCORE BREAKDOWN v5]` with all scorers = 0/max

✗ See fewer than 1 trade per hour

✗ See win rate < 30%

---

## Test Data Locations

### Replay Test Files:
- 2026-02-20: `signals_NSE_NIFTY50-INDEX_2026-02-20.csv`
- 2026-02-18: `signals_NSE_NIFTY50-INDEX_2026-02-18.csv`
- 2026-02-16: `signals_NSE_NIFTY50-INDEX_2026-02-16.csv`

### Trade Results:
- `trades_NSE_NIFTY50-INDEX_2026-02-20.csv`
- `trades_NSE_NIFTY50-INDEX_2026-02-18.csv`
- `trades_NSE_NIFTY50-INDEX_2026-02-16.csv`

---

## Timeline Summary

| Phase | Duration | Mode | Success Criteria |
|-------|----------|------|------------------|
| 1: Import | 5 min | Static | Imports work |
| 2: Replay | 30 min | REPLAY | 3-5 trades, ≥50% win |
| 3: Paper | 2-4 hrs | PAPER | Same + no errors |
| 4: Live | Ongoing | LIVE | Profitable trading |

---

## Rollback Plan

If issues arise, immediately revert:

```bash
# Revert signals.py to backup
git checkout signals.py

# Or manually remove lines 515-580 (indicator computation block)
# And lines 560-576 (new indicators dict entries)
# And line 22 (classify_cpr_width import)
```

Expected behavior after rollback: score=0/50 returns (pre-fix behavior)

---

**Status: Ready for Testing**  
**Recommended: Start with REPLAY validation today, deploy PAPER tomorrow if successful**
