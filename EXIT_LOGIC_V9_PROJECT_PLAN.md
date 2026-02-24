# Exit Logic v9 - Enhancement Project Plan

**Project:** Exit Logic v8 → v9 Enhancement  
**Date:** 2026-02-24  
**Status:** 🔵 PLANNING PHASE  
**Baseline (v8):** 63.6% win rate, +39.53 pts P&L, 0 convertible losses

---

## Executive Summary

Exit Logic v9 builds on v8's adaptive threshold foundation by adding **stress resilience**, **time-aware exits**, **capital efficiency optimization**, and **database integrity**. The goal is to improve adaptability under extreme conditions while preserving v8's proven performance.

**v9 Vision:** From adaptive to resilient—exits that not only scale with volatility but also time, capital constraints, and market shocks.

---

## v9 Enhancement Roadmap

### Enhancement 1: Dynamic Breakout Sustain (Volatility-Aware Filtering)

**Current State (v8):**
```
BREAKOUT_SUSTAIN_MIN = 3 (fixed for all markets)
Rule: Require 3 bars at R4/S4 before hold activation
Issue: In extreme volatility, 3 bars may be too loose
       In low volatility, 3 bars may be too strict
```

**v9 Enhancement:**
```
BREAKOUT_SUSTAIN_MIN = 2 + ceil(ATR(10) / 10)
Rule: Scale sustain requirement with volatility regime
      Low ATR (5 pts)  → 2 bars (tight filtering)
      Med ATR (15 pts) → 3 bars (balanced)
      High ATR (30 pts) → 5 bars (strict filtering)
      
Benefit: Strict filtering in calm markets (prevent noise)
         Loose filtering in volatile markets (catch real trends)
```

**Implementation:**
- Location: position_manager.py, BREAKOUT_HOLD rule (~line 768)
- New constant: `BREAKOUT_SUSTAIN_BASE = 2`, `BREAKOUT_SUSTAIN_SCALE = 10`
- Calculate: `sustain_required = BREAKOUT_SUSTAIN_BASE + ceil(atr_val / BREAKOUT_SUSTAIN_SCALE)`
- Logging: `[BREAKOUT SUSTAIN] bars_required=3 atr=15.0 regime=moderate`

**CSV Schema Addition:**
```
breakout_sustain_required, breakout_sustain_achieved, volatility_regime
```

---

### Enhancement 2: Time-Based Quick Profit (Urgency Exit)

**Current State (v8):**
```
QUICK_PROFIT fires when:
  ul_peak_move >= quick_profit_threshold
  
Issue: If market moves sideways, QUICK_PROFIT may never trigger
       Capital locked for 10+ bars (inefficient)
```

**v9 Enhancement:**
```
New Rule: TIME QUICK PROFIT (Priority: 2.5, between QUICK_PROFIT and DRAWDOWN)

Triggers when:
  - QUICK_PROFIT not yet fired (half_qty = False)
  - bars_held >= TIME_QUICK_PROFIT_MAX (default: 10 bars)
  - cur_gain >= TIME_QUICK_PROFIT_MIN_GAIN (default: +3 pts, lower than normal +10)

Action: Book 50% at reduced threshold (accept smaller wins to free capital)

Benefit: Prevents capital lockup in sideways markets
         Ensures capital turnover (multiple small wins vs one big win)
         Better utilization of capital constraints
         
Logging: [TIME EXIT] bars_elapsed=10 min_gain_threshold=3.0pts exit_premium=82500.00
```

**Implementation:**
- Location: position_manager.py, rules section (~line 750)
- Constants: `TIME_QUICK_PROFIT_MAX = 10`, `TIME_QUICK_PROFIT_MIN_GAIN = 3.0`
- New exit decision: If bars_held >= 10 and cur_gain >= 3, exit (TIME_EXIT rule)
- Logging: `[TIME EXIT] bars_elapsed=10 min_gain_threshold=3.0pts current_gain=3.5pts`

**CSV Schema Addition:**
```
time_exit_triggered, bars_at_time_exit, min_gain_at_time_exit
```

---

### Enhancement 3: Capital Utilization Metrics (Efficiency Tracking)

**Current State (v8):**
```
Tracks: bars_to_profit (single trade metric)
Missing: Overall capital efficiency across session
```

