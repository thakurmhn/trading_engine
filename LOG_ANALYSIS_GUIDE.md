# Log Analysis Quick Reference
**For Troubleshooting Signal & Exit Issues**

---

## 🔍 Problem: `[SIGNAL CHECK] score=0/50 side=None`

### Step 1: Check if indicators are populated

```bash
grep "\[INDICATOR DF\]" trading_engine.log | head -1
```

**Should show:**
```
[INDICATOR DF] NSE:NIFTY50-INDEX 3m ema9=25461.0 ema13=25456.25 
adx14=27.01 cci20=203.35 rsi14=61.68 ... supertrend_bias=UP slope=UP
```

**If missing:** Data not flowing → check market_data.py or execution.py

---

### Step 2: Check if indicators dict is built

```bash
grep "\[INDICATORS BUILT\]" trading_engine.log | tail -5
```

**Should show:**
```
[INDICATORS BUILT] MOM_CALL=True MOM_PUT=False CPR=NARROW ET=BREAKOUT RSI_prev=61.5
```

**If missing or False values:** Restored indicators not working → check signals.py lines 515-584

---

### Step 3: Check entry scoring start

```bash
grep "\[ENTRY SCORING v5 START\]" trading_engine.log | tail -1
```

**Should show:**
```
[ENTRY SCORING v5 START] regime=NORMAL base_threshold=50 
ST_15m=BULLISH ST_3m=BULLISH RSI=61.7
```

**If missing:** Entry logic not called → check signals.py check_entry_condition() call

---

### Step 4: Check why each side was blocked

```bash
grep "\[DEBUG SIDE\]" trading_engine.log
```

**Should show something like:**
```
[DEBUG SIDE][CALL BLOCKED] RSI_DIRECTIONAL: RSI=45.0<50 (no bullish momentum)
[DEBUG SIDE][PUT BLOCKED] RSI_DIRECTIONAL: RSI=45.0>50 (no bearish momentum block)
```

**Or:**
```
[DEBUG SIDE DECISION] CHOSEN=CALL best_score=83 threshold=50 
RSI=61.7 ST_15m=BULLISH ST_3m=BULLISH
```

**If both blocked:** Both sides filter out → check gating logic in entry_logic.py

---

### Step 5: Check score breakdown

```bash
grep "\[SCORE BREAKDOWN v5\]" trading_engine.log | tail -1
```

**Should show:**
```
[SCORE BREAKDOWN v5][CALL] 83/50 | Indicators: MOM=OK CPR=NARROW ET=BREAKOUT RSI_prev=AVAIL | 
ST=20/20 RSI=10/10 CCI=15/15 VWAP=10/10 PIV=8/15 MOM=15/15 CPR=5/5 ET=0/5
```

**Check each component:**
- `ST=20/20` → Trend alignment working ✓
- `RSI=10/10` → RSI scorer working ✓
- `MOM=15/15` → Momentum indicator working ✓
- `CPR=5/5` → CPR width working ✓
- Any component =0/max? → That dimension scoring zero

**If all zero:** Pre-filter blocked before scoring → check [ENTRY BLOCKED] reason

---

### Step 6: Check pre-filter blocks

```bash
grep -E "\[ENTRY BLOCKED\]|\[ENTRY SCORING.*blocked\]" trading_engine.log
```

**Common blocks:**
```
[ENTRY BLOCKED][CALL] RSI_DIRECTIONAL RSI=45.0<50
[ENTRY SCORING v5 START] reason=Regime blocked: LOW ATR=5.2
[ENTRY SCORING v5 START] reason=PRE_OPEN
[ENTRY SCORING v5 START] reason=LUNCH_CHOP
```

**Solutions:**
- ATR LOW: Wait for volatility to pick up
- PRE_OPEN: Market not open yet (9:30+ IST)
- LUNCH_CHOP: Trading banned 12:00-12:20
- RSI DIRECTIONAL: RSI not aligned with intended side

---

## 🚀 Problem: Trades exiting too early (scored exits)

### Check exit scoring

```bash
grep "\[EXIT SCORE v4\]" trading_engine.log
```

**Should show:**
```
[EXIT SCORE v4] bar=818 CALL score=45/45 
ST=0 MOM=25 PIV=20 WR=0 REV3=0 
RSI=51[gate=True] WR=-25.0[gate=True] 
gain=-8.0pts peak=+12.6pts [ACCEL]
```

**Interpretation:**
- `score=45/45`: Perfectly at threshold → will fire
- `MOM=25`: Momentum contributor present
- `PIV=20`: Pivot rejection firing
- `[ACCEL]`: Accelerator is active (losing trade gate relaxation)
- `gate=True` means gate allows the exit

**If firing when shouldn't:**
- Check if score should be below 45
- Check if gating should block this (RSI/WR gates)

---

### Check if accelerator is activating

```bash
grep "\[TRADE DIAGNOSTICS\]\[ACCELERATOR" trading_engine.log
```

**Should show (when losing):**
```
[TRADE DIAGNOSTICS][ACCELERATOR ACTIVE] bar=818 CALL 
gain=-8.0pts peak=+12.6pts bars_held=8 
RSI_relaxed=True WR_relaxed=True
```

