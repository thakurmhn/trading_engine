# ===== st_pullback_cci.py =====
"""
Supertrend Pullback + CCI Rejection entry/exit module.

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

Broker integration
------------------
Pass broker_fn callables at runtime; the module itself is broker-agnostic.
Defaults to Fyers-compatible send_live_entry_order / send_paper_exit_order
when those functions are injected from execution.py.

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
        """PUT: ST line is above price.  Track upward pullback toward ST.

        Priority order (checked top-to-bottom):
        1. Price far below ST → arm 'was_away', clear 'approaching'.
        2. Already approaching → evaluate trigger or reset.
        3. Price newly near ST after being away → arm 'approaching'.
        """
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
        """CALL: ST line is below price.  Track downward pullback toward ST.

        Priority order (checked top-to-bottom):
        1. Price far above ST → arm 'was_away', clear 'approaching'.
        2. Already approaching → evaluate trigger or reset.
        3. Price newly near ST after being away → arm 'approaching'.
        """
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
    before the main PT target is reached.  Uses the same directional
    logic as compute_profit_target.

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

    Convenience wrapper
    -------------------
    Use ``signal_side(df_3m, df_15m)`` for a plain 'BUY'/'SELL'/'NONE' string.
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
    broker_fn: Optional[Union[BrokerAdapter, Callable]] = None,
    timestamp=None,
    tg: float = 0.0,
    config: Optional[STEntryConfig] = None,
) -> Tuple[bool, Optional[str]]:
    """Place an entry order for the Supertrend pullback strategy.

    Parameters
    ----------
    side       : 'BUY' or 'SELL'
    broker_fn  : ``BrokerAdapter`` instance  — uses ``adapter.place_entry()``.
                 Plain callable(symbol, qty, side_int) — legacy interface.
                 ``None`` — internal paper simulation.
    tg         : Conservative first target (TG); included in the audit log.
    config     : ``STEntryConfig`` instance; rr_ratio/tg_rr_ratio included in
                 the ``[ENTRY DISPATCH]`` audit log when provided.
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
            if isinstance(broker_fn, BrokerAdapter):
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
    broker_fn: Optional[Union[BrokerAdapter, Callable]] = None,
    timestamp=None,
) -> Tuple[bool, Optional[str]]:
    """Place an exit order for an open Supertrend pullback position.

    Parameters
    ----------
    broker_fn : ``BrokerAdapter`` instance  — uses ``adapter.place_exit()``.
                Plain callable(symbol, qty, reason) — legacy interface.
                ``None`` — internal paper simulation.
    """
    ts = timestamp or "N/A"

    logging.info(
        f"[EXIT ORDER][{mode}] symbol={symbol} qty={qty} reason={reason} timestamp={ts}"
    )

    if broker_fn is not None:
        try:
            if isinstance(broker_fn, BrokerAdapter):
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


# ---------------------------------------------------------------------------
# Broker adapter layer
# ---------------------------------------------------------------------------
# Each concrete adapter wraps a broker's native SDK into the two-method
# interface (place_entry / place_exit) used by place_st_pullback_entry/exit.
#
# Usage example (Fyers):
#   from st_pullback_cci import FyersAdapter, place_st_pullback_entry
#   adapter = FyersAdapter(fyers_client)
#   ok, oid = place_st_pullback_entry(..., broker_fn=adapter)
#
# Usage example (Zerodha Kite):
#   from st_pullback_cci import ZerodhaKiteAdapter
#   adapter = ZerodhaKiteAdapter(kite_client)          # KiteConnect instance
#   ok, oid = place_st_pullback_entry(..., broker_fn=adapter)
# ---------------------------------------------------------------------------


class BrokerAdapter(ABC):
    """Abstract base class for broker-specific order placement.

    Concrete subclasses must implement ``place_entry`` and ``place_exit``.
    Both return ``(success: bool, order_id: str | None)``.
    """

    @abstractmethod
    def place_entry(
        self,
        symbol: str,
        qty: int,
        side_int: int,
        limit_price: float = 0.0,
        stop: float = 0.0,
        target: float = 0.0,
    ) -> Tuple[bool, Optional[str]]:
        """Place an entry order.

        Parameters
        ----------
        side_int   : 1 = BUY, -1 = SELL  (Fyers/standard convention).
        limit_price: 0.0 → market order; >0 → limit order.
        stop       : Stop-loss price (informational; included in audit log).
        target     : Take-profit price (informational; included in audit log).
        """

    @abstractmethod
    def place_exit(
        self,
        symbol: str,
        qty: int,
        reason: str,
    ) -> Tuple[bool, Optional[str]]:
        """Place a market exit (square-off) order."""


