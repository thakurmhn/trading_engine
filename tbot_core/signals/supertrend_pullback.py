# ===== tbot_core/signals/supertrend_pullback.py =====
"""
Supertrend Pullback + CCI Rejection entry/exit module.

Migrated from root st_pullback_cci.py — signal detection logic only.
Broker adapters are in tbot_core/broker/.

Strategy logic
--------------
Trend regime:
    15m Supertrend bias   → higher-timeframe direction filter.
    3m  Supertrend bias + slope → entry-timeframe confirmation.

Bearish (SELL / PUT):
    Gate  : ST15m_bias=BEARISH, ST3m_bias=BEARISH, ST3m_slope=DOWN
    Trigger A — Pullback:  price pulls back UP toward ST line then is
                           rejected (high approaches ST line, close < ST line).
    Trigger B — CCI:       CCI(14) >= +100  (overbought in a downtrend).

Bullish (BUY / CALL):
    Gate  : ST15m_bias=BULLISH, ST3m_bias=BULLISH, ST3m_slope=UP
    Trigger A — Pullback:  price pulls back DOWN toward ST line then is
                           rejected (low approaches ST line, close > ST line).
    Trigger B — CCI:       CCI(14) <= -100  (oversold in an uptrend).

Risk management
---------------
Stop Loss  = ST_line ± sl_buffer_atr_mult * ATR  (above line for PUT, below for CALL)
Profit Tgt = entry + (entry - SL) * rr_ratio

Logging tags (mandatory)
------------------------
[ENTRY BLOCKED][ST_CONFLICT]       — 15m vs 3m bias mismatch
[ENTRY BLOCKED][ST_SLOPE_CONFLICT] — 3m slope does not confirm bias
[ENTRY ALLOWED][ST_BIAS_OK]        — biases + slope confirmed
[ENTRY SIGNAL][PULLBACK]           — pullback trigger fired
[ENTRY SIGNAL][CCI_REJECTION]      — CCI trigger fired
[ENTRY BLOCKED][NO_TRIGGER]        — gate passed but no trigger active
[ENTRY DIAG][ST_PULLBACK]          — structured diagnostics per evaluation
"""

from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple, Union

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Optional pandas_ta import (used for CCI if the column is absent).
# Falls back to a pure-pandas implementation if pandas_ta is not installed.
# ---------------------------------------------------------------------------
try:
    import pandas_ta as ta  # type: ignore
    _HAS_PANDAS_TA = True
except ImportError:
    _HAS_PANDAS_TA = False


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class STEntryConfig:
    """All tunable parameters for the ST-pullback / CCI strategy.

    Supertrend fresh-computation settings are only used when the candle
    DataFrame does not already contain precomputed supertrend_* columns.
    """
    # Supertrend (for fresh computation)
    st_period: int = 10
    st_multiplier: float = 3.0

    # CCI
    cci_period: int = 14
    cci_put_thresh: float = 100.0    # CCI >= this to trigger SELL/PUT
    cci_call_thresh: float = -100.0  # CCI <= this to trigger BUY/CALL

    # Pullback detection (multiples of ATR)
    away_atr_mult: float = 0.5   # min distance to be counted as "away"
    touch_atr_mult: float = 0.25 # max distance to be counted as "touching"

    # Risk management
    sl_buffer_atr_mult: float = 0.25  # SL = ST_line ± this × ATR
    tg_rr_ratio: float = 1.0          # TG first target (conservative, e.g. partial booking)
    rr_ratio: float = 2.0             # PT final target reward:risk ratio

    # Mode
    mode: str = "PAPER"               # "PAPER" or "LIVE"

    # Diagnostics
    log_diagnostics: bool = True


# ---------------------------------------------------------------------------
# Bias normalisation (mirrors _supertrend_alignment_gate in execution.py)
# ---------------------------------------------------------------------------

def _norm_bias(raw: str) -> str:
    txt = str(raw).strip().upper()
    if txt in {"BULLISH", "UP"}:
        return "BULLISH"
    if txt in {"BEARISH", "DOWN"}:
        return "BEARISH"
    return "NEUTRAL"


