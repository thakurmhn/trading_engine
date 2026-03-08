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
    williams_r,
    calculate_cci,
    compute_rsi,
    classify_cpr_width,
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


def _safe_float(val, default=None):
    """Safely convert value to float, returning default if not possible."""
    try:
        if val is None:
            return default
        f = float(val)
        return f if pd.notna(f) else default
    except (ValueError, TypeError):
        return default


def compute_tilt_state(close_price, cpr_levels, camarilla_levels):
    """Compute market tilt state from current price vs CPR and Camarilla levels.

    Returns:
        str: "BULLISH_TILT", "BEARISH_TILT", or "NEUTRAL"
    """
    if close_price is None or not np.isfinite(close_price):
        return "NEUTRAL"
    tc = _safe_float((cpr_levels or {}).get("tc"))
    bc = _safe_float((cpr_levels or {}).get("bc"))
    r3 = _safe_float((camarilla_levels or {}).get("r3"))
    s3 = _safe_float((camarilla_levels or {}).get("s3"))
    if tc is None or bc is None or r3 is None or s3 is None:
        return "NEUTRAL"
    # BULLISH_TILT: price above CPR top AND above R3
    if close_price > tc and close_price > r3:
        return "BULLISH_TILT"
    # BEARISH_TILT: price below CPR bottom AND below S3
    if close_price < bc and close_price < s3:
        return "BEARISH_TILT"
    return "NEUTRAL"


