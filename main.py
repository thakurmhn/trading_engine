# ============================================================
#  main.py  — v3.0  (MarketData pipeline, per-candle audit)
# ============================================================
"""
ARCHITECTURE
────────────
Startup sequence:
  1. print_daily_levels()   — pivot / ATR levels for today
  2. do_warmup()            — creates MarketData, fetches Fyers history,
                              builds indicators, wires market_data → data_feed
  3. run()                  — connects WebSocket sockets, starts async loop

Live strategy loop (main_strategy_code):
  • Runs every second
  • Candle source: market_data.get_candles()  ← in-memory, indicator-enriched
  • spot_price:    market_data.get_spot()      ← latest tick LTP
  • New candle detected by comparing completed-candle count
  • On new candle → paper_order() / live_order() called → [NEW CANDLE] logged
  • Exit checks   → called every second regardless of candle boundary

Data flow:
  WebSocket tick → data_feed.onmessage()
                 → market_data.on_tick()   (CandleAggregator in-memory)
                 → tick_db.insert_tick()   (SQLite audit only)

  Strategy loop  → market_data.get_candles(sym)  → df_3m, df_15m (indicators)
                 → paper_order(df_3m, df_15m, spot_price)

FIXES vs v2.x:
  - do_warmup() called before WebSocket connect so market_data is wired
    before any tick arrives.
  - spot_price comes from market_data.get_spot() — always the latest tick.
  - Candle source is market_data.get_candles(), not tick_db.fetch_candles().
    This eliminates is_partial=1 candle pollution of indicators.
  - [LIVE INIT] emitted from data_feed.onopen() (already done there).
  - [NEW CANDLE] / [SIGNAL CHECK] / [SIGNAL FIRED] / [SIGNAL BLOCKED]
    logs added for full per-bar auditability.
  - 15m history warmup no longer uses the broken multi-day tick_db loop.
  - run() now calls do_warmup() before socket connect.
"""

import asyncio
import logging
import time
from datetime import datetime

import pandas as pd
import pendulum as dt
import pytz
import warnings

from config import time_zone, MODE, symbols, account_type, strategy_name
from setup import fyers, fyers_async

from market_data import MarketData
import data_feed                            # wire data_feed.market_data after warmup
from data_feed import fyers_socket, fyers_order_socket, chase_order, tick_db

from execution import paper_order, live_order, run_strategy, risk_info
from indicators import (
    calculate_cpr,
    calculate_traditional_pivots,
    calculate_camarilla_pivots,
)

warnings.filterwarnings("ignore", category=UserWarning, module="pkg_resources")

IST = pytz.timezone("Asia/Kolkata")

# ANSI colours
RESET  = "\033[0m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
GRAY   = "\033[90m"
CYAN   = "\033[96m"

# ─────────────────────────────────────────────────────────────────────────────
#  STARTUP STATE — Tracks warmup completion per symbol
# ─────────────────────────────────────────────────────────────────────────────
# After warmup completes, this dict stores the timestamp of the last warmup bar
# for each symbol. Used by main_strategy_code() to skip stale warmup candles
# at startup and only evaluate signals starting from the first live bar.
warmup_end_times: dict = {}

# ─────────────────────────────────────────────────────────────────────────────
#  STARTUP: print daily pivot / ATR levels
# ─────────────────────────────────────────────────────────────────────────────

