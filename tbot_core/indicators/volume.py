"""Volume indicators: VWAP (typical price MA proxy)."""

import logging

import numpy as np
import pandas as pd


def calculate_typical_price_ma(df, period=20):
    """Rolling mean of typical price (H+L+C)/3.

    VWAP substitute for volume-less instruments like NSE:NIFTY50-INDEX.
    """
    try:
        tp = (df["high"] + df["low"] + df["close"]) / 3
        return tp.rolling(period, min_periods=1).mean()
    except Exception as e:
        logging.debug(f"[TPMA ERROR] {e}")
        return pd.Series([np.nan] * len(df), index=df.index)
