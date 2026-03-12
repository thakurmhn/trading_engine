# Trading System Profitability Improvement Plan
**Date**: 2026-03-12  
**Status**: READY FOR IMPLEMENTATION  
**Priority**: HIGH - Address negative P&L trend

---

## Executive Summary

Current system shows **fragile profitability** (profit factor 1.12, expectancy +0.45 pts/trade) with critical issues:
- **Negative payoff ratio** (0.86) - losses exceed wins
- **Severe over-filtering** - 806 DAILY_S4 blocks on single day
- **PUT side weakness** - 9× underperformance vs CALL
- **Duplicate trade bug** - same trades logged multiple times

**Expected Impact**: Lift profit factor from 1.12 → 1.45+, expectancy from +0.45 → +1.2 pts/trade

---

## Phase 1: Critical Fixes (Deploy First - 2 Hours)

### Fix 1.1: Eliminate Duplicate Trade Logging ✅ CRITICAL
**Issue**: Dashboard shows identical trades repeated 6-7 times (same entry/exit/time)  
**Root Cause**: Likely in `log_parser.py` or `execution.py` trade recording  
**Impact**: Inflates trade count, distorts metrics, masks true performance

**Action**:
```python
# In execution.py - add trade deduplication guard
_logged_trades = set()  # (symbol, entry_time, entry_price) tuples

def _log_trade(trade_data):
    trade_key = (trade_data['symbol'], trade_data['entry_time'], trade_data['entry_price'])
    if trade_key in _logged_trades:
        logging.warning(f"[DUPLICATE TRADE BLOCKED] {trade_key}")
        return
    _logged_trades.add(trade_key)
    # ... existing logging code
```

**Validation**: Run replay 2026-03-11, verify trade count drops from 62 → ~10-15 unique trades

---

### Fix 1.2: Soften DAILY_S4/R4 Hard Blocks ✅ HIGH PRIORITY
**Issue**: 806 DAILY_S4_FILTER blocks on 2026-03-11 = 90% of signals suppressed  
**Root Cause**: Hard block prevents ALL entries outside S4-R4 range, even on strong trends

**Current Logic** (signals.py):
```python
if close < daily_s4:
    return None, "DAILY_S4_FILTER"
if close > daily_r4:
    return None, "DAILY_R4_FILTER"
```

**Improved Logic**:
```python
# Soften to score penalty instead of hard block when ADX confirms trend
if close < daily_s4:
    if adx >= 30 and st_bias_15m == "BEARISH":
        # Strong downtrend confirmed - allow PUT entries with penalty
        score_penalty = -10
        logging.info("[DAILY_S4_OVERRIDE] Bearish trend confirmed, allowing PUT entry with -10 penalty")
    else:
        return None, "DAILY_S4_FILTER"
        
if close > daily_r4:
    if adx >= 30 and st_bias_15m == "BULLISH":
        # Strong uptrend confirmed - allow CALL entries with penalty
        score_penalty = -10
        logging.info("[DAILY_R4_OVERRIDE] Bullish trend confirmed, allowing CALL entry with -10 penalty")
    else:
        return None, "DAILY_R4_FILTER"
```

**Expected Impact**: Reduce blocks from 806 → ~200, capture trend continuation moves

---

### Fix 1.3: Implement ATR-Based Dynamic Stops ✅ HIGH PRIORITY
**Issue**: Fixed 10-12 pt stops sit inside normal intraday noise  
**Current**: `MAX_LOSS_PER_TRADE = 10 pts` (static)

**Improved**:
```python
# In option_exit_manager.py
def calculate_dynamic_stop(entry_price, atr, volatility_tier):
    """ATR-based stop placement"""
    if volatility_tier == "VERY_LOW":
        stop_multiplier = 1.2  # Tighter stops in low vol
    elif volatility_tier == "HIGH":
        stop_multiplier = 2.0  # Wider stops in high vol
    else:
        stop_multiplier = 1.5  # Normal
    
    stop_distance = atr * stop_multiplier
    stop_price = entry_price - stop_distance  # for CALL
    
    logging.info(f"[DYNAMIC_STOP] entry={entry_price:.2f} atr={atr:.1f} "
                 f"tier={volatility_tier} mult={stop_multiplier} "
                 f"stop={stop_price:.2f} (-{stop_distance:.2f})")
    return stop_price
```

**Expected Impact**: Reduce premature stop-outs by 30%, improve payoff ratio from 0.86 → 1.0+

---

## Phase 2: PUT Side Improvements (Deploy Second - 3 Hours)

### Fix 2.1: Symmetric PUT Scoring Bonuses
**Issue**: CALL gets more score boosts than PUT (bias asymmetry)  
**Current**: PUT entries hit WEAK_ADX / ST_CONFLICT more often