**v9 Enhancement:**
```
New Metrics:

A) Per-Trade:
   - capital_deployed_bars = bars from entry to first profit (QUICK_PROFIT or TIME_EXIT)
   - capital_utilization_pct = (capital_deployed_bars / avg_trade_duration) × 100
   - efficiency_score = capital_deployed_bars / peak_bars_held × 100

B) Session-Level (tracked in replay analyzer):
   - Total trades = 22
   - Sum of deployed_bars = 45 bars total
   - Avg deployment time = 45 / 22 = 2.05 bars per trade
   - Session capital efficiency = (22 trades × 2.05 bars) / (22 × 5 bar avg) = 41%
   
C) Thresholds for alerts:
   - If efficiency < 30% → Too much capital lockup (need tighter stops?)
   - If efficiency > 70% → Too many quick exits (maybe targets too tight?)
   - Ideal range: 40-60% (balanced capital turnover)
```

**Implementation:**
- Location: position_manager.py, position state tracking (~line 550)
- New position state fields:
  ```python
  self._t["deployment_start_bar"] = current_bar  # Set at entry
  self._t["capital_deployed_at_profit"] = -1    # Set when profit triggered
  self._t["capital_utilization_pct"] = 0        # Calculated at exit
  self._t["efficiency_score"] = 0               # Calculated at exit
  ```
- Logging: `[CAPITAL UTILIZATION] deployed_bars=2 utilization_pct=40.0 score=95.5`

**Replay Analyzer Updates:**
- Track session-level metrics
- Calculate average efficiency per date
- Flag days with efficiency outside 30-70% range
- Report in enhanced CSV

**CSV Schema Addition:**
```
capital_deployed_bars, capital_utilization_pct, efficiency_score, session_efficiency_avg
```

---

### Enhancement 4: Stress Testing Framework (Resilience Validation)

**Current State (v8):**
```
Only tests against historical replay data
Missing: Synthetic stress scenarios
```

**v9 Enhancement - Synthetic Scenarios in Replay Analyzer:**

```
Scenario 1: GAP OPEN (5% gap at market open)
  Entry: 82500
  After 1 bar: 82500 × (1 + 0.05) = 86625 (HUGE gap)
  Tests: How do thresholds hold? Do stops trigger too late?
  Expected: LOSS_CUT triggers within 2 bars (max -15 pts on scaled threshold)

Scenario 2: FLASH REVERSAL (up 50 pts then immediate -60 pts reversal)
  Entry: 82500
  Bar 1: 82550 (+50 pts, QUICK_PROFIT threshold hit!)
  Bar 2: 82440 (-60 from peak, -45 total slippage)
  Tests: Can DRAWDOWN_EXIT catch the reversal?
  Expected: Exit with < -20 pts loss before crash

Scenario 3: EXTREME VOLATILITY (ATR = 50 pts spikes)
  All candles wicks extend ±25 pts (2.5x normal)
  Tests: Dynamic thresholds (loss_cut = -25, quick_profit = 50)
  Expected: Thresholds adapt, avoid over-exits and under-exits

Scenario 4: LOW LIQUIDITY (multiple 3-5 bar consolidation zones)
  Prices stay ±2 pts range for 5+ bars
  Tests: TIME_EXIT triggers at bar 10 with min_gain_threshold
  Expected: Exits at reduced gains rather than locking up capital

Scenario 5: TRENDING EXHAUSTION (strong trend then sudden stall)
  Entry: 82500, trend to 82600 (+100 pts, way above QUICK_PROFIT)
  Bar 15: Trend stalls, QUICK_PROFIT still not hit (unusual)
  Test: Does TIME_EXIT fire? Does trader exit or wait?
  Expected: TIME_EXIT fires at bar 10 with 3+ pts gain (avoid hoping)
```

**Implementation:**
- Create synthetic OHLC data generators in replay_analyzer_v7.py
- Run each scenario 1000x with random variations
- Track: Entry rule, exit rule, P&L distribution, max drawdown
- Report: Pass/fail for each scenario (win rate ≥ 60%, convertible losses = 0)
- Logging: `[STRESS TEST] scenario=gap_open trials=1000 pass_rate=98.5% avg_pnl=+2.5pts`

**CSV Schema Addition:**
```
stress_scenario, scenario_pass_fail, max_loss_in_scenario, avg_pnl_in_scenario
```

---

### Enhancement 5: Database Pre-Cleaner (Data Integrity)

**Current State (v8):**
```
Replay analyzer reads all candles as-is
Missing: Detection of pre/post market candles, gaps, corruptions
```

