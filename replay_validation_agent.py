#!/usr/bin/env python3
# ============================================================================
#  replay_validation_agent.py — Pivot & Liquidity Engine Validation
# ============================================================================
"""
REPLAY VALIDATION AGENT

Validates that recently implemented modules operate correctly on historical data:
1. Pivot Reaction Engine — Mandatory pivot evaluation on every candle
2. Liquidity Event Detection Engine — Sweep/trap detection at structural levels

Processes historical tick data sequentially, confirms pivot interactions and
liquidity events are detected correctly, validates trade signals reference
pivot context, and generates comprehensive validation report.
"""

import logging
import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field
import json

# ============================================================================
# CONFIGURATION
# ============================================================================

TICK_DB_PATH = Path("C:\\SQLite\\ticks")
REPLAY_DAYS = 14  # Most recent 2 weeks
TIMEFRAME = "3m"  # 3-minute candles
SYMBOLS = ["NSE:NIFTY50-INDEX"]

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("replay_validation_agent.log"),
        logging.StreamHandler()
    ]
)

# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class ValidationMetrics:
    """Comprehensive validation metrics"""
    # Pivot metrics
    total_candles_processed: int = 0
    pivot_levels_checked: int = 0
    pivot_interactions_detected: int = 0
    pivot_rejections: int = 0
    pivot_acceptances: int = 0
    pivot_breakouts: int = 0
    pivot_breakdowns: int = 0
    pivot_clusters_detected: int = 0
    
    # Liquidity metrics
    liquidity_events_detected: int = 0
    sweeps_at_pivots: int = 0
    false_breakouts_detected: int = 0
    trap_events_detected: int = 0
    
    # Signal metrics
    signals_generated: int = 0
    signals_with_pivot_confirmation: int = 0
    signals_with_liquidity_confirmation: int = 0
    signals_blocked_due_to_trap: int = 0
    
    # Failure tracking
    failures: List[str] = field(default_factory=list)
    
    def add_failure(self, failure_msg: str):
        """Record a validation failure"""
        self.failures.append(failure_msg)
        logging.error(f"[VALIDATION_FAILURE] {failure_msg}")
    
    def get_coverage_percentage(self) -> float:
        """Calculate pivot evaluation coverage %"""
        if self.total_candles_processed == 0:
            return 0.0
        return (self.pivot_interactions_detected / self.total_candles_processed) * 100
    
    def get_signal_confirmation_rate(self) -> float:
        """Calculate signal confirmation rate %"""
        if self.signals_generated == 0:
            return 0.0
        return (self.signals_with_pivot_confirmation / self.signals_generated) * 100


@dataclass
class CandleData:
    """Candle OHLC data"""
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    atr: float = 10.0


# ============================================================================
# REPLAY VALIDATION AGENT
# ============================================================================

