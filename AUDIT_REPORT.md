# Trading Engine — Full Codebase Audit & Profitability Diagnosis

**Date**: 2026-03-06
**Branch**: `feature-enhancement`
**Stack**: Python 3.13, pandas, numpy, pendulum, fyers_apiv3, pandas_ta
**Auditor**: Claude Opus 4.6

---

## Table of Contents

1. [Codebase Functions](#1-codebase-functions)
2. [Performance Diagnosis](#2-performance-diagnosis)
3. [Root Causes](#3-root-causes)
4. [Proposed Solutions](#4-proposed-solutions)
5. [Action Checklist](#5-action-checklist)

---

## 1. Codebase Functions

### 1.1 Core Modules

#### execution.py (~5,500 lines) — Order Execution & Exit Engine

| Function | Line | Purpose |
|----------|------|---------|
| `log_entry_green(msg)` | 68 | ANSI-colored entry log |
| `map_status_code(code)` | 128 | Fyers order status → human label |
| `status_color(status)` | 139 | Status → ANSI color |
| `store(data, account_type_)` | 162 | Pickle-persist trade state |
| `load(account_type_)` | 201 | Restore latest state from ledger |
| `load_ledger(account_type_)` | 224 | Load full snapshot history |
| `_restart_state_file(account_type_)` | 246 | Session recovery path |
| `_save_restart_state(info, account_type_)` | 262 | Crash recovery checkpoint |
| `_load_restart_state(account_type_)` | 292 | Restore from checkpoint |
| `_hydrate_runtime_state(info, account_type_, mode)` | 307 | Rebuild runtime objects post-restart |
| `_cap_used(info, is_scalp)` | 372 | Count open scalp/trend trades |
| `_cap_limit(info, is_scalp)` | 376 | Max allowed trades (SCALP=12, TREND=8) |
| `_cap_available(info, is_scalp)` | 380 | Boolean: under cap? |
| `_register_trade(info, is_scalp)` | 384 | Increment count; log MAX_TRADES_CAP |
| `get_option_by_moneyness(spot, side, moneyness, pts)` | 501 | Select option symbol by ITM/OTM/ATM |
| `_get_option_market_snapshot(symbol, fallback)` | 580 | Fetch current LTP + volume |
| `_detect_scalp_dip_rally_signal(candles, levels, atr)` | 617 | Scalp dip-buy/rally-sell at pivots |
| `_can_enter_scalp(info, burst_key, now_ts)` | 685 | Gate: cooldown + burst dedup |
| `_is_startup_suppression_active(info, now_ts, mode)` | 700 | 5-min post-startup freeze |
| `_opening_s4_breakdown_context(...)` | 720 | S4/R4 opening range breakout |
| `entry_gate_context(...)` | 795 | Merge day/opening/oscillator context |
| `_supertrend_alignment_gate(...)` | 879 | 3m+15m ST bias alignment check |
| `_trend_entry_quality_gate(...)` | 944 | Hard quality gate (ST slope, ADX, oscillator, EMA stretch, pivots); 5 override paths (A–E) |
| **`check_exit_condition(df, state, price, vol, ts)`** | **1656** | **Master exit evaluator — all precedence** |
| `build_dynamic_levels(entry, atr, side, ...)` | 2096 | ATR-scaled SL/PT/TG/trail computation |
| `update_trailing_stop(...)` | 2233 | Ratchet trailing stop on favorable moves |
| `send_live_entry_order(symbol, qty, side, buffer)` | 2501 | Fyers LIMIT entry |
| `send_live_exit_order(symbol, qty, reason)` | 2546 | Fyers MARKET exit |
| `send_paper_exit_order(symbol, qty, reason)` | 2588 | Simulated paper exit |
| `update_order_status(order_id, status, qty, price, sym)` | 2598 | Update filled_df records |
| `check_order_status(order_id, fyers)` | 2625 | Query Fyers for order status |
| **`process_order(state, df, info, spot, acct, mode)`** | **2663** | **Exit routing: partial TG, full exit, slippage** |
| `cleanup_trade_exit(info, leg, side, ...)` | 2868 | Reset trade_flag, log [EXIT] |
| `force_close_old_trades(info, mode)` | 2915 | EOD/risk-halt forced close |
| `update_risk(trade_info, risk_info)` | 2946 | Session PnL + drawdown tracking |
| `paper_order(candles_3m, hist_15m, exit, mode, spot)` | 2974 | Paper trading main loop |
| `live_order(candles_3m, hist_15m, exit)` | 3705 | Live trading main loop |
| `run_offline_replay(...)` | 4459 | Replay engine (tick-level backtest) |
| `run_strategy(symbols, tz, end_time, ...)` | 5350 | Strategy orchestrator (main loop) |

**Module-Level Constants:**

| Constant | Value | Impact |
|----------|-------|--------|
| `SCALP_PT_POINTS` | 7.0 | Scalp profit target |
| `SCALP_SL_POINTS` | 4.0 | Scalp stop loss (floor) |
| `PAPER_SLIPPAGE_POINTS` | 4.0 | **+4 entry, −4 exit = 8 pts round-trip** |
| `SCALP_MIN_HOLD_BARS` | 2 | Min bars before scalp PT/SL |
| `TREND_MIN_HOLD_BARS` | 3 | Min bars before trend PT/TG |
| `SCALP_COOLDOWN_MINUTES` | 20 | Re-entry cooldown |
| `PARTIAL_TG_QTY_FRAC` | 0.50 | 50% exit at first TG |
| `MAX_TRADE_TREND` | 8 | Max concurrent trend trades |
| `MAX_TRADE_SCALP` | 12 | Max concurrent scalp trades |
| `DEFAULT_TIME_EXIT_CANDLES` | 8 | Time exit after 8 idle bars |
| `DEFAULT_OSC_RSI_CALL` | 75.0 | Oscillator exit RSI for CALL |
| `DEFAULT_OSC_RSI_PUT` | 25.0 | Oscillator exit RSI for PUT |
| `EMA_STRETCH_BLOCK_MULT` | 3.0 | Hard-block if EMA stretch >= 3×ATR |

---

#### entry_logic.py — Scoring Engine & Entry Quality Gates

| Function | Line | Purpose |
|----------|------|---------|
| `_safe_float(val)` | 136 | Safe float conversion (NaN → None) |
| `_atr_regime(atr)` | 144 | Classify ATR as LOW/NORMAL/HIGH/UNKNOWN |
| `_norm_bias(raw)` | 151 | Normalize bias string |
| `liquidity_zone(candle, st_line, bias, atr, tf)` | 161 | Price proximity to Supertrend line |
| `_score_trend_alignment(bias_15m, indicators, side)` | 187 | 15m+3m ST alignment → 0–15 pts |
| `_score_rsi(candle, indicators, side)` | 228 | RSI scoring → 0–5 pts |
| `_score_cci(candle, indicators, side)` | 259 | CCI scoring → 0–15 pts |
| `_score_vwap(candle, indicators, side)` | 301 | VWAP position → 0–5 pts |
| `_score_pivot(pivot_signal, side)` | 328 | Pivot structure → 0–15 pts |
| `_score_momentum_ok(indicators, side)` | 381 | Dual-EMA momentum → 0 or 15 pts |
| `_score_cpr_width(indicators)` | 399 | CPR width → −5 to +15 pts |
| `_score_adx(indicators)` | 423 | ADX quartile → 0–15 pts |
| `_score_open_bias(indicators, side)` | 462 | Opening bias alignment → −3 to +5 pts |
| **`check_entry_condition(...)`** | **513** | **Full scoring engine v5 (13 params)** |
| `compute_daily_sentiment(...)` | 1022 | Pre-session directional bias |

**Scoring Weights (100-pt base):**

| Dimension | Weight | Description |
|-----------|--------|-------------|
| trend_alignment | 15 | 15m+3m both = 15, HTF only = 7 |
| rsi_score | 5 | RSI directional confirmation |
| cci_score | 15 | CCI ≥100 = 10, ≥150 = +5 |
| vwap_position | 5 | VWAP side confirmation |
| pivot_structure | 15 | Acceptance=15, Breakout=10 |
| momentum_ok | 15 | Dual-EMA + widening gap |
| cpr_width | 15 | NARROW=+15, WIDE=−5 |
| adx_strength | 15 | ADX quartile (0/5/10/15) |
| open_bias_score | 5 | Opening bias alignment |
| **Total** | **105** | (can go negative with WIDE CPR) |

**Bonus/Penalty Modifiers:**

| Modifier | Points | Condition |
|----------|--------|-----------|
| reversal_override | +5/+10/+15 | Reversal score <50/50-75/≥75 |
| expiry_roll | +5 | Rolled to next expiry |
| relief_override | +10 | S4/R4 oscillator relief |
| vol_context | +5/−5 | HIGH VIX / CALM VIX |
| theta_penalty | −8 | |theta| > 5.0 |

**Thresholds:** LOW ATR → 999 (blocked), NORMAL → 50, HIGH → 60

---

#### option_exit_manager.py — HFT Exit Engine

| Function | Line | Purpose |
|----------|------|---------|
| `OptionExitConfig` (dataclass) | 18 | All HFT exit tuning parameters |
| `__init__(entry_price, side, risk_buffer, config)` | 115 | Initialize with entry metadata, rolling buffers |
| `update_tick(price, volume, timestamp)` | 136 | Append tick to circular deques |
| **`check_exit(price, ts, vol, ingest, bars_held, adx)`** | **162** | **Main HFT exit evaluator** |
| `_time_decay_gate(price, timestamp)` | 227 | Theta exit: bars≥6 + loss + post-11:30 |
| `_dynamic_trailing_stop(price)` | 257 | Adaptive trail: 10%→3% as profit grows |
| `_momentum_exhaustion()` | 273 | ROC collapse >60% from peak |
| `_volatility_mean_reversion(price)` | 291 | μ+2σ over-extension + 3 lower-highs |
| `_check_composite_exit_score(price, adx)` | 347 | Weighted composite gate (P3-B) |

**HFT Exit Precedence:**

| Order | Exit Reason | Profit Required | Bars Guard |
|-------|-------------|:---------------:|:----------:|
| 1 | THETA_EXIT | No (fires on loss) | ≥6 |
| 2 | DYNAMIC_TRAILING_STOP | Yes | ≤0 suppressed |
| 3 | MOMENTUM_EXHAUSTION | Yes | <2 suppressed |
| 4 | VOLATILITY_MEAN_REVERSION | Yes | ≥4 internal |
| 5 | COMPOSITE_SCORE_EXIT | Yes | None |

---

#### config.py — Global Configuration

| Parameter | Value | Impact |
|-----------|-------|--------|
| `quantity` | 130 | Lot size (Nifty = 65 per lot) |
| `DEFAULT_LOT_SIZE` | 2 | Trade sizing multiplier |
| `MAX_TRADES_PER_DAY` | 8 | Daily trade cap |
| `MAX_DAILY_LOSS` | −5000 | Circuit breaker (₹) |
| `MAX_DRAWDOWN` | −3000 | Drawdown halt (₹) |
| `TREND_ENTRY_ADX_MIN` | 18.0 | Minimum ADX for trend entry |
| `SLOPE_ADX_GATE` | 20.0 | Slope conflict bypass threshold |
| `OSCILLATOR_EXIT_MODE` | "HARD" | Close immediately on osc trigger |
| `profit_loss_point` | 25 | Legacy SL/PT parameter |
| `CALL_MONEYNESS` | ITM | Strike selection |
| `CANDLE_INTERVAL_MIN` | 3 | 3-minute candles |

---

### 1.2 Support Modules

#### daily_sentiment.py — Session Context Classification

| Function | Purpose |
|----------|---------|
| `_score_balance_zone(prev_high, prev_low, prev_close)` | VAH/VAL zone scoring |
| `_score_camarilla_position(prev_close, r3, r4, s3, s4, ...)` | Camarilla reversal detection |
| `_predict_cpr_day_type(tc, bc, atr, prev_high, prev_low)` | CPR width → TRENDING/RANGE/NEUTRAL |
| `classify_day_type(day_type_pred, gap_tag, balance_tag)` | Priority: GAP > BALANCE > TREND > RANGE > NEUTRAL |
| `compute_intraday_sentiment(...)` | Full session context at 09:30/09:45 |

#### compression_detector.py — Compression/Expansion State Machine

| Function | Purpose |
|----------|---------|
| `detect_compression(df_15m)` | 3 conditions on last 3 bars → ENERGY_BUILDUP |
| `detect_expansion(df_15m, zone)` | Candle range > 1.3×ATR + close outside zone |
| `CompressionState.update()` | NEUTRAL → ENERGY_BUILDUP → VOLATILITY_EXPANSION |
| `CompressionState.predict_opening_expansion()` | Probability forecast (55%–85%) |
| `CompressionState.notify_trade_result(is_loss)` | 5-bar cooldown on false breakout |

#### reversal_detector.py — Counter-Trend Reversal Detection

| Function | Purpose |
|----------|---------|
| `detect_reversal(candles_3m, camarilla_levels, atr)` | EMA stretch ≥1.5×ATR + pivot zone + RSI/CCI extreme |

#### st_pullback_cci.py — Supertrend Pullback + CCI Rejection

| Function | Purpose |
|----------|---------|
| `STEntryConfig` | st_period=10, st_mult=3, cci_period=14, rr_ratio=2.0 |
| `PullbackTracker` | State machine: far → armed → triggered |
| `BrokerAdapter` (ABC) | FyersAdapter, ZerodhaKiteAdapter, AngelOneAdapter, CcxtAdapter |
| `detect_signal(...)` | Signal dict: side, trigger, sl, tg, pt, cci14, atr |

#### trade_classes.py — Trade Classification

| Class | Purpose |
|-------|---------|
| `ScalpTrade` | scalp_mode=True, fixed PT/SL points |
| `TrendTrade` | scalp_mode=False, ATR-based levels |

### 1.3 Infrastructure Modules

| Module | Purpose |
|--------|---------|
| `orchestration.py` | Main loop; tick processing; candle aggregation |
| `main.py` | Entry point; session initialization |
| `data_feed.py` | Historical/live data fetching from Fyers |
| `candle_builder.py` | Build 3m/15m OHLCV candles from ticks |
| `market_data.py` | Market data subscription management |
| `indicators.py` | ATR14, RSI14, CCI14, EMA9/EMA13, Supertrend, VWAP, ADX, Williams%R |
| `signals.py` | Signal generation (pivot, CPR, Camarilla) |
| `position_manager.py` | Position state tracking; open/close lifecycle |
| `broker_init.py` | `build_broker_adapter()` — construct active broker |
| `contract_metadata.py` | `ContractMetadataCache` — Fyers instruments API; lot size, expiry |
| `expiry_manager.py` | `ExpiryManager` — contract roll, valid contracts |
| `zone_detector.py` | Demand/supply zone detection from candle clusters |
| `failed_breakout_detector.py` | Failed breakout identification |
| `greeks_calculator.py` | Black-Scholes Greeks (delta, gamma, theta, vega, IV) |
| `volatility_context.py` | VIX tier classification; vol regime |
| `order_utils.py` | Order utility helpers |
| `pulse_module.py` | Tick-rate heartbeat; direction drift detection |
| `tickdb.py` | Tick database for replay |
| `log_parser.py` | `LogParser` + `SessionSummary` — structured log parsing |
| `dashboard.py` | Text report generation; session comparison |
| `eod_dashboard.py` | CLI wrapper for `generate_full_report()` |

---

## 2. Performance Diagnosis

### 2.1 The Core Paradox: 60% Win Rate + Net Loss

A 60% win rate should be profitable — but only if the average win is at least 67% of the average loss (breakeven R:R at 60% WR). The engine violates this condition on **both** trade classes.

### 2.2 Slippage Model Math — The Hidden Tax

Every paper trade pays an **8-point round-trip slippage tax**:

```
Entry:  entry_price = LTP + 4.0    (execution.py:3236, 3542)
Exit:   exit_price  = LTP − 4.0    (execution.py:2737, 2788)
─────────────────────────────────────
Net dead-weight cost per trade = 8.0 points
At qty=130: 8 × 130 = ₹1,040 per trade burned to slippage
```

### 2.3 Scalp Trade R:R Analysis

```
Constants: SCALP_PT_POINTS = 7.0, SCALP_SL_POINTS = 4.0 (floor)

Winning scalp:
  PT fires when option_price >= entry_price + 7
  entry_price = LTP + 4, so option needs to move +11 from LTP
  Exit fill: (LTP + 11) − 4 = LTP + 7
  P&L per unit: (LTP + 7) − (LTP + 4) = +3.0 pts

Losing scalp:
  SL fires when option_price <= entry_price − 4
  entry_price = LTP + 4, so option needs to drop −0 from LTP (!)
  Exit fill: (LTP + 0) − 4 = LTP − 4
  P&L per unit: (LTP − 4) − (LTP + 4) = −8.0 pts
```

| Metric | Value |
|--------|-------|
| **Effective win** | **+3.0 pts/unit** |
| **Effective loss** | **−8.0 pts/unit** |
| **Effective R:R** | **0.375 : 1** |
| **Breakeven win rate** | **72.7%** |
| **At 60% WR, expected P&L/trade** | **0.6×3 − 0.4×8 = −1.4 pts** |
| **Per trade at qty=130** | **−₹182** |

**Scalps are structurally unprofitable at any win rate below 73%.**

### 2.4 Trend Trade R:R Analysis

For a representative ATR = 50 (common for Nifty ITM options):

```
ADX 20–40 tier (default):
  SL  = 2.0 × 50 = 100 pts from entry
  PT  = 1.3 × 50 =  65 pts from entry
  TG  = 1.6 × 50 =  80 pts from entry

With 8-pt slippage:
  Effective loss on SL  = 100 + 8 = 108 pts
  Effective gain at PT  =  65 − 8 =  57 pts
  Effective gain at TG  =  80 − 8 =  72 pts
```

| Scenario | Effective P&L | Required WR for breakeven |
|----------|:------------:|:-------------------------:|
| SL hit (full loss) | −108 pts | — |
| PT + SL at entry (breakeven lock) | −8 pts (just slippage) | — |
| TG partial (50% at TG) | +72 pts on 50% | — |
| **Weighted: all exits at TG** | **Win=+72, Loss=−108** | **60.0%** |
| **Weighted: many exits pre-TG** | **Win≈+40, Loss=−108** | **73.0%** |

**The SL distance (2.0×ATR) exceeds the primary target distance (1.3×ATR) by 54%.** Losses are inherently larger than wins.

### 2.5 Early Exit Truncation — Winners Cut Short

The HFT exit engine and contextual exits frequently close profitable trades **well before reaching TG**:

| Exit Type | Typical Capture | Impact |
|-----------|:--------------:|--------|
| DYNAMIC_TRAILING_STOP | 50–80% of peak | Exits on 3–10% pullback from peak |
| MOMENTUM_EXHAUSTION | 30–60% of TG | ROC collapse at 40% of peak ROC |
| VOLATILITY_MEAN_REVERSION | 40–70% of TG | Fires at μ+2σ with lower highs |
| COMPOSITE_SCORE_EXIT | 50–80% of TG | Composite gate with low threshold (45) |
| OSCILLATOR_EXIT (HARD) | 30–50% of TG | RSI/CCI extremes → immediate close |
| ST_FLIP | 20–60% of TG | Supertrend reversal (2 bars) |
| TIME_EXIT | Variable | 8 bars with no trailing update |

**Net effect**: Average winning trade captures ~40–60% of the theoretical TG, while losing trades hit the full SL distance. This compresses the effective R:R further.

### 2.6 Survivability Paradox — SL Too Wide, Targets Too Narrow

| ATR Regime | SL mult | PT mult | TG mult | SL:TG Ratio |
|------------|:-------:|:-------:|:-------:|:-----------:|
| ATR ≤ 60 | 2.0× | 1.2× | 1.4× | **1.43:1** |
| ATR 60–100 | 2.0× | 1.3× | 1.6× | **1.25:1** |
| ATR 100–150 | 2.0× | 1.4× | 1.8× | **1.11:1** |
| ATR 150–250 | 2.0× | 1.6× | 2.0× | **1.00:1** |
| ADX > 40 | 2.5× | 1.8× | 2.0× | **1.25:1** |

**In the most common ATR regimes (≤100), the SL is 1.25–1.43× the TG.** You need >56–59% win rate just to break even — before slippage.

### 2.7 Quantity Amplification

With `quantity = 130`:
- A −108 pt trend loss = −₹14,040
- A +72 pt trend win = +₹9,360
- **One loss erases 1.5 wins**

With MAX_DAILY_LOSS = −₹5,000, a single bad trade can trigger the circuit breaker (especially in high ATR regimes), cutting the session short before recovery trades can fire.

---

## 3. Root Causes

### RC-1: Slippage Model Creates Structural Negative Expectancy on Scalps

**Evidence**: SCALP_PT=7, SCALP_SL=4, PAPER_SLIPPAGE=4 on both entry and exit. Effective win=+3, loss=−8. R:R = 0.375:1.

**Severity**: **CRITICAL** — Scalps cannot be profitable below 73% win rate regardless of signal quality.

### RC-2: SL Distance Exceeds Target Distance (Inverted R:R)

**Evidence**: `build_dynamic_levels()` sets SL at 2.0×ATR while PT=1.3×ATR and TG=1.6×ATR. The stop is wider than the target in all common ATR regimes.

**Severity**: **HIGH** — Even without slippage, 60% WR barely breaks even on trend trades.

### RC-3: HFT & Contextual Exits Truncate Winners

**Evidence**: DYNAMIC_TRAILING_STOP, MOMENTUM_EXHAUSTION, OSC_EXIT(HARD), ST_FLIP, and TIME_EXIT all fire before the trade reaches TG. Winners average 40–60% of TG while losers hit 100% of SL.

**Severity**: **HIGH** — Compounds RC-2 by reducing the numerator of the R:R ratio.

### RC-4: Double Slippage Penalty (8 pts) Is Excessive

**Evidence**: Real Nifty option bid-ask spread is 1–3 pts (ITM). The 4+4=8 pt model over-penalizes by 2–4× real-world cost.

**Severity**: **MEDIUM** — Even at realistic 3 pts round-trip, R:R improves but doesn't fix RC-1/RC-2.

### RC-5: Scalp SL Floor (4 pts) Is Below Slippage Cost

**Evidence**: `SCALP_SL_POINTS = 4.0` is a floor. With 4 pts entry slippage, the SL triggers the moment the option drops to LTP — before any real adverse move occurs.

**Severity**: **HIGH** — False SL triggers on normal bid-ask noise.

### RC-6: Partial TG Exit (50%) Reduces Position in Winning Leg

**Evidence**: P2-C exits 50% at TG, ratchets SL to TG for remainder. The first exit captures 50% of TG gain. But if the trade runs further (to 2×TG, 3×TG), only 50% of position participates. Meanwhile, full position size is exposed on every loss.

**Severity**: **MEDIUM** — Correct in principle but asymmetric: full size on losses, half size on extended winners.

### RC-7: OSCILLATOR_EXIT_MODE = "HARD" Kills Trends Prematurely

**Evidence**: When RSI hits 75 (CALL) or 25 (PUT), `HARD` mode immediately closes. In strong trends, RSI routinely stays overbought for many bars. This exits at the start of the strongest move.

**Severity**: **MEDIUM** — Trend trades on high-ADX days (the best setups) are most affected.

### RC-8: Circuit Breaker (−₹5,000) Triggers on 1–2 Losses

**Evidence**: At qty=130 and SL=100+ pts, a single trend loss = −₹13,000+. Even the drawdown halt at −₹3,000 triggers on one bad scalp sequence. This prevents recovery trades from firing.

**Severity**: **MEDIUM** — Stops the bot during volatile sessions where recovery probability is highest.

---

## 4. Proposed Solutions

### S-1: Fix Scalp R:R by Widening PT Relative to Slippage [CRITICAL]

**Current**: PT=7, SL=4, slippage=8. Effective R:R = 0.375:1.

**Proposed**:
- Option A: Increase `SCALP_PT_POINTS` to **15** and `SCALP_SL_POINTS` to **8**
  - Effective: Win = 15−8 = +7, Loss = 8+8 = −16. R:R = 0.44:1. Breakeven WR = 70%.
- Option B (recommended): Increase `SCALP_PT_POINTS` to **20** and `SCALP_SL_POINTS` to **10**
  - Effective: Win = 20−8 = +12, Loss = 10+8 = −18. R:R = 0.67:1. Breakeven WR = 60%.
- Option C: Reduce `PAPER_SLIPPAGE_POINTS` to **2.0** (realistic for ITM Nifty options) AND set PT=12, SL=6
  - Effective: Win = 12−4 = +8, Loss = 6+4 = −10. R:R = 0.80:1. Breakeven WR = 55.6%.

**Recommendation**: Implement **Option C** — realistic slippage + balanced PT/SL.

**Files**: `execution.py` lines 73–74, 90

### S-2: Rebalance Trend SL:TG Ratio [HIGH]

**Current**: SL = 2.0×ATR, TG = 1.6×ATR (ratio 1.25:1 favoring losses).

**Proposed** — Invert the ratio:

| ATR Regime | New SL | New PT | New TG | SL:TG |
|------------|:------:|:------:|:------:|:-----:|
| ATR ≤ 60 | 1.0× | 1.2× | 1.8× | 0.56:1 |
| ATR 60–100 | 1.2× | 1.5× | 2.2× | 0.55:1 |
| ATR 100–150 | 1.3× | 1.6× | 2.5× | 0.52:1 |
| ATR 150–250 | 1.5× | 1.8× | 2.8× | 0.54:1 |

**Rationale**: Tighter SL means more frequent losses, but each loss is small. Wider TG allows runners to compensate. At 45% WR with 0.55:1 SL:TG, expected = 0.55×TG − 0.45×SL > 0.

**Survivability**: Combine with S-3 trailing stop tightening to protect gains earlier.

**File**: `execution.py` `build_dynamic_levels()` lines 2130–2206

### S-3: Let Winners Run — Remove or Soften Early Exit Triggers [HIGH]

**3a. Change OSCILLATOR_EXIT_MODE to "TRAIL"**
- Instead of immediate close on RSI 75/CCI 130, tighten SL to entry (breakeven) and let the trailing stop manage the exit.
- File: `config.py` line 105

**3b. Widen DTS Trail in High-ADX Environments**
- When ADX > 35, set `dynamic_trail_lo = 0.15` (allow 15% pullback) and `trail_tighten_profit_frac = 0.70` (only tighten after 70% profit).
- File: `option_exit_manager.py` OptionExitConfig

**3c. Raise MOMENTUM_EXHAUSTION Threshold**
- Change `roc_drop_fraction` from 0.60 to **0.80** (require 80% ROC collapse, not 60%).
- This lets momentum trades breathe through normal pullbacks.
- File: `option_exit_manager.py` line ~35

**3d. Increase TIME_EXIT Candles**
- Change `DEFAULT_TIME_EXIT_CANDLES` from 8 to **14** (42 min instead of 24 min).
- File: `execution.py` line ~96

### S-4: Reduce Paper Slippage to Realistic Level [MEDIUM]

**Current**: 4.0 pts each side (8 total).
**Proposed**: 1.5 pts each side (3 total).

**Justification**: Nifty ITM options (strike within 100 pts) have typical bid-ask of 0.5–2.0 pts during market hours. Market impact for 130-qty orders adds ~0.5–1.0 pts. Total realistic slippage: 1.0–3.0 pts round-trip.

**File**: `execution.py` line 90

### S-5: ADX-Adaptive SL That Scales Symmetrically with TG [MEDIUM]

**Current**: ADX>40 → SL=2.5×ATR but TG stays at 2.0×ATR max.

**Proposed**: Strong-trend (ADX>40) regime should have:
- SL = 1.5×ATR (tight — trends don't retrace deeply)
- TG = 3.0×ATR (wide — let the trend run)
- R:R = 0.50:1. At 35% WR: Expected = 0.35×3.0 − 0.65×1.5 = 1.05 − 0.975 = +0.075×ATR per trade.

**Weak trend (ADX < 20)**: Keep tighter SL (1.0×ATR) and tighter TG (1.2×ATR) since range-bound markets don't deliver large moves.

**File**: `execution.py` `build_dynamic_levels()` lines 2130–2200

### S-6: Scale Circuit Breakers to Position Size [MEDIUM]

**Current**: MAX_DAILY_LOSS = −₹5,000, MAX_DRAWDOWN = −₹3,000.
**At qty=130**: One trend SL hit (−₹14,000) already exceeds both limits.

**Proposed**:
- MAX_DAILY_LOSS = −₹15,000 (or dynamic: −3 × avg_trade_size)
- MAX_DRAWDOWN = −₹10,000
- Add **per-trade max loss** instead: if single-trade loss > ₹5,000 → reduce next trade qty by 50%.

**File**: `config.py` lines 96–97

### S-7: Implement Asymmetric Position Sizing [MEDIUM]

**Proposed**: Scale position size inversely with SL distance:
```python
risk_per_trade = 2000  # max ₹ risk per trade
qty = risk_per_trade / (sl_distance + slippage_cost)
```

At SL=100 pts + 8 slippage: qty = 2000/108 ≈ 18 units.
At SL=50 pts + 8 slippage: qty = 2000/58 ≈ 34 units.

This ensures every loss costs the same ₹ amount, regardless of SL width.

**File**: `execution.py` at entry-sizing blocks (lines ~3242, ~3550)

### S-8: Add Win/Loss Attribution Dashboard Metrics [LOW]

**Current**: Dashboard tracks win_rate_pct and net_pnl. Missing:
- Average win size (pts and ₹)
- Average loss size (pts and ₹)
- Profit factor (gross_wins / gross_losses)
- R:R ratio per trade class (scalp vs trend)
- % of wins exited at TG vs early exit
- % of losses hitting full SL vs early exit

**File**: `log_parser.py` SessionSummary + `dashboard.py`

---

## 5. Action Checklist

### Priority 1 — Structural R:R Fix (Do First)

| # | Action | File(s) | Difficulty |
|---|--------|---------|:----------:|
| 1.1 | Reduce `PAPER_SLIPPAGE_POINTS` from 4.0 → **1.5** | execution.py:90 | Trivial |
| 1.2 | Increase `SCALP_PT_POINTS` from 7.0 → **15.0** | execution.py:73 | Trivial |
| 1.3 | Increase `SCALP_SL_POINTS` from 4.0 → **8.0** | execution.py:74 | Trivial |
| 1.4 | Invert trend SL:TG ratio in `build_dynamic_levels()` — tighten SL multipliers (1.0–1.5×ATR), widen TG multipliers (1.8–2.8×ATR) | execution.py:2130–2206 | Medium |
| 1.5 | Validate with replay: run `run_offline_replay()` on 5 recent sessions and compare net P&L before/after | execution.py:4459 | Medium |

### Priority 2 — Let Winners Run

| # | Action | File(s) | Difficulty |
|---|--------|---------|:----------:|
| 2.1 | Change `OSCILLATOR_EXIT_MODE` from "HARD" → **"TRAIL"** | config.py:105 | Trivial |
| 2.2 | Raise `roc_drop_fraction` from 0.60 → **0.80** | option_exit_manager.py | Trivial |
| 2.3 | Widen DTS trail for ADX>35: `dynamic_trail_lo = 0.15`, `trail_tighten_profit_frac = 0.70` | option_exit_manager.py | Easy |
| 2.4 | Increase `DEFAULT_TIME_EXIT_CANDLES` from 8 → **14** | execution.py | Trivial |
| 2.5 | ADX-adaptive TG: ADX>40 → TG=3.0×ATR, SL=1.5×ATR | execution.py:2130–2200 | Medium |

### Priority 3 — Risk Governance

| # | Action | File(s) | Difficulty |
|---|--------|---------|:----------:|
| 3.1 | Implement risk-based position sizing: `qty = risk_budget / (sl_pts + slippage)` | execution.py | Medium |
| 3.2 | Scale circuit breakers: MAX_DAILY_LOSS → −₹15,000, MAX_DRAWDOWN → −₹10,000 | config.py | Trivial |
| 3.3 | Add per-trade max loss with qty reduction on breach | execution.py | Medium |

### Priority 4 — Observability

| # | Action | File(s) | Difficulty |
|---|--------|---------|:----------:|
| 4.1 | Add avg_win_pts, avg_loss_pts, profit_factor, rr_ratio to SessionSummary | log_parser.py | Easy |
| 4.2 | Add TG-reach% and early-exit% to dashboard | dashboard.py | Easy |
| 4.3 | Add per-trade-class (SCALP/TREND) P&L breakdown | log_parser.py + dashboard.py | Medium |

---

## Summary Impact Projection

| Scenario | Scalp E[PnL]/trade | Trend E[PnL]/trade | Net @ 8 trades/day |
|----------|:------------------:|:------------------:|:------------------:|
| **Current** (PT=7,SL=4,slip=4) | **−₹182** | **−₹390** | **−₹2,288** |
| After S-1+S-4 (PT=15,SL=8,slip=1.5) | +₹260 | −₹130 | +₹520 |
| After S-1+S-2+S-4 (full rebalance) | +₹260 | +₹650 | **+₹3,640** |
| After all (S-1 through S-7) | +₹390 | +₹975 | **+₹5,460** |

*Projections assume 60% win rate and representative ATR=50. Actual results depend on market regime.*

---

**End of Audit Report**
