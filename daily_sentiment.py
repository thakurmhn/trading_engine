# ===== daily_sentiment.py =====
"""
Pre-session daily sentiment and bias prediction module.

Computes a directional bias for the trading day from prior-day price structure
BEFORE the market opens. Used to pre-configure entry scoring thresholds,
preferred trade side, and hold-time expectations.

Inputs (all price-derived, no volume, no external indicators):
  - Prior-day OHLC (high, low, close)
  - CPR levels (pivot, bc, tc) from calculate_cpr()
  - Camarilla levels (r3, r4, s3, s4) from calculate_camarilla_pivots()
  - Prior-day compression state from CompressionState (optional)
  - Prior-day 15m ATR value for normalisation

Output keys:
  sentiment        : "BULLISH" | "BEARISH" | "NEUTRAL"
  confidence       : float 0-100
  preferred_side   : "CALL" | "PUT" | None
  day_type_pred    : "TRENDING" | "RANGE" | "NEUTRAL"
  threshold_adj    : int   — add to entry scoring threshold for the day
  max_hold_adj     : int   — add to MAX_HOLD bars for the day
  camarilla_bias   : "ABOVE_R3" | "BELOW_S3" | "IN_RANGE" | "ABOVE_R4" | "BELOW_S4"
  balance_zone_pos : "ABOVE_VAH" | "BELOW_VAL" | "IN_ZONE"
  opening_gap_pred : "GAP_UP" | "GAP_DOWN" | "FLAT" | "UNKNOWN"
  reasons          : list[str]

All functions are pure (no side effects, no global state).
Integration point: call `get_daily_sentiment()` once in do_warmup() or at the
start of paper_order() when hist_yesterday_15m is available, then pass the
result into entry_logic.check_entry_condition() as `daily_sentiment_result`.
"""

from __future__ import annotations

import logging
import numpy as np
import pandas as pd
from typing import Optional

RESET  = "\033[0m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"


def _safe_float(val) -> Optional[float]:
    try:
        v = float(val)
        return None if (v != v) else v   # NaN check
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# SUB-COMPONENTS
# ─────────────────────────────────────────────────────────────────────────────

def _score_camarilla_position(
    prev_close: float, r3, r4, s3, s4,
    prev_high: float = None, prev_low: float = None,
) -> tuple:
    """Map prior close vs Camarilla levels to a directional score.

    Includes R4/S4 reversal detection: if price tested a level intraday but
    closed back inside the range, the rejection signals a reversal bias.

    Returns (bull_pts, bear_pts, bias_tag, reasons).
    """
    bull_pts, bear_pts = 0, 0
    reasons: list = []
    bias_tag = "IN_RANGE"

    if r3 is None or s3 is None:
        logging.debug("[CAMARILLA_BIAS] levels unavailable — skipped")
        return bull_pts, bear_pts, bias_tag, ["Camarilla levels unavailable"]

    # Reversal: tested R4 intraday but closed back inside R3 → bearish rejection
    if (r4 is not None and prev_high is not None
            and prev_high > r4 and prev_close < r3):
        bear_pts += 3
        bias_tag  = "REVERSAL_FROM_R4"
        reasons.append(
            f"TESTED_R4_CLOSED_INSIDE(high={prev_high:.0f}>{r4:.0f} but "
            f"close={prev_close:.0f}<{r3:.0f}) → R4 rejection, bearish reversal bias"
        )
    # Reversal: tested S4 intraday but closed back above S3 → bullish rejection
    elif (s4 is not None and prev_low is not None
            and prev_low < s4 and prev_close > s3):
        bull_pts += 3
        bias_tag  = "REVERSAL_FROM_S4"
        reasons.append(
            f"TESTED_S4_CLOSED_INSIDE(low={prev_low:.0f}<{s4:.0f} but "
            f"close={prev_close:.0f}>{s3:.0f}) → S4 rejection, bullish reversal bias"
        )
    elif r4 is not None and prev_close > r4:
        bull_pts += 4
        bias_tag  = "ABOVE_R4"
        reasons.append(
            f"CLOSE_ABOVE_R4({prev_close:.0f}>{r4:.0f}) → strong breakout bias, CALL preferred"
        )
    elif prev_close > r3:
        bull_pts += 3
        bias_tag  = "ABOVE_R3"
        reasons.append(
            f"CLOSE_ABOVE_R3({prev_close:.0f}>{r3:.0f}) → bullish breakout day likely"
        )
    elif s4 is not None and prev_close < s4:
        bear_pts += 4
        bias_tag  = "BELOW_S4"
        reasons.append(
            f"CLOSE_BELOW_S4({prev_close:.0f}<{s4:.0f}) → strong breakdown bias, PUT preferred"
        )
    elif prev_close < s3:
        bear_pts += 3
        bias_tag  = "BELOW_S3"
        reasons.append(
            f"CLOSE_BELOW_S3({prev_close:.0f}<{s3:.0f}) → bearish breakdown day likely"
        )
    else:
        reasons.append(
            f"CLOSE_IN_CAMARILLA_RANGE({s3:.0f}–{r3:.0f}) → reversal or range day possible"
        )

    logging.debug(
        f"[CAMARILLA_BIAS] close={prev_close:.0f} r3={r3:.0f} s3={s3:.0f} "
        f"→ bias={bias_tag} bull={bull_pts} bear={bear_pts}"
    )
    return bull_pts, bear_pts, bias_tag, reasons


