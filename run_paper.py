#!/usr/bin/env python3
"""
Paper Trading Runner — OptionsBuyingStrategy (tbot_core)
========================================================

Runs the options buying strategy in PAPER mode with live WebSocket
tick feed but simulated order fills (no real broker orders).

Architecture:
  1. Warmup  — Fetch Fyers historical candles, build indicators
  2. Pivots  — Print CPR / Traditional / Camarilla levels
  3. Connect — WebSocket sockets for live tick streaming
  4. Loop    — Async strategy loop: detect candles → entry/exit

Usage:
    python run_paper.py
    python run_paper.py --quantity 4 --max-trades 6
    python run_paper.py --adx-min 20.0 --rr-ratio 2.5
"""

import asyncio
import logging
import time
import argparse
from datetime import datetime

import pandas as pd
import pendulum as dt

from config import time_zone, symbols, strategy_name
from setup import fyers, fyers_async
from market_data import MarketData
import data_feed
from data_feed import fyers_socket, fyers_order_socket, chase_order, tick_db

from options_buying_strategy import OptionsBuyingStrategy, StrategyConfig
from tbot_core.indicators import (
    calculate_cpr,
    calculate_traditional_pivots,
    calculate_camarilla_pivots,
)

# ── ANSI Colours ─────────────────────────────────────────────────────
RESET  = "\033[0m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
GRAY   = "\033[90m"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ── Startup state ────────────────────────────────────────────────────
warmup_end_times: dict = {}


# ─────────────────────────────────────────────────────────────────────
#  Daily Pivot Levels
# ─────────────────────────────────────────────────────────────────────

def print_daily_levels(md: MarketData) -> None:
    """Print CPR, Traditional, Camarilla pivots from previous day OHLC."""
    logger.info(f"{CYAN}{'─' * 70}{RESET}")
    logger.info(f"{CYAN}  PAPER TRADING — {strategy_name}  |  tbot_core Strategy{RESET}")
    logger.info(f"{CYAN}{'─' * 70}{RESET}")

    for sym in symbols:
        prev_ohlc = md.get_prev_day_ohlc(sym)
        if not prev_ohlc:
            logger.warning(f"[PIVOT CHECK] {sym} No previous day data")
            continue

        ph = float(prev_ohlc.get("high", 0))
        pl = float(prev_ohlc.get("low", 0))
        pc = float(prev_ohlc.get("close", 0))

        if ph <= 0 or pl <= 0 or pc <= 0 or (ph == pl == pc):
            logger.warning(f"[PIVOT CHECK] {sym} Invalid OHLC: H={ph} L={pl} C={pc}")
            continue
        if ph < pl:
            ph, pl = pl, ph

        cpr  = calculate_cpr(ph, pl, pc)
        trad = calculate_traditional_pivots(ph, pl, pc)
        cam  = calculate_camarilla_pivots(ph, pl, pc)

        logger.info(
            f"{GREEN}[LEVELS] {sym} H={ph:.2f} L={pl:.2f} C={pc:.2f} | "
            f"CPR: P={cpr['pivot']:.2f} TC={cpr['tc']:.2f} BC={cpr['bc']:.2f} | "
            f"Cam: R3={cam['r3']:.2f} S3={cam['s3']:.2f} "
            f"R4={cam['r4']:.2f} S4={cam['s4']:.2f}{RESET}"
        )


# ─────────────────────────────────────────────────────────────────────
#  Pre-Market Warmup
# ─────────────────────────────────────────────────────────────────────