**v9 Enhancement - DB Cleaner:**

```
Pre-Processing Steps:

Step 1: Detect pre-market candles (before 09:15 IST)
  - Look for timestamp < 09:15
  - Flag and optionally remove
  - Log: [DB CLEANUP] file=ticks_2026-02-24.db candles_removed=5 reason=pre_market

Step 2: Detect post-market candles (after 15:30 IST)
  - Look for timestamp > 15:30
  - Flag and optionally remove
  - Log: [DB CLEANUP] file=... candles_removed=3 reason=post_market

Step 3: Detect gaps in timestamp (missing 3-minute candles)
  - Expected: Every 3 minutes (180 seconds)
  - Check: gap > 6 minutes (missing 2+ candles)
  - Flag and skip trade spanning gap
  - Log: [DB CLEANUP] file=... gap_detected 09:21-09:27 reason=missing_candles

Step 4: Detect price spikes (>10% move in 1 candle = likely data error)
  - Compare: |close - prev_close| / prev_close > 0.10
  - Flag as potentially corrupt
  - Option: Skip candle or use prev close as close
  - Log: [DB CLEANUP] file=... spike_detected bar=45 move=+15% action=skip

Step 5: Detect sessions with insufficient data (< 50 candles)
  - Expected: Market hours = 6.5 hours = 130 candles (3-min)
  - If < 50: Market didn't open or data incomplete
  - Skip entire session
  - Log: [DB CLEANUP] file=... session_skipped reason=insufficient_data candles=32

Output: Clean, consistent OHLC data ready for replay
CSV Report: candles_before, candles_after, gaps_detected, spikes_flagged, sessions_skipped
```

**Implementation:**
- Create `DatabaseCleaner` class in replay_analyzer_v7.py
- Methods:
  - `filter_pre_market()` - Remove timestamp < 09:15
  - `filter_post_market()` - Remove timestamp > 15:30
  - `detect_gaps()` - Find missing candles
  - `detect_price_spikes()` - Find > 10% moves
  - `validate_session_completeness()` - Check >= 50 candles
  - `clean_and_validate()` - Master orchestration
- Logging: `[DB CLEANUP]` tags for each action

**CSV Schema Addition:**
```
db_candles_original, db_candles_after_cleaning, gaps_detected_count, spike_flags_count, pre_market_removed, post_market_removed
```

---

## v9 Architecture Summary

### Position Manager Changes (15-20% code increase)
```
Current position_manager.py: 1485 lines
Expected after v9: ~1650 lines (+165 lines, ~11%)

New sections:
- Dynamic breakout sustain calculation (30 lines)
- Time-based quick profit rule (25 lines)
- Capital utilization tracking (20 lines)
- Enhanced logging for new rules (15 lines)
- Helper: calculate_breakout_sustain() (10 lines)
```

### Replay Analyzer Enhancements (30-40% code increase)
```
Current replay_analyzer_v7.py: 373 lines
Expected after v9: ~550 lines (+177 lines, ~47%)

New sections:
- DatabaseCleaner class (100 lines)
- Synthetic scenario generators (80 lines)
- Stress test runner (50 lines)
- Enhanced CSV reporting (30 lines)
- Capital efficiency metrics (30 lines)
```

### CSV Schema Evolution
```
v8: 15 columns
v9: 25+ columns

New columns:
- breakout_sustain_required, breakout_sustain_achieved
- time_exit_triggered, bars_at_time_exit, min_gain_at_time_exit
- capital_deployed_bars, capital_utilization_pct, efficiency_score
- stress_scenario, scenario_pass_fail
- db_candles_before, db_candles_after, gaps_detected, spike_flags
```

---

## v9 Implementation Workflow

### Phase 1: Code Enhancements (5 hours)
1. Add dynamic breakout sustain to position_manager.py
2. Add time-based quick profit rule
3. Add capital utilization tracking
4. Create helper methods and constants
5. Implement enhanced CSV tracking

### Phase 2: Replay Analyzer Extensions (6 hours)
1. Create DatabaseCleaner class
2. Implement synthetic scenario generators (5 scenarios)
3. Build stress test runner (1000 trials per scenario)
4. Extend CSV reporting
5. Add capital efficiency analysis

### Phase 3: Testing & Validation (4 hours)
1. Unit tests for each new rule
2. Replay baseline validation (should match/exceed v8)
3. Stress test execution (all 5 scenarios)
4. Convertible loss verification (must be 0)
5. Performance comparison

