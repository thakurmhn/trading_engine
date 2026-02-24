# ===== signals.py (v3 — IMPROVED) =====
"""
IMPROVEMENTS IN THIS VERSION:
1. VWAP signal added as a high-value confluence indicator
2. Volume confirmation gate — avoids entries on thin/stale bars
3. Opening range breakout (ORB) detection for 9:30–9:45 setup
4. HTF/LTF confluence gate: requires 15m and 3m bias to NOT be opposite
5. Candle close-based pivot detection (uses close, not just price proximity)
6. signal_blockers counters extended for better diagnostic coverage
7. All existing bug-fixes from v2 retained
"""

import logging
import pandas as pd
import numpy as np

from config import CANDLE_BODY_RANGE, ATR_VALUE
from indicators import (
    calculate_atr,
    resolve_atr,
    daily_atr,
    momentum_ok,
    classify_cpr_width,
    williams_r,
    calculate_cci,
    compute_rsi,
)
from entry_logic import check_entry_condition

# ANSI COLORS
RESET   = "\033[0m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
RED     = "\033[91m"
MAGENTA = "\033[95m"
GRAY    = "\033[90m"
CYAN    = "\033[96m"


# ─────────────────────────────────────────────────────────────────────────────
# NORMALISE BIAS
# ─────────────────────────────────────────────────────────────────────────────
def _norm_bias(raw):
    if raw in ("BULLISH", "UP"):    return "BULLISH"
    if raw in ("BEARISH", "DOWN"):  return "BEARISH"
    return "NEUTRAL"


# ─────────────────────────────────────────────────────────────────────────────
# DIAGNOSTIC COUNTERS
# ─────────────────────────────────────────────────────────────────────────────
signal_blockers = {
    "ATR":              0,
    "SCORE_LOW":        0,
    "RSI_EXHAUSTION":   0,   # RSI oversold PUT / overbought CALL entry blocked
    "HTF_LTF_CONFLICT": 0,
    "NARROW_RANGE":     0,
    "PARTIAL_CANDLE":   0,
    "NO_SIGNAL":        0,
}


