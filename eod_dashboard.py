#!/usr/bin/env python3
import os
import sys
import datetime
import argparse
from dashboard import generate_full_report

def main():
    parser = argparse.ArgumentParser(description="Generate EOD dashboard report")
    parser.add_argument(
        "--date",
        type=str,
        help="Date in YYYY-MM-DD format (default: today)",
        default=datetime.date.today().strftime("%Y-%m-%d")
    )
    parser.add_argument(
        "--logdir",   # <-- this must match what you use later
        type=str,
        help="Directory containing log files (default: current directory)",
        default=None
    )
    args = parser.parse_args()

    # Use current directory if --logdir not provided
    log_dir = args.logdir if args.logdir else os.getcwd()

    # Build log file path (your naming convention: options_trade_engine_YYYY-MM-DD.log)
    log_file = os.path.join(log_dir, f"options_trade_engine_{args.date}.log")

    if not os.path.exists(log_file):
        print(f"[ERROR] No log file found for {args.date}: {log_file}")
        sys.exit(1)

    # Generate dashboard report (parses log internally)
    print(f"[INFO] Parsing log file: {log_file}")
    print(f"[INFO] Generating dashboard report for {args.date}")
    result = generate_full_report(log_file, output_dir=log_dir)

    # Print text report to console
    text_path = result.get("text")
    if text_path and os.path.exists(text_path):
        with open(text_path) as f:
            print("\n=== DASHBOARD REPORT ===\n")
            print(f.read())
        print(f"[INFO] Dashboard report saved to {text_path}")
    else:
        print("[WARN] Text report not generated.")

if __name__ == "__main__":
    main()