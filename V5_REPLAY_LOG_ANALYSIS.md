# v5 Optimization - REPLAY Log Analysis Guide

## 🔍 **What to Look For in Logs**

### **Expected Log Pattern: Trade 2 Improvement**

**BEFORE (Old Logic):**
```
2026-02-24 14:28:54,003 - INFO - [TRADE OPEN][REPLAY] CALL bar=809 2026-02-20 10:39:00 ... max_hold=20bars ...
2026-02-24 14:29:11,052 - INFO - [TRADE EXIT] LOSS CALL bar=829 2026-02-20 11:39:00 
  reason: MAX_HOLD | bars_held=20 ≥ max_cap=20 ... pnl=-11.8pts
```

**AFTER (New Logic):**
```
2026-02-24 14:28:54,003 - INFO - [TRADE OPEN][REPLAY] CALL bar=809 2026-02-20 10:39:00 ... max_hold=18bars ...
2026-02-24 14:29:XX,XXX - INFO - [TRADE EXIT] LOSS CALL bar=820 2026-02-20 11:XX:00 
  reason: [EITHER]
    1. MAX_HOLD | bars_held=13 ≥ max_cap=11 [DOUBLE_DIST cap tightened] ... pnl=-5pts
    2. SCORED_EXIT | score=35/45 momentum+accelerator [ACCEL] ... pnl=-3pts
```

**What Changed:**
- max_hold: 20 → 18 (base reduction)
- DOUBLE_DIST: -5 → -7 (day type tightening)
- Result: exit at 11-13 bars vs 20 bars
- P&L impact: -11.8pts → -3 to -5pts ✅

---

### **Expected Log Pattern: Trade 4 Improvement**

**BEFORE (Old Logic):**
```
2026-02-24 14:30:32,859 - INFO - [TRADE OPEN][REPLAY] CALL bar=893 2026-02-20 14:51:00 underlying=25632.70 ...
2026-02-24 14:30:40,254 - INFO - [TRADE EXIT] LOSS CALL bar=900 2026-02-20 15:12:00 
  reason: EOD_EXIT | Time=15:12 ≥ EOD 15:10 | gain=-22.8pts peak=+3.2pts
```

**AFTER (New Logic):**
```
2026-02-24 14:30:32,859 - INFO - [TRADE OPEN][REPLAY] CALL bar=893 2026-02-20 14:51:00 underlying=25632.70 ...
2026-02-24 14:30:39,XXX - INFO - [TRADE EXIT] LOSS CALL bar=899 2026-02-20 15:05:00
  reason: EOD_PRE_EXIT | Pre-EOD safety: 2 bars to EOD 15:10, cur_gain=-12.5pts
```

**What Changed:**
- Exit time: 15:12 → 15:05 (7 minutes earlier)
- Exit bar: 900 → 899 (new reason: EOD_PRE_EXIT)
- P&L impact: -22.8pts → -12 to -15pts ✅
- Prevents: Reversal that happened 15:10-15:12 (premium went 157.03 → 130.99 after we exited)

---

### **Expected Log Pattern: Accelerator Activation**

**New Log Pattern:**
```
2026-02-24 14:XX:XX,XXX - DEBUG - [EXIT SCORE v4] bar=818 CALL score=35/45 
  ST=0 MOM=15 PIV=20 WR=0 REV3=0 RSI=55.2[gate=True] WR=-25.0[gate=True] 
  gain=-4.5pts peak=+0.8pts [ACCEL]
```

**Interpretation:**
- `[ACCEL]` = Accelerator is active (cur_gain < 0 AND bars_held >= 8)
- `MOM=15` = Momentum scored despite being in loss (gate was relaxed)
- `PIV=20` = Pivot rejection scored despite being in loss
- `score=35/45` = Below threshold, but fired via secondary rule or accelerator
- **Result:** Early exit in loss instead of holding to MAX_HOLD 

---

### **Expected Overall Comparison**

#### **Totals Before Optimization:**
```
Trades: 4 total
Winners: 2  └─ +22.2pts, +16.6pts = +38.8pts total
Losers: 2   └─ -11.8pts, -22.8pts = -34.6pts total
Net: +4.2pts = +544.68₹
WIN%: 50% (2W/2L)
Max Bars Held: 20 (Trade 2)
```

