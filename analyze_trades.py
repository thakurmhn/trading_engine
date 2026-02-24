#!/usr/bin/env python3
"""
Analyze exit rule distribution from latest trades CSV and recent logs.
"""
import os
import glob
import re
from pathlib import Path
from collections import defaultdict
import pandas as pd

# Find the latest trades CSV
trade_files = glob.glob("trades_*.csv")
if not trade_files:
    print("No trades CSV found. Run replay first.")
    exit(1)

latest_file = max(trade_files, key=lambda x: os.path.getmtime(x))
print(f"\n=== ANALYZING TRADES FROM: {latest_file} ===\n")

try:
    df = pd.read_csv(latest_file)
    print(f"Total trades: {len(df)}")
    print(f"\nColumns: {list(df.columns)}\n")
    
    if len(df) > 0:
        # Calculate win/loss
        if 'pnl_points' in df.columns:
            winners = (df['pnl_points'] > 0).sum()
            losers = (df['pnl_points'] < 0).sum()
            breakeven = (df['pnl_points'] == 0).sum()
            total_points = df['pnl_points'].sum()
            
            print(f"Winners: {winners}")
            print(f"Losers: {losers}")
            print(f"Breakeven: {breakeven}")
            print(f"Win %: {winners/(len(df))*100:.1f}%" if len(df) > 0 else "N/A")
            print(f"Total P&L (pts): {total_points:.2f}")
            
        if 'pnl_rupees' in df.columns:
            total_rs = df['pnl_rupees'].sum()
            max_win = df['pnl_rupees'].max()
            max_loss = df['pnl_rupees'].min()
            print(f"Total P&L (Rs): {total_rs:.2f}")
            print(f"Max win: ₹{max_win:.2f}")
            print(f"Max loss: ₹{max_loss:.2f}")
        
        print("\n=== FIRST 10 TRADES ===\n")
        print(df[['entry_premium', 'exit_premium', 'pnl_points', 'pnl_rupees', 'bars_held']].head(10).to_string())
        
    else:
        print("No trades in CSV")
        
except Exception as e:
    print(f"Error reading CSV: {e}")

# Try to find exit rules in recent output logs
print("\n\n=== SEARCHING FOR [EXIT DECISION] LOGS ===\n")
exit_rule_counts = defaultdict(int)

# Check for log output files that might have been created
log_patterns = ["*.log", "*output*"]
for pattern in log_patterns:
    for log_file in glob.glob(pattern):
        try:
            with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
                # Find all [EXIT DECISION] lines
                matches = re.findall(r'\[EXIT DECISION\].*rule=(\w+)', content)
                for match in matches:
                    exit_rule_counts[match] += 1
        except:
            pass

if exit_rule_counts:
    print("Exit rule distribution:")
    for rule, count in sorted(exit_rule_counts.items(), key=lambda x: x[1], reverse=True):
        print(f"  {rule}: {count}")
else:
    print("No exit decision logs found in files.")
    print("Re-run replay to generate logs with [EXIT DECISION] annotations.")

print("\n=== SUMMARY ===")
print("Exit logic v7 includes 4 rules:")
print("  1. LOSS_CUT (priority=1): Exit if loss > -10 pts within 5 bars")
print("  2. QUICK_PROFIT (priority=2): Exit 50% if UL +10 pts move")
print("  3. DRAWDOWN_EXIT (priority=3): Exit if peak_gain - current > 9 pts")
print("  4. BREAKOUT_HOLD (priority=4): Suppress exits on R4/S4 sustain")
