# Trading Engine – Session Memory

## Project Overview
- **Root**: `c:\Users\mohan\trading_engine`
- **Branch**: `feature-enhancement`
- **Stack**: Python 3.13, pandas, numpy, pendulum, fyers_apiv3, pandas_ta

## Key Files
| File | Purpose |
|------|---------|
| `execution.py` | Core order/exit logic (`check_exit_condition`, `cleanup_trade_exit`, scalp gates) |
| `option_exit_manager.py` | HFT exit engine (DTS, momentum exhaustion, volatility mean-reversion) |
| `st_pullback_cci.py` | Supertrend Pullback + CCI Rejection entry/exit module |
| `test_st_pullback_cci.py` | 35 tests for st_pullback_cci (all passing) |
| `test_exit_logic.py` | 71 tests for exit logic (all passing) |
| `test_phase4.py` | 33 tests for Phase 4 zone/pulse scoring + regime-adaptive exits |
| `regime_context.py` | RegimeContext frozen dataclass + builder (Phase 3) |
| `test_regime_context.py` | 36 tests for regime_context (all passing) |
| `compression_detector.py` | 15m compression/expansion state machine (`CompressionState`) |
| `test_compression_detector.py` | 30 tests for compression_detector (all passing) |
| `test_phase5_regime.py` | 23 tests for Phase 5 regime attribution (all passing) |

## Architecture Decisions

### execution.py exit precedence (check_exit_condition)
1. HFT override (OptionExitManager) – highest
2. SL – always active, ungated
3. Scalp PT/SL (early return when scalp_mode=True)
4. Minimum-hold gate (bars_held=0, no pt_hit → suppress)
5. TG / PT deferred/booking
6. Contextual (trailing stop, OSC, ST_FLIP, reversal, EMA plateau, time exit)

### Suppression whitelist
- MOMENTUM_EXHAUSTION suppressed if bars_held < 2
- Any HFT reason suppressed if bars_held <= 0 **except** SL_HIT and PT_HIT

### State dict pitfall
`state.get("stop", 0)` returns None (not 0) when the key exists with value None.
**In tests**: never include stop/pt/tg keys in state when value is None – omit them so the default=0 fallback works.

## Test Infrastructure (test_exit_logic.py)
- Stubs all heavy deps via `sys.modules` before importing execution.py
- Stubs: config, setup, indicators, signals, orchestration, position_manager, day_type, fyers_apiv3
- `_base_state()` omits stop/pt/tg keys when None to avoid TypeError at trail-stop comparison

## st_pullback_cci.py – Key Details
- `STEntryConfig`: st_period=10, st_mult=3, cci_period=14, tg_rr_ratio=1.0, rr_ratio=2.0
- `PullbackTracker`: priority order – (1) far→arm, (2) approaching→check trigger, (3) newly near→arm
- Broker: BrokerAdapter ABC + FyersAdapter, ZerodhaKiteAdapter, AngelOneAdapter, CcxtAdapter
- Returns signal dict with: side, trigger, sl, tg, pt, cci14, atr, st3m_line

## execution.py – Gate Tags
- `[ENTRY BLOCKED][ST_CONFLICT]` – 15m/3m biases differ
- `[ENTRY BLOCKED][ST_SLOPE_CONFLICT]` – 3m slope mismatches bias
- `[ENTRY ALLOWED][ST_SLOPE_OVERRIDE]` – slope conflict overridden by reversal/CPR+pivot
- `[REVERSAL_SIGNAL]` – reversal_detector.py fired (side, score, strength, pivot_zone)
- `[REVERSAL_OVERRIDE]` – oscillator extreme flipped to confirmation in entry_logic.py
- `[DAY_TYPE]` – daily_sentiment.py compute_intraday_sentiment() day classification
- `[ENTRY ALLOWED][ST_BIAS_OK]` – biases + slope confirmed
- `[EXIT AUDIT]` – written on every exit via audit()
- `[EXIT][SL_HIT]`, `[EXIT][TG_HIT]`, `[EXIT][SCALP_PT_HIT]`, `[EXIT][SCALP_SL_HIT]`

