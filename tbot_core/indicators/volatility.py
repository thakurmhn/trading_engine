"""Volatility indicators: ATR, daily ATR, ATR resolution."""

import logging

import pandas as pd


def calculate_atr(candles, period=14):
    """Calculate ATR using a rolling window. Returns the latest ATR value (scalar)."""
    highs = candles['high'].astype(float)
    lows = candles['low'].astype(float)
    closes = candles['close'].astype(float)

    prev_close = closes.shift(1)
    tr = pd.concat([
        highs - lows,
        (highs - prev_close).abs(),
        (lows - prev_close).abs()
    ], axis=1).max(axis=1)

    atr_series = tr.rolling(period).mean()
    return atr_series.iloc[-1] if not atr_series.empty else None


def calculate_atr_series(df, period=14):
    """Calculate ATR as a full Series (for DataFrame enrichment)."""
    high, low, close = df["high"], df["low"], df["close"]
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=period, min_periods=period).mean()


def resolve_atr(candles_3m, daily_atr_val=None, period=14):
    """Resolve ATR value for signal detection.

    Returns (atr_value, source_string).
    """
    if daily_atr_val is not None:
        try:
            return float(daily_atr_val), "daily override"
        except Exception:
            return None, "daily override invalid"

    if candles_3m is not None and isinstance(candles_3m, pd.DataFrame):
        atr_val = calculate_atr(candles_3m, period=period)
        if atr_val is not None:
            logging.debug(f"[ATR CALC] period={period} value={atr_val:.2f}")
            return atr_val, "calculated"
        else:
            return None, "calculation failed"

    return None, "unavailable"


def daily_atr(df_daily, period=7):
    """Daily ATR calculation with NaN handling."""
    if df_daily is None or len(df_daily) < period + 1:
        return None

    hl = df_daily["high"] - df_daily["low"]
    hc = (df_daily["high"] - df_daily["close"].shift()).abs()
    lc = (df_daily["low"] - df_daily["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)

    atr_series = tr.rolling(period).mean().dropna()
    if atr_series.empty:
        return None
    val = atr_series.iloc[-1]
    return None if pd.isna(val) else float(val)
