# Trading System Profitability Diagnostic Report

## Executive Summary
- Net expectancy across last 14 trading days (2026-02-25 → 2026-03-10) is **+0.45 pts/trade** with profit factor **1.12**; profitability is fragile and concentrated on 2026-03-08 (+570 pts) while most other days are negative or flat.
- Losses are larger than wins (avg win **+12.72 pts**, avg loss **-14.74 pts**); two losing days (2026-03-03, 2026-03-09) erase a week of small gains.
- Directional asymmetry: **PUT side barely contributes (+21 pts) vs CALL (+198 pts)**, indicating poor bearish signal quality or adverse execution when shorting.
- Exit behaviour dominated by **LOSS / SL_HIT / REVERSAL_EXIT**; protective exits fire frequently while profit-taking / trailing exits are rare, leading to poor payoff skew.
- Entry pipeline shows **very high block rates** (e.g., 4,697 blocks on 2026-03-08; 129 blocks on 2026-03-10) mostly from `ST_CONFLICT`, `ST_SLOPE_CONFLICT`, `DAILY_R4/S4`, and `WEAK_ADX`, creating late/filtered entries yet still allowing trades via alternate paths.
- Latest replay test (2026-03-02, NSE:NIFTY50-INDEX) completed: **5 PUT trades, 100% win, +9107 Rs**, all exited via QUICK_PROFIT with BE stop; runtime (~75s) remains heavy, so replay loop still needs optimization.

## System Architecture Overview
- **Signals & Scoring:** `signals.py`, `entry_logic.py`, `st_pullback_cci.py`, `volatility_context.py` compute CPR width, Camarilla pivots, ADX/RSI/CCI, supertrend slopes, and bias/tilt scores.
- **Execution & Risk:** `execution.py`, `position_manager.py`, `option_exit_manager.py`, `reversal_detector.py`, `failed_breakout_detector.py` manage sizing, cooldowns, exit rules (LOSS_CUT, QUICK_PROFIT, DRAWDOWN_EXIT, BREAKOUT_HOLD, composite score exits).
- **Data/DB:** `tickdb.py`, `data_feed.py`, `market_data.py` ingest tick DBs and cached pickles; `contract_metadata.py`, `expiry_manager.py` for contract rolls.
- **Logging & Dashboard:** `log_parser.py` builds structured trades; `dashboard.py` generates CSV/JSON/text reports; rich exit/entry tags used for diagnostics.

## Code Analysis Findings
- **Entry scoring delay:** `entry_logic.py` aggregates multi-tier weights (supertrend 15m/3m, ADX, CPR, VWAP, pivots). Heavy gating (`ST_CONFLICT`, `ST_SLOPE_CONFLICT`, `DAILY_R4/S4`) often suppresses entries; however, `Entry OK` still logs trades while `Signals fired` stays zero in dashboard reports → signal emission likely bypasses `_RE_SIGNAL` logging (missing `[SIGNAL FIRED]` emit) causing observability gap and potential late confirmation.
- **Risk asymmetry:** In `execution.py`, `MAX_LOSS_PER_TRADE` effectively 10–12 pts while `quick profit` trims only 50% at +10 pts and leaves remainder exposed; combined with `DRAWDOWN_EXIT` at peak-9 pts, payoff skews negative when volatility spikes.
- **PUT path under-optimized:** Bias alignment and tilt (`signals.py`) favour CALL when price above CPR/VWAP; PUT path gains fewer positive score boosts and hits `WEAK_ADX`/`ST_CONFLICT` more often, yielding lower-quality shorts.
- **Stop placement static:** Stops are fixed-point (`LOSS_CUT` threshold) instead of ATR/volatility adaptive; `volatility_context.py` computes tiers but is not wired into `option_exit_manager.py` sizing of stops.
- **Replay performance:** `run_replay_v7.py` loads full tick DB synchronously; no batching/early cutoffs, causing timeouts.

## Log Analysis Results (last 14 days)
- Aggregated from `trades_2026-02-25.csv … trades_2026-03-10.csv`:
  - Trades: **291**, Win rate **55.3%**, Profit factor **1.12**, Expectancy **+0.45 pts**, Avg hold **4.34 bars**.
  - Side P&L: CALL **+198.14 pts**, PUT **+21.31 pts**.
  - Exit mix: LOSS **87**, SL_HIT **23**, REVERSAL_EXIT **12**, WIN **129**, COMPOSITE_SCORE_EXIT **16**, PT/SCALP hits **14** total.
  - Day contributions (pts): 2026-03-08 **+570** dominates; 2026-03-03 **-72**, 2026-03-02 **-70**, 2026-03-09 **-85**, 2026-03-10 **-9**.