**If not showing:** Accelerator not firing → check trigger condition:
```python
losing_trade_accelerator = (cur_gain < 0 and t["bars_held"] >= 8)
```

---

## 💰 Problem: Trade losses larger than expected

### Check if trade held too long

```bash
grep "\[TRADE EXIT\].*LOSS" trading_engine.log | tail -3
```

**Should show:**
```
[TRADE EXIT] LOSS CALL bar=818 2026-02-20 11:06:00 
prem 153.60→145.63 P&L=-7.97pts held=8bars
```

**Good signs:**
- `held=8bars`: Short hold → v5 MAX_HOLD reduction working
- Reason contains `SCORED_v4`: Indicator exit fired → v4 logic working
- Reason contains `PRE_EOD`: Early exit → v5 pre-EOD working

**Bad signs:**
- `held=20bars`: MAX_HOLD fired unchanged → v5 optimization not active
- Reason contains `EOD_EXIT` at 15:12: Held to hard stop → pre-EOD not active
- Reason contains `trail_stop`: Trailed into loss

---

### Check if trade improved

```bash
grep "\[TRADE IMPROVED\]" trading_engine.log
```

**Should show:**
```
[TRADE IMPROVED] CALL reduced loss: bar_stayed=8 
(early exit via v5 logic) loss=-7.97pts (optimization active)
```

**If no [TRADE IMPROVED] logs:** v5 optimizations may not be active

---

## 📊 Performance Analysis

### Count trades by type

```bash
grep "\[TRADE EXIT\]" trading_engine.log | grep -c "WIN"   # Winners
grep "\[TRADE EXIT\]" trading_engine.log | grep -c "LOSS"  # Losers
```

### Analyze exit reasons

```bash
grep "\[TRADE EXIT\]" trading_engine.log | sed 's/.*reason: //' | sort | uniq -c
```

**Should show mix of:**
```
  2 PARTIAL_EXIT | ul_move=...     ← Winners using partial exit
  2 SCORED_v4 | score=45/45        ← Scored exits
  1 EOD_EXIT | Time=15:12          ← Hard EOD stop
  1 MAX_HOLD | bars_held=18        ← MAX_HOLD cap hit
```

### Average bars held

```bash
grep "\[TRADE EXIT\]" trading_engine.log | sed 's/.*held=//' | sed 's/bars.*//' | 
awk '{sum+=$1; count++} END {print "Avg bars held:", sum/count}'
```

**Expected v5:**
- Winners: 5-11 bars (partial exit)
- Losers: 8-18 bars (early scored exit)
- Average: ~10 bars

**If average >15 bars:** MAX_HOLD not reducing → check constant at line 171

---

## 🔧 Debug Mode (grep patterns)

### Show all scoring details for one bar

```bash
grep "bar=800" trading_engine.log
```

### Show all side decision logs

```bash
grep "\[DEBUG SIDE" trading_engine.log
```

### Show all indicator calculations

```bash
grep "\[INDICATORS BUILT\]" trading_engine.log
```

### Show trade lifecycle

```bash
grep "CALL.*2026-02-20 10:42:00" trading_engine.log
```

---

## 📋 Log Levels Reference

| Pattern | Level | Meaning |
|---------|-------|---------|
| `[SIGNAL CHECK]` | INFO | Every bar signal evaluation |
| `[SIGNAL FIRED]` | INFO | Signal passing scoring |
| `[SIGNAL BLOCKED]` | INFO | Signal near-miss (within 15pts) |
| `[ENTRY OK]` | INFO | Entry passed, showing full breakdown |
| `[TRADE OPEN]` | INFO | Entry executed |
| `[TRADE EXIT]` | INFO | Exit executed with P&L |
| `[TRADE IMPROVED]` | INFO | v5 optimization reduced loss |
| `[EXIT SCORE v4]` | DEBUG | Per-bar exit scoring details |
| `[DEBUG SIDE]` | DEBUG | Side selection details |
| `[DEBUG SIDE DECISION]` | DEBUG | Final side choice |
| `[TRADE DIAGNOSTICS]` | INFO | Accelerator activation |
| `[INDICATORS BUILT]` | DEBUG | Indicator dict population |

---

## 🎯 Verification Checklist

- [ ] `[INDICATORS BUILT]` shows MOM_CALL/Put, CPR, ET, RSI_prev
- [ ] `[SCORE BREAKDOWN v5]` shows all 8 components
- [ ] `[SIGNAL CHECK]` shows side=CALL or PUT (not None)
- [ ] `[SIGNAL FIRED]` exists for expected trades
- [ ] `[EXIT SCORE v4]` shows scores at/above 45 before exit
- [ ] `[TRADE IMPROVED]` shows loss reductions for losing trades
- [ ] `[TRADE DIAGNOSTICS][ACCELERATOR]` shows on losing trades
- [ ] Trades held <12 bars average (v5 working)
- [ ] Losses reduced vs pre-v5 baseline
- [ ] P&L shows improvement trend

---

**Generated:** 2026-02-24  
**For Use With:** v5 Exit Logic + Enhanced Diagnostics  
**Last Updated:** Entry, Exit, Optimization logs validated ✅