## compression_detector.py – Key Details
- `detect_compression(df_15m)` — 3 conditions on last 3 15m bars: range<0.45×ATR, cluster<1.2×maxSingle, net_move<0.5×ATR
- `detect_expansion(df_15m, zone)` — candle_range>1.3×ATR, close outside zone high/low
- `CompressionState` — `market_state`: NEUTRAL → ENERGY_BUILDUP → VOLATILITY_EXPANSION (strings, not module constants)
- `update()` guard: `len<3` only applies in NEUTRAL state; ENERGY_BUILDUP can process single bars
- ATR column is `atr14` (same name for all timeframes); NOT `ATR_15m`
- Signal dict keys: side, entry_price, sl, tg, pt, source="COMPRESSION_BREAKOUT", score=75, strength="HIGH"
- Integration: `_compression_state` module-level in execution.py; replay uses per-loop `_comp_state`
- Replay: compression entry dispatched before quality gate, stale signal consumed in `pm.is_open()` block
- `notify_trade_result(is_loss)` — activates `_cooldown_bars_remaining=5` on loss; wired into `process_order()` for COMPRESSION_BREAKOUT exits

## Profitability Fixes (Audit → Implementation)
| Fix | File | Status |
|-----|------|--------|
| P1-A: theta decay gate | option_exit_manager.py | Done |
| P1-B: scalp [SCALP_OVERRIDE] log | execution.py | Done |
| P1-C: false breakout cooldown + notify wiring | compression_detector.py + execution.py | Done |
| P2-A: ADX scoring 0/5/10/15 quartile (+15 max) | entry_logic.py | Done |
| P2-B: CPR weight 5→15, wide=-5 | entry_logic.py | Done |
| P2-C: partial TG exit (50% qty, SL ratchet) | execution.py | Done |
| P2-D: trade_class SCALP/TREND separation | execution.py | Done |
| P3-A: compute_daily_sentiment() | entry_logic.py + daily_sentiment.py | Done |
| P3-C: paper slippage ±4 pts | execution.py | Done |
| P3-D: MAX_TRADES_PER_DAY 20→8 | config.py | Done |
| P3-E: vol reversion min 3 bars + 4 bars_held | option_exit_manager.py | Done |
| bars_held forwarded to hf_mgr.check_exit() | execution.py | Done |
| P3-B: dynamic exit threshold (composite score gate) | option_exit_manager.py | Done |
| P4-A: balance zone VAH/VAL proxy + [BALANCE_ZONE] log | daily_sentiment.py | Done |
| P4-B: Camarilla R4/S4 reversal detection + [CAMARILLA_BIAS] log | daily_sentiment.py | Done |
| P4-C: CPR width pre-classification + [CPR_PRECLASS] log | daily_sentiment.py | Done |
| P4-D: predict_opening_expansion() + [COMPRESSION_FORECAST] log | compression_detector.py | Done |
| P5-A: ATR-scaled SL (2×/2.5×/1.2× ATR via ADX tier) | execution.py | Done |
| P5-B: [TREND_LOSS] tag on non-scalp SL exits | execution.py | Done |
| P5-C: trend_loss_count in log_parser + dashboard | log_parser.py, dashboard.py | Done |
| P6-A: Oscillator gate widened defaults + ATR expansion tier | execution.py | Done |
| P6-B: OSC_EXTREME block log moved after Cases 1-4 (not counted when relief fires) | execution.py | Done |
| P6-C: osc_override — removed NARROW_CPR requirement | execution.py | Done |
| P6-D: Slope conflict Path C (ADX < SLOPE_ADX_GATE=20 → bypass) | execution.py + config.py | Done |
| P6-E: ATR-expansion for sl_mult in build_dynamic_levels (survivability fix) | execution.py | Done |
| Phase 3: RegimeContext engine | regime_context.py | Done |
| Phase 4: Zone/Pulse scoring + regime-adaptive exits | execution.py + entry_logic.py | Done |
| Phase 5: Dashboard regime attribution | log_parser.py + dashboard.py | Done |

> Phase 5 details: `memory/phase5_regime_dashboard.md`

## Key Integration Points (execution.py)
- `check_exit_condition()` line ~1044: `hf_mgr.check_exit(..., bars_held=bars_held)` — bars_held wired
- `process_order()` after `state["lifecycle_state"]="EXIT"`: calls `_compression_state.notify_trade_result(is_loss=True)` when source==COMPRESSION_BREAKOUT and pnl_value<0