- Recent session (2026-03-10, `dashboard.py report options_trade_engine_2026-03-10.log`):
  - Trades 8, Win rate 75%, **Net -9.35 pts** → R/R skewed by two large losers.
  - Blocks 129; top: `ST_CONFLICT` 36, `WEAK_ADX` 28, `DAILY_R4_FILTER` 17, `ST_SLOPE_CONFLICT` 16, `LATE_ENTRY` 10.
- Replay-heavy day (2026-03-08):
  - Trades 186, Win rate 57.5%, Net **+569.99 pts**.
  - Blocks 4,697 dominated by `DAILY_S4_FILTER`, `ST_SLOPE_CONFLICT`, `ST_CONFLICT`.

## Replay Test Results
- `execution.py --date 2026-03-02 --sym NSE:NIFTY50-INDEX --db C:\SQLite\ticks\ticks_2026-03-02.db` (latest replay)
  - Runtime ~75s; replay completed.
  - Trades: 5 (all PUT), **Win 100%**, P&L **+9107 Rs**, avg score 70.
  - Exit mix: all **QUICK_PROFIT** with stop moved to BE; peak UL moves +12.9 -> +47.8 pts.
  - Blockers: `DAILY_CAM_FILTER` 61 bars, `LATE_ENTRY` 15, `NO_SIGNAL` 14, `SIGNAL_SKIP` 13, `COOLDOWN` 10.
  - Artifacts: `signals_NSE_NIFTY50-INDEX_2026-03-02.csv`, `trades_NSE_NIFTY50-INDEX_2026-03-02.csv`.
- `run_replay_v7.py --date 2026-03-10 --db C:\SQLite\ticks\ticks_2026-03-10.db` previously timed out after ~14s; replay loop still needs performance tuning (batching/chunked reads).

## Trade Performance Metrics
- Win rate 55.3%, Profit factor 1.12, Expectancy +0.45 pts/trade.
- Avg win +12.72 pts vs avg loss -14.74 pts (payoff ratio 0.86).
- Avg holding time 4.34 bars; losing trades often exit via LOSS/SL within ≤5 bars (per exit spec).
- Side imbalance: CALL count 139, PUT 152, but CALL P&L >9× PUT.

## Signal Quality Analysis
- Observability: `[SIGNAL FIRED]` lines absent in logs while trades occur → difficult to timestamp signal vs execution; likely masking late confirmation issues.
- Entry gating: High `ST_CONFLICT` / `ST_SLOPE_CONFLICT` implies frequent disagreement between 15m and 3m supertrend; entries that pass may be late because slope alignment is required before dispatch.
- Context filters: `DAILY_R4/S4` blocks numerous trades (especially 2026-03-08), potentially preventing trend participation after initial breakout.
- Volatility filter: `WEAK_ADX` blocks appear even on trend days; thresholds may be too high for opening range, delaying entries until move is advanced.

## Strategy Weaknesses
- **Payoff skew:** Loss magnitude exceeds win magnitude; exits cut winners early (quick profit trim at +10 pts) but allow losses to -14 pts.
- **Directional bias mismatch:** PUT trades underperform, suggesting bearish setups trigger in noisy/down-chop contexts without trend confirmation (2026-03-02 replay is an exception driven by strong bearish day and CALL blocks).
- **Late entry / over-filtering:** Heavy supertrend/pivot gating plus late-entry blocks lead to chasing moves with tight stops.
- **Context blindness in stops:** Stops ignore current ATR/volatility tier; fixed 10–12 pt stop sits inside normal intraday noise.
- **Replay/performance debt:** Replay loop is slow (~75s for 131 bars) and still times out on heavier days; limits iterative tuning.

