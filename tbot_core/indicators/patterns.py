"""Candlestick reversal pattern detection (15 patterns).

Each function takes an OHLCV DataFrame and returns:
- bool for single-bar checks (last bar matches)
- list[int] of indices for batch mode (scan_all_patterns)

All patterns are side-agnostic unless noted. Body/wick ratios use
absolute values to handle both green and red candles.
"""

import pandas as pd
import numpy as np

# ── Helpers ──────────────────────────────────────────────────────────────────

def _body(row):
    return abs(row["close"] - row["open"])

def _range(row):
    return row["high"] - row["low"]

def _upper_wick(row):
    return row["high"] - max(row["open"], row["close"])

def _lower_wick(row):
    return min(row["open"], row["close"]) - row["low"]

def _is_bullish(row):
    return row["close"] > row["open"]

def _is_bearish(row):
    return row["close"] < row["open"]


# ── 1. Doji ──────────────────────────────────────────────────────────────────

def detect_doji(df, body_pct=0.10):
    """Doji: body < body_pct of range (indecision candle).

    Returns True if the last bar is a doji.
    """
    if df is None or len(df) < 1:
        return False
    row = df.iloc[-1]
    r = _range(row)
    if r == 0:
        return True
    return _body(row) / r < body_pct


# ── 2. Hammer ────────────────────────────────────────────────────────────────

def detect_hammer(df, wick_ratio=2.0):
    """Hammer: lower wick >= wick_ratio * body, small upper wick. Bullish reversal.

    Requires prior bar to be bearish (context: at bottom of move).
    """
    if df is None or len(df) < 2:
        return False
    row = df.iloc[-1]
    prev = df.iloc[-2]
    body = _body(row)
    if body == 0:
        return False
    lower = _lower_wick(row)
    upper = _upper_wick(row)
    return (
        lower >= wick_ratio * body
        and upper <= body * 0.5
        and _is_bearish(prev)
    )


# ── 3. Inverted Hammer ──────────────────────────────────────────────────────

def detect_inverted_hammer(df, wick_ratio=2.0):
    """Inverted Hammer: upper wick >= wick_ratio * body, small lower wick. Bullish reversal."""
    if df is None or len(df) < 2:
        return False
    row = df.iloc[-1]
    prev = df.iloc[-2]
    body = _body(row)
    if body == 0:
        return False
    upper = _upper_wick(row)
    lower = _lower_wick(row)
    return (
        upper >= wick_ratio * body
        and lower <= body * 0.5
        and _is_bearish(prev)
    )


# ── 4. Shooting Star ────────────────────────────────────────────────────────

def detect_shooting_star(df, wick_ratio=2.0):
    """Shooting Star: upper wick >= wick_ratio * body at top of trend. Bearish reversal."""
    if df is None or len(df) < 2:
        return False
    row = df.iloc[-1]
    prev = df.iloc[-2]
    body = _body(row)
    if body == 0:
        return False
    upper = _upper_wick(row)
    lower = _lower_wick(row)
    return (
        upper >= wick_ratio * body
        and lower <= body * 0.5
        and _is_bullish(prev)
    )


# ── 5. Engulfing ────────────────────────────────────────────────────────────

def detect_engulfing(df, direction="BULL"):
    """Bullish/Bearish Engulfing: current body engulfs prior body.

    Parameters
    ----------
    direction : "BULL" or "BEAR"
    """
    if df is None or len(df) < 2:
        return False
    curr = df.iloc[-1]
    prev = df.iloc[-2]
    curr_body_high = max(curr["open"], curr["close"])
    curr_body_low = min(curr["open"], curr["close"])
    prev_body_high = max(prev["open"], prev["close"])
    prev_body_low = min(prev["open"], prev["close"])

    engulfs = curr_body_high > prev_body_high and curr_body_low < prev_body_low

    if direction == "BULL":
        return engulfs and _is_bullish(curr) and _is_bearish(prev)
    else:
        return engulfs and _is_bearish(curr) and _is_bullish(prev)