# ---------------------------------------------------------------------------
# CCI computation
# ---------------------------------------------------------------------------

def _compute_cci(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Return CCI(period) series for *df*.

    Prefers pandas_ta when available; otherwise uses a pure-pandas
    implementation of the standard CCI formula.
    """
    col = f"cci{period}"
    if col in df.columns:
        return df[col].astype(float)

    if _HAS_PANDAS_TA:
        result = ta.cci(df["high"], df["low"], df["close"], length=period)
        return result.astype(float) if result is not None else pd.Series(
            [float("nan")] * len(df), index=df.index
        )

    # Pure-pandas CCI (Lambert 1980)
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    sma = tp.rolling(period, min_periods=period).mean()
    mad = tp.rolling(period, min_periods=period).apply(
        lambda x: np.mean(np.abs(x - np.mean(x))), raw=True
    )
    denom = 0.015 * mad
    cci = (tp - sma) / denom.replace(0, np.nan)
    return cci.astype(float)


# ---------------------------------------------------------------------------
# ATR helper
# ---------------------------------------------------------------------------

def _get_atr(df: pd.DataFrame, period: int = 14) -> float:
    """Return a single scalar ATR value from the last row of *df*."""
    col = f"atr{period}"
    if col in df.columns:
        val = df[col].iloc[-1]
        if pd.notna(val) and float(val) > 0:
            return float(val)

    # Rolling ATR fallback
    if len(df) < period + 1:
        return float("nan")
    hl  = df["high"] - df["low"]
    hc  = (df["high"] - df["close"].shift(1)).abs()
    lc  = (df["low"]  - df["close"].shift(1)).abs()
    tr  = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    atr = tr.rolling(period).mean().iloc[-1]
    return float(atr) if pd.notna(atr) else float("nan")


# ---------------------------------------------------------------------------
# Supertrend fresh computation
# ---------------------------------------------------------------------------

def _compute_supertrend(
    df: pd.DataFrame,
    period: int = 10,
    multiplier: float = 3.0,
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Compute Supertrend(period, multiplier) from OHLC data.

    Returns (line_series, bias_series, slope_series).
    bias values: 'BULLISH' | 'BEARISH' | 'NEUTRAL'
    slope values: 'UP' | 'DOWN' | 'FLAT'
    """
    if _HAS_PANDAS_TA:
        result = ta.supertrend(
            df["high"], df["low"], df["close"],
            length=period, multiplier=multiplier
        )
        if result is not None and not result.empty:
            # pandas_ta columns: SUPERT_p_m, SUPERTd_p_m, SUPERTl_p_m, SUPERTs_p_m
            line_col = f"SUPERT_{period}_{multiplier}"
            dir_col  = f"SUPERTd_{period}_{multiplier}"
            line_s   = result.get(line_col, pd.Series(dtype=float))
            dir_s    = result.get(dir_col,  pd.Series(dtype=float))
            bias_s   = dir_s.map(lambda d: "BULLISH" if d == 1 else ("BEARISH" if d == -1 else "NEUTRAL"))
            # Slope from line change
            slope_s = _slope_from_line(line_s.reindex(df.index))
            return line_s.reindex(df.index), bias_s.reindex(df.index), slope_s

    # Pure-pandas fallback (same logic as orchestration.supertrend)
    df2 = df.copy()
    hl2 = (df2["high"] + df2["low"]) / 2.0
    tr  = pd.concat([
        df2["high"] - df2["low"],
        (df2["high"] - df2["close"].shift(1)).abs(),
        (df2["low"]  - df2["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    ub_raw = hl2 + multiplier * atr
    lb_raw = hl2 - multiplier * atr

    final_ub = ub_raw.copy()
    final_lb = lb_raw.copy()
    for i in range(1, len(df2)):
        final_ub.iloc[i] = (
            min(ub_raw.iloc[i], final_ub.iloc[i - 1])
            if df2["close"].iloc[i - 1] <= final_ub.iloc[i - 1]
            else ub_raw.iloc[i]
        )
        final_lb.iloc[i] = (
            max(lb_raw.iloc[i], final_lb.iloc[i - 1])
            if df2["close"].iloc[i - 1] >= final_lb.iloc[i - 1]
            else lb_raw.iloc[i]
        )

    line  = pd.Series(index=df2.index, dtype=float)
    bias  = pd.Series(index=df2.index, dtype=object)
    for i in range(period, len(df2)):
        close_i   = df2["close"].iloc[i]
        prev_ub   = final_ub.iloc[i - 1]
        prev_lb   = final_lb.iloc[i - 1]
        prev_bias = bias.iloc[i - 1] if i > period else "NEUTRAL"
        if close_i > prev_ub:
            line.iloc[i] = final_lb.iloc[i]
            bias.iloc[i] = "BULLISH"
        elif close_i < prev_lb:
            line.iloc[i] = final_ub.iloc[i]
            bias.iloc[i] = "BEARISH"
        else:
            line.iloc[i] = (
                final_lb.iloc[i] if prev_bias == "BULLISH" else
                final_ub.iloc[i] if prev_bias == "BEARISH" else
                line.iloc[i - 1]
            )
            bias.iloc[i] = prev_bias if prev_bias in ("BULLISH", "BEARISH") else "NEUTRAL"

    slope = _slope_from_line(line, lookback=5)
    return line, bias, slope


def _slope_from_line(line: pd.Series, lookback: int = 5) -> pd.Series:
    slope = pd.Series("FLAT", index=line.index, dtype=object)
    for i in range(lookback, len(line)):
        prev = line.iloc[i - lookback]
        curr = line.iloc[i]
        if pd.isna(prev) or pd.isna(curr):
            slope.iloc[i] = slope.iloc[i - 1] if i > 0 else "FLAT"
        elif curr > prev:
            slope.iloc[i] = "UP"
        elif curr < prev:
            slope.iloc[i] = "DOWN"
        else:
            slope.iloc[i] = "FLAT"
    return slope


# ---------------------------------------------------------------------------
# Supertrend snapshot from candle row (with fresh-compute fallback)
# ---------------------------------------------------------------------------

def _st_snapshot(df: pd.DataFrame, config: STEntryConfig) -> Dict:
    """Return the last-row ST values, computing fresh if columns are absent."""
    last = df.iloc[-1]
    has_cols = all(c in df.columns for c in ("supertrend_line", "supertrend_bias", "supertrend_slope"))

    if has_cols and pd.notna(last.get("supertrend_line")):
        return {
            "line":  float(last["supertrend_line"]),
            "bias":  _norm_bias(str(last.get("supertrend_bias", "NEUTRAL"))),
            "slope": str(last.get("supertrend_slope", "FLAT")).upper(),
        }

    # Fresh computation
    line_s, bias_s, slope_s = _compute_supertrend(df, config.st_period, config.st_multiplier)
    return {
        "line":  float(line_s.iloc[-1]) if pd.notna(line_s.iloc[-1]) else float("nan"),
        "bias":  _norm_bias(str(bias_s.iloc[-1])),
        "slope": str(slope_s.iloc[-1]).upper(),
    }


# ---------------------------------------------------------------------------
# Pullback state machine
# ---------------------------------------------------------------------------

class PullbackTracker:
    """Per-symbol stateful pullback detector.

    Tracks whether price was "away" from the Supertrend line (trend
    established) and has since "pulled back" toward it.  When the candle
    touches / approaches the line and closes in the trend direction the
    tracker fires a PULLBACK trigger.

    Instantiate one PullbackTracker per symbol and pass it on every
    call to check_entry_signal.
    """

    def __init__(
        self,
        away_atr_mult: float = 0.5,
        touch_atr_mult: float = 0.25,
    ) -> None:
        self.away_atr_mult  = away_atr_mult
        self.touch_atr_mult = touch_atr_mult
        self._was_away      = False
        self._approaching   = False
        self._last_bias: Optional[str] = None

    # ------------------------------------------------------------------ #
    # Public interface                                                     #
    # ------------------------------------------------------------------ #

    def update(
        self,
        close: float,
        high: float,
        low: float,
        st_line: float,
        atr: float,
        side: str,
    ) -> Optional[str]:
        """Evaluate one candle and return 'PULLBACK' if triggered, else None.

        Parameters
        ----------
        side : 'PUT' or 'CALL'
            PUT  → bearish trend, ST line above price.
            CALL → bullish trend, ST line below price.
        """
        if not all(math.isfinite(v) for v in (close, high, low, st_line, atr)) or atr <= 0:
            return None

        if side == "PUT":
            return self._update_bearish(close, high, st_line, atr)
        return self._update_bullish(close, low, st_line, atr)

    def reset(self) -> None:
        """Hard reset (call when bias flips or a trade is entered)."""
        self._was_away    = False
        self._approaching = False
        self._last_bias   = None

    def reset_on_bias_change(self, current_bias: str) -> None:
        """Soft reset when the trend direction changes."""
        if self._last_bias is not None and self._last_bias != current_bias:
            self._was_away    = False
            self._approaching = False
            logging.debug(
                f"[PULLBACK TRACKER] Bias changed {self._last_bias}→{current_bias}; state reset."
            )
        self._last_bias = current_bias

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _update_bearish(self, close: float, high: float, st_line: float, atr: float) -> Optional[str]:
        """PUT: ST line is above price.  Track upward pullback toward ST."""
        dist = st_line - close  # positive when price < ST (normal BEARISH)

        # 1. Price far below line — establish / maintain "away" status.
        if dist > self.away_atr_mult * atr:
            self._was_away    = True
            self._approaching = False
            return None

        # 2. Already in approach mode — check for rejection trigger first.
        if self._approaching:
            touched = high >= (st_line - 0.1 * atr)
            if touched and close < st_line:
                self._was_away    = False
                self._approaching = False
                return "PULLBACK"
            return None

        # 3. Transition: price is now close to the line and was previously away.
        if 0 <= dist <= self.touch_atr_mult * atr and self._was_away:
            self._approaching = True

        return None

    def _update_bullish(self, close: float, low: float, st_line: float, atr: float) -> Optional[str]:
        """CALL: ST line is below price.  Track downward pullback toward ST."""
        dist = close - st_line  # positive when price > ST (normal BULLISH)

        # 1. Price far above line — establish / maintain "away" status.
        if dist > self.away_atr_mult * atr:
            self._was_away    = True
            self._approaching = False
            return None

        # 2. Already in approach mode — check for rejection trigger first.
        if self._approaching:
            touched = low <= (st_line + 0.1 * atr)
            if touched and close > st_line:
                self._was_away    = False
                self._approaching = False
                return "PULLBACK"
            return None

        # 3. Transition: price is now close to the line and was previously away.
        if 0 <= dist <= self.touch_atr_mult * atr and self._was_away:
            self._approaching = True

        return None

    # ------------------------------------------------------------------ #
    # Diagnostics                                                          #
    # ------------------------------------------------------------------ #

    @property
    def state_dict(self) -> Dict:
        return {
            "was_away":   self._was_away,
            "approaching": self._approaching,
            "last_bias":  self._last_bias,
        }


# ---------------------------------------------------------------------------
# Risk management
# ---------------------------------------------------------------------------

def compute_stop_loss(
    entry_price: float,
    st_line: float,
    side: str,
    atr: float,
    buffer_mult: float = 0.25,
) -> float:
    """Compute stop-loss price.

    PUT  (SELL): SL placed above ST line — st_line + buffer_mult * ATR.
    CALL (BUY):  SL placed below ST line — st_line - buffer_mult * ATR.
    """
    buf = buffer_mult * atr
    if side == "PUT":
        return round(st_line + buf, 2)
    return round(st_line - buf, 2)


def compute_profit_target(
    entry_price: float,
    sl: float,
    rr_ratio: float = 2.0,
) -> float:
    """Compute final profit target using a fixed reward:risk ratio.

    For PUT  (short): entry_price - risk * rr_ratio
    For CALL (long):  entry_price + risk * rr_ratio
    Risk = |entry_price - sl|.
    """
    risk = abs(entry_price - sl)
    if sl > entry_price:          # PUT: SL above entry
        return round(entry_price - risk * rr_ratio, 2)
    return round(entry_price + risk * rr_ratio, 2)   # CALL: SL below entry


def compute_trailing_target(
    entry_price: float,
    sl: float,
    tg_rr_ratio: float = 1.0,
) -> float:
    """Compute conservative first take-profit / trailing-goal (TG).

    TG uses a lower RR ratio (default 1:1) intended for partial booking
    before the main PT target is reached.

    For PUT  (short): entry_price - risk * tg_rr_ratio
    For CALL (long):  entry_price + risk * tg_rr_ratio
    """
    risk = abs(entry_price - sl)
    if sl > entry_price:          # PUT: SL above entry
        return round(entry_price - risk * tg_rr_ratio, 2)
    return round(entry_price + risk * tg_rr_ratio, 2)   # CALL: SL below entry


# ---------------------------------------------------------------------------
# Core signal check
# ---------------------------------------------------------------------------

def check_entry_signal(
    df_3m: pd.DataFrame,
    df_15m: pd.DataFrame,
    symbol: str = "UNKNOWN",
    config: Optional[STEntryConfig] = None,
    tracker: Optional[PullbackTracker] = None,
    timestamp=None,
) -> Optional[Dict]:
    """Evaluate one market snapshot for a Supertrend pullback/CCI entry.

    Returns a signal dict on entry, or ``None`` when no signal is present.

    Return dict keys
    ----------------
    side        : 'BUY'  | 'SELL'          (market direction)
    option_type : 'CALL' | 'PUT'           (options side)
    trigger     : 'PULLBACK' | 'CCI_REJECTION'
    reason      : human-readable summary
    st3m_line   : float
    atr         : float
    sl          : float
    pt          : float
    cci14       : float
    details     : dict  (full diagnostics snapshot)
    """
    if config is None:
        config = STEntryConfig()
    if tracker is None:
        tracker = PullbackTracker(config.away_atr_mult, config.touch_atr_mult)

    ts = timestamp or (df_3m.index[-1] if not df_3m.empty else "?")

    # ------------------------------------------------------------------
    # 1. Guard — minimum candle count
    # ------------------------------------------------------------------
    if df_3m is None or df_3m.empty:
        logging.warning(f"[ST PULLBACK][{symbol}] df_3m is empty, skipping.")
        return None

    if df_15m is None or df_15m.empty:
        logging.warning(f"[ST PULLBACK][{symbol}] df_15m is empty — 15m gate cannot be evaluated.")
        return None

    # ------------------------------------------------------------------
    # 2. Extract indicator snapshots
    # ------------------------------------------------------------------
    st3m  = _st_snapshot(df_3m,  config)
    st15m = _st_snapshot(df_15m, config)
    atr   = _get_atr(df_3m)

    cci_series = _compute_cci(df_3m, config.cci_period)
    cci14 = float(cci_series.iloc[-1]) if not cci_series.empty and pd.notna(cci_series.iloc[-1]) else float("nan")

    last_3m = df_3m.iloc[-1]
    close   = float(last_3m.get("close", float("nan")))
    high    = float(last_3m.get("high",  float("nan")))
    low     = float(last_3m.get("low",   float("nan")))

    # Camarilla / CPR diagnostics (best-effort — not gating)
    s4_val = _safe_float(last_3m.get("s4"))
    r4_val = _safe_float(last_3m.get("r4"))
    cpr_width = str(last_3m.get("cpr_width", "UNKNOWN"))
    compressed_cam = bool(last_3m.get("compressed_cam", False))

    diag: Dict = {
        "timestamp":    ts,
        "symbol":       symbol,
        "ST3m_bias":    st3m["bias"],
        "ST3m_slope":   st3m["slope"],
        "ST3m_line":    st3m["line"],
        "ST15m_bias":   st15m["bias"],
        "ST15m_line":   st15m["line"],
        "close":        close,
        "high":         high,
        "low":          low,
        "atr":          atr,
        "cci14":        cci14,
        "s4":           s4_val,
        "r4":           r4_val,
        "cpr_width":    cpr_width,
        "compressed_cam": compressed_cam,
        "tracker_state": tracker.state_dict,
    }

    if config.log_diagnostics:
        logging.info(
            "[ENTRY DIAG][ST_PULLBACK] "
            f"timestamp={ts} symbol={symbol} "
            f"ST3m_bias={st3m['bias']} ST3m_slope={st3m['slope']} ST3m_line={st3m['line']} "
            f"ST15m_bias={st15m['bias']} "
            f"close={close} high={high} low={low} atr={atr} cci14={cci14} "
            f"s4={s4_val} r4={r4_val} cpr_width={cpr_width} compressed_cam={compressed_cam} "
            f"tracker={tracker.state_dict}"
        )

    # ------------------------------------------------------------------
    # 3. Bias alignment gate (15m bias — no slope check)
    # ------------------------------------------------------------------
    bias_15m = st15m["bias"]
    bias_3m  = st3m["bias"]

    if bias_15m not in ("BULLISH", "BEARISH") or bias_3m not in ("BULLISH", "BEARISH"):
        logging.info(
            "[ENTRY BLOCKED][ST_CONFLICT] "
            f"timestamp={ts} symbol={symbol} "
            f"ST15m_bias={bias_15m} ST3m_bias={bias_3m} "
            "reason=One or both biases are NEUTRAL, entry suppressed."
        )
        tracker.reset_on_bias_change(bias_3m)
        return None

    if bias_15m != bias_3m:
        logging.info(
            "[ENTRY BLOCKED][ST_CONFLICT] "
            f"timestamp={ts} symbol={symbol} "
            f"ST15m_bias={bias_15m} ST3m_bias={bias_3m} "
            "reason=15m vs 3m bias conflict, entry suppressed."
        )
        tracker.reset_on_bias_change(bias_3m)
        return None

    # ------------------------------------------------------------------
    # 4. 3m slope confirmation gate
    # ------------------------------------------------------------------
    slope_3m = st3m["slope"]
    slope_ok = (
        (bias_3m == "BULLISH" and slope_3m == "UP") or
        (bias_3m == "BEARISH" and slope_3m == "DOWN")
    )
    if not slope_ok:
        logging.info(
            "[ENTRY BLOCKED][ST_SLOPE_CONFLICT] "
            f"timestamp={ts} symbol={symbol} "
            f"ST3m_bias={bias_3m} ST3m_slope={slope_3m} "
            "reason=3m slope does not confirm bias direction, entry suppressed."
        )
        tracker.reset_on_bias_change(bias_3m)
        return None

    # ------------------------------------------------------------------
    # 5. Gate passed — log alignment and determine option side
    # ------------------------------------------------------------------
    side       = "PUT"  if bias_3m == "BEARISH" else "CALL"
    mkt_side   = "SELL" if side == "PUT" else "BUY"

    logging.info(
        "[ENTRY ALLOWED][ST_BIAS_OK] "
        f"timestamp={ts} symbol={symbol} allowed_side={side} "
        f"ST15m_bias={bias_15m} ST3m_bias={bias_3m} ST3m_slope={slope_3m} "
        "reason=15m/3m biases aligned and 3m slope confirmed."
    )

    tracker.reset_on_bias_change(bias_3m)

    # ------------------------------------------------------------------
    # 6. Validate ATR for risk computation
    # ------------------------------------------------------------------
    if not math.isfinite(atr) or atr <= 0:
        logging.warning(
            f"[ENTRY BLOCKED][INVALID_ATR] "
            f"timestamp={ts} symbol={symbol} atr={atr} "
            "reason=ATR unavailable or zero, cannot size SL/PT."
        )
        return None

    # ------------------------------------------------------------------
    # 7. CCI rejection trigger
    # ------------------------------------------------------------------
    cci_trigger: Optional[str] = None
    if math.isfinite(cci14):
        if side == "PUT"  and cci14 >= config.cci_put_thresh:
            cci_trigger = "CCI_REJECTION"
        elif side == "CALL" and cci14 <= config.cci_call_thresh:
            cci_trigger = "CCI_REJECTION"

    # ------------------------------------------------------------------
    # 8. Pullback trigger (stateful)
    # ------------------------------------------------------------------
    st3m_line = st3m["line"]
    pb_trigger: Optional[str] = tracker.update(
        close=close, high=high, low=low,
        st_line=st3m_line, atr=atr, side=side,
    )

    # ------------------------------------------------------------------
    # 9. Select active trigger (pullback takes priority over CCI)
    # ------------------------------------------------------------------
    trigger = pb_trigger or cci_trigger

    if trigger is None:
        logging.info(
            "[ENTRY BLOCKED][NO_TRIGGER] "
            f"timestamp={ts} symbol={symbol} side={side} "
            f"cci14={cci14:.1f} tracker={tracker.state_dict} "
            "reason=Gate passed but neither pullback nor CCI trigger active."
        )
        return None

    # ------------------------------------------------------------------
    # 10. Build signal
    # ------------------------------------------------------------------
    sl = compute_stop_loss(close, st3m_line, side, atr, config.sl_buffer_atr_mult)
    tg = compute_trailing_target(close, sl, config.tg_rr_ratio)   # conservative first target
    pt = compute_profit_target(close, sl, config.rr_ratio)         # final target

    diag.update({"sl": sl, "tg": tg, "pt": pt, "trigger": trigger, "side": mkt_side})

    log_tag = "[ENTRY SIGNAL][PULLBACK]" if trigger == "PULLBACK" else "[ENTRY SIGNAL][CCI_REJECTION]"
    logging.info(
        f"{log_tag} "
        f"timestamp={ts} symbol={symbol} side={mkt_side} option_type={side} "
        f"ST3m_line={st3m_line} close={close} cci14={cci14:.1f} "
        f"sl={sl} tg={tg} pt={pt} atr={atr} "
        f"trigger={trigger} ST15m_bias={bias_15m} ST3m_bias={bias_3m} "
        f"s4={s4_val} r4={r4_val} cpr_width={cpr_width} compressed_cam={compressed_cam}"
    )

    return {
        "side":        mkt_side,
        "option_type": side,
        "trigger":     trigger,
        "reason":      (
            f"ST_PULLBACK trigger={trigger} ST15m={bias_15m} "
            f"ST3m={bias_3m}/{slope_3m} CCI14={cci14:.1f}"
        ),
        "st3m_line":   st3m_line,
        "atr":         atr,
        "sl":          sl,
        "tg":          tg,   # conservative first take-profit (partial booking)
        "pt":          pt,   # final take-profit target
        "cci14":       cci14,
        "details":     diag,
    }


# ---------------------------------------------------------------------------
# Convenience wrapper — returns 'BUY' | 'SELL' | 'NONE'
# ---------------------------------------------------------------------------

def signal_side(
    df_3m: pd.DataFrame,
    df_15m: pd.DataFrame,
    symbol: str = "UNKNOWN",
    config: Optional[STEntryConfig] = None,
    tracker: Optional[PullbackTracker] = None,
) -> str:
    """Thin wrapper around check_entry_signal.

    Returns 'BUY', 'SELL', or 'NONE' as specified in the strategy brief.
    """
    sig = check_entry_signal(df_3m, df_15m, symbol=symbol, config=config, tracker=tracker)
    if sig is None:
        return "NONE"
    return sig["side"]


# ---------------------------------------------------------------------------
# Broker integration hooks
# ---------------------------------------------------------------------------

def place_st_pullback_entry(
    symbol: str,
    qty: int,
    side: str,
    entry_price: float,
    sl: float,
    pt: float,
    mode: str = "PAPER",
    broker_fn=None,
    timestamp=None,
    tg: float = 0.0,
    config: Optional[STEntryConfig] = None,
) -> Tuple[bool, Optional[str]]:
    """Place an entry order for the Supertrend pullback strategy.

    Parameters
    ----------
    side       : 'BUY' or 'SELL'
    broker_fn  : BrokerAdapter instance or plain callable.
                 ``None`` — internal paper simulation.
    """
    side_int = 1 if side == "BUY" else -1
    ts = timestamp or "N/A"
    _rr   = config.rr_ratio    if config is not None else "N/A"
    _tgrr = config.tg_rr_ratio if config is not None else "N/A"

    logging.info(
        f"[ENTRY ORDER][{mode}] symbol={symbol} side={side} qty={qty} "
        f"entry={entry_price} sl={sl} tg={tg} pt={pt} "
        f"rr_ratio={_rr} tg_rr_ratio={_tgrr} timestamp={ts}"
    )

    if broker_fn is not None:
        try:
            if hasattr(broker_fn, 'place_entry'):
                success, order_id = broker_fn.place_entry(
                    symbol, qty, side_int, entry_price, stop=sl, target=pt
                )
            else:
                success, order_id = broker_fn(symbol, qty, side_int)

            if success:
                broker_name = type(broker_fn).__name__
                logging.info(
                    f"[ENTRY DISPATCH] broker={broker_name} symbol={symbol} side={side} "
                    f"entry={entry_price} sl={sl} tg={tg} pt={pt} "
                    f"rr_ratio={_rr} tg_rr_ratio={_tgrr} "
                    f"order_id={order_id} timestamp={ts}"
                )
            else:
                logging.error(
                    f"[ENTRY ORDER][FAILED] symbol={symbol} side={side} timestamp={ts}"
                )
            return success, order_id
        except Exception as exc:
            logging.error(
                f"[ENTRY ORDER][ERROR] symbol={symbol} side={side} error={exc}"
            )
            return False, None

    # Internal paper simulation
    paper_id = f"paper_pb_{symbol}_{side}_{ts}"
    logging.info(
        f"[ENTRY ORDER][PAPER_SIM] symbol={symbol} side={side} qty={qty} "
        f"entry={entry_price} sl={sl} tg={tg} pt={pt} order_id={paper_id}"
    )
    return True, paper_id


def place_st_pullback_exit(
    symbol: str,
    qty: int,
    reason: str,
    mode: str = "PAPER",
    broker_fn=None,
    timestamp=None,
) -> Tuple[bool, Optional[str]]:
    """Place an exit order for an open Supertrend pullback position."""
    ts = timestamp or "N/A"

    logging.info(
        f"[EXIT ORDER][{mode}] symbol={symbol} qty={qty} reason={reason} timestamp={ts}"
    )

    if broker_fn is not None:
        try:
            if hasattr(broker_fn, 'place_exit'):
                success, order_id = broker_fn.place_exit(symbol, qty, reason)
            else:
                success, order_id = broker_fn(symbol, qty, reason)

            if success:
                broker_name = type(broker_fn).__name__
                logging.info(
                    f"[EXIT DISPATCH] broker={broker_name} symbol={symbol} reason={reason} "
                    f"order_id={order_id} timestamp={ts}"
                )
            else:
                logging.error(
                    f"[EXIT ORDER][FAILED] symbol={symbol} reason={reason} timestamp={ts}"
                )
            return success, order_id
        except Exception as exc:
            logging.error(
                f"[EXIT ORDER][ERROR] symbol={symbol} reason={reason} error={exc}"
            )
            return False, None

    paper_id = f"paper_exit_{symbol}_{reason}_{ts}"
    logging.info(
        f"[EXIT ORDER][PAPER_SIM] symbol={symbol} qty={qty} reason={reason} "
        f"order_id={paper_id}"
    )
    return True, paper_id


def _safe_float(val) -> Optional[float]:
    if val is None:
        return None
    try:
        f = float(val)
        return f if not (math.isnan(f) or math.isinf(f)) else None
    except (TypeError, ValueError):
        return None


__all__ = [
    "STEntryConfig",
    "PullbackTracker",
    "check_entry_signal",
    "signal_side",
    "compute_stop_loss",
    "compute_profit_target",
    "compute_trailing_target",
    "place_st_pullback_entry",
    "place_st_pullback_exit",
    "_norm_bias",
    "_compute_cci",
    "_get_atr",
    "_compute_supertrend",
    "_st_snapshot",
    "_safe_float",
]