## Root Cause Analysis (Top 10)
1. **Negative payoff ratio** (avg loss > avg win) – exits allow deeper losses than gains; see `option_exit_manager.py` LOSS_CUT vs QUICK_PROFIT thresholds.
2. **PUT signal weakness** – CALL P&L +198 vs PUT +21; bearish score boosts weaker and more often blocked by `WEAK_ADX`/`ST_CONFLICT` in `signals.py`.
3. **Exit dominated by forced losses** – 110+ trades closed via LOSS/SL_HIT/REVERSAL_EXIT; profitable exits (PT/SCALP/Composite) rare.
4. **Over-filtering by pivots** – `DAILY_R4/S4` filters block thousands of signals on 2026-03-08; likely miss early trend legs, forcing late entries with poor R/R.
5. **Supertrend disagreement noise** – `ST_CONFLICT` + `ST_SLOPE_CONFLICT` top blockers; when alignment finally occurs, price may already have moved.
6. **Lack of volatility-adjusted stops** – fixed point stops ignore VIX tiers computed in `volatility_context.py`, causing stop placement inside expected range.
7. **Signal logging gap** – missing `[SIGNAL FIRED]` lines in logs; hampers diagnosis and suggests signal dispatch bypasses standard path (risk of async timing).
8. **Replay performance bottleneck** – timeouts prevent iterative tuning; likely due to full-history loading without chunking.
9. **Day-type sensitivity insufficient** – despite CPR width scoring, day-level guardrails (e.g., `STARTUP_SUPPRESSION`, `ORB` tags) still allow overtrading on chop days (18 trades on 2026-03-03 with -72 pts).
10. **Position sizing static across regimes** – `position_manager.py` uses fixed lot from config; no reduction during weak ADX / wide CPR leading to outsized losses on low-quality trades.

## Engineering Fix Recommendations
- **Risk/Payoff Rebalance**
  - Increase initial stop to ATR(14)*x and trail dynamically; cap per-trade loss to 0.6× expected reward.
  - Move quick-profit to +15 pts and trail remainder with step tied to realized volatility; add partial at +10/+20 ladder.
- **PUT Path Enhancements**
  - Add bearish-only score bonuses when price < VWAP & CPR bottom with falling ADX; relax `WEAK_ADX` threshold for PUT in downtrends.
  - Mirror CALL tilt logic to avoid asymmetric blocking.
- **Entry Timing**
  - Allow entry when higher TF supertrend aligns but lower TF conflict persists if ADX rising and momentum ignition detected (candle range > 1.5× ATR(5)); tag as “early breakout” with tighter size.
  - Reduce `DAILY_R4/S4` hard blocks to soft filters that require momentum confirmation instead of outright suppression.
- **Context-Aware Stops**
  - Wire `volatility_context.py` tiers into `option_exit_manager.py` to set LOSS_CUT = k * ATR and DRAWDOWN_EXIT = 0.6 * max_favorable_move.
  - Add CPR-width based stop multiplier: narrow CPR → tighter stops; wide CPR → wider + reduced size.
- **Signal Observability**
  - Emit `[SIGNAL FIRED]` before gating in `entry_logic.py` / `signals.py` so dashboard can measure signal-to-entry latency.
  - Log per-gate scores to inspect over-filtering.
- **Replay Performance**
  - Batch tick reads (e.g., 5s bars) and cache indicators; add `--start/--end` time filters; profile hot loops in `run_replay_v7.py`.
- **Position Sizing**
  - Scale lot by bias confidence (score matrix) and volatility tier; halve size when ADX < threshold or CPR is wide.
- **Exit Logic Diversity**
  - Introduce time-based profit protection (e.g., if >5 bars and +8 pts, trail to +4).
  - Add momentum exhaustion detection (volume/CCI reversal) to exit winners later on trend days.

## Priority Improvement Roadmap
1. Implement volatility-adjusted stop/target and quick-profit ladder; backtest vs last 14 days.
2. Add signal logging + latency metrics; rerun dashboard to validate gating impact.
3. Tune PUT-specific thresholds and soften `DAILY_R4/S4` blocks; compare CALL vs PUT P&L.
4. Optimize replay pipeline (chunked loading, caching) to enable daily regression runs.
5. Introduce size scaling by regime/score; enforce max daily loss per side.

## Expected Impact of Fixes
- Raising payoff ratio to >1.2 with ATR-based stops and staggered profit-taking should lift expectancy from +0.45 to ~+1.2 pts/trade (with current 55% win).
- Improved PUT accuracy and reduced over-filtering aim to balance side P&L (+150–200 pts over 14 days).
- Replay performance fixes enable faster iterative tuning, increasing confidence in deployment changes.
