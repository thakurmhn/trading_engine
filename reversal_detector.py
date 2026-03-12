# ===== reversal_detector.py =====
"""
Intraday mean-reversion signal detector.

Detects high-probability reversal setups when:
  1. Price is stretched far from EMA9/EMA13 (≥ 1.5 × ATR).
  2. Close is aligned with a key Camarilla pivot zone:
       CALL reversals → near/below S3, S4, or S5 (support)
       PUT  reversals → near/above R3, R4, or R5 (resistance)
  3. Oscillator extremes CONFIRM the reversal:
       Oversold (CALL)  : RSI < 25  OR  CCI < −200
       Overbought (PUT) : RSI > 75  OR  CCI > +200

Additional context modifiers:
  - Startup guard  : signals suppressed before 09:20 IST (warm-up period)
  - GAP_DAY boost  : +10 pts when day_type_tag == "GAP_DAY" (gap-exhaustion
                     reversals are higher-probability setups)

Oscillator extremes are treated as CONFIRMATION signals here —
the opposite of the standard entry gate which blocks on extremes.

Output signal dict:
  side          : "CALL" | "PUT"
  reason        : human-readable evidence string
  entry_price   : float  — current close
  target        : float  — expected mean-reversion target (EMA13)
  stop          : float  — protective stop level
  score         : int    — 0–100 composite conviction
  strength      : "HIGH" | "MEDIUM" | "WEAK"
  ema9          : float
  ema13         : float
  stretch       : float  — (close − EMA9) / ATR   (negative = below EMAs)
  pivot_zone    : str    — "S5"|"S4"|"S3"|"R3"|"R4"|"R5"|"IN_RANGE"
  osc_confirmed : bool
  atr           : float
  gap_boost     : bool   — True if GAP_DAY scoring bonus applied

Integration:
  from reversal_detector import detect_reversal

  sig = detect_reversal(
      candles_3m, camarilla_levels,
      atr_value=atr,
      current_time=bar_time,
      day_type_tag="GAP_DAY",
  )
  if sig:
      # pass into entry_logic or execution as override path
"""

from __future__ import annotations

import logging
from datetime import time as dtime
from typing import Optional

import numpy as np
import pandas as pd

RESET  = "\033[0m"
CYAN   = "\033[96m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"

# ── Thresholds ────────────────────────────────────────────────────────────────
_STRETCH_THRESHOLD = 1.5      # ATR multiples to qualify as "stretched"
_STRETCH_STRONG    = 2.5      # ATR multiples for HIGH-strength signal

_RSI_OVERSOLD      = 25.0
_RSI_OVERBOUGHT    = 75.0
_CCI_OVERSOLD      = -200.0
_CCI_OVERBOUGHT    = 200.0

_PIVOT_BUFFER_ATR  = 0.30     # price must be within 0.30 × ATR of pivot level

# Startup guard: no reversal signals before this time (warm-up artefacts)
# Phase 6.4: lowered from 09:20 to 09:03 to allow opening reversal captures.
# The quality gate + EMA stretch gate still protect against warm-up noise.
_STARTUP_GUARD_TIME = dtime(9, 3)    # 09:03 IST (first valid 3m bar)

# GAP_DAY score bonus (gap exhaustion reversals)
_GAP_DAY_BONUS     = 10

# Minimum candle count to compute EMA13
_MIN_CANDLES       = 15

# Phase 6.4: Snap-back detection — price was stretched but is now reverting
_SNAPBACK_LOOKBACK = 3   # check last N bars for stretch peak
_SNAPBACK_REVERT_FRAC = 0.25  # price must have reverted ≥25% of peak stretch

# Phase 6.4: EMA stretch threshold for reversal tag logging
EMA_STRETCH_REVERSAL_THRESHOLD = 3.0


# ─────────────────────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _safe_float(val) -> Optional[float]:
    try:
        v = float(val)
        return None if (v != v) else v
    except Exception:
        return None