# ── 6. Morning Star ─────────────────────────────────────────────────────────

def detect_morning_star(df, doji_pct=0.30):
    """Morning Star: 3-bar pattern (bearish -> small body -> bullish). Bullish reversal."""
    if df is None or len(df) < 3:
        return False
    first, middle, last = df.iloc[-3], df.iloc[-2], df.iloc[-1]
    r_mid = _range(middle)
    return (
        _is_bearish(first)
        and (r_mid == 0 or _body(middle) / r_mid < doji_pct)
        and _is_bullish(last)
        and last["close"] > (first["open"] + first["close"]) / 2
    )


# ── 7. Evening Star ─────────────────────────────────────────────────────────

def detect_evening_star(df, doji_pct=0.30):
    """Evening Star: 3-bar pattern (bullish -> small body -> bearish). Bearish reversal."""
    if df is None or len(df) < 3:
        return False
    first, middle, last = df.iloc[-3], df.iloc[-2], df.iloc[-1]
    r_mid = _range(middle)
    return (
        _is_bullish(first)
        and (r_mid == 0 or _body(middle) / r_mid < doji_pct)
        and _is_bearish(last)
        and last["close"] < (first["open"] + first["close"]) / 2
    )


# ── 8. Three White Soldiers ─────────────────────────────────────────────────

def detect_three_white_soldiers(df):
    """Three White Soldiers: 3 consecutive bullish bodies, each higher close."""
    if df is None or len(df) < 3:
        return False
    bars = df.iloc[-3:]
    return all(
        _is_bullish(bars.iloc[i])
        and bars.iloc[i]["close"] > bars.iloc[i - 1]["close"]
        for i in range(1, 3)
    ) and _is_bullish(bars.iloc[0])


# ── 9. Three Black Crows ────────────────────────────────────────────────────

def detect_three_black_crows(df):
    """Three Black Crows: 3 consecutive bearish bodies, each lower close."""
    if df is None or len(df) < 3:
        return False
    bars = df.iloc[-3:]
    return all(
        _is_bearish(bars.iloc[i])
        and bars.iloc[i]["close"] < bars.iloc[i - 1]["close"]
        for i in range(1, 3)
    ) and _is_bearish(bars.iloc[0])


# ── 10. Harami ───────────────────────────────────────────────────────────────

def detect_harami(df, direction="BULL"):
    """Harami: small body inside prior body.

    BULL harami: bearish bar followed by smaller bullish bar inside it.
    BEAR harami: bullish bar followed by smaller bearish bar inside it.
    """
    if df is None or len(df) < 2:
        return False
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    prev_body_high = max(prev["open"], prev["close"])
    prev_body_low = min(prev["open"], prev["close"])
    curr_body_high = max(curr["open"], curr["close"])
    curr_body_low = min(curr["open"], curr["close"])

    inside = curr_body_high <= prev_body_high and curr_body_low >= prev_body_low
    smaller = _body(curr) < _body(prev)

    if direction == "BULL":
        return inside and smaller and _is_bearish(prev) and _is_bullish(curr)
    else:
        return inside and smaller and _is_bullish(prev) and _is_bearish(curr)


# ── 11. Piercing Line ───────────────────────────────────────────────────────

def detect_piercing_line(df):
    """Piercing Line: bearish bar then bullish bar closing above prior midpoint. Bullish."""
    if df is None or len(df) < 2:
        return False
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    midpoint = (prev["open"] + prev["close"]) / 2
    return (
        _is_bearish(prev)
        and _is_bullish(curr)
        and curr["open"] < prev["close"]
        and curr["close"] > midpoint
        and curr["close"] < prev["open"]
    )


# ── 12. Dark Cloud Cover ────────────────────────────────────────────────────

