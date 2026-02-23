# ============================================================
#  main.py  — v2.0  (MarketData-driven)
# ============================================================
"""
ARCHITECTURE CHANGE (v2 vs v1)
───────────────────────────────
v1 problems:
  ✗ Indicators built from SQLite candles (unreliable — gaps, flat bars, post-market)
  ✗ Warmup fetched from SQLite (not Fyers API) → stale / missing data
  ✗ No pre-market warmup — first bars had NaN indicators
  ✗ Post-market bars (15:30+) poisoned EMA, RSI, Supertrend state
  ✗ CCI = NA on flat candles after 15:30

v2 design:
  ✓ PRE-MARKET (before 9:15): Fyers API fetches WARMUP_3M_DAYS of 3m history
    and WARMUP_15M_DAYS of 15m history. Indicators fully populated before
    first live tick arrives. This matches exactly how charting platforms work.

  ✓ INTRADAY: Every WebSocket tick → market_data.on_tick() → CandleAggregator
    builds candles purely in RAM. No SQLite reads for indicators.

  ✓ INDICATOR REBUILD: Only on new completed candle (every ~3 min).
    Full warmup history + today's completed candles → build_indicator_dataframe().
    Cached between candle closes — zero recompute cost each second.

  ✓ POST-MARKET: _is_market_hours() filter in CandleAggregator silently
    rejects all ticks after 15:30 IST. Aggregator never closes a post-market
    candle. Indicators never see flat post-market bars.

  ✓ SQLite: Raw tick audit log only. Never read for indicators in LIVE mode.

STARTUP SEQUENCE:
  1. 09:00–09:14  →  warmup()  fetches Fyers history, builds initial indicators
  2. 09:15        →  WebSocket connects, ticks start flowing
  3. Each 3m close → get_candles() returns freshly enriched df
  4. Strategy loop calls paper_order() / live_order() every second (exit checks)
     and on new candle (entry signal evaluation)
  5. 15:30        →  strategy shuts down, EOD exit fires in position_manager
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
import data_feed                            # sets data_feed.market_data after warmup
from data_feed import fyers_socket, fyers_order_socket, chase_order, tick_db

from execution import paper_order, live_order, run_strategy, risk_info
from indicators import (
    calculate_cpr,
    calculate_traditional_pivots,
    calculate_camarilla_pivots,
)

warnings.filterwarnings("ignore", category=UserWarning, module="pkg_resources")

IST = pytz.timezone("Asia/Kolkata")

# ANSI COLORS
RESET   = "\033[0m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
RED     = "\033[91m"
GRAY    = "\033[90m"
CYAN    = "\033[96m"


# ─────────────────────────────────────────────────────────────────────────────
#  PRE-MARKET WARMUP
# ─────────────────────────────────────────────────────────────────────────────
def do_warmup() -> MarketData:
    """
    Called once before market opens (9:00–9:14 IST).

    1. Creates MarketData with Fyers client.
    2. Fetches historical candles from Fyers API (not SQLite).
    3. Builds indicators on full history — Supertrend/ADX/CCI fully warmed.
    4. Wires market_data into data_feed so ticks flow correctly.

    Returns the MarketData singleton used by the strategy loop.
    """
    logging.info(f"{GREEN}[WARMUP] Starting pre-market warmup...{RESET}")

    md = MarketData(fyers_client=fyers, mode="LIVE")
    md.warmup(symbols)

    # Wire into data_feed so on_tick() calls route here
    data_feed.market_data = md

    # Print daily pivot levels using prev-day OHLC from warmup
    for sym in symbols:
        prev = md.get_prev_day_ohlc(sym)
        if prev:
            ph, pl, pc = prev["high"], prev["low"], prev["close"]
            cpr  = calculate_cpr(ph, pl, pc)
            trad = calculate_traditional_pivots(ph, pl, pc)
            cam  = calculate_camarilla_pivots(ph, pl, pc)
            logging.info(
                f"{GREEN}[{sym}] prev_day={prev['date']} "
                f"H={ph} L={pl} C={pc} | "
                f"CPR: P={cpr['pivot']} TC={cpr['tc']} BC={cpr['bc']} | "
                f"Trad: R1={trad['r1']} S1={trad['s1']} | "
                f"Cam: R3={cam['r3']} S3={cam['s3']}{RESET}"
            )
        else:
            logging.warning(f"[WARMUP] No prev-day OHLC for {sym}")

    logging.info(f"{GREEN}[WARMUP] Complete. Bot is ready for market open.{RESET}")
    return md


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN STRATEGY LOOP
# ─────────────────────────────────────────────────────────────────────────────
async def main_strategy_code(md: MarketData) -> None:
    """
    Async strategy loop. Runs every second.

    Candle rebuild: only when new completed 3m candle available (every ~3 min).
    Exit checks: every second — position_manager handles this on every call.
    Entry signals: de-duplicated per candle in paper_order/live_order.
    """
    today    = dt.now(time_zone).date()
    end_time = dt.datetime(today.year, today.month, today.day, 15, 30, tz=time_zone)

    # Track candle count per symbol to detect new completed candles
    last_candle_count = {sym: 0 for sym in symbols}

    logging.info(f"{GREEN}[MAIN] Strategy loop started. End={end_time}{RESET}")

    while True:
        ct = dt.now(time_zone)

        if ct > end_time.add(minutes=2):
            logging.info("[MAIN] Session ended.")
            return

        # ── Order management (every 5 seconds) ─────────────────────────────
        if ct.second % 5 == 0:
            try:
                ob     = await fyers_async.orderbook()
                ord_df = pd.DataFrame(ob.get("orderBook", []))
                chase_order(ord_df)

                pos = await fyers_async.positions()
                pnl = int(pos.get("overall", {}).get("pl_total", 0))
                logging.debug(f"{GRAY}[PnL] {pnl}{RESET}")
            except Exception as e:
                logging.debug(f"[ORDER MGT] {e}")

        if MODE != "STRATEGY":
            await asyncio.sleep(1)
            continue

        # ── Strategy ─────────────────────────────────────────────────────────
        for sym in symbols:
            try:
                df_3m, df_15m = md.get_candles(sym)
                spot          = md.get_spot(sym)

                if df_3m is None or df_3m.empty:
                    logging.debug(f"[MAIN] No candles yet for {sym}")
                    await asyncio.sleep(1)
                    continue

                # Log when a new candle arrives
                new_count = len(df_3m)
                if new_count != last_candle_count[sym]:
                    last_candle_count[sym] = new_count
                    last = df_3m.iloc[-1]
                    logging.info(
                        f"{CYAN}[NEW CANDLE] {sym} bar={new_count} "
                        f"t={last.get('time','')} "
                        f"c={last['close']:.2f} "
                        f"bias={last.get('supertrend_bias','?')} "
                        f"rsi={last.get('rsi14', float('nan')):.1f} "
                        f"adx={last.get('adx14', float('nan')):.1f}{RESET}"
                    )

                # ── Exit + entry ─────────────────────────────────────────────
                if account_type.upper() == "PAPER":
                    paper_order(df_3m, hist_yesterday_15m=df_15m, mode="LIVE",
                                spot_price=spot)
                else:
                    live_order(df_3m, hist_yesterday_15m=df_15m,
                               spot_price=spot)

            except Exception as e:
                logging.error(f"[STRATEGY ERROR] {sym}: {e}", exc_info=True)

        await asyncio.sleep(1)


# ─────────────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────
def run():
    # Step 1: Pre-market warmup from Fyers API
    md = do_warmup()

    # Step 2: Start websocket connections
    fyers_socket.connect()
    fyers_order_socket.connect()
    time.sleep(2)   # allow sockets to establish

    logging.info(f"{GREEN}[MAIN] WebSocket connected. Starting strategy loop.{RESET}")

    # Step 3: Run async strategy loop
    try:
        asyncio.run(main_strategy_code(md))
    except KeyboardInterrupt:
        logging.info("[MAIN] Interrupted by user.")
    finally:
        logging.info("[MAIN] Shutdown complete.")


if __name__ == "__main__":
    run()