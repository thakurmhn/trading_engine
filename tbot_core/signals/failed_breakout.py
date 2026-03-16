"""Failed breakout detector for pivot rejections.

This module detects short-horizon failed breakouts around Camarilla pivots.
It is designed as an additive governance layer and does not place orders.

Signal logic:
- Price crosses R3/R4 and reverts back below within 2-3 bars -> PUT reversal.
- Price crosses S3/S4 and reverts back above within 2-3 bars -> CALL reversal.
- Confirmation requires:
  1) oscillator extreme in reversal direction, and
  2) EMA stretch measured in ATR units.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd


def _safe_float(v, default=float("nan")) -> float:
    try:
        x = float(v)
        return x if np.isfinite(x) else default
    except Exception:
        return default


def detect_failed_breakout(
    candles_3m: pd.DataFrame,
    camarilla_levels: dict,
    lookback_bars: int = 3,
    osc_rsi_extreme: float = 70.0,
    osc_cci_extreme: float = 150.0,
    stretch_min_atr: float = 1.2,
) -> Optional[dict]:
    """Detect failed breakout reversal opportunities.

    Returns a signal dict on detection:
    {
      "side": "CALL"|"PUT",
      "tag": "FAILED_BREAKOUT_REVERSAL",
      "reason": "...",
      "pivot": "R3"|"R4"|"S3"|"S4",
      "bars_since_cross": int,
      "stretch": float,
      "rsi": float,
      "cci": float,
    }
    """
    if candles_3m is None or len(candles_3m) < max(lookback_bars + 2, 8):
        return None

    last = candles_3m.iloc[-1]
    close_now = _safe_float(last.get("close"))
    rsi = _safe_float(last.get("rsi14"))
    cci = _safe_float(last.get("cci20"))
    atr = _safe_float(last.get("atr14"))
    if not np.isfinite(atr) or atr <= 0 or not np.isfinite(close_now):
        return None

    closes = candles_3m["close"].astype(float)
    ema9 = float(closes.ewm(span=9, adjust=False).mean().iloc[-1])
    stretch = (close_now - ema9) / atr if atr > 0 else float("nan")

    r3 = _safe_float(camarilla_levels.get("r3"))
    r4 = _safe_float(camarilla_levels.get("r4"))
    s3 = _safe_float(camarilla_levels.get("s3"))
    s4 = _safe_float(camarilla_levels.get("s4"))

    # Resistance failed breakout -> PUT reversal candidate.
    for pivot_name, level in (("R4", r4), ("R3", r3)):
        if not np.isfinite(level) or close_now >= level:
            continue
        for k in range(1, min(lookback_bars, len(candles_3m) - 1) + 1):
            prev_close = _safe_float(candles_3m.iloc[-1 - k].get("close"))
            if np.isfinite(prev_close) and prev_close > level:
                osc_ok = (np.isfinite(rsi) and rsi >= osc_rsi_extreme) or (
                    np.isfinite(cci) and cci >= osc_cci_extreme
                )
                stretch_ok = np.isfinite(stretch) and stretch >= stretch_min_atr
                if osc_ok and stretch_ok:
                    logging.info(
                        "[FAILED_BREAKOUT][REVERSAL] "
                        f"side=PUT pivot={pivot_name} bars_since_cross={k} "
                        f"close={close_now:.2f} level={level:.2f} "
                        f"stretch={stretch:.2f} rsi={rsi:.1f} cci={cci:.1f}"
                    )
                    return {
                        "side": "PUT",
                        "tag": "FAILED_BREAKOUT_REVERSAL",
                        "reason": f"Failed breakout at {pivot_name}, rejection confirmed.",
                        "pivot": pivot_name,
                        "bars_since_cross": int(k),
                        "stretch": float(stretch),
                        "rsi": float(rsi) if np.isfinite(rsi) else float("nan"),
                        "cci": float(cci) if np.isfinite(cci) else float("nan"),
                    }
                return None

    # Support failed breakout -> CALL reversal candidate.
    for pivot_name, level in (("S4", s4), ("S3", s3)):
        if not np.isfinite(level) or close_now <= level:
            continue
        for k in range(1, min(lookback_bars, len(candles_3m) - 1) + 1):
            prev_close = _safe_float(candles_3m.iloc[-1 - k].get("close"))
            if np.isfinite(prev_close) and prev_close < level:
                osc_ok = (np.isfinite(rsi) and rsi <= (100.0 - osc_rsi_extreme)) or (
                    np.isfinite(cci) and cci <= -osc_cci_extreme
                )
                stretch_ok = np.isfinite(stretch) and stretch <= -stretch_min_atr
                if osc_ok and stretch_ok:
                    logging.info(
                        "[FAILED_BREAKOUT][REVERSAL] "
                        f"side=CALL pivot={pivot_name} bars_since_cross={k} "
                        f"close={close_now:.2f} level={level:.2f} "
                        f"stretch={stretch:.2f} rsi={rsi:.1f} cci={cci:.1f}"
                    )
                    return {
                        "side": "CALL",
                        "tag": "FAILED_BREAKOUT_REVERSAL",
                        "reason": f"Failed breakout at {pivot_name}, rejection confirmed.",
                        "pivot": pivot_name,
                        "bars_since_cross": int(k),
                        "stretch": float(stretch),
                        "rsi": float(rsi) if np.isfinite(rsi) else float("nan"),
                        "cci": float(cci) if np.isfinite(cci) else float("nan"),
                    }
                return None
    return None


__all__ = ["detect_failed_breakout"]