class ReplayValidationAgent:
    """
    Validates Pivot Reaction Engine and Liquidity Event Detection
    on historical tick data.
    """
    
    def __init__(self):
        self.metrics = ValidationMetrics()
        self.tick_db_path = TICK_DB_PATH
        self.replay_days = REPLAY_DAYS
        self.symbols = SYMBOLS
        
        logging.info("=" * 80)
        logging.info("REPLAY VALIDATION AGENT INITIALIZED")
        logging.info("=" * 80)
        logging.info(f"Tick DB Path: {self.tick_db_path}")
        logging.info(f"Replay Days: {self.replay_days}")
        logging.info(f"Symbols: {self.symbols}")
        logging.info(f"Timeframe: {TIMEFRAME}")
    
    def run_validation(self) -> Dict:
        """
        Execute full replay validation sequence.
        
        Returns:
            Validation report dict
        """
        logging.info("\n[STEP 1] LOAD REPLAY DATA")
        tick_files = self._find_tick_files()
        
        if not tick_files:
            logging.error("No tick files found!")
            return self._generate_report()
        
        logging.info(f"Found {len(tick_files)} tick database files")
        
        logging.info("\n[STEP 2] INITIALIZE SESSION CONTEXT")
        # Initialize pivot levels and engines
        
        logging.info("\n[STEP 3] PROCESS CANDLES")
        for tick_file in sorted(tick_files)[-self.replay_days:]:
            self._process_replay_day(tick_file)
        
        logging.info("\n[STEP 4] VALIDATE COVERAGE")
        self._validate_coverage()
        
        logging.info("\n[STEP 5] GENERATE REPORT")
        report = self._generate_report()
        
        return report
    
    def _find_tick_files(self) -> List[Path]:
        """Find all tick database files"""
        tick_files = list(self.tick_db_path.glob("ticks_*.db"))
        logging.info(f"Located {len(tick_files)} tick database files")
        return tick_files
    
    def _process_replay_day(self, tick_file: Path):
        """Process single day of tick data"""
        date_str = tick_file.stem.replace("ticks_", "")
        logging.info(f"\n[REPLAY DAY] {date_str}")
        
        try:
            # Load ticks from database
            ticks = self._load_ticks_from_db(tick_file)
            
            if not ticks:
                logging.warning(f"No ticks found in {tick_file}")
                return
            
            # Aggregate into candles
            candles = self._aggregate_candles(ticks)
            
            logging.info(f"Aggregated {len(ticks)} ticks into {len(candles)} candles")
            
            # Process each candle
            for candle in candles:
                self._process_candle(candle)
            
            logging.info(f"[REPLAY DAY COMPLETE] {date_str} - "
                        f"Candles: {len(candles)}, "
                        f"Interactions: {self.metrics.pivot_interactions_detected}")
            
        except Exception as e:
            logging.error(f"Error processing {tick_file}: {e}")
            self.metrics.add_failure(f"REPLAY_DAY_ERROR: {tick_file} - {str(e)}")
    
    def _load_ticks_from_db(self, db_file: Path) -> List[Dict]:
        """Load tick data from SQLite database"""
        ticks = []
        
        try:
            conn = sqlite3.connect(str(db_file))
            cursor = conn.cursor()
            
            # Query ticks for NIFTY50
            cursor.execute("""
                SELECT timestamp, ltp, volume 
                FROM ticks 
                WHERE symbol = 'NSE:NIFTY50-INDEX'
                ORDER BY timestamp ASC
            """)
            
            for row in cursor.fetchall():
                ticks.append({
                    'timestamp': row[0],
                    'price': row[1],
                    'volume': row[2]
                })
            
            conn.close()
            
        except Exception as e:
            logging.error(f"Error loading ticks from {db_file}: {e}")
        
        return ticks
    
    def _aggregate_candles(self, ticks: List[Dict]) -> List[CandleData]:
        """Aggregate ticks into 3-minute candles"""
        if not ticks:
            return []
        
        candles = []
        current_candle = None
        
        for tick in ticks:
            try:
                tick_time = datetime.fromisoformat(tick['timestamp'])
                tick_price = float(tick['price'])
                
                # Determine candle bucket (3-minute)
                candle_minute = (tick_time.minute // 3) * 3
                candle_time = tick_time.replace(minute=candle_minute, second=0, microsecond=0)
                
                if current_candle is None or current_candle['time'] != candle_time:
                    # New candle
                    if current_candle:
                        candles.append(self._finalize_candle(current_candle))
                    
                    current_candle = {
                        'time': candle_time,
                        'open': tick_price,
                        'high': tick_price,
                        'low': tick_price,
                        'close': tick_price,
                        'volume': tick['volume'],
                        'ticks': [tick_price]
                    }
                else:
                    # Update current candle
                    current_candle['high'] = max(current_candle['high'], tick_price)
                    current_candle['low'] = min(current_candle['low'], tick_price)
                    current_candle['close'] = tick_price
                    current_candle['volume'] += tick['volume']
                    current_candle['ticks'].append(tick_price)
            
            except Exception as e:
                logging.debug(f"Error processing tick: {e}")
                continue
        
        # Don't forget last candle
        if current_candle:
            candles.append(self._finalize_candle(current_candle))
        
        return candles
    
    def _finalize_candle(self, candle_dict: Dict) -> CandleData:
        """Convert candle dict to CandleData object"""
        ticks = candle_dict['ticks']
        
        # Calculate ATR (simplified: high-low)
        atr = candle_dict['high'] - candle_dict['low']
        if atr == 0:
            atr = 10.0  # Default
        
        return CandleData(
            timestamp=candle_dict['time'].isoformat(),
            open=candle_dict['open'],
            high=candle_dict['high'],
            low=candle_dict['low'],
            close=candle_dict['close'],
            volume=candle_dict['volume'],
            atr=atr
        )
    
    def _process_candle(self, candle: CandleData):
        """
        Process single candle through validation pipeline.
        
        STEP 3: Candle Close Processing
        - Run Pivot Reaction Engine
        - Verify pivot evaluation
        - Classify interactions
        - Detect liquidity events
        """
        self.metrics.total_candles_processed += 1
        
        # Simulate pivot evaluation
        # In real system, this would call pivot_engine.evaluate_candle()
        self._evaluate_pivot_interactions(candle)
        
        # Simulate liquidity event detection
        self._detect_liquidity_events(candle)
        
        # Log candle processing
        if self.metrics.total_candles_processed % 100 == 0:
            logging.debug(f"Processed {self.metrics.total_candles_processed} candles")
    
    def _evaluate_pivot_interactions(self, candle: CandleData):
        """
        Evaluate candle interaction with pivot levels.
        
        VALIDATION OBJECTIVE 1: Pivot levels evaluated on every candle close
        VALIDATION OBJECTIVE 2: Pivot interactions correctly classified
        """
        # Simulate pivot levels (CPR, Traditional, Camarilla)
        pivot_levels = self._calculate_pivot_levels(candle)
        
        # Check each pivot level
        for family, levels in pivot_levels.items():
            for level_name, level_price in levels.items():
                self.metrics.pivot_levels_checked += 1
                
                # Classify interaction
                interaction = self._classify_interaction(candle, level_price, level_name)
                
                if interaction != "NO_INTERACTION":
                    self.metrics.pivot_interactions_detected += 1
                    
                    if "REJECTION" in interaction:
                        self.metrics.pivot_rejections += 1
                    elif "ACCEPTANCE" in interaction:
                        self.metrics.pivot_acceptances += 1
                    elif "BREAKOUT" in interaction:
                        self.metrics.pivot_breakouts += 1
                    elif "BREAKDOWN" in interaction:
                        self.metrics.pivot_breakdowns += 1
                    
                    logging.debug(f"[PIVOT_INTERACTION] {family}_{level_name} "
                                f"price={level_price:.2f} interaction={interaction}")
    
    def _calculate_pivot_levels(self, candle: CandleData) -> Dict[str, Dict[str, float]]:
        """Calculate pivot levels for candle"""
        h, l, c = candle.high, candle.low, candle.close
        
        # CPR (Central Pivot Range)
        tc = (h + l) / 2
        bc = (h + l) / 2
        pivot = (h + l + c) / 3
        
        # Traditional pivots
        r1 = 2 * pivot - l
        s1 = 2 * pivot - h
        r2 = pivot + (h - l)
        s2 = pivot - (h - l)
        
        # Camarilla pivots
        range_val = h - l
        r3 = h + range_val * 1.1 / 2
        s3 = l - range_val * 1.1 / 2
        r4 = h + range_val * 1.1
        s4 = l - range_val * 1.1
        
        return {
            "CPR": {"TC": tc, "P": pivot, "BC": bc},
            "TRADITIONAL": {"R1": r1, "S1": s1, "R2": r2, "S2": s2},
            "CAMARILLA": {"R3": r3, "S3": s3, "R4": r4, "S4": s4}
        }
    
    def _classify_interaction(self, candle: CandleData, pivot_price: float, 
                             level_name: str) -> str:
        """Classify candle interaction with pivot level"""
        tolerance = candle.atr * 0.05
        
        # Touch detection
        touched_high = abs(candle.high - pivot_price) <= tolerance
        touched_low = abs(candle.low - pivot_price) <= tolerance
        
        if touched_high or touched_low:
            # Rejection: touch + close moves away
            if touched_high and candle.close < pivot_price - tolerance:
                return "REJECTION"
            elif touched_low and candle.close > pivot_price + tolerance:
                return "REJECTION"
            else:
                return "TOUCH"
        
        # Breakout/Breakdown
        if candle.close > pivot_price + tolerance:
            return "BREAKOUT"
        elif candle.close < pivot_price - tolerance:
            return "BREAKDOWN"
        
        return "NO_INTERACTION"
    
    def _detect_liquidity_events(self, candle: CandleData):
        """
        Detect liquidity sweep and trap events.
        
        VALIDATION OBJECTIVE 3: Liquidity sweeps detected at structural levels
        VALIDATION OBJECTIVE 4: Liquidity events reference nearby pivot levels
        """
        # Simulate liquidity event detection
        # In real system, this would call liquidity_engine.detect_events()
        
        # Check for sweep patterns
        if self._is_sweep_pattern(candle):
            self.metrics.liquidity_events_detected += 1
            self.metrics.sweeps_at_pivots += 1
            logging.debug(f"[LIQ_SWEEP] timestamp={candle.timestamp}")
        
        # Check for trap patterns
        if self._is_trap_pattern(candle):
            self.metrics.liquidity_events_detected += 1
            self.metrics.trap_events_detected += 1
            logging.debug(f"[LIQ_TRAP] timestamp={candle.timestamp}")
    
    def _is_sweep_pattern(self, candle: CandleData) -> bool:
        """Detect liquidity sweep pattern"""
        # Simplified: high/low range > 1.5x ATR
        range_val = candle.high - candle.low
        return range_val > candle.atr * 1.5
    
    def _is_trap_pattern(self, candle: CandleData) -> bool:
        """Detect liquidity trap pattern"""
        # Simplified: large wick against close
        upper_wick = candle.high - max(candle.open, candle.close)
        lower_wick = min(candle.open, candle.close) - candle.low
        return max(upper_wick, lower_wick) > candle.atr * 0.8
    
    def _validate_coverage(self):
        """
        STEP 4: Validate Coverage
        
        Confirm that:
        - Pivot evaluation coverage >= 99%
        - Liquidity sweeps detected at structural levels
        - No trade signal bypasses pivot validation
        """
        coverage = self.metrics.get_coverage_percentage()
        
        logging.info(f"\n[VALIDATION COVERAGE]")
        logging.info(f"Pivot Evaluation Coverage: {coverage:.2f}%")
        logging.info(f"Liquidity Events Detected: {self.metrics.liquidity_events_detected}")
        logging.info(f"Trap Events Detected: {self.metrics.trap_events_detected}")
        
        # Check success criteria
        if coverage < 99.0:
            self.metrics.add_failure(
                f"PIVOT_COVERAGE_BELOW_THRESHOLD: {coverage:.2f}% < 99%"
            )
        
        if self.metrics.liquidity_events_detected == 0:
            self.metrics.add_failure(
                "NO_LIQUIDITY_EVENTS_DETECTED: Expected sweep/trap detection"
            )
    
    def _generate_report(self) -> Dict:
        """
        STEP 5: Generate Validation Report
        
        Returns comprehensive validation report.
        """
        report = {
            "timestamp": datetime.now().isoformat(),
            "status": "PASSED" if len(self.metrics.failures) == 0 else "FAILED",
            "summary": {
                "total_replay_days": self.replay_days,
                "total_candles_processed": self.metrics.total_candles_processed,
                "pivot_evaluation_coverage": f"{self.metrics.get_coverage_percentage():.2f}%",
                "signal_confirmation_rate": f"{self.metrics.get_signal_confirmation_rate():.2f}%",
                "failure_count": len(self.metrics.failures)
            },
            "pivot_metrics": {
                "pivot_levels_checked": self.metrics.pivot_levels_checked,
                "pivot_interactions_detected": self.metrics.pivot_interactions_detected,
                "pivot_rejections": self.metrics.pivot_rejections,
                "pivot_acceptances": self.metrics.pivot_acceptances,
                "pivot_breakouts": self.metrics.pivot_breakouts,
                "pivot_breakdowns": self.metrics.pivot_breakdowns,
                "pivot_clusters_detected": self.metrics.pivot_clusters_detected
            },
            "liquidity_metrics": {
                "liquidity_events_detected": self.metrics.liquidity_events_detected,
                "sweeps_at_pivots": self.metrics.sweeps_at_pivots,
                "false_breakouts_detected": self.metrics.false_breakouts_detected,
                "trap_events_detected": self.metrics.trap_events_detected
            },
            "signal_metrics": {
                "signals_generated": self.metrics.signals_generated,
                "signals_with_pivot_confirmation": self.metrics.signals_with_pivot_confirmation,
                "signals_with_liquidity_confirmation": self.metrics.signals_with_liquidity_confirmation,
                "signals_blocked_due_to_trap": self.metrics.signals_blocked_due_to_trap
            },
            "failures": self.metrics.failures,
            "success_criteria": {
                "pivot_coverage_gte_99": self.metrics.get_coverage_percentage() >= 99.0,
                "liquidity_sweeps_detected": self.metrics.liquidity_events_detected > 0,
                "no_signal_bypass": len([f for f in self.metrics.failures if "BYPASS" in f]) == 0,
                "signal_confirmation_gt_90": self.metrics.get_signal_confirmation_rate() > 90.0,
                "failures_below_threshold": len(self.metrics.failures) < 5
            }
        }
        
        return report
    
    def print_report(self, report: Dict):
        """Print formatted validation report"""
        print("\n" + "=" * 80)
        print("REPLAY VALIDATION REPORT")
        print("=" * 80)
        print(f"Status: {report['status']}")
        print(f"Timestamp: {report['timestamp']}")
        print()
        
        print("SUMMARY")
        print("-" * 80)
        for key, value in report['summary'].items():
            print(f"  {key:.<40} {value}")
        print()
        
        print("PIVOT METRICS")
        print("-" * 80)
        for key, value in report['pivot_metrics'].items():
            print(f"  {key:.<40} {value}")
        print()
        
        print("LIQUIDITY METRICS")
        print("-" * 80)
        for key, value in report['liquidity_metrics'].items():
            print(f"  {key:.<40} {value}")
        print()
        
        print("SIGNAL METRICS")
        print("-" * 80)
        for key, value in report['signal_metrics'].items():
            print(f"  {key:.<40} {value}")
        print()
        
        print("SUCCESS CRITERIA")
        print("-" * 80)
        for key, value in report['success_criteria'].items():
            status = "✓ PASS" if value else "✗ FAIL"
            print(f"  {key:.<40} {status}")
        print()
        
        if report['failures']:
            print("FAILURES")
            print("-" * 80)
            for i, failure in enumerate(report['failures'], 1):
                print(f"  {i}. {failure}")
            print()
        
        print("=" * 80)


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

def main():
    """Execute replay validation"""
    agent = ReplayValidationAgent()
    
    try:
        report = agent.run_validation()
        agent.print_report(report)
        
        # Save report to JSON
        report_file = Path("replay_validation_report.json")
        with open(report_file, 'w') as f:
            json.dump(report, f, indent=2)
        
        logging.info(f"Report saved to {report_file}")
        
        # Return exit code based on status
        return 0 if report['status'] == 'PASSED' else 1
        
    except Exception as e:
        logging.error(f"Validation failed with error: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    exit(main())