def detect_dark_cloud_cover(df):
    """Dark Cloud Cover: bullish bar then bearish bar closing below prior midpoint. Bearish."""
    if df is None or len(df) < 2:
        return False
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    midpoint = (prev["open"] + prev["close"]) / 2
    return (
        _is_bullish(prev)
        and _is_bearish(curr)
        and curr["open"] > prev["close"]
        and curr["close"] < midpoint
        and curr["close"] > prev["open"]
    )


# ── 13. Tweezer Top ─────────────────────────────────────────────────────────

def detect_tweezer_top(df, tolerance_pct=0.001):
    """Tweezer Top: 2 bars with matching highs (within tolerance). Bearish reversal."""
    if df is None or len(df) < 2:
        return False
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    avg_high = (prev["high"] + curr["high"]) / 2
    if avg_high == 0:
        return False
    return (
        abs(prev["high"] - curr["high"]) / avg_high < tolerance_pct
        and _is_bullish(prev)
        and _is_bearish(curr)
    )


# ── 14. Tweezer Bottom ──────────────────────────────────────────────────────

def detect_tweezer_bottom(df, tolerance_pct=0.001):
    """Tweezer Bottom: 2 bars with matching lows (within tolerance). Bullish reversal."""
    if df is None or len(df) < 2:
        return False
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    avg_low = (prev["low"] + curr["low"]) / 2
    if avg_low == 0:
        return False
    return (
        abs(prev["low"] - curr["low"]) / avg_low < tolerance_pct
        and _is_bearish(prev)
        and _is_bullish(curr)
    )


# ── 15. Spinning Top ────────────────────────────────────────────────────────

def detect_spinning_top(df, body_pct=0.30, wick_ratio=1.0):
    """Spinning Top: small body with roughly equal upper and lower wicks. Indecision."""
    if df is None or len(df) < 1:
        return False
    row = df.iloc[-1]
    r = _range(row)
    if r == 0:
        return False
    body = _body(row)
    upper = _upper_wick(row)
    lower = _lower_wick(row)
    return (
        body / r < body_pct
        and upper > 0
        and lower > 0
        and min(upper, lower) / max(upper, lower) > 0.5
    )


# ── 16. Marubozu ────────────────────────────────────────────────────────────

def detect_marubozu(df, wick_pct=0.05):
    """Marubozu: body is nearly the entire range (very small wicks). Strong conviction."""
    if df is None or len(df) < 1:
        return False
    row = df.iloc[-1]
    r = _range(row)
    if r == 0:
        return False
    upper = _upper_wick(row)
    lower = _lower_wick(row)
    return upper / r < wick_pct and lower / r < wick_pct


# ── Batch Scanner ────────────────────────────────────────────────────────────

def scan_all_patterns(df):
    """Scan the last bars for all 15+ patterns.

    Returns dict[str, bool] indicating which patterns are detected on the
    most recent bar(s).

    Usage:
        patterns = scan_all_patterns(df)
        if patterns["hammer"]:
            ...
    """
    if df is None or df.empty:
        return {}

    return {
        "doji": detect_doji(df),
        "hammer": detect_hammer(df),
        "inverted_hammer": detect_inverted_hammer(df),
        "shooting_star": detect_shooting_star(df),
        "engulfing_bull": detect_engulfing(df, "BULL"),
        "engulfing_bear": detect_engulfing(df, "BEAR"),
        "morning_star": detect_morning_star(df),
        "evening_star": detect_evening_star(df),
        "three_white_soldiers": detect_three_white_soldiers(df),
        "three_black_crows": detect_three_black_crows(df),
        "harami_bull": detect_harami(df, "BULL"),
        "harami_bear": detect_harami(df, "BEAR"),
        "piercing_line": detect_piercing_line(df),
        "dark_cloud_cover": detect_dark_cloud_cover(df),
        "tweezer_top": detect_tweezer_top(df),
        "tweezer_bottom": detect_tweezer_bottom(df),
        "spinning_top": detect_spinning_top(df),
        "marubozu": detect_marubozu(df),
    }