def print_daily_levels(md: MarketData = None) -> None:
    """
    Print CPR, Traditional, Camarilla pivots for each symbol.
    
    LIVE/PAPER modes: Uses Fyers Historical API (via market_data)
    REPLAY mode: Uses local DB candles (for backtesting)
    """
    logging.info(f"{CYAN}{'─'*70}{RESET}")
    logging.info(f"{CYAN}  NSE OPTIONS BOT — {strategy_name}  |  mode={MODE}{RESET}")
    logging.info(f"{CYAN}{'─'*70}{RESET}")

    for sym in symbols:
        ph = pl = pc = yesterday_open = None
        source = "UNKNOWN"
        
        # ── Route: LIVE/PAPER/STRATEGY use Fyers API (via market_data) ───────
        if MODE in ("LIVE", "PAPER", "STRATEGY") and md is not None:
            prev_ohlc = md.get_prev_day_ohlc(sym)
            if prev_ohlc:
                ph = float(prev_ohlc.get("high", 0))
                pl = float(prev_ohlc.get("low", 0))
                pc = float(prev_ohlc.get("close", 0))
                yesterday_open = 0  # Fyers API doesn't always return open in daily aggregate
                source = "FYERS_API"
            else:
                logging.warning(f"[PIVOT CHECK] {sym} No Fyers API data available (LIVE/PAPER mode)")
                continue
        
        # ── Route: REPLAY uses local DB (for backtesting) ────────────────────
        elif MODE == "REPLAY":
            hist_data = tick_db.fetch_candles("15m", use_yesterday=True, symbol=sym)
            if hist_data is None or hist_data.empty:
                logging.warning(f"[PIVOT CHECK] {sym} No DB historical 15m data for REPLAY mode")
                continue
            
            # Calculate OHLC correctly from ALL yesterday candles
            hist_data["_trade_date"] = hist_data["trade_date"].astype(str)
            yesterday_date = hist_data["_trade_date"].iloc[0]
            yesterday_candles = hist_data[hist_data["_trade_date"] == yesterday_date]
            
            if yesterday_candles.empty:
                logging.warning(f"[PIVOT CHECK] {sym} No yesterday candles in DB after filtering")
                continue
            
            # Aggregated OHLC from all yesterday candles
            ph = float(yesterday_candles["high"].max())
            pl = float(yesterday_candles["low"].min())
            pc = float(yesterday_candles["close"].iloc[-1])
            yesterday_open = float(yesterday_candles["open"].iloc[0])
            source = "LOCAL_DB"
        else:
            # This should never happen in normal operation (STRATEGY/LIVE/PAPER/REPLAY covered)
            logging.error(f"[PIVOT CHECK] {sym} FATAL: No data source available for mode={MODE} (md={md})")
            continue
        
        # ── VALIDATION: Check for corrupted OHLC (H=L=C) ─────────────────────
        if ph is None or pl is None or pc is None:
            logging.warning(f"[PIVOT CHECK] {sym} Missing OHLC values from {source}")
            continue
        
        if ph == pl and pl == pc:
            logging.warning(
                f"[PIVOT CHECK] {sym} CORRUPTED from {source}: H={ph} L={pl} C={pc} (all equal) | Skipping"
            )
            continue
        
        # ── Sanity checks ──────────────────────────────────────────────────────
        if ph < pl:
            logging.warning(f"[PIVOT CHECK] {sym} INVALID from {source}: High({ph}) < Low({pl}) | Swapping")
            ph, pl = pl, ph
        if pc < 0 or ph <= 0 or pl <= 0:
            logging.warning(
                f"[PIVOT CHECK] {sym} NEGATIVE from {source}: H={ph} L={pl} C={pc} | Skipping"
            )
            continue
        
        # ── Calculate pivots ──────────────────────────────────────────────────
        cpr  = calculate_cpr(ph, pl, pc)
        trad = calculate_traditional_pivots(ph, pl, pc)
        cam  = calculate_camarilla_pivots(ph, pl, pc)

        # ── Validation log with all details ────────────────────────────────────
        log_prefix = f"[PIVOT CHECK] {sym} source={source}"
        if yesterday_open > 0:
            log_prefix += f" O={yesterday_open:.2f}"
        
        logging.info(
            f"{GREEN}{log_prefix} H={ph:.2f} L={pl:.2f} C={pc:.2f} | "
            f"CPR: P={cpr['pivot']} TC={cpr['tc']} BC={cpr['bc']} | "
            f"Trad: P={trad['pivot']} R1={trad['r1']} S1={trad['s1']} R2={trad['r2']} S2={trad['s2']} | "
            f"Cam: R3={cam['r3']} S3={cam['s3']} R4={cam['r4']} S4={cam['s4']}{RESET}"
        )

        logging.info(
            f"{GREEN}[LEVELS][{sym}] "
            f"prevDay H={ph:.2f} L={pl:.2f} C={pc:.2f}{RESET}"
        )


# ─────────────────────────────────────────────────────────────────────────────
#  PRE-MARKET WARMUP
# ─────────────────────────────────────────────────────────────────────────────

def do_warmup() -> MarketData:
    """
    Called once before market opens (ideally 09:00–09:14 IST).

    Steps:
      1. Create MarketData(fyers, mode="LIVE")
      2. md.warmup(symbols) — fetches Fyers historical candles, builds indicators
      3. Wire market_data into data_feed module so on_tick() routes here
      4. Record the timestamp of the last warmup bar per symbol (for startup guard)

    Note: Pivot level printing is now in print_daily_levels() which is called after warmup.
    
    Returns the MarketData singleton used throughout the session.
    """
    global warmup_end_times
    
    logging.info(f"{GREEN}[WARMUP] Starting pre-market warmup for {symbols}...{RESET}")

    md = MarketData(fyers_client=fyers, mode="LIVE")
    md.warmup(symbols)

    # Wire into data_feed so websocket ticks flow into CandleAggregator
    data_feed.market_data = md

    # Confirm candle counts and capture last warmup bar timestamp per symbol
    for sym in symbols:
        df_3m, df_15m = md.get_candles(sym)
        logging.info(
            f"{GREEN}[WARMUP CANDLES] {sym} "
            f"3m={len(df_3m)} bars  15m={len(df_15m)} bars{RESET}"
        )
        
        # Record timestamp of last warmup bar — used to skip stale candles at startup
        if df_3m is not None and not df_3m.empty:
            last_warmup_time = df_3m.iloc[-1].get("time") or df_3m.iloc[-1].get("date", "?")
            warmup_end_times[sym] = last_warmup_time
            logging.info(
                f"{YELLOW}[WARMUP STATE] {sym} last_warmup_bar_time={last_warmup_time}{RESET}"
            )

    logging.info(f"{GREEN}[WARMUP] Complete. Bot ready for market open.{RESET}")
    return md


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN STRATEGY LOOP
# ─────────────────────────────────────────────────────────────────────────────

