# Architecture Change Summary — v2.0
## From: SQLite-first indicators | To: Fyers API warmup + in-memory tick aggregation

---

## What Changed and Why

### The Core Problem
The old architecture used SQLite as the candle source for indicators.
SQLite is an audit log — it was never designed to be a reliable indicator feed:

| SQLite Problem | Indicator Impact |
|---------------|-----------------|
| Post-market flat candles (15:30–18:45) | EMA decayed toward flat price, RSI froze at stale value, Supertrend line converged to close price — all confirmed in 2026-02-20 replay log |
| CCI on zero-range bar (high=low=close) | mean_dev=0 → division by zero → `cci20=NA` — confirmed in replay log at ~13:06 |
| is_partial=1 candles in DB | Indicators computed on incomplete OHLCV |
| Missing ticks / websocket gaps | Silent holes in candle series distort rolling windows |
| No warmup at market open | First 14–28 bars had NaN ADX/Supertrend — no signals possible for ~45 min |

### The New Architecture

```
PRE-MARKET (9:00–9:14 IST)
  main.py → do_warmup()
    → MarketData.warmup()
      → Fyers API: 3 days × 3m bars   (~225 bars, all indicators fully warm)
      → Fyers API: 10 days × 15m bars (~250 bars, ADX/ST fully warm)
      → Stored in _warmup_3m / _warmup_15m (in RAM, immutable)
      → prev_day OHLC extracted → pivots printed to log

INTRADAY (9:15–15:30 IST)
  WebSocket tick arrives
    → data_feed.onmessage()
      → tick_db.insert_tick()           (audit log — SQLite)
      → market_data.on_tick()           (in-memory CandleAggregator)
        → CandleAggregator.on_tick()
          → _is_market_hours() check    (rejects post-15:30 ticks silently)
          → accumulates OHLCV in RAM
          → on slot change: emits completed candle to _candles_3m list

  Strategy loop (every second)
    → market_data.get_candles(sym)
      → checks if new completed candle available (count changed)
      → if YES: _merge_warmup_and_live() + build_indicator_dataframe()
      → if NO:  returns cached df (zero compute)
    → paper_order(df_3m, df_15m)        (exit every second, entry de-duped)

POST-MARKET (>15:30 IST)
  → CandleAggregator._is_market_hours() rejects all ticks
  → No new candles emitted
  → Indicator state frozen at last valid 15:30 bar
  → get_candles() returns same cached df until shutdown
```

---

## Files Changed

### New Files
| File | Purpose |
|------|---------|
| `market_data.py` | Authoritative data layer. CandleAggregator + Fyers warmup + indicator cache |
| `execution_patch.py` | Patch instructions for execution.py and position_manager.py |

### Modified Files
| File | Key Changes |
|------|-------------|
| `orchestration.py` | `_strip_post_market()` added; CCI flat-bar guard; supertrend always 3-return; RSI staleness check |
| `data_feed.py` | `onmessage()` delegates to `market_data.on_tick()`, not `tick_db.build_candles_from_ticks()` |
| `main.py` | `do_warmup()` calls Fyers API before market open; strategy loop uses `md.get_candles()` |

### Unchanged Files
`entry_logic.py`, `signals.py`, `position_manager.py`, `day_type.py`, `indicators.py`, `order_utils.py`, `config.py`, `setup.py`, `tickdb.py`

---

## Bug Fixes Confirmed by Replay Log (2026-02-20)

### Bug 1 — CCI = NA on flat bars
**Log evidence:** `cci20=NA` appearing for 40+ consecutive bars after 13:06 replay time
**Root cause:** Post-market candles have identical H/L/C. CCI mean_dev = 0. Division by zero.
**Fix:** `calculate_cci()` in `orchestration.py` — `md.replace(0, np.nan)` before division
**Guard 2:** `CandleAggregator._is_market_hours()` prevents post-market ticks from ever creating these candles in live mode

