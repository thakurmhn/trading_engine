# Logging Improvements

import logging
import json
from datetime import datetime
from typing import Dict, Any

class StructuredFormatter(logging.Formatter):
    """Structured logging formatter for better analysis"""
    
    def format(self, record):
        log_entry = {
            'timestamp': datetime.utcnow().isoformat(),
            'level': record.levelname,
            'module': record.module,
            'function': record.funcName,
            'line': record.lineno,
            'message': record.getMessage(),
        }
        
        # Add extra fields if present
        if hasattr(record, 'trade_id'):
            log_entry['trade_id'] = record.trade_id
        if hasattr(record, 'symbol'):
            log_entry['symbol'] = record.symbol
        if hasattr(record, 'side'):
            log_entry['side'] = record.side
        if hasattr(record, 'price'):
            log_entry['price'] = record.price
        if hasattr(record, 'quantity'):
            log_entry['quantity'] = record.quantity
            
        return json.dumps(log_entry)

class TradingLogger:
    """Enhanced logging for trading operations"""
    
    def __init__(self, name: str, log_file: str = None):
        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.INFO)
        
        # Console handler with color formatting
        console_handler = logging.StreamHandler()
        console_formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        console_handler.setFormatter(console_formatter)
        self.logger.addHandler(console_handler)
        
        # File handler with structured logging
        if log_file:
            file_handler = logging.FileHandler(log_file)
            file_handler.setFormatter(StructuredFormatter())
            self.logger.addHandler(file_handler)
    
    def log_trade_entry(self, symbol: str, side: str, price: float, quantity: int, 
                       trade_id: str, reason: str, **kwargs):
        """Log trade entry with structured data"""
        extra = {
            'trade_id': trade_id,
            'symbol': symbol,
            'side': side,
            'price': price,
            'quantity': quantity,
            'action': 'ENTRY',
            'reason': reason
        }
        extra.update(kwargs)
        self.logger.info(f"[ENTRY] {side} {symbol} @ {price} qty={quantity} reason={reason}", extra=extra)
    
    def log_trade_exit(self, symbol: str, side: str, entry_price: float, exit_price: float,
                      quantity: int, trade_id: str, reason: str, pnl: float, **kwargs):
        """Log trade exit with structured data"""
        extra = {
            'trade_id': trade_id,
            'symbol': symbol,
            'side': side,
            'entry_price': entry_price,
            'exit_price': exit_price,
            'quantity': quantity,
            'action': 'EXIT',
            'reason': reason,
            'pnl': pnl
        }
        extra.update(kwargs)
        self.logger.info(f"[EXIT] {side} {symbol} @ {exit_price} pnl={pnl} reason={reason}", extra=extra)
    
    def log_error(self, message: str, error: Exception = None, **kwargs):
        """Log errors with context"""
        if error:
            self.logger.error(f"{message}: {error}", exc_info=True, extra=kwargs)
        else:
            self.logger.error(message, extra=kwargs)
    
    def log_performance_metric(self, metric_name: str, value: float, **kwargs):
        """Log performance metrics"""
        extra = {'metric': metric_name, 'value': value}
        extra.update(kwargs)
        self.logger.info(f"[METRIC] {metric_name}={value}", extra=extra)

def setup_trading_logger(name: str, log_file: str = None) -> TradingLogger:
    """Setup enhanced trading logger"""
    return TradingLogger(name, log_file)

# Log rotation setup
def setup_log_rotation(log_file: str, max_bytes: int = 10*1024*1024, backup_count: int = 5):
    """Setup log rotation to prevent large log files"""
    from logging.handlers import RotatingFileHandler
    
    handler = RotatingFileHandler(
        log_file, 
        maxBytes=max_bytes, 
        backupCount=backup_count
    )
    handler.setFormatter(StructuredFormatter())
    return handler