def _score_cpr_position(prev_close: float, pivot, bc, tc) -> tuple:
    """Map prior close vs CPR to a directional nudge.

    Returns (bull_pts, bear_pts, reasons).
    """
    bull_pts, bear_pts = 0, 0
    reasons: list = []

    if pivot is None:
        return bull_pts, bear_pts, ["CPR pivot unavailable"]

    if prev_close > pivot:
        bull_pts += 1
        reasons.append(f"CLOSE_ABOVE_PIVOT({prev_close:.0f}>{pivot:.0f})")
    elif prev_close < pivot:
        bear_pts += 1
        reasons.append(f"CLOSE_BELOW_PIVOT({prev_close:.0f}<{pivot:.0f})")

    if tc is not None and bc is not None:
        if prev_close > tc:
            bull_pts += 1
            reasons.append(f"CLOSE_ABOVE_TC({prev_close:.0f}>{tc:.0f}) → accepted above CPR")
        elif prev_close < bc:
            bear_pts += 1
            reasons.append(f"CLOSE_BELOW_BC({prev_close:.0f}<{bc:.0f}) → accepted below CPR")

    return bull_pts, bear_pts, reasons


def _predict_cpr_day_type(tc, bc, atr_value, prev_high, prev_low) -> tuple:
    """Classify expected day type from CPR width relative to ATR.

    Returns (day_type_pred, threshold_adj, max_hold_adj, reasons).
    """
    reasons: list = []
    day_type_pred = "NEUTRAL"
    threshold_adj = 0
    max_hold_adj  = 0

    if tc is None or bc is None:
        return day_type_pred, threshold_adj, max_hold_adj, ["CPR tc/bc unavailable"]

    cpr_width = tc - bc
    day_range = (prev_high - prev_low) if (prev_high and prev_low) else 0.0
    ref_unit  = (
        float(atr_value) if (atr_value and float(atr_value) > 0)
        else max(day_range, 1.0)
    )
    width_ratio = cpr_width / ref_unit if ref_unit > 0 else 0.0

    if width_ratio < 0.25:
        day_type_pred = "TRENDING"
        threshold_adj = -5
        max_hold_adj  = +2
        reasons.append(
            f"NARROW_CPR(width={cpr_width:.1f}, ratio={width_ratio:.2f}x ATR) "
            f"→ TRENDING day predicted, threshold –5, hold +2 bars"
        )
    elif width_ratio > 0.80:
        day_type_pred = "RANGE"
        threshold_adj = +8
        max_hold_adj  = -3
        reasons.append(
            f"WIDE_CPR(width={cpr_width:.1f}, ratio={width_ratio:.2f}x ATR) "
            f"→ RANGE/choppy day predicted, threshold +8, hold –3 bars"
        )
    else:
        reasons.append(
            f"NORMAL_CPR(width={cpr_width:.1f}, ratio={width_ratio:.2f}x ATR) → NEUTRAL day"
        )

    logging.debug(
        f"[CPR_PRECLASS] width={cpr_width:.1f} atr_ref={ref_unit:.1f} "
        f"ratio={width_ratio:.2f} → {day_type_pred} threshold_adj={threshold_adj:+d}"
    )
    return day_type_pred, threshold_adj, max_hold_adj, reasons


