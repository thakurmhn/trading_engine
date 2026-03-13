"""
tbot_core.indicators — All indicators importable from here.

Every indicator takes a standard OHLCV DataFrame and returns Series/float.
Works with any timeframe DataFrame.

Usage:
    from tbot_core.indicators import rsi, cci, atr, supertrend, ema, adx
    from tbot_core.indicators import cpr, traditional_pivots, camarilla_pivots
    from tbot_core.indicators import detect_hammer, scan_all_patterns
"""

# Trend indicators
from tbot_core.indicators.trend import (  # noqa: F401
    supertrend,
    calculate_ema as ema,
    calculate_ema,
    calculate_adx as adx,
    calculate_adx,
    ema_bias,
    adx_bias,
)

# Momentum indicators
from tbot_core.indicators.momentum import (  # noqa: F401
    compute_rsi as rsi,
    compute_rsi,
    calculate_cci as cci,
    calculate_cci,
    cci_bias,
    williams_r,
    momentum_ok,
)

# Volatility indicators
from tbot_core.indicators.volatility import (  # noqa: F401
    calculate_atr as atr,
    calculate_atr,
    calculate_atr_series,
    resolve_atr,
    daily_atr,
)

# Pivot indicators
from tbot_core.indicators.pivots import (  # noqa: F401
    calculate_cpr as cpr,
    calculate_cpr,
    calculate_traditional_pivots as traditional_pivots,
    calculate_traditional_pivots,
    calculate_camarilla_pivots as camarilla_pivots,
    calculate_camarilla_pivots,
    classify_cpr_width,
)

# Volume indicators
from tbot_core.indicators.volume import (  # noqa: F401
    calculate_typical_price_ma as vwap,
    calculate_typical_price_ma,
)

# Candlestick patterns
from tbot_core.indicators.patterns import (  # noqa: F401
    detect_doji,
    detect_hammer,
    detect_inverted_hammer,
    detect_shooting_star,
    detect_engulfing,
    detect_morning_star,
    detect_evening_star,
    detect_three_white_soldiers,
    detect_three_black_crows,
    detect_harami,
    detect_piercing_line,
    detect_dark_cloud_cover,
    detect_tweezer_top,
    detect_tweezer_bottom,
    detect_spinning_top,
    detect_marubozu,
    scan_all_patterns,
)

# Indicator DataFrame builder
from tbot_core.indicators.builder import build_indicator_dataframe  # noqa: F401