### Bug 2 — Post-market bars processed
**Log evidence:** `POST_MARKET: 66 bars` in signal blockers; indicator log continuing to 19:13 IST
**Root cause:** SQLite DB contains rows until 18:45. replay loop processed all of them.
**Fix (live):** `CandleAggregator.on_tick()` rejects ticks where `_is_market_hours()` = False
**Fix (replay):** `_strip_post_market()` in `build_indicator_dataframe()` + `_filter_market_hours()` in `MarketData._warmup_replay()`

### Bug 3 — RSI frozen at 45.23 for 40+ bars
**Log evidence:** `rsi14=45.23` repeated 40+ times in replay log
**Root cause:** RSI EWM smoothing converges when close price is flat (post-market candles)
**Fix:** Same as Bug 2 — flat candles never reach indicator computation in live mode
**Detection:** `_check_rsi_staleness()` warns in log if last 6 closes are identical

### Bug 4 — MAX_HOLD exit on still-trending trade
**Log evidence:** `[EXIT SIGNAL] reason=MAX_HOLD exit_px=201.23 gain=+48.23 peak=+51.20 held=20bars trail_updates=0`; underlying continued to ~25634 after exit
**Fix:** `_should_extend_max_hold()` in `execution_patch.py` — extends MAX_HOLD by 8 bars when trail active + 15m bias aligned + profitable

### Bug C1 (from prior audit) — supertrend() return value mismatch
**Root cause:** `candle_builder.py` expected 2 return values; `orchestration.py` produced 3
**Fix:** `supertrend()` in `orchestration.py` always returns `(line, bias, slope)` — 3 values

---

## How to Verify (Empirical Checklist)

### Pre-market warmup verification
```
[WARMUP] NSE:NIFTY50-INDEX 3m: 225 historical bars (3 days)
[WARMUP] NSE:NIFTY50-INDEX 15m: 248 historical bars (10 days)
[WARMUP] prev_day H=25600 L=25400 C=25571 (2026-02-19)
[CPR] P=25457 TC=25471 BC=25443 | Cam: R3=25461 S3=25447
```
If warmup is skipped or < 30 bars → Fyers API call failed → check token.

### Intraday tick flow verification
```
[TICK] NSE:NIFTY50-INDEX ltp=25432.5 vol=0.0
[NEW CANDLE] bar=1 t=2026-02-20 09:18:00 c=25435.00 bias=UP rsi=NaN adx=NaN
```
First ~14 bars: ADX/ST = NaN (expected — needs warmup bars, which are from Fyers history).
After bar 15+: all indicators populated.

### Post-market guard verification
After 15:30 IST in live mode: no new `[NEW CANDLE]` log lines should appear.
`[TICK]` lines may continue (websocket keeps feeding), but no candle emissions.

### CCI fix verification (replay mode)
Run `python execution.py --date 2026-02-20 --db ticks_2026-02-20.db`
Verify: `cci20=NA` no longer appears after 13:00. Should see `cci20=NaN` at most for the first few warmup bars, then valid values throughout session.

### MAX_HOLD extension verification (replay)
Look for:
```
[MAX_HOLD EXTENDED] trail_active=True 15m_aligned=True held=20 max=20+8
```
Trade should continue past bar 811 until trail stop fires or OSC_EXHAUSTION.

---

## SQLite Role (After Redesign)

| Purpose | Uses SQLite? |
|---------|-------------|
| Indicator computation (live) | NO — in-memory only |
| Indicator computation (replay) | YES — but filtered through `_filter_market_hours()` |
| Raw tick persistence (audit) | YES — `tick_db.insert_tick()` still runs |
| Replay / backtest candles | YES — `MarketData._warmup_replay()` reads SQLite |
| Pivot levels at startup | NO — Fyers API historical data |

SQLite is now correctly scoped: write-only in live mode, read-only in replay mode.