def do_warmup() -> MarketData:
    """Fetch historical candles, build indicators, wire data_feed."""
    global warmup_end_times

    logger.info(f"{GREEN}[WARMUP] Starting pre-market warmup for {symbols}...{RESET}")

    md = MarketData(fyers_client=fyers, mode="LIVE")
    md.warmup(symbols)

    # Wire into data_feed so websocket ticks flow into CandleAggregator
    data_feed.market_data = md

    for sym in symbols:
        df_3m, df_15m = md.get_candles(sym)
        n3 = len(df_3m) if df_3m is not None else 0
        n15 = len(df_15m) if df_15m is not None else 0
        logger.info(f"{GREEN}[WARMUP] {sym} 3m={n3} bars  15m={n15} bars{RESET}")

        if df_3m is not None and not df_3m.empty:
            warmup_end_times[sym] = df_3m.iloc[-1].get("time") or df_3m.iloc[-1].get("date", "?")

    logger.info(f"{GREEN}[WARMUP] Complete. Bot ready for market open.{RESET}")
    return md


# ─────────────────────────────────────────────────────────────────────
#  Async Strategy Loop
# ─────────────────────────────────────────────────────────────────────

async def paper_strategy_loop(md: MarketData, strategy: OptionsBuyingStrategy) -> None:
    """Async loop — runs every second, calls strategy.run_paper() on each candle."""

    today = dt.now(time_zone).date()
    end_time = dt.datetime(today.year, today.month, today.day, 15, 30, tz=time_zone)

    last_candle_count: dict = {sym: 0 for sym in symbols}
    first_live_candle_seen: dict = {sym: False for sym in symbols}

    logger.info(
        f"{GREEN}[MAIN] Paper strategy loop started. end_time={end_time}{RESET}"
    )

    while True:
        ct = dt.now(time_zone)

        # ── Session end ──────────────────────────────────────────────
        if ct > end_time.add(minutes=2):
            logger.info(f"{YELLOW}[MAIN] Session ended at {ct}. Shutting down.{RESET}")
            # Save final trade CSV
            strategy.save_trades_csv(suffix="PAPER")
            summary = strategy.get_trade_summary()
            logger.info(
                f"{GREEN}[SESSION SUMMARY] "
                f"Trades={summary['total_trades']} "
                f"PnL={summary['total_pnl']:.2f} "
                f"WinRate={summary['win_rate']:.1f}%{RESET}"
            )
            return

        # ── Order management (every 5 seconds) ──────────────────────
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
                pnl = int(pos1.get("overall", {}).get("pl_total", 0))
                logger.debug(f"{GRAY}[PnL] broker_pnl={pnl}{RESET}")
            except Exception as exc:
                logger.debug(f"[ORDERBOOK ERROR] {exc}")

        # ── Pulse logging (every 30s) ────────────────────────────────
        if ct.second % 30 == 0:
            try:
                from data_feed import pulse
                pulse.log_stats()
            except Exception:
                pass

        # ── Strategy per symbol ──────────────────────────────────────
        for sym in symbols:
            try:
                spot = md.get_spot(sym)
                if spot and spot > 0:
                    data_feed.spot_price = spot

                df_3m, df_15m = md.get_candles(sym)
                n3 = len(df_3m) if df_3m is not None and not df_3m.empty else 0
                is_new_candle = n3 > last_candle_count[sym]

                if is_new_candle:
                    last_bar = df_3m.iloc[-1] if n3 > 0 else None
                    bar_time = str(last_bar.get("time", "?")) if last_bar is not None else "?"

                    logger.info(
                        f"{CYAN}[NEW CANDLE] {sym} bar={bar_time} n3m={n3} spot={spot:.2f}{RESET}"
                    )
                    last_candle_count[sym] = n3

                    # Startup guard: skip warmup candles
                    if not first_live_candle_seen[sym]:
                        if bar_time == str(warmup_end_times.get(sym)):
                            logger.info(f"{YELLOW}[STARTUP GUARD] {sym} Skipping warmup candle{RESET}")
                            await asyncio.sleep(1)
                            continue
                        else:
                            first_live_candle_seen[sym] = True
                            logger.info(f"{GREEN}[STARTUP GUARD ACTIVE] {sym} First live candle{RESET}")

                # ── Call strategy ────────────────────────────────────
                if df_3m is not None and not df_3m.empty:
                    result = strategy.run_paper(
                        candles_3m=df_3m,
                        candles_15m=df_15m,
                        spot_price=spot,
                        mode="LIVE",
                    )
                    if result:
                        if result.get("entry"):
                            logger.info(
                                f"{GREEN}[PAPER ENTRY] {result.get('side')} "
                                f"type={result.get('type')}{RESET}"
                            )
                        elif result.get("exit"):
                            logger.info(
                                f"{YELLOW}[PAPER EXIT] {result.get('side')} "
                                f"reason={result.get('reason')}{RESET}"
                            )

            except Exception as exc:
                logger.error(f"[STRATEGY ERROR] {sym}: {exc}", exc_info=True)

        await asyncio.sleep(1)