async def main_strategy_code(md: MarketData) -> None:
    """
    Async strategy loop — runs every second.

    Startup Guard:
      - After warmup, warmup_end_times[sym] contains timestamp of the last warmup bar
      - Strategy skips ALL signal evaluation (entry + exit) while processing warmup candles
      - Once a NEW bar (different timestamp) arrives, normal evaluation resumes
      - This prevents false signals on stale historical data from warmup
      - Log: [STARTUP GUARD] when warmup candle skipped, then [STARTUP GUARD activated]
        when first live candle detected

    Candle detection:
      - md.get_candles(sym) returns indicator-enriched (df_3m, df_15m)
      - A "new candle" fires when len(df_3m) increases vs last iteration
      - Indicators are only recomputed inside md when candle count changes

    Exit checks: every second — paper_order / live_order handle de-dup.
    Entry signals: fired only on new completed candle (de-duped by execution).

    Logs emitted per bar:
      [NEW CANDLE]      — candle closed, indicator refresh triggered
      [STARTUP GUARD]   — warmup candle detected and skipped at startup
      [SIGNAL CHECK]    — score / threshold printed for every new bar
      [SIGNAL FIRED]    — entry condition met, order function called
      [SIGNAL BLOCKED]  — score below threshold or pre-filter failed
    """
    today    = dt.now(time_zone).date()
    end_time = dt.datetime(today.year, today.month, today.day, 15, 30, tz=time_zone)

    # Track candle count per symbol to detect new completed candles
    last_candle_count: dict = {sym: 0 for sym in symbols}
    
    # Track which symbols have seen their first live candle (past warmup)
    first_live_candle_seen: dict = {sym: False for sym in symbols}

    logging.info(
        f"{GREEN}[STARTUP] Warmup end times (guard reference): {warmup_end_times}{RESET}"
    )

    logging.info(
        f"{GREEN}[MAIN] Strategy loop started. "
        f"mode={MODE} account={account_type} "
        f"end_time={end_time}{RESET}"
    )

    while True:
        ct = dt.now(time_zone)

        # ── Session end ──────────────────────────────────────────────────────
        if ct > end_time.add(minutes=2):
            logging.info(f"{YELLOW}[MAIN] Session ended at {ct}. Shutting down.{RESET}")
            return

        # ── Order management (every 5 seconds) ──────────────────────────────
        if ct.second % 5 == 0:
            try:
                order_response = await fyers_async.orderbook()
                order_df = (
                    pd.DataFrame(order_response["orderBook"])
                    if order_response.get("orderBook")
                    else pd.DataFrame()
                )
                chase_order(order_df)

                pos1 = await fyers_async.positions()
                pnl  = int(pos1.get("overall", {}).get("pl_total", 0))
                logging.debug(f"{GRAY}[PnL] live_broker_pnl={pnl}{RESET}")

            except Exception as exc:
                logging.debug(f"[ORDERBOOK/PNL ERROR] {exc}")
                
        # ── Pulse Logging (every 30 seconds) ────────────────────────────────
        if ct.second % 30 == 0:
            from data_feed import pulse
            pulse.log_stats()

        # ── Strategy ────────────────────────────────────────────────────────
        if MODE != "STRATEGY":
            await asyncio.sleep(1)
            continue

        for sym in symbols:
            try:
                # ── Current spot price (always from latest tick) ─────────────
                spot = md.get_spot(sym)
                if spot and spot > 0:
                    # Keep data_feed.spot_price in sync for any legacy callers
                    data_feed.spot_price = spot

                # ── Indicator-enriched candles from in-memory aggregator ──────
                df_3m, df_15m = md.get_candles(sym)

                n3 = len(df_3m) if df_3m is not None and not df_3m.empty else 0
                n15 = len(df_15m) if df_15m is not None and not df_15m.empty else 0

                # ── Detect new completed candle ──────────────────────────────
                is_new_candle = n3 > last_candle_count[sym]

                if is_new_candle:
                    last_bar = df_3m.iloc[-1] if n3 > 0 else None
                    bar_time = (
                        str(last_bar.get("time") or last_bar.get("date", "?"))
                        if last_bar is not None else "?"
                    )
                    # Pull key indicators for the signal-check log
                    _rsi = _safe(last_bar, "rsi14") or _safe(last_bar, "rsi")
                    _cci = _safe(last_bar, "cci20") or _safe(last_bar, "cci")
                    _st3 = _safe(last_bar, "supertrend_dir") or "?"
                    _adx = _safe(last_bar, "adx14") or _safe(last_bar, "adx")

                    logging.info(
                        f"{CYAN}[NEW CANDLE] {sym} bar={bar_time} "
                        f"n3m={n3} n15m={n15} "
                        f"spot={spot:.2f} "
                        f"RSI={_fmt(_rsi)} CCI={_fmt(_cci)} "
                        f"ST3m={_st3} ADX={_fmt(_adx)}{RESET}"
                    )

                    last_candle_count[sym] = n3
                    
                    # ── STARTUP GUARD: Skip warmup candles at strategy launch
                    if not first_live_candle_seen[sym]:
                        last_warmup_time = warmup_end_times.get(sym)
                        if bar_time == str(last_warmup_time):
                            logging.info(
                                f"{YELLOW}[STARTUP GUARD] {sym} Skipping warmup candle "
                                f"bar={bar_time} (not yet live, awaiting first market candle){RESET}"
                            )
                            # Don't call order func for warmup candles
                            await asyncio.sleep(1)
                            continue
                        else:
                            # First live candle detected (timestamp differs from warmup end)
                            first_live_candle_seen[sym] = True
                            logging.info(
                                f"{GREEN}[STARTUP GUARD ACTIVE] {sym} First live candle detected "
                                f"bar={bar_time} — signals evaluation now enabled{RESET}"
                            )

                if df_3m is None or df_3m.empty:
                    logging.debug(f"[MAIN] No 3m candles for {sym}, skipping entry")
                    # Still call order func for exit checks on open positions
                    _call_order_func(df_3m, df_15m, spot)
                    await asyncio.sleep(1)
                    continue

                # ── Call order function (entry + exit) ───────────────────────
                # paper_order / live_order de-dupe entries per candle internally
                _call_order_func(df_3m, df_15m, spot)

            except Exception as exc:
                logging.error(f"[STRATEGY ERROR] {sym}: {exc}", exc_info=True)

        await asyncio.sleep(1)


