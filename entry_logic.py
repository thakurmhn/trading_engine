# ===== entry_logic.py (v5 — FULL SPEC IMPLEMENTATION) =====
# ===== entry_logic.py (v5 — FULL SPEC IMPLEMENTATION + P5 Extension) =====
"""
v5 implements the complete scoring framework from the system specification,
including Priority 5 opening bias extensions.

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
open_bias_score          5    CALL + (OPEN_LOW or GAP_UP); PUT + (OPEN_HIGH or GAP_DOWN)
                         -3   OPEN_CLOSE_EQUAL or BALANCE_OPEN (marginal-entry suppressor)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
THEORETICAL MAX: 100 pts (Acceptance path + aligned open bias)
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

6. OPENING BIAS EXTENSION (P5):
   • Tags: [OPEN_HIGH], [OPEN_LOW], [OPEN_ABOVE_CLOSE], [OPEN_BELOW_CLOSE],
            [OPEN_CLOSE_EQUAL], [GAP_UP], [GAP_DOWN], [BALANCE_OPEN]
   • Alignment bonus: +5 pts (CALL + OPEN_LOW/GAP_UP; PUT + OPEN_HIGH/GAP_DOWN)
   • Dampener: -3 pts (OPEN_CLOSE_EQUAL or BALANCE_OPEN when no alignment bonus)
   • Alignment overrides dampener; no change to WEIGHTS sum (remains 105)

7. MANDATORY PRE-FILTERS (unchanged from v4):
   • ATR regime gate (LOW → blocked)
   • RSI exhaustion guard (RSI<30 PUT / RSI>75 CALL)
   • Time-of-day: PRE_OPEN, OPENING_NOISE, LUNCH_CHOP, EOD_BLOCK
   • Early session RSI guard (pre-10:15)
   • RSI directional hard filter: PUT blocked RSI>50, CALL blocked RSI<50

8. DYNAMIC THRESHOLDS (unchanged from v4):
   • Afternoon 12:20-14:00: floor = base+25 = 75
   • Late session 14:00+: hard minimum 65
   • Counter-3m surcharge: +8 pts + hard floor 70
   • Counter-HTF surcharge: +3~7 pts + hard floor 65
   • Day type modifiers (TRENDING-8, RANGE+8, etc.)

Empirically validated: all Feb 16–26 replay trades pass with zero regressions.
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
    "open_bias_score":    5,   # P5-B: OPEN_LOW=+5 CALL, OPEN_HIGH=+5 PUT (aligned only)
}

# ── ATR regime thresholds ──────────────────────────────────────────────────────
THRESHOLDS = {
    "LOW":    999,    # low volatility → blocked
    "NORMAL":  50,    # base threshold
    "HIGH":    60,    # elevated volatility → need more conviction
}

ATR_LOW_MAX  = 15    # below → LOW regime (blocked)
ATR_HIGH_MIN = 120   # above → HIGH regime

# ── Volatility context scoring constants ───────────────────────────────────────
# Theta penalty: abs(theta) pts/day > threshold → apply score penalty
_THETA_PENALTY_THRESHOLD = 5.0
# Vega risk high: vega pts per 1% vol move > threshold → reduce position size
_VEGA_RISK_HIGH = 15.0

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


def _score_open_bias(indicators, side):
    """Comprehensive opening bias score (P5-E) — range -3..+5.

    +5 pts (alignment bonus, highest priority):
      CALL + (OPEN_LOW  or GAP_UP)   — bullish open confirmation
      PUT  + (OPEN_HIGH or GAP_DOWN) — bearish open confirmation

    -3 pts (neutral dampener, applied only when no alignment):
      OPEN_CLOSE_EQUAL or BALANCE_OPEN — suppress marginal counter-trend entries

    Priority: alignment (+5) always overrides neutral dampener (-3).

    Reads from indicators dict:
      open_bias    : "OPEN_HIGH" | "OPEN_LOW" | "NONE"       (P5-A)
      gap_tag      : "GAP_UP"   | "GAP_DOWN"  | "NO_GAP"     (P5-C)
      vs_close_tag : "OPEN_ABOVE_CLOSE" | "OPEN_BELOW_CLOSE" | "OPEN_CLOSE_EQUAL"  (P5-B)
      balance_tag  : "BALANCE_OPEN" | "OUTSIDE_BALANCE"      (P5-D)
    Keys absent from indicators default to non-dampening values (no penalty).
    """
    w         = WEIGHTS["open_bias_score"]   # 5
    open_bias = indicators.get("open_bias",    "NONE")
    gap_tag   = indicators.get("gap_tag",      "NO_GAP")
    vs_close  = indicators.get("vs_close_tag", None)
    balance   = indicators.get("balance_tag",  None)

    # ── alignment bonus ───────────────────────────────────────────────────────
    if side == "CALL" and (open_bias == "OPEN_LOW" or gap_tag == "GAP_UP"):
        logging.debug(
            f"[OPEN_BIAS_SCORE] CALL aligned: open_bias={open_bias} gap={gap_tag} → +{w} pts"
        )
        return w
    if side == "PUT" and (open_bias == "OPEN_HIGH" or gap_tag == "GAP_DOWN"):
        logging.debug(
            f"[OPEN_BIAS_SCORE] PUT aligned: open_bias={open_bias} gap={gap_tag} → +{w} pts"
        )
        return w

    # ── neutral dampener (only when no alignment) ─────────────────────────────
    if vs_close == "OPEN_CLOSE_EQUAL" or balance == "BALANCE_OPEN":
        logging.debug(
            f"[OPEN_BIAS_SCORE] Neutral suppressor: vs_close={vs_close} balance={balance} → -3 pts"
        )
        return -3

    return 0


# ─────────────────────────────────────────────────────────────────────────────
# ZONE SCORING — Phase 4A
# ─────────────────────────────────────────────────────────────────────────────

def _score_zone(zone_signal, side):
    """Score based on zone_detector output (zone_signal dict or None).

    Zone BREAKOUT aligned with side → +10 (strong continuation signal)
    Zone REVERSAL aligned with side → +8  (mean-reversion confirmation)
    Zone touch opposing side → -5 (suppress entry into opposing zone)
    No zone signal → 0

    Max: 10 pts.
    """
    if zone_signal is None:
        return 0

    action = zone_signal.get("action", "")
    zone_side = zone_signal.get("side", "")

    if zone_side == side:
        if action == "BREAKOUT":
            logging.debug(
                f"[ZONE][SCORE][{side}] BREAKOUT aligned +10 "
                f"zone_type={zone_signal.get('zone_type')}"
            )
            return 10
        if action == "REVERSAL":
            logging.debug(
                f"[ZONE][SCORE][{side}] REVERSAL aligned +8 "
                f"zone_type={zone_signal.get('zone_type')}"
            )
            return 8
    elif zone_side and zone_side != side:
        # Zone favours opposite side — suppress this side
        logging.debug(
            f"[ZONE][SCORE][{side}] opposing zone_side={zone_side} -5"
        )
        return -5

    return 0


# ─────────────────────────────────────────────────────────────────────────────
# PULSE SCORING — Phase 4B
# ─────────────────────────────────────────────────────────────────────────────

def _score_pulse(pulse_metrics, side):
    """Score based on pulse_module output (dict or None).

    Burst + drift aligned with side → +8 (momentum confirmation)
    Burst + drift opposing           → -5 (momentum against us)
    Burst + neutral drift            → +3 (tick activity, no direction)
    No burst / no metrics            → 0

    Max: 8 pts.
    """
    if pulse_metrics is None:
        return 0

    burst = pulse_metrics.get("burst_flag", False)
    drift = pulse_metrics.get("direction_drift", "NEUTRAL")
    tick_rate = pulse_metrics.get("tick_rate", 0.0)

    if not burst:
        return 0

    # Map drift to side alignment
    drift_aligned = (
        (side == "CALL" and drift == "UP") or
        (side == "PUT" and drift == "DOWN")
    )
    drift_opposing = (
        (side == "CALL" and drift == "DOWN") or
        (side == "PUT" and drift == "UP")
    )

    if drift_aligned:
        logging.debug(
            f"[PULSE][SCORE][{side}] burst+drift={drift} aligned +8 "
            f"tick_rate={tick_rate:.1f}"
        )
        return 8
    if drift_opposing:
        logging.debug(
            f"[PULSE][SCORE][{side}] burst+drift={drift} opposing -5 "
            f"tick_rate={tick_rate:.1f}"
        )
        return -5

    # Burst but neutral drift
    logging.debug(
        f"[PULSE][SCORE][{side}] burst+NEUTRAL +3 tick_rate={tick_rate:.1f}"
    )
    return 3


# ─────────────────────────────────────────────────────────────────────────────
# BAR-CLOSE ALIGNMENT (Phase 6)
# ─────────────────────────────────────────────────────────────────────────────

def _check_bar_close_alignment(candle, indicators, side):
    """Check bar-close alignment on 3m and 15m timeframes.

    Bullish (CALL): close_curr > close_prev near EMA9/EMA13 with BULLISH bias.
    Bearish (PUT):  close_curr < close_prev near EMA9/EMA13 with BEARISH bias.

    Returns:
        tuple: (alignment_status, confirmed_tf)
            alignment_status: "ALIGNED" | "MISALIGNED" | "NEUTRAL"
            confirmed_tf: "15m" | "3m" | None
    """
    # 3m bar-close check
    close_curr = _safe_float(candle.get("close"))
    close_prev = _safe_float(indicators.get("close_prev_3m"))
    ema9 = _safe_float(candle.get("ema9"))
    ema13 = _safe_float(candle.get("ema13"))
    atr = _safe_float(indicators.get("atr"))
    st_bias_3m = str(indicators.get("st_bias_3m", "NEUTRAL")).upper()

    confirmed_3m = False
    if close_curr is not None and close_prev is not None and atr is not None and atr > 0:
        near_ema = False
        if ema9 is not None and abs(close_curr - ema9) <= 1.5 * atr:
            near_ema = True
        if ema13 is not None and abs(close_curr - ema13) <= 1.5 * atr:
            near_ema = True

        if near_ema:
            if side == "CALL" and close_curr > close_prev and st_bias_3m == "BULLISH":
                confirmed_3m = True
            elif side == "PUT" and close_curr < close_prev and st_bias_3m == "BEARISH":
                confirmed_3m = True

    # 15m bar-close check
    confirmed_15m = False
    candle_15m = indicators.get("candle_15m")
    if candle_15m is not None:
        close_15m = _safe_float(candle_15m.get("close") if hasattr(candle_15m, "get") else getattr(candle_15m, "close", None))
        close_prev_15m = _safe_float(indicators.get("close_prev_15m"))
        ema9_15m = _safe_float(candle_15m.get("ema9") if hasattr(candle_15m, "get") else getattr(candle_15m, "ema9", None))
        ema13_15m = _safe_float(candle_15m.get("ema13") if hasattr(candle_15m, "get") else getattr(candle_15m, "ema13", None))
        bias_15m_raw = str(candle_15m.get("supertrend_bias") if hasattr(candle_15m, "get") else getattr(candle_15m, "supertrend_bias", "NEUTRAL")).upper()
        if bias_15m_raw in ("UP",):
            bias_15m_raw = "BULLISH"
        elif bias_15m_raw in ("DOWN",):
            bias_15m_raw = "BEARISH"

        if close_15m is not None and close_prev_15m is not None and atr is not None and atr > 0:
            near_ema_15m = False
            if ema9_15m is not None and abs(close_15m - ema9_15m) <= 2.0 * atr:
                near_ema_15m = True
            if ema13_15m is not None and abs(close_15m - ema13_15m) <= 2.0 * atr:
                near_ema_15m = True

            if near_ema_15m:
                if side == "CALL" and close_15m > close_prev_15m and bias_15m_raw == "BULLISH":
                    confirmed_15m = True
                elif side == "PUT" and close_15m < close_prev_15m and bias_15m_raw == "BEARISH":
                    confirmed_15m = True

    if confirmed_15m:
        return "ALIGNED", "15m"
    if confirmed_3m:
        return "ALIGNED", "3m"

    # Check for misalignment (bar close goes opposite to intended side)
    if close_curr is not None and close_prev is not None:
        if side == "CALL" and close_curr < close_prev:
            return "MISALIGNED", None
        if side == "PUT" and close_curr > close_prev:
            return "MISALIGNED", None

    return "NEUTRAL", None


# ─────────────────────────────────────────────────────────────────────────────
# SPREAD NOISE PROXY (Phase 6)
# ─────────────────────────────────────────────────────────────────────────────

def detect_spread_noise(candle, indicators):
    """Detect if entry premium drift is within spread noise.

    If the candle's (high - low) ≤ 2 pts OR the close-to-open move ≤ 2 pts,
    the entry is dominated by spread noise rather than genuine movement.

    Returns:
        True if spread noise detected, False otherwise.
    """
    close = _safe_float(candle.get("close"))
    open_px = _safe_float(candle.get("open"))
    high = _safe_float(candle.get("high"))
    low = _safe_float(candle.get("low"))

    if close is not None and open_px is not None:
        drift = abs(close - open_px)
        if drift <= 2.0:
            logging.debug(
                f"[SPREAD_NOISE] close_open_drift={drift:.2f} <= 2 pts"
            )
            return True

    if high is not None and low is not None:
        rng = high - low
        if rng <= 2.0:
            logging.debug(
                f"[SPREAD_NOISE] bar_range={rng:.2f} <= 2 pts"
            )
            return True

    return False


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────

def check_entry_condition(candle, indicators, bias_15m,
                          pivot_signal=None, current_time=None,
                          day_type_result=None,
                          reversal_signal=None,
                          osc_relief_active=False,
                          lot_size=None,
                          expiry=None,
                          is_expiry_roll=False,
                          symbol=None,
                          vix_tier=None,
                          greeks=None,
                          zone_signal=None,
                          pulse_metrics=None,
                          daily_camarilla_levels=None,
                          st_details=None):
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
        "lot_size":  None,   # filled in after lot-size enforcement block
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
    _reversal_override_active = (
        reversal_signal is not None and reversal_signal.get("side") in ("CALL", "PUT")
    )
    # Daily Camarilla trend confirmation: below daily S4 → RSI<30 is bearish momentum,
    # not exhaustion. Above daily R4 → RSI>75 is bullish momentum, not overbought.
    _close_val = _safe_float(candle.get("close"))
    _daily_s4_f = (_safe_float(daily_camarilla_levels.get("s4"))
                   if daily_camarilla_levels else None)
    _daily_r4_f = (_safe_float(daily_camarilla_levels.get("r4"))
                   if daily_camarilla_levels else None)
    _daily_cam_trend = False
    if _close_val is not None:
        if _daily_s4_f is not None and _close_val < _daily_s4_f:
            _daily_cam_trend = True  # below daily S4 → bearish confirmed
        elif _daily_r4_f is not None and _close_val > _daily_r4_f:
            _daily_cam_trend = True  # above daily R4 → bullish confirmed
    if _rsi_3m is not None and not _reversal_override_active:
        # Daily Cam trend: relax RSI guard from 30 to 15 (don't block trend-following),
        # but still block extreme capitulation (RSI<15) even on confirmed trend days.
        _rsi_floor = 15.0 if _daily_cam_trend else 30.0
        _rsi_ceil  = 88.0 if _daily_cam_trend else 75.0
        if _rsi_3m < _rsi_floor:
            result["reason"] = f"RSI_OVERSOLD ({_rsi_3m:.1f}<{_rsi_floor:.0f}) — PUT into capitulation blocked"
            return result
        if _rsi_3m > _rsi_ceil:
            result["reason"] = f"RSI_OVERBOUGHT ({_rsi_3m:.1f}>{_rsi_ceil:.0f}) — CALL into exhaustion blocked"
            return result
    elif _rsi_3m is not None and _reversal_override_active:
        # Reversal detector is active: oscillator extremes become CONFIRMATION, not blockers
        rev_side = reversal_signal["side"]
        if _rsi_3m < 30 and rev_side != "CALL":
            result["reason"] = f"RSI_OVERSOLD ({_rsi_3m:.1f}<30) — reversal side mismatch, blocked"
            return result
        if _rsi_3m > 75 and rev_side != "PUT":
            result["reason"] = f"RSI_OVERBOUGHT ({_rsi_3m:.1f}>75) — reversal side mismatch, blocked"
            return result
        logging.info(
            f"{CYAN}[REVERSAL_OVERRIDE] RSI={_rsi_3m:.1f} oscillator extreme flipped to "
            f"confirmation for {rev_side} reversal "
            f"score={reversal_signal.get('score')} "
            f"strength={reversal_signal.get('strength')} "
            f"pivot_zone={reversal_signal.get('pivot_zone')}{RESET}"
        )

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
        # Bypassed when daily Camarilla confirms trend (gap day below S4/above R4)
        if t < 10 * 60 + 15 and _rsi_3m is not None and not _daily_cam_trend:
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

    # ── Lot size enforcement ──────────────────────────────────────────────────
    # Resolve effective lot size: caller-supplied → config default → 1 (safe fallback).
    try:
        import config as _cfg
        _config_lot = _cfg.DEFAULT_LOT_SIZE
    except Exception:
        _config_lot = None

    _effective_lot = lot_size if lot_size is not None else (_config_lot or 1)
    _sym_label = symbol or "NIFTY"
    logging.debug(
        f"[LOT_SIZE] symbol={_sym_label} applied={_effective_lot} "
        f"source={'caller' if lot_size is not None else 'config'}"
    )
    result["lot_size"] = _effective_lot

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
        # Exception: when reversal_signal is active and matches this side,
        # the extreme RSI is a CONFIRMATION — bypass the directional filter.
        _rev_matches_side = (
            _reversal_override_active
            and reversal_signal.get("side") == side
        )
        if _rsi_3m is not None and not _rev_matches_side:
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
            "open_bias_score":  _score_open_bias(indicators, side),
            "zone_score":       _score_zone(zone_signal, side),
            "pulse_score":      _score_pulse(pulse_metrics, side),
        }

        # Phase 6: Bar-close alignment attribution
        _bar_align, _bar_tf = _check_bar_close_alignment(candle, indicators, side)
        bd["bar_close_alignment"] = 0  # no score impact — attribution only
        bd["_bar_align_status"] = _bar_align
        bd["_bar_align_tf"] = _bar_tf

        # Phase 6: Spread noise proxy
        bd["_spread_noise"] = detect_spread_noise(candle, indicators)

        # ── Phase 6.3: Trend Alignment Override (Fix 2) ────────────────────
        # When ST_CONFLICT_OVERRIDE is active (ADX confirmed trend despite ST
        # timeframe disagreement), grant partial trend_alignment credit.
        _st_conflict_ovr = (st_details or {}).get("st_conflict_override", False)
        if _st_conflict_ovr and bd["trend_alignment"] < 10:
            _ovr_adx = _safe_float(indicators.get("adx")) or 0
            if _ovr_adx >= 25:
                _old_ta = bd["trend_alignment"]
                bd["trend_alignment"] = 10
                logging.info(
                    f"[TREND_ALIGN_OVERRIDE] side={side} "
                    f"trend_alignment={_old_ta}->{bd['trend_alignment']} "
                    f"adx={_ovr_adx:.1f} st_conflict_override=True "
                    "reason=ADX confirms trend despite ST timeframe disagreement"
                )

        # ── Phase 6.3: Day-Bias Misalignment Penalty (Fix 3) ─────────────
        # On TRENDING days, counter-trend entries receive a score penalty.
        bd["day_bias_penalty"] = 0
        if day_type_result is not None:
            _dt_name = getattr(getattr(day_type_result, "name", None), "value", "UNKNOWN")
            _open_bias_ctx = (st_details or {}).get("open_bias_context") or {}
            _gap_ctx_p = str(_open_bias_ctx.get("gap_tag", "UNKNOWN")).upper()
            _is_counter = False
            if _dt_name in ("TRENDING",):
                # Determine trend direction from bias context
                _bias_ctx = (st_details or {}).get("bias", "Unknown")
                if (side == "CALL" and _bias_ctx in ("Negative", "BEARISH")) or \
                   (side == "PUT" and _bias_ctx in ("Positive", "BULLISH")):
                    _is_counter = True
                    bd["day_bias_penalty"] = -15
            elif _dt_name in ("GAP_DAY",) or _gap_ctx_p in ("GAP_UP", "GAP_DOWN"):
                if (side == "CALL" and _gap_ctx_p == "GAP_DOWN") or \
                   (side == "PUT" and _gap_ctx_p == "GAP_UP"):
                    _is_counter = True
                    bd["day_bias_penalty"] = -10
            if _is_counter:
                logging.info(
                    f"[DAY_BIAS_PENALTY] side={side} penalty={bd['day_bias_penalty']} "
                    f"day_type={_dt_name} "
                    "reason=Counter-trend entry penalized on directional day"
                )

        # Reversal bonus: HIGH-strength reversal signal aligned with this side
        # adds up to 15 pts to confirm the mean-reversion conviction.
        if _rev_matches_side:
            rev_score  = reversal_signal.get("score", 0)
            rev_bonus  = 15 if rev_score >= 75 else (10 if rev_score >= 50 else 5)
            bd["reversal_override"] = rev_bonus
            logging.debug(
                f"[REVERSAL_OVERRIDE][{side}] bonus={rev_bonus} "
                f"reversal_score={rev_score} pivot={reversal_signal.get('pivot_zone')}"
            )
        else:
            bd["reversal_override"] = 0

        # Expiry roll bonus: +5 pts when position is rolled into next expiry contract
        # Confirms the trader has correctly identified and applied contract roll logic.
        if is_expiry_roll:
            bd["expiry_roll"] = 5
            logging.debug(
                f"[EXPIRY_ROLL][SCORE_BONUS][{side}] +5 pts "
                f"lot_size={lot_size} expiry={expiry} "
                "reason=position rolled to next expiry with valid intrinsic"
            )
        else:
            bd["expiry_roll"] = 0

        # OSC relief bonus: +10 pts when S4/R4 breakout relief applied
        # Price has traded decisively outside Camarilla extremes — continuation likely.
        if osc_relief_active:
            bd["relief_override"] = 10
            logging.debug(
                f"[OSC_RELIEF][SCORE_BONUS][{side}] +10 pts "
                "reason=S4/R4 breakout relief applied, oscillator exhaustion bypassed"
            )
        else:
            bd["relief_override"] = 0

        # Volatility context score adjustment (VIX tier)
        # CALM   VIX (<15): low vol → false signals more common; suppress marginal entries
        # HIGH   VIX (≥20): high vol → conviction signals carry more premium value
        # NEUTRAL VIX:      no adjustment
        _vol_adj = 0
        if vix_tier == "HIGH":
            _vol_adj = 5
        elif vix_tier == "CALM":
            _vol_adj = -5
        bd["vol_context"] = _vol_adj

        # Indicator–Volatility alignment: log how VIX tier shapes gating + ATR stops
        if vix_tier is not None:
            _atr_stops = "TIGHTER" if vix_tier == "CALM" else ("WIDER" if vix_tier == "HIGH" else "NORMAL")
            _osc_gate  = "STRICTER" if vix_tier == "CALM" else ("RELAXED" if vix_tier == "HIGH" else "NORMAL")
            _adx_log   = str(_safe_float(indicators.get("adx14")) or "?")
            _rsi_log_a = f"{_rsi_3m:.1f}" if _rsi_3m is not None else "?"
            logging.debug(
                f"[VOL_CONTEXT][ALIGN][{side}] "
                f"indicators=RSI:{_rsi_log_a}_ADX:{_adx_log} "
                f"vix_tier={vix_tier} "
                f"adj=score:{_vol_adj:+d}_osc:{_osc_gate}_atr:{_atr_stops}"
            )

        # Theta decay penalty: penalise entries with high daily theta decay
        # High theta erodes premium quickly — reduces expected holding value
        _theta_adj = 0
        _theta_val = getattr(greeks, "theta", None) if greeks is not None else None
        _vega_val  = getattr(greeks, "vega",  None) if greeks is not None else None
        if _theta_val is not None and abs(_theta_val) > _THETA_PENALTY_THRESHOLD:
            _theta_adj = -8
        bd["theta_penalty"] = _theta_adj

        # Indicator–Greeks alignment: log theta/vega risk characterisation
        if greeks is not None:
            _vega_risk = (
                "HIGH" if (_vega_val is not None and abs(_vega_val) > _VEGA_RISK_HIGH) else "NORMAL"
            )
            logging.debug(
                f"[GREEKS_ALIGN][{side}] "
                f"symbol={symbol or 'N/A'} "
                f"theta={_theta_val if _theta_val is not None else 'N/A'} "
                f"vega={_vega_val if _vega_val is not None else 'N/A'} "
                f"adj=theta:{_theta_adj:+d}_vega_risk:{_vega_risk}"
            )

        # Log combined vol-context adjustment once per side when any adjustment applies
        if vix_tier is not None or greeks is not None:
            logging.debug(
                f"[VOL_CONTEXT][SCORE_ADJUST][{side}] "
                f"vix_tier={vix_tier or 'N/A'} "
                f"vol_adj={_vol_adj} "
                f"theta={_theta_val if _theta_val is not None else 'N/A'} "
                f"theta_adj={_theta_adj} "
                f"vega={_vega_val if _vega_val is not None else 'N/A'}"
            )

        total = sum(v for v in bd.values() if isinstance(v, (int, float)))

        # Scoring matrix audit: log base + each vol adjustment + final vs threshold
        if _vol_adj != 0 or _theta_adj != 0:
            _base_score = total - _vol_adj - _theta_adj
            logging.debug(
                f"[SCORE_MATRIX][{side}] "
                f"base={_base_score} "
                f"vol_adj={_vol_adj:+d} "
                f"theta_adj={_theta_adj:+d} "
                f"final={total}/{side_threshold}"
            )

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

        # ── Volatility-adjusted position sizing ───────────────────────────────
        # Scale lots by: confidence, ATR, VIX tier, Vega risk.
        # Floor at 1 lot; cap at config default (already resolved as _effective_lot).
        _vix_reduce  = (vix_tier in ("HIGH", "CALM"))  # both vol extremes reduce lots
        _vega_reduce = (
            greeks is not None
            and abs(getattr(greeks, "vega", 0.0)) > _VEGA_RISK_HIGH
        )
        _conf_low    = (best_score < best_threshold + 5)   # marginal entry
        # Each active risk flag reduces by 1 lot (cumulative, floored at 1)
        _lot_cuts  = sum([bool(_vix_reduce), bool(_vega_reduce), bool(_conf_low)])
        _sized_lots = max(1, _effective_lot - _lot_cuts)
        _sized_lots = min(_sized_lots, _effective_lot)    # hard cap at config lot
        result["lot_size"] = _sized_lots

        logging.info(
            f"[POSITION_SIZE] equity=N/A score={best_score} atr={atr:.1f} "
            f"vix_tier={vix_tier or 'N/A'} "
            f"vega_high={_vega_reduce} conf_low={_conf_low} "
            f"lots={_sized_lots}"
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

        _ob_val  = indicators.get("open_bias", "NONE")
        _ob_pts  = best_bd.get("open_bias_score", 0)
        _ob_s    = f" OB={_ob_val}({_ob_pts:+d})" if _ob_val != "NONE" else ""
        _rev_pts = best_bd.get("reversal_override", 0)
        _rev_s   = f" [REVERSAL_OVERRIDE+{_rev_pts}]" if _rev_pts > 0 else ""
        # Phase 4: Zone + Pulse attribution tags
        _zone_pts = best_bd.get("zone_score", 0)
        _zone_s = ""
        if _zone_pts != 0 and zone_signal is not None:
            _zt = zone_signal.get("zone_type", "?")
            _za = zone_signal.get("action", "?")
            _zone_s = f" [ZONE]{_zt}_{_za}({_zone_pts:+d})"
        _pulse_pts = best_bd.get("pulse_score", 0)
        _pulse_s = ""
        if _pulse_pts != 0 and pulse_metrics is not None:
            _pd = pulse_metrics.get("direction_drift", "?")
            _pr = pulse_metrics.get("tick_rate", 0.0)
            _pulse_s = f" [PULSE]{_pd}_{_pr:.0f}t/s({_pulse_pts:+d})"
        # Phase 6: Bar-close alignment tag
        _ba_status = best_bd.get("_bar_align_status", "NEUTRAL")
        _ba_tf = best_bd.get("_bar_align_tf")
        _ba_s = ""
        if _ba_status == "ALIGNED" and _ba_tf:
            _ba_s = f" [BAR_CLOSE_ALIGNMENT][TF={_ba_tf}]"
        elif _ba_status == "MISALIGNED":
            _ba_s = " [BAR_CLOSE_MISALIGNED]"
        # Phase 6: Spread noise tag
        _sn = best_bd.get("_spread_noise", False)
        _sn_s = " [SPREAD_NOISE]" if _sn else ""
        logging.info(
            f"{GREEN}[ENTRY OK] {best_side} score={best_score}/{best_threshold}"
            f"{surcharge_note} {regime} {strength}"
            f" | ST={best_bd.get('trend_alignment',0)}/15"
            f" {_rsi_s}{_rsi_a} {_cci_s}"
            f" VWAP={best_bd.get('vwap_position',0)}/5"
            f" PIV={best_bd.get('pivot_structure',0)}/15"
            f" {_mom_s} {_cpr_s} {_adx_s}"
            f"{_ob_s}{_rev_s}{_piv_s}{_zone_s}{_pulse_s}{_ba_s}{_sn_s}{RESET}"
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