# ─────────────────────────────────────────────────────────────────────────────
# DIAGNOSTIC COUNTERS
# ─────────────────────────────────────────────────────────────────────────────
signal_blockers = {
    "ATR":              0,
    "SCORE_LOW":        0,
    "HTF_LTF_CONFLICT": 0,
    "NARROW_RANGE":     0,   # replaces LOW_VOLUME — index has no volume
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
    Only valid before 11:45 IST — after that the opening range is stale.
    Fire CALL when close breaks above ORB high with confirmation.
    Fire PUT when close breaks below ORB low with confirmation.
    """
    if orb_high is None or orb_low is None or atr is None:
        return None

    # Time gate: ORB remains active until 11:45 IST.
    if current_time is not None:
        t = current_time.hour * 60 + current_time.minute
        if t >= 11 * 60 + 45:
            logging.info(
                f"[ORB_EXPIRED] time={current_time.hour:02d}:{current_time.minute:02d} "
                "window_end=11:45"
            )
            return None
        logging.info(
            f"[ORB_ACTIVE] time={current_time.hour:02d}:{current_time.minute:02d} "
            "window_end=11:45"
        )

    close = float(last.close)
    if call_ok and close > orb_high + 0.01 * atr:
        return "CALL", "ORB_BREAKOUT_HIGH"
    if put_ok and close < orb_low - 0.01 * atr:
        return "PUT", "ORB_BREAKOUT_LOW"
    return None


# ─────────────────────────────────────────────────────────────────────────────
# UTILITY FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────
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
                         current_time=None):
    """Return best pivot signal tuple (side, reason) for the given side, or None."""
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
                  vwap=None,          # pass VWAP from paper_order / live_order
                  orb_high=None,      # opening range high
                  orb_low=None,       # opening range low
                  day_type_result=None,  # NEW: DayTypeResult for threshold modifier
                  osc_relief_active=False,  # NEW: S4/R4 relief override from gate
                  zone_signal=None,      # Phase 4A: zone_detector output
                  pulse_metrics=None,    # Phase 4B: pulse_module metrics dict
                  daily_camarilla_levels=None):  # Fixed daily S4/R4 for RSI bypass
    """
    Unified signal detection with VWAP, ORB, and volume confirmation.

    IMPROVEMENTS vs v2:
      - VWAP signals added as high-value pivot source
      - ORB breakout support
      - Volume confirmation (low volume bars skipped)
      - HTF/LTF anti-conflict gate (won't buy CALL if 15m=DOWN and 3m=DOWN)
      - Blockers now categorised for better diagnostics
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

    # --- Range gate (replaces volume gate — NSE index has no volume) ---
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
    # Don't enter CALL if both 15m and 3m are bearish (avoid fighting trend)
    # Don't enter PUT if both 15m and 3m are bullish
    if st_bias == "BEARISH" and st_bias_3m == "BEARISH":
        # Allow PUT entries only
        call_allowed, put_allowed = False, True
    elif st_bias == "BULLISH" and st_bias_3m == "BULLISH":
        call_allowed, put_allowed = True, False
    else:
        call_allowed, put_allowed = True, True  # Mixed/neutral → allow both

    # Auto-compute VWAP from candles if not passed externally
    if vwap is None:
        vwap = calculate_vwap(candles_3m)

    logging.debug(
        f"[SIGNAL] 15m={st_bias} 3m={st_bias_3m} atr={atr:.1f} "
        f"tpma={f'{vwap:.1f}' if vwap else 'N/A'} "
        f"call_ok={call_allowed} put_ok={put_allowed}"
    )

    # --- Pivot signals for both sides ---
    pv_call = _best_pivot_for_side(
        last_3m, prev_3m, rng, atr,
        cpr_levels, camarilla_levels, traditional_levels,
        "CALL", st_bias, vwap=vwap, orb_high=orb_high, orb_low=orb_low,
        current_time=current_time,
    ) if call_allowed else None

    pv_put  = _best_pivot_for_side(
        last_3m, prev_3m, rng, atr,
        cpr_levels, camarilla_levels, traditional_levels,
        "PUT", st_bias, vwap=vwap, orb_high=orb_high, orb_low=orb_low,
        current_time=current_time,
    ) if put_allowed else None

    pivot_signal = pv_call or pv_put

    # --- Compute missing indicators (v5 fix) ───────────────────────────────
    # These were being called for candle_strength but not passed to check_entry_condition()
    # Restores 15-25 pts of scoring capability

    # 1. Momentum_ok for both sides (15 pts each if True)
    mom_ok_call, _ = momentum_ok(candles_3m, "CALL")
    mom_ok_put,  _ = momentum_ok(candles_3m, "PUT")

    # 2. CPR width classification (NARROW = +5 pts bonus)
    cpr_width = classify_cpr_width(cpr_levels, float(last_3m.get("close", 0)))

    # 3. Entry type from pivot reason (PULLBACK/REJECTION = +5 pts bonus)
    entry_type = "CONTINUATION"  # default
    if pivot_signal and len(pivot_signal) > 1:
        reason = pivot_signal[1].upper()
        if "BREAKOUT" in reason:
            entry_type = "BREAKOUT"
        elif "PULLBACK" in reason:
            entry_type = "PULLBACK"
        elif "REJECTION" in reason:
            entry_type = "REJECTION"
        elif "ACCEPTANCE" in reason:
            entry_type = "ACCEPTANCE"
        else:
            entry_type = "CONTINUATION"

    # 4. RSI previous value (for slope +2 bonus)
    rsi_prev = None
    if len(candles_3m) >= 2:
        try:
            prev_rsi_val = float(candles_3m.iloc[-2].get("rsi14") or candles_3m.iloc[-2].get("rsi"))
            if not pd.isna(prev_rsi_val):
                rsi_prev = prev_rsi_val
        except Exception:
            pass

    # 5. Previous bar close values (Phase 6: bar-close alignment)
    close_prev_3m = None
    if len(candles_3m) >= 2:
        try:
            _cpv = float(candles_3m.iloc[-2]["close"])
            if not pd.isna(_cpv):
                close_prev_3m = _cpv
        except Exception:
            pass
    close_prev_15m = None
    if has_15m and len(candles_15m) >= 2:
        try:
            _cpv15 = float(candles_15m.iloc[-2]["close"])
            if not pd.isna(_cpv15):
                close_prev_15m = _cpv15
        except Exception:
            pass

    # --- Build indicators dict ---
    def _safe(val):
        try:
            v = float(val)
            return None if pd.isna(v) else v
        except Exception:
            return None

    indicators = {
        # Original indicators
        "atr":                 atr,
        "supertrend_line_3m":  _safe(last_3m.get("supertrend_line")),
        "supertrend_line_15m": _safe(last_15m.get("supertrend_line")) if has_15m else None,
        "ema_fast":            _safe(last_3m.get("ema9")),
        "ema_slow":            _safe(last_3m.get("ema13")),
        "adx":                 _safe(last_3m.get("adx14")),
        "cci":                 _safe(last_3m.get("cci20")),
        "candle_15m":          last_15m if has_15m else None,
        "st_bias_3m":          st_bias_3m,
        "vwap":                vwap,
        
        # NEW (v5 fix): Missing indicators restored
        "momentum_ok_call":    mom_ok_call,
        "momentum_ok_put":     mom_ok_put,
        "cpr_width":           cpr_width,
        "entry_type":          entry_type,
        "rsi_prev":            rsi_prev,
        # Phase 6: bar-close alignment inputs
        "close_prev_3m":       close_prev_3m,
        "close_prev_15m":      close_prev_15m,
    }

    logging.debug(
        f"[INDICATORS BUILT] MOM_CALL={mom_ok_call} MOM_PUT={mom_ok_put} "
        f"CPR={cpr_width} ET={entry_type} RSI_prev={rsi_prev}"
    )

    # --- Scoring engine ---
    lz_signal = check_entry_condition(
        candle=last_3m,
        indicators=indicators,
        bias_15m=st_bias,
        pivot_signal=pivot_signal,
        current_time=current_time,
        day_type_result=day_type_result,
        osc_relief_active=osc_relief_active,
        zone_signal=zone_signal,
        pulse_metrics=pulse_metrics,
        daily_camarilla_levels=daily_camarilla_levels,
    )

    # ── [SIGNAL CHECK] — emitted for every bar regardless of outcome ──────────
    _sc  = lz_signal.get("score",     0)  or 0
    _thr = lz_signal.get("threshold", 50) or 50
    _bd  = lz_signal.get("breakdown", {})
    _gap = _thr - _sc
    logging.info(
        f"[SIGNAL CHECK] bar={len(candles_3m)} "
        f"side={lz_signal.get('side','?')} "
        f"score={_sc}/{_thr} gap={_gap:+.0f} "
        f"ST15m={st_bias} ST3m={st_bias_3m} "
        f"atr={atr:.1f} pivot={pivot_signal} "
        f"breakdown={_bd}"
    )

    if lz_signal["action"] not in ("BUY", "SELL"):
        reason_str = lz_signal.get("reason", "")
        if "RSI_OVERSOLD" in reason_str or "RSI_OVERBOUGHT" in reason_str:
            signal_blockers["RSI_EXHAUSTION"] = signal_blockers.get("RSI_EXHAUSTION", 0) + 1
        else:
            signal_blockers["SCORE_LOW"] += 1

        # Near-miss (within 15 pts of threshold) → INFO so the trader can see why
        _log = logging.info if _gap <= 15 else logging.debug
        _log(
            f"[SIGNAL BLOCKED] {reason_str} | "
            f"3m={st_bias_3m} 15m={st_bias} atr={atr:.1f} "
            f"MOM_CALL={indicators.get('momentum_ok_call')} MOM_PUT={indicators.get('momentum_ok_put')} "
            f"breakdown={_bd}"
        )
        return None

    side = lz_signal.get("side") or ("CALL" if lz_signal["action"] == "BUY" else "PUT")

    # Final HTF/LTF check on chosen side
    if side == "CALL" and not call_allowed:
        signal_blockers["HTF_LTF_CONFLICT"] += 1
        return None
    if side == "PUT" and not put_allowed:
        signal_blockers["HTF_LTF_CONFLICT"] += 1
        return None

    reason = lz_signal["reason"]
    state  = _make_state(side, reason, candles_3m, atr, last_3m, prev_3m)

    if pivot_signal and pivot_signal[0] == side:
        source       = "PIVOT"
        pivot_reason = pivot_signal[1]
    else:
        source       = "LIQUIDITY_ZONE"
        pivot_reason = None

    # VWAP context tag
    if vwap is not None:
        close = float(last_3m.close)
        vwap_pos = "ABOVE_VWAP" if close > vwap else "BELOW_VWAP"
    else:
        vwap_pos = "VWAP_NA"

    state["source"]       = source
    state["pivot_reason"] = pivot_reason
    state["score"]        = lz_signal.get("score", 0)
    state["threshold"]    = lz_signal.get("threshold", 50)
    state["breakdown"]    = lz_signal.get("breakdown", {})  # v6: entry score breakdown
    state["strength"]     = lz_signal.get("strength", "MEDIUM")
    state["zone_type"]    = lz_signal.get("zone_type")
    state["vwap_pos"]     = vwap_pos
    state["vwap"]         = vwap
    
    # Add indicator snapshots for side decision audit trail
    state["st_bias"]      = st_bias_3m      # 3m supertrend bias
    state["st_bias_15m"]  = st_bias         # 15m supertrend bias
    state["rsi"]          = _safe_float(last_3m.get("rsi14") or last_3m.get("rsi")) or "?"
    state["cci"]          = _safe_float(last_3m.get("cci20") or last_3m.get("cci")) or "?"

    # Phase 6: Bias alignment attribution
    _bd = lz_signal.get("breakdown", {})
    _bias_align = _bd.get("_bar_align_status", "NEUTRAL")
    _bias_tf = _bd.get("_bar_align_tf")
    state["bias_alignment"] = _bias_align
    state["bias_alignment_tf"] = _bias_tf
    state["spread_noise"] = _bd.get("_spread_noise", False)
    _ba_tag = f" [BIAS_ALIGNMENT]{_bias_align}"
    if _bias_tf:
        _ba_tag += f" TF={_bias_tf}"
    logging.info(
        f"[BIAS_ALIGNMENT] side={side} status={_bias_align} "
        f"tf={_bias_tf or 'NONE'} "
        f"bias15m={st_bias} bias3m={st_bias_3m}"
    )

    # Phase 6.1: Tilt state
    _close_for_tilt = float(last_3m.get("close", 0))
    _tilt_state = compute_tilt_state(_close_for_tilt, cpr_levels, camarilla_levels)
    state["tilt_state"] = _tilt_state
    logging.info(f"[TILT_STATE={_tilt_state}] side={side} close={_close_for_tilt:.2f}")

    logging.info(
        f"{GREEN}[SIGNAL FIRED] {side} source={source} "
        f"score={state['score']} strength={state['strength']} "
        f"bias15m={st_bias} bias3m={st_bias_3m} "
        f"pivot={pivot_reason} {vwap_pos}{_ba_tag} | {reason}{RESET}"
    )
    return state
