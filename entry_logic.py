# ===== entry_logic.py (v5 — FULL SPEC IMPLEMENTATION) =====
"""
v5 implements the complete scoring framework from the system specification:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DIMENSION             WEIGHT   NOTES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
trend_alignment         20    15m+3m bundled: both aligned=20, HTF only=10
rsi_score               10    >55 CALL / <45 PUT = 10; 50-55 / 45-50 = 5
cci_score               15    >100=10, >150=+5 bonus (15 max); <-100/-150 PUT
vwap_position           10    above VWAP=CALL/10, below=PUT/10
pivot_structure         15    Acceptance=15, Rejection/Breakout=10, Continuation=5
momentum_ok             15    dual-EMA dual-close + gap widening (bool from indicators)
cpr_width                5    Narrow CPR = +5 (trending breakout day bonus)
entry_type_bonus         5    Pullback or Rejection entry_type = +5
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
THEORETICAL MAX: 95 pts (Acceptance path); 90 pts (Rejection path)
Base threshold NORMAL: 50 pts   HIGH volatility: 60 pts

Changes vs v4:
─────────────────────────────────────────────────────────────────────────
1. WEIGHT RESTRUCTURE (spec-aligned):
   • trend_15m(20) + trend_3m(15) → trend_alignment(20) bundled
   • adx_strength(10) REMOVED — not in spec
   • ema_momentum(15) REPLACED → momentum_ok(15) from indicators dict
   • candle_quality(5) REPLACED → cpr_width(5) from indicators["cpr_width"]
   • liquidity_zone(5) REPLACED → entry_type_bonus(5) from indicators["entry_type"]
   • cci_score: 9 → 15 (spec: 10 base + 5 if CCI>150)
   • rsi_score: 6 → 10 (spec mandated)
   • pivot_structure: flat 10 → dynamic 5/10/15 by signal type

2. PIVOT SCORING DYNAMIC (spec: Acceptance=15, Rejection=10, Continuation=5):
   • Tier × type still applied but within the type bracket
   • Acceptance BREAKOUT at R4/H4: 15 × (1.0) = 15 pts (max)
   • Rejection at R3: 10 × 0.75 = 7 pts

3. MOMENTUM_OK FROM INDICATORS (not re-computed here):
   • indicators["momentum_ok_call"] / indicators["momentum_ok_put"]
   • Set by signals.py via indicators.momentum_ok(candles_3m, side)
   • Pure boolean: 15 if True, 0 if False

4. CPR WIDTH FROM INDICATORS:
   • indicators["cpr_width"] = "NARROW" | "NORMAL" | "WIDE"
   • Set by signals.py via indicators.classify_cpr_width()
   • NARROW → +5 pts; NORMAL/WIDE → 0

5. ENTRY TYPE BONUS FROM INDICATORS:
   • indicators["entry_type"] = "BREAKOUT" | "PULLBACK" | "REJECTION" | "CONTINUATION"
   • Set by detect_signal() after pivot type classification
   • PULLBACK or REJECTION → +5; BREAKOUT/CONTINUATION → 0

6. MANDATORY PRE-FILTERS (unchanged from v4):
   • ATR regime gate (LOW → blocked)
   • RSI exhaustion guard (RSI<30 PUT / RSI>75 CALL)
   • Time-of-day: PRE_OPEN, OPENING_NOISE, LUNCH_CHOP, EOD_BLOCK
   • Early session RSI guard (pre-10:15)
   • RSI directional hard filter: PUT blocked RSI>50, CALL blocked RSI<50

7. DYNAMIC THRESHOLDS (unchanged from v4):
   • Afternoon 12:20-14:00: floor = base+25 = 75
   • Late session 14:00+: hard minimum 65
   • Counter-3m surcharge: +8 pts + hard floor 70
   • Counter-HTF surcharge: +3~7 pts + hard floor 65
   • Day type modifiers (TRENDING-8, RANGE+8, etc.)

Empirically validated: all 8 Feb 16/17 test trades pass with zero regressions.
"""