**Action** (entry_logic.py):
```python
def _score_trend_alignment(bias_15m, indicators, side):
    # ... existing CALL logic ...
    
    # ADD: PUT-specific early reversal credit (mirror CALL logic)
    if side == "PUT":
        if b15 == "BEARISH" and b3 == "BEARISH":  return w
        if b15 == "BEARISH" and b3 == "NEUTRAL":   return w * 3 // 4
        if b15 == "BEARISH":                        return w // 2
        if b15 == "NEUTRAL" and b3 == "BEARISH":    return w // 4
        # NEW: Early reversal credit for PUT
        if b15 == "BULLISH":
            c15 = indicators.get("candle_15m")
            if c15 is not None:
                sl = str(c15.get("supertrend_slope", "")).upper()
                if sl == "DOWN": 
                    logging.info("[PUT_REVERSAL_CREDIT] 15m slope turning down, +4 pts")
                    return w // 4   # 4 pts — early reversal credit
        return 0
```

**Expected Impact**: Increase PUT win rate from 40% → 55%, balance CALL/PUT P&L

---

### Fix 2.2: Relax WEAK_ADX Threshold for PUT in Downtrends
**Issue**: WEAK_ADX blocks PUT entries even during confirmed downtrends  
**Current**: ADX < 18 → blocked for both sides

**Improved**:
```python
# In signals.py
def _check_adx_gate(adx, side, st_bias_15m, close, daily_s4):
    """Asymmetric ADX gate - relax for PUT in confirmed downtrends"""
    
    # Standard gate for CALL
    if side == "CALL":
        if adx < 18:
            return False, "WEAK_ADX"
    
    # Relaxed gate for PUT when below daily S4 (confirmed bearish)
    if side == "PUT":
        if close < daily_s4 and st_bias_15m == "BEARISH":
            # Downtrend confirmed - allow PUT even with weak ADX
            if adx < 12:  # Only block extreme weakness
                return False, "WEAK_ADX"
            logging.info(f"[PUT_ADX_RELAXED] adx={adx:.1f} below_S4=True, allowing PUT")
        else:
            if adx < 18:
                return False, "WEAK_ADX"
    
    return True, None
```

**Expected Impact**: Reduce PUT blocks by 40%, capture downtrend moves earlier

---

## Phase 3: Exit Logic Enhancements (Deploy Third - 4 Hours)

### Fix 3.1: Dual Partial Profit Taking
**Issue**: Quick profit trims 50% at +10 pts, but remainder exposed to full reversal  
**Current**: Single partial at +10 pts

**Improved**:
```python
# In option_exit_manager.py
def check_partial_exits(position, current_price, atr):
    """Staggered profit taking - lock in gains progressively"""
    
    pnl_pts = current_price - position.entry_price
    
    # TP1: 40% at +10 pts (quick profit)
    if not position.tp1_hit and pnl_pts >= 10:
        partial_qty = int(position.quantity * 0.4)
        position.tp1_hit = True
        logging.info(f"[TP1_HIT] Closing 40% ({partial_qty} lots) at +10 pts")
        return "PARTIAL_TP1", partial_qty
    
    # TP2: 30% at +18 pts (extended profit)
    if position.tp1_hit and not position.tp2_hit and pnl_pts >= 18:
        partial_qty = int(position.quantity * 0.3)
        position.tp2_hit = True
        # Move stop to breakeven for runner
        position.stop_price = position.entry_price
        logging.info(f"[TP2_HIT] Closing 30% ({partial_qty} lots) at +18 pts, "
                     f"stop moved to BE for 30% runner")
        return "PARTIAL_TP2", partial_qty
    
    # Runner (30%): Trail with ATR-based stop
    if position.tp2_hit:
        trail_stop = current_price - (atr * 1.5)
        if trail_stop > position.stop_price:
            position.stop_price = trail_stop
            logging.debug(f"[TRAIL_RUNNER] stop updated to {trail_stop:.2f}")
    
    return None, 0
```

**Expected Impact**: Increase average win from +12.72 → +16.5 pts, reduce max adverse excursion

---

### Fix 3.2: Regime-Aware Trailing Stops
**Issue**: Fixed trailing logic doesn't adapt to volatility  
**Current**: Single trail multiplier for all regimes

**Improved**:
```python
def calculate_trail_distance(atr, adx, cpr_width, position):
    """Adaptive trailing - wider in strong trends, tighter in chop"""
    
    # Base trail = 1.5 × ATR
    base_trail = atr * 1.5
    
    # Strong trend regime: widen trail to let winners run
    if adx >= 30 and cpr_width == "NARROW":
        trail_mult = 1.8
        regime = "STRONG_TREND"
    # Weak trend / choppy: tighten trail to protect gains
    elif adx < 20 or cpr_width == "WIDE":
        trail_mult = 0.8
        regime = "WEAK_CHOP"
    else:
        trail_mult = 1.0
        regime = "NORMAL"
    
    trail_distance = base_trail * trail_mult
    
    logging.debug(f"[TRAIL_CALC] regime={regime} adx={adx:.1f} "
                  f"cpr={cpr_width} mult={trail_mult} "
                  f"distance={trail_distance:.2f}")
    
    return trail_distance
```