## entry_logic.py WEIGHTS (current — post P2 revision)
| Dimension | Weight | Notes |
|-----------|--------|-------|
| trend_alignment | 15 | both 15m+3m=15, HTF only=7 |
| rsi_score | 5 | hard-block does filtering; score reduced |
| cci_score | 15 | >100=10, >150=+5 |
| vwap_position | 5 | weight released to ADX+CPR |
| pivot_structure | 15 | Acceptance=15, Rejection/Breakout=10 |
| momentum_ok | 15 | boolean; 15 if True |
| cpr_width | 15 | NARROW=+15, NORMAL=0, WIDE=-5 |
| adx_strength | 15 | <18=0, 18-25=5, 25-35=10, 35+=15 |
| **Total** | **100** | |

## Key New Files
- `trade_classes.py` — ScalpTrade / TrendTrade dataclasses (P2-D); ScalpTrade.scalp_mode=True always, TrendTrade.scalp_mode=False always
- `contract_metadata.py` — Fyers instruments API wrapper; ContractMetadataCache singleton; get_lot_size/get_expiry/filter_intrinsic
- `expiry_manager.py` — Expiry lifecycle manager; ExpiryManager singleton; check_roll/get_valid_contracts; logs [CONTRACT_ROLL]
- → See `memory/contract_expiry.md` for full details
- `reversal_detector.py` — `detect_reversal(candles_3m, camarilla_levels, atr)` → signal dict or None
  - Conditions: EMA9/EMA13 stretch ≥ 1.5×ATR + pivot zone (S3/S4 for CALL, R3/R4 for PUT) + RSI<25/CCI<-200 (CALL) or RSI>75/CCI>+200 (PUT)
  - Oscillator extremes are CONFIRMATION here (flipped from standard gate)
  - Signal keys: side, reason, entry_price, target, stop, score (0-100), strength, ema9, ema13, stretch, pivot_zone, osc_confirmed, atr

## Reversal Detector Integration
- `entry_logic.check_entry_condition(..., reversal_signal=None)` — new optional param
  - When reversal_signal matches side: RSI exhaustion guard bypassed, RSI directional filter bypassed
  - Reversal bonus added to breakdown: HIGH(score≥75)=15, MEDIUM=10, WEAK=5
  - `[REVERSAL_OVERRIDE]` logged when oscillator extreme is flipped to confirmation
- `execution._trend_entry_quality_gate(..., reversal_signal=None)` — new optional param
  - ST_SLOPE_CONFLICT override: Path A = reversal_signal aligned + score≥50; Path B = NARROW_CPR + compressed_cam + close_above_r4/close_below_s4
  - `[ENTRY ALLOWED][ST_SLOPE_OVERRIDE]` logged with override_reason; `[ENTRY BLOCKED][ST_SLOPE_CONFLICT]` only if no override applies
  - `detect_reversal()` called at each of 3 gate call sites (warmup, paper, live, replay)

## daily_sentiment.py – New Functions (Mar 2026)
- `classify_day_type(day_type_pred, gap_tag, balance_tag)` → "TREND_DAY" | "RANGE_DAY" | "GAP_DAY" | "BALANCE_DAY" | "NEUTRAL_DAY"
  - Priority: GAP_DAY > BALANCE_DAY > TREND_DAY > RANGE_DAY > NEUTRAL_DAY
- `compute_intraday_sentiment(today_open, today_high, today_low, prev_close, prev_high, prev_low, cpr_levels, camarilla_levels, ...)` — call at 09:30, 09:45, periodically
  - Returns all daily_sentiment keys + day_type_tag, open_bias_tag, gap_tag, balance_tag, vs_close_tag, cpr_width
  - Logs `[DAY_TYPE]` with all classification tags immediately

## OSC Relief Override (Mar 2026)
- `_trend_entry_quality_gate` Case 4: after logging `[ENTRY BLOCKED][OSC_EXTREME]`, checks if close < S4−ATR (PUT) or close > R4+ATR (CALL)
  - If true: logs `[OSC_RELIEF][S4/R4_BREAK] side=PUT/CALL reason=...` and sets `st_details["osc_relief_override"]=True`, returns True
  - Audit trail: BLOCK is always logged first, then RELIEF override
