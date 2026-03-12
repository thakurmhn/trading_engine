# Critical Function Refactoring

from typing import Dict, Any, Optional, Tuple
import logging
import pandas as pd
import pendulum as dt

class OrderManager:
    """Manages order processing logic separated from the main functions"""
    
    def __init__(self, account_type: str):
        self.account_type = account_type
        self.logger = logging.getLogger(f"OrderManager_{account_type}")
    
    def validate_entry_conditions(self, info: Dict, risk_info: Dict, ct) -> Tuple[bool, str]:
        """Validate basic entry conditions"""
        # Risk gate
        if risk_info.get("halt_trading", False):
            return False, "Risk halt active"
        
        # Startup suppression
        if self._is_startup_suppression_active(info, ct):
            return False, "Startup suppression active"
        
        # Trade count limits
        if not self._has_trade_capacity(info):
            return False, "Trade capacity exceeded"
        
        return True, "OK"
    
    def _is_startup_suppression_active(self, info: Dict, ct) -> bool:
        """Check if startup suppression is active"""
        suppress_until = info.get("startup_suppression_until")
        if suppress_until and ct < suppress_until:
            return True
        return False
    
    def _has_trade_capacity(self, info: Dict) -> bool:
        """Check if we have capacity for more trades"""
        trend_used = info.get("trend_trade_count", 0)
        trend_max = info.get("max_trades_trend", 8)
        scalp_used = info.get("scalp_trade_count", 0)
        scalp_max = info.get("max_trades_scalp", 12)
        
        return trend_used < trend_max or scalp_used < scalp_max
    
    def process_cooldown_logic(self, info: Dict, ct, breakdown_ctx: Dict) -> Tuple[bool, str]:
        """Process entry cooldown logic"""
        COOLDOWN_SECONDS = 120
        
        if not info.get("last_exit_time"):
            return True, "No previous exit"
        
        elapsed = (ct - info["last_exit_time"]).total_seconds()
        if elapsed >= COOLDOWN_SECONDS:
            return True, "Cooldown expired"
        
        # Check for cooldown bypass conditions
        if (breakdown_ctx.get("opening_s4_breakdown", False) or 
            breakdown_ctx.get("opening_r4_breakout", False)):
            return True, "Opening breakdown bypass"
        
        return False, f"Cooldown active: {elapsed:.0f}s < {COOLDOWN_SECONDS}s"
    
    def check_late_entry_gate(self, ct) -> Tuple[bool, str]:
        """Check if entry is too close to EOD"""
        bar_min_now = ct.hour * 60 + ct.minute
        if bar_min_now >= 14 * 60 + 45:  # 14:45
            return False, "Too close to EOD"
        return True, "Time OK"