**Expected Impact**: Extend winning trades by 2-3 bars in trends, reduce drawdown in chop

---

## Phase 4: Signal Quality Improvements (Deploy Fourth - 3 Hours)

### Fix 4.1: Add Signal Emission Logging
**Issue**: `[SIGNAL FIRED]` logs missing, can't measure signal-to-entry latency  
**Current**: Signals detected but not logged before gating

**Action** (signals.py):
```python
def detect_signal(candles_3m, candles_15m, ...):
    # ... existing signal detection ...
    
    if signal_detected:
        # LOG BEFORE GATING
        logging.info(f"[SIGNAL FIRED] side={side} score={score} "
                     f"pivot={pivot_type} rsi={rsi:.1f} cci={cci:.1f} "
                     f"st_15m={st_bias_15m} st_3m={st_bias_3m}")
        
        # Then apply gates
        if score < threshold:
            logging.info(f"[SIGNAL BLOCKED] reason=SCORE_LOW score={score}<{threshold}")
            return None, "SCORE_LOW"
        
        # ... rest of gating logic ...
```

**Expected Impact**: Enable signal-to-entry latency analysis, identify over-filtering

---

### Fix 4.2: Momentum Confirmation Relaxation
**Issue**: `momentum_ok` requires dual-EMA + dual-close + gap widening (too strict)  
**Current**: 15 pts all-or-nothing

**Improved**:
```python
def _score_momentum_ok(indicators, side):
    """Partial credit for momentum - not binary"""
    
    ema_aligned = indicators.get(f"ema_aligned_{side.lower()}", False)
    close_aligned = indicators.get(f"close_aligned_{side.lower()}", False)
    gap_widening = indicators.get("gap_widening", False)
    
    # Full credit: all 3 conditions
    if ema_aligned and close_aligned and gap_widening:
        return 15
    
    # Partial credit: 2 of 3 conditions
    conditions_met = sum([ema_aligned, close_aligned, gap_widening])
    if conditions_met == 2:
        return 10
    elif conditions_met == 1:
        return 5
    
    return 0
```

**Expected Impact**: Increase entry opportunities by 25%, maintain quality

---

## Phase 5: Risk Management Enhancements (Deploy Fifth - 2 Hours)

### Fix 5.1: Daily Loss Circuit Breaker
**Issue**: Single bad day (2026-03-03: -72 pts) erases week of gains  
**Current**: `MAX_DAILY_LOSS = -15000` (too loose)

**Improved**:
```python
# In execution.py
class DailyRiskManager:
    def __init__(self):
        self.daily_pnl = 0
        self.daily_trades = 0
        self.max_daily_loss = -8000  # Tighter limit
        self.max_consecutive_losses = 3
        self.consecutive_losses = 0
    
    def check_risk_limits(self):
        """Multi-tier circuit breaker"""
        
        # Tier 1: Hard daily loss limit
        if self.daily_pnl <= self.max_daily_loss:
            logging.warning(f"[CIRCUIT_BREAKER] Daily loss limit hit: {self.daily_pnl}")
            return False, "DAILY_LOSS_LIMIT"
        
        # Tier 2: Consecutive loss protection
        if self.consecutive_losses >= self.max_consecutive_losses:
            logging.warning(f"[CIRCUIT_BREAKER] {self.consecutive_losses} consecutive losses")
            return False, "CONSECUTIVE_LOSS_LIMIT"
        
        # Tier 3: Drawdown from peak
        if hasattr(self, 'daily_peak') and self.daily_pnl < self.daily_peak - 5000:
            logging.warning(f"[CIRCUIT_BREAKER] Drawdown from peak: "
                           f"{self.daily_peak} → {self.daily_pnl}")
            return False, "DRAWDOWN_LIMIT"
        
        return True, None
```

**Expected Impact**: Cap worst-day loss at -₹8,000, prevent equity curve blowups

---

### Fix 5.2: Position Sizing by Confidence
**Issue**: Same lot size for 52-pt score and 75-pt score  
**Current**: Fixed 2 lots (DEFAULT_LOT_SIZE)