# ─────────────────────────────────────────────────────────────────────
#  Entry Point
# ─────────────────────────────────────────────────────────────────────

def run(args) -> None:
    """Full startup: warmup → pivots → sockets → strategy loop."""

    # Build strategy config from CLI args
    config = StrategyConfig(
        mode="PAPER",
        quantity=args.quantity,
        max_trades_per_day=args.max_trades,
        max_trades_trend=args.max_trades_trend,
        max_trades_scalp=args.max_trades_scalp,
        trend_entry_adx_min=args.adx_min,
        rr_ratio=args.rr_ratio,
        paper_slippage_pts=args.slippage,
    )
    strategy = OptionsBuyingStrategy(config=config)

    logger.info(
        f"{GREEN}[CONFIG] quantity={config.quantity} "
        f"max_trades={config.max_trades_per_day} "
        f"adx_min={config.trend_entry_adx_min} "
        f"rr_ratio={config.rr_ratio} "
        f"slippage={config.paper_slippage_pts}{RESET}"
    )

    # 1. Warmup
    md = do_warmup()

    # 2. Print daily levels
    print_daily_levels(md)

    # 3. Hydrate runtime state (cooldowns, restart recovery)
    strategy.paper_info = strategy._hydrate_runtime_state(
        strategy.paper_info, config.account_type, "PAPER"
    )

    # 4. Connect sockets
    fyers_socket.connect()
    fyers_order_socket.connect()
    time.sleep(2)

    logger.info(f"{GREEN}[RUN] Sockets connected. Starting PAPER strategy loop...{RESET}")

    # 5. Run async strategy loop
    try:
        asyncio.run(paper_strategy_loop(md, strategy))
    except KeyboardInterrupt:
        logger.info(f"{YELLOW}[MAIN] Interrupted by user.{RESET}")
        strategy.save_trades_csv(suffix="PAPER")
        summary = strategy.get_trade_summary()
        logger.info(
            f"{GREEN}[FINAL] Trades={summary['total_trades']} "
            f"PnL={summary['total_pnl']:.2f} "
            f"WinRate={summary['win_rate']:.1f}%{RESET}"
        )
    finally:
        logger.info("[MAIN] Paper trading terminated.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Paper Trading — OptionsBuyingStrategy")
    parser.add_argument("--quantity", type=int, default=130, help="Lot size (default: 130)")
    parser.add_argument("--max-trades", type=int, default=12, help="Max trades/day (default: 12)")
    parser.add_argument("--max-trades-trend", type=int, default=8, help="Max trend trades (default: 8)")
    parser.add_argument("--max-trades-scalp", type=int, default=12, help="Max scalp trades (default: 12)")
    parser.add_argument("--adx-min", type=float, default=18.0, help="Min ADX for trend entry (default: 18.0)")
    parser.add_argument("--rr-ratio", type=float, default=2.0, help="Risk:Reward ratio (default: 2.0)")
    parser.add_argument("--slippage", type=float, default=1.5, help="Paper slippage points (default: 1.5)")
    args = parser.parse_args()

    run(args)