def _ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average, NaN-safe."""
    return series.ewm(span=period, adjust=False).mean()


def _classify_pivot_zone(
    close: float,
    r3: Optional[float],
    r4: Optional[float],
    s3: Optional[float],
    s4: Optional[float],
    atr: float,
    r5: Optional[float] = None,
    s5: Optional[float] = None,
) -> str:
    """Return the nearest Camarilla zone tag for a given close price.

    Checks R5/S5 first (most extreme levels), then R4/S4, then R3/S3.
    Returns "IN_RANGE" when price is not near any mapped level.
    """
    buf = _PIVOT_BUFFER_ATR * atr

    # ── PUT resistance zones (price approaching from below) ───────────────────
    if r5 is not None and close >= r5 - buf:
        return "R5"
    if r4 is not None and close >= r4 - buf:
        return "R4"
    if r3 is not None and close >= r3 - buf:
        return "R3"

    # ── CALL support zones (price approaching from above) ─────────────────────
    if s5 is not None and close <= s5 + buf:
        return "S5"
    if s4 is not None and close <= s4 + buf:
        return "S4"
    if s3 is not None and close <= s3 + buf:
        return "S3"

    return "IN_RANGE"


def _compute_score(
    side: str,
    stretch: float,
    pivot_zone: str,
    rsi: Optional[float],
    cci: Optional[float],
    strong_osc: bool,
    gap_day: bool = False,
) -> int:
    """Compute a 0–100 conviction score for the reversal signal.

    Components:
      EMA stretch   : 0–40 pts  (how far price is from mean)
      Pivot zone    : 0–30 pts  (quality of support/resistance level)
      Oscillator    : 0–30 pts  (confirmation from RSI/CCI extremes)
      GAP_DAY bonus : +10 pts   (gap-exhaustion context)
    """
    score = 0

    # ── EMA stretch component (0–40 pts) ──────────────────────────────────────
    abs_stretch = abs(stretch)
    if abs_stretch >= _STRETCH_STRONG:
        score += 40
    elif abs_stretch >= _STRETCH_THRESHOLD + 0.5:
        score += 30
    elif abs_stretch >= _STRETCH_THRESHOLD:
        score += 20

    # ── Pivot zone component (0–30 pts) ──────────────────────────────────────
    if side == "CALL":
        if   pivot_zone == "S5": score += 30
        elif pivot_zone == "S4": score += 30
        elif pivot_zone == "S3": score += 20
    else:  # PUT
        if   pivot_zone == "R5": score += 30
        elif pivot_zone == "R4": score += 30
        elif pivot_zone == "R3": score += 20

    # ── Oscillator extreme component (0–30 pts) ───────────────────────────────
    if strong_osc:
        score += 30
    else:
        # Partial credit for a single oscillator approaching extreme territory
        _rsi_extreme = (
            (side == "CALL" and rsi is not None and rsi < _RSI_OVERSOLD + 10)
            or (side == "PUT"  and rsi is not None and rsi > _RSI_OVERBOUGHT - 10)
        )
        _cci_extreme = (
            (side == "CALL" and cci is not None and cci < _CCI_OVERSOLD + 50)
            or (side == "PUT"  and cci is not None and cci > _CCI_OVERBOUGHT - 50)
        )
        if _rsi_extreme or _cci_extreme:
            score += 15

    # ── GAP_DAY bonus (+10 pts) ───────────────────────────────────────────────
    if gap_day:
        score += _GAP_DAY_BONUS

    return min(score, 100)


def _detect_snapback(closes_arr, ema9_arr, atr: float, side: str) -> dict:
    """Check if price was more stretched in recent bars and is now snapping back.

    Returns dict with snapback=True/False, peak_stretch, current_stretch, revert_pct.
    """
    n = min(_SNAPBACK_LOOKBACK, len(closes_arr) - 1)
    if n < 1 or atr <= 0:
        return {"snapback": False, "peak_stretch": 0, "current_stretch": 0, "revert_pct": 0}

    current_stretch = (float(closes_arr[-1]) - float(ema9_arr[-1])) / atr

    # Find peak stretch in the lookback window (excluding current bar)
    peak_stretch = current_stretch
    for j in range(1, n + 1):
        idx = len(closes_arr) - 1 - j
        if idx < 0:
            break
        s = (float(closes_arr[idx]) - float(ema9_arr[idx])) / atr
        if side == "CALL" and s < peak_stretch:
            peak_stretch = s
        elif side == "PUT" and s > peak_stretch:
            peak_stretch = s

    # Revert fraction: how much of the peak stretch has been recovered
    if abs(peak_stretch) < 0.01:
        return {"snapback": False, "peak_stretch": peak_stretch,
                "current_stretch": current_stretch, "revert_pct": 0}

    if side == "CALL":
        revert_pct = (current_stretch - peak_stretch) / abs(peak_stretch) if peak_stretch < 0 else 0
    else:
        revert_pct = (peak_stretch - current_stretch) / abs(peak_stretch) if peak_stretch > 0 else 0

    snapback = revert_pct >= _SNAPBACK_REVERT_FRAC
    return {
        "snapback": snapback,
        "peak_stretch": round(peak_stretch, 3),
        "current_stretch": round(current_stretch, 3),
        "revert_pct": round(revert_pct, 3),
    }


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────

def detect_reversal(
    candles_3m: pd.DataFrame,
    camarilla_levels: dict,
    atr_value: Optional[float] = None,
    current_time=None,
    day_type_tag: Optional[str] = None,
) -> Optional[dict]:
    """Detect a strong reversal opportunity from 3m candle data.

    Parameters
    ----------
    candles_3m:
        3m intraday candles.  Required columns: close, rsi14 (or rsi),
        cci20 (or cci), atr14 (used when atr_value is None).
    camarilla_levels:
        dict with keys r3, r4, s3, s4 (and optionally r5, s5).
    atr_value:
        Current ATR(14) on 3m bars.  Overrides the atr14 column when given.
    current_time:
        Current bar datetime/Timestamp.  Signals are suppressed before 09:20
        (startup guard).  Pass None to skip the guard.
    day_type_tag:
        Day classification string from daily_sentiment.compute_intraday_sentiment().
        "GAP_DAY" adds a +10 pt bonus (gap-exhaustion reversals).

    Returns
    -------
    Signal dict or None if conditions are not met.
    Keys: side, reason, entry_price, target, stop, score, strength,
          ema9, ema13, stretch, pivot_zone, osc_confirmed, atr, gap_boost.
    """
    # ── Startup guard (warm-up suppression) ───────────────────────────────────
    if current_time is not None:
        try:
            _bar_time = (
                current_time.time()
                if hasattr(current_time, "time")
                else current_time
            )
            if _bar_time < _STARTUP_GUARD_TIME:
                logging.debug(
                    f"[REVERSAL_DETECTOR] startup guard active "
                    f"({_bar_time} < {_STARTUP_GUARD_TIME}) — suppressed"
                )
                return None
        except Exception:
            pass

    if candles_3m is None or len(candles_3m) < _MIN_CANDLES:
        return None

    closes = candles_3m["close"].astype(float)
    last   = candles_3m.iloc[-1]

    close = _safe_float(last.get("close"))
    if close is None:
        return None

    # ── ATR ───────────────────────────────────────────────────────────────────
    atr = _safe_float(atr_value)
    if atr is None:
        atr = _safe_float(last.get("atr14"))
    if atr is None or atr <= 0:
        return None

    # ── EMA9 / EMA13 ─────────────────────────────────────────────────────────
    ema9_series  = _ema(closes, 9)
    ema13_series = _ema(closes, 13)
    ema9  = float(ema9_series.iloc[-1])
    ema13 = float(ema13_series.iloc[-1])

    # Stretch measured from EMA9 (faster, closer proxy for recent mean)
    stretch = (close - ema9) / atr    # positive = above EMAs, negative = below

    # ── Camarilla levels (including S5/R5) ────────────────────────────────────
    r3 = _safe_float(camarilla_levels.get("r3"))
    r4 = _safe_float(camarilla_levels.get("r4"))
    r5 = _safe_float(camarilla_levels.get("r5"))
    s3 = _safe_float(camarilla_levels.get("s3"))
    s4 = _safe_float(camarilla_levels.get("s4"))
    s5 = _safe_float(camarilla_levels.get("s5"))

    # ── Oscillators ───────────────────────────────────────────────────────────
    rsi = _safe_float(last.get("rsi14") or last.get("rsi"))
    cci = _safe_float(last.get("cci20") or last.get("cci"))

    # ── Determine candidate side ─────────────────────────────────────────────
    oversold_ema   = stretch <= -_STRETCH_THRESHOLD   # below EMAs → CALL reversal candidate
    overbought_ema = stretch >=  _STRETCH_THRESHOLD   # above EMAs → PUT reversal candidate

    if not oversold_ema and not overbought_ema:
        logging.debug(
            f"[REVERSAL_DETECTOR] stretch={stretch:.2f}x ATR, "
            f"threshold=±{_STRETCH_THRESHOLD} — no stretch, skipped"
        )
        return None

    side = "CALL" if oversold_ema else "PUT"

    # ── Oscillator confirmation ────────────────────────────────────────────────
    rsi_confirms_call = (rsi is not None and rsi < _RSI_OVERSOLD)
    rsi_confirms_put  = (rsi is not None and rsi > _RSI_OVERBOUGHT)
    cci_confirms_call = (cci is not None and cci < _CCI_OVERSOLD)
    cci_confirms_put  = (cci is not None and cci > _CCI_OVERBOUGHT)

    osc_confirmed = (
        (side == "CALL" and (rsi_confirms_call or cci_confirms_call))
        or (side == "PUT"  and (rsi_confirms_put  or cci_confirms_put))
    )

    # ── Pivot zone alignment ──────────────────────────────────────────────────
    pivot_zone = _classify_pivot_zone(close, r3, r4, s3, s4, atr, r5=r5, s5=s5)

    # Support zones for CALL; resistance zones for PUT.
    # IN_RANGE is allowed but will score 0 pts on the pivot component.
    if side == "CALL" and pivot_zone not in ("S3", "S4", "S5", "IN_RANGE"):
        logging.debug(
            f"[REVERSAL_DETECTOR] CALL candidate pivot_zone={pivot_zone} "
            "not near support (R zone) — skipped"
        )
        return None
    if side == "PUT" and pivot_zone not in ("R3", "R4", "R5", "IN_RANGE"):
        logging.debug(
            f"[REVERSAL_DETECTOR] PUT candidate pivot_zone={pivot_zone} "
            "not near resistance (S zone) — skipped"
        )
        return None

    # ── GAP_DAY context ───────────────────────────────────────────────────────
    gap_day = (day_type_tag == "GAP_DAY")

    # ── Phase 6.4: Snap-back detection ──────────────────────────────────────
    _closes_arr = closes.values
    _ema9_arr = ema9_series.values
    snapback_info = _detect_snapback(_closes_arr, _ema9_arr, atr, side)

    # ── Score ─────────────────────────────────────────────────────────────────
    strong_osc = (
        (side == "CALL" and (rsi_confirms_call or cci_confirms_call))
        or (side == "PUT"  and (rsi_confirms_put  or cci_confirms_put))
    )
    score = _compute_score(side, stretch, pivot_zone, rsi, cci, strong_osc, gap_day=gap_day)

    # Phase 6.4: Snap-back bonus (+5 pts) — price was more stretched recently
    # and is now reverting toward mean, increasing reversal probability.
    if snapback_info["snapback"]:
        score = min(score + 5, 100)

    # Require minimum conviction
    if score < 35:
        logging.debug(
            f"[REVERSAL_DETECTOR] score={score} < minimum 35 — signal suppressed"
        )
        return None

    # ── Strength tier ─────────────────────────────────────────────────────────
    if score >= 75:
        strength = "HIGH"
    elif score >= 50:
        strength = "MEDIUM"
    else:
        strength = "WEAK"

    # ── Target and Stop ───────────────────────────────────────────────────────
    # Mean-reversion target: EMA13 (where price should revert to)
    # Stop: 1.0 × ATR beyond the extreme
    target = round(ema13, 2)
    stop   = round(close - atr, 2) if side == "CALL" else round(close + atr, 2)

    # ── Reason string ─────────────────────────────────────────────────────────
    osc_parts = []
    if rsi is not None:
        osc_parts.append(f"RSI={rsi:.1f}")
    if cci is not None:
        osc_parts.append(f"CCI={cci:.0f}")
    osc_str = " ".join(osc_parts) if osc_parts else "OSC=N/A"

    gap_note = " GAP_DAY_BOOST" if gap_day else ""
    osc_context = "ZoneB-Reversal" if pivot_zone in ("R3", "R4", "S3", "S4") else "ZoneC-Continuation"
    reason = (
        f"{side} reversal: stretch={stretch:.2f}x ATR "
        f"ema9={ema9:.1f} ema13={ema13:.1f} "
        f"pivot_zone={pivot_zone} {osc_str} "
        f"osc_confirmed={osc_confirmed} score={score} strength={strength} "
        f"osc_context={osc_context}{gap_note}"
    )

    # Phase 6.4 attribution flags
    _pivot_confirmed = pivot_zone in ("S3", "S4", "S5", "R3", "R4", "R5")

    signal = {
        "side":          side,
        "reason":        reason,
        "entry_price":   close,
        "target":        target,
        "stop":          stop,
        "score":         score,
        "strength":      strength,
        "ema9":          round(ema9, 2),
        "ema13":         round(ema13, 2),
        "stretch":       round(stretch, 3),
        "pivot_zone":    pivot_zone,
        "osc_confirmed": osc_confirmed,
        "atr":           round(atr, 2),
        "gap_boost":     gap_day,
        "osc_context":   osc_context,
        # Phase 6.4 fields
        "pivot_confirmed": _pivot_confirmed,
        "snapback":        snapback_info["snapback"],
        "snapback_info":   snapback_info,
    }

    _color = GREEN if strength == "HIGH" else YELLOW
    logging.info(
        f"{_color}[REVERSAL_SIGNAL] {side} score={score} strength={strength} "
        f"stretch={stretch:.2f}x ATR pivot={pivot_zone} "
        f"osc_confirmed={osc_confirmed} {osc_str} "
        f"ema9={ema9:.1f} ema13={ema13:.1f} close={close:.1f} "
        f"target={target:.1f} stop={stop:.1f} "
        f"osc_context={osc_context}"
        f"{' gap_boost=True' if gap_day else ''}"
        f"{' snapback=True' if snapback_info['snapback'] else ''}{RESET}"
    )

    # Phase 6.4 attribution tags
    if abs(stretch) >= EMA_STRETCH_REVERSAL_THRESHOLD:
        logging.info(
            f"[REVERSAL_EMA_STRETCH] side={side} stretch={stretch:.2f}x "
            f"snapback={snapback_info['snapback']} "
            f"peak_stretch={snapback_info['peak_stretch']} "
            f"revert_pct={snapback_info['revert_pct']}"
        )
    if _pivot_confirmed:
        logging.info(
            f"[REVERSAL_PIVOT_CONFIRM] side={side} pivot_zone={pivot_zone} "
            f"score_boost=included"
        )
    if osc_confirmed:
        logging.info(
            f"[REVERSAL_OSC_CONFIRM] side={side} "
            f"RSI={rsi:.1f if rsi is not None else 'N/A'} "
            f"CCI={cci:.0f if cci is not None else 'N/A'} "
            f"reason=Oscillator extreme treated as confirmation"
        )

    return signal


def get_reversal_signal(
    candles_3m: pd.DataFrame,
    camarilla_levels: dict,
    atr_value: Optional[float] = None,
    current_time=None,
    day_type_tag: Optional[str] = None,
) -> Optional[dict]:
    """Public alias for detect_reversal — same semantics."""
    return detect_reversal(
        candles_3m, camarilla_levels,
        atr_value=atr_value,
        current_time=current_time,
        day_type_tag=day_type_tag,
    )


__all__ = [
    "detect_reversal",
    "get_reversal_signal",
]