### Phase 4: Documentation (3 hours)
1. Technical specification (v9 enhancements)
2. Implementation guide (how to use new features)
3. Stress test results (summary + scenarios)
4. Deployment guide
5. Monitoring and alerting guide

### Total Estimated Effort: 18 hours

---

## Success Criteria

### Baseline Preservation (MUST HAVE)
- ✅ Win rate ≥ 63.6%
- ✅ Total P&L ≥ +39.53 pts
- ✅ Convertible losses = 0
- ✅ No new bugs or errors

### Improvements (SHOULD HAVE)
- ✅ Capital efficiency ≥ v8 (avg bars_to_profit ≤ v8)
- ✅ Database cleaner removes ≥ 10% corrupt data
- ✅ Stress tests pass ≥ 95% resilience rate
- ✅ Time-exit triggers on ≥ 10% of trades
- ✅ Dynamic sustain adapts successfully (2-5 bar range)

### Documentation (MUST HAVE)
- ✅ 5 technical documents (v9 deep-dive)
- ✅ Deployment guide with examples
- ✅ CSV schema update documented
- ✅ Monitoring KPIs defined
- ✅ Stress test results published

---

## Risk Assessment

### Low Risk ✅
- All enhancements are additive (don't remove v8 logic)
- Easy to disable individual features if issues
- Backward compatible CSV schema (add columns, don't remove)
- Can rollback to v8 in minutes

### Medium Risk 🟡
- Dynamic sustain may reduce BREAKOUT_HOLD frequency in some markets
- Time-exit may create new loss scenarios if min_gain_threshold too low
- Stress tests are synthetic—may not represent real market

### Mitigation
- Thorough testing on historical data
- Conservative parameter selection (TIME_QUICK_PROFIT_MAX = 10, min_gain = 3)
- Gradual rollout (paper trading first, then small live size)
- Detailed monitoring of new rules

---

## Deliverables Checklist

**Code:**
- [ ] position_manager.py (v9 updated)
- [ ] replay_analyzer_v7.py (v9 extended)
- [ ] Enhanced CSV schema
- [ ] Test suite passing

**Documentation:**
- [ ] EXIT_LOGIC_V9_ENHANCEMENTS.md (technical)
- [ ] EXIT_LOGIC_V9_IMPLEMENTATION_GUIDE.md (operations)
- [ ] EXIT_LOGIC_V9_STRESS_TEST_RESULTS.md (validation)
- [ ] EXIT_LOGIC_V9_DATABASE_CLEANING_REPORT.md (data quality)
- [ ] EXIT_LOGIC_V9_DEPLOYMENT_GUIDE.md (deployment)

**Validation:**
- [ ] Baseline replay test passed (win rate, P&L, convertible losses)
- [ ] Stress test suite results (5 scenarios × 1000 trials)
- [ ] Capital efficiency analysis
- [ ] Zero regressions detected

---

## v8 → v9 Comparison

| Aspect | v8 | v9 |
|--------|----|----|
| **Dynamic Thresholds** | ✅ Yes (ATR-scaled) | ✅ Enhanced (new rules) |
| **Breakout Filtering** | Fixed 3 bars | Dynamic 2-5 bars (ATR-scaled) |
| **Quick Profit** | UL threshold only | UL + Time constraint (10 bar max) |
| **Capital Tracking** | Per-trade metric | Per-trade + session-level |
| **Stress Testing** | No | 5 scenarios × 1000 trials |
| **Data Cleaning** | No | Pre-market/post-market filter + gap detection |
| **Resilience** | Good | Excellent (tested under shocks) |
| **Complexity** | Moderate | Moderate+ (few new rules, better metrics) |
| **CPU Impact** | Low | Low+ (stress tests offline only) |

---

## Next Steps (Today)

1. ✅ Review and approve v9 plan
2. ⏳ Implement phase 1 (code enhancements)
3. ⏳ Implement phase 2 (replay analyzer)
4. ⏳ Execute phase 3 (testing & validation)
5. ⏳ Create phase 4 (documentation)
6. ⏳ Deploy v9 to production (with monitoring)

---

**v9 Project Status:** 🔵 PLANNING APPROVED  
**Next Action:** Begin Implementation (Phase 1)  
**Estimated Completion:** 18 hours from now  

Ready to proceed? Approve to begin Phase 1 implementation.
