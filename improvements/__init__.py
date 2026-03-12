# Master Improvements File - Import all enhancements
"""
This file consolidates all the immediate improvements for execution.py
Import this file to access all enhancement utilities in one place.
"""

# Error Handling Improvements
from .error_handling_fixes import (
    retry_on_failure,
    safe_api_call,
    safe_market_data_fetch,
    validate_order_params
)

# Configuration Management
from .config_management import (
    TRADING_CONSTANTS,
    ENVIRONMENT_CONFIGS,
    get_config
)

# Logging Improvements
from .logging_improvements import (
    StructuredFormatter,
    TradingLogger,
    setup_trading_logger,
    setup_log_rotation
)

# Performance Optimizations
from .performance_optimizations import (
    PerformanceOptimizer,
    DataCache,
    perf_optimizer,
    data_cache,
    performance_monitor
)

# Function Refactoring Classes
from .function_refactoring import (
    OrderManager,
    ScalpEntryProcessor,
    TrendEntryProcessor,
    ExitManager
)

# Quick setup function for immediate use
def setup_improvements(environment='DEVELOPMENT', log_file=None):
    """
    Quick setup function to initialize all improvements
    
    Args:
        environment: 'DEVELOPMENT', 'PRODUCTION', or 'TESTING'
        log_file: Optional log file path
    
    Returns:
        dict: Configured components ready for use
    """
    
    # Get configuration
    config = get_config(environment)
    
    # Setup enhanced logging
    logger = setup_trading_logger('execution_improved', log_file)
    
    # Initialize performance optimizer
    perf_optimizer.clear_expired_cache()
    data_cache.clear()
    
    # Create order management components
    order_manager = OrderManager("PAPER")  # or "LIVE"
    scalp_processor = ScalpEntryProcessor("PAPER")
    trend_processor = TrendEntryProcessor("PAPER")
    exit_manager = ExitManager("PAPER")
    
    return {
        'config': config,
        'logger': logger,
        'perf_optimizer': perf_optimizer,
        'data_cache': data_cache,
        'order_manager': order_manager,
        'scalp_processor': scalp_processor,
        'trend_processor': trend_processor,
        'exit_manager': exit_manager
    }

# Example usage:
"""
# At the top of your execution.py file, add:
from improvements import setup_improvements, retry_on_failure, safe_api_call

# Initialize improvements
components = setup_improvements('PRODUCTION', 'logs/execution_improved.log')
config = components['config']
logger = components['logger']

# Apply decorators to existing functions:
@retry_on_failure(max_retries=3)
@safe_api_call
def send_live_entry_order(symbol, qty, side, buffer=config['ENTRY_OFFSET']):
    # Your existing code here
    pass

# Use structured logging:
logger.log_trade_entry(symbol, side, price, quantity, trade_id, reason)

# Use performance monitoring:
@performance_monitor
def paper_order(candles_3m, hist_yesterday_15m=None, exit=False, mode="REPLAY", spot_price=None):
    # Your existing code here
    pass
"""

__all__ = [
    'retry_on_failure',
    'safe_api_call', 
    'safe_market_data_fetch',
    'validate_order_params',
    'TRADING_CONSTANTS',
    'get_config',
    'setup_trading_logger',
    'PerformanceOptimizer',
    'DataCache',
    'performance_monitor',
    'OrderManager',
    'ScalpEntryProcessor', 
    'TrendEntryProcessor',
    'ExitManager',
    'setup_improvements'
]