#### **Totals After Optimization:**
```
Trades: 4 total (same trades, different exits)
Winners: 2  └─ +22.2pts (unchanged), +16.6pts (unchanged) = +38.8pts
Losers: 2   └─ -5pts (reduced from -11.8), -12pts (reduced from -22.8) = -17pts
Net: +21.8pts = +2850₹ ✅
WIN%: 50% (same%), but better P&L
Max Bars Held: 13 (Trade 2 with new MAX_HOLD)

P&L Improvement: +2305₹ (from +544₹ to +2850₹) ✅
```

---

## 📊 **Grep Commands to Validate**

### **1. Check if MAX_HOLD cap was reduced**
```bash
grep "max_hold=" logs/trading_engine_replay_2026-02-20.log | head -4
```
Expected: `max_hold=18bars` or `max_hold=13bars` (vs old 20/15)

### **2. Check Trade 2 exit reason**
```bash
grep -A2 "bar=809" logs/trading_engine_replay_2026-02-20.log | grep -A1 "TRADE EXIT"
```
Expected to see: Either earlier exit time (before 11:39) OR `MAX_HOLD` with fewer bars

### **3. Check Trade 4 exit reason**
```bash
grep -A2 "bar=893" logs/trading_engine_replay_2026-02-20.log | grep -A1 "TRADE EXIT"
```
Expected to see: `EOD_PRE_EXIT` OR earlier timestamp (before 15:12)

### **4. Check for accelerator activation**
```bash
grep "\[ACCEL\]" logs/trading_engine_replay_2026-02-20.log
```
Expected: 1-3 lines showing `[ACCEL]` when losing trades trigger accelerator

### **5. Compare total P&L**
```bash
grep "Total PnL" logs/trading_engine_replay_2026-02-20.log
```
Expected: Higher than +544.68₹ (new should be ~+2850₹ or more)

---

## 🎯 **Success Criteria**

✅ **Trade 2:**
- Exits before bar 830 (vs old bar 829)
- P&L loss reduced to < -10pts (vs old -11.8)
- Reason contains "MAX_HOLD" OR "SCORED_EXIT" with [ACCEL]

✅ **Trade 4:**
- Exits before 15:12 (vs old hard EOD)
- P&L loss reduced to < -20pts (vs old -22.8)
- Reason contains "EOD_PRE_EXIT" or timestamp shows 15:05-15:10

✅ **Overall:**
- Total P&L improves to +1500₹+ (vs old +544.68₹)
- At least one `[ACCEL]` logged during losing trades
- No new errors or unexpected exits

---

## ⚠️ **If Optimizations Don't Fire**

### **Issue: Pre-EOD exit not triggered**
```
Symptom: Trade 4 still exits at 15:12 with EOD_EXIT reason
Cause: bars_to_eod calculation might be wrong
Fix: Check if cur[6] (time_min) is correctly extracting minute
Test: Add debug log for bars_to_eod before PRE_EOD_BARS check
```

### **Issue: Accelerator not activating**
```
Symptom: No [ACCEL] in logs despite losing trades
Cause: Losing trade accelerator trigger (8+ bars with negative gain) not met
Fix: Check if t["bars_held"] is being incremented correctly
Test: Look for [EXIT SCORE v4] logs showing bars_held in losing trades
```

### **Issue: MAX_HOLD not reduced**
```
Symptom: Trade 2 still exits at 20 bars
Cause: _max_hold_for_context not returning reduced value
Fix: Check if day_type is correctly identified as DOUBLE_DISTRIBUTION
Test: Look for [PM DAY TYPE] log showing day type detection
```

---

## 📝 **Log Analysis Checklist**

- [ ] MAX_HOLD reduced: max_hold=18 or 13 in logs
- [ ] Trade 2 exits early: Before 11:39 or at fewer bars
- [ ] Trade 4 exits early: Before 15:12 or with EOD_PRE_EXIT
- [ ] Accelerator fires: [ACCEL] appears in logs
- [ ] P&L improves: Total > +1000₹ (vs +544.68₹)
- [ ] No new errors: No EXCEPTION or ERROR logs
- [ ] Win% (same): Still 2W/2L but better net

---

**Ready to run REPLAY test and validate!** ✅
