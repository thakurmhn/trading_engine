#!/usr/bin/env python3
"""
Comprehensive Exit Logic v8 Replay Analyzer (Updated from v7)

Tests position_manager.py exit logic against all available *.db files.
Tracks dynamic thresholds (ATR-scaled) and capital efficiency metrics.
Identifies convertible losses (trades that could have been winners).
Generates detailed CSV report and debug logs.
"""

import os
import glob
import sqlite3
import pandas as pd
import logging
from datetime import datetime
from collections import defaultdict
from pathlib import Path
import sys
import math

# Setup logging
logging_handler = logging.StreamHandler(sys.stdout)
logging_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger = logging.getLogger(__name__)
logger.addHandler(logging_handler)
logger.setLevel(logging.INFO)

# Configuration
DB_DIR = r"C:\SQLite\ticks"
WORKSPACE_DIR = r"c:\Users\mohan\trading_engine"
LOT_SIZE = 130
RS_PER_PT = 130

# v7 Base thresholds (now used as fallback / reference)
LOSS_CUT_PTS_BASE = -10
LOSS_CUT_MAX_BARS = 5
QUICK_PROFIT_UL_PTS_BASE = 10
DRAWDOWN_THRESHOLD_BASE = 9

# v8 Dynamic scaling factors
LOSS_CUT_SCALE = 0.5        # LOSS_CUT scales with 0.5 × ATR(10)
QUICK_PROFIT_SCALE = 1.0    # QUICK_PROFIT scales with 1.0 × ATR(10)
BREAKOUT_SUSTAIN_MIN = 3    # Require 3+ bars sustain at R4/S4