def _score_balance_zone(prev_high: float, prev_low: float, prev_close: float) -> tuple:
    """Infer balance zone position from prior-day range (price-based VAH/VAL).

    The middle 60% of the prior range is treated as the 'value area'.
    A close outside this zone → imbalance → directional opening expected.

    Returns (bull_pts, bear_pts, zone_pos_tag, reasons).
    """
    bull_pts, bear_pts = 0, 0
    zone_pos_tag = "IN_ZONE"
    reasons: list = []

    day_range = prev_high - prev_low
    if day_range <= 0:
        return bull_pts, bear_pts, zone_pos_tag, ["Zero day range — balance zone skipped"]

    val_proxy = prev_low  + 0.20 * day_range   # bottom of 60% acceptance zone
    vah_proxy = prev_high - 0.20 * day_range   # top of 60% acceptance zone

    if prev_close > vah_proxy:
        bull_pts    += 2
        zone_pos_tag = "ABOVE_VAH"
        reasons.append(
            f"CLOSE_ABOVE_VAH({prev_close:.0f}>{vah_proxy:.0f}) "
            f"→ price imbalance above value, continuation bullish expected"
        )
    elif prev_close < val_proxy:
        bear_pts    += 2
        zone_pos_tag = "BELOW_VAL"
        reasons.append(
            f"CLOSE_BELOW_VAL({prev_close:.0f}<{val_proxy:.0f}) "
            f"→ price imbalance below value, continuation bearish expected"
        )
    else:
        reasons.append(
            f"CLOSE_IN_VALUE_AREA({val_proxy:.0f}–{vah_proxy:.0f}) "
            f"→ balanced, range behaviour likely at open"
        )

    logging.debug(
        f"[BALANCE_ZONE] close={prev_close:.0f} vah={vah_proxy:.0f} val={val_proxy:.0f} "
        f"→ position={zone_pos_tag} bull={bull_pts} bear={bear_pts}"
    )
    return bull_pts, bear_pts, zone_pos_tag, reasons


def _predict_opening_gap(
    compression_state_at_close: str,
    camarilla_bias: str,
    bull_pts: int,
    bear_pts: int,
) -> tuple:
    """Predict whether opening will gap/momentum based on compression + Camarilla.

    Returns (opening_gap_pred, bull_adj, bear_adj, reasons).
    """
    reasons: list = []
    bull_adj, bear_adj = 0, 0
    opening_gap_pred = "FLAT"

    if compression_state_at_close == "ENERGY_BUILDUP":
        reasons.append(
            "COMPRESSION_AT_CLOSE → explosive opening bar or gap likely"
        )
        if camarilla_bias in ("ABOVE_R3", "ABOVE_R4"):
            opening_gap_pred = "GAP_UP"
            bull_adj += 2
            reasons.append("Camarilla bias BULLISH → GAP_UP prediction")
        elif camarilla_bias in ("BELOW_S3", "BELOW_S4"):
            opening_gap_pred = "GAP_DOWN"
            bear_adj += 2
            reasons.append("Camarilla bias BEARISH → GAP_DOWN prediction")
        else:
            opening_gap_pred = "UNKNOWN"
            reasons.append("Camarilla IN_RANGE → gap direction uncertain")
    else:
        reasons.append(
            f"NO_COMPRESSION_AT_CLOSE(state={compression_state_at_close}) "
            f"→ normal open expected"
        )

    return opening_gap_pred, bull_adj, bear_adj, reasons


# ─────────────────────────────────────────────────────────────────────────────
# P5-A  OPEN=HIGH / OPEN=LOW BIAS
# ─────────────────────────────────────────────────────────────────────────────

