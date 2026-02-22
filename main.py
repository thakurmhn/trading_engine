# ===== main.py (FIXED) =====
"""
ROOT CAUSE OF NO SIGNALS (from 14:44 log):

BUG 1 — run_strategy() fails silently every iteration due to Fyers quote API error
  Log: [SPOT REFRESH FAILED] NSE:NIFTY50-INDEX: 'd'
  Code in run_strategy():
      quote = fyers.quotes(data={"symbols": sym})
      spot_price = quote["d"][0]["v"]["lp"]   ← KeyError 'd' when API returns error dict
      ...
      continue   ← skips everything for this symbol

  Result: paper_order() is NEVER called. No signals possible.

BUG 2 — run_strategy() uses wrong candle source
  update_candles_and_signals() fetches from Fyers history API (yesterday only).
  The actual live candles are built by data_feed.py from websocket ticks
  and stored in tick_db. These two are completely separate sources.
  The log shows "[LIVE 3M] ... 112 rows" from tick_db but run_strategy
  never sees those candles.

BUG 3 — run_strategy() has sleep_until_next_boundary(180) inside asyncio
  This blocks the entire event loop for up to 3 minutes.
  asyncio.sleep(1) in main_strategy_code() is meaningless because
  run_strategy() synchronously sleeps for 3 minutes inside it.
  Result: order socket, PnL fetch, chase_order all freeze for 3 minutes.

FIX:
  Remove run_strategy() from the hot loop entirely.
  main_strategy_code() now directly:
    1. Fetches today's candles from tick_db (the correct live source)
    2. Builds indicators via build_indicator_dataframe()
    3. Calls paper_order() or live_order() directly every second
    4. Exit checks run every second (paper_order handles de-dup internally)
  
  run_strategy() is kept for REPLAY mode only (unchanged).
  Fyers quote API is no longer in the critical path.
  spot_price comes from the websocket LTP (most recent tick).
"""

import asyncio
import time
import logging
import pandas as pd
import pendulum as dt
import warnings
from datetime import datetime, timedelta

from config import time_zone, MODE, symbols, account_type, strategy_name
from execution import paper_order, live_order, run_strategy, risk_info
from data_feed import (
    fyers_socket, fyers_order_socket, chase_order,
    fyers_async, tick_db, tick_aggregator, symbols, spot_price as _ws_spot
)
from orchestration import (
    build_indicator_dataframe,
    fetch_fyers_history_warmup,
    fetch_fyers_intraday,
)
from indicators import (
    calculate_cpr,
    calculate_traditional_pivots,
    calculate_camarilla_pivots,
    resolve_atr,
    daily_atr,
)

warnings.filterwarnings("ignore", category=UserWarning, module="pkg_resources")

# ANSI COLORS
RESET   = "\033[0m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
RED     = "\033[91m"
MAGENTA = "\033[95m"
GRAY    = "\033[90m"
CYAN    = "\033[96m"

symbols = ["NSE:NIFTY50-INDEX"]


# ─────────────────────────────────────────────────────────────────────────────
# STARTUP: print daily pivot levels
# ─────────────────────────────────────────────────────────────────────────────

