# Investigation Report: March 2, 2026 — Missed CALL/PUT Moves

## 1. Replay Evidence

### Session Overview

| Metric | Live (Paper) | Replay |
|---|---|---|
| Trades | 8 | 3 |
| Winners | 1 (12.5%) | 1 (33.3%) |
| Net P&L | -70.20 pts | -18.64 pts |
| Survivability (≥3 bars) | 0/8 (0%) | 3/3 (100%) |

### Day Classification

- **Day type:** `TRENDING_DAY` (strong down trend)  
- **CPR width:** `NORMAL` (not NARROW)  
- **Gap:** `NO_GAP` (opening classified as gap-down but tag says NO_GAP — misclassification issue)  
- **Balance:** `OUTSIDE_BALANCE`

### Blocker Distribution (Replay)

| Blocker | Count | % of Blocks |
|---|---|---|
| ST_SLOPE_CONFLICT | 18 | 69% |
| OSC_EXTREME | 6 | 23% |
| ST_CONFLICT | 2 | 8% |

### Key Signals Fired But Not Acted On

- **11 `REVERSAL_SIGNAL` events fired → 0 reversal trades taken**
- **23 `ST_SLOPE_OVERRIDE` events fired → gates opened but `detect_signal` returned `score=0` repeatedly**

**Critical anomaly:**

- `score=0/999` with `gap=+999` appeared on **bars 543–552 (13:27–13:54)**  
- `detect_signal` returned **NO signal at all during the major afternoon breakdown**

---

# 2. Root Cause Analysis

## Root Cause A: Oscillator Gate Too Tight for Trend Days (09:03–09:12)

At the open, the market dropped **~625 points**. The engine correctly identified **PUT** as the direction (`ST_BIAS_OK` passed), but `OSC_EXTREME` blocked every attempt.


09:06 RSI=12.6 threshold=[17.0, 80.0] → BLOCKED (RSI < 17)
09:09 RSI=12.6 threshold=[30.0, 85.0] → BLOCKED (RSI < 30)
09:12 RSI=12.6 threshold=[30.0, 85.0] → BLOCKED (RSI < 30)


Even with **ADX_STRONG_40 tier expansion**, the RSI lower bound (17–30) was still above the actual RSI **12.6**.

The **`OSC_OVERRIDE_PIVOT_BREAK`** fired at **09:03**, but `detect_signal` still returned `score=0`.

**Verdict**

Intentional suppression worked **too aggressively**.  
On a day where **NIFTY dropped 800+ points**, **RSI=12 is trend confirmation, not exhaustion**.

---

## Root Cause B: Persistent 15m Supertrend BULLISH Lag (09:15–14:00)

The **15m Supertrend flipped BULLISH at 09:15** and **never flipped back**, despite price falling from:


24,900 → 24,465


This caused a cascading scoring failure.

### PUT Entries (Correct Side)

- `trend_alignment = 0`
- Both ST timeframes still **BULLISH**

### CALL Entries (Wrong Side)

- `ST_SLOPE_CONFLICT` blocked them
- Slope was **DOWN**, contradicting BULLISH bias

### Result

Neither direction scored high enough.

- PUT scores: **58–62 / 70**
- Missing points: **~15**

The missing **trend_alignment score** was exactly the gap.

---

## Root Cause C: Signal Scorer Returns `score=0` Despite Open Gates

Critical bars:


455
459
543–552


Quality gates passed:

- `ST_BIAS_OK`
- `OSC_OVERRIDE`
- `ST_CONFLICT_OVERRIDE`

But:


detect_signal → score=0
breakdown={}


Meaning:

The **ST pullback + CCI module found no pullback pattern**, despite extreme momentum.

On **bars 543–552**, the threshold showed:


score=0 / 999


`/999` indicates **detect_signal was never called**.

Likely cause:

Early exit due to **ST3m / ST15m conflict path**.

---

## Root Cause D: Wrong-Side CALL Entries in Afternoon

Two losing trades:


14:12 CALL
14:54 CALL


Despite a clearly **bearish day structure**.

### Why they passed scoring

| Component | Score |
|---|---|
| trend_alignment | 15 |
| pivot_structure | 10–12 |
| adx_strength | 15 |

Total scores:


95 / 65
70 / 65


However:


day_type = TRENDING
trend_bias = Negative


These **should have been penalized or blocked**.

Observation:


open_bias_score = 0


Meaning the **day-bias alignment logic contributed nothing**.

---

## Root Cause E: Afternoon Drop (24,800 → 24,465) Completely Missed

Timeline:


13:27–13:54 → score=0/999 (no signal)
14:12 → CALL entered (wrong side) -13 pts
14:54 → CALL entered again -11.7 pts
Post-exit → LOSS_COOLDOWN + LATE_ENTRY gate


Cooldown:


LOSS_COOLDOWN = 10 bars (~30 min)


Combined with **LATE_ENTRY (>14:45)**, the engine was **locked out** of the remaining trend move.

