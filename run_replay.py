#!/usr/bin/env python3
"""
Replay Runner — OptionsBuyingStrategy (tbot_core)
==================================================

Runs the options buying strategy in OFFLINE replay mode using
SQLite tick database. No live connection needed — fully offline
candle-by-candle simulation with all indicators, signals, entries,
and exits.

Features:
  - Auto-prepends previous trading days for indicator warmup
  - Regime-aware entries/exits (trend continuation, compression, reversal)
  - Signal-only mode for analysis without trade simulation
  - Multi-day replay with session reset between days
  - CSV output: signals + trades with full PnL breakdown

Usage:
    # Single day replay
    python run_replay.py --date 2026-03-10

    # Signal-only mode (no trades, just log signals)
    python run_replay.py --date 2026-03-10 --signal-only

    # Custom database path
    python run_replay.py --date 2026-03-10 --db "C:\\SQLite\\ticks\\ticks_2026-03-10.db"

    # Multi-day replay
    python run_replay.py --from 2026-03-03 --to 2026-03-10

    # Custom output directory
    python run_replay.py --date 2026-03-10 --output-dir ./results

    # Custom lot size
    python run_replay.py --date 2026-03-10 --quantity 4
"""

import sys
import os
import logging
import argparse
from datetime import datetime, timedelta

from options_buying_strategy import OptionsBuyingStrategy, StrategyConfig

# ── ANSI Colours ─────────────────────────────────────────────────────
RESET  = "\033[0m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
#  Single-Day Replay
# ─────────────────────────────────────────────────────────────────────

def run_single_day(
    date_str: str,
    db_path: str,
    signal_only: bool = False,
    output_dir: str = ".",
    quantity: int = 130,
    warmup_candles: int = 35,
) -> dict:
    """Run replay for a single date and return results."""

    logger.info(f"\n{GREEN}{'=' * 70}{RESET}")
    logger.info(f"{GREEN}  REPLAY — OptionsBuyingStrategy (tbot_core){RESET}")
    logger.info(f"{GREEN}  Date: {date_str}{RESET}")
    logger.info(f"{GREEN}  DB:   {db_path}{RESET}")
    logger.info(f"{GREEN}  Mode: {'SIGNALS ONLY' if signal_only else 'FULL SIMULATION'}{RESET}")
    logger.info(f"{GREEN}  Qty:  {quantity}{RESET}")
    logger.info(f"{GREEN}{'=' * 70}{RESET}\n")

    # Validate database exists
    if not os.path.exists(db_path):
        logger.error(f"{RED}[ERROR] Database not found: {db_path}{RESET}")
        return {"error": f"Database not found: {db_path}"}

    # Create output directory if needed
    os.makedirs(output_dir, exist_ok=True)

    # Build strategy
    config = StrategyConfig(
        mode="REPLAY",
        quantity=quantity,
    )
    strategy = OptionsBuyingStrategy(config=config)

    # Run replay (delegates to execution.run_offline_replay)
    try:
        result = strategy.run(
            mode="REPLAY",
            db_path=db_path,
            date_str=date_str,
            signal_only=signal_only,
            min_warmup_candles=warmup_candles,
            output_dir=output_dir,
        )
    except Exception as e:
        logger.error(f"{RED}[REPLAY FAILED] {date_str}: {e}{RESET}")
        import traceback
        traceback.print_exc()
        return {"error": str(e), "date": date_str}

    # Print results
    logger.info(f"\n{GREEN}{'─' * 70}{RESET}")
    logger.info(f"{GREEN}  Replay Complete: {date_str}{RESET}")

    if result:
        trades = result.get("trades", [])
        signals = result.get("signals", [])
        total_pnl = sum(t.get("pnl", 0) for t in trades) if trades else 0
        winners = sum(1 for t in trades if t.get("pnl", 0) > 0)
        losers = sum(1 for t in trades if t.get("pnl", 0) < 0)
        win_rate = (winners / len(trades) * 100) if trades else 0

        logger.info(f"{GREEN}  Signals: {len(signals) if signals else 'N/A'}{RESET}")
        logger.info(f"{GREEN}  Trades:  {len(trades) if trades else 0}{RESET}")
        logger.info(f"{GREEN}  PnL:     {total_pnl:.2f}{RESET}")
        logger.info(f"{GREEN}  Winners: {winners}  Losers: {losers}  WinRate: {win_rate:.1f}%{RESET}")
    else:
        logger.info(f"{YELLOW}  No result returned (check logs for details){RESET}")

    logger.info(f"{GREEN}{'─' * 70}{RESET}")

    output_files = [
        f"signals_NSE_NIFTY50-INDEX_{date_str}.csv",
        f"trades_NSE_NIFTY50-INDEX_{date_str}.csv",
    ]
    for f in output_files:
        fpath = os.path.join(output_dir, f)
        if os.path.exists(fpath):
            logger.info(f"  Output: {fpath}")

    return result or {}


# ─────────────────────────────────────────────────────────────────────
#  Multi-Day Replay
# ─────────────────────────────────────────────────────────────────────