def print_daily_levels():
    """Print pivot levels and ATR regime for each symbol at startup."""
    for sym in symbols:
        hist_data = tick_db.fetch_candles("15m", use_yesterday=True, symbol=sym)
        if hist_data is None or hist_data.empty:
            logging.warning(f"[DAILY LEVELS] No historical data for {sym}")
            continue

        prev_day = hist_data.iloc[-1]
        ph, pl, pc = float(prev_day['high']), float(prev_day['low']), float(prev_day['close'])

        cpr_levels  = calculate_cpr(ph, pl, pc)
        trad_levels = calculate_traditional_pivots(ph, pl, pc)
        cam_levels  = calculate_camarilla_pivots(ph, pl, pc)

        daily_atr_val = daily_atr(hist_data)
        atr_val, atr_src = resolve_atr(pd.DataFrame(), daily_atr_val)
        atr_display = f"{atr_val:.2f}" if atr_val else "N/A"
        atr_regime  = "HIGH" if (atr_val and atr_val > 120) else "LOW"

        logging.info(
            f"{GREEN}[{sym}] "
            f"CPR: P={cpr_levels['pivot']} TC={cpr_levels['tc']} BC={cpr_levels['bc']} | "
            f"Trad: P={trad_levels['pivot']} R1={trad_levels['r1']} S1={trad_levels['s1']} | "
            f"Cam: R3={cam_levels['r3']} S3={cam_levels['s3']} | "
            f"ATR={atr_display} ({atr_src}, {atr_regime}){RESET}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# CANDLE FETCH: get today's indicator-enriched candles from tick_db
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# SESSION WARMUP CACHE — Fyers historical data loaded once at startup
# ─────────────────────────────────────────────────────────────────────────────
# Populated in warmup_fyers_history() called from run() before main loop.
# Never re-fetched during the session — stable base for indicator computation.
_base_3m:  dict = {}   # {symbol: DataFrame}  — 5 days consistent Fyers 3m candles
_base_15m: dict = {}   # {symbol: DataFrame}  — 5 days consistent Fyers 15m candles
_warmup_done: set = set()


def warmup_fyers_history():
    """
    Called ONCE at startup before the main loop begins.
    Fetches 5 trading days of 3m + 15m candles from Fyers Historical API
    for every symbol and stores them in _base_3m / _base_15m caches.

    Why at startup and not per-bar:
        Fyers API has rate limits. Fetching every 3 minutes would burn quota.
        Historical data (past days) never changes — fetch once, cache forever.

    This gives every indicator (ADX14, CCI20, SuperTrend, EMA) enough warmup
    bars to produce valid values from the first intraday candle:
        5 days × 75 bars/day = 375 3m bars
        5 days × 25 bars/day = 125 15m bars (ADX14 needs 28+, CCI20 needs 20+)
    """
    logging.info(f"{GREEN}[WARMUP] Fetching Fyers historical data for {symbols}...{RESET}")
    for sym in symbols:
        try:
            df3, df15 = fetch_fyers_history_warmup(sym, days=5)
            _base_3m[sym]  = df3
            _base_15m[sym] = df15
            _warmup_done.add(sym)
            logging.info(
                f"{GREEN}[WARMUP] {sym} ready: "
                f"3m={len(df3)} bars, 15m={len(df15)} bars{RESET}"
            )
        except Exception as e:
            logging.error(f"[WARMUP ERROR] {sym}: {e}", exc_info=True)
            _base_3m[sym]  = pd.DataFrame()
            _base_15m[sym] = pd.DataFrame()


def get_fyers_live_candles(sym: str) -> tuple:
    """
    Build indicator-enriched DataFrames for the current session.

    Data source hierarchy:
        1. PRIMARY: Fyers historical (warmup cache) + tick_aggregator (intraday)
           - Consistent, exchange-validated historical bars
           - Real-time candles from WebSocket ticks (in-memory, no SQLite latency)
        2. FALLBACK A: Fyers Historical API with include_today=True
           - Used when tick_aggregator has no candles (bot started mid-session
             or WebSocket was down)
        3. FALLBACK B: SQLite tick_db
           - Last resort — data may be inconsistent but better than nothing
           - Logs a clear warning when this path is taken

    Staleness detection:
        If last WebSocket tick > 5 minutes ago → WebSocket may be disconnected.
        Switches to Fallback A and logs WARNING so operator knows.

    Returns (df_3m, df_15m) ready for build_indicator_dataframe().
    Empty DataFrames if all sources fail.
    """
    try:
        base_3m  = _base_3m.get(sym,  pd.DataFrame())
        base_15m = _base_15m.get(sym, pd.DataFrame())

        # ── Check WebSocket staleness ────────────────────────────────────────
        stale_seconds = tick_aggregator.last_tick_age_seconds(sym)
        ws_alive      = stale_seconds < 300   # 5 minutes threshold

        if not ws_alive and tick_aggregator.candle_count(sym, "3m") > 0:
            logging.warning(
                f"{YELLOW}[STALE] {sym} last tick {stale_seconds:.0f}s ago "
                f"— WebSocket may be down. Switching to Fyers API fallback.{RESET}"
            )

        # ── Intraday candles: tick_aggregator (primary) ──────────────────────
        intra_3m  = tick_aggregator.get_candles(sym, "3m")
        intra_15m = tick_aggregator.get_candles(sym, "15m")

        # ── Fallback A: Fyers intraday API ───────────────────────────────────
        # Triggered if: aggregator has no candles OR WebSocket is stale
        if intra_3m.empty or not ws_alive:
            if not ws_alive or intra_3m.empty:
                logging.info(
                    f"{CYAN}[FALLBACK A] {sym}: using Fyers intraday API "
                    f"(aggregator_bars={tick_aggregator.candle_count(sym, '3m')}, "
                    f"ws_age={stale_seconds:.0f}s){RESET}"
                )
                fa_3m, fa_15m = fetch_fyers_intraday(sym)
                if not fa_3m.empty:
                    intra_3m  = fa_3m
                if not fa_15m.empty:
                    intra_15m = fa_15m

        # ── Fallback B: SQLite DB ────────────────────────────────────────────
        if intra_3m.empty:
            logging.warning(
                f"{YELLOW}[FALLBACK B] {sym}: Fyers intraday unavailable. "
                f"Using SQLite DB (data may be inconsistent).{RESET}"
            )
            intra_3m  = tick_db.fetch_candles("3m",  use_yesterday=False, symbol=sym)
            intra_15m = tick_db.fetch_candles("15m", use_yesterday=False, symbol=sym)

        # ── Merge: historical base + intraday ───────────────────────────────
        def _merge(base: pd.DataFrame, intra: pd.DataFrame) -> pd.DataFrame:
            if base.empty and intra.empty:
                return pd.DataFrame()
            if base.empty:
                return intra.copy()
            if intra.empty:
                return base.copy()

            combined = pd.concat([base, intra], ignore_index=True)

            # Determine the datetime column to deduplicate on
            if "date" in combined.columns:
                combined["date"] = pd.to_datetime(combined["date"], utc=False)
                if combined["date"].dt.tz is None:
                    combined["date"] = combined["date"].dt.tz_localize("Asia/Kolkata")
                combined = (
                    combined
                    .drop_duplicates(subset=["date"])
                    .sort_values("date")
                    .reset_index(drop=True)
                )
            elif "time" in combined.columns:
                combined = (
                    combined
                    .drop_duplicates(subset=["time"])
                    .sort_values("time")
                    .reset_index(drop=True)
                )
            return combined

        df_3m  = _merge(base_3m,  intra_3m)
        df_15m = _merge(base_15m, intra_15m)

        # ── Build indicators ─────────────────────────────────────────────────
        if not df_3m.empty:
            df_3m  = build_indicator_dataframe(sym, df_3m,  interval="3m")
        if not df_15m.empty:
            df_15m = build_indicator_dataframe(sym, df_15m, interval="15m")

        # ── Log data source summary ──────────────────────────────────────────
        src_label = (
            "Fyers+WebSocket" if (not base_3m.empty and not intra_3m.empty)
            else "Fyers-only"  if not base_3m.empty
            else "WebSocket-only"
        )
        logging.info(
            f"{GRAY}[CANDLE SRC] {sym} source={src_label} "
            f"3m={len(df_3m)} bars (base={len(base_3m)}, intra={len(intra_3m)}) "
            f"15m={len(df_15m)} bars ws_age={stale_seconds:.0f}s{RESET}"
        )

        return df_3m, df_15m

    except Exception as e:
        logging.error(f"[GET LIVE CANDLES] {sym}: {e}", exc_info=True)
        return pd.DataFrame(), pd.DataFrame()


# ─────────────────────────────────────────────────────────────────────────────
# MAIN STRATEGY LOOP (FIXED)
# ─────────────────────────────────────────────────────────────────────────────

async def main_strategy_code():
    """
    Main async loop. Runs every second.

    FIX: No longer calls run_strategy() which:
      - Required Fyers quote API (fails intermittently → [SPOT REFRESH FAILED])
      - Used Fyers history candles (wrong source — live candles are in tick_db)
      - Blocked the event loop with sleep_until_next_boundary(180)

    Now directly:
      - Fetches tick_db candles (correct live source, no API call needed)
      - Builds indicators
      - Calls paper_order() or live_order() every second
      - Exit checks fire every second regardless of candle boundaries
    """
    today    = dt.now(time_zone).date()
    end_time = dt.datetime(today.year, today.month, today.day, 15, 30, tz=time_zone)

    # Cache: only rebuild indicators when candle count changes (every ~3 min)
    candle_cache = {sym: (pd.DataFrame(), pd.DataFrame(), 0) for sym in symbols}
    # (df_3m, df_15m, last_3m_count)
    # candle_count now tracks tick_aggregator completed candles (not SQLite rows)

    logging.info(f"{GREEN}[MAIN] Strategy loop started. End time={end_time}{RESET}")

    while True:
        ct = dt.now(time_zone)

        # Stop after session end
        if ct > end_time.add(minutes=2):
            logging.info("[MAIN] Session ended. Shutting down.")
            return

        # ── Order management (every 5 seconds) ─────────────────────────────
        if ct.second % 5 == 0:
            try:
                order_response = await fyers_async.orderbook()
                order_df = (pd.DataFrame(order_response["orderBook"])
                            if order_response.get("orderBook") else pd.DataFrame())
                chase_order(order_df)

                pos1 = await fyers_async.positions()
                pnl  = int(pos1.get("overall", {}).get("pl_total", 0))
                logging.info(f"{GRAY}[PnL] Live broker PnL={pnl}{RESET}")

            except Exception as e:
                logging.debug(f"[ORDERBOOK/PNL ERROR] {e}")

        # ── Strategy ────────────────────────────────────────────────────────
        if MODE != "STRATEGY":
            await asyncio.sleep(1)
            continue

        for sym in symbols:
            try:
                # Detect new candle from tick_aggregator (primary) or SQLite (fallback)
                current_count = tick_aggregator.candle_count(sym, "3m")
                if current_count == 0:
                    # Aggregator empty — bot may have started mid-session
                    # Fall back to SQLite count to detect changes
                    _fb = tick_db.fetch_candles("3m", use_yesterday=False, symbol=sym)
                    current_count = len(_fb) if _fb is not None else 0

                # Rebuild indicators when a new 3m candle is available
                cached_3m, cached_15m, last_count = candle_cache[sym]
                if current_count != last_count or cached_3m.empty:
                    df_3m, df_15m = get_fyers_live_candles(sym)
                    candle_cache[sym] = (df_3m, df_15m, current_count)
                    logging.info(
                        f"{GRAY}[CANDLE REFRESH] {sym} "
                        f"3m={len(df_3m) if not df_3m.empty else 0} "
                        f"15m={len(df_15m) if not df_15m.empty else 0}{RESET}"
                    )
                else:
                    df_3m, df_15m = cached_3m, cached_15m

                if df_3m is None or df_3m.empty:
                    logging.debug(f"[MAIN] No 3m candles for {sym}, skipping")
                    continue

                # Call order function — handles exit every call, entry de-duped per candle
                if account_type.upper() == "PAPER":
                    paper_order(df_3m, hist_yesterday_15m=df_15m, mode="LIVE")
                else:
                    live_order(df_3m, hist_yesterday_15m=df_15m)

            except Exception as e:
                logging.error(f"[STRATEGY ERROR] {sym}: {e}", exc_info=True)

        await asyncio.sleep(1)


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def run():
    fyers_socket.connect()
    fyers_order_socket.connect()
    time.sleep(2)  # allow sockets to connect

    # ── Warmup: fetch consistent Fyers historical data before loop starts ────
    # This gives indicators (ADX, SuperTrend, CCI, EMA) enough bars to produce
    # valid values from the very first intraday candle.
    # Called here (not inside async loop) — runs synchronously once.
    warmup_fyers_history()

    try:
        asyncio.run(main_strategy_code())
    except KeyboardInterrupt:
        logging.info("[MAIN] Interrupted.")
    finally:
        logging.info("[MAIN] Terminated.")


if __name__ == "__main__":
    print_daily_levels()
    run()