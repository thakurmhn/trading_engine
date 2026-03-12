# Configuration Management Improvements

# Extract hardcoded constants from execution.py to config files

# trading_config.py
TRADING_CONSTANTS = {
    # Scalp trading settings
    'SCALP_PT_POINTS': 18.0,
    'SCALP_SL_POINTS': 10.0,
    'SCALP_MIN_HOLD_BARS': 2,
    'SCALP_EXTREME_MOVE_ATR_MULT': 0.90,
    'SCALP_ATR_SL_MIN_MULT': 0.60,
    'SCALP_ATR_SL_MAX_MULT': 0.80,
    'SCALP_COOLDOWN_MINUTES': 20,
    'SCALP_HISTORY_MAXLEN': 120,
    
    # Trend trading settings
    'TREND_MIN_HOLD_BARS': 3,
    'TREND_EXTREME_MOVE_ATR_MULT': 1.15,
    
    # Paper trading settings
    'PAPER_SLIPPAGE_POINTS': 1.5,
    
    # Exit settings
    'PARTIAL_TG_QTY_FRAC': 0.50,
    'PARTIAL_PT1_QTY_FRAC': 0.40,
    'PARTIAL_PT2_QTY_FRAC': 0.30,
    'DEFAULT_TIME_EXIT_CANDLES': 16,
    
    # Oscillator settings
    'DEFAULT_OSC_RSI_CALL': 75.0,
    'DEFAULT_OSC_RSI_PUT': 25.0,
    'DEFAULT_OSC_CCI_CALL': 130.0,
    'DEFAULT_OSC_CCI_PUT': -130.0,
    'DEFAULT_OSC_WR_CALL': -10.0,
    'DEFAULT_OSC_WR_PUT': -88.0,
    
    # Risk management
    'EMA_STRETCH_BLOCK_MULT': 2.5,
    'EMA_STRETCH_TAG_MULT': 1.8,
    'MAX_TRADE_TREND': 8,
    'MAX_TRADE_SCALP': 12,
    
    # Trailing settings
    'TRAIL_STRONG_MULT': 1.5,
    'TRAIL_WEAK_MULT': 0.8,
    'TRAIL_TREND_DAY_MULT': 1.8,
    
    # Risk scaling
    'RISK_SCALING_TREND': 1.0,
    'RISK_SCALING_RANGE': 0.6,
    'RISK_SCALING_REVERSAL': 0.7,
    
    # Pulse settings
    'PULSE_TICKRATE_THRESHOLD': 15.0,
    'STARTUP_SUPPRESSION_MINUTES': 5,
}

# Environment-specific settings
ENVIRONMENT_CONFIGS = {
    'DEVELOPMENT': {
        'LOG_LEVEL': 'DEBUG',
        'RETRY_ATTEMPTS': 3,
        'API_TIMEOUT': 30,
        'ENABLE_PAPER_SLIPPAGE': True,
    },
    'PRODUCTION': {
        'LOG_LEVEL': 'INFO',
        'RETRY_ATTEMPTS': 5,
        'API_TIMEOUT': 10,
        'ENABLE_PAPER_SLIPPAGE': False,
    },
    'TESTING': {
        'LOG_LEVEL': 'WARNING',
        'RETRY_ATTEMPTS': 1,
        'API_TIMEOUT': 5,
        'ENABLE_PAPER_SLIPPAGE': True,
    }
}

def get_config(env='DEVELOPMENT'):
    """Get configuration for specific environment"""
    base_config = TRADING_CONSTANTS.copy()
    env_config = ENVIRONMENT_CONFIGS.get(env, ENVIRONMENT_CONFIGS['DEVELOPMENT'])
    base_config.update(env_config)
    return base_config