def _score_open_position(
    today_open: float,
    today_high: float,
    today_low: float,
    tolerance: float = 0.5,
) -> tuple:
    """Detect Open=High or Open=Low intraday condition at market open.

    Open=High (today_open ≥ today_high − tolerance):
        Price opened at or very near the session high → sellers in control
        from tick-one → bearish.  bear_pts += 3, tag = ``OPEN_HIGH``.

    Open=Low (today_open ≤ today_low + tolerance):
        Price opened at or very near the session low → buyers in control
        from tick-one → bullish.  bull_pts += 3, tag = ``OPEN_LOW``.

    Both conditions are mutually exclusive (checked in priority order).

    Parameters
    ----------
    today_open  : Opening price of the first intraday candle.
    today_high  : High of that candle (typically 3m ORB high).
    today_low   : Low of that candle (typically 3m ORB low).
    tolerance   : Price tolerance in index points (default 0.5 for NIFTY50).

    Returns
    -------
    (bull_pts, bear_pts, open_bias_tag, reasons)
    """
    bull_pts, bear_pts = 0, 0
    open_bias_tag = "NONE"
    reasons: list = []

    if today_open is None or today_high is None or today_low is None:
        reasons.append("Open position data unavailable — skipped")
        return bull_pts, bear_pts, open_bias_tag, reasons

    if today_open >= today_high - tolerance:
        bear_pts += 3
        open_bias_tag = "OPEN_HIGH"
        reasons.append(
            f"OPEN_HIGH: open={today_open:.2f}≈high={today_high:.2f} "
            f"→ bearish price rejection from opening high"
        )
    elif today_open <= today_low + tolerance:
        bull_pts += 3
        open_bias_tag = "OPEN_LOW"
        reasons.append(
            f"OPEN_LOW: open={today_open:.2f}≈low={today_low:.2f} "
            f"→ bullish price rejection from opening low"
        )
    else:
        reasons.append(
            f"OPEN_NEUTRAL: open={today_open:.2f} "
            f"high={today_high:.2f} low={today_low:.2f} → no strong open bias"
        )

    logging.info(
        f"{CYAN}[OPEN_POSITION] tag={open_bias_tag} "
        f"open={today_open:.2f} high={today_high:.2f} low={today_low:.2f} "
        f"tol={tolerance:.2f} bull={bull_pts} bear={bear_pts}{RESET}"
    )
    return bull_pts, bear_pts, open_bias_tag, reasons


def get_open_position_bias(
    today_open: float,
    today_high: float,
    today_low: float,
    tolerance: float = 0.5,
) -> dict:
    """Compute intraday open=high / open=low bias from the first session candle.

    Call once after the first 3m candle of the session is complete
    (i.e. at ~09:18 IST for NIFTY50 3m bars).  Pass the result into
    ``check_entry_condition()`` via ``indicators["open_bias"]``.

    Returns
    -------
    dict with keys:
        open_bias       : "OPEN_HIGH" | "OPEN_LOW" | "NONE"
        bull_pts        : int
        bear_pts        : int
        preferred_side  : "PUT" | "CALL" | None
        reasons         : list[str]
    """
    bull_pts, bear_pts, open_bias_tag, reasons = _score_open_position(
        today_open, today_high, today_low, tolerance
    )
    preferred_side = (
        "PUT"  if open_bias_tag == "OPEN_HIGH" else
        "CALL" if open_bias_tag == "OPEN_LOW"  else
        None
    )
    return {
        "open_bias":      open_bias_tag,
        "bull_pts":       bull_pts,
        "bear_pts":       bear_pts,
        "preferred_side": preferred_side,
        "reasons":        reasons,
    }


# ─────────────────────────────────────────────────────────────────────────────
# P5-B  OPEN vs PREVIOUS CLOSE
# ─────────────────────────────────────────────────────────────────────────────

