#!/usr/bin/env python3
"""
Live Trading Runner — OptionsBuyingStrategy (tbot_core)
=======================================================

Runs the options buying strategy in LIVE mode with real broker
orders via Fyers API + WebSocket tick feed.

Architecture:
  1. Warmup  — Fetch Fyers historical candles, build indicators
  2. Pivots  — Print CPR / Traditional / Camarilla levels
  3. Connect — WebSocket sockets for live tick streaming
  4. Loop    — Async strategy loop: detect candles → real entries/exits

Safety Features:
  - Requires explicit --confirm flag to start (prevents accidental runs)
  - Max daily loss + max drawdown halt (risk_info)
  - Max trades per day cap (trend + scalp separate)
  - Session end auto-exit (15:15 IST)
  - Restart state persistence (pickle-based, recovers open positions)
  - Full audit trail via log tags

Usage:
    # Dry run (shows config, does NOT trade)
    python run_live.py

    # Start live trading (requires --confirm)
    python run_live.py --confirm

    # Custom lot size
    python run_live.py --confirm --quantity 130

    # Conservative settings
    python run_live.py --confirm --max-trades 4 --adx-min 22.0
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

warmup_end_times: dict = {}


# ─────────────────────────────────────────────────────────────────────
#  Daily Pivot Levels
# ─────────────────────────────────────────────────────────────────────

def print_daily_levels(md: MarketData) -> None:
    """Print CPR, Traditional, Camarilla pivots from previous day OHLC."""
    logger.info(f"{CYAN}{'─' * 70}{RESET}")
    logger.info(f"{RED}  *** LIVE TRADING *** — {strategy_name}  |  tbot_core Strategy{RESET}")
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
#  Async Strategy Loop — LIVE
# ─────────────────────────────────────────────────────────────────────

async def live_strategy_loop(md: MarketData, strategy: OptionsBuyingStrategy) -> None:
    """Async loop — runs every second, calls strategy.run_live() on each candle."""

    today = dt.now(time_zone).date()
    end_time = dt.datetime(today.year, today.month, today.day, 15, 30, tz=time_zone)

    last_candle_count: dict = {sym: 0 for sym in symbols}
    first_live_candle_seen: dict = {sym: False for sym in symbols}

    logger.info(
        f"{RED}[MAIN] *** LIVE *** strategy loop started. end_time={end_time}{RESET}"
    )

    while True:
        ct = dt.now(time_zone)

        # ── Session end ──────────────────────────────────────────────
        if ct > end_time.add(minutes=2):
            logger.info(f"{YELLOW}[MAIN] Session ended at {ct}. Shutting down.{RESET}")
            # Final summary
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
                logger.info(f"{GRAY}[BROKER PnL] {pnl}{RESET}")
            except Exception as exc:
                logger.debug(f"[ORDERBOOK ERROR] {exc}")

        # ── Pulse logging (every 30s) ────────────────────────────────
        if ct.second % 30 == 0:
            try:
                from data_feed import pulse
                pulse.log_stats()
            except Exception:
                pass

        # ── Risk halt check ──────────────────────────────────────────
        if strategy.risk_info.get("halt_trading", False):
            if ct.second == 0:
                logger.warning(f"{RED}[RISK HALT] Trading halted — daily loss/drawdown limit{RESET}")
            await asyncio.sleep(1)
            continue

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

                    # Log key indicators
                    _rsi = _safe(last_bar, "rsi14")
                    _cci = _safe(last_bar, "cci20")
                    _adx = _safe(last_bar, "adx14")
                    _st  = _safe(last_bar, "supertrend_bias")

                    logger.info(
                        f"{CYAN}[NEW CANDLE] {sym} bar={bar_time} n3m={n3} "
                        f"spot={spot:.2f} RSI={_fmt(_rsi)} CCI={_fmt(_cci)} "
                        f"ADX={_fmt(_adx)} ST={_st}{RESET}"
                    )
                    last_candle_count[sym] = n3

                    # Startup guard
                    if not first_live_candle_seen[sym]:
                        if bar_time == str(warmup_end_times.get(sym)):
                            logger.info(f"{YELLOW}[STARTUP GUARD] {sym} Skipping warmup candle{RESET}")
                            await asyncio.sleep(1)
                            continue
                        else:
                            first_live_candle_seen[sym] = True
                            logger.info(f"{GREEN}[STARTUP GUARD ACTIVE] {sym} First live candle{RESET}")

                # ── Call strategy (LIVE mode) ────────────────────────
                if df_3m is not None and not df_3m.empty:
                    result = strategy.run_live(
                        candles_3m=df_3m,
                        candles_15m=df_15m,
                    )
                    if result:
                        if result.get("entry"):
                            logger.info(
                                f"{GREEN}[*** LIVE ENTRY ***] {result.get('side')} "
                                f"type={result.get('type')}{RESET}"
                            )
                        elif result.get("exit"):
                            logger.info(
                                f"{YELLOW}[*** LIVE EXIT ***] {result.get('side')} "
                                f"reason={result.get('reason')}{RESET}"
                            )

            except Exception as exc:
                logger.error(f"[STRATEGY ERROR] {sym}: {exc}", exc_info=True)

        await asyncio.sleep(1)


def _safe(bar, key):
    """Safely extract a value from a pandas Series."""
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


# ─────────────────────────────────────────────────────────────────────
#  Entry Point
# ─────────────────────────────────────────────────────────────────────

def run(args) -> None:
    """Full startup: warmup → pivots → sockets → live strategy loop."""

    config = StrategyConfig(
        mode="LIVE",
        quantity=args.quantity,
        max_trades_per_day=args.max_trades,
        max_trades_trend=args.max_trades_trend,
        max_trades_scalp=args.max_trades_scalp,
        trend_entry_adx_min=args.adx_min,
        rr_ratio=args.rr_ratio,
        account_type="LIVE",
    )

    # ── Safety confirmation ──────────────────────────────────────────
    if not args.confirm:
        logger.info(f"\n{RED}{'=' * 70}{RESET}")
        logger.info(f"{RED}  *** LIVE TRADING MODE ***{RESET}")
        logger.info(f"{RED}  This will place REAL orders with REAL money.{RESET}")
        logger.info(f"{RED}{'─' * 70}{RESET}")
        logger.info(f"{YELLOW}  Config:{RESET}")
        logger.info(f"{YELLOW}    Quantity:    {config.quantity}{RESET}")
        logger.info(f"{YELLOW}    Max Trades:  {config.max_trades_per_day} (trend={config.max_trades_trend}, scalp={config.max_trades_scalp}){RESET}")
        logger.info(f"{YELLOW}    ADX Min:     {config.trend_entry_adx_min}{RESET}")
        logger.info(f"{YELLOW}    R:R Ratio:   {config.rr_ratio}{RESET}")
        logger.info(f"{YELLOW}    Max Loss:    {config.max_daily_loss}{RESET}")
        logger.info(f"{YELLOW}    Max DD:      {config.max_drawdown}{RESET}")
        logger.info(f"{RED}{'─' * 70}{RESET}")
        logger.info(f"{RED}  To start live trading, run with --confirm flag:{RESET}")
        logger.info(f"{RED}    python run_live.py --confirm{RESET}")
        logger.info(f"{RED}{'=' * 70}{RESET}\n")
        return

    strategy = OptionsBuyingStrategy(config=config)

    logger.info(f"\n{RED}{'=' * 70}{RESET}")
    logger.info(f"{RED}  *** STARTING LIVE TRADING ***{RESET}")
    logger.info(f"{RED}  Quantity={config.quantity} MaxTrades={config.max_trades_per_day}{RESET}")
    logger.info(f"{RED}{'=' * 70}{RESET}\n")

    # 1. Warmup
    md = do_warmup()

    # 2. Print daily levels
    print_daily_levels(md)

    # 3. Hydrate runtime state (recover open positions from restart)
    strategy.live_info = strategy._hydrate_runtime_state(
        strategy.live_info, "LIVE", "LIVE"
    )

    # 4. Connect sockets
    fyers_socket.connect()
    fyers_order_socket.connect()
    time.sleep(2)

    logger.info(f"{RED}[RUN] Sockets connected. Starting LIVE strategy loop...{RESET}")

    # 5. Run async strategy loop
    try:
        asyncio.run(live_strategy_loop(md, strategy))
    except KeyboardInterrupt:
        logger.info(f"{YELLOW}[MAIN] Interrupted by user.{RESET}")

        # Force-exit any open positions on interrupt
        info = strategy.live_info
        for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
            st = info.get(leg, {})
            if st.get("trade_flag", 0) == 1:
                name = st.get("option_name", "")
                qty = st.get("quantity", 0)
                logger.warning(
                    f"{RED}[EMERGENCY EXIT] {side} {name} qty={qty} — user interrupt{RESET}"
                )
                strategy._send_live_exit_order(name, qty, "USER_INTERRUPT")
                strategy.cleanup_trade_exit(info, leg, side, name, qty, None, "LIVE", "USER_INTERRUPT")

        summary = strategy.get_trade_summary()
        logger.info(
            f"{GREEN}[FINAL] Trades={summary['total_trades']} "
            f"PnL={summary['total_pnl']:.2f} "
            f"WinRate={summary['win_rate']:.1f}%{RESET}"
        )
    finally:
        logger.info("[MAIN] Live trading terminated.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Live Trading — OptionsBuyingStrategy")
    parser.add_argument("--confirm", action="store_true",
                        help="Required flag to actually start live trading")
    parser.add_argument("--quantity", type=int, default=130,
                        help="Lot size (default: 130)")
    parser.add_argument("--max-trades", type=int, default=12,
                        help="Max trades/day (default: 12)")
    parser.add_argument("--max-trades-trend", type=int, default=8,
                        help="Max trend trades (default: 8)")
    parser.add_argument("--max-trades-scalp", type=int, default=12,
                        help="Max scalp trades (default: 12)")
    parser.add_argument("--adx-min", type=float, default=18.0,
                        help="Min ADX for trend entry (default: 18.0)")
    parser.add_argument("--rr-ratio", type=float, default=2.0,
                        help="Risk:Reward ratio (default: 2.0)")
    args = parser.parse_args()

    run(args)