- `detect_signal` now accepts `osc_relief_active=False`; passed from `st_details.get("osc_relief_override", False)` at all 3 call sites
- `check_entry_condition` now accepts `osc_relief_active=False`; adds `bd["relief_override"]=10` when True
- Log tag: `[OSC_RELIEF][S4/R4_BREAK]` — parsed by log_parser into `oscillator_relief_count`
- Score bonus tag: `[OSC_RELIEF][SCORE_BONUS][CALL/PUT]` — debug level

## log_parser.py – New SessionSummary Fields (Mar 2026)
- `day_type_tag: str` — parsed from `[DAY_TYPE]` log
- `cpr_width_tag: str` — parsed from `[DAY_TYPE]` cpr_width field
- `reversal_trades_count: int` — count of `[REVERSAL_OVERRIDE]` fired
- `st_slope_override_count: int` — count of `[ENTRY ALLOWED][ST_SLOPE_OVERRIDE]`
- `oscillator_relief_count: int` — count of `[OSC_RELIEF][S4/R4_BREAK]` fired
- `reversal_pnl_attribution: float` property — net P&L from reversal trades
- `to_dict()` includes all new fields

## dashboard.py – Text Report Extensions (Mar 2026)
- `_write_text_report`: new DAY CLASSIFICATION section (day_type_tag, cpr_width, open_bias_tag, gap_tag, balance_tag)
- New REVERSAL DETECTOR section (reversal_trades, reversal P&L, slope overrides) when fired
- `compare_sessions` aggregate includes reversal_trades_count, reversal_pnl_attribution, st_slope_override_count

## option_exit_manager.py — OptionExitConfig new fields (P3-B)
- `exit_threshold_base: int = 45` — base for composite score threshold
- `dynamic_threshold_enabled: bool = True` — disable gate by setting False
- `check_exit()` now accepts `adx_value: float = 0.0` param
- `_check_composite_exit_score(price, adx_value)` — new gate: score 0-100 vs threshold 40-55
  - ADX≥35 → +10 threshold, ADX≥25 → +5 threshold (strong trend = hold longer)
  - profit≥80% → -10 threshold (protect gains = exit sooner)
  - Fires `COMPOSITE_SCORE_EXIT` + logs `[DYNAMIC_EXIT_THRESHOLD]`
- `_volatility_mean_reversion()` now logs `[VOL_REVERSION_REFINED]` on fire

## Log Tags added
- `[DYNAMIC_EXIT_THRESHOLD]` — option_exit_manager.py P3-B composite gate
- `[VOL_REVERSION_REFINED]` — option_exit_manager.py P3-E when structure confirmed
- `[SLIPPAGE_MODELED]` — execution.py on every paper entry/exit fill
- `[MAX_TRADES_CAP]` — execution.py when trade_count >= max_trades (paper + live)
- `[BALANCE_ZONE]` — daily_sentiment.py P4-A VAH/VAL position at debug level
- `[CAMARILLA_BIAS]` — daily_sentiment.py P4-B Camarilla level mapping + reversal at debug level
- `[CPR_PRECLASS]` — daily_sentiment.py P4-C CPR width classification at debug level
- `[COMPRESSION_FORECAST]` — compression_detector.py P4-D expansion forecast at info level (ENERGY_BUILDUP) or debug (others)

## daily_sentiment.py – Key Details (P4)
- `_score_balance_zone(prev_high, prev_low, prev_close)` — VAH=prev_high-0.20×range, VAL=prev_low+0.20×range; returns (bull_pts, bear_pts, zone_pos_tag, reasons)
- `_score_camarilla_position(prev_close, r3, r4, s3, s4, prev_high=None, prev_low=None)` — reversal tags: REVERSAL_FROM_R4 (prev_high>r4 AND close<r3), REVERSAL_FROM_S4 (prev_low<s4 AND close>s3)
- `_predict_cpr_day_type(tc, bc, atr_value, prev_high, prev_low)` — ratios: <0.25→TRENDING(-5 threshold,+2 hold), >0.80→RANGE(+8 threshold,-3 hold), else NEUTRAL
- `CompressionState.predict_opening_expansion()` — if ENERGY_BUILDUP: strength>=3→85%, >=2→70%, else→55%; NEUTRAL/other→EXPANSION_UNLIKELY