def _score_open_vs_prev_close(
    today_open: float,
    prev_close: float,
    tolerance: float = 0.5,
) -> tuple:
    """Compare today's open to the previous session close.

    Returns (bull_pts, bear_pts, vs_close_tag, reasons).
    """
    bull_pts, bear_pts = 0, 0
    reasons: list = []

    if today_open is None or prev_close is None:
        reasons.append("Open vs prev-close data unavailable — skipped")
        return bull_pts, bear_pts, "OPEN_CLOSE_EQUAL", reasons

    diff = today_open - prev_close
    if abs(diff) <= tolerance:
        vs_close_tag = "OPEN_CLOSE_EQUAL"
        reasons.append(
            f"OPEN_CLOSE_EQUAL: open={today_open:.2f} prev_close={prev_close:.2f} "
            f"diff={diff:+.2f} ≤ tol={tolerance:.2f} → neutral"
        )
    elif diff > 0:
        bull_pts += 2
        vs_close_tag = "OPEN_ABOVE_CLOSE"
        reasons.append(
            f"OPEN_ABOVE_CLOSE: open={today_open:.2f} > prev_close={prev_close:.2f} "
            f"diff={diff:+.2f} → bullish open"
        )
    else:
        bear_pts += 2
        vs_close_tag = "OPEN_BELOW_CLOSE"
        reasons.append(
            f"OPEN_BELOW_CLOSE: open={today_open:.2f} < prev_close={prev_close:.2f} "
            f"diff={diff:+.2f} → bearish open"
        )

    logging.info(
        f"{CYAN}[{vs_close_tag}] open={today_open:.2f} prev_close={prev_close:.2f} "
        f"diff={diff:+.2f} bull={bull_pts} bear={bear_pts}{RESET}"
    )
    return bull_pts, bear_pts, vs_close_tag, reasons


# ─────────────────────────────────────────────────────────────────────────────
# P5-C  GAP vs PREVIOUS HIGH / LOW
# ─────────────────────────────────────────────────────────────────────────────

def _score_gap(
    today_open: float,
    prev_high: float,
    prev_low: float,
) -> tuple:
    """Detect true gap-up / gap-down vs the previous session range.

    Returns (bull_pts, bear_pts, gap_tag, reasons).
    """
    bull_pts, bear_pts = 0, 0
    reasons: list = []

    if today_open is None or prev_high is None or prev_low is None:
        reasons.append("Gap data unavailable — skipped")
        return bull_pts, bear_pts, "NO_GAP", reasons

    if today_open > prev_high:
        bull_pts += 3
        gap_tag = "GAP_UP"
        reasons.append(
            f"GAP_UP: open={today_open:.2f} > prev_high={prev_high:.2f} "
            f"→ bullish gap continuation"
        )
    elif today_open < prev_low:
        bear_pts += 3
        gap_tag = "GAP_DOWN"
        reasons.append(
            f"GAP_DOWN: open={today_open:.2f} < prev_low={prev_low:.2f} "
            f"→ bearish gap continuation"
        )
    else:
        gap_tag = "NO_GAP"
        reasons.append(
            f"NO_GAP: open={today_open:.2f} within prev range "
            f"[{prev_low:.2f}–{prev_high:.2f}]"
        )

    logging.info(
        f"{CYAN}[{gap_tag}] open={today_open:.2f} "
        f"prev_high={prev_high:.2f} prev_low={prev_low:.2f} "
        f"bull={bull_pts} bear={bear_pts}{RESET}"
    )
    return bull_pts, bear_pts, gap_tag, reasons


# ─────────────────────────────────────────────────────────────────────────────
# P5-D  OPEN vs CPR BALANCE ZONE
# ─────────────────────────────────────────────────────────────────────────────