---

# 3. Fix Proposal

## Fix 1: Trend Day RSI Floor Override

### Problem


RSI = 12 blocked as "extreme"


On **trend days this represents strength**, not exhaustion.

### Fix

Remove RSI lower bound for trend-aligned entries.

```python
# In _trend_entry_quality_gate OSC_EXTREME check:

if day_type == "TRENDING_DAY" and signal_side_aligns_with_trend:
    rsi_range[0] = 0  # No lower bound

Tag

[OSC_TREND_OVERRIDE]
Fix 2: Trend Alignment Scoring with ST Conflict Override
Problem

trend_alignment = 0 when ST15m lags.

Fix

Allow ADX override path to provide partial credit.

# In check_entry_condition scoring:

if st_conflict_override_active and adx >= ADX_OVERRIDE_THRESHOLD:
    bd["trend_alignment"] = 10

Tag

[TREND_ALIGN_OVERRIDE]
Fix 3: Day-Bias Misalignment Penalty
Problem

Counter-trend CALL trades scored extremely high.

Fix

Add explicit penalty.

if day_type == "TRENDING_DAY" and side != trend_direction:
    bd["day_bias_penalty"] = -15
    logging.info(f"[DAY_BIAS_PENALTY] side={side} day_trend={trend_direction}")

Tag

[DAY_BIAS_PENALTY]
Fix 4: Momentum Entry Path (Gap / Expansion Moves)
Problem

detect_signal only triggers on pullbacks.

Trend days often have momentum continuation without pullback.

Fix

Add momentum detection path.

if not pullback_detected and atr_stretch > 2.0 and adx > 30:
    return {
        "side": confirmed_side,
        "source": "MOMENTUM_ENTRY",
        "score": 70
    }

Tag

[MOMENTUM_ENTRY]
Fix 5: score=0/999 Sentinel Audit
Problem

Signal engine sometimes skips evaluation entirely.

Fix

Force detect_signal() execution when overrides are active.

Add log tag.

[SIGNAL_SKIP]
Fix 6: Cooldown Reduction on Trend Days
Problem

Loss cooldown blocked participation in the main move.

Fix

Reduce cooldown on trend days.

TREND_DAY cooldown = 5 bars

Apply when exit reason was counter-trend loss.

Tag

[COOLDOWN_REDUCED]
4. Generalization Across Day Types
Handling the Same Market Moves
Day Type	Morning Gap PUT	Mid CALL Bounce	Afternoon PUT
TREND_DAY	Fix 1 + Fix 4 enable early PUT entry	Fix 3 blocks CALL	Fix 2 + Fix 5 ensure signals
RANGE_DAY	OSC_EXTREME correctly blocks	CALL bounce valid	PUT continuation blocked
GAP_DAY	Momentum entry boosted	Counter-trend penalty -10	Reduced threshold
HIGH_VOL	ATR scaling already applied	SL widened	Position size reduced
Cross-Day Regime Rules
TRENDING_DAY
RSI lower bound = 0
Counter-trend penalty = -15
Cooldown = 5 bars
Score threshold = 60
RANGE_DAY
Standard oscillator bounds
No bias penalty
Cooldown = 5–10 bars
Score threshold = 70
GAP_DAY
RSI floor = 5
Gap-aligned bonus = +10
Counter-gap penalty = -10 (first 30 min)
Score threshold = 65
BALANCE_DAY
Oscillator bounds tightened
Both sides allowed
Cooldown = 7 bars
Score threshold = 75
Dashboard Attribution Tags
Tag	Meaning	Parser Field
[OSC_TREND_OVERRIDE]	RSI floor removed	osc_trend_override_count
[TREND_ALIGN_OVERRIDE]	ADX trend scoring	trend_align_override_count
[DAY_BIAS_PENALTY]	Counter-trend penalty	day_bias_penalty_count
[MOMENTUM_ENTRY]	Momentum-based entry	momentum_entry_count
[SIGNAL_SKIP]	Signal scorer skipped	signal_skip_count
[COOLDOWN_REDUCED]	Trend-day cooldown reduction	cooldown_reduced_count
Summary

The March 2nd session was a strong trending day, but the engine's defensive governance prevented capturing a ~800 point move.

Core Issues

Oscillator gating misinterpreted trend strength

RSI = 12 → treated as exhaustion
Reality → strong bearish momentum

15m Supertrend lag

trend_alignment = 0
for nearly 5 hours

No momentum-entry path

The system only detects pullback patterns, missing gap-down momentum moves.

Result

The engine:

Blocked early trend-aligned PUT entries

Allowed counter-trend CALL trades

Missed the entire afternoon breakdown

Outcome of Proposed Fixes

The fixes:

Preserve safety on RANGE/BALANCE days

Unlock participation on TRENDING days

Introduce momentum detection

Improve trend alignment scoring

The day-type regime matrix ensures all fixes remain context-aware and non-overfitted.