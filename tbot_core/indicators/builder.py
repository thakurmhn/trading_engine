"""Indicator DataFrame builder — enriches OHLCV with all standard indicators.

Extracted from orchestration.py:build_indicator_dataframe().
"""

import logging

import pandas as pd

from tbot_core.indicators.trend import calculate_ema, calculate_adx, supertrend
from tbot_core.indicators.momentum import compute_rsi, calculate_cci
from tbot_core.indicators.volatility import calculate_atr_series
from tbot_core.indicators.volume import calculate_typical_price_ma


def _fmt(val):
    return f"{val:.2f}" if val is not None and not pd.isna(val) else "NA"


def build_indicator_dataframe(symbol, df, interval="3m"):
    """Enrich an OHLCV DataFrame with all standard indicators.

    Adds columns: ema9, ema13, adx14, cci20, atr14, rsi14, vwap,
    supertrend_line, supertrend_bias, supertrend_slope.

    Parameters
    ----------
    symbol : str
        Instrument symbol (for logging).
    df : pd.DataFrame
        OHLCV DataFrame.
    interval : str
        Timeframe label (for logging).

    Returns
    -------
    pd.DataFrame
        Enriched DataFrame (copy, original is not modified).
    """
    if df is None or df.empty:
        logging.warning(f"[INDICATORS] No {interval} candles for {symbol}")
        return pd.DataFrame()

    df = df.copy()

    df["ema9"] = calculate_ema(df, column="close", period=9)
    df["ema13"] = calculate_ema(df, column="close", period=13)

    try:
        df["adx14"] = calculate_adx(df)
    except Exception as e:
        logging.error(f"[ADX ERROR] {e}")
        df["adx14"] = float("nan")

    try:
        df["cci20"] = calculate_cci(df)
    except Exception as e:
        logging.error(f"[CCI ERROR] {e}")
        df["cci20"] = float("nan")

    try:
        df["atr14"] = calculate_atr_series(df, period=14)
    except Exception as e:
        logging.error(f"[ATR ERROR] {e}")
        df["atr14"] = float("nan")

    try:
        line_s, bias_s, slope_s = supertrend(df, atr_period=14, multiplier=3)
        df["supertrend_line"] = line_s
        df["supertrend_bias"] = bias_s
        df["supertrend_slope"] = slope_s
    except Exception as e:
        logging.error(f"[SUPERTREND ERROR] {e}")
        df["supertrend_line"] = float("nan")
        df["supertrend_bias"] = "NEUTRAL"
        df["supertrend_slope"] = "FLAT"

    try:
        df["rsi14"] = compute_rsi(df["close"], period=14)
    except Exception as e:
        logging.error(f"[RSI ERROR] {e}")
        df["rsi14"] = float("nan")

    try:
        df["vwap"] = calculate_typical_price_ma(df, period=20)
    except Exception as e:
        logging.debug(f"[TPMA ERROR] {e}")
        df["vwap"] = float("nan")

    last = df.iloc[-1]
    logging.info(
        f"[INDICATOR DF] {symbol} {interval} "
        f"ema9={_fmt(last['ema9'])} ema13={_fmt(last['ema13'])} "
        f"adx14={_fmt(last['adx14'])} cci20={_fmt(last['cci20'])} "
        f"rsi14={_fmt(last['rsi14'])} "
        f"supertrend_bias={last.get('supertrend_bias', '')} "
        f"slope={last.get('supertrend_slope', '')} "
        f"line={_fmt(last['supertrend_line'])} "
        f"vwap={_fmt(last.get('vwap'))}"
    )
    return df