def _score_balance_zone_open(
    today_open: float,
    cpr_bc: float,
    cpr_tc: float,
) -> tuple:
    """Detect if today's open is inside the CPR balance zone (BC–TC).

    Inside CPR → BALANCE_OPEN → range day expected, neutral bias.
    Outside CPR → OUTSIDE_BALANCE → trending bias.
    Both return 0 pts; the tag is used for dampening in entry_logic.

    Returns (bull_pts, bear_pts, balance_tag, reasons).
    """
    bull_pts, bear_pts = 0, 0
    reasons: list = []

    if today_open is None or cpr_bc is None or cpr_tc is None:
        reasons.append("Balance zone data unavailable — skipped")
        return bull_pts, bear_pts, "OUTSIDE_BALANCE", reasons

    zone_low  = min(cpr_bc, cpr_tc)
    zone_high = max(cpr_bc, cpr_tc)

    if zone_low <= today_open <= zone_high:
        balance_tag = "BALANCE_OPEN"
        reasons.append(
            f"BALANCE_OPEN: open={today_open:.2f} inside CPR zone "
            f"[{zone_low:.2f}–{zone_high:.2f}] → indecisive/range day"
        )
    else:
        balance_tag = "OUTSIDE_BALANCE"
        reasons.append(
            f"OUTSIDE_BALANCE: open={today_open:.2f} outside CPR zone "
            f"[{zone_low:.2f}–{zone_high:.2f}] → trending bias"
        )

    logging.info(
        f"{CYAN}[{balance_tag}] open={today_open:.2f} "
        f"bc={cpr_bc:.2f} tc={cpr_tc:.2f}{RESET}"
    )
    return bull_pts, bear_pts, balance_tag, reasons


# ─────────────────────────────────────────────────────────────────────────────
# P5  COMPREHENSIVE OPENING BIAS AGGREGATOR
# ─────────────────────────────────────────────────────────────────────────────

def get_opening_bias(
    today_open: float,
    today_high: float,
    today_low: float,
    prev_close: float,
    prev_high: float,
    prev_low: float,
    cpr_bc: float,
    cpr_tc: float,
    tolerance: float = 0.5,
) -> dict:
    """Comprehensive opening bias detection combining all P5 scorers.

    Aggregates P5-A (open position), P5-B (vs prev close), P5-C (gap),
    P5-D (balance zone open).

    Call once after the first intraday candle is complete.  The returned
    tags should be unpacked into the ``indicators`` dict before calling
    ``check_entry_condition()``:

        opening = get_opening_bias(...)
        indicators.update({
            "open_bias":    opening["open_pos_tag"],
            "gap_tag":      opening["gap_tag"],
            "vs_close_tag": opening["vs_close_tag"],
            "balance_tag":  opening["balance_tag"],
        })

    Returns
    -------
    dict with keys:
        open_pos_tag    : "OPEN_HIGH" | "OPEN_LOW" | "NONE"
        vs_close_tag    : "OPEN_ABOVE_CLOSE" | "OPEN_BELOW_CLOSE" | "OPEN_CLOSE_EQUAL"
        gap_tag         : "GAP_UP" | "GAP_DOWN" | "NO_GAP"
        balance_tag     : "BALANCE_OPEN" | "OUTSIDE_BALANCE"
        bull_pts        : int  (sum from P5-A + P5-B + P5-C)
        bear_pts        : int
        preferred_side  : "CALL" | "PUT" | None
        tags            : list[str]  (all four tag values)
        reasons         : list[str]
    """
    all_reasons: list = []
    bull_pts = bear_pts = 0

    # P5-A
    a_bull, a_bear, open_pos_tag, a_reasons = _score_open_position(
        today_open, today_high, today_low, tolerance
    )
    bull_pts += a_bull
    bear_pts += a_bear
    all_reasons.extend(a_reasons)

    # P5-B
    b_bull, b_bear, vs_close_tag, b_reasons = _score_open_vs_prev_close(
        today_open, prev_close, tolerance
    )
    bull_pts += b_bull
    bear_pts += b_bear
    all_reasons.extend(b_reasons)

    # P5-C
    c_bull, c_bear, gap_tag, c_reasons = _score_gap(
        today_open, prev_high, prev_low
    )
    bull_pts += c_bull
    bear_pts += c_bear
    all_reasons.extend(c_reasons)

    # P5-D  (0 pts — used only for dampening in entry_logic)
    _, _, balance_tag, d_reasons = _score_balance_zone_open(
        today_open, cpr_bc, cpr_tc
    )
    all_reasons.extend(d_reasons)

    if bull_pts > bear_pts:
        preferred_side = "CALL"
    elif bear_pts > bull_pts:
        preferred_side = "PUT"
    else:
        preferred_side = None

    logging.info(
        f"{CYAN}[OPENING_BIAS] pos={open_pos_tag} vs_close={vs_close_tag} "
        f"gap={gap_tag} balance={balance_tag} "
        f"bull={bull_pts} bear={bear_pts} preferred={preferred_side}{RESET}"
    )

    return {
        "open_pos_tag":   open_pos_tag,
        "vs_close_tag":   vs_close_tag,
        "gap_tag":        gap_tag,
        "balance_tag":    balance_tag,
        "bull_pts":       bull_pts,
        "bear_pts":       bear_pts,
        "preferred_side": preferred_side,
        "tags":           [open_pos_tag, vs_close_tag, gap_tag, balance_tag],
        "reasons":        all_reasons,
    }


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────

