# v5 SCORING FIX - DEPLOYMENT CHECKLIST

**Created:** 2026-02-24  
**Goal:** Safely validate and deploy the scoring engine fix through REPLAY → PAPER → LIVE pipeline  

---

## PRE-DEPLOYMENT VALIDATION

- [ ] Read IMPLEMENTATION_SUMMARY.md (overview)
- [ ] Read V5_SCORING_FIX_COMPLETE.md (technical details)
- [ ] Read V5_FIX_QUICK_REFERENCE.md (quick ref)
- [ ] Read V5_TESTING_GUIDE.md (testing procedures)

---

## PHASE 1: IMPORT & CODE VALIDATION (5 minutes)

### Verify Fix Is Installed
- [ ] Run: `grep "momentum_ok_call" signals.py` → Should return 2 matches
- [ ] Run: `grep "from indicators import.*classify_cpr_width" signals.py` → Should find import
- [ ] Run: `grep '"momentum_ok_call": mom_ok_call' signals.py` → Should find dict entry

### Verify Imports Work
```bash
# Run this command:
python -c "from signals import detect_signal; from entry_logic import check_entry_condition; from indicators import classify_cpr_width, momentum_ok; print('OK')"
```
- [ ] Expected output: `OK`
- [ ] No ImportError or ModuleNotFoundError

### Verify No Syntax Errors
```bash
# Run this command:
python -m py_compile signals.py entry_logic.py indicators.py
```
- [ ] No syntax errors printed
- [ ] All 3 files compile successfully

---

## PHASE 2: REPLAY MODE TESTING (30 minutes)

### Setup REPLAY
```bash
# Edit config.py if needed
MODE = "REPLAY"
SYMBOL = "NSE:NIFTY50-INDEX"
REPLAY_DATE = "2026-02-20"
```
- [ ] Config updated to REPLAY mode
- [ ] Date selected (recommend 2026-02-20 for known good data)

### Start REPLAY
```bash
python main.py
```
- [ ] Start time: Note the start time here: ___________
- [ ] No errors in first 30 seconds of startup
- [ ] Logs directory created: logs/
- [ ] See "Starting REPLAY mode" message

### Monitor Logs (Every 5 minutes during replay)

**What to Search For:**
```bash
tail -50 logs/trading_engine_replay_*.log
```

During first few bars, look for:
- [ ] `[INDICATORS BUILT]` message with MOM_CALL, MOM_PUT, CPR, ET, RSI_prev
- [ ] `[SCORE BREAKDOWN v5]` message with all indicators and scorers
- [ ] NO `score=0/50 side=None` messages
- [ ] NO `score too low` messages

After 10-15 bars, should see:
- [ ] First `[ENTRY OK]` message with score > 50
- [ ] Side determination shows CALL or PUT (not None)
- [ ] `[TRADE OPENED]` message

### Check Results

**Entry Count:**
```bash
grep "\[ENTRY OK\]" logs/trading_engine_replay_2026-02-20.log | wc -l
```
- [ ] Count: _____ entries
- [ ] Expected: 3-5 minimum

**Score Values:**
```bash
grep "\[ENTRY OK\]" logs/trading_engine_replay_2026-02-20.log | head -3
```
- [ ] All show score > 50
- [ ] Example: `score=95/50` or `score=72/50`
- [ ] NO `score=0/50`

**Side Determination:**
```bash
grep "\[ENTRY OK\]" logs/trading_engine_replay_2026-02-20.log | head -3
```
- [ ] Show mix of CALL and PUT entries
- [ ] NO `side=None` entries

**Win Rate:**
```bash
# Count total closed trades
grep "\[TRADE CLOSED\]" logs/trading_engine_replay_2026-02-20.log | wc -l

# Count winners (look for profit/positive)
grep "\[TRADE CLOSED\].*profit\|+[0-9]" logs/trading_engine_replay_2026-02-20.log | wc -l

# Count losers (look for loss/negative)
grep "\[TRADE CLOSED\].*loss\|-[0-9]" logs/trading_engine_replay_2026-02-20.log | wc -l
```
- [ ] Total trades: _____
- [ ] Winners: _____
- [ ] Losers: _____
- [ ] Win rate: _____ % (should be ≥50%)

