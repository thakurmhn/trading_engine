"""Trend indicators: Supertrend, EMA, ADX."""

import logging

import numpy as np
import pandas as pd


def supertrend(df, atr_period=14, multiplier=3, atr_val=None, slope_lookback=5):
    """Supertrend with correct bias reconciliation.

    Parameters
    ----------
    df : pd.DataFrame
        OHLCV DataFrame with 'high', 'low', 'close' columns.
    atr_period : int
        ATR lookback period.
    multiplier : float
        ATR multiplier for band width.
    atr_val : float or None
        Optional ATR override (scalar or Series).
    slope_lookback : int
        Bars to look back for slope calculation.

    Returns
    -------
    tuple[pd.Series, pd.Series, pd.Series]
        (line_series, bias_series, slope_series)
        bias: "UP" | "DOWN" | "NEUTRAL"
        slope: "UP" | "DOWN" | "FLAT"
    """
    if df is None or df.empty:
        idx = df.index if isinstance(df, pd.DataFrame) else pd.Index([])
        return (
            pd.Series(index=idx, dtype=float),
            pd.Series(index=idx, dtype=object),
            pd.Series(index=idx, dtype=object),
        )

    df = df.copy()

    df['H-L'] = df['high'] - df['low']
    df['H-C'] = abs(df['high'] - df['close'].shift())
    df['L-C'] = abs(df['low'] - df['close'].shift())
    df['TR'] = df[['H-L', 'H-C', 'L-C']].max(axis=1)

    if atr_val is None or (isinstance(atr_val, float) and pd.isna(atr_val)):
        df['ATR'] = df['TR'].rolling(atr_period).mean()
    else:
        try:
            if np.isscalar(atr_val):
                df['ATR'] = float(atr_val)
            else:
                atr_series = pd.Series(atr_val, index=df.index, dtype=float)
                df['ATR'] = atr_series.reindex(df.index).ffill()
            logging.debug("[SUPERTREND] Using ATR override")
        except Exception:
            logging.warning("[SUPERTREND] Invalid ATR override, falling back to rolling ATR")
            df['ATR'] = df['TR'].rolling(atr_period).mean()

    hl2 = (df['high'] + df['low']) / 2
    df['upperband'] = hl2 + multiplier * df['ATR']
    df['lowerband'] = hl2 - multiplier * df['ATR']

    df['final_upperband'] = df['upperband'].copy()
    df['final_lowerband'] = df['lowerband'].copy()

    start_idx = max(1, atr_period)
    for i in range(start_idx, len(df)):
        prev_ub = df['final_upperband'].iloc[i - 1]
        prev_lb = df['final_lowerband'].iloc[i - 1]
        prev_close = df['close'].iloc[i - 1]

        if prev_close <= prev_ub:
            df.loc[df.index[i], 'final_upperband'] = min(df['upperband'].iloc[i], prev_ub)
        else:
            df.loc[df.index[i], 'final_upperband'] = df['upperband'].iloc[i]

        if prev_close >= prev_lb:
            df.loc[df.index[i], 'final_lowerband'] = max(df['lowerband'].iloc[i], prev_lb)
        else:
            df.loc[df.index[i], 'final_lowerband'] = df['lowerband'].iloc[i]

    line = pd.Series(index=df.index, dtype=float)
    bias = pd.Series(index=df.index, dtype=object)
    slope = pd.Series(index=df.index, dtype=object)

    for i in range(start_idx, len(df)):
        close_i = df['close'].iloc[i]
        prev_ub = df['final_upperband'].iloc[i - 1]
        prev_lb = df['final_lowerband'].iloc[i - 1]
        curr_lb = df['final_lowerband'].iloc[i]
        curr_ub = df['final_upperband'].iloc[i]

        if close_i > prev_ub:
            line.iloc[i] = curr_lb
            bias.iloc[i] = "UP"
        elif close_i < prev_lb:
            line.iloc[i] = curr_ub
            bias.iloc[i] = "DOWN"
        else:
            prev_bias = bias.iloc[i - 1] if i > start_idx else "NEUTRAL"
            prev_line = line.iloc[i - 1] if i > start_idx else float('nan')
            if prev_bias == "UP":
                line.iloc[i] = curr_lb
                bias.iloc[i] = "UP"
            elif prev_bias == "DOWN":
                line.iloc[i] = curr_ub
                bias.iloc[i] = "DOWN"
            else:
                line.iloc[i] = prev_line
                bias.iloc[i] = "NEUTRAL"

        if i >= (start_idx + max(1, slope_lookback)):
            prev_line = line.iloc[i - max(1, slope_lookback)]
            curr_line = line.iloc[i]
            if pd.isna(prev_line) or pd.isna(curr_line):
                slope.iloc[i] = slope.iloc[i - 1] if i > 0 else "FLAT"
            elif curr_line > prev_line:
                slope.iloc[i] = "UP"
            elif curr_line < prev_line:
                slope.iloc[i] = "DOWN"
            else:
                slope.iloc[i] = "FLAT"
        else:
            slope.iloc[i] = "FLAT"

    # Reconcile bias against line position
    corrected = 0
    for i in range(start_idx, len(df)):
        cl = line.iloc[i]
        cc = df['close'].iloc[i]
        if pd.isna(cl):
            continue
        if cc > cl and bias.iloc[i] != "UP":
            bias.iloc[i] = "UP"
            corrected += 1
        elif cc < cl and bias.iloc[i] != "DOWN":
            bias.iloc[i] = "DOWN"
            corrected += 1

    if corrected > 0:
        logging.info(f"[SUPERTREND] Bias corrected {corrected} rows")

    if len(df) > 0:
        last_atr = df['ATR'].iloc[-1] if "ATR" in df.columns else float("nan")
        logging.debug(
            f"[SUPERTREND] bias={bias.iloc[-1]} slope={slope.iloc[-1]} "
            f"atr={last_atr:.2f} corrected={corrected}"
        )

    return line, bias, slope