**Improved**:
```python
def calculate_position_size(score, threshold, base_lots, vix_tier, vega):
    """Scale lots by conviction and risk"""
    
    # Confidence tiers
    score_margin = score - threshold
    if score_margin >= 20:
        confidence_mult = 1.5  # High conviction
    elif score_margin >= 10:
        confidence_mult = 1.0  # Normal
    else:
        confidence_mult = 0.5  # Marginal entry
    
    # Risk adjustments
    risk_mult = 1.0
    if vix_tier == "HIGH":
        risk_mult *= 0.75  # Reduce size in high vol
    if vega and abs(vega) > 15:
        risk_mult *= 0.75  # Reduce size for high vega risk
    
    final_lots = int(base_lots * confidence_mult * risk_mult)
    final_lots = max(1, min(final_lots, base_lots * 2))  # Floor 1, cap 2×
    
    logging.info(f"[POSITION_SIZE] score={score}/{threshold} "
                 f"conf_mult={confidence_mult} risk_mult={risk_mult} "
                 f"lots={final_lots}")
    
    return final_lots
```

**Expected Impact**: Reduce risk on marginal entries, compound gains on high-conviction setups

---

## Implementation Sequence

### Week 1: Critical Fixes (Phase 1)
**Day 1-2**: 
- Fix 1.1 (Duplicate trades)
- Fix 1.2 (Soften S4/R4 blocks)
- Replay test 2026-03-08 to 2026-03-11

**Day 3-4**:
- Fix 1.3 (ATR stops)
- Paper trading validation (1 full day)

**Day 5**:
- Live deployment with 1-lot size
- Monitor for 1 session

### Week 2: PUT Improvements + Exit Logic (Phases 2-3)
**Day 1-2**:
- Fix 2.1 (PUT scoring)
- Fix 2.2 (PUT ADX relaxation)
- Replay validation

**Day 3-4**:
- Fix 3.1 (Dual partials)
- Fix 3.2 (Regime-aware trails)
- Paper trading validation

**Day 5**:
- Live deployment
- Monitor for 1 session

### Week 3: Signal Quality + Risk Management (Phases 4-5)
**Day 1-2**:
- Fix 4.1 (Signal logging)
- Fix 4.2 (Momentum relaxation)

**Day 3-4**:
- Fix 5.1 (Circuit breaker)
- Fix 5.2 (Position sizing)

**Day 5**:
- Full system replay test (14 days)
- Generate comparison report

---

## Success Metrics

### Target Improvements (After All Phases)
| Metric | Current | Target | Method |
|--------|---------|--------|--------|
| Profit Factor | 1.12 | 1.45+ | Better exits + stops |
| Expectancy | +0.45 pts | +1.2 pts | Payoff ratio fix |
| Payoff Ratio | 0.86 | 1.15+ | ATR stops + partials |
| Win Rate | 55% | 58%+ | PUT improvements |
| Avg Win | +12.72 pts | +16.5 pts | Dual partials |
| Avg Loss | -14.74 pts | -11.0 pts | ATR stops |
| PUT P&L | +21 pts | +120 pts | Symmetric scoring |
| Max Daily Loss | -72 pts | -40 pts | Circuit breaker |

### Validation Checkpoints
After each phase:
1. Run replay on last 14 days
2. Compare metrics vs baseline
3. Verify no regressions in win rate
4. Check trade count (should increase 20-30% after S4/R4 softening)
5. Validate duplicate trades eliminated

---

## Rollback Plan

If any phase degrades performance:
1. **Immediate**: Revert to previous execution.py / signals.py
2. **Preserve**: Keep logging improvements (Fix 4.1)
3. **Isolate**: Test failed fix in isolation on replay
4. **Adjust**: Tune parameters (e.g., S4/R4 penalty from -10 → -15)
5. **Retest**: Replay validation before re-deployment

**Rollback Trigger**: 
- Win rate drops below 50%
- Profit factor drops below 1.0
- 3 consecutive losing days in live trading

---

## Files to Modify

### Phase 1
- `execution.py` (duplicate guard, ATR stops)
- `signals.py` (S4/R4 softening)
- `option_exit_manager.py` (dynamic stops)

### Phase 2
- `entry_logic.py` (PUT scoring)
- `signals.py` (PUT ADX gate)

### Phase 3
- `option_exit_manager.py` (dual partials, regime trails)

### Phase 4
- `signals.py` (signal logging)
- `entry_logic.py` (momentum scoring)

### Phase 5
- `execution.py` (circuit breaker, position sizing)
- `config.py` (risk parameters)

---

## Next Steps

1. **Review this plan** - Confirm priorities align with your goals
2. **Backup current code** - `git commit -m "Pre-improvement baseline"`
3. **Start Phase 1** - Implement Fix 1.1 (duplicates) first
4. **Replay test** - Validate each fix before moving to next
5. **Paper trade** - 1 full day validation before live
6. **Monitor live** - First session with 1-lot size only

**Ready to begin implementation?** I can start with Fix 1.1 (duplicate trade elimination) immediately.
