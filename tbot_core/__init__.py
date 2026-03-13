"""
tbot_core — Modular Trading Engine Library
==========================================

Reusable library extracted from the trading_engine monolith.
Supports Options Buying, Options Selling, Equity Intraday, Swing, and Screeners.

Usage:
    from tbot_core.indicators import rsi, cci, atr, supertrend, ema, adx
    from tbot_core.indicators import cpr, traditional_pivots, camarilla_pivots
    from tbot_core.indicators import detect_hammer, detect_engulfing, scan_all_patterns
    from tbot_core.broker import build_broker_adapter
    from tbot_core.data import Timeframe, CandleStore
    from tbot_core.signals import check_supertrend_pullback, detect_compression_breakout
    from tbot_core.entry import EntryScorer, QualityGate
    from tbot_core.exit import ExitEngine, StopLossRule, TrailingStopRule
    from tbot_core.context import RegimeContext, compute_regime_context
"""

__version__ = "0.1.0"
