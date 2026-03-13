"""Tick -> OHLCV candle aggregation (any timeframe).

Extracted from candle_builder.py with indicator enrichment decoupled.
"""

import logging

import pandas as pd

from tbot_core.indicators.builder import build_indicator_dataframe


def build_3min_candle(df_ticks, symbol):
    """Build 3m candles from tick data for a given symbol."""
    try:
        if df_ticks.empty or not isinstance(df_ticks, pd.DataFrame):
            logging.debug(f"[CANDLE BUILDER] No tick data for {symbol}")
            return pd.DataFrame()

        required_cols = {"timestamp", "last_price", "volume"}
        if not required_cols.issubset(df_ticks.columns):
            logging.error(f"[CANDLE BUILDER] Missing columns for {symbol}")
            return pd.DataFrame()

        df_ticks = df_ticks.copy()
        df_ticks['timestamp'] = pd.to_datetime(df_ticks['timestamp'], errors='coerce')
        df_ticks = df_ticks.dropna(subset=['timestamp'])
        if df_ticks.empty:
            return pd.DataFrame()

        df_ticks.set_index('timestamp', inplace=True)
        df_ticks['last_price'] = pd.to_numeric(df_ticks['last_price'], errors='coerce')
        df_ticks['volume'] = pd.to_numeric(df_ticks['volume'], errors='coerce').fillna(0)

        ohlcv = df_ticks['last_price'].resample('3min').ohlc()
        ohlcv['volume'] = df_ticks['volume'].resample('3min').sum()

        ohlcv["trade_date"] = ohlcv.index.date.astype(str)
        ohlcv["ist_slot"] = ohlcv.index.strftime("%H:%M:%S")
        ohlcv["symbol"] = symbol
        ohlcv["time"] = ohlcv.index.strftime("%Y-%m-%d %H:%M:%S")

        ohlcv = build_indicator_dataframe(symbol, ohlcv, interval="3m")

        logging.info(f"[CANDLE BUILDER] Built {len(ohlcv)} 3m candles for {symbol}")
        return ohlcv

    except Exception as e:
        logging.error(f"[CANDLE BUILDER ERROR] {e}")
        return pd.DataFrame(columns=[
            "trade_date", "ist_slot", "time", "open", "high", "low", "close", "volume", "symbol"
        ])


def prepare_intraday(df_intraday, target_date=None):
    """Detect time column, parse, and filter by target date."""
    if not isinstance(df_intraday, pd.DataFrame):
        logging.error("[ERROR] Input is not a DataFrame")
        return pd.DataFrame()

    df = df_intraday.copy()

    if "timestamp" in df.columns:
        df["datetime"] = pd.to_datetime(df["timestamp"], errors="coerce")
    elif "last_traded_time" in df.columns:
        df["datetime"] = pd.to_datetime(df["last_traded_time"], unit="s", errors="coerce")
    elif "ist_slot" in df.columns:
        df["datetime"] = pd.to_datetime(df["ist_slot"], errors="coerce")
    elif "ist_candle" in df.columns:
        df["datetime"] = pd.to_datetime(df["ist_candle"], errors="coerce")
    else:
        logging.error("[ERROR] No valid time column found")
        return pd.DataFrame()

    df = df.dropna(subset=["datetime"]).set_index("datetime")

    if target_date is not None:
        df = df[df.index.date == target_date]

    for col in ["ltp", "last_price", "open", "high", "low", "close", "volume", "vol_traded_today"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    return df


def resample_15m(df):
    """Resample tick/OHLCV data to 15m candles."""
    if df.empty:
        return pd.DataFrame()

    if "last_price" in df.columns:
        df_15m = df["last_price"].resample("15min").ohlc()
        if "volume" in df.columns:
            df_15m["volume"] = df["volume"].resample("15min").sum()
    elif "ltp" in df.columns:
        df_15m = df["ltp"].resample("15min").ohlc()
        if "vol_traded_today" in df.columns:
            df_15m["volume"] = df["vol_traded_today"].resample("15min").sum()
        elif "volume" in df.columns:
            df_15m["volume"] = df["volume"].resample("15min").sum()
    elif {"open", "high", "low", "close"} <= set(df.columns):
        agg_dict = {"open": "first", "high": "max", "low": "min", "close": "last"}
        if "volume" in df.columns:
            agg_dict["volume"] = "sum"
        df_15m = df.resample("15min").agg(agg_dict)
    else:
        logging.error("[ERROR] No OHLC or tick columns for resampling")
        return pd.DataFrame()

    df_15m = df_15m.dropna()
    df_15m = df_15m[(df_15m["high"] - df_15m["low"]) < 200]
    return df_15m


def resample_candles(df, timeframe):
    """Generic resampler: any source DataFrame to target Timeframe.

    Parameters
    ----------
    df : pd.DataFrame
        Source OHLCV DataFrame with DatetimeIndex.
    timeframe : Timeframe
        Target timeframe.

    Returns
    -------
    pd.DataFrame
        Resampled OHLCV.
    """
    from tbot_core.config.timeframes import Timeframe

    if df.empty:
        return pd.DataFrame()

    rule = timeframe.pandas_rule if isinstance(timeframe, Timeframe) else str(timeframe)

    agg_dict = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "volume" in df.columns:
        agg_dict["volume"] = "sum"

    result = df.resample(rule).agg(agg_dict).dropna()
    return result
