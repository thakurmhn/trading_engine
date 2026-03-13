"""Momentum indicators: RSI, CCI, Williams %R, momentum_ok."""

import logging

import numpy as np
import pandas as pd


def compute_rsi(series, period=14):
    """Compute Relative Strength Index (RSI).

    Parameters
    ----------
    series : pd.Series
        Closing prices.
    period : int
        Lookback period (default 14).

    Returns
    -------
    pd.Series
        RSI values (0-100).
    """
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def calculate_cci(df, period=20):
    """Commodity Channel Index (CCI) as a Series.

    Uses min_periods=period//4 for early convergence and floors mean deviation
    at 0.5 to prevent explosion after flat-bar consolidation.
    """
    if df is None or df.empty or not {"high", "low", "close"}.issubset(df.columns):
        logging.warning("[CCI] No data")
        return pd.Series(dtype=float, index=df.index if df is not None else None)

    min_p = max(period // 4, 3)
    tp = (df["high"] + df["low"] + df["close"]) / 3
    ma = tp.rolling(period, min_periods=min_p).mean()
    md = (tp - ma).abs().rolling(period, min_periods=min_p).mean()
    md_safe = md.clip(lower=0.5)
    return (tp - ma) / (0.015 * md_safe)


def cci_bias(df, period=20, threshold=60):
    """CCI bias: checks last CCI value against thresholds."""
    if df is None or df.empty or not {"high", "low", "close"}.issubset(df.columns):
        return "NEUTRAL"

    tp = (df["high"] + df["low"] + df["close"]) / 3
    ma = tp.rolling(period).mean()
    md = (tp - ma).abs().rolling(period).mean()
    cci_val = (tp - ma) / (0.015 * md)

    if cci_val.empty or pd.isna(cci_val.iloc[-1]):
        return "NEUTRAL"

    last_cci = cci_val.iloc[-1]
    if last_cci > threshold:
        return "BULLISH"
    elif last_cci < -threshold:
        return "BEARISH"
    return "NEUTRAL"


def williams_r(candles, period=14):
    """Compute Williams %R oscillator.

    Returns float in range [-100, 0].
    """
    if candles is None or candles.empty or len(candles) < period:
        logging.warning("[W%R] Not enough candles")
        return np.nan

    highest_high = candles['high'].tail(period).max()
    lowest_low = candles['low'].tail(period).min()
    last_close = candles['close'].iloc[-1]

    if highest_high == lowest_low:
        logging.warning("[W%R] Invalid range (high == low)")
        return np.nan

    wr = ((highest_high - last_close) / (highest_high - lowest_low)) * -100

    logging.debug(
        f"[W%R] high={highest_high:.2f}, low={lowest_low:.2f}, "
        f"close={last_close:.2f}, W%R={wr:.2f}"
    )
    return wr


def momentum_ok(candles, side):
    """Dual-EMA momentum confirmation.

    CALL (bullish):
        1. Last 2 closes both above max(EMA9, EMA13)
        2. EMA9 > EMA13 (fast above slow)
        3. EMA gap widening vs prior bar

    PUT (bearish): mirror conditions.

    Returns (bool, float) where float is current EMA gap.
    """
    if candles is None or len(candles) < 3:
        return False, 0.0

    if "ema9" not in candles.columns or "ema13" not in candles.columns:
        last = candles.iloc[-1]
        prev = candles.iloc[-2]
        try:
            delta = float(last["close"]) - float(prev["close"])
            ok = delta > 0 if side == "CALL" else delta < 0
            return ok, delta
        except Exception:
            return False, 0.0

    try:
        last3 = candles.tail(3)
        e9v = last3["ema9"].astype(float).values
        e13v = last3["ema13"].astype(float).values
        cv = last3["close"].astype(float).values
    except Exception:
        return False, 0.0

    if any(pd.isna(e9v)) or any(pd.isna(e13v)) or any(pd.isna(cv)):
        return False, 0.0

    gap_prev = e9v[-2] - e13v[-2]
    gap_curr = e9v[-1] - e13v[-1]

    if side == "CALL":
        closes_above = (cv[-2] > e9v[-2] and cv[-2] > e13v[-2] and
                        cv[-1] > e9v[-1] and cv[-1] > e13v[-1])
        ema_aligned = e9v[-1] > e13v[-1]
        gap_widening = gap_curr > gap_prev
        ok = closes_above and ema_aligned and gap_widening
    else:
        closes_below = (cv[-2] < e9v[-2] and cv[-2] < e13v[-2] and
                        cv[-1] < e9v[-1] and cv[-1] < e13v[-1])
        ema_aligned = e9v[-1] < e13v[-1]
        gap_widening = (-gap_curr) > (-gap_prev)
        ok = closes_below and ema_aligned and gap_widening

    logging.debug(
        f"[MOMENTUM_OK][{side}] ok={ok} gap_curr={gap_curr:.2f} gap_prev={gap_prev:.2f} "
        f"e9={e9v[-1]:.2f} e13={e13v[-1]:.2f} c={cv[-1]:.2f}"
    )
    return ok, gap_curr