class FyersAdapter(BrokerAdapter):
    """Fyers API v3 adapter.

    Wraps ``fyers.place_order()`` into the BrokerAdapter interface.
    Inject the authenticated ``FyersModel`` instance from execution.py.

    Example
    -------
    from execution import fyers
    adapter = FyersAdapter(fyers)
    ok, oid = place_st_pullback_entry("NSE:NIFTY...", 50, "SELL", 120.0, 125.0, 110.0,
                                       mode="LIVE", broker_fn=adapter)
    """

    def __init__(self, fyers_client) -> None:
        self._fyers = fyers_client

    def place_entry(
        self,
        symbol: str,
        qty: int,
        side_int: int,
        limit_price: float = 0.0,
        stop: float = 0.0,
        target: float = 0.0,
    ) -> Tuple[bool, Optional[str]]:
        try:
            logging.info(
                f"[FYERS ENTRY] symbol={symbol} side_int={side_int} "
                f"limit_price={limit_price} stop={stop} target={target}"
            )
            order_data = {
                "symbol":       symbol,
                "qty":          qty,
                "type":         1 if limit_price > 0 else 2,   # 1=LIMIT, 2=MARKET
                "side":         side_int,
                "productType":  "INTRADAY",
                "limitPrice":   limit_price,
                "stopPrice":    0,
                "validity":     "DAY",
                "offlineOrder": False,
                "disclosedQty": 0,
                "isSliceOrder": False,
                "orderTag":     "ST_PULLBACK",
            }
            resp = self._fyers.place_order(data=order_data)
            if resp.get("s") == "ok":
                return True, resp.get("id")
            logging.error(f"[FYERS ENTRY FAILED] symbol={symbol} response={resp}")
            return False, None
        except Exception as exc:
            logging.error(f"[FYERS ENTRY ERROR] symbol={symbol} error={exc}")
            return False, None

    def place_exit(
        self,
        symbol: str,
        qty: int,
        reason: str,
    ) -> Tuple[bool, Optional[str]]:
        try:
            order_data = {
                "symbol":       symbol,
                "qty":          qty,
                "type":         2,       # MARKET
                "side":         -1,      # SELL / square-off
                "productType":  "INTRADAY",
                "limitPrice":   0,
                "stopPrice":    0,
                "validity":     "DAY",
                "offlineOrder": False,
                "disclosedQty": 0,
                "isSliceOrder": False,
                "orderTag":     str(reason),
            }
            resp = self._fyers.place_order(data=order_data)
            if resp.get("s") == "ok":
                return True, resp.get("id")
            logging.error(f"[FYERS EXIT FAILED] symbol={symbol} response={resp}")
            return False, None
        except Exception as exc:
            logging.error(f"[FYERS EXIT ERROR] symbol={symbol} error={exc}")
            return False, None


class ZerodhaKiteAdapter(BrokerAdapter):
    """Zerodha Kite Connect v3 adapter.

    Requires ``kiteconnect`` package:  pip install kiteconnect

    Example
    -------
    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key="your_key")
    kite.set_access_token("access_token")
    adapter = ZerodhaKiteAdapter(kite)
    """

    def __init__(self, kite_client) -> None:
        self._kite = kite_client

    def place_entry(
        self,
        symbol: str,
        qty: int,
        side_int: int,
        limit_price: float = 0.0,
        stop: float = 0.0,
        target: float = 0.0,
    ) -> Tuple[bool, Optional[str]]:
        try:
            txn = "BUY" if side_int == 1 else "SELL"
            logging.info(
                f"[KITE ENTRY] symbol={symbol} side={txn} "
                f"limit_price={limit_price} stop={stop} target={target}"
            )
            order_id = self._kite.place_order(
                variety="regular",
                exchange="NFO",
                tradingsymbol=symbol,
                transaction_type=txn,
                quantity=qty,
                product="MIS",
                order_type="LIMIT" if limit_price > 0 else "MARKET",
                price=limit_price if limit_price > 0 else None,
                tag="ST_PULLBACK",
            )
            return True, str(order_id)
        except Exception as exc:
            logging.error(f"[KITE ENTRY ERROR] symbol={symbol} error={exc}")
            return False, None

    def place_exit(
        self,
        symbol: str,
        qty: int,
        reason: str,
    ) -> Tuple[bool, Optional[str]]:
        try:
            order_id = self._kite.place_order(
                variety="regular",
                exchange="NFO",
                tradingsymbol=symbol,
                transaction_type="SELL",
                quantity=qty,
                product="MIS",
                order_type="MARKET",
                tag=f"EXIT_{reason[:18]}",
            )
            return True, str(order_id)
        except Exception as exc:
            logging.error(f"[KITE EXIT ERROR] symbol={symbol} error={exc}")
            return False, None


