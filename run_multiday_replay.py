#!/usr/bin/env python3
"""
Multi-day replay validation script.
Runs replay for the last 5 trading days and aggregates metrics.
"""

import subprocess
import sys
import os
import json
from datetime import datetime, timedelta
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Trading days to replay (most recent first)
REPLAY_DATES = [
    "2026-03-11",
    "2026-03-10",
    "2026-03-09",
    "2026-03-06",
    "2026-03-05",
]

def run_replay(date_str):
    """Run replay for a single date."""
    logging.info(f"\n{'='*70}")
    logging.info(f"REPLAY: {date_str}")
    logging.info(f"{'='*70}")
    
    cmd = [
        sys.executable,
        "main.py",
        "--mode", "REPLAY",
        "--date", date_str
    ]
    
    try:
        result = subprocess.run(
            cmd,
            cwd=os.getcwd(),
            capture_output=False,
            timeout=300
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        logging.error(f"Replay timeout for {date_str}")
        return False
    except Exception as e:
        logging.error(f"Replay failed for {date_str}: {e}")
        return False


def extract_metrics(log_file):
    """Extract key metrics from log file."""
    metrics = {
        "date": None,
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0,
        "pnl_pts": 0,
        "pnl_rs": 0,
        "profit_factor": 0,
        "payoff_ratio": 0,
    }
    
    if not os.path.exists(log_file):
        return metrics
    
    try:
        with open(log_file, 'r') as f:
            content = f.read()
            
            # Extract date
            if "RESULTS:" in content:
                for line in content.split('\n'):
                    if "RESULTS:" in line:
                        parts = line.split()
                        for i, p in enumerate(parts):
                            if p == "RESULTS:" and i+1 < len(parts):
                                metrics["date"] = parts[i+1]
                                break
            
            # Extract trade counts
            if "Trades taken" in content:
                for line in content.split('\n'):
                    if "Trades taken" in line:
                        try:
                            trades = int(line.split(':')[1].split()[0])
                            metrics["trades"] = trades
                        except:
                            pass
            
            # Extract win/loss
            if "Win / Loss" in content:
                for line in content.split('\n'):
                    if "Win / Loss" in line:
                        try:
                            parts = line.split(':')[1].split('/')
                            wins = int(parts[0].strip())
                            losses = int(parts[1].strip().split()[0])
                            metrics["wins"] = wins
                            metrics["losses"] = losses
                            if (wins + losses) > 0:
                                metrics["win_rate"] = wins / (wins + losses) * 100
                        except:
                            pass
            
            # Extract PnL
            if "Total PnL" in content:
                for line in content.split('\n'):
                    if "Total PnL (Rs):" in line:
                        try:
                            pnl_str = line.split(':')[1].strip()
                            pnl_rs = float(pnl_str.replace('+', '').replace('Rs', '').strip())
                            metrics["pnl_rs"] = pnl_rs
                        except:
                            pass
            
            # Extract profit factor from dashboard
            if "Profit Factor" in content:
                for line in content.split('\n'):
                    if "Profit Factor" in line and ":" in line:
                        try:
                            pf = float(line.split(':')[1].strip())
                            metrics["profit_factor"] = pf
                        except:
                            pass
            
            # Extract payoff ratio
            if "Payoff Ratio" in content:
                for line in content.split('\n'):
                    if "Payoff Ratio" in line and ":" in line:
                        try:
                            pr = float(line.split(':')[1].strip())
                            metrics["payoff_ratio"] = pr
                        except:
                            pass
    
    except Exception as e:
        logging.error(f"Error extracting metrics from {log_file}: {e}")
    
    return metrics


def main():
    """Run multi-day replay and aggregate results."""
    logging.info(f"Starting multi-day replay validation for {len(REPLAY_DATES)} days")
    
    results = []
    successful = 0
    failed = 0
    
    for date_str in REPLAY_DATES:
        success = run_replay(date_str)
        
        if success:
            successful += 1
            log_file = f"options_trade_engine_{date_str}.log"
            metrics = extract_metrics(log_file)
            results.append(metrics)
            
            logging.info(f"✓ {date_str}: {metrics['trades']} trades, "
                        f"W/L={metrics['wins']}/{metrics['losses']}, "
                        f"WR={metrics['win_rate']:.1f}%, "
                        f"PnL={metrics['pnl_rs']:.0f}Rs, "
                        f"PF={metrics['profit_factor']:.2f}")
        else:
            failed += 1
            logging.error(f"✗ {date_str}: Replay failed")
    
    # Aggregate results
    logging.info(f"\n{'='*70}")
    logging.info(f"MULTI-DAY REPLAY SUMMARY")
    logging.info(f"{'='*70}")
    logging.info(f"Successful replays: {successful}/{len(REPLAY_DATES)}")
    logging.info(f"Failed replays: {failed}/{len(REPLAY_DATES)}")
    
    if results:
        total_trades = sum(r['trades'] for r in results)
        total_wins = sum(r['wins'] for r in results)
        total_losses = sum(r['losses'] for r in results)
        total_pnl = sum(r['pnl_rs'] for r in results)
        avg_pf = sum(r['profit_factor'] for r in results) / len(results) if results else 0
        avg_pr = sum(r['payoff_ratio'] for r in results) / len(results) if results else 0
        
        overall_wr = (total_wins / (total_wins + total_losses) * 100) if (total_wins + total_losses) > 0 else 0
        
        logging.info(f"\nAggregated Metrics:")
        logging.info(f"  Total Trades: {total_trades}")
        logging.info(f"  Total Wins/Losses: {total_wins}/{total_losses}")
        logging.info(f"  Overall Win Rate: {overall_wr:.1f}%")
        logging.info(f"  Total PnL: {total_pnl:.0f} Rs")
        logging.info(f"  Avg Profit Factor: {avg_pf:.2f}")
        logging.info(f"  Avg Payoff Ratio: {avg_pr:.2f}")
        
        # Consistency check
        logging.info(f"\nConsistency Analysis:")
        pf_values = [r['profit_factor'] for r in results if r['profit_factor'] > 0]
        if pf_values:
            pf_min = min(pf_values)
            pf_max = max(pf_values)
            pf_std = (pf_max - pf_min) / avg_pf * 100 if avg_pf > 0 else 0
            logging.info(f"  Profit Factor Range: {pf_min:.2f} - {pf_max:.2f} (variance: {pf_std:.1f}%)")
        
        wr_values = [r['win_rate'] for r in results]
        if wr_values:
            wr_min = min(wr_values)
            wr_max = max(wr_values)
            logging.info(f"  Win Rate Range: {wr_min:.1f}% - {wr_max:.1f}%")
        
        # Status
        if avg_pf >= 1.25 and overall_wr >= 50:
            logging.info(f"\n✓ VALIDATION PASSED: System is consistent and profitable")
        else:
            logging.info(f"\n⚠ VALIDATION WARNING: Check metrics")
    
    logging.info(f"{'='*70}\n")


if __name__ == "__main__":
    main()