## Test Suite Summary
- **Total validated (Phase 5)**: 618 pass across all suites
- `test_phase5_regime.py` — **23 pass**
- `test_dashboard.py` — **247 pass**
- `test_phase4.py` — **33 pass**
- `test_regime_context.py` — **36 pass**
- `test_exit_logic.py` — **71 pass**
- `test_profitability_fixes.py` — **208 pass**, 1 pre-existing failure, 2 skipped
- Run with: `-k "not TestExecutionConstants and not TestPartialTGExit"`
- **Pre-existing failure**: `test_osc_extreme_block_logged_when_truly_blocked` — log msg changed to `BIAS_MISALIGN_BLOCKED` in prior phase

### TestOscillatorGateTuning setup note
- `setUpClass` must `sys.modules.pop("execution", None)` to force fresh import
- Must patch `day_type` stub: add `make_day_type_classifier`, `apply_day_type_to_pm`, `DayType`, `DayTypeResult`, **`DayTypeClassifier`** attributes if stub lacks them (test_exit_logic.py stubs day_type without these)
- `DayTypeClassifier` must be patched **unconditionally** (separate `if not hasattr` block) since stub may already have `make_day_type_classifier` but not `DayTypeClassifier`
- Use `adx_min=14.0` default in `_run_gate` so ADX=15 candles pass the WEAK_ADX gate without interfering with oscillator tests

### Bug fixed: oscillator_blocks counter
- `osc_blocks` was never incremented because `_RE_ENTRY_BLOCKED` handler `continue`d before the OSC_EXTREME check
- Fix: move `osc_blocks += 1` into the `_RE_ENTRY_BLOCKED` handler when `subtype == "OSC_EXTREME"`

### _RE_EXIT_RICH (log_parser.py)
- Captures `[EXIT][PAPER/LIVE reason] SIDE contract Entry=N Exit=N Qty=N PnL=N (points=N) BarsHeld=N`
- Emitted by `execution.py` `cleanup_trade_exit()` — NOT captured by any previous regex
- Priority: struct > rich_exit > V1 pairs > EXIT AUDIT fallback
- ANSI codes (`\x1b[93m...\x1b[0m`) stripped by `_strip()` before matching
- `SessionSummary.exit_reason_counts` (dict) — attribute name; `to_dict()` key is `exit_reasons`

## Oscillator Gate Tuning (governance fix — Mar 2026)

### execution.py — _trend_entry_quality_gate changes
| Change | Before | After |
|--------|--------|-------|
| Default RSI range | [35, 65] | [30, 70] |
| Default CCI range | [-120, +120] | [-150, +150] |
| `osc_override` CPR condition | `cpr_width == "NARROW" AND compressed_cam` | `compressed_cam` only |
| OSC_EXTREME block log placement | Before Case 4 (S4/R4 relief) | After Case 4 — only logs when truly blocked |
| ATR expansion tier | None | ATR_HIGH (1.5×MA): +5 RSI/+30 CCI; ATR_ELEVATED (1.3×MA): +3/+20 |
| Slope conflict Path C | None | ADX < SLOPE_ADX_GATE (20.0): bypass slope conflict in low-trend env |

### config.py — new constant
- `SLOPE_ADX_GATE = float(os.getenv("SLOPE_ADX_GATE", "20.0"))` — added after TREND_ENTRY_ADX_MIN

### Log tags added
- `[OSC_OVERRIDE][TREND_CONFIRMED]` now includes `atr_tier=ATR_HIGH|ATR_ELEVATED|ATR_DEFAULT`
- `[ENTRY BLOCKED][OSC_EXTREME]` now includes `atr_tier=...`
- `st_details["atr_expand_tier"]` — new key in gate diagnostics dict

## Dashboard Module (log_parser.py + dashboard.py)