class ScalpEntryProcessor:
    """Handles scalp entry logic"""
    
    def __init__(self, account_type: str):
        self.account_type = account_type
        self.logger = logging.getLogger(f"ScalpEntry_{account_type}")
    
    def process_scalp_entry(self, info: Dict, candles_3m: pd.DataFrame, 
                           traditional_levels: Dict, atr: float, 
                           pulse_metrics: Dict) -> Tuple[bool, Optional[Dict]]:
        """Process scalp entry logic"""
        # Check cooldown
        scalp_cd_until = info.get("scalp_cooldown_until")
        ct = dt.now()
        
        if scalp_cd_until and ct < scalp_cd_until:
            self.logger.info(f"Scalp cooldown active until {scalp_cd_until}")
            return False, None
        
        # Check pulse conditions
        if not self._validate_pulse_conditions(pulse_metrics):
            return False, None
        
        # Detect scalp signal
        scalp_signal = self._detect_scalp_signal(candles_3m, traditional_levels, atr)
        if not scalp_signal:
            return False, None
        
        # Validate pulse direction alignment
        if not self._validate_pulse_direction(pulse_metrics, scalp_signal):
            return False, None
        
        return True, scalp_signal
    
    def _validate_pulse_conditions(self, pulse_metrics: Dict) -> bool:
        """Validate pulse rate and burst conditions"""
        tick_rate = pulse_metrics.get("tick_rate", 0)
        burst_flag = pulse_metrics.get("burst_flag", False)
        
        PULSE_TICKRATE_THRESHOLD = 15.0
        
        if not burst_flag or tick_rate < PULSE_TICKRATE_THRESHOLD:
            self.logger.info(f"Pulse validation failed: tick_rate={tick_rate}, burst={burst_flag}")
            return False
        
        return True
    
    def _detect_scalp_signal(self, candles_3m: pd.DataFrame, 
                            traditional_levels: Dict, atr: float) -> Optional[Dict]:
        """Detect scalp dip/rally signals"""
        if len(candles_3m) < 2 or not traditional_levels or atr <= 0:
            return None
        
        last = candles_3m.iloc[-1]
        rng = last.high - last.low
        if rng == 0:
            return None
        
        # Check for dip/rally patterns
        s1 = traditional_levels.get("s1")
        r1 = traditional_levels.get("r1")
        atr_buf = 0.25 * atr
        
        # CALL: Buy on dip at support
        if s1 and last.low <= s1 + atr_buf and (last.close - last.low) > 0.5 * rng:
            return {
                "side": "CALL",
                "reason": "SCALP_BUY_DIP_S1",
                "zone": "S1",
                "level": float(s1),
                "stop": float(s1 - 0.5 * atr)
            }
        
        # PUT: Sell on rally at resistance
        if r1 and last.high >= r1 - atr_buf and (last.high - last.close) > 0.5 * rng:
            return {
                "side": "PUT", 
                "reason": "SCALP_SELL_RALLY_R1",
                "zone": "R1",
                "level": float(r1),
                "stop": float(r1 + 0.5 * atr)
            }
        
        return None
    
    def _validate_pulse_direction(self, pulse_metrics: Dict, scalp_signal: Dict) -> bool:
        """Validate pulse direction matches scalp signal"""
        direction_drift = pulse_metrics.get("direction_drift", "NEUTRAL")
        scalp_side = scalp_signal["side"]
        
        if direction_drift == "UP" and scalp_side != "CALL":
            self.logger.info(f"Direction mismatch: drift=UP but scalp_side={scalp_side}")
            return False
        
        if direction_drift == "DOWN" and scalp_side != "PUT":
            self.logger.info(f"Direction mismatch: drift=DOWN but scalp_side={scalp_side}")
            return False
        
        return True

