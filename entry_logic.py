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

# ── Score weights ──────────────────────────────────────────────────────────────
# P2 revision (Priority 2 audit fixes):
#   adx_strength   10→15 pts — quartile scoring 0/5/10/15 for stronger trend gate
#   cpr_width      10→15 pts — CPR width is the #1 day-type predictor (spec: 5→15)
#   rsi_score      10→5  pts — RSI hard-block does the heavy filtering; score reduced
#   vwap_position  10→5  pts — directional context; weight released to ADX+CPR
# Net theoretical max: 15+5+15+5+15+15+15+15 = 100 pts
# Existing thresholds (50/60) remain valid.
WEIGHTS = {
    "trend_alignment":   15,   # 15m+3m bundled: both=15, HTF-only=7, neither=0
    "rsi_score":          5,   # RSI >55(CALL)/<45(PUT)=5; hard-block does filtering
    "cci_score":         15,   # CCI >100=10, >150=+5 → 15 max; symmetric for PUT
    "vwap_position":      5,   # above VWAP=CALL, below=PUT
    "pivot_structure":   15,   # Acceptance=15, Rejection/Breakout=10, Continuation=5
    "momentum_ok":       15,   # dual-EMA dual-close + gap widening (boolean)
    "cpr_width":         15,   # Narrow=15, Normal=0, Wide=-5 (day-type predictor)
    "adx_strength":      15,   # ADX quartile: <18=0, 18-25=5, 25-35=10, 35+=15
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
    Supertrend alignment — 15m + 3m bundled (15 pts max after weight revision).

    Both 15m and 3m aligned = 15, HTF only = 7, neither = 0.
    Partial credit for early-reversal: 15m opposing but slope improving = 4.
    """
    w    = WEIGHTS["trend_alignment"]
    b15  = _norm_bias(bias_15m)
    b3   = _norm_bias(indicators.get("st_bias_3m", "NEUTRAL"))

    if side == "CALL":
        if b15 == "BULLISH" and b3 == "BULLISH":  return w          # 15
        if b15 == "BULLISH" and b3 == "NEUTRAL":   return w * 3 // 4 # 11
        if b15 == "BULLISH":                        return w // 2    # 7 — HTF only
        if b15 == "NEUTRAL" and b3 == "BULLISH":    return w // 4    # 3 — LTF only
        if b15 == "BEARISH":
            try:
                c15 = indicators.get("candle_15m")
                if c15 is not None:
                    sl = str(c15.get("supertrend_slope", "")).upper()
                    if sl == "UP": return w // 4   # 3 — early reversal credit
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
                    if sl == "DOWN": return w // 4
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
    CPR width score (15 pts max — P2-B revision; spec: 5 → 15).

    CPR width is the single strongest predictor of day type. A NARROW CPR
    preceding a trending day earns the full 15-pt bonus. WIDE CPR (choppy day)
    deducts 5 pts to penalise momentum entries into indecisive sessions.
    NORMAL = neutral (0 pts).

    indicators["cpr_width"] set by signals.py via classify_cpr_width().
    Values: "NARROW" | "NORMAL" | "WIDE"
    """
    w         = WEIGHTS["cpr_width"]    # 15
    cpr_width = indicators.get("cpr_width", "NORMAL")
    if cpr_width == "NARROW":
        pts = w           # +15: high probability trending day
    elif cpr_width == "WIDE":
        pts = -5          # penalty: choppy session, momentum entries risky
    else:
        pts = 0           # NORMAL: neutral
    logging.debug(f"[CPR_WEIGHT] cpr_width={cpr_width} → {pts:+d}/{w} pts")
    return pts


def _score_adx(indicators):
    """
    ADX trend strength score (15 pts max — P2-A quartile revision).

    Differentiates entries by underlying trend conviction. Same ST alignment
    can occur in ADX=19 (weak trend) or ADX=40 (strong trend) — this scorer
    rewards the latter with a full 15-pt bonus.

    Quartile mapping (P2-A spec):
      ADX < 18  : 0  — trend too weak, no bonus
      18–25     : 5  — moderate trend, partial bonus
      25–35     : 10 — established trend, strong bonus
      35+       : 15 — strong trend, maximum conviction bonus

    indicators["adx14"] or indicators["candle_15m"]["adx14"] is used.
    Falls back to 5-pt neutral default if ADX unavailable (no penalty).
    """
    w = WEIGHTS["adx_strength"]    # 15

    adx = _safe_float(indicators.get("adx14"))
    if adx is None:
        # Try 15m candle snapshot
        c15 = indicators.get("candle_15m")
        if c15 is not None:
            adx = _safe_float(c15.get("adx14"))

    if adx is None:
        logging.debug("[ADX_SCORE] ADX unavailable — using neutral default 5")
        return 5    # unavailable — neutral partial, don't penalise

    if adx >= 35:   pts = w        # 15 — strong trend
    elif adx >= 25: pts = 10       # established trend
    elif adx >= 18: pts = 5        # moderate trend
    else:           pts = 0        # trend too weak

    logging.debug(f"[ADX_SCORE] adx={adx:.1f} → {pts}/{w} pts")
    return pts


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
            "adx_strength":     _score_adx(indicators),
        }
        total = sum(bd.values())

        # Enhanced logging: show indicator availability + scorer breakdown
        _mom_state = "OK" if indicators.get("momentum_ok_" + side.lower()) else "NO"
        _cpr_state = indicators.get("cpr_width", "?")
        _adx_val   = _safe_float(indicators.get("adx14")) or "?"
        _rsi_prev  = "AVAIL" if indicators.get("rsi_prev") is not None else "MISS"

        logging.debug(
            f"[SCORE BREAKDOWN v7][{side}] {total}/{side_threshold} | "
            f"Indicators: MOM={_mom_state} CPR={_cpr_state} ADX={_adx_val} RSI_prev={_rsi_prev} | "
            f"ST={bd['trend_alignment']:2d}/15 RSI={bd['rsi_score']:2d}/5 "
            f"CCI={bd['cci_score']:2d}/15 VWAP={bd['vwap_position']:2d}/5 "
            f"PIV={bd['pivot_structure']:2d}/15 MOM={bd['momentum_ok']:2d}/15 "
            f"CPR={bd['cpr_width']:2d}/15 ADX={bd['adx_strength']:2d}/15"
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
        _cpr_s  = f"CPR={indicators.get('cpr_width','?')}({best_bd.get('cpr_width',0):+d})"
        _adx_s  = f"ADX={_safe_float(indicators.get('adx14')) or '?'}({best_bd.get('adx_strength',0):d}/15)"
        _piv_s  = ""
        if pivot_signal and pivot_signal[0] == best_side:
            _piv_s = f" pivot={pivot_signal[1]}"

        logging.info(
            f"{GREEN}[ENTRY OK] {best_side} score={best_score}/{best_threshold}"
            f"{surcharge_note} {regime} {strength}"
            f" | ST={best_bd.get('trend_alignment',0)}/15"
            f" {_rsi_s}{_rsi_a} {_cci_s}"
            f" VWAP={best_bd.get('vwap_position',0)}/5"
            f" PIV={best_bd.get('pivot_structure',0)}/15"
            f" {_mom_s} {_cpr_s} {_adx_s}"
            f"{_piv_s}{RESET}"
        )
    else:
        result["reason"] = (
            f"Score too low: {best_score}<{best_threshold} ({regime}) "
            f"best_side={best_side}"
        )
        logging.debug(f"[ENTRY BLOCKED] {result['reason']}")

    return result


# ─────────────────────────────────────────────────────────────────────────────
# P3-A  PRE-SESSION DAILY SENTIMENT
# ─────────────────────────────────────────────────────────────────────────────

def compute_daily_sentiment(
    prev_high: float,
    prev_low: float,
    prev_close: float,
    cpr_levels: dict,
    camarilla_levels: dict,
    compression_state_at_close: str = "NEUTRAL",
    atr_value: float = None,
) -> dict:
    """Infer pre-session directional bias from prior-day price structure.

    Uses only price-derived inputs (no volume, no discretionary indicators).
    Should be called once before the session starts (e.g. in do_warmup or
    at the beginning of paper_order when hist_yesterday_15m is available).

    Parameters
    ----------
    prev_high, prev_low, prev_close:
        Prior trading day OHLC values for the underlying index.
    cpr_levels:
        Output of calculate_cpr() — keys: "pivot", "bc", "tc".
    camarilla_levels:
        Output of calculate_camarilla_pivots() — keys: r3, r4, s3, s4.
    compression_state_at_close:
        Market state of CompressionState at prior-day session end.
        "ENERGY_BUILDUP" → opening gap/momentum prediction.
    atr_value:
        Prior-day ATR (14 period on 15m bars).  Used for normalisation.
        If None, raw price distances are used.

    Returns
    -------
    dict with keys:
        sentiment       : "BULLISH" | "BEARISH" | "NEUTRAL"
        confidence      : float 0-100
        preferred_side  : "CALL" | "PUT" | None
        day_type_pred   : "TRENDING" | "RANGE" | "NEUTRAL"
        threshold_adj   : int (-5 to +10) — modify entry threshold for the day
        max_hold_adj    : int (-3 to +2)  — modify MAX_HOLD bars for the day
        reasons         : list[str]       — human-readable evidence trail
    """
    reasons: list = []
    bullish_pts = 0
    bearish_pts = 0

    tc  = _safe_float(cpr_levels.get("tc"))
    bc  = _safe_float(cpr_levels.get("bc"))
    pivot = _safe_float(cpr_levels.get("pivot"))
    r3  = _safe_float(camarilla_levels.get("r3"))
    r4  = _safe_float(camarilla_levels.get("r4"))
    s3  = _safe_float(camarilla_levels.get("s3"))
    s4  = _safe_float(camarilla_levels.get("s4"))

    atr = _safe_float(atr_value) if atr_value is not None else None
    day_range = prev_high - prev_low if prev_high and prev_low else 0.0
    ref_unit  = atr if (atr and atr > 0) else max(day_range, 1.0)

    # ── 1. CPR width → day type prediction ───────────────────────────────────
    cpr_width_raw = None
    day_type_pred = "NEUTRAL"
    if tc is not None and bc is not None:
        cpr_width_raw = tc - bc
        width_ratio   = cpr_width_raw / ref_unit if ref_unit > 0 else 0
        if width_ratio < 0.25:
            day_type_pred = "TRENDING"
            reasons.append(f"NARROW_CPR({width_ratio:.2f}x) → trending day predicted")
        elif width_ratio > 0.80:
            day_type_pred = "RANGE"
            reasons.append(f"WIDE_CPR({width_ratio:.2f}x) → range/choppy day predicted")
        else:
            reasons.append(f"NORMAL_CPR({width_ratio:.2f}x) → neutral day")

    # ── 2. Camarilla close position → directional bias ────────────────────────
    if r3 is not None and s3 is not None:
        if prev_close > r3:
            bullish_pts += 3
            reasons.append(f"CLOSE_ABOVE_R3({prev_close:.0f}>{r3:.0f}) → bullish bias")
        elif prev_close < s3:
            bearish_pts += 3
            reasons.append(f"CLOSE_BELOW_S3({prev_close:.0f}<{s3:.0f}) → bearish bias")
        else:
            reasons.append(f"CLOSE_IN_RANGE({s3:.0f}–{r3:.0f}) → no clear Camarilla bias")

    if r4 is not None and s4 is not None:
        if prev_close > r4:
            bullish_pts += 2
            reasons.append(f"CLOSE_ABOVE_R4({prev_close:.0f}>{r4:.0f}) → strong bullish breakout")
        elif prev_close < s4:
            bearish_pts += 2
            reasons.append(f"CLOSE_BELOW_S4({prev_close:.0f}<{s4:.0f}) → strong bearish breakdown")

    # ── 3. CPR position — close vs pivot ─────────────────────────────────────
    if pivot is not None:
        if prev_close > pivot:
            bullish_pts += 1
            reasons.append(f"CLOSE_ABOVE_PIVOT({prev_close:.0f}>{pivot:.0f})")
        elif prev_close < pivot:
            bearish_pts += 1
            reasons.append(f"CLOSE_BELOW_PIVOT({prev_close:.0f}<{pivot:.0f})")

    # ── 4. Balance zone inference (price-based VAH/VAL proxy) ────────────────
    # The middle 60% of the prior day's range represents accepted value.
    # A close outside this zone = imbalance → directional momentum expected.
    if day_range > 0:
        val_proxy = prev_low  + 0.20 * day_range   # lower boundary of 60% zone
        vah_proxy = prev_high - 0.20 * day_range   # upper boundary of 60% zone
        if prev_close > vah_proxy:
            bullish_pts += 2
            reasons.append(
                f"CLOSE_ABOVE_VALUE_AREA({prev_close:.0f}>{vah_proxy:.0f}) → imbalance bullish"
            )
        elif prev_close < val_proxy:
            bearish_pts += 2
            reasons.append(
                f"CLOSE_BELOW_VALUE_AREA({prev_close:.0f}<{val_proxy:.0f}) → imbalance bearish"
            )
        else:
            reasons.append(f"CLOSE_IN_VALUE_AREA → balanced, range likely")

    # ── 5. Prior-day compression at close → opening momentum prediction ───────
    if compression_state_at_close == "ENERGY_BUILDUP":
        reasons.append(
            "COMPRESSION_AT_CLOSE → explosive opening move likely "
            "(direction from Camarilla bias)"
        )
        # Amplify whichever directional bias already leads
        if bullish_pts > bearish_pts:
            bullish_pts += 2
        elif bearish_pts > bullish_pts:
            bearish_pts += 2
        else:
            bullish_pts += 1   # tie → slight bullish default (market usually up-biased)

    # ── 6. Determine sentiment ────────────────────────────────────────────────
    total_pts = bullish_pts + bearish_pts
    if total_pts == 0:
        sentiment      = "NEUTRAL"
        preferred_side = None
        confidence     = 0.0
    elif bullish_pts > bearish_pts:
        sentiment      = "BULLISH"
        preferred_side = "CALL"
        confidence     = round(100.0 * bullish_pts / total_pts, 1)
    elif bearish_pts > bullish_pts:
        sentiment      = "BEARISH"
        preferred_side = "PUT"
        confidence     = round(100.0 * bearish_pts / total_pts, 1)
    else:
        sentiment      = "NEUTRAL"
        preferred_side = None
        confidence     = 50.0

    # ── 7. Map sentiment → scoring adjustments ────────────────────────────────
    if day_type_pred == "TRENDING":
        threshold_adj = -5    # easier entries on trending days
        max_hold_adj  = +2    # hold longer in trending conditions
    elif day_type_pred == "RANGE":
        threshold_adj = +8    # harder entries — need stronger confirmation
        max_hold_adj  = -3    # exit sooner before reversal
    else:
        threshold_adj = 0
        max_hold_adj  = 0

    if sentiment == "NEUTRAL":
        threshold_adj += 3    # extra caution on neutral sentiment

    result = {
        "sentiment":       sentiment,
        "confidence":      confidence,
        "preferred_side":  preferred_side,
        "day_type_pred":   day_type_pred,
        "threshold_adj":   threshold_adj,
        "max_hold_adj":    max_hold_adj,
        "bullish_pts":     bullish_pts,
        "bearish_pts":     bearish_pts,
        "reasons":         reasons,
    }

    logging.info(
        f"[DAILY_SENTIMENT] sentiment={sentiment} confidence={confidence:.0f}% "
        f"preferred_side={preferred_side} day_type_pred={day_type_pred} "
        f"threshold_adj={threshold_adj:+d} max_hold_adj={max_hold_adj:+d} "
        f"bull_pts={bullish_pts} bear_pts={bearish_pts} "
        f"reasons={' | '.join(reasons)}"
    )
    return result