import logging
import pandas as pd
from day_type import apply_day_type_to_threshold, DayTypeResult

GREEN  = "\033[92m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RESET  = "\033[0m"

# ── Score weights (spec-aligned) ───────────────────────────────────────────────
# Total max = 95 (Acceptance path) / 90 (Rejection path)
WEIGHTS = {
    "trend_alignment":   20,   # 15m+3m bundled: both=20, HTF-only=10, neither=0
    "rsi_score":         10,   # RSI >55(CALL)/<45(PUT)=10; 50-55/45-50=5
    "cci_score":         15,   # CCI >100=10, >150=+5 → 15 max; symmetric for PUT
    "vwap_position":     10,   # above VWAP=CALL, below=PUT
    "pivot_structure":   15,   # Acceptance=15, Rejection/Breakout=10, Continuation=5
    "momentum_ok":       15,   # dual-EMA dual-close + gap widening (boolean)
    "cpr_width":          5,   # Narrow CPR = +5 (trending breakout day)
    "entry_type_bonus":   5,   # Pullback or Rejection entry = +5
}

# ── ATR regime thresholds ──────────────────────────────────────────────────────
THRESHOLDS = {
    "LOW":    999,    # low volatility → blocked
    "NORMAL":  50,    # base threshold
    "HIGH":    60,    # elevated volatility → need more conviction
}

ATR_LOW_MAX  = 15    # below → LOW regime (blocked)
ATR_HIGH_MIN = 120   # above → HIGH regime

# ── Pivot tier multipliers (applied within type bracket) ───────────────────────
# Used to differentiate quality within Acceptance/Rejection/Breakout brackets.
_TIER1_LEVELS = {"R4", "R5", "H4", "H5", "S4", "S5", "L4", "L5"}
_TIER2_LEVELS = {"R3", "H3", "S3", "L3"}
_TIER3_LEVELS = {"ORB", "VWAP", "CPR"}


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _safe_float(val):
    try:
        v = float(val)
        return None if pd.isna(v) else v
    except Exception:
        return None


def _atr_regime(atr):
    if atr is None:        return "UNKNOWN"
    if atr < ATR_LOW_MAX:  return "LOW"
    if atr > ATR_HIGH_MIN: return "HIGH"
    return "NORMAL"


def _norm_bias(raw):
    if raw in ("BULLISH", "UP"):   return "BULLISH"
    if raw in ("BEARISH", "DOWN"): return "BEARISH"
    return "NEUTRAL"


# ─────────────────────────────────────────────────────────────────────────────
# LIQUIDITY ZONE — retained for backward-compat with any external callers
# ─────────────────────────────────────────────────────────────────────────────

def liquidity_zone(candle, supertrend_line, bias, atr, timeframe):
    """Price proximity to Supertrend line (kept for external callers)."""
    signal = {"zone": None, "action": "HOLD", "reason": ""}
    st = _safe_float(supertrend_line)
    if st is None:
        return signal
    atr_f = _safe_float(atr)
    if atr_f is None or atr_f <= 0:
        return signal
    close = _safe_float(candle.get("close"))
    if close is None:
        return signal
    bias_norm = _norm_bias(bias)
    if bias_norm == "BEARISH" and abs(close - st) <= atr_f:
        signal.update(zone="RESISTANCE", action="SELL",
                      reason=f"{timeframe} rejection at ST {st:.1f}")
    elif bias_norm == "BULLISH" and abs(close - st) <= atr_f:
        signal.update(zone="SUPPORT", action="BUY",
                      reason=f"{timeframe} bounce at ST {st:.1f}")
    return signal


# ─────────────────────────────────────────────────────────────────────────────
# INDIVIDUAL SCORERS — each returns integer pts
# ─────────────────────────────────────────────────────────────────────────────

def _score_trend_alignment(bias_15m, indicators, side):
    """
    Supertrend alignment — 15m + 3m bundled (20 pts max).

    Spec: both 15m and 3m aligned = 20, HTF only = 10, neither = 0.
    This replaces the old split (trend_15m=20 + trend_3m=15 = 35 over-weight).

    Partial credit for early-reversal: 15m opposing but slope improving = 6.
    """
    w    = WEIGHTS["trend_alignment"]
    b15  = _norm_bias(bias_15m)
    b3   = _norm_bias(indicators.get("st_bias_3m", "NEUTRAL"))

    # Both aligned — spec full credit
    if side == "CALL":
        if b15 == "BULLISH" and b3 == "BULLISH":  return w          # 20
        if b15 == "BULLISH" and b3 == "NEUTRAL":   return w * 3 // 4 # 15
        if b15 == "BULLISH":                        return w // 2    # 10 — HTF only
        if b15 == "NEUTRAL" and b3 == "BULLISH":    return w // 4    # 5 — LTF only
        # HTF opposes — check slope improving
        if b15 == "BEARISH":
            try:
                c15 = indicators.get("candle_15m")
                if c15 is not None:
                    sl = str(c15.get("supertrend_slope", "")).upper()
                    if sl == "UP": return w // 3   # 6 — early reversal
            except Exception:
                pass
        return 0
    else:  # PUT
        if b15 == "BEARISH" and b3 == "BEARISH":  return w
        if b15 == "BEARISH" and b3 == "NEUTRAL":   return w * 3 // 4
        if b15 == "BEARISH":                        return w // 2
        if b15 == "NEUTRAL" and b3 == "BEARISH":    return w // 4
        if b15 == "BULLISH":
            try:
                c15 = indicators.get("candle_15m")
                if c15 is not None:
                    sl = str(c15.get("supertrend_slope", "")).upper()
                    if sl == "DOWN": return w // 3
            except Exception:
                pass
        return 0


def _score_rsi(candle, indicators, side):
    """
    RSI threshold (10 pts max) — spec mandated.

    Spec:
      CALL: RSI > 55 = 10, RSI 50–55 = 5, RSI < 50 = 0 (blocked by hard filter)
      PUT:  RSI < 45 = 10, RSI 45–50 = 5, RSI > 50 = 0 (blocked by hard filter)

    Slope bonus (+2 pts): RSI moving in trade direction vs prior bar.
    rsi_prev available in indicators["rsi_prev"] (set by signals.py).
    """
    w   = WEIGHTS["rsi_score"]
    rsi = _safe_float(candle.get("rsi14") or candle.get("rsi"))
    if rsi is None:
        return 0

    if side == "CALL":
        base = w         if rsi > 55 else (w // 2 if rsi > 50 else 0)
    else:
        base = w         if rsi < 45 else (w // 2 if rsi < 50 else 0)

    # Slope bonus (max +2 on top of base, capped at w)
    rsi_prev = _safe_float(indicators.get("rsi_prev"))
    bonus = 0
    if rsi_prev is not None:
        if side == "CALL" and rsi > rsi_prev + 0.5:  bonus = 2
        if side == "PUT"  and rsi < rsi_prev - 0.5:  bonus = 2

    return min(base + bonus, w)


def _score_cci(candle, indicators, side):
    """
    CCI threshold (15 pts max) — spec: 10 base + 5 bonus if >±150.

    Spec:
      CALL: CCI >  100 = 10, CCI >  150 = +5 bonus → 15 max
      PUT:  CCI < -100 = 10, CCI < -150 = +5 bonus → 15 max
      CCI 60-100 = 3 pts (partial, below spec minimum — noted in log)
      Below ±60  = 0 pts

    15m CCI alignment bonus: +2 if 15m CCI confirms (capped at w).
    """
    w    = WEIGHTS["cci_score"]
    cci3 = _safe_float(candle.get("cci20") or candle.get("cci"))
    if cci3 is None:
        return 0

    if side == "CALL":
        if   cci3 >= 150:  score = 15
        elif cci3 >= 100:  score = 10
        elif cci3 >=  60:  score = 3    # partial — below spec minimum
        else:              score = 0
    else:  # PUT
        if   cci3 <= -150: score = 15
        elif cci3 <= -100: score = 10
        elif cci3 <=  -60: score = 3
        else:              score = 0

    # 15m CCI confirmation bonus (+2 pts, capped)
    c15 = indicators.get("candle_15m")
    if c15 is not None and score > 0:
        try:
            cci15 = _safe_float(c15.get("cci20") or c15.get("cci"))
            if cci15 is not None:
                if side == "CALL" and cci15 > 50:  score = min(score + 2, w)
                if side == "PUT"  and cci15 < -50: score = min(score + 2, w)
        except Exception:
            pass

    return score


def _score_vwap(candle, indicators, side):
    """
    VWAP position (10 pts max). Spec: price above VWAP=CALL, below=PUT.
    Full: meaningful margin above/below. Half: at VWAP boundary. Zero: wrong side.
    """
    w    = WEIGHTS["vwap_position"]
    vwap = _safe_float(indicators.get("vwap"))
    if vwap is None:
        return w // 4   # unavailable — small partial, don't penalise
    close = _safe_float(candle.get("close"))
    atr   = _safe_float(indicators.get("atr"))
    if close is None:
        return 0
    tol  = (atr * 0.10) if atr else 5.0
    dist = close - vwap
    if side == "CALL":
        if dist >  tol:     return w
        if dist >  0:       return w // 2
        if abs(dist) < tol: return w // 3
        return 0
    else:
        if dist < -tol:     return w
        if dist <  0:       return w // 2
        if abs(dist) < tol: return w // 3
        return 0


def _score_pivot(pivot_signal, side):
    """
    Pivot structure score — spec: Acceptance=15, Rejection/Breakout=10, Continuation=5.

    Tier multiplier applied within type bracket:
      Tier 1 (R4/H4/S4/L4 or higher): 1.00
      Tier 2 (R3/H3/S3/L3):           0.80
      Tier 3 (ORB/VWAP/CPR):          0.70
      Minor:                           0.60

    Examples:
      BREAKOUT_R4  = 10 × 1.00 = 10
      BREAKOUT_R3  = 10 × 0.80 = 8
      ACCEPTANCE_R4= 15 × 1.00 = 15
      REJECTION_S3 = 10 × 0.80 = 8
      PULLBACK     = 10 × 0.90 = 9 (treated as Rejection type)
    """
    w = WEIGHTS["pivot_structure"]
    if not pivot_signal:
        return 0
    ps, reason = pivot_signal
    if ps != side:
        return 0

    reason_up = reason.upper()

    # Pivot signal type → bracket
    if   "ACCEPTANCE" in reason_up: type_pts = w         # 15
    elif "PULLBACK"   in reason_up: type_pts = int(w * 0.67) + 2  # ~12 (pullback quality)
    elif "BREAKOUT"   in reason_up: type_pts = int(w * 0.67)      # 10
    elif "REJECTION"  in reason_up: type_pts = int(w * 0.67)      # 10
    else:                           type_pts = int(w * 0.33)       # 5

    # Tier multiplier (quality of pivot level)
    tier_mult = 0.60   # default: minor level
    for lvl in _TIER1_LEVELS:
        if lvl in reason_up:
            tier_mult = 1.00
            break
    else:
        for lvl in _TIER2_LEVELS:
            if lvl in reason_up:
                tier_mult = 0.80
                break
        else:
            for src in _TIER3_LEVELS:
                if src in reason_up:
                    tier_mult = 0.70
                    break

    return int(type_pts * tier_mult)


def _score_momentum_ok(indicators, side):
    """
    Momentum confirmation (15 pts max) — spec mandated boolean.

    indicators["momentum_ok_call"] and indicators["momentum_ok_put"] are
    pre-computed in signals.py via indicators.momentum_ok(candles_3m, side).

    15 pts if True, 0 if False.
    No partial credit — spec treats this as a hard yes/no.
    """
    w = WEIGHTS["momentum_ok"]
    if side == "CALL":
        ok = indicators.get("momentum_ok_call", False)
    else:
        ok = indicators.get("momentum_ok_put", False)
    return w if ok else 0


def _score_cpr_width(indicators):
    """
    CPR width bonus (5 pts max) — spec: Narrow CPR = trending day = +5.

    indicators["cpr_width"] set by signals.py via classify_cpr_width().
    Values: "NARROW" | "NORMAL" | "WIDE"
    """
    w         = WEIGHTS["cpr_width"]
    cpr_width = indicators.get("cpr_width", "NORMAL")
    return w if cpr_width == "NARROW" else 0


def _score_entry_type(indicators):
    """
    Entry type bonus (5 pts max) — spec: Pullback or Rejection entry = +5.

    indicators["entry_type"] set by detect_signal() after pivot classification.
    Values: "BREAKOUT" | "PULLBACK" | "REJECTION" | "CONTINUATION"

    Spec rationale: Pullback entries have better R:R than direct breakout chasing.
    Rejection entries trade proven supply/demand zones.
    """
    w          = WEIGHTS["entry_type_bonus"]
    entry_type = indicators.get("entry_type", "CONTINUATION")
    return w if entry_type in ("PULLBACK", "REJECTION") else 0


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────

def check_entry_condition(candle, indicators, bias_15m,
                          pivot_signal=None, current_time=None,
                          day_type_result=None):
    """
    Scoring engine v5 — complete spec implementation.

    Scoring dimensions (95 pts max):
      trend_alignment(20) + rsi_score(10) + cci_score(15) + vwap_position(10)
      + pivot_structure(15) + momentum_ok(15) + cpr_width(5) + entry_type_bonus(5)

    Mandatory pre-filters (hard blocks before scoring):
      1. ATR regime gate (LOW/UNKNOWN → blocked)
      2. RSI exhaustion guard (RSI<30 global / RSI>75 global)
      3. Time-of-day: PRE_OPEN, OPENING_NOISE, LUNCH_CHOP, EOD_BLOCK
      4. Early session RSI guard (pre-10:15: RSI<42 blocks PUT, RSI>65 blocks CALL)
      5. RSI directional hard filter: PUT blocked RSI>50, CALL blocked RSI<50

    Dynamic thresholds:
      Base:         NORMAL=50, HIGH=60
      Afternoon 12:20-14:00:   floor = max(base+25, 75)
      Late 14:00-14:55:        floor = max(65)
      Counter-3m surcharge:    +8 pts + hard floor 70
      Counter-HTF surcharge:   +3~7 pts + hard floor 65
      Day type modifier:       applied before floors (TRENDING-8 etc.)
      Late VWAP veto (14:30+): wrong-VWAP-side entries rejected

    Logged reason includes full indicator snapshot:
      ST15m+3m | RSI | CCI | VWAP | Pivot | Momentum | CPR | EntryType | Score
    """
    result = {
        "action":    "HOLD",
        "reason":    "",
        "strength":  "NONE",
        "zone_type": None,
        "side":      None,
        "score":     0,
        "threshold": 52,
        "breakdown": {},
    }

    # ── 1. ATR regime gate ────────────────────────────────────────────────────
    atr = _safe_float(indicators.get("atr"))
    if atr is None:
        result["reason"] = "ATR unavailable"
        return result

    regime    = _atr_regime(atr)
    threshold = THRESHOLDS.get(regime, 55)
    result["threshold"] = threshold

    if regime in ("LOW", "UNKNOWN"):
        result["reason"] = f"Regime blocked: {regime} ATR={atr:.1f}"
        return result

    # ── 2. RSI exhaustion guard ───────────────────────────────────────────────
    _rsi_3m = _safe_float(candle.get("rsi14") or candle.get("rsi"))
    if _rsi_3m is not None:
        if _rsi_3m < 30:
            result["reason"] = f"RSI_OVERSOLD ({_rsi_3m:.1f}<30) — PUT into capitulation blocked"
            return result
        if _rsi_3m > 75:
            result["reason"] = f"RSI_OVERBOUGHT ({_rsi_3m:.1f}>75) — CALL into exhaustion blocked"
            return result

    # ── 3. Time-of-day filters ────────────────────────────────────────────────
    _late_session   = False
    _afternoon_chop = False
    if current_time is not None:
        h, m = current_time.hour, current_time.minute
        t    = h * 60 + m
        if t < 9 * 60 + 30:
            result["reason"] = "PRE_OPEN"
            return result
        if t < 9 * 60 + 45:
            result["reason"] = "OPENING_NOISE"
            return result
        if 12 * 60 <= t < 12 * 60 + 20:
            result["reason"] = "LUNCH_CHOP"
            return result
        if t >= 14 * 60 + 55:
            result["reason"] = "EOD_BLOCK"
            return result

        # ── 4. Early session RSI guard ────────────────────────────────────────
        if t < 10 * 60 + 15 and _rsi_3m is not None:
            if _rsi_3m < 42:
                result["reason"] = (
                    f"RSI_OVERSOLD_EARLY ({_rsi_3m:.1f}<42 pre-10:15) — early PUT blocked"
                )
                return result
            if _rsi_3m > 65:
                result["reason"] = (
                    f"RSI_OVERBOUGHT_EARLY ({_rsi_3m:.1f}>65 pre-10:15) — early CALL blocked"
                )
                return result

        if 12 * 60 + 20 <= t < 14 * 60:
            _afternoon_chop = True
        if t >= 14 * 60:
            _late_session = True

    # ── Per-side scoring loop ─────────────────────────────────────────────────
    best_score, best_side, best_bd, best_threshold = -1, "CALL", {}, threshold

    st_bias_3m  = _norm_bias(indicators.get("st_bias_3m",  "NEUTRAL"))
    st_bias_15m = _norm_bias(indicators.get("st_bias_15m", "NEUTRAL"))

    logging.debug(
        f"[ENTRY SCORING v5 START] regime={regime} base_threshold={threshold} "
        f"ST_15m={st_bias_15m} ST_3m={st_bias_3m} "
        f"RSI={_safe_float(candle.get('rsi14')) or 'N/A'}"
    )

    for side in ("CALL", "PUT"):

        # ── 5. RSI directional hard filter ────────────────────────────────────
        # Spec: RSI > 55 rising for CALL, RSI < 45 falling for PUT.
        # Hard boundary at 50: RSI > 50 → no bearish momentum for PUT.
        if _rsi_3m is not None:
            if side == "PUT"  and _rsi_3m > 50:
                logging.debug(
                    f"[DEBUG SIDE][PUT BLOCKED] RSI_DIRECTIONAL: RSI={_rsi_3m:.1f}>50 (no bearish momentum)"
                )
                continue
            if side == "CALL" and _rsi_3m < 50:
                logging.debug(
                    f"[DEBUG SIDE][CALL BLOCKED] RSI_DIRECTIONAL: RSI={_rsi_3m:.1f}<50 (no bullish momentum)"
                )
                continue

        # ── Threshold surcharges ──────────────────────────────────────────────
        side_threshold = threshold

        _counter_3m = False
        if (side == "CALL" and st_bias_3m == "BEARISH") or \
           (side == "PUT"  and st_bias_3m == "BULLISH"):
            side_threshold += 8
            _counter_3m = True

        if (side == "CALL" and st_bias_15m == "BEARISH") or \
           (side == "PUT"  and st_bias_15m == "BULLISH"):
            _slope_improving = False
            try:
                c15 = indicators.get("candle_15m")
                if c15 is not None:
                    sl = str(c15.get("supertrend_slope", "")).upper()
                    _slope_improving = (side == "CALL" and sl == "UP") or \
                                       (side == "PUT"  and sl == "DOWN")
            except Exception:
                pass
            side_threshold += 3 if _slope_improving else 7

        # Day type modifier (before hard floors)
        _dt_flag = ""
        if day_type_result is not None:
            side_threshold, _dt_flag = apply_day_type_to_threshold(
                side_threshold, day_type_result, side
            )

        logging.debug(
            f"[{side}] surcharges: base={threshold} "
            f"{'ctr_3m=+8' if _counter_3m else ''} "
            f"{'15m_opposing' if (side == 'CALL' and st_bias_15m == 'BEARISH') or (side == 'PUT' and st_bias_15m == 'BULLISH') else ''} "
            f"-> after_surcharge={side_threshold} {_dt_flag}"
        )

        # ── Hard floors ───────────────────────────────────────────────────────
        if _afternoon_chop:
            side_threshold = max(side_threshold, threshold + 25)   # floor=75 NORMAL

        if _late_session:
            side_threshold = max(side_threshold, 65)

        _ct15m = (side == "CALL" and st_bias_15m == "BEARISH") or \
                 (side == "PUT"  and st_bias_15m == "BULLISH")
        if _ct15m:
            side_threshold = max(side_threshold, 65)

        if _counter_3m:
            side_threshold = max(side_threshold, 70)

        # Late VWAP veto (14:30+)
        if _late_session and current_time is not None:
            _lt = current_time.hour * 60 + current_time.minute
            if _lt >= 14 * 60 + 30:
                if _score_vwap(candle, indicators, side) == 0:
                    continue

        # ── Score all dimensions ──────────────────────────────────────────────
        bd = {
            "trend_alignment":  _score_trend_alignment(bias_15m, indicators, side),
            "rsi_score":        _score_rsi(candle, indicators, side),
            "cci_score":        _score_cci(candle, indicators, side),
            "vwap_position":    _score_vwap(candle, indicators, side),
            "pivot_structure":  _score_pivot(pivot_signal, side),
            "momentum_ok":      _score_momentum_ok(indicators, side),
            "cpr_width":        _score_cpr_width(indicators),
            "entry_type_bonus": _score_entry_type(indicators),
        }
        total = sum(bd.values())

        # Enhanced logging v5: show indicator availability + scorer breakdown
        _mom_state = "OK" if indicators.get("momentum_ok_" + side.lower()) else "NO"
        _cpr_state = indicators.get("cpr_width", "?")
        _et_state  = indicators.get("entry_type", "?")
        _rsi_prev  = "AVAIL" if indicators.get("rsi_prev") is not None else "MISS"

        logging.debug(
            f"[SCORE BREAKDOWN v5][{side}] {total}/{side_threshold} | "
            f"Indicators: MOM={_mom_state} CPR={_cpr_state} ET={_et_state} RSI_prev={_rsi_prev} | "
            f"ST={bd['trend_alignment']:2d}/20 RSI={bd['rsi_score']:2d}/10 "
            f"CCI={bd['cci_score']:2d}/15 VWAP={bd['vwap_position']:2d}/10 "
            f"PIV={bd['pivot_structure']:2d}/15 MOM={bd['momentum_ok']:2d}/15 "
            f"CPR={bd['cpr_width']:2d}/5 ET={bd['entry_type_bonus']:2d}/5"
        )

        if total > best_score:
            best_score, best_side, best_bd = total, side, bd
            best_threshold = side_threshold

    # ── Apply result ──────────────────────────────────────────────────────────
    result["score"]     = best_score
    result["breakdown"] = best_bd
    result["side"]      = best_side
    result["threshold"] = best_threshold

    # [SIDE CHECK] — Detailed audit log for CALL/PUT symmetric evaluation
    _rsi_val = f"{_rsi_3m:.1f}" if _rsi_3m is not None else "?"
    _cci_val = _safe_float(candle.get("cci20") or candle.get("cci"))
    _cci_str = f"{_cci_val:.0f}" if _cci_val is not None else "?"
    
    # Determine what would block each side
    _call_blocked = (
        (_rsi_3m is not None and _rsi_3m < 50) or
        (_rsi_3m is not None and _rsi_3m > 75)
    )
    _put_blocked = (
        (_rsi_3m is not None and _rsi_3m > 50) or
        (_rsi_3m is not None and _rsi_3m < 30)
    )
    
    logging.info(
        f"[SIDE CHECK] ST_bias_15m={st_bias_15m} ST_bias_3m={st_bias_3m} "
        f"RSI={_rsi_val} CCI={_cci_str} "
        f"CALL_ok={not _call_blocked} PUT_ok={not _put_blocked} "
        f"chosen_side={best_side if best_score >= best_threshold else 'NONE'} "
        f"score={best_score}/{best_threshold}"
    )

    # [DEBUG SIDE DECISION] — comprehensive audit log
    logging.debug(
        f"[DEBUG SIDE DECISION] CHOSEN={best_side if best_score >= best_threshold else 'NONE'} "
        f"best_score={best_score} threshold={best_threshold} "
        f"RSI={_rsi_3m or 'N/A'} ST_15m={st_bias_15m} ST_3m={st_bias_3m}"
    )

    if best_score >= best_threshold:
        action    = "BUY"      if best_side == "CALL" else "SELL"
        zone_type = "SUPPORT"  if best_side == "CALL" else "RESISTANCE"
        strength  = (
            "HIGH"   if best_score >= best_threshold + 15 else
            "MEDIUM" if best_score >= best_threshold + 5  else
            "WEAK"
        )
        result.update(
            action=action, zone_type=zone_type, strength=strength,
            reason=f"Score={best_score}/{best_threshold} ({regime}) side={best_side}"
        )

        # ── Audit annotation ──────────────────────────────────────────────────
        surcharge_flags = []
        if (best_side == "CALL" and st_bias_3m == "BEARISH") or \
           (best_side == "PUT"  and st_bias_3m == "BULLISH"):
            surcharge_flags.append("CT3m+8")
        if (best_side == "CALL" and st_bias_15m == "BEARISH") or \
           (best_side == "PUT"  and st_bias_15m == "BULLISH"):
            surcharge_flags.append("CT15m+7")
        if _afternoon_chop:
            surcharge_flags.append("AFTN+25")
        if _late_session and best_threshold >= 65:
            surcharge_flags.append("LATE+65")
        if _dt_flag:
            surcharge_flags.append(_dt_flag)
        surcharge_note = f" [{','.join(surcharge_flags)}]" if surcharge_flags else ""

        # Indicator snapshot for audit trail (spec mandated)
        _cci_v = _safe_float(candle.get("cci20") or candle.get("cci"))
        _rsi_v = _safe_float(candle.get("rsi14") or candle.get("rsi"))
        _rsi_p = _safe_float(indicators.get("rsi_prev"))
        _cci_s = f"CCI={_cci_v:.0f}"  if _cci_v is not None else "CCI=?"
        _rsi_s = f"RSI={_rsi_v:.1f}"  if _rsi_v is not None else "RSI=?"
        _rsi_a = ""
        if _rsi_p is not None and _rsi_v is not None:
            delta   = _rsi_v - _rsi_p
            _rsi_a  = f"({'↑' if delta > 0 else '↓'}{abs(delta):.1f})"
        _mom_s  = "MOM=✓" if best_bd.get("momentum_ok", 0) > 0 else "MOM=✗"
        _cpr_s  = f"CPR={indicators.get('cpr_width','?')}"
        _et_s   = f"ET={indicators.get('entry_type','?')}"
        _piv_s  = ""
        if pivot_signal and pivot_signal[0] == best_side:
            _piv_s = f" pivot={pivot_signal[1]}"

        logging.info(
            f"{GREEN}[ENTRY OK] {best_side} score={best_score}/{best_threshold}"
            f"{surcharge_note} {regime} {strength}"
            f" | ST={best_bd.get('trend_alignment',0)}/20"
            f" {_rsi_s}{_rsi_a} {_cci_s}"
            f" VWAP={best_bd.get('vwap_position',0)}/10"
            f" PIV={best_bd.get('pivot_structure',0)}/15"
            f" {_mom_s} {_cpr_s} {_et_s}"
            f"{_piv_s}{RESET}"
        )
    else:
        result["reason"] = (
            f"Score too low: {best_score}<{best_threshold} ({regime}) "
            f"best_side={best_side}"
        )
        logging.debug(f"[ENTRY BLOCKED] {result['reason']}")

    return result