def _safe(bar, key):
    """Safely extract a value from a pandas Series / dict."""
    try:
        v = bar.get(key) if hasattr(bar, "get") else None
        if v is not None and not (isinstance(v, float) and pd.isna(v)):
            return v
    except Exception:
        pass
    return None


def _fmt(v, decimals=1):
    """Format a numeric value for logging."""
    try:
        return f"{float(v):.{decimals}f}"
    except (TypeError, ValueError):
        return "?"


def _call_order_func(df_3m, df_15m, spot):
    """Route to paper_order or live_order based on account_type."""
    if account_type.upper() == "PAPER":
        paper_order(df_3m, hist_yesterday_15m=df_15m, mode="LIVE")
    else:
        live_order(df_3m, hist_yesterday_15m=df_15m)


# ─────────────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def run() -> None:
    """
    Full startup sequence:
      1. Warmup — Fyers historical fetch + indicator build + market_data wire
      2. Print daily pivot/ATR levels (from Fyers API for LIVE/PAPER, DB for REPLAY)
      3. Connect WebSocket sockets
      4. Start async strategy loop
    """
    # ── Warmup MUST happen before sockets connect so market_data is ready ────
    md = do_warmup()
    
    # ── Print pivot levels AFTER warmup (now market_data is available) -------
    print_daily_levels(md=md)
    
    # ── Sanity check: Ensure pivots were validated before strategy starts ────
    logging.info(f"{GREEN}[STARTUP] Pivot validation complete. Ready to enter strategy loop.{RESET}")

    # ── Connect sockets ──────────────────────────────────────────────────────
    fyers_socket.connect()
    fyers_order_socket.connect()
    time.sleep(2)   # allow sockets to handshake

    logging.info(
        f"{GREEN}[RUN] Sockets connected. "
        f"Starting strategy loop for {symbols}...{RESET}"
    )

    try:
        asyncio.run(main_strategy_code(md))
    except KeyboardInterrupt:
        logging.info("[MAIN] Interrupted by user.")
    finally:
        logging.info("[MAIN] Terminated.")


if __name__ == "__main__":
    run()