### REPLAY Results

- [ ] ✓ PASS: 3+ entries, score > 50, valid sides, ≥50% win rate
- [ ] ✗ FAIL: 0 entries, score=0, no sides, or <50% win rate
  
**If FAIL:** See troubleshooting in V5_TESTING_GUIDE.md

---

## PHASE 3: PAPER MODE DEPLOYMENT (2-4 hours)

### Setup PAPER
```bash
# Edit config.py
MODE = "PAPER"  # Change from "REPLAY" to "PAPER"
```
- [ ] Config updated to PAPER mode

### Start PAPER Trading
```bash
python main.py
```
- [ ] Start time: ___________
- [ ] No errors at startup
- [ ] Connected to Fyers API (PAPER account)
- [ ] See "Entering PAPER mode" confirmations

### Monitor First 15 Minutes
```bash
tail -20 logs/trading_engine_paper_$(date +%Y-%m-%d).log
```

Check for:
- [ ] `[INDICATORS BUILT]` messages appearing
- [ ] `[ENTRY OK]` messages appearing
- [ ] `[TRADE OPENED]` messages (actual paper orders)
- [ ] NO errors or exceptions
- [ ] NO "Connection failed" messages

### Monitor Every 30 Minutes

```bash
# Get latest entries
tail -50 logs/trading_engine_paper_$(date +%Y-%m-%d).log | grep "\[ENTRY OK\]\|\[TRADE"

# Check for errors
grep "ERROR\|EXCEPTION" logs/trading_engine_paper_$(date +%Y-%m-%d).log

# Check score values
grep "\[ENTRY OK\]" logs/trading_engine_paper_$(date +%Y-%m-%d).log | tail -1
```

- [ ] Entries continuing to appear (not slowing down)
- [ ] Score values reasonable (60-95)
- [ ] No error messages appearing
- [ ] Trades opening and closing

### Stop After 2-4 Hours

Press Ctrl+C to gracefully stop
- [ ] Engine shutting down without errors
- [ ] All trades closed or halted

### Analyze PAPER Results

```bash
# Count entries this session
grep "\[ENTRY OK\]" logs/trading_engine_paper_$(date +%Y-%m-%d).log | wc -l

# Check average score
grep "\[ENTRY OK\]" logs/trading_engine_paper_$(date +%Y-%m-%d).log | grep -o "score=[0-9]*" | cut -d= -f2 | awk '{sum+=$1; count++} END {print "Avg score:", sum/count}'

# Check for errors
grep "error\|ERROR\|except" logs/trading_engine_paper_$(date +%Y-%m-%d).log | wc -l
```

- [ ] Entries: _____ (should be 3-8 in 2-4 hours)
- [ ] Avg score: _____ (should be 60-85)
- [ ] Errors: _____ (should be 0)

### PAPER Results

- [ ] ✓ PASS: 6+ entries, 60-85 avg score, 0 errors, ≥50% win rate
- [ ] ✗ FAIL: <3 entries, low scores, errors, or <50% win rate

**If FAIL:** 
1. Check logs for [INDICATORS BUILT] - if missing, score engine broken
2. Check for ATR gate messages - if "ATR=LOW", market lacks volatility
3. Check for RSI gate messages - if "RSI out of range", wait for normalization
4. Contact support with log excerpt

**If PASS:** Ready for LIVE testing

---

## PHASE 4: LIVE MODE READINESS

### Final Checklist Before LIVE