### New file: log_parser.py
- `LogParser(log_path).parse()` → `SessionSummary` dataclass
- `parse_session(path)` / `parse_multiple(paths)` convenience functions
- Handles TWO log formats (new TRADE OPEN/EXIT pairs + legacy EXIT AUDIT)
- Arrow variants `→`/`->` and currency `₹`/`Rs` both handled; FIFO open matching

### dashboard.py additions
- `save_report_json(trades, summary, path)` → JSON with `{generated_at, summary, trades}`
- `generate_full_report(log_path, output_dir)` → CSV + JSON + chart + text report

## Volatility Context Layer (Mar 2026)
> Full details: `memory/volatility_context.md`
- `volatility_context.py` — India VIX fetch (NSE:INDIAVIX-INDEX), tier CALM/NEUTRAL/HIGH
- `greeks_calculator.py` — BSM Greeks + IV back-solve via py_vollib (`pip install py_vollib`)
- `check_entry_condition(..., vix_tier=None, greeks=None)` — 2 new params
  - `bd["vol_context"]`: HIGH=+5, CALM=-5, NEUTRAL=0 per side
  - `bd["theta_penalty"]`: -8 when `|theta|>5 pts/day`
  - Position sizing: each of (HIGH VIX, high vega, low confidence) cuts 1 lot; floor=1, cap=DEFAULT_LOT_SIZE
- Log tags: `[VIX_CONTEXT]`, `[GREEKS]`, `[VOL_CONTEXT][SCORE_ADJUST]`, `[POSITION_SIZE]`
- log_parser: 4 new fields: `vix_tier_count`, `greeks_usage_count`, `theta_penalty_count`, `vega_penalty_count`
- `_scan_file()` return tuple: 21 → 25 elements
- dashboard: VOLATILITY CONTEXT section in text report + comparison table
- `compare_sessions(baseline_paths, fixed_paths, output_dir)` → head-to-head comparison
- `_write_text_report(session, path)` → human-readable session diagnostics
- CLI: `python -m dashboard report <log> [-o dir]`
- CLI: `python -m dashboard compare --baseline <logs> --fixed <logs> [-o dir]`

### Validated against real logs
- Feb 22 (legacy format): 37 trades, 43.2% win, -182.24 pts ✓
- Feb 24 (new format): 184 trades, 46.2% win, +447.18 pts ✓
- Feb 26 (live): 2 trades, 100% win, +5.27 pts ✓
- Exit reasons correctly parsed: ST_FLIP_2, MAX_HOLD, TRAIL_STOP, EOD_EXIT, etc.

## DayTypeClassifier Integration (Item #6 — Mar 2026)

### execution.py changes
- Import: added `DayTypeClassifier` to `from day_type import (...)` — line 43
- Module-level: `_paper_dtc`, `_paper_dtc_date`, `_live_dtc`, `_live_dtc_date` after `_compression_state` (line 106)
  - Use plain `= None` not `Optional[DayTypeClassifier] = None` — `Optional` not imported
- **paper_order**: DTC init/update block inserted after `cam_pre =` (before `breakdown_ctx =`)
  - Init when `_paper_dtc is None or _paper_dtc_date != _today_paper` using `make_day_type_classifier`
  - Update every call: `_paper_day_type = _paper_dtc.update(candles_3m)`
  - Lock at midday: `ct.hour*60+ct.minute >= 720 and confidence in ("MEDIUM","HIGH")`
  - `_paper_day_type_tag` now prefers DTC `.name.value` over gap-based fallback
  - `day_type_result=_paper_day_type` (full DayTypeResult, not string) passed to `_trend_entry_quality_gate`
  - PM modifiers applied after `trail_step = levels["trail_step"]`: override `trail_step` from `pm_trail_step`, store `pm_max_hold`
  - `paper_info[leg].update()` gains `"pm_max_hold": _pm_max_hold_paper`, `"day_type_modifier": signal_modifier`
- **live_order**: Same pattern as paper_order using `_live_dtc`, `_live_dtc_date`, `_live_day_type`
- Log tags: `[DAY TYPE] Paper/Live classifier initialized`, `[DAY_TYPE][PM] PAPER/LIVE trail_step→... pm_max_hold→...`
- Replay already fully integrated — no changes needed

## User Preferences
- PEP8-compliant, production-ready code
- No extra comments or docstrings on unchanged code
- Tests must all pass before declaring done