# ─────────────────────────────────────────────────────────────────────────────
# TYPICAL PRICE MA — used as VWAP reference in detect_vwap_signal
# NSE:NIFTY50-INDEX has no volume — real VWAP is meaningless.
# Rolling mean of (H+L+C)/3 over the session is the practical equivalent.
# ─────────────────────────────────────────────────────────────────────────────
def calculate_vwap(candles: pd.DataFrame, period: int = 20) -> float | None:
    """
    Returns rolling typical price MA (last value) as VWAP substitute.
    Works correctly for NSE index which has no volume.
    """
    try:
        if candles is None or len(candles) < 2:
            return None
        tp = (candles["high"] + candles["low"] + candles["close"]) / 3
        tpma = tp.rolling(period, min_periods=1).mean()
        val = tpma.iloc[-1]
        return float(val) if not pd.isna(val) else None
    except Exception as e:
        logging.debug(f"[TPMA] calc error: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# OPENING RANGE (first N bars after 9:30)
# ─────────────────────────────────────────────────────────────────────────────
def get_opening_range(candles: pd.DataFrame, n_bars: int = 5):
    """
    Returns (orb_high, orb_low) from first n_bars of the session.
    Returns (None, None) if insufficient data.
    """
    try:
        time_col = "date" if "date" in candles.columns else "time" if "time" in candles.columns else None
        if time_col:
            session_start = candles[candles[time_col].astype(str).str.contains("09:3|09:4")].head(n_bars)
            if not session_start.empty:
                return float(session_start["high"].max()), float(session_start["low"].min())
        # Fallback: use first n_bars
        opening = candles.head(n_bars)
        return float(opening["high"].max()), float(opening["low"].min())
    except Exception:
        return None, None


# ─────────────────────────────────────────────────────────────────────────────
# CANDLE RANGE FILTER — replaces volume_is_ok for volume-less NSE indices
# NSE:NIFTY50-INDEX has no volume. Range (H-L) is the proxy for activity.
# Flat/doji candles with near-zero range are exactly what we want to skip.
# ─────────────────────────────────────────────────────────────────────────────
def range_is_ok(candles: pd.DataFrame, lookback: int = 10) -> bool:
    """
    Reject entries on unusually narrow candles (flat bars, pre-open noise).
    Returns False if latest bar's range < 30% of recent average range.
    """
    try:
        rng = candles["high"] - candles["low"]
        avg_rng  = rng.tail(lookback + 1).iloc[:-1].mean()
        last_rng = rng.iloc[-1]
        if avg_rng == 0 or pd.isna(avg_rng) or pd.isna(last_rng):
            return True   # can't determine → don't block
        return last_rng >= avg_rng * 0.30
    except Exception:
        return True


# ─────────────────────────────────────────────────────────────────────────────
# CANDLE HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def candle_strength(candles_3m, side):
    last = candles_3m.iloc[-1]
    body = abs(last.close - last.open)
    rng  = last.high - last.low
    if rng == 0:
        return False, 0
    mom_ok_val, momentum = momentum_ok(candles_3m, side)
    return (body / rng) > CANDLE_BODY_RANGE and mom_ok_val, momentum


# ─────────────────────────────────────────────────────────────────────────────
# PIVOT DETECTION (unchanged from v2)
# ─────────────────────────────────────────────────────────────────────────────
def detect_cpr(last, atr, cpr_levels, call_ok, put_ok, bias=None):
    pivot, bc, tc = cpr_levels["pivot"], cpr_levels["bc"], cpr_levels["tc"]
    if call_ok and last.close > tc + 0.01 * atr:
        return "CALL", "BREAKOUT_CPR_TC"
    if put_ok and last.close < bc - 0.01 * atr:
        return "PUT", "BREAKOUT_CPR_BC"
    if call_ok and abs(last.close - tc) <= 0.5 * atr:
        return "CALL", "ACCEPTANCE_CPR_TC"
    if put_ok and abs(last.close - bc) <= 0.5 * atr:
        return "PUT", "ACCEPTANCE_CPR_BC"
    if bc < last.close < tc:
        if bias == "BULLISH" and call_ok:
            return "CALL", "CONTINUATION_CPR"
        elif bias == "BEARISH" and put_ok:
            return "PUT", "CONTINUATION_CPR"
        else:
            return "HOLD", "CONTINUATION_CPR"
    return None


def detect_camarilla(last, rng, atr, camarilla_levels, call_ok, put_ok, bias=None):
    r3, r4, s3, s4 = (camarilla_levels["r3"], camarilla_levels["r4"],
                      camarilla_levels["s3"], camarilla_levels["s4"])
    if call_ok and last.close > r4 + 0.01 * atr:
        return "CALL", "BREAKOUT_R4"
    if call_ok and last.close > r3 + 0.01 * atr:
        return "CALL", "BREAKOUT_R3"
    if put_ok and last.close < s4 - 0.01 * atr:
        return "PUT", "BREAKOUT_S4"
    if put_ok and last.close < s3 - 0.01 * atr:
        return "PUT", "BREAKOUT_S3"
    if call_ok and abs(last.close - r3) <= 0.5 * atr:
        return "CALL", "ACCEPTANCE_R3"
    if put_ok and abs(last.close - s3) <= 0.5 * atr:
        return "PUT", "ACCEPTANCE_S3"
    if s3 < last.close < r3:
        if bias == "BULLISH" and call_ok:
            return "CALL", "CONTINUATION_CAM"
        elif bias == "BEARISH" and put_ok:
            return "PUT", "CONTINUATION_CAM"
        else:
            return "HOLD", "CONTINUATION_CAM"
    if call_ok and last.low <= s3 and (last.close - last.low) > 0.2 * rng:
        return "CALL", "REJECTION_S3"
    if put_ok and last.high >= r3 and (last.high - last.close) > 0.2 * rng:
        return "PUT", "REJECTION_R3"
    return None


def detect_traditional_acceptance(last, atr, traditional_levels, call_ok, put_ok):
    r2, s2 = traditional_levels["r2"], traditional_levels["s2"]
    if call_ok and last.close > r2 + 0.01 * atr:
        return "CALL", "BREAKOUT_R2"
    if put_ok and last.close < s2 - 0.01 * atr:
        return "PUT", "BREAKOUT_S2"
    if call_ok and abs(last.close - r2) <= 0.5 * atr:
        return "CALL", "ACCEPTANCE_R2"
    if put_ok and abs(last.close - s2) <= 0.5 * atr:
        return "PUT", "ACCEPTANCE_S2"
    return None


def detect_traditional_rejection(last, rng, traditional_levels, call_ok, put_ok):
    r1, s1 = traditional_levels["r1"], traditional_levels["s1"]
    if call_ok and last.low <= s1 and (last.close - last.low) > 0.2 * rng:
        return "CALL", "REJECTION_S1"
    if put_ok and last.high >= r1 and (last.high - last.close) > 0.2 * rng:
        return "PUT", "REJECTION_R1"
    return None


def detect_traditional_continuation(last, atr, traditional_levels, call_ok, put_ok, bias=None):
    r2, s2 = traditional_levels["r2"], traditional_levels["s2"]
    if call_ok and last.low <= r2 and last.close > r2 + 0.03 * atr:
        return "CALL", "CONTINUATION_R2"
    if put_ok and last.high >= s2 and last.close < s2 - 0.03 * atr:
        return "PUT", "CONTINUATION_S2"
    return None


def detect_pivot_acceptance(last, prev, atr, traditional_levels, call_ok, put_ok):
    pivot = traditional_levels["pivot"]
    if call_ok and prev.close < pivot and last.close > pivot + 0.01 * atr:
        return "CALL", "BREAKOUT_PIVOT"
    if put_ok and prev.close > pivot and last.close < pivot - 0.01 * atr:
        return "PUT", "BREAKOUT_PIVOT"
    if call_ok and abs(last.close - pivot) <= 0.5 * atr:
        return "CALL", "ACCEPTANCE_PIVOT"
    if put_ok and abs(last.close - pivot) <= 0.5 * atr:
        return "PUT", "ACCEPTANCE_PIVOT"
    return None


def detect_pivot_rejection(last, rng, traditional_levels, call_ok, put_ok):
    pivot = traditional_levels["pivot"]
    if call_ok and last.low <= pivot and (last.close - last.low) > 0.2 * rng:
        return "CALL", "REJECTION_PIVOT"
    if put_ok and last.high >= pivot and (last.high - last.close) > 0.2 * rng:
        return "PUT", "REJECTION_PIVOT"
    return None


def detect_pivot_continuation(last, atr, traditional_levels, call_ok, put_ok, bias=None):
    pivot = traditional_levels["pivot"]
    if abs(last.close - pivot) <= 0.5 * atr:
        if bias == "BULLISH" and call_ok:
            return "CALL", "CONTINUATION_PIVOT"
        elif bias == "BEARISH" and put_ok:
            return "PUT", "CONTINUATION_PIVOT"
        else:
            return "HOLD", "CONTINUATION_PIVOT"
    return None


# ─────────────────────────────────────────────────────────────────────────────
# NEW: VWAP PIVOT DETECTION
# ─────────────────────────────────────────────────────────────────────────────
def detect_vwap_signal(last, prev, atr, vwap, call_ok, put_ok):
    """
    VWAP-based signal detection.
    - Breakout above VWAP → CALL
    - Breakdown below VWAP → PUT
    - Rejection at VWAP → counter-trend entry
    """
    if vwap is None or atr is None or atr <= 0:
        return None
    close = float(last.close)
    prev_close = float(prev.close)
    tol = 0.3 * atr

    # Breakout: prev below VWAP, current closes above
    if call_ok and prev_close < vwap and close > vwap + 0.01 * atr:
        return "CALL", "BREAKOUT_VWAP"
    # Breakdown: prev above VWAP, current closes below
    if put_ok and prev_close > vwap and close < vwap - 0.01 * atr:
        return "PUT", "BREAKDOWN_VWAP"
    # Acceptance near VWAP (close within tolerance, candle direction matches)
    if call_ok and abs(close - vwap) <= tol and close > prev_close:
        return "CALL", "ACCEPTANCE_VWAP"
    if put_ok and abs(close - vwap) <= tol and close < prev_close:
        return "PUT", "ACCEPTANCE_VWAP"
    # VWAP rejection (price tagged VWAP from below/above and reversed)
    rng = float(last.high) - float(last.low)
    if put_ok and last.high >= vwap and (last.high - close) > 0.2 * rng:
        return "PUT", "REJECTION_VWAP"
    if call_ok and last.low <= vwap and (close - last.low) > 0.2 * rng:
        return "CALL", "REJECTION_VWAP_BOUNCE"
    return None


# ─────────────────────────────────────────────────────────────────────────────
# NEW: OPENING RANGE BREAKOUT
# ─────────────────────────────────────────────────────────────────────────────
def detect_orb_signal(last, atr, orb_high, orb_low, call_ok, put_ok,
                      current_time=None):
    """
    Opening range breakout entry.
    Only valid before 11:30 IST — after that the opening range is stale.
    Fire CALL when close breaks above ORB high with confirmation.
    Fire PUT when close breaks below ORB low with confirmation.
    """
    if orb_high is None or orb_low is None or atr is None:
        return None

    # Time gate: ORB only meaningful in first 2 hours of session
    if current_time is not None:
        t = current_time.hour * 60 + current_time.minute
        if t >= 11 * 60 + 30:   # after 11:30 → stale, skip
            return None

    close = float(last.close)
    if call_ok and close > orb_high + 0.01 * atr:
        return "CALL", "ORB_BREAKOUT_HIGH"
    if put_ok and close < orb_low - 0.01 * atr:
        return "PUT", "ORB_BREAKOUT_LOW"
    return None


# ─────────────────────────────────────────────────────────────────────────────
# PULLBACK RETEST DETECTION (NEW — spec: enter on retests of breakout levels)
# ─────────────────────────────────────────────────────────────────────────────

# Module-level breakout state tracker.
# Populated by detect_signal on every confirmed breakout pivot signal.
# Reset at session start (new import) or when a trade is opened.
_breakout_state = {
    "side":  None,   # "CALL" | "PUT" | None
    "level": None,   # float — breakout pivot level (R3, R4, S3, S4, VWAP etc.)
    "bar":   -999,   # bar index when breakout fired
}


def reset_breakout_state():
    """Call at session start or after a position is opened."""
    _breakout_state["side"]  = None
    _breakout_state["level"] = None
    _breakout_state["bar"]   = -999


def _record_breakout(side: str, level: float, bar_idx: int):
    """Store the most recent breakout for pullback detection."""
    _breakout_state["side"]  = side
    _breakout_state["level"] = level
    _breakout_state["bar"]   = bar_idx
    logging.debug(
        f"[BREAKOUT_STATE] recorded {side} level={level:.1f} bar={bar_idx}"
    )


def detect_pullback_retest(last_3m, prev_3m, atr, vwap, call_ok, put_ok,
                           current_bar_idx: int = 0):
    """
    Detect a pullback retest entry after a prior breakout.

    Conditions:
      - A breakout was recorded within the last 10 bars (fresh, not stale)
      - Price has retraced to within 0.5×ATR of the breakout level
      - Current close is in the trade direction (rising for CALL, falling for PUT)
        relative to the prior bar

    Also detects VWAP pullback when a breakout side is known but price has
    pulled back to VWAP (a common Pivot Boss retest zone).

    Returns tuple (side, reason) or None.
    Reason format: "PULLBACK_RETEST_R3" | "PULLBACK_VWAP" etc.
    """
    bs  = _breakout_state["side"]
    blv = _breakout_state["level"]
    bbr = _breakout_state["bar"]

    if bs is None or blv is None:
        return None

    # Stale breakout gate: ignore if breakout was > 10 bars ago
    if current_bar_idx - bbr > 10:
        return None

    close = float(last_3m.close)
    prev_close = float(prev_3m.close)
    tol = 0.5 * atr if atr else 5.0

    if bs == "CALL" and call_ok:
        # Price retested the breakout level from above
        if (blv - tol) <= close <= (blv + tol) and close > prev_close:
            # Determine level label from known camarilla names
            return "CALL", "PULLBACK_RETEST"
        # VWAP pullback for CALL
        if vwap and abs(close - vwap) <= tol and close > prev_close:
            return "CALL", "PULLBACK_VWAP"

    if bs == "PUT" and put_ok:
        if (blv - tol) <= close <= (blv + tol) and close < prev_close:
            return "PUT", "PULLBACK_RETEST"
        if vwap and abs(close - vwap) <= tol and close < prev_close:
            return "PUT", "PULLBACK_VWAP"

    return None
def to_scalar(val):
    try:
        if val is None:
            return None
        if hasattr(val, "item"):
            return val.item()
        return float(val)
    except Exception:
        return None


def classify_volatility(atr_val, close_price=None, thresholds=(0.5, 1.0)):
    if atr_val is None:
        return "UNKNOWN"
    atr_pct = (atr_val / close_price * 100) if close_price else atr_val
    if atr_pct < thresholds[0]:
        return "LOW"
    elif atr_pct < thresholds[1]:
        return "MEDIUM"
    else:
        return "HIGH"


def dynamic_targets(entry_price, atr, side, sl_factor=1.5, pt_factor=1.0, tg_factor=2.0):
    if side == "CALL":
        return {"SL": entry_price - atr * sl_factor,
                "PT": entry_price + atr * pt_factor,
                "TG": entry_price + atr * tg_factor}
    elif side == "PUT":
        return {"SL": entry_price + atr * sl_factor,
                "PT": entry_price - atr * pt_factor,
                "TG": entry_price - atr * tg_factor}
    return {}


def signal_confidence(vol_regime, bias_score, reason):
    if vol_regime == "HIGH" and "BREAKOUT" in reason and bias_score >= 60:
        return "STRONG"
    elif vol_regime == "LOW" and "CONTINUATION" in reason and bias_score < 40:
        return "WEAK"
    elif bias_score >= 50:
        return "MEDIUM-HIGH"
    else:
        return "MEDIUM"


# ─────────────────────────────────────────────────────────────────────────────
# STATE BUILDER
# ─────────────────────────────────────────────────────────────────────────────
def _make_state(side, reason, candles_3m, atr, last, prev):
    _, momentum = momentum_ok(candles_3m, side)
    return {
        "side":           side,
        "reason":         reason,
        "entry_candle":   len(candles_3m) - 1,
        "atr_entry":      atr,
        "prev_gap":       abs(last.close - prev.close),
        "momentum":       momentum,
        "trail_updates":  0,
        "consec_count":   0,
        "peak_momentum":  abs(momentum) if momentum else 0,
        "peak_candle":    len(candles_3m) - 1,
        "plateau_count":  0,
        "partial_booked": False,
    }


# ─────────────────────────────────────────────────────────────────────────────
# BEST PIVOT FINDER
# ─────────────────────────────────────────────────────────────────────────────
def _best_pivot_for_side(last_3m, prev_3m, rng, atr,
                         cpr_levels, camarilla_levels, traditional_levels,
                         side, st_bias, vwap=None, orb_high=None, orb_low=None,
                         current_time=None, current_bar_idx=0):
    """Return best pivot signal tuple (side, reason) for the given side, or None.

    Priority order: Camarilla → CPR → VWAP → Traditional acceptance →
                    Pivot acceptance → Traditional rejection → Pivot rejection →
                    Traditional/Pivot continuation → ORB → Pullback retest

    Pullback retest is last in priority — only fires when no fresh breakout
    signal exists but a prior breakout level is being retested.
    """
    call_ok = (side == "CALL")
    put_ok  = (side == "PUT")
    candidates = [
        detect_camarilla(last_3m, rng, atr, camarilla_levels, call_ok, put_ok, st_bias),
        detect_cpr(last_3m, atr, cpr_levels, call_ok, put_ok, st_bias),
        detect_vwap_signal(last_3m, prev_3m, atr, vwap, call_ok, put_ok),
        detect_traditional_acceptance(last_3m, atr, traditional_levels, call_ok, put_ok),
        detect_pivot_acceptance(last_3m, prev_3m, atr, traditional_levels, call_ok, put_ok),
        detect_traditional_rejection(last_3m, rng, traditional_levels, call_ok, put_ok),
        detect_pivot_rejection(last_3m, rng, traditional_levels, call_ok, put_ok),
        detect_traditional_continuation(last_3m, atr, traditional_levels, call_ok, put_ok, st_bias),
        detect_pivot_continuation(last_3m, atr, traditional_levels, call_ok, put_ok, st_bias),
        detect_orb_signal(last_3m, atr, orb_high, orb_low, call_ok, put_ok, current_time),
        detect_pullback_retest(last_3m, prev_3m, atr, vwap, call_ok, put_ok, current_bar_idx),
    ]
    for res in candidates:
        if res and res[0] == side:
            return res
    return None


# ─────────────────────────────────────────────────────────────────────────────
# MAIN FUNCTION — detect_signal
# ─────────────────────────────────────────────────────────────────────────────
def detect_signal(candles_3m, candles_15m,
                  cpr_levels, camarilla_levels, traditional_levels,
                  atr=None, include_partial=False,
                  current_time=None,
                  vwap=None,
                  orb_high=None,
                  orb_low=None,
                  day_type_result=None,
                  current_bar_idx=0):
    """
    Unified signal detection — v4 (Spec-Aligned with Momentum + CPR + Pullback).

    New in v4 vs v3:
      - momentum_ok (dual-EMA + gap widening) computed and scored
      - CPR width classified and passed to entry_logic for scoring
      - Pullback retest detection (retests of R3/R4/S3/S4/VWAP after breakout)
      - Entry type classification (BREAKOUT | PULLBACK | REJECTION | CONTINUATION)
      - Breakout state tracking (_breakout_state) for pullback detection
      - current_bar_idx parameter for staleness gating of pullback signals
      - Full audit log: ST+RSI+CCI+Pivot+Momentum+CPR+EntryType
    """

    # --- Partial candle guard ---
    if "is_partial" in candles_3m.columns:
        if not include_partial and candles_3m.iloc[-1].get("is_partial", False):
            signal_blockers["PARTIAL_CANDLE"] += 1
            return None

    # --- ATR gate ---
    if atr is None or pd.isna(atr) or atr < 10 or atr > 250:
        signal_blockers["ATR"] += 1
        logging.debug(f"[SIGNAL] ATR gate: atr={atr}")
        return None

    if len(candles_3m) < 3:
        signal_blockers["NO_SIGNAL"] += 1
        return None

    last_3m = candles_3m.iloc[-1]
    prev_3m = candles_3m.iloc[-2]
    rng     = float(last_3m.high) - float(last_3m.low)

    # --- Range gate ---
    if not range_is_ok(candles_3m):
        signal_blockers["NARROW_RANGE"] += 1
        logging.debug("[SIGNAL] Blocked: narrow range candle")
        return None

    # --- 15m data ---
    has_15m  = (candles_15m is not None and not candles_15m.empty)
    last_15m = candles_15m.iloc[-1] if has_15m else pd.Series(dtype=object)

    # --- Bias normalisation ---
    raw_bias_15m = last_15m.get("supertrend_bias", "NEUTRAL") if has_15m else "NEUTRAL"
    st_bias      = _norm_bias(raw_bias_15m)
    raw_bias_3m  = last_3m.get("supertrend_bias", "NEUTRAL")
    st_bias_3m   = _norm_bias(raw_bias_3m)

    # --- HTF/LTF conflict gate ---
    if st_bias == "BEARISH" and st_bias_3m == "BEARISH":
        call_allowed, put_allowed = False, True
    elif st_bias == "BULLISH" and st_bias_3m == "BULLISH":
        call_allowed, put_allowed = True, False
    else:
        call_allowed, put_allowed = True, True

    # --- VWAP ---
    if vwap is None:
        vwap = calculate_vwap(candles_3m)

    # --- Momentum (spec: dual-EMA dual-close + gap widening) ---
    _mom_call_ok, _mom_call_gap = momentum_ok(candles_3m, "CALL")
    _mom_put_ok,  _mom_put_gap  = momentum_ok(candles_3m, "PUT")
    logging.debug(
        f"[MOMENTUM] CALL_ok={_mom_call_ok} gap={_mom_call_gap:.2f} | "
        f"PUT_ok={_mom_put_ok} gap={_mom_put_gap:.2f}"
    )

    # --- CPR width classification (day-type context) ---
    _close_price = float(last_3m.get("close", 0)) if last_3m.get("close") else None
    _cpr_width   = classify_cpr_width(cpr_levels, _close_price)
    logging.debug(f"[CPR_WIDTH] {_cpr_width} (TC={cpr_levels.get('tc','?')} BC={cpr_levels.get('bc','?')} px={_close_price})")

    logging.debug(
        f"[SIGNAL] 15m={st_bias} 3m={st_bias_3m} atr={atr:.1f} "
        f"tpma={f'{vwap:.1f}' if vwap else 'N/A'} "
        f"call_ok={call_allowed} put_ok={put_allowed} "
        f"cpr_width={_cpr_width}"
    )

    # --- Pivot signals for both sides ---
    pv_call = _best_pivot_for_side(
        last_3m, prev_3m, rng, atr,
        cpr_levels, camarilla_levels, traditional_levels,
        "CALL", st_bias, vwap=vwap, orb_high=orb_high, orb_low=orb_low,
        current_time=current_time, current_bar_idx=current_bar_idx,
    ) if call_allowed else None

    pv_put  = _best_pivot_for_side(
        last_3m, prev_3m, rng, atr,
        cpr_levels, camarilla_levels, traditional_levels,
        "PUT", st_bias, vwap=vwap, orb_high=orb_high, orb_low=orb_low,
        current_time=current_time, current_bar_idx=current_bar_idx,
    ) if put_allowed else None

    pivot_signal = pv_call or pv_put

    # --- Build indicators dict ---
    def _safe(val):
        try:
            v = float(val)
            return None if pd.isna(v) else v
        except Exception:
            return None

    # RSI prev bar for slope detection
    _rsi_prev = None
    if len(candles_3m) >= 2:
        try:
            _rsi_prev = _safe(candles_3m.iloc[-2].get("rsi14") or candles_3m.iloc[-2].get("rsi"))
        except Exception:
            pass

    # Determine momentum_ok for the tentative side (set after scoring completes)
    # Pass both so entry_logic can pick the correct one after side is chosen.
    indicators = {
        "atr":                 atr,
        "supertrend_line_3m":  _safe(last_3m.get("supertrend_line")),
        "supertrend_line_15m": _safe(last_15m.get("supertrend_line")) if has_15m else None,
        "ema_fast":            _safe(last_3m.get("ema9")),
        "ema_slow":            _safe(last_3m.get("ema13")),
        "adx":                 _safe(last_3m.get("adx14")),
        "cci":                 _safe(last_3m.get("cci20")),
        "candle_15m":          last_15m if has_15m else None,
        "st_bias_3m":          st_bias_3m,
        "st_bias_15m":         st_bias,
        "vwap":                vwap,
        "rsi_prev":            _rsi_prev,
        # NEW: momentum_ok per side — entry_logic uses these for scoring
        "momentum_ok_call":    _mom_call_ok,
        "momentum_ok_put":     _mom_put_ok,
        "momentum_gap_call":   _mom_call_gap,
        "momentum_gap_put":    _mom_put_gap,
        # NEW: CPR width for day-type bonus scoring
        "cpr_width":           _cpr_width,
    }

    # --- PA gate ---
    _pa_put_blocked  = False
    _pa_call_blocked = False
    if len(candles_3m) >= 4 and atr:
        _closes = candles_3m["close"].iloc[-4:].values.astype(float)
        _tol    = 0.15 * atr
        _d3, _d2, _d1, _d0 = _closes[-4], _closes[-3], _closes[-2], _closes[-1]
        _pa_recovering = (_d0 > _d1 + _tol) or ((_d0 > _d1 and _d0 > _d2) and _d0 > _d3 + _tol)
        _pa_declining  = (_d0 < _d1 - _tol) or ((_d0 < _d1 and _d0 < _d2) and _d0 < _d3 - _tol)
        _pa_put_blocked  = _pa_recovering
        _pa_call_blocked = _pa_declining
        if _pa_put_blocked:
            logging.info(
                f"[PA GATE] Recovering — PUT pre-blocked | "
                f"closes={_d2:.0f}→{_d1:.0f}→{_d0:.0f} tol={_tol:.1f}"
            )
        if _pa_call_blocked:
            logging.info(
                f"[PA GATE] Declining — CALL pre-blocked | "
                f"closes={_d2:.0f}→{_d1:.0f}→{_d0:.0f} tol={_tol:.1f}"
            )
        if _pa_put_blocked and not _pa_call_blocked:
            pv_call = _best_pivot_for_side(
                last_3m, prev_3m, rng, atr,
                cpr_levels, camarilla_levels, traditional_levels,
                "CALL", st_bias, vwap=vwap, orb_high=orb_high, orb_low=orb_low,
                current_time=current_time, current_bar_idx=current_bar_idx,
            ) if call_allowed else None
            pivot_signal = pv_call
        elif _pa_call_blocked and not _pa_put_blocked:
            pv_put = _best_pivot_for_side(
                last_3m, prev_3m, rng, atr,
                cpr_levels, camarilla_levels, traditional_levels,
                "PUT", st_bias, vwap=vwap, orb_high=orb_high, orb_low=orb_low,
                current_time=current_time, current_bar_idx=current_bar_idx,
            ) if put_allowed else None
            pivot_signal = pv_put
        elif _pa_put_blocked and _pa_call_blocked:
            signal_blockers["NO_SIGNAL"] += 1
            return None

    # --- Scoring engine ---
    lz_signal = check_entry_condition(
        candle=last_3m,
        indicators=indicators,
        bias_15m=st_bias,
        pivot_signal=pivot_signal,
        current_time=current_time,
        day_type_result=day_type_result,
    )

    if lz_signal["action"] not in ("BUY", "SELL"):
        reason_str = lz_signal.get("reason", "")
        if "RSI_OVERSOLD" in reason_str or "RSI_OVERBOUGHT" in reason_str:
            signal_blockers["RSI_EXHAUSTION"] += 1
        else:
            signal_blockers["SCORE_LOW"] += 1
        logging.info(
            f"[SIGNAL BLOCKED] {reason_str} | "
            f"3m={st_bias_3m} 15m={st_bias} atr={atr:.1f} "
            f"breakdown={lz_signal.get('breakdown',{})}"
        )
        return None

    side = lz_signal.get("side") or ("CALL" if lz_signal["action"] == "BUY" else "PUT")

    # PA gate enforcement
    if side == "PUT" and _pa_put_blocked:
        signal_blockers["NO_SIGNAL"] += 1
        logging.info(
            f"[SIGNAL BLOCKED] PA_GATE — scoring chose PUT but price is recovering "
            f"(put_blocked=True score={lz_signal.get('score','?')})")
        return None
    if side == "CALL" and _pa_call_blocked:
        signal_blockers["NO_SIGNAL"] += 1
        logging.info(
            f"[SIGNAL BLOCKED] PA_GATE — scoring chose CALL but price is declining "
            f"(call_blocked=True score={lz_signal.get('score','?')})")
        return None

    # Final HTF/LTF check
    if side == "CALL" and not call_allowed:
        signal_blockers["HTF_LTF_CONFLICT"] += 1
        return None
    if side == "PUT" and not put_allowed:
        signal_blockers["HTF_LTF_CONFLICT"] += 1
        return None

    # --- Entry type classification ---
    # Determines entry character for log, audit, and scoring bonus.
    # BREAKOUT    : price broke through a new level (R3/R4/S3/S4/CPR/ORB)
    # PULLBACK    : retest of a prior breakout level or VWAP
    # REJECTION   : failed breakout fade (price rejected at key level)
    # CONTINUATION: inside range but bias-aligned
    _pivot_reason = (pivot_signal[1] if pivot_signal and pivot_signal[0] == side else "")
    if "BREAKOUT" in _pivot_reason or "ORB" in _pivot_reason:
        _entry_type = "BREAKOUT"
        # Record this breakout level for future pullback detection
        _close_for_state = float(last_3m.get("close", 0))
        _record_breakout(side, _close_for_state, current_bar_idx)
    elif "PULLBACK" in _pivot_reason:
        _entry_type = "PULLBACK"
    elif "REJECTION" in _pivot_reason:
        _entry_type = "REJECTION"
    else:
        _entry_type = "CONTINUATION"

    # --- Momentum resolved for chosen side ---
    _mom_ok   = _mom_call_ok  if side == "CALL" else _mom_put_ok
    _mom_gap  = _mom_call_gap if side == "CALL" else _mom_put_gap

    # --- Build state ---
    reason = lz_signal["reason"]
    state  = _make_state(side, reason, candles_3m, atr, last_3m, prev_3m)

    if pivot_signal and pivot_signal[0] == side:
        source       = "PIVOT"
        pivot_reason = pivot_signal[1]
    else:
        source       = "LIQUIDITY_ZONE"
        pivot_reason = None

    # VWAP context
    if vwap is not None:
        close = float(last_3m.close)
        vwap_pos = "ABOVE_VWAP" if close > vwap else "BELOW_VWAP"
    else:
        vwap_pos = "VWAP_NA"

    state["source"]       = source
    state["pivot_reason"] = pivot_reason
    state["score"]        = lz_signal.get("score", 0)
    state["strength"]     = lz_signal.get("strength", "MEDIUM")
    state["zone_type"]    = lz_signal.get("zone_type")
    state["vwap_pos"]     = vwap_pos
    state["vwap"]         = vwap
    # NEW fields
    state["momentum_ok"]  = _mom_ok
    state["momentum_gap"] = _mom_gap
    state["cpr_width"]    = _cpr_width
    state["entry_type"]   = _entry_type

    # Spec-aligned audit log:
    # "Supertrend alignment + RSI + CCI + Pivot acceptance/rejection + Momentum_ok + CPR + EntryType"
    _cci_val = _safe(last_3m.get("cci20") or last_3m.get("cci"))
    _rsi_val = _safe(last_3m.get("rsi14") or last_3m.get("rsi"))
    logging.info(
        f"{GREEN}[SIGNAL FIRED] {side} "
        f"score={state['score']} strength={state['strength']} "
        f"| ST15m={st_bias} ST3m={st_bias_3m} "
        f"RSI={f'{_rsi_val:.1f}' if _rsi_val else '?'} "
        f"CCI={f'{_cci_val:.0f}' if _cci_val else '?'} "
        f"pivot={pivot_reason or 'NONE'} "
        f"momentum_ok={_mom_ok}(gap={_mom_gap:.2f}) "
        f"CPR={_cpr_width} "
        f"entry_type={_entry_type} "
        f"{vwap_pos} "
        f"| {reason}{RESET}"
    )
    return state