- [ ] REPLAY validation: ✓ Passed (3+ trades, ≥50% win)
- [ ] PAPER validation: ✓ Passed (6+ entries, 0 errors)
- [ ] No error messages in PAPER logs
- [ ] Profit/loss per trade: Within risk limits
- [ ] Order execution: Fast (< 2 seconds)
- [ ] Side determination: Consistent (CALL/PUT not None)

### LIVE Deployment

Edit config.py:
```python
MODE = "LIVE"
ACCOUNT_TYPE = "LIVE"  # Or whatever LIVE account identifier
```

- [ ] Config updated to LIVE mode
- [ ] Double-checked account setting
- [ ] Backup of config.py created
- [ ] Risk limits reviewed

### LIVE Monitoring (First 30 minutes)

```bash
tail -f logs/trading_engine_live_$(date +%Y-%m-%d).log
```

Watch for:
- [ ] `[INDICATORS BUILT]` - appearing normally
- [ ] `[ENTRY OK]` - signals firing
- [ ] `[TRADE OPENED]` - real orders executing
- [ ] `[TRADE CLOSED]` - exits properly
- [ ] NO errors or exceptions

### LIVE Validation

Targets after first hour LIVE:
- [ ] 1-3 real trades executed
- [ ] Profit/loss: Positive or within expected range
- [ ] No execution errors
- [ ] Order fills in < 2 seconds

---

## MONITORING CHECKLIST

### Daily Monitoring

- [ ] Check logs for starts with no errors
- [ ] Verify `[INDICATORS BUILT]` appearing regularly
- [ ] Confirm `[ENTRY OK]` messages when expected
- [ ] Track P&L
- [ ] No unexpected error messages

### Weekly Monitoring

- [ ] Review win rate (should stay ≥50%)
- [ ] Review avg profit per trade
- [ ] Check for pattern of errors
- [ ] Verify no degradation in signal quality

### Monthly Monitoring

- [ ] Review overall P&L
- [ ] Compare to baseline (before fix)
- [ ] Consider any strategy adjustments
- [ ] Plan next optimization phase

---

## TROUBLESHOOTING QUICK REFERENCE

| Issue | Indicator | Next Step |
|-------|-----------|-----------|
| No entries | score=0/50 | Check grep for fix, run verify_scoring_fix.py |
| Low entry rate | <1 per hour | Check ATR, RSI, time gates in logs |
| High loss rate | Win% <50% | Check CPR_width, entry_type in breakdown |
| Order failures | "order rejected" | Check position limits, margin |
| Connection errors | "API error" | Check Fyers account, network |
| Slow execution | >5 sec to fill | Check market liquidity, order size |

Full troubleshooting in: V5_TESTING_GUIDE.md → "Diagnostic: If Score..." section

---

## EMERGENCY ROLLBACK

If critical issues:

```bash
# Stop trading engine (Ctrl+C if running)

# Revert signals.py
git checkout signals.py

# Restart
python main.py
```

Expected behavior: Reverts to pre-fix (score=0, no entries)

---

## Documentation Cross-Reference

| Question | Document | Section |
|----------|----------|---------|
| What changed? | IMPLEMENTATION_SUMMARY.md | "What Changed" |
| How does it work? | V5_SCORING_FIX_COMPLETE.md | "Implementation Details" |
| How to verify? | V5_FIX_QUICK_REFERENCE.md | "Verification From Code" |
| How to test? | V5_TESTING_GUIDE.md | All 4 phases |
| What went wrong? | V5_TESTING_GUIDE.md | "Common Issues & Fixes" |

---

## SIGN-OFF

**Prepared By:** System  
**Prepared On:** 2026-02-24  
**Status:** Ready for Deployment  
**Next Action:** Run REPLAY mode test today  

**Checkpoints:**
- [ ] REPLAY: PASS _____ (date/time)
- [ ] PAPER: PASS _____ (date/time)
- [ ] LIVE: PASS _____ (date/time)

---

**For Questions:** See V5_TESTING_GUIDE.md or IMPLEMENTATION_SUMMARY.md
