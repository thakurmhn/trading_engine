#!/usr/bin/env python3
"""
Run offline REPLAY with new exit logic v7 (4 simple rules):
1. LOSS_CUT (exit if loss < -10 pts within 5 bars)
2. QUICK_PROFIT (exit if UL move >= 10 pts, book 50%)
3. DRAWDOWN_EXIT (exit if peak - cur >= 9 pts)
4. BREAKOUT_HOLD (hold longer on R4/S4 sustain)

Usage:
    python run_replay_v7.py --date 2026-02-20 --signal-only
    python run_replay_v7.py --date 2026-02-20  (full trade sim)
"""

import sys
import logging
import argparse
from tickdb import tick_db
from execution import run_strategy

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

GREEN = "\033[92m"
YELLOW = "\033[93m"
RESET = "\033[0m"

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Offline replay with exit logic v7 (4 simple rules)"
    )
    parser.add_argument("--date", type=str, default="2026-02-20",
                        help="Date to replay (YYYY-MM-DD)")
    parser.add_argument("--signal-only", action="store_true",
                        help="Signals only (skip trade simulation)")
    parser.add_argument("--db", type=str, default="C:\\SQLite\\ticks\\ticks_2026-02-20.db",
                        help="Path to ticks database")
    args = parser.parse_args()

    print(f"\n{GREEN}='*80{RESET}")
    print(f"{GREEN}  REPLAY WITH EXIT LOGIC V7 (4 SIMPLE RULES){RESET}")
    print(f"{GREEN}  Date: {args.date}{RESET}")
    print(f"{GREEN}  Mode: {'SIGNALS ONLY' if args.signal_only else 'FULL SIMULATION'}{RESET}")
    print(f"{GREEN}='*80{RESET}\n")

    try:
        run_strategy(
            symbols=["NSE:NIFTY50-INDEX"],
            mode="OFFLINE",
            tick_db=tick_db,
            date_str=args.date,
            signal_only=args.signal_only,
            min_warmup_candles=35,
            output_dir=".",
            db_path=args.db,
        )
        
        print(f"\n{YELLOW}Replay completed successfully!{RESET}")
        print(f"Check CSV files for results:")
        print(f"  - signals_NSE_NIFTY50-INDEX_{args.date}.csv")
        print(f"  - trades_NSE_NIFTY50-INDEX_{args.date}.csv")
        
    except Exception as e:
        print(f"\n{RED}Replay failed: {e}{RESET}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
