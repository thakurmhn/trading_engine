#!/usr/bin/env python3
"""
Utility to check survivability metrics from the latest dashboard report.
Usage: python check_survivability.py [YYYY-MM-DD]
"""

import os
import sys
import glob
import re
from dashboard import generate_full_report

def get_latest_log_file():
    files = glob.glob("options_trade_engine_*.log")
    if not files:
        return None
    return max(files, key=os.path.getmtime)

def main():
    target_date = sys.argv[1] if len(sys.argv) > 1 else None
    
    log_file = None
    if target_date:
        # Try to find a log file matching the target date
        # Note: config.py names logs by execution date, so this might not match
        # if replay was run on a different day. We check anyway.
        candidate = f"options_trade_engine_{target_date}.log"
        if os.path.exists(candidate):
            log_file = candidate
    
    if not log_file:
        log_file = get_latest_log_file()
        if log_file:
            print(f"Target log not found, using latest: {log_file}")
    
    if not log_file:
        print("No log file found.")
        return

    print(f"Generating report for {log_file}...")
    result = generate_full_report(log_file, output_dir="reports")
    text_path = result.get("text")
    
    if not text_path or not os.path.exists(text_path):
        print("Failed to generate report.")
        return
        
    print(f"Reading report: {text_path}")
    with open(text_path, "r", encoding="utf-8") as f:
        content = f.read()
        
    match = re.search(r"(SURVIVABILITY & LIQUIDITY.*?)(?=\n\n|\Z)", content, re.DOTALL)
    if match:
        print("\n" + "="*50)
        print(match.group(1))
        print("="*50 + "\n")
    else:
        print("\n[INFO] No SURVIVABILITY & LIQUIDITY section found (counts likely 0).")

if __name__ == "__main__":
    main()