class AngelOneAdapter(BrokerAdapter):
    """Angel One Smart API adapter.

    Requires ``smartapi-python`` package:  pip install smartapi-python

    Example
    -------
    from SmartApi import SmartConnect
    client = SmartConnect(api_key="your_key")
    client.generateSession("client_code", "pin", totp_value)
    adapter = AngelOneAdapter(client)
    """

    def __init__(self, smart_connect_client) -> None:
        self._client = smart_connect_client

    def place_entry(
        self,
        symbol: str,
        qty: int,
        side_int: int,
        limit_price: float = 0.0,
        stop: float = 0.0,
        target: float = 0.0,
    ) -> Tuple[bool, Optional[str]]:
        try:
            txn = "BUY" if side_int == 1 else "SELL"
            logging.info(
                f"[ANGEL ENTRY] symbol={symbol} side={txn} "
                f"limit_price={limit_price} stop={stop} target={target}"
            )
            params = {
                "variety":         "NORMAL",
                "tradingsymbol":   symbol,
                "symboltoken":     "",            # populate from option chain lookup
                "transactiontype": txn,
                "exchange":        "NFO",
                "ordertype":       "LIMIT" if limit_price > 0 else "MARKET",
                "producttype":     "INTRADAY",
                "duration":        "DAY",
                "price":           str(limit_price) if limit_price > 0 else "0",
                "squareoff":       "0",
                "stoploss":        "0",
                "quantity":        str(qty),
                "ordertag":        "ST_PULLBACK",
            }
            resp = self._client.placeOrder(params)
            if resp and resp.get("status"):
                return True, resp.get("data", {}).get("orderid")
            logging.error(f"[ANGEL ENTRY FAILED] symbol={symbol} response={resp}")
            return False, None
        except Exception as exc:
            logging.error(f"[ANGEL ENTRY ERROR] symbol={symbol} error={exc}")
            return False, None

    def place_exit(
        self,
        symbol: str,
        qty: int,
        reason: str,
    ) -> Tuple[bool, Optional[str]]:
        try:
            params = {
                "variety":         "NORMAL",
                "tradingsymbol":   symbol,
                "symboltoken":     "",
                "transactiontype": "SELL",
                "exchange":        "NFO",
                "ordertype":       "MARKET",
                "producttype":     "INTRADAY",
                "duration":        "DAY",
                "price":           "0",
                "squareoff":       "0",
                "stoploss":        "0",
                "quantity":        str(qty),
                "ordertag":        f"EXIT_{reason[:18]}",
            }
            resp = self._client.placeOrder(params)
            if resp and resp.get("status"):
                return True, resp.get("data", {}).get("orderid")
            logging.error(f"[ANGEL EXIT FAILED] symbol={symbol} response={resp}")
            return False, None
        except Exception as exc:
            logging.error(f"[ANGEL EXIT ERROR] symbol={symbol} error={exc}")
            return False, None


class CcxtAdapter(BrokerAdapter):
    """Generic ccxt adapter for crypto exchanges (Binance, Bybit, OKX, …).

    Requires ``ccxt`` package:  pip install ccxt

    Example
    -------
    import ccxt
    exchange = ccxt.binance({
        "apiKey": "your_key", "secret": "your_secret",
        "options": {"defaultType": "future"},
    })
    adapter = CcxtAdapter(exchange)
    """

    def __init__(self, exchange, quote_currency: str = "USDT") -> None:
        self._exchange     = exchange
        self._quote        = quote_currency

    def place_entry(
        self,
        symbol: str,
        qty: int,
        side_int: int,
        limit_price: float = 0.0,
        stop: float = 0.0,
        target: float = 0.0,
    ) -> Tuple[bool, Optional[str]]:
        try:
            ccxt_side  = "buy" if side_int == 1 else "sell"
            order_type = "limit" if limit_price > 0 else "market"
            price      = limit_price if limit_price > 0 else None
            logging.info(
                f"[CCXT ENTRY] symbol={symbol} side={ccxt_side} "
                f"limit_price={limit_price} stop={stop} target={target}"
            )
            order = self._exchange.create_order(
                symbol=symbol,
                type=order_type,
                side=ccxt_side,
                amount=qty,
                price=price,
                params={"clientOrderId": "ST_PULLBACK"},
            )
            return True, order.get("id")
        except Exception as exc:
            logging.error(f"[CCXT ENTRY ERROR] symbol={symbol} error={exc}")
            return False, None

    def place_exit(
        self,
        symbol: str,
        qty: int,
        reason: str,
    ) -> Tuple[bool, Optional[str]]:
        try:
            order = self._exchange.create_order(
                symbol=symbol,
                type="market",
                side="sell",
                amount=qty,
                params={"clientOrderId": f"EXIT_{reason[:18]}"},
            )
            return True, order.get("id")
        except Exception as exc:
            logging.error(f"[CCXT EXIT ERROR] symbol={symbol} error={exc}")
            return False, None


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _safe_float(val) -> Optional[float]:
    try:
        f = float(val)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return None
