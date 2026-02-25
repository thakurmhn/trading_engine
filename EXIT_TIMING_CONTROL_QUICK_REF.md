# EXIT TIMING CONTROL: Quick Reference
## Minimum Bar Hold Logic (3–5 bars)

---

## PROBLEM SOLVED

**Before Fix:**
```
[ENTRY] CALL NSE:NIFTY2630225500CE @ 218.95  [Bar 0]
[EXIT] TARGET_HIT CALL NSE:NIFTY2630225500CE @ 235.20  [30 seconds later]
PnL = 2,112.50 (hit at bar 0.5)
```
❌ Exit happens **immediately** — strategy designed for 3–5 bar hold

**After Fix:**
```
[ENTRY] CALL NSE:NIFTY2630225500CE @ 218.95  [Bar 0]
[EXIT DEFERRED] PT target hit before min bars (1 < 3)... [Bar 1, 30 sec]
[EXIT DEFERRED] PT target hit before min bars (2 < 3)... [Bar 2, 1 min]
[EXIT] TARGET_HIT CALL NSE:NIFTY2630225500CE @ 240.30  [Bar 3, 3 min]
PnL = 2,779.50 (hit at bar 3, enforced hold window)
```
✅ Exit deferred until bar 3 — **holding period enforced**

---

## THE RULE

```
MIN_BARS_FOR_PT_TG = 3  (always enforced)

┌─────────────────────────────────────────────┐
│ Exit Type    │ Timing      │ Example         │
├──────────────┼─────────────┼─────────────────┤
│ SL_HIT       │ Immediate   │ Bar 0, 1, 2,... │
│ PT_HIT       │ Bar 3+      │ Hit bar 1?      │
│ TG_HIT       │ Bar 3+      │ Hit bar 2? → Def│
│ Trailing     │ Bar 3+      │ Enable at bar 3 │
│ Oscillator   │ Bar 3+      │ Defer if early  │
│ Reversal     │ Bar 3+      │ Defer if early  │
│ Supertrend   │ Bar 3+      │ Defer if early  │
└─────────────────────────────────────────────┘
```

---

## HOW IT WORKS

### Entry (Bar 0)
```python
entry_candle = len(candles_df) - 1  # Index of entry candle
state["entry_candle"] = entry_candle
```

### Exit Check (Every Bar)
```python
bars_held = current_index - entry_candle

if bars_held < 3:
    if condition_is_SL:
        return True, "SL_HIT"  # ✅ Exit immediately
    elif condition_is_PT_or_TG:
        return False, None      # ❌ Defer to next bar
        logging.info("[EXIT DEFERRED] ...waiting for bar 3")
```

### Example Timeline

```
Bar 0 (Entry Time: 9:30:00)
├─ entry_candle = 100
├─ bars_held = 0
└─ [ENTRY] CALL @ 218.95

Bar 1 (9:33:00)
├─ bars_held = 1
├─ current_price = 235.20
├─ PT = 240.05, TG = 258.71, SL = 196.94
├─ 235 >= 240? → Yes, but bars_held=1 < 3
└─ [EXIT DEFERRED] PT hit before min bars (1 < 3)...

Bar 2 (9:36:00)
├─ bars_held = 2
├─ current_price = 242.80
├─ 242.80 >= 240.05? → Yes, but bars_held=2 < 3
└─ [EXIT DEFERRED] PT hit before min bars (2 < 3)...

Bar 3 (9:39:00)
├─ bars_held = 3
├─ current_price = 240.30
├─ 240.30 >= 240.05? → Yes, bars_held=3 >= 3 ✅
└─ [EXIT] TARGET_HIT @ 240.30, bars_held=3
```

---

## LOG SIGNATURES

### When SL hits at Bar 1
```
[EXIT CHECK] CALL NSE:NIFTY2630225500CE bars_held=1 ltp=192.50 SL=196.94 PT=240.05 TG=258.71
[EXIT][SL_HIT] CALL NSE:NIFTY2630225500CE Entry=218.95 Exit=192.50 Qty=130 PnL=-3414.50 BarsHeld=1
```
✅ Exit **IMMEDIATE** despite bars_held=1

### When PT hits at Bar 1 (deferred)
```
[EXIT CHECK] CALL NSE:NIFTY2630225500CE bars_held=1 ltp=235.20 SL=196.94 PT=240.05 TG=258.71
[EXIT DEFERRED] PT target hit before min bars (1 < 3). ltp=235.20 pt=240.05 — defer until bar 3
```
❌ Exit **DEFERRED** until bar 3+

