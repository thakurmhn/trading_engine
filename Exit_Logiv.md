# position_manager.py v3 — Exit Logic Changelog
*Empirically audited, runtime-validated. All 12 test paths pass.*

---

## 11 Bugs Fixed vs v2

### CRITICAL Fixes

| # | Category | v2 Bug | v3 Fix |
|---|----------|--------|--------|
| 1 | **Trail stop premature** | Trail computed as % of *option premium gain* (`ep + peak_gain*(1-step)`). At entry ₹120 with +7pts premium gain, trail_stop=126.16 — fires on the very next bar. | Trail now computed in **underlying-point space**, converted to option price via adaptive delta. `trail_stop = ep + delta * ul_peak_move * (1-step)`. Floor at 50% of entry. |
| 2 | **Delta constant** | `DELTA_APPROX=0.50` constant. Deep ITM options after a 60pt move have delta≈0.62, not 0.50. Constant undervalues option LTP → false HARD_STOP triggers in replay. | `delta = 0.50 + 0.002 × ul_move`, clamped `[0.25, 0.85]`. ITM options now priced higher, OTM priced lower. No more false hard-stops. |

### HIGH Fixes

| # | Category | v2 Bug | v3 Fix |
|---|----------|--------|--------|
| 3 | **TRAIL_MIN too tight** | Flat `TRAIL_MIN_PTS=15`. Activates in 3 bars, giving trail no room before reversal fires. | Adaptive: 25 (NORMAL), 20 (HIGH vol), 30 (LOW vol), 35 (NARROW CPR / TRENDING day). Trend days let winners breathe. |
| 4 | **Partial exit breakeven** | After PARTIAL_EXIT, `hard_stop` moved to `entry_px` (option premium space). In replay, underlying and option price are different — breakeven guard was in wrong space. | Added `hard_stop_ul = entry_ul` (underlying-space guard). Both premium and UL breakeven are now enforced post-partial. |
| 5 | **RSI NaN cross detection** | If `rsi14` is NaN (warm-up bars), `prev_rsi` never updates, silently breaking the RSI neutral-cross detection for entire session. | NaN guard: only update `prev_rsi` when `rsi` is finite. Other exit signals still fire regardless. |
| 6 | **REVERSAL_3 hard bypass** | 3 consecutive bearish candles fired as **hard exit** (bypassed scoring). Cut winners in trending markets even when ADX>25 suppressed it — the suppression check came too late (trade already exited). | REVERSAL_3 moved into scoring as `WT_REVERSAL_3=15`. Requires combination to reach 45pt threshold. ADX suppression still applies within scoring. |

### MEDIUM Fixes

| # | Category | v2 Bug | v3 Fix |
|---|----------|--------|--------|
| 7 | **MAX_HOLD day-type blind** | MAX_HOLD used only CPR-width (intrabar CPR ≠ daily CPR). Day type was stored but ignored. | `day_type` is now primary: TRENDING → +10 bars, RANGE → −5 bars. CPR is secondary: NARROW → +3, WIDE → −2. |
| 8 | **W%R too aggressive** | Flat `WT_WR_EXTREME=20`. W%R ≥ 0 fires constantly in strong uptrends, contributes 20pts toward 45pt threshold even alone. | Dynamic: `WT_WR_SOLO=15` (alone), `WT_WR_COMBINED=25` (when MOM or PIV also scores). W%R alone can never fire below 45pt threshold. |
| 9 | **Pivot rejection threshold** | Breakout failure fired at `entry_ul × 0.9985` ≈ ±4pts on NIFTY. Any 4pt pullback in a healthy breakout triggered rejection. | Threshold is now `entry_ul ± 0.5 × ATR`. Uses `atr_entry` stored at open as fallback when live ATR unavailable. |

### LOW Fixes

| # | Category | v2 Bug | v3 Fix |
|---|----------|--------|--------|
| 10 | **ST flip 15m suppression** | When 15m was aligned and trade profitable, ST flip was **fully suppressed** regardless of flip count. After 4+ consecutive 3m flips, 15m is lagging the reversal but suppression continues. | Suppress only for ≤2 flip_bars. After 3+ flip_bars → downgrade to `WT_ST_FLIP_ONLY=20` (acknowledges 15m lag). |
| 11 | **Momentum single-bar exit** | Single `momentum_ok=False` bar scored immediately. One noisy candle in a strong trend triggered premature exit. | `mom_fail_bars` counter: score only after **2 consecutive** False bars. Resets to 0 on any True bar. Entry uses same 2-bar spec. |

---

## Updated Score Architecture

```
Signal                Score    Fires alone?    Notes
────────────────────  ─────    ────────────    ──────────────────────────────
ST_RSI_CONFIRMED        50     YES (≥45)       ST flip ×2 + RSI crosses 50
ST_FLIP_ONLY            20     NO              Needs ≥25 pts from others
MOMENTUM_CCI            25     NO              2×mom_fail + CCI<±50
MOMENTUM_ONLY           15     NO              2×mom_fail alone
PIVOT_REJECTION         20     NO              ATR-tolerance wick/breakout
WR_SOLO                 15     NO              W%R extreme alone
WR_COMBINED             25     NO (w/ others)  W%R extreme + MOM or PIV
REVERSAL_3              15     NO              3×bearish bars + ADX<25

Threshold: 45 / 100
Secondary rule: ST_flip_only(20) + any other ≥20 → EXIT
```

---

## Exit Flow (v3)

```
TIER 1 — HARD (always fires, no score)
  → HARD_STOP       : option LTP ≤ 45% of entry
  → HARD_STOP_UL    : underlying < entry UL post-partial (new)
  → TRAIL_STOP      : UL-based trail; tightens after 2×mom_fail
  → EOD_EXIT        : 15:10 IST
  → MAX_HOLD        : day_type-aware (TREND=30, RANGE=15, NORMAL=20)

TIER 2 — SCORED (fire when score ≥ 45)
  → PARTIAL_EXIT    : 50% booked at 25pt UL gain (adaptive)
  → EXIT_SCORED     : ST+RSI, MOM_fail, PIV, W%R, REV3 (scored)
```

---

## Validation Results

```
T1:  HARD_STOP / scored exit on crash            ✓  (scored fires before hard stop = better price)
T2a: PARTIAL_EXIT at 25pt UL gain                ✓
T2b: TRAIL_STOP fires on reversal after partial  ✓
T3:  ST_RSI_CONFIRMED (50pts) fires alone        ✓
T4:  REV3 suppressed in trending pullback        ✓  (close > ema9, ADX=32)
T5:  REV3 + MOM scores ≥25 in range market       ✓
T6:  MOM_FAIL single bar does NOT exit           ✓
T7:  W%R=15 solo does NOT fire below threshold   ✓  (w/ non-wick candle)
T8:  TREND day trail_min=35pts                   ✓
T9:  RANGE day max_hold=13 bars                  ✓
T10: EOD_EXIT at 15:12                           ✓
T11: Adaptive delta > 0.50 for ITM               ✓

12/12 PASS
```