def get_daily_sentiment(
    prev_high: float,
    prev_low: float,
    prev_close: float,
    cpr_levels: dict,
    camarilla_levels: dict,
    compression_state_at_close: str = "NEUTRAL",
    atr_value: float = None,
) -> dict:
    """Compute pre-session directional sentiment from prior-day price structure.

    Call once at the start of each session (before entries are evaluated).
    Pass the result into check_entry_condition() as `daily_sentiment_result`.

    Parameters
    ----------
    prev_high, prev_low, prev_close:
        Prior trading session OHLC for the underlying index (e.g. NIFTY50).
    cpr_levels:
        Output of indicators.calculate_cpr() — keys: "pivot", "bc", "tc".
    camarilla_levels:
        Output of indicators.calculate_camarilla_pivots() — keys: r3, r4, s3, s4.
    compression_state_at_close:
        The `market_state` attribute of CompressionState at prior-day close.
        Accepted: "NEUTRAL" | "ENERGY_BUILDUP" | "VOLATILITY_EXPANSION".
    atr_value:
        Prior-day 15m ATR(14).  Used for CPR width normalisation.
        Pass None to fall back to prior-day range as the reference unit.

    Returns
    -------
    dict — see module docstring for full key list.
    """
    all_reasons: list = []
    bull_pts = 0
    bear_pts = 0

    prev_high  = _safe_float(prev_high)  or 0.0
    prev_low   = _safe_float(prev_low)   or 0.0
    prev_close = _safe_float(prev_close) or 0.0

    r3    = _safe_float(camarilla_levels.get("r3"))
    r4    = _safe_float(camarilla_levels.get("r4"))
    s3    = _safe_float(camarilla_levels.get("s3"))
    s4    = _safe_float(camarilla_levels.get("s4"))
    pivot = _safe_float(cpr_levels.get("pivot"))
    bc    = _safe_float(cpr_levels.get("bc"))
    tc    = _safe_float(cpr_levels.get("tc"))

    # 1. CPR day-type prediction
    day_type_pred, dt_threshold_adj, dt_max_hold_adj, dt_reasons = _predict_cpr_day_type(
        tc, bc, atr_value, prev_high, prev_low
    )
    all_reasons.extend(dt_reasons)

    # 2. Camarilla directional bias
    cam_bull, cam_bear, camarilla_bias, cam_reasons = _score_camarilla_position(
        prev_close, r3, r4, s3, s4,
        prev_high=prev_high, prev_low=prev_low,
    )
    bull_pts += cam_bull
    bear_pts += cam_bear
    all_reasons.extend(cam_reasons)

    # 3. CPR position
    cpr_bull, cpr_bear, cpr_reasons = _score_cpr_position(prev_close, pivot, bc, tc)
    bull_pts += cpr_bull
    bear_pts += cpr_bear
    all_reasons.extend(cpr_reasons)

    # 4. Balance zone
    bz_bull, bz_bear, balance_zone_pos, bz_reasons = _score_balance_zone(
        prev_high, prev_low, prev_close
    )
    bull_pts += bz_bull
    bear_pts += bz_bear
    all_reasons.extend(bz_reasons)

    # 5. Opening gap / compression prediction
    opening_gap_pred, gap_bull, gap_bear, gap_reasons = _predict_opening_gap(
        compression_state_at_close, camarilla_bias, bull_pts, bear_pts
    )
    bull_pts += gap_bull
    bear_pts += gap_bear
    all_reasons.extend(gap_reasons)

    # 6. Determine net sentiment
    total_pts = bull_pts + bear_pts
    if total_pts == 0 or abs(bull_pts - bear_pts) < 2:
        sentiment      = "NEUTRAL"
        preferred_side = None
        confidence     = 0.0 if total_pts == 0 else 50.0
    elif bull_pts > bear_pts:
        sentiment      = "BULLISH"
        preferred_side = "CALL"
        confidence     = round(100.0 * bull_pts / total_pts, 1)
    else:
        sentiment      = "BEARISH"
        preferred_side = "PUT"
        confidence     = round(100.0 * bear_pts / total_pts, 1)

    # 7. Merge threshold/hold adjustments
    # Add extra caution penalty on NEUTRAL sentiment days
    threshold_adj = dt_threshold_adj + (3 if sentiment == "NEUTRAL" else 0)
    max_hold_adj  = dt_max_hold_adj

    result = {
        "sentiment":               sentiment,
        "confidence":              confidence,
        "preferred_side":          preferred_side,
        "day_type_pred":           day_type_pred,
        "threshold_adj":           threshold_adj,
        "max_hold_adj":            max_hold_adj,
        "camarilla_bias":          camarilla_bias,
        "balance_zone_pos":        balance_zone_pos,
        "opening_gap_pred":        opening_gap_pred,
        "bullish_pts":             bull_pts,
        "bearish_pts":             bear_pts,
        "reasons":                 all_reasons,
    }

    logging.info(
        f"{CYAN}[DAILY_SENTIMENT] "
        f"sentiment={sentiment} confidence={confidence:.0f}% "
        f"preferred_side={preferred_side} "
        f"day_type={day_type_pred} "
        f"cam_bias={camarilla_bias} "
        f"balance={balance_zone_pos} "
        f"gap_pred={opening_gap_pred} "
        f"threshold_adj={threshold_adj:+d} max_hold_adj={max_hold_adj:+d} "
        f"bull={bull_pts} bear={bear_pts}{RESET}"
    )
    return result