### When PT hits at Bar 3+ (allowed)
```
[EXIT CHECK] CALL NSE:NIFTY2630225500CE bars_held=3 ltp=240.30 SL=196.94 PT=240.05 TG=258.71
[PARTIAL] CALL NSE:NIFTY2630225500CE ltp=240.30 >= pt=240.05 bars_held=3 -> stop locked...
[EXIT][TARGET_HIT] CALL NSE:NIFTY2630225500CE Entry=218.95 Exit=240.30 Qty=130 PnL=2779.50 BarsHeld=3
```
✅ Exit **ALLOWED** at bars_held=3

---

## VALIDATION IN LOGS

**Search for these patterns to confirm exit timing control is working:**

### ✅ GOOD (Fixed behavior)
```
Scenario 1: SL hit immediately
[EXIT][SL_HIT] ... BarsHeld=1  ← Exit at bar 1 (risk protection)

Scenario 2: PT deferred then hit
[EXIT DEFERRED] PT target hit before min bars (1 < 3)...
[EXIT DEFERRED] PT target hit before min bars (2 < 3)...
[EXIT][TARGET_HIT] ... BarsHeld=3  ← Exit at bar 3 only

Scenario 3: Oscillator deferred
[EXIT DEFERRED] Oscillator signal too early (2 < 3)...
later...
[EXIT][OSC] ... BarsHeld=4  ← Exit once bar threshold met
```

### ❌ BAD (Old broken behavior)
```
[EXIT][TARGET_HIT] ... BarsHeld=0  ← NO! (immediate exit)
[EXIT][TARGET_HIT] ... BarsHeld=1  ← NO! (too early)
(no [EXIT DEFERRED] messages)  ← NO! (no deferral tracking)
```

---

## TESTING COMMANDS

### REPLAY Mode (Quick Test)
```bash
python main.py --mode REPLAY --date 2026-02-25
# Watch for [EXIT DEFERRED] messages in logs
```

### PAPER Mode (Real Data)
```bash
python main.py --mode PAPER
# Monitor first 2–3 trades for:
# - SL exits at bar 1–2 (immediate)
# - PT/TG exits at bar 3+ (deferred if hit early)
```

### Log Search
```bash
# Find all exit timing checks
grep "\[EXIT" trades_*.log

# Find deferred exits
grep "DEFERRED" trades_*.log

# Find SL hits (should be any bar)
grep "SL_HIT" trades_*.log

# Count exit types
grep -c "TARGET_HIT" trades_*.log
grep -c "SL_HIT" trades_*.log
grep -c "DEFERRED" trades_*.log
```

---

## CONFIG REFERENCE

**Current Settings:**
```python
# In check_exit_condition():
MIN_BARS_FOR_PT_TG = 3  # Hard-coded in code

# Bars are calculated as:
bars_held = current_candle_index - entry_candle_index
```

**To Change Minimum Hold:**
Edit line in `check_exit_condition()`:
```python
MIN_BARS_FOR_PT_TG = 5  # Change to 5 for more conservative
```
Then redeploy.

---

## EXPECTED BEHAVIOR

### Win Scenario (PT hit at bar 3+)
```
Entry: 218.95 (bar 0)
Bar 1: Price=235.20 → Deferred (1 < 3)
Bar 2: Price=242.80 → Deferred (2 < 3)
Bar 3: Price=240.30 → EXIT TARGET_HIT
Exit: 240.30 (bar 3)
PnL: +2,779.50
Duration: ~3 minutes
```

### Loss Scenario (SL hit at bar 2)
```
Entry: 218.95 (bar 0)
Bar 1: Price=210.00 → No action
Bar 2: Price=192.50 → SL HIT (immediate exit)
Exit: 192.50 (bar 2)
PnL: -3,414.50
Duration: ~2 minutes
```

### Ambiguous Scenario (TG hit before bar 3, SL holds)
```
Entry: 218.95 (bar 0)
Bar 1: Price=245.00 (TG=258.71) → Below TG, deferred
Bar 2: Price=210.00 (SL=196.94) → SL HIT immediately
Exit: 210.00 (bar 2)
PnL: -1,167.50
Duration: ~2 minutes
```
✅ SL protection works even if TG was near

---

## SUMMARY

| Feature | Before | After |
|---------|--------|-------|
| **SL Timing** | Bar 0+ (always) | Bar 0+ (always) |
| **PT Timing** | Bar 0+ (immediate) | Bar 3+ (deferred) |
| **TG Timing** | Bar 0+ (immediate) | Bar 3+ (deferred) |
| **Hold Window** | None (exits instantly) | 3 bars minimum |
| **Logging** | No deferral info | [EXIT DEFERRED] + bars_held |
| **PnL** | Hit quickly | Hit at bar 3+ |
| **Strategy Fit** | Long-hold | Quick scalping (3–5 bars) |

---

**✅ Exit Timing Control Active!**

Now exits respect the 3-bar minimum hold for profit targets while protecting downside with immediate stop-loss execution.