class TrendEntryProcessor:
    """Handles trend entry logic"""
    
    def __init__(self, account_type: str):
        self.account_type = account_type
        self.logger = logging.getLogger(f"TrendEntry_{account_type}")
    
    def process_trend_entry(self, candles_3m: pd.DataFrame, candles_15m: pd.DataFrame,
                           cpr_levels: Dict, cam_levels: Dict, traditional_levels: Dict,
                           atr: float, ct, ticker: str) -> Tuple[bool, Optional[str], Optional[Dict]]:
        """Process trend entry logic"""
        # Quality gate check
        quality_ok, allowed_side, gate_reason, st_details = self._run_quality_gate(
            candles_3m, candles_15m, ct, ticker, cpr_levels, cam_levels
        )
        
        if not quality_ok:
            self.logger.info(f"Quality gate failed: {gate_reason}")
            return False, None, st_details
        
        # Signal detection
        signal = self._detect_trend_signal(
            candles_3m, candles_15m, cpr_levels, cam_levels, 
            traditional_levels, atr, ct, st_details
        )
        
        if not signal:
            return False, allowed_side, st_details
        
        # Validate signal side matches allowed side
        if signal["side"] != allowed_side:
            if not self._is_reversal_override(signal):
                self.logger.info(f"Signal side mismatch: {signal['side']} != {allowed_side}")
                return False, allowed_side, st_details
        
        return True, allowed_side, {"signal": signal, "st_details": st_details}
    
    def _run_quality_gate(self, candles_3m: pd.DataFrame, candles_15m: pd.DataFrame,
                         ct, ticker: str, cpr_levels: Dict, cam_levels: Dict) -> Tuple[bool, str, str, Dict]:
        """Run trend entry quality gate - placeholder for actual implementation"""
        # This would call the actual _trend_entry_quality_gate function
        # Simplified for this refactoring example
        return True, "CALL", "OK", {"adx14": 25.0, "rsi14": 50.0}
    
    def _detect_trend_signal(self, candles_3m: pd.DataFrame, candles_15m: pd.DataFrame,
                            cpr_levels: Dict, cam_levels: Dict, traditional_levels: Dict,
                            atr: float, ct, st_details: Dict) -> Optional[Dict]:
        """Detect trend signals - placeholder for actual implementation"""
        # This would call the actual detect_signal function
        # Simplified for this refactoring example
        return {
            "side": "CALL",
            "reason": "TREND_SIGNAL",
            "source": "SUPERTREND",
            "score": 75
        }
    
    def _is_reversal_override(self, signal: Dict) -> bool:
        """Check if signal qualifies for reversal override"""
        return (signal.get("pivot_event") == "REJECT" or 
                "REVERSAL" in signal.get("reason", "").upper())

class ExitManager:
    """Handles exit processing logic"""
    
    def __init__(self, account_type: str):
        self.account_type = account_type
        self.logger = logging.getLogger(f"ExitManager_{account_type}")
    
    def process_exits(self, info: Dict, candles_3m: pd.DataFrame, spot_price: float) -> bool:
        """Process exits for all open positions"""
        exit_occurred = False
        ct = dt.now()
        
        for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
            if info[leg].get("trade_flag", 0) == 1:
                state = info[leg]
                triggered, reason = self._check_exit_conditions(state, candles_3m, spot_price)
                
                if triggered:
                    success = self._execute_exit(state, leg, side, reason, spot_price)
                    if success:
                        info["last_exit_time"] = ct
                        exit_occurred = True
                        self.logger.info(f"Exit executed: {side} - {reason}")
        
        return exit_occurred
    
    def _check_exit_conditions(self, state: Dict, candles_3m: pd.DataFrame, 
                              spot_price: float) -> Tuple[bool, Optional[str]]:
        """Check if exit conditions are met - simplified version"""
        # This would call the actual check_exit_condition function
        # Simplified for this refactoring example
        current_price = spot_price  # Simplified
        stop = state.get("stop")
        
        if stop and current_price <= stop:
            return True, "SL_HIT"
        
        return False, None
    
    def _execute_exit(self, state: Dict, leg: str, side: str, reason: str, 
                     spot_price: float) -> bool:
        """Execute the exit order"""
        symbol = state.get("option_name")
        qty = state.get("quantity", 0)
        
        if self.account_type.lower() == "paper":
            success, order_id = self._send_paper_exit(symbol, qty, reason)
        else:
            success, order_id = self._send_live_exit(symbol, qty, reason)
        
        if success:
            # Update state
            state["trade_flag"] = 0
            state["is_open"] = False
            state["lifecycle_state"] = "EXIT"
        
        return success
    
    def _send_paper_exit(self, symbol: str, qty: int, reason: str) -> Tuple[bool, str]:
        """Send paper exit order"""
        self.logger.info(f"[PAPER EXIT] {symbol} qty={qty} reason={reason}")
        return True, f"paper_exit_{symbol}_{reason}"
    
    def _send_live_exit(self, symbol: str, qty: int, reason: str) -> Tuple[bool, str]:
        """Send live exit order"""
        # This would call the actual send_live_exit_order function
        self.logger.info(f"[LIVE EXIT] {symbol} qty={qty} reason={reason}")
        return True, f"live_exit_{symbol}_{reason}"