def run_multi_day(
    from_date: str,
    to_date: str,
    db_dir: str,
    signal_only: bool = False,
    output_dir: str = ".",
    quantity: int = 130,
    warmup_candles: int = 35,
) -> list:
    """Run replay across multiple dates. Returns list of per-day results."""

    start = datetime.strptime(from_date, "%Y-%m-%d")
    end = datetime.strptime(to_date, "%Y-%m-%d")

    logger.info(f"\n{CYAN}{'=' * 70}{RESET}")
    logger.info(f"{CYAN}  MULTI-DAY REPLAY: {from_date} → {to_date}{RESET}")
    logger.info(f"{CYAN}{'=' * 70}{RESET}\n")

    all_results = []
    total_pnl = 0.0
    total_trades = 0
    total_winners = 0
    total_losers = 0
    days_run = 0

    current = start
    while current <= end:
        date_str = current.strftime("%Y-%m-%d")

        # Skip weekends (Saturday=5, Sunday=6)
        if current.weekday() >= 5:
            current += timedelta(days=1)
            continue

        # Look for database file
        db_path = os.path.join(db_dir, f"ticks_{date_str}.db")
        if not os.path.exists(db_path):
            logger.info(f"{YELLOW}[SKIP] {date_str} — no database found at {db_path}{RESET}")
            current += timedelta(days=1)
            continue

        result = run_single_day(
            date_str=date_str,
            db_path=db_path,
            signal_only=signal_only,
            output_dir=output_dir,
            quantity=quantity,
            warmup_candles=warmup_candles,
        )
        all_results.append({"date": date_str, "result": result})

        # Aggregate stats
        if result and not result.get("error"):
            trades = result.get("trades", [])
            day_pnl = sum(t.get("pnl", 0) for t in trades) if trades else 0
            day_winners = sum(1 for t in trades if t.get("pnl", 0) > 0)
            day_losers = sum(1 for t in trades if t.get("pnl", 0) < 0)

            total_pnl += day_pnl
            total_trades += len(trades) if trades else 0
            total_winners += day_winners
            total_losers += day_losers
            days_run += 1

        current += timedelta(days=1)

    # ── Aggregate Summary ────────────────────────────────────────────
    logger.info(f"\n{GREEN}{'=' * 70}{RESET}")
    logger.info(f"{GREEN}  MULTI-DAY SUMMARY: {from_date} → {to_date}{RESET}")
    logger.info(f"{GREEN}{'─' * 70}{RESET}")
    logger.info(f"{GREEN}  Days Run:    {days_run}{RESET}")
    logger.info(f"{GREEN}  Total Trades: {total_trades}{RESET}")
    logger.info(f"{GREEN}  Total PnL:    {total_pnl:.2f}{RESET}")
    logger.info(f"{GREEN}  Winners:      {total_winners}{RESET}")
    logger.info(f"{GREEN}  Losers:       {total_losers}{RESET}")
    if total_trades > 0:
        win_rate = total_winners / total_trades * 100
        avg_pnl = total_pnl / total_trades
        logger.info(f"{GREEN}  Win Rate:     {win_rate:.1f}%{RESET}")
        logger.info(f"{GREEN}  Avg PnL/Trade: {avg_pnl:.2f}{RESET}")
    if days_run > 0:
        logger.info(f"{GREEN}  Avg PnL/Day:  {total_pnl / days_run:.2f}{RESET}")
    logger.info(f"{GREEN}{'=' * 70}{RESET}")

    return all_results


# ─────────────────────────────────────────────────────────────────────
#  CLI Entry Point
# ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Replay Runner — OptionsBuyingStrategy (tbot_core)"
    )

    # Date selection (single or range)
    parser.add_argument("--date", type=str,
                        help="Single date to replay (YYYY-MM-DD)")
    parser.add_argument("--from", dest="from_date", type=str,
                        help="Multi-day start date (YYYY-MM-DD)")
    parser.add_argument("--to", dest="to_date", type=str,
                        help="Multi-day end date (YYYY-MM-DD)")

    # Database
    parser.add_argument("--db", type=str,
                        help="Path to ticks database (single-day) or directory (multi-day)")
    parser.add_argument("--db-dir", type=str, default=r"C:\SQLite\ticks",
                        help="Directory containing ticks_YYYY-MM-DD.db files (default: C:\\SQLite\\ticks)")

    # Mode
    parser.add_argument("--signal-only", action="store_true",
                        help="Signals only — skip trade simulation")

    # Output
    parser.add_argument("--output-dir", type=str, default=".",
                        help="Output directory for CSV files (default: current dir)")

    # Strategy params
    parser.add_argument("--quantity", type=int, default=130,
                        help="Lot size (default: 130)")
    parser.add_argument("--warmup", type=int, default=35,
                        help="Min warmup candles (default: 35)")

    args = parser.parse_args()

    # ── Validate arguments ───────────────────────────────────────────
    if not args.date and not args.from_date:
        parser.error("Either --date (single day) or --from/--to (multi-day) is required")

    if args.from_date and not args.to_date:
        args.to_date = args.from_date  # Single day via range syntax

    # ── Route to single-day or multi-day ─────────────────────────────
    try:
        if args.date:
            # Single day
            db_path = args.db or os.path.join(args.db_dir, f"ticks_{args.date}.db")
            run_single_day(
                date_str=args.date,
                db_path=db_path,
                signal_only=args.signal_only,
                output_dir=args.output_dir,
                quantity=args.quantity,
                warmup_candles=args.warmup,
            )
        else:
            # Multi-day
            db_dir = args.db or args.db_dir
            run_multi_day(
                from_date=args.from_date,
                to_date=args.to_date,
                db_dir=db_dir,
                signal_only=args.signal_only,
                output_dir=args.output_dir,
                quantity=args.quantity,
                warmup_candles=args.warmup,
            )

    except Exception as e:
        logger.error(f"\n{RED}Replay failed: {e}{RESET}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