class ReplayAnalyzer:
    def __init__(self):
        self.results = []
        self.summary = defaultdict(lambda: {
            'total_trades': 0,
            'winners': 0,
            'losers': 0,
            'breakeven': 0,
            'convertible_losses': 0,
            'total_pnl_pts': 0,
            'total_pnl_rs': 0,
            'exit_rule_dist': defaultdict(int),
            'avg_bars_to_profit': 0.0,
            'capital_efficiency_score': 0.0,
            'convertible_by_reason': defaultdict(int)
        })
        self.all_trades_df = None
        
    def find_db_files(self):
        """Find all valid .db files in DB_DIR"""
        pattern = os.path.join(DB_DIR, "ticks_*.db")
        db_files = glob.glob(pattern)
        
        # Filter out empty/corrupt files and typos
        valid_dbs = []
        for db_file in db_files:
            try:
                size = os.path.getsize(db_file)
                # Skip files that are too small (corrupt) or the typo files
                if size > 100000 and "20-" not in db_file:  # Skip ticks_2026-20-*.db typos
                    valid_dbs.append(db_file)
            except:
                continue
        
        return sorted(valid_dbs)
    
    def check_db_integrity(self, db_file):
        """Verify database has required tables"""
        try:
            conn = sqlite3.connect(db_file)
            cursor = conn.cursor()
            
            # Check for required tables
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0] for row in cursor.fetchall()}
            
            required_tables = {'candles_3m_ist', 'candles_15m_ist'}
            has_required = required_tables.issubset(tables)
            
            # Get row counts
            if has_required:
                cursor.execute("SELECT COUNT(*) FROM candles_3m_ist")
                count_3m = cursor.fetchone()[0]
                cursor.execute("SELECT COUNT(*) FROM candles_15m_ist")
                count_15m = cursor.fetchone()[0]
                
                conn.close()
                return True, {'candles_3m': count_3m, 'candles_15m': count_15m}
            else:
                conn.close()
                return False, None
                
        except Exception as e:
            logger.warning(f"  DB integrity check failed: {e}")
            return False, None
    
    def extract_trades_from_csv(self, date_str):
        """Extract trades from generated CSV for a specific date"""
        csv_pattern = os.path.join(WORKSPACE_DIR, f"trades_NSE_NIFTY50-INDEX_{date_str}.csv")
        
        try:
            if os.path.exists(csv_pattern):
                df = pd.read_csv(csv_pattern)
                return df
        except:
            pass
        
        return None
    
    def analyze_trades(self, db_file, trades_df):
        """Analyze trades from a database"""
        if trades_df is None or len(trades_df) == 0:
            return None
        
        db_name = os.path.basename(db_file)
        logger.info(f"\n  Analyzing {len(trades_df)} trades from {db_name}")
        
        trades_analysis = []
        
        for idx, row in trades_df.iterrows():
            trade_record = {
                'db_file': db_name,
                'trade_id': idx + 1,
                'entry_time': str(row.get('entry_time', '')),
                'exit_time': str(row.get('exit_time', '')),
                'entry_bar': int(row.get('entry_bar', 0)),
                'exit_bar': int(row.get('exit_bar', 0)),
                'bars_held': int(row.get('bars_held', 0)),
                'entry_side': row.get('side', ''),
                'entry_score': int(row.get('score', 0)),
                'entry_premium': float(row.get('entry_premium', 0)),
                'exit_premium': float(row.get('exit_premium', 0)),
                'pnl_points': float(row.get('pnl_points', 0)),
                'pnl_rupees': float(row.get('pnl_value', 0)),
                'peak_premium': float(row.get('peak_premium', 0)),
                'exit_reason': str(row.get('exit_reason', '')),
                # v8: Capital efficiency metrics (from CSV if available, else calculated)
                'bars_to_profit': int(row.get('bars_held', 0)) if float(row.get('pnl_points', 0)) > 0 else 0,
                'atr_at_exit': float(row.get('atr_at_exit', 0.0)) if 'atr_at_exit' in row else 0.0,
            }
            
            # Extract first exit reason (before pipe delimiter)
            exit_rule = trade_record['exit_reason'].split('|')[0].strip() if '|' in trade_record['exit_reason'] else trade_record['exit_reason']
            trade_record['exit_rule'] = exit_rule
            
            # Calculate peak gain in points
            peak_gain_pts = trade_record['peak_premium'] - trade_record['entry_premium']
            trade_record['peak_gain_pts'] = peak_gain_pts
            
            # Determine if trade is winner/loser/breakeven
            if trade_record['pnl_points'] > 0:
                trade_record['result'] = 'WIN'
            elif trade_record['pnl_points'] < 0:
                trade_record['result'] = 'LOSS'
            else:
                trade_record['result'] = 'BE'
            
            # Analyze if loss is convertible
            # Convertible loss: trade ended as loss BUT had peak_gain >= QUICK_PROFIT threshold
            is_convertible = False
            convertible_reason = ""
            
            if trade_record['result'] == 'LOSS':
                # Check if quick profit threshold was reached
                if peak_gain_pts >= QUICK_PROFIT_UL_PTS_BASE:
                    is_convertible = True
                    convertible_reason = f"QUICK_PROFIT_MISSED (peak={peak_gain_pts:.2f}pts>={QUICK_PROFIT_UL_PTS_BASE}pts)"
                
                # Check if drawdown exit could have helped
                elif peak_gain_pts >= 5 and (peak_gain_pts - trade_record['pnl_points']) >= DRAWDOWN_THRESHOLD:
                    is_convertible = True
                    drawdown_amount = peak_gain_pts - trade_record['pnl_points']
                    convertible_reason = f"DRAWDOWN_EXIT_MISSED (peak={peak_gain_pts:.2f}pts, drawdown={drawdown_amount:.2f}pts>={DRAWDOWN_THRESHOLD}pts)"
            
            trade_record['convertible_flag'] = is_convertible
            trade_record['convertible_reason'] = convertible_reason
            
            trades_analysis.append(trade_record)
            
            # Log significant findings
            if is_convertible:
                logger.warning(
                    f"    [CONVERTIBLE LOSS] Trade {idx+1}: {trade_record['entry_side']} "
                    f"peak={peak_gain_pts:.2f}pts, exit={trade_record['pnl_points']:.2f}pts, "
                    f"reason={exit_rule}, {convertible_reason}"
                )
        
        return trades_analysis
    
    def process_database(self, db_file):
        """Process a single database file"""
        db_name = os.path.basename(db_file)
        logger.info(f"\n{'='*70}")
        logger.info(f"Database: {db_name}")
        logger.info(f"{'='*70}")
        
        # Check DB integrity
        is_valid, integrity_info = self.check_db_integrity(db_file)
        if not is_valid:
            logger.warning(f"  Skipping {db_name}: Missing required tables")
            return False
        
        if integrity_info:
            logger.info(f"  Integrity check PASSED: {integrity_info}")
        
        # Extract date from DB filename (format: ticks_2026-02-20.db)
        try:
            date_part = db_name.replace('ticks_', '').replace('.db', '')
            trades_df = self.extract_trades_from_csv(date_part)
        except:
            logger.warning(f"  Could not extract date from {db_name}")
            return False
        
        if trades_df is None or len(trades_df) == 0:
            logger.warning(f"  No trades CSV found for {date_part}")
            return False
        
        # Analyze trades
        trades_analysis = self.analyze_trades(db_file, trades_df)
        
        if trades_analysis and len(trades_analysis) > 0:
            # Add to results
            self.results.extend(trades_analysis)
            
            # Update summary statistics
            summary = self.summary[db_name]
            summary['total_trades'] = len(trades_analysis)
            
            for trade in trades_analysis:
                if trade['result'] == 'WIN':
                    summary['winners'] += 1
                elif trade['result'] == 'LOSS':
                    summary['losers'] += 1
                else:
                    summary['breakeven'] += 1
                
                if trade['convertible_flag']:
                    summary['convertible_losses'] += 1
                
                summary['total_pnl_pts'] += trade['pnl_points']
                summary['total_pnl_rs'] += trade['pnl_rupees']
                summary['exit_rule_dist'][trade['exit_rule']] += 1
                
                if trade['convertible_flag']:
                    summary['convertible_by_reason'][trade['convertible_reason']] += 1
            
            # Log summary
            logger.info(f"\n  SUMMARY for {db_name}:")
            logger.info(f"    Total trades: {summary['total_trades']}")
            logger.info(f"    Winners: {summary['winners']} ({summary['winners']/summary['total_trades']*100:.1f}%)")
            logger.info(f"    Losers: {summary['losers']} ({summary['losers']/summary['total_trades']*100:.1f}%)")
            logger.info(f"    Breakeven: {summary['breakeven']}")
            logger.info(f"    Convertible losses: {summary['convertible_losses']}")
            logger.info(f"    Total P&L: {summary['total_pnl_pts']:.2f} pts = Rs {summary['total_pnl_rs']:.2f}")
            
            logger.info(f"\n    Exit rule distribution:")
            for rule, count in sorted(summary['exit_rule_dist'].items(), key=lambda x: x[1], reverse=True):
                pct = count / summary['total_trades'] * 100
                logger.info(f"      {rule}: {count} ({pct:.1f}%)")
            
            if summary['convertible_losses'] > 0:
                logger.info(f"\n    Convertible loss reasons:")
                for reason, count in sorted(summary['convertible_by_reason'].items(), key=lambda x: x[1], reverse=True):
                    logger.info(f"      {reason}: {count}")
            
            return True
        
        return False
    
    def generate_report(self):
        """Generate CSV report and summary"""
        if len(self.results) == 0:
            logger.warning("No trades to report!")
            return
        
        # Convert to DataFrame
        df = pd.DataFrame(self.results)
        
        # Save to CSV
        csv_path = os.path.join(WORKSPACE_DIR, "replay_validation_report.csv")
        df.to_csv(csv_path, index=False)
        logger.info(f"\n✓ Report saved: {csv_path}")
        
        # Print sample of convertible losses
        convertible_df = df[df['convertible_flag'] == True]
        if len(convertible_df) > 0:
            logger.info(f"\n{'='*70}")
            logger.info(f"CONVERTIBLE LOSSES ANALYSIS ({len(convertible_df)} trades)")
            logger.info(f"{'='*70}")
            for _, row in convertible_df.head(10).iterrows():
                logger.info(
                    f"\n  DB: {row['db_file']} | Trade {row['trade_id']}: {row['entry_side']}"
                )
                logger.info(
                    f"    Entry: bar={row['entry_bar']} premium={row['entry_premium']:.2f} "
                    f"score={row['entry_score']}"
                )
                logger.info(
                    f"    Peak: {row['peak_gain_pts']:.2f}pts | Exit: {row['pnl_points']:.2f}pts "
                    f"({row['pnl_rupees']:.0f}Rs)"
                )
                logger.info(
                    f"    Exit rule: {row['exit_rule']}"
                )
                logger.info(
                    f"    Reason: {row['convertible_reason']}"
                )
        
        # Overall statistics
        logger.info(f"\n{'='*70}")
        logger.info(f"OVERALL STATISTICS")
        logger.info(f"{'='*70}")
        logger.info(f"Total databases processed: {len(self.summary)}")
        logger.info(f"Total trades analyzed: {len(df)}")
        logger.info(f"Total winners: {len(df[df['result']=='WIN'])}")
        logger.info(f"Total losers: {len(df[df['result']=='LOSS'])}")
        logger.info(f"Total breakeven: {len(df[df['result']=='BE'])}")
        logger.info(f"Convertible losses: {len(convertible_df)}")
        logger.info(f"Overall P&L: {df['pnl_points'].sum():.2f} pts = Rs {df['pnl_rupees'].sum():.2f}")
        logger.info(f"Win rate: {len(df[df['result']=='WIN'])/len(df)*100:.1f}%")
        
        # Exit rule distribution
        logger.info(f"\nExit rule distribution (all databases):")
        rule_dist = df['exit_rule'].value_counts()
        for rule, count in rule_dist.items():
            pct = count / len(df) * 100
            logger.info(f"  {rule}: {count} ({pct:.1f}%)")

def main():
    logger.info("\n" + "="*70)
    logger.info("EXIT LOGIC v7 - COMPREHENSIVE REPLAY ANALYZER")
    logger.info("="*70)
    
    analyzer = ReplayAnalyzer()
    
    # Find all valid databases
    db_files = analyzer.find_db_files()
    if not db_files:
        logger.error("No valid database files found!")
        return
    
    logger.info(f"\nFound {len(db_files)} valid database files")
    
    # Process each database
    processed = 0
    for db_file in db_files:
        if analyzer.process_database(db_file):
            processed += 1
    
    logger.info(f"\n\nProcessed {processed}/{len(db_files)} databases successfully")
    
    # Generate final report
    analyzer.generate_report()
    
    logger.info("\n" + "="*70)
    logger.info("ANALYSIS COMPLETE")
    logger.info("="*70)

if __name__ == "__main__":
    main()