def calculate_ema(df, period=20, column="close"):
    """Exponential Moving Average (EMA) from a DataFrame column."""
    if df is None or df.empty or column not in df.columns:
        logging.error("[EMA] Empty or invalid DataFrame")
        return pd.Series(dtype=float, index=df.index if df is not None else None)

    series = df[column].dropna()
    return series.ewm(span=period, adjust=False).mean()


def calculate_adx(df, period=14):
    """Average Directional Index (ADX) using Wilder EWM smoothing.

    Uses alpha=1/period instead of rolling sum x2. Needs period+1 bars
    (vs 2*period with rolling). Matches TradingView/Bloomberg.
    """
    if not {"high", "low", "close"}.issubset(df.columns):
        logging.error("[ADX] Missing required columns")
        return pd.Series(dtype=float, index=df.index)

    if len(df) < period + 1:
        return pd.Series([float("nan")] * len(df), index=df.index)

    df = df.copy()
    alpha = 1.0 / period

    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift(1)).abs(),
        (df["low"] - df["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)

    up = df["high"].diff()
    dn = (-df["low"].diff())
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)

    tr_s = tr.ewm(alpha=alpha, adjust=False).mean()
    pdm_s = plus_dm.ewm(alpha=alpha, adjust=False).mean()
    mdm_s = minus_dm.ewm(alpha=alpha, adjust=False).mean()

    plus_di = 100 * pdm_s / tr_s.replace(0, np.nan)
    minus_di = 100 * mdm_s / tr_s.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=alpha, adjust=False).mean()
    adx.iloc[:period] = float("nan")
    return adx


def ema_bias(df, period=20):
    """EMA bias: compares last close vs EMA."""
    if df is None or df.empty or "close" not in df.columns:
        return "NEUTRAL"
    ema_val = df["close"].ewm(span=period).mean()
    if ema_val.empty or pd.isna(ema_val.iloc[-1]):
        return "NEUTRAL"
    last_close = df["close"].iloc[-1]
    return "BULLISH" if last_close > ema_val.iloc[-1] else "BEARISH"


def adx_bias(df, period=14, threshold=20):
    """ADX bias: compares +DI vs -DI with ADX strength check."""
    if df is None or df.empty or not {"high", "low", "close"}.issubset(df.columns):
        logging.warning("[ADX BIAS] No data")
        return "NEUTRAL"

    high, low, close = df["high"], df["low"], df["close"]

    plus_dm = high.diff()
    minus_dm = low.diff().abs()
    plus_dm[plus_dm < 0] = 0
    minus_dm[minus_dm < 0] = 0

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)

    atr_val = tr.rolling(period).mean().dropna()
    if atr_val.empty:
        logging.warning("[ADX BIAS] ATR unavailable")
        return "NEUTRAL"

    plus_di = 100 * (plus_dm.rolling(period).mean() / atr_val)
    minus_di = 100 * (minus_dm.rolling(period).mean() / atr_val)
    adx_val = 100 * (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan)).rolling(period).mean()

    if adx_val.empty or pd.isna(adx_val.iloc[-1]):
        logging.warning("[ADX BIAS] ADX unavailable")
        return "NEUTRAL"

    adx_last = adx_val.iloc[-1]
    logging.debug(f"[ADX BIAS] +DI={plus_di.iloc[-1]:.2f}, -DI={minus_di.iloc[-1]:.2f}, ADX={adx_last:.2f}")

    if adx_last < threshold:
        return "NEUTRAL"
    return "BULLISH" if plus_di.iloc[-1] > minus_di.iloc[-1] else "BEARISH"