def get_daily_sentiment_from_candles(
    df_15m_yesterday: pd.DataFrame,
    cpr_levels: dict,
    camarilla_levels: dict,
    compression_state_at_close: str = "NEUTRAL",
) -> dict:
    """Convenience wrapper: extract OHLC + ATR from a 15m DataFrame.

    Parameters
    ----------
    df_15m_yesterday:
        Complete prior-day 15m candle DataFrame.  Must have columns:
        open, high, low, close, atr14.
    cpr_levels, camarilla_levels, compression_state_at_close:
        Same as get_daily_sentiment().
    """
    if df_15m_yesterday is None or df_15m_yesterday.empty:
        logging.warning(
            "[DAILY_SENTIMENT] No prior-day 15m data — returning NEUTRAL sentiment"
        )
        return {
            "sentiment": "NEUTRAL", "confidence": 0.0, "preferred_side": None,
            "day_type_pred": "NEUTRAL", "threshold_adj": 0, "max_hold_adj": 0,
            "camarilla_bias": "IN_RANGE", "balance_zone_pos": "IN_ZONE",
            "opening_gap_pred": "UNKNOWN", "bullish_pts": 0, "bearish_pts": 0,
            "reasons": ["No prior-day data"],
        }

    prev_high  = float(df_15m_yesterday["high"].max())
    prev_low   = float(df_15m_yesterday["low"].min())
    prev_close = float(df_15m_yesterday["close"].iloc[-1])
    atr_value  = (
        float(df_15m_yesterday["atr14"].iloc[-1])
        if "atr14" in df_15m_yesterday.columns
        and pd.notna(df_15m_yesterday["atr14"].iloc[-1])
        else None
    )

    return get_daily_sentiment(
        prev_high=prev_high,
        prev_low=prev_low,
        prev_close=prev_close,
        cpr_levels=cpr_levels,
        camarilla_levels=camarilla_levels,
        compression_state_at_close=compression_state_at_close,
        atr_value=atr_value,
    )


__all__ = [
    "get_daily_sentiment",
    "get_daily_sentiment_from_candles",
    "get_open_position_bias",
    "get_opening_bias",
    "_score_open_vs_prev_close",
    "_score_gap",
    "_score_balance_zone_open",
]
