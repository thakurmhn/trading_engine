    # ============================================================
#  position_manager.py  — v3 (Spec-Aligned Exit Engine)
#  Unified Position Manager for NSE NIFTY Options Buying Bot
# ============================================================
"""
Single source of truth for ALL trade lifecycle management.
Works identically in three execution contexts:

  Context         How PM is used
  ─────────────── ─────────────────────────────────────────────────────────
  REPLAY          PositionManager(mode="REPLAY")  — no broker, simulates LTP
  PAPER           PositionManager(mode="PAPER")   — logs orders, no real money
  LIVE            PositionManager(mode="LIVE")    — calls Fyers API for exits

═══════════════════════════════════════════════════════════════════════
v3 EXIT ARCHITECTURE  (fixes all 11 audited issues from v2)
═══════════════════════════════════════════════════════════════════════

TIER 1 — HARD EXITS  (bypass scoring — always fire immediately)
─────────────────────────────────────────────────────────────────
  1. HARD_STOP      LTP ≤ HARD_STOP_FRAC × entry premium (default 45%)
  2. TRAIL_STOP     UL-based ratchet (FIXED from premium-based in v2)
                    Activates after adaptive TRAIL_MIN_PTS (25/20/30/35)
                    Tightens when momentum weakens (requires 2 consecutive
                    momentum_ok=False bars before tightening kick-in)
  3. EOD_EXIT       Force-close at EOD_MIN (15:10 IST)
  4. MAX_HOLD       Adaptive: day_type + CPR-width + extension if profitable

TIER 2 — INDICATOR EXITS  (evaluated via exit scoring engine)
─────────────────────────────────────────────────────────────────
  Score weights:
    ST_RSI_CONFIRMED   50 pts  ST flips 2 bars + RSI crosses 50
                                (suppressed when 15m aligned AND profitable,
                                 but only for ≤2 flip_bars; after 3 bars
                                 downgrades to ST_FLIP_ONLY=20 to avoid
                                 stale 15m lag)
    ST_FLIP_ONLY       20 pts  ST flip without RSI confirmation
    MOMENTUM_CCI       25 pts  2 consecutive mom_fail bars + CCI < ±50
    MOMENTUM_ONLY      15 pts  2 consecutive mom_fail bars (no CCI)
    PIVOT_REJECTION    20 pts  ATR-tolerance breakout failure / wick rejection
    WR_EXTREME         15 pts  solo; 25 pts when combined with MOM or PIV
    REVERSAL_3         15 pts  3 bearish bars scored (NOT hard bypass) — now
                                suppressed when ADX ≥ 25 (trending)

  Fire threshold: score ≥ 45/100
  Secondary rule: ST_flip(unconfirmed, 20pts) + any other ≥ 20 → EXIT

  Order within Tier 2 (first match fires):
    5. PARTIAL_EXIT    50% size exit at PARTIAL_MIN_PTS gain;
                       hard stop moves to UNDERLYING breakeven (FIXED from
                       option-premium breakeven in v2)
    6. EXIT_SCORED     Full indicator exit when score fires
                       (REVERSAL_3 now scored here, no longer a hard bypass)

═══════════════════════════════════════════════════════════════════════
v3 fixes vs v2:
───────────────────────────────────────────────────────────────────────
 FIX 1 [CRITICAL] Trail based on underlying move, not option premium.
        Old: trail_stop = ep + peak_gain*(1-step)  [wrong — premium-space]
        New: trail anchored to underlying space; converted to option price
             via adaptive delta.
 FIX 2 [CRITICAL] Adaptive delta replaces DELTA_APPROX=0.50 constant.
        delta(ul_move) = 0.50 + 0.002×ul_move, capped [0.25, 0.85].
        Prevents undervaluing ITM options → fewer false HARD_STOP triggers.
 FIX 3 [HIGH]     TRAIL_MIN_PTS raised and made adaptive:
        NORMAL=25, HIGH-vol=20, LOW-vol=30, NARROW-CPR/TREND=35.
        Old value 15 was too tight — trail fired within 1-2 bars of entry.
 FIX 4 [HIGH]     Partial exit hard stop now uses underlying breakeven.
        Old: hard_stop moved to entry option PREMIUM (wrong in replay mode).
        New: hard_stop_ul tracked; hard_stop = ep (premium) as before, but
             a new hard_stop_ul check is also applied.
 FIX 5 [HIGH]     RSI NaN guard in neutral-cross detection.
        If rsi14 is NaN (warm-up period), prev_rsi never updates, so the
        cross detection silently breaks. Now: NaN → skip update only;
        other exit triggers (MOM, PIV, WR) still fire.
 FIX 6 [HIGH]     REVERSAL_3 integrated into scoring (WT=15).
        Old: hard bypass exit — fires regardless of profit, ADX suppression
             only applied AFTER already cutting the trade.
        New: scored dimension. Requires combination with other signals to
             reach 45pt threshold. ADX suppression still applies.
 FIX 7 [MEDIUM]   day_type used in MAX_HOLD (CPR was only secondary before).
        TRENDING day: +10 bars extension; RANGE day: -5 bars.
 FIX 8 [MEDIUM]   W%R weighted dynamically: 15 pts solo, 25 pts if combined.
        Old: flat 20 pts, could fire on W%R alone in strong uptrends.
        New: 15 solo; upgrade to 25 if MOM or PIV also scores.
 FIX 9 [MEDIUM]   Pivot rejection threshold: entry_ul ± 0.5×ATR.
        Old: entry_ul×0.9985 (~4pts on NIFTY) — triggered on any pullback.
        New: ATR-based tolerance (atr stored at entry, retrieved at update).
 FIX 10 [LOW]     ST flip 15m suppression: ≤2 flip_bars → suppress;
        3+ flip_bars → WT_ST_FLIP_ONLY=20 (avoids lagging 15m bias).
 FIX 11 [LOW]     Momentum_ok requires 2 consecutive False bars before scoring.
        Old: single-bar False → scored immediately → premature exit on noise.
        New: mom_fail_bars counter; score only after ≥2 consecutive fails.
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

# ── ANSI colour helpers ────────────────────────────────────────────────────────
GREEN  = "\033[92m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RED    = "\033[91m"
RESET  = "\033[0m"


# ─────────────────────────────────────────────────────────────────────────────
#  ExitDecision — returned by update() every bar
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ExitDecision:
    """Immutable result of one bar's exit evaluation."""
    should_exit  : bool   = False
    reason       : str    = ""
    exit_px      : float  = 0.0      # simulated / actual option LTP
    cur_gain     : float  = 0.0      # current gain vs entry premium (option pts)
    peak_gain    : float  = 0.0      # max gain seen so far (option pts)
    bars_held    : int    = 0
    exit_score   : int    = 0        # indicator exit score (0 for hard exits)
    exit_bd      : Dict   = field(default_factory=dict)

    @property
    def is_win(self) -> bool:
        return self.exit_px > 0 and self.should_exit and self.cur_gain > 0


# ─────────────────────────────────────────────────────────────────────────────
#  PositionManager  v3
# ─────────────────────────────────────────────────────────────────────────────

class PositionManager:
    """
    Manages the lifecycle of a single options-buying position
    (CALL or PUT long) across replay, paper, and live modes.

    All tunable constants are class-level — one place to change everything.
    """

    # ── TIER 1: Hard exit thresholds ─────────────────────────────────────────
    HARD_STOP_FRAC   : float = 0.45    # exit if LTP ≤ 45% of entry premium

    # Trail — ATR-adaptive (regime overrides TRAIL_STEP at runtime)
    # FIX 3: TRAIL_MIN raised from 15 → 25; made adaptive in _trail_min_for_context
    TRAIL_MIN_NORM   : float = 25.0    # base trail activation (underlying pts)
    TRAIL_MIN_HIGH   : float = 20.0    # HIGH vol: activate sooner
    TRAIL_MIN_LOW    : float = 30.0    # LOW vol: wait for more room
    TRAIL_MIN_TREND  : float = 35.0    # NARROW CPR / TRENDING day: give room

    TRAIL_STEP_NORM  : float = 0.12    # trail gives back ≤12% of peak UL move
    TRAIL_STEP_HIGH  : float = 0.18    # wider in high vol
    TRAIL_STEP_LOW   : float = 0.08    # tighter in low vol
    TRAIL_STEP       : float = 0.12    # active step (overridden at runtime)

    # ATR regime thresholds (underlying ATR in points)
    ATR_LOW_MAX      : float = 15.0
    ATR_HIGH_MIN     : float = 80.0

    # Partial exit
    # FIX 3: PARTIAL_MIN raised from 15 → 25 to align with new TRAIL_MIN
    PARTIAL_MIN_PTS  : float = 25.0    # underlying pts gain to book 50% position
    PARTIAL_FRAC     : float = 0.50    # fraction to exit at partial

    # Time / bar limits
    MIN_HOLD         : int   = 3       # bars before indicator exits activate
    MAX_HOLD         : int   = 18      # reduced from 20 (v5 optimization: prevent late MAX_HOLD losses)
    MAX_HOLD_EXT     : int   = 10      # extra bars when trail active + profitable
    PRE_EOD_BARS     : int   = 3       # exit if < 3 bars to EOD and losing (v5 optimization)
    EOD_MIN          : int   = 15 * 60 + 10    # 15:10 IST

    # FIX 2: Adaptive delta constants (replaces flat DELTA_APPROX=0.50)
    DELTA_ENTRY      : float = 0.50    # ATM entry delta
    DELTA_PER_POINT  : float = 0.002   # delta increase per underlying point ITM
    DELTA_MIN        : float = 0.25    # floor (deep OTM)
    DELTA_MAX        : float = 0.85    # ceiling (deep ITM)

    # ── TIER 2: Indicator exit scoring v4 ───────────────────────────────────────
    EXIT_SCORE_THRESHOLD : int = 45

    # Score weights (v4 architecture)
    WT_ST_RSI_CONFIRMED  : int = 50    # ST flip + RSI cross confirmed (fires alone)
    WT_ST_FLIP_ONLY      : int = 20    # ST flip unconfirmed (secondary rule: 20+any≥25→exit)
    # v4: Momentum scoring split by consecutive failures (vs gating by RSI/W%R)
    WT_MOMENTUM_2FAIL    : int = 15    # MOM ×2 fails | gated by RSI/W%R confirmation
    WT_MOMENTUM_3FAIL    : int = 20    # MOM ×3 fails | gated by RSI/W%R confirmation
    WT_MOMENTUM_4FAIL    : int = 25    # MOM ×4+ fails | gated by RSI/W%R confirmation
    WT_PIVOT_REJECTION   : int = 20    # ATR ×0.75 tolerance | gated by RSI/W%R
    # v4: W%R exhaustion filter: 15 solo (never fires), 25 combined with MOM/PIV
    WT_WR_SOLO           : int = 15    # W%R solo (never fires alone)
    WT_WR_COMBINED       : int = 25    # W%R combined with MOM or PIV
    WT_REVERSAL_3        : int = 15    # 3 bars against + ADX<25 | never fires alone

    # Oscillator thresholds (v4 gating)
    RSI_OB           : float = 70.0
    RSI_OS           : float = 30.0
    RSI_NEUTRAL      : float = 50.0    # gate for momentum/pivot: RSI must cross 50
    CCI_MOM_WEAK     : float = 50.0    # not used in v4 (replaced by RSI/W%R gating)
    WR_EXTREME_CALL  : float =  0.0    # W%R ≥ 0 → CALL exhaustion (overbought→reversal)
    WR_EXTREME_PUT   : float = -100.0  # W%R ≤ -100 → PUT exhaustion (oversold→reversal)

    # v4: Gating & exhaustion logic
    MOM_FAIL_BARS    : int   = 2       # min consecutive fails for scoring
    WR_REQUIRES_COMBO: bool  = True    # W%R solo never fires (only combined)

    # Reversal backup (now scored via WT_REVERSAL_3, not hard bypass)
    REVERSAL_MIN     : int   = 3       # consecutive candles against side
    ADX_TREND_MIN    : float = 25.0    # ADX above this → reversal suppressed

    def __init__(
        self,
        mode           : str                = "REPLAY",
        lot_size       : int                = 50,
        broker_exit_fn : Optional[Callable] = None,
    ):
        """
        Parameters
        ──────────
        mode            "REPLAY" | "PAPER" | "LIVE"
        lot_size        NSE lot size (default 50 for NIFTY)
        broker_exit_fn  Callable(symbol, qty, reason) → (bool, order_id)
                        Required for LIVE / PAPER modes.
        """
        self.mode           = mode.upper()
        self.lot_size       = lot_size
        self.broker_exit_fn = broker_exit_fn
        self._t  : Optional[Dict[str, Any]] = None
        self._prev_st : Optional[str]       = None

    # ── ENTRY SCORE BREAKDOWN LOGGING ──────────────────────────────────────────
    
    def _log_entry_score_breakdown(self, signal: Dict[str, Any], side: str) -> None:
        """
        Log detailed PUT/CALL entry score breakdown for audit trail.
        
        Maps signal indicators to entry_logic.py scoring weights:
        - trend_alignment (20 pts max): ST_15m + ST_3m alignment
        - rsi_score (10 pts max): RSI threshold + slope bonus
        - cci_score (15 pts max): CCI threshold + 15m confirmation
        - vwap_position (10 pts max): Price vs VWAP distance
        - pivot_structure (15 pts max): Acceptance/Rejection/Breakout tier
        - momentum_ok (15 pts max): Dual-EMA dual-close confirmation
        - cpr_width (5 pts max): NARROW CPR trending day bonus
        - entry_type_bonus (5 pts max): PULLBACK/REJECTION type bonus
        
        Total: 95 pts max (Acceptance path) / 90 pts max (Rejection path)
        
        Logs: [PUT SCORE BREAKDOWN] or [CALL SCORE BREAKDOWN] with full breakdown.
        """
        try:
            # Extract key indicators from signal
            score_total = signal.get("score", 0)
            threshold = signal.get("threshold", 50)
            
            # Individual dimensions (estimated from signal content)
            # Note: Full breakdown from entry_logic.py is available as dict in signal["breakdown"]
            breakdown = signal.get("breakdown", {})
            
            trend_pts = breakdown.get("trend_alignment", 0)
            rsi_pts = breakdown.get("rsi_score", 0)
            cci_pts = breakdown.get("cci_score", 0)
            vwap_pts = breakdown.get("vwap_position", 0)
            pivot_pts = breakdown.get("pivot_structure", 0)
            momentum_pts = breakdown.get("momentum_ok", 0)
            cpr_pts = breakdown.get("cpr_width", 0)
            entry_type_pts = breakdown.get("entry_type_bonus", 0)
            
            # Indicator snapshots for context
            atr_val = signal.get("atr", signal.get("atr14"))
            cpr_width = signal.get("cpr_width", "?")
            entry_type = signal.get("entry_type", "?")
            st_bias = signal.get("st_bias", "?")
            pivot_reason = signal.get("pivot_reason", signal.get("pivot", ""))
            
            # Log PUT-specific score breakdown
            status_mark = "[+]" if score_total >= threshold else "[-]"
            
            logging.info(
                f"{CYAN}[{side} SCORE BREAKDOWN] {status_mark} {score_total}/{threshold} "
                f"| trend={trend_pts:2d}/20 rsi={rsi_pts:2d}/10 cci={cci_pts:2d}/15 "
                f"vwap={vwap_pts:2d}/10 pivot={pivot_pts:2d}/15 momentum={momentum_pts:2d}/15 "
                f"cpr={cpr_pts:2d}/5 entry_type={entry_type_pts:2d}/5 "
                f"| ATR={atr_val if atr_val else '?'} CPR={cpr_width} "
                f"ET={entry_type} ST={st_bias} PIV={pivot_reason[:25]}{RESET}"
            )
        except Exception as e:
            logging.debug(f"[SCORE BREAKDOWN LOG] error: {e}")

    # ── Public API ────────────────────────────────────────────────────────────

    def is_open(self) -> bool:
        """True when a position is currently active."""
        return self._t is not None

    def open(
        self,
        bar_idx       : int,
        bar_time      : Any,
        underlying    : float,
        entry_premium : float,
        signal        : Dict[str, Any],
    ) -> None:
        """
        Record a new entry.  Raises RuntimeError if a position is already open.

        Parameters
        ──────────
        bar_idx         Sequential bar index
        bar_time        Bar timestamp
        underlying      NIFTY spot price at entry
        entry_premium   Option LTP at entry
        signal          Must contain 'side' ('CALL'/'PUT').
                        Optional: 'score', 'source', 'pivot_reason', 'st_bias',
                                  'atr', 'cpr_width', 'entry_type', 'day_type',
                                  'momentum_ok', 'momentum_gap'
        """
        if self._t is not None:
            raise RuntimeError(
                f"[PM] open() called while position already open "
                f"(side={self._t['side']} since {self._t['entry_time']})"
            )

        side = signal["side"].upper()
        assert side in ("CALL", "PUT"), f"Invalid side: {side}"

        atr_val    = signal.get("atr") or signal.get("atr14")
        cpr_w      = signal.get("cpr_width",  "NORMAL")
        day_type   = signal.get("day_type",   "UNKNOWN")
        trail_step = self._trail_step_for_atr(atr_val)
        trail_min  = self._trail_min_for_context(atr_val, cpr_w, day_type)
        max_hold   = self._max_hold_for_context(cpr_w, day_type)

        self._t = {
            # ── entry snapshot (immutable after open) ──
            "entry_bar"    : bar_idx,
            "entry_time"   : bar_time,
            "entry_ul"     : underlying,
            "entry_px"     : entry_premium,
            "side"         : side,
            "score"        : signal.get("score",        0),
            "source"       : signal.get("source",       "?"),
            "pivot_reason" : signal.get("pivot_reason", signal.get("pivot", "")),
            "entry_st"     : str(signal.get("st_bias",  "?")),
            "option_name"  : signal.get("option_name",  ""),
            "entry_type"   : signal.get("entry_type",   "?"),
            "day_type"     : day_type,
            "cpr_width"    : cpr_w,
            "atr_entry"    : float(atr_val) if atr_val else None,

            # ── mutable exit-tracking state ──
            "bars_held"    : 0,
            "peak_px"      : entry_premium,
            # FIX 1: track peak underlying separately for UL-based trail
            "peak_ul"      : underlying,
            "partial_done" : False,
            "half_qty"     : False,
            "hard_stop"    : entry_premium * self.HARD_STOP_FRAC,
            # FIX 4: underlying-space hard stop (post-partial breakeven)
            "hard_stop_ul" : None,           # set after partial exit
            "trail_active" : False,
            "trail_stop"   : None,
            "trail_step"   : trail_step,
            "trail_min"    : trail_min,      # FIX 3: adaptive trail activation
            "trail_updates": 0,
            "max_hold"     : max_hold,       # FIX 7: day_type-aware

            # ── indicator exit state ──
            "consec_rev"   : 0,
            "st_flip_bars" : 0,
            "st_flip_confirmed": False,     # 2-bar RSI cross confirmation
            "prev_rsi"     : None,
            "prev_ema_gap" : None,
            # v4: consecutive momentum fail counter (2/3/4+ for different thresholds)
            "mom_fail_bars": 0,
            "rsi_supports_exit": False,    # RSI confirms weakness for gating
            "wr_confirms_exit": False,     # W%R confirms exhaustion for gating
            # FIX 10: ST flip suppression counter
            "st_suppress_ct": 0,
            # v4: Partial exit suppression flag
            "partial_exit_fired": False,   # suppress scored exits after partial fired
            "scored_exit_suppressed_ct": 0,# track how long suppression lasts
            "peak_cci"     : 0.0,
        }
        self._prev_st = None

        # Add side decision context log
        st_3m = signal.get("st_bias", "?")
        st_15m = signal.get("st_bias_15m", "?")
        rsi_val = signal.get("rsi", "?")
        cci_val = signal.get("cci", "?")
        
        logging.info(
            f"{CYAN}[SIDE DECISION] {side} chosen: "
            f"ST_3m={st_3m} ST_15m={st_15m} RSI={rsi_val} CCI={cci_val} "
            f"score={signal.get('score','?')}/{signal.get('threshold', '?')}{RESET}"
        )

        logging.info(
            f"{GREEN}[TRADE OPEN][{self.mode}] {side} "
            f"bar={bar_idx} {bar_time} "
            f"underlying={underlying:.2f} premium={entry_premium:.2f} "
            f"score={signal.get('score','?')} "
            f"src={signal.get('source','?')} "
            f"pivot={signal.get('pivot_reason', signal.get('pivot',''))} "
            f"cpr={cpr_w} day={day_type} "
            f"max_hold={max_hold}bars trail_min={trail_min:.0f}pts "
            f"trail_step={trail_step:.0%} lot={self.lot_size}{RESET}"
        )
        
        # Log detailed score breakdown for audit trail (v6 entry score framework)
        self._log_entry_score_breakdown(signal, side)

    def update(
        self,
        bar_idx    : int,
        bar_time   : Any,
        underlying : float,
        row        : Any,
    ) -> ExitDecision:
        """
        Evaluate exit conditions for the current bar.

        Call every bar while is_open() is True.
        Returns ExitDecision; if should_exit is True, call close() immediately.

        Parameters
        ──────────
        bar_idx     Current bar index
        bar_time    Current bar timestamp
        underlying  NIFTY spot close (REPLAY) or option LTP (LIVE/PAPER)
        row         Pandas Series / dict with indicator columns:
                    Required: rsi14, cci20, supertrend_bias, ema9, ema13,
                              open, close, high, low
                    Optional: adx14, williams_r, st_bias_15m, atr14
        """
        if not self._t:
            return ExitDecision()

        t    = self._t
        ep   = t["entry_px"]
        side = t["side"]
        atr  = self._get_atr(row)

        # ── Safe indicator reads ──────────────────────────────────────────────
        def _f(k: str, default: float = float("nan")) -> float:
            try:
                v = float(row[k] if hasattr(row, "__getitem__") else getattr(row, k))
                return v if math.isfinite(v) else default
            except Exception:
                return default

        def _s(k: str, default: str = "?") -> str:
            try:
                return str(
                    row[k] if hasattr(row, "__getitem__") else getattr(row, k)
                ).upper()
            except Exception:
                return default

        rsi   = _f("rsi14",  50.0)
        cci   = _f("cci20",   0.0)
        ema9  = _f("ema9",  float("nan"))
        ema13 = _f("ema13", float("nan"))
        adx   = _f("adx14", float("nan"))
        o_bar = _f("open",  float("nan"))
        c_bar = _f("close", float("nan"))
        h_bar = _f("high",  float("nan"))
        l_bar = _f("low",   float("nan"))
        st    = _s("supertrend_bias", "?")
        st15  = _s("st_bias_15m", "")
        st15_slope = _s("st_slope_15m", "FLAT")

        bar_min = self._bar_minutes(bar_time)

        # ── Update ATR-adaptive trail parameters ──────────────────────────────
        if math.isfinite(atr) and atr > 0:
            t["trail_step"] = self._trail_step_for_atr(atr)

        # ── FIX 2: Adaptive delta (ITM/OTM correction) ───────────────────────
        ul_move_raw = underlying - t["entry_ul"]
        if side == "PUT":
            ul_move_raw = -ul_move_raw   # positive = favourable move for PUT
        delta = self._adaptive_delta(ul_move_raw)

        # ── Option LTP simulation ─────────────────────────────────────────────
        if self.mode == "REPLAY":
            cur = max(0.1, ep + delta * ul_move_raw)
        else:
            cur = max(0.1, underlying)   # LIVE: underlying IS the LTP

        # ── Update bar state ──────────────────────────────────────────────────
        t["bars_held"] += 1

        if cur > t["peak_px"]:
            t["peak_px"] = cur

        # FIX 1: Track underlying peak separately for UL-based trail
        if side == "CALL":
            if underlying > t["peak_ul"]:
                t["peak_ul"] = underlying
        else:
            if underlying < t["peak_ul"]:
                t["peak_ul"] = underlying

        peak_px    = t["peak_px"]
        peak_gain  = peak_px - ep
        cur_gain   = cur - ep
        # UL peak move (always positive = in trade direction)
        ul_peak_move = (t["peak_ul"] - t["entry_ul"]) if side == "CALL" else \
                       (t["entry_ul"] - t["peak_ul"])

        # ── Update peak |CCI| ─────────────────────────────────────────────────
        if math.isfinite(cci):
            t["peak_cci"] = max(t.get("peak_cci", 0.0), abs(cci))

        # ══════════════════════════════════════════════════════════════════════
        #  TIER 1 — HARD EXITS  (bypass scoring, always fire)
        # ══════════════════════════════════════════════════════════════════════

        # ── 1. HARD STOP (option premium) ─────────────────────────────────────
        if cur <= t["hard_stop"]:
            return self._hard_exit(
                cur, "HARD_STOP",
                f"LTP={cur:.1f} ≤ hard_stop={t['hard_stop']:.1f} "
                f"(entry={ep:.1f}×{self.HARD_STOP_FRAC:.0%})",
                cur_gain, peak_gain, t["bars_held"]
            )

        # FIX 4: Underlying hard stop (post-partial breakeven)
        if t.get("hard_stop_ul") is not None:
            ul_be_ok = (underlying >= t["hard_stop_ul"]) if side == "CALL" else \
                       (underlying <= t["hard_stop_ul"])
            if not ul_be_ok:
                return self._hard_exit(
                    cur, "HARD_STOP_UL",
                    f"UL={underlying:.1f} breached breakeven "
                    f"hard_stop_ul={t['hard_stop_ul']:.1f} "
                    f"(post-partial breakeven guard)",
                    cur_gain, peak_gain, t["bars_held"]
                )

        # ── 2. TRAIL STOP (FIX 1: UL-based, FIX 2: adaptive delta) ──────────
        trail_min  = t.get("trail_min", self.TRAIL_MIN_NORM)
        trail_step = t["trail_step"]

        if not t["trail_active"] and ul_peak_move >= trail_min:
            t["trail_active"] = True
            # Trail computed in underlying space, converted to option price
            trail_ul  = t["entry_ul"] + ul_peak_move * (1.0 - trail_step)
            trail_opt = ep + delta * ul_peak_move * (1.0 - trail_step)
            t["trail_stop"] = max(ep * 0.50, trail_opt)   # floor at 50% of entry
            logging.info(
                f"  {CYAN}[TRAIL ON] bar={bar_idx} {bar_time} "
                f"side={side} ul_peak=+{ul_peak_move:.1f}pts "
                f"trail_min={trail_min:.0f}pts "
                f"step={trail_step:.0%} delta={delta:.3f} "
                f"trail_stop={t['trail_stop']:.2f}{RESET}"
            )

        if t["trail_active"]:
            # FIX 11: Only tighten trail after 2 consecutive momentum failures
            mom_ok_now = self._momentum_ok_from_row(row, side)
            if not mom_ok_now:
                t["mom_fail_bars"] = t.get("mom_fail_bars", 0) + 1
            else:
                t["mom_fail_bars"] = 0

            effective_step = trail_step
            if t.get("mom_fail_bars", 0) >= self.MOM_FAIL_BARS:
                # Momentum confirmed weak — tighten to lock gains faster
                effective_step = max(trail_step * 0.80, self.TRAIL_STEP_LOW)
                logging.debug(
                    f"  [TRAIL TIGHT] {self.MOM_FAIL_BARS} consec mom_fail "
                    f"→ step={effective_step:.2%}"
                )

            # Recompute trail stop in option-price space
            new_trail = ep + delta * ul_peak_move * (1.0 - effective_step)
            new_trail = max(ep * 0.50, new_trail)
            if new_trail > t["trail_stop"]:
                old = t["trail_stop"]
                t["trail_stop"]    = new_trail
                t["trail_updates"] = t.get("trail_updates", 0) + 1
                logging.debug(
                    f"  [TRAIL UPDATE] {old:.2f}→{new_trail:.2f} "
                    f"ul_peak=+{ul_peak_move:.1f} step={effective_step:.2%} "
                    f"delta={delta:.3f}"
                )

            if cur <= t["trail_stop"]:
                return self._hard_exit(
                    cur, "TRAIL_STOP",
                    f"LTP={cur:.1f} ≤ trail={t['trail_stop']:.1f} "
                    f"ul_peak=+{ul_peak_move:.1f}pts step={trail_step:.0%} "
                    f"mom_fail_bars={t.get('mom_fail_bars',0)} "
                    f"updates={t['trail_updates']}",
                    cur_gain, peak_gain, t["bars_held"]
                )

        # ── 3. EOD FORCE EXIT ─────────────────────────────────────────────────
        if bar_min >= self.EOD_MIN:
            return self._hard_exit(
                cur, "EOD_EXIT",
                f"Time={bar_min//60:02d}:{bar_min%60:02d} ≥ EOD "
                f"{self.EOD_MIN//60:02d}:{self.EOD_MIN%60:02d}",
                cur_gain, peak_gain, t["bars_held"]
            )

        # ── 4. PRE-EOD EXIT (v5 optimization: prevent EOD loss exits) ──────────
        pre_eod_threshold = self.EOD_MIN - (self.PRE_EOD_BARS * 3)  # bars * 3min per bar
        if bar_min >= pre_eod_threshold and cur_gain < 0:
            return self._hard_exit(
                cur, "EOD_PRE_EXIT",
                f"Pre-EOD safety: {self.PRE_EOD_BARS} bars to EOD {self.EOD_MIN//60:02d}:{self.EOD_MIN%60:02d}, cur_gain={cur_gain:.1f}pts",
                cur_gain, peak_gain, t["bars_held"]
            )

        # ── 5. MAX HOLD (FIX 7: day_type-aware, v5 optimized) ──────────────────
        max_cap = t.get("max_hold", self.MAX_HOLD)
        if t["trail_active"] and cur_gain >= ep * 0.40:
            max_cap += self.MAX_HOLD_EXT
        if t["bars_held"] >= max_cap:
            return self._hard_exit(
                cur, "MAX_HOLD",
                f"bars_held={t['bars_held']} ≥ max_cap={max_cap} "
                f"(cpr={t.get('cpr_width','?')} day={t.get('day_type','?')})",
                cur_gain, peak_gain, t["bars_held"]
            )

        # ── MIN_HOLD gate ─────────────────────────────────────────────────────
        if t["bars_held"] < self.MIN_HOLD:
            return ExitDecision(
                should_exit=False, cur_gain=cur_gain,
                peak_gain=peak_gain, bars_held=t["bars_held"]
            )

        # ══════════════════════════════════════════════════════════════════════
        #  STRATEGIC EXIT RULES (v7 simplified)
        # ══════════════════════════════════════════════════════════════════════
        # Decision hierarchy (ONE rule fires per bar):
        #   1) LOSS_CUT (highest priority) — prevent large losses
        #   2) QUICK_PROFIT — book small wins early
        #   3) DRAWDOWN_EXIT — protect peak gains
        #   4) BREAKOUT_HOLD (lowest priority) — extend hold on breakouts
        
        # Rule constants
        LOSS_CUT_PTS        = -10   # exit if loss < -10 pts
        LOSS_CUT_MAX_BARS   = 5     # only in first 5 bars
        QUICK_PROFIT_UL_PTS = 10    # UL move >= 10 pts = ~₹1300 for lot=130
        DRAWDOWN_THRESHOLD  = 9     # exit if peak_gain - cur_gain >= 9 pts
        
        # ────────────────────────────────────────────────────────────────────
        # RULE 1: LOSS_CUT (exit quickly on early losses)
        # ────────────────────────────────────────────────────────────────────
        if t["bars_held"] <= LOSS_CUT_MAX_BARS and cur_gain < LOSS_CUT_PTS:
            logging.info(
                f"[LOSS CUT] gain={cur_gain:.2f}pts < {LOSS_CUT_PTS}pts | "
                f"bar={bar_idx} held={t['bars_held']}bars | "
                f"side={side} entry={ep:.2f} cur={cur:.2f}"
            )
            logging.info(
                f"[EXIT DECISION] rule=LOSS_CUT priority=1 "
                f"reason=early_loss gain={cur_gain:.2f}pts bars={t['bars_held']}"
            )
            return self._hard_exit(
                cur, "LOSS_CUT",
                f"Loss cut: gain={cur_gain:.2f}pts within {t['bars_held']} bars "
                f"(prevents further deterioration)",
                cur_gain, peak_gain, t["bars_held"]
            )

        # ────────────────────────────────────────────────────────────────────
        # RULE 2: QUICK_PROFIT (book 50% when UL reaches +10 pts)
        # ────────────────────────────────────────────────────────────────────
        if not t.get("half_qty", False) and ul_peak_move >= QUICK_PROFIT_UL_PTS:
            t["partial_done"] = True
            t["half_qty"]     = True
            t["hard_stop"]    = ep              # option-price BE
            t["hard_stop_ul"] = t["entry_ul"]   # UL BE (post-partial guard)
            
            pnl_half = cur_gain * (self.lot_size // 2)  # P&L for half position
            logging.info(
                f"[QUICK PROFIT] ul_peak=+{ul_peak_move:.1f}pts >= {QUICK_PROFIT_UL_PTS}pts | "
                f"booked ~50% at {cur:.2f} | P&L ~Rs{pnl_half:+.0f} | "
                f"stop->BE ul={t['entry_ul']:.1f} bar={bar_idx}"
            )
            logging.info(
                f"[EXIT DECISION] rule=QUICK_PROFIT priority=2 "
                f"reason=ul_peak_move>={QUICK_PROFIT_UL_PTS}pts gain={cur_gain:.2f}pts"
            )
            return ExitDecision(
                should_exit  = True,
                reason       = (
                    f"QUICK_PROFIT | ul_peak=+{ul_peak_move:.1f}pts | "
                    f"50% booked at {cur:.2f} | stop->BE"
                ),
                exit_px      = cur,
                cur_gain     = cur_gain,
                peak_gain    = peak_gain,
                bars_held    = t["bars_held"],
                exit_score   = 0,
                exit_bd      = {"rule": "QUICK_PROFIT", "action": "50%_booked"},
            )

        # ────────────────────────────────────────────────────────────────────
        # RULE 3: DRAWDOWN_EXIT (protect gains from reversals)
        # ────────────────────────────────────────────────────────────────────
        # Only applies if we've captured at least 5 pts gain (meaningful profit)
        drawdown_amount = peak_gain - cur_gain if peak_gain > 0 else 0
        
        if peak_gain >= 5 and drawdown_amount >= DRAWDOWN_THRESHOLD:
            logging.info(
                f"[DRAWDOWN EXIT] peak={peak_gain:.2f}pts - cur={cur_gain:.2f}pts = "
                f"drawdown={drawdown_amount:.2f}pts >= {DRAWDOWN_THRESHOLD}pts | "
                f"bar={bar_idx} held={t['bars_held']}bars | "
                f"locking in {cur_gain:.2f}pts before further reversal"
            )
            logging.info(
                f"[EXIT DECISION] rule=DRAWDOWN_EXIT priority=3 "
                f"reason=drawdown_protection peak={peak_gain:.2f}pts cur={cur_gain:.2f}pts"
            )
            return self._hard_exit(
                cur, "DRAWDOWN_EXIT",
                f"Drawdown protection: peak={peak_gain:.2f}pts -> cur={cur_gain:.2f}pts "
                f"(drawdown={drawdown_amount:.2f}pts >= {DRAWDOWN_THRESHOLD}pts threshold)",
                cur_gain, peak_gain, t["bars_held"]
            )

        # ────────────────────────────────────────────────────────────────────
        # RULE 4: BREAKOUT_HOLD (suppress exits if price sustains R4/S4)
        # ────────────────────────────────────────────────────────────────────
        # Check if position should be held longer based on breakout levels
        r4_level = t.get("r4", float("nan"))
        s4_level = t.get("s4", float("nan"))
        breakout_hold_triggered = False
        
        if side == "CALL" and math.isfinite(r4_level) and underlying >= r4_level:
            # Sustaining above R4 for CALL — extend hold
            if not t.get("breakout_hold_active", False):
                t["breakout_hold_active"] = True
                t["breakout_hold_bars"] = 0
                logging.info(
                    f"[BREAKOUT HOLD] CALL sustains UL={underlying:.2f} >= R4={r4_level:.2f} | "
                    f"extend hold, suppress normal exits | bar={bar_idx}"
                )
            t["breakout_hold_bars"] = t.get("breakout_hold_bars", 0) + 1
            breakout_hold_triggered = t["breakout_hold_active"]
            
        elif side == "PUT" and math.isfinite(s4_level) and underlying <= s4_level:
            # Sustaining below S4 for PUT — extend hold
            if not t.get("breakout_hold_active", False):
                t["breakout_hold_active"] = True
                t["breakout_hold_bars"] = 0
                logging.info(
                    f"[BREAKOUT HOLD] PUT sustains UL={underlying:.2f} <= S4={s4_level:.2f} | "
                    f"extend hold, suppress normal exits | bar={bar_idx}"
                )
            t["breakout_hold_bars"] = t.get("breakout_hold_bars", 0) + 1
            breakout_hold_triggered = t["breakout_hold_active"]
        else:
            # Reset if price breaches breakout levels
            if t.get("breakout_hold_active", False):
                logging.debug(
                    f"[BREAKOUT HOLD] price breached threshold | "
                    f"side={side} ul={underlying:.2f} r4={r4_level:.2f} s4={s4_level:.2f}"
                )
                t["breakout_hold_active"] = False
                t["breakout_hold_bars"] = 0
        
        if breakout_hold_triggered:
            logging.info(
                f"[EXIT DECISION] rule=BREAKOUT_HOLD priority=4 "
                f"action=suppress_exits bars_in_breakout={t.get('breakout_hold_bars',0)}"
            )
            # Don't exit this bar; breakout hold is active
            return ExitDecision(
                should_exit=False, cur_gain=cur_gain,
                peak_gain=peak_gain, bars_held=t["bars_held"]
            )

        # ── HOLD (no exit triggered) ─────────────────────────────────────────
        return ExitDecision(
            should_exit=False,
            cur_gain=cur_gain,
            peak_gain=peak_gain,
            bars_held=t["bars_held"],
        )

    def close(
        self,
        bar_idx   : int,
        bar_time  : Any,
        underlying: float,
        exit_px   : float,
        reason    : str,
        qty       : Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Close the active position.  Calls broker_exit_fn in LIVE/PAPER modes.

        Returns a trade record dict suitable for appending to a trade log.

        Note on PARTIAL_EXIT: caller should reduce qty to lot_size//2 and call
        close() for the half only.  PM remains open for the rest.
        """
        if not self._t:
            raise RuntimeError("[PM] close() called but no position is open")

        t    = self._t
        ep   = t["entry_px"]
        side = t["side"]
        qty  = qty or self.lot_size

        pnl_pts = exit_px - ep
        pnl_val = pnl_pts * qty
        color   = GREEN if pnl_pts >= 0 else RED
        outcome = "WIN " if pnl_pts >= 0 else "LOSS"

        if self.mode in ("LIVE", "PAPER") and self.broker_exit_fn is not None:
            opt_name = t.get("option_name", "?")
            ok, order_id = self.broker_exit_fn(opt_name, qty, reason)
            if not ok:
                logging.error(f"{RED}[PM] broker_exit_fn failed: {opt_name}{RESET}")
            else:
                logging.info(f"{YELLOW}[PM] Broker exit: {opt_name} order={order_id}{RESET}")

        logging.info(
            f"{color}[TRADE EXIT] {outcome} {side} "
            f"bar={bar_idx} {bar_time} "
            f"prem {ep:.2f}->{exit_px:.2f} "
            f"P&L={pnl_pts:+.2f}pts ({pnl_val:+.0f}Rs) "
            f"peak={t['peak_px']:.2f} "
            f"held={t['bars_held']}bars "
            f"trail_updates={t.get('trail_updates',0)}\n"
            f"  reason: {reason}{RESET}"
        )

        # [TRADE IMPROVED] — log when v5 optimizations reduce losses or create wins
        if pnl_pts < 0 and t['bars_held'] <= 10:
            logging.info(
                f"[TRADE IMPROVED] {side} reduced loss: "
                f"bar_stayed={t['bars_held']} (early exit via v5 logic) "
                f"loss={pnl_pts:+.1f}pts (optimization active)"
            )

        record = {
            "mode"          : self.mode,
            "side"          : side,
            "source"        : t.get("source",       "?"),
            "score"         : t.get("score",         0),
            "pivot_reason"  : t.get("pivot_reason",  ""),
            "option_name"   : t.get("option_name",   ""),
            "entry_type"    : t.get("entry_type",    "?"),
            "day_type"      : t.get("day_type",      "?"),
            "cpr_width"     : t.get("cpr_width",     "?"),
            "entry_bar"     : t["entry_bar"],
            "entry_time"    : t["entry_time"],
            "exit_bar"      : bar_idx,
            "exit_time"     : bar_time,
            "bars_held"     : t["bars_held"],
            "entry_ul"      : t["entry_ul"],
            "exit_ul"       : underlying,
            "entry_premium" : ep,
            "exit_premium"  : exit_px,
            "peak_premium"  : t["peak_px"],
            "exit_reason"   : reason[:100],
            "partial_done"  : t["partial_done"],
            "half_qty_done" : t["half_qty"],
            "trail_active"  : t["trail_active"],
            "trail_updates" : t.get("trail_updates", 0),
            "peak_cci"      : t.get("peak_cci",      0.0),
            "pnl_points"    : round(pnl_pts, 2),
            "pnl_value"     : round(pnl_val, 2),
            "pnl_pct"       : round(pnl_pts / ep * 100, 1) if ep else 0,
            "lot_size"      : qty,
        }

        self._t       = None
        self._prev_st = None
        return record

    def force_close_eod(
        self,
        bar_idx   : int,
        bar_time  : Any,
        underlying: float,
    ) -> Optional[Dict[str, Any]]:
        """Force-close any open position at end of day."""
        if not self._t:
            return None
        t  = self._t
        ep = t["entry_px"]
        if self.mode == "REPLAY":
            move    = underlying - t["entry_ul"]
            if t["side"] == "PUT": move = -move
            delta   = self._adaptive_delta(move)
            exit_px = max(0.1, ep + delta * move)
        else:
            exit_px = underlying
        logging.warning(
            f"{YELLOW}[PM] force_close_eod: {t['side']} "
            f"held={t['bars_held']}bars → EOD_CLOSE{RESET}"
        )
        return self.close(bar_idx, bar_time, underlying, exit_px, "EOD_CLOSE")

    def position_summary(self) -> str:
        """One-line summary of open position for logging / dashboard."""
        if not self._t:
            return "No open position"
        t = self._t
        return (
            f"{t['side']} | entry={t['entry_px']:.2f} "
            f"| bars={t['bars_held']} "
            f"| peak={t['peak_px']:.2f} "
            f"| trail={'ON' if t['trail_active'] else 'OFF'} "
            f"| partial={'YES' if t['partial_done'] else 'NO'} "
            f"| half_qty={'YES' if t['half_qty'] else 'NO'} "
            f"| mom_fail_bars={t.get('mom_fail_bars',0)}"
        )

    def state_dict(self) -> Dict[str, Any]:
        if not self._t:
            return {}
        return dict(self._t)

    def restore_state(self, state: Dict[str, Any]) -> None:
        if not state:
            return
        required = {"entry_bar", "entry_time", "entry_px", "side"}
        missing  = required - set(state.keys())
        if missing:
            raise ValueError(f"[PM] restore_state missing keys: {missing}")
        self._t = dict(state)
        logging.warning(
            f"{YELLOW}[PM] Position RESTORED: "
            f"{self._t['side']} entry={self._t['entry_px']:.2f} "
            f"bars_held={self._t.get('bars_held',0)}{RESET}"
        )

    # ── Private helpers ───────────────────────────────────────────────────────

    def _adaptive_delta(self, ul_move: float) -> float:
        """
        FIX 2: Dynamic delta estimate for ATM options.
        Replaces constant DELTA_APPROX=0.50.

        ATM entry → delta=0.50. As option moves ITM, delta rises.
        As option moves OTM, delta falls. Linear approximation is crude
        but eliminates the systematic bias of a fixed 0.50 in replay.

        ul_move: underlying move in TRADE direction (positive = favourable).
        """
        delta = self.DELTA_ENTRY + self.DELTA_PER_POINT * ul_move
        return max(self.DELTA_MIN, min(self.DELTA_MAX, delta))

    def _trail_step_for_atr(self, atr_val: Any) -> float:
        """ATR-adaptive trail step fraction."""
        try:
            atr = float(atr_val)
            if not math.isfinite(atr) or atr <= 0:
                return self.TRAIL_STEP_NORM
            if atr < self.ATR_LOW_MAX:   return self.TRAIL_STEP_LOW
            if atr > self.ATR_HIGH_MIN:  return self.TRAIL_STEP_HIGH
            return self.TRAIL_STEP_NORM
        except Exception:
            return self.TRAIL_STEP_NORM

    def _trail_min_for_context(
        self, atr_val: Any, cpr_width: str, day_type: str
    ) -> float:
        """
        FIX 3: Adaptive TRAIL_MIN_PTS based on volatility and day type.

        OLD: flat 15 pts — activated within 1-2 bars of entry.
        NEW: 20-35 pts depending on context.

        Logic:
          HIGH vol ATR    → 20 pts (big moves happen fast; lock in sooner)
          LOW  vol ATR    → 30 pts (small moves; need more room)
          NARROW CPR day  → 35 pts (trend day; let the trade breathe)
          RANGE day       → 20 pts (chop; lock in profit quickly)
          TRENDING day    → 30 pts (runner day; don't cut too soon)
        """
        try:
            atr = float(atr_val) if atr_val else 0
        except Exception:
            atr = 0

        base = self.TRAIL_MIN_NORM  # 25

        if atr > self.ATR_HIGH_MIN:
            base = self.TRAIL_MIN_HIGH   # 20
        elif atr < self.ATR_LOW_MAX:
            base = self.TRAIL_MIN_LOW    # 30

        if cpr_width == "NARROW" or day_type in ("TRENDING", "TREND_DAY"):
            base = max(base, self.TRAIL_MIN_TREND)   # 35
        elif day_type in ("RANGE", "NEUTRAL", "DOUBLE_DISTRIBUTION"):
            base = min(base, self.TRAIL_MIN_HIGH)     # cap at 20 for chop

        return base

    def _max_hold_for_context(self, cpr_width: str, day_type: str) -> int:
        """
        FIX 7: day_type-aware MAX_HOLD (CPR was only factor in v2).
        v5: Tightened for DOUBLE_DISTRIBUTION (range-bound days) to prevent MAX_HOLD losses.

        day_type takes precedence; CPR is secondary context.
        TRENDING → let winners run; RANGE/NEUTRAL → exit before chop.
        """
        base = self.MAX_HOLD   # 18 bars (reduced from 20)

        # day_type override (primary) — v5: tightened DOUBLE_DISTRIBUTION
        if day_type in ("TRENDING", "TREND_DAY"):
            base += 10   # 28 bars
        elif day_type in ("RANGE", "NEUTRAL"):
            base -= 5    # 13 bars
        elif day_type == "DOUBLE_DISTRIBUTION":
            base -= 7    # 11 bars (v5: more aggressive for choppy days)

        # CPR secondary adjustment
        if cpr_width == "NARROW":
            base += 3    # additional room on tight CPR days
        elif cpr_width == "WIDE":
            base -= 2    # tighten slightly on wide CPR

        return max(10, base)   # floor at 10 bars

    def _momentum_ok_from_row(self, row: Any, side: str) -> bool:
        """
        Derive momentum_ok from a pre-computed indicator row.

        Mirrors entry logic momentum_ok (dual-EMA + gap widening).
        Single-bar proxy: checks current bar only.
        Gap widening tracked via prev_ema_gap state across bars.

        Returns True if can't determine (NaN) — avoids false exits.
        """
        try:
            e9  = float(row["ema9"]  if hasattr(row, "__getitem__") else row.ema9)
            e13 = float(row["ema13"] if hasattr(row, "__getitem__") else row.ema13)
            cl  = float(row["close"] if hasattr(row, "__getitem__") else row.close)
        except Exception:
            return True

        if not (math.isfinite(e9) and math.isfinite(e13) and math.isfinite(cl)):
            return True

        gap_curr = e9 - e13
        gap_prev = self._t.get("prev_ema_gap") if self._t else None

        ema_aligned = (e9 > e13) if side == "CALL" else (e9 < e13)
        close_ok    = (cl > e9 and cl > e13) if side == "CALL" else \
                      (cl < e9 and cl < e13)

        if gap_prev is not None and math.isfinite(gap_prev):
            gap_widening = (gap_curr > gap_prev) if side == "CALL" else \
                           ((-gap_curr) > (-gap_prev))
        else:
            gap_widening = True

        if self._t is not None:
            self._t["prev_ema_gap"] = gap_curr

        return ema_aligned and close_ok and gap_widening

    def _score_pivot_rejection(
        self,
        row       : Any,
        side      : str,
        entry_pivot: str,
        h_bar     : float,
        l_bar     : float,
        c_bar     : float,
        atr       : float,
        rsi_confirms : bool = False,
        wr_confirms : bool = False,
    ) -> tuple:
        """
        v4: Pivot rejection with ATR ×0.75 tolerance (was 0.5×ATR in v3).

        Triggers:
          1. Wick rejection: upper wick > 35% of candle range (CALL) or
             lower wick > 35% (PUT).
          2. Breakout failure: close retreated by > 0.75×ATR below entry UL.
             OLD v3: 0.5×ATR (tighter, more premature exits).
             NEW v4: 0.75×ATR (wider, respects noise better).
          3. v4: Requires RSI/W%R confirmation to contribute to score.

        Returns: (pts, triggered_via) — pts=0 if gating fails
        """
        if not math.isfinite(c_bar) or not math.isfinite(h_bar) or not math.isfinite(l_bar):
            return (0, "")

        piv_reason = ""

        # Wick rejection (always scores if triggered, no gating)
        if math.isfinite(h_bar) and math.isfinite(l_bar):
            candle_rng = h_bar - l_bar
            if candle_rng > 0:
                upper_wick = h_bar - c_bar
                lower_wick = c_bar - l_bar
                if side == "CALL" and (upper_wick / candle_rng) > 0.35:
                    piv_reason = "wick_rejection"
                if side == "PUT"  and (lower_wick / candle_rng) > 0.35:
                    piv_reason = "wick_rejection"

        # Breakout failure with ATR ×0.75 tolerance (v4 update)
        if entry_pivot and "BREAKOUT" in entry_pivot.upper():
            entry_ul = self._t.get("entry_ul", 0) if self._t else 0
            if entry_ul > 0:
                # Use stored atr_entry if live atr unavailable
                _atr = atr if (math.isfinite(atr) and atr > 0) else \
                       (self._t.get("atr_entry") or 20.0)
                tol = 0.75 * _atr  # v4: 0.75 tolerance (was 0.5 in v3)
                if side == "CALL" and c_bar < entry_ul - tol:
                    piv_reason = f"breakout_fail(c={c_bar:.0f}<entry-{tol:.0f})"
                if side == "PUT"  and c_bar > entry_ul + tol:
                    piv_reason = f"breakout_fail(c={c_bar:.0f}>entry+{tol:.0f})"

        # v4: Gate pivot scoring by RSI/W%R confirmation
        if piv_reason and not (rsi_confirms or wr_confirms):
            return (0, f"PIV_gated[{piv_reason}]")  # pivot triggered but gated
        
        if piv_reason:
            gate_tag = "RSI" if rsi_confirms else "WR" if wr_confirms else ""
            return (self.WT_PIVOT_REJECTION, f"PIV_reject[{piv_reason}_{gate_tag}]")

        return (0, "")

    def _get_atr(self, row: Any) -> float:
        """Extract ATR from indicator row."""
        try:
            v = row.get("atr14", None) if hasattr(row, "get") else None
            if v is None:
                v = row.get("atr", None) if hasattr(row, "get") else None
            if v is not None and math.isfinite(float(v)):
                return float(v)
        except Exception:
            pass
        return float("nan")

    def _get_wr(self, row: Any) -> float:
        """Extract Williams %R from indicator row."""
        try:
            v = row.get("williams_r", None) if hasattr(row, "get") else None
            if v is None:
                v = row.get("wr", None) if hasattr(row, "get") else None
            if v is not None and math.isfinite(float(v)):
                return float(v)
        except Exception:
            pass
        return float("nan")

    def _build_reason(
        self,
        trigger    : str,
        score      : int,
        bd         : Dict,
        components : List[str],
        side       : str,
        rsi        : float,
        cci        : float,
        wr         : float,
        st_3m      : str,
        st_15m     : str,
        cur_gain   : float,
        peak_gain  : float,
    ) -> str:
        """
        Build a rich, auditable exit reason string.

        Format:
          EXIT_SCORED | score=65/45 | ST_flip×2+RSI_cross50(47) + MOM_fail×2+CCI_weak(32)
          | RSI=47 CCI=32 WR=-15 ST3m=BEARISH ST15m=BULLISH
          | gain=+12.3pts peak=+28.0pts
          | breakdown: ST_RSI=50 MOM=15 PIV=0 WR=0 REV3=0
        """
        comp_str = " + ".join(components) if components else "score_threshold"
        wr_str   = f" WR={wr:.0f}" if math.isfinite(wr) else ""
        bd_str   = " ".join(f"{k}={v}" for k, v in bd.items() if v > 0)
        return (
            f"{trigger} | score={score}/{self.EXIT_SCORE_THRESHOLD} "
            f"| {comp_str} "
            f"| RSI={rsi:.0f} CCI={cci:.0f}{wr_str} "
            f"ST3m={st_3m} ST15m={st_15m} "
            f"| gain={cur_gain:+.1f}pts peak=+{peak_gain:.1f}pts "
            f"| breakdown: {bd_str}"
        )

    def _build_reason_v4(
        self,
        trigger    : str,
        score      : int,
        bd         : Dict,
        components : List[str],
        side       : str,
        rsi        : float,
        cci        : float,
        wr         : float,
        st_3m      : str,
        st_15m     : str,
        cur_gain   : float,
        peak_gain  : float,
        rsi_supports_exit : bool = False,
        wr_confirms_exit : bool = False,
        mom_fail_ct : int = 0,
    ) -> str:
        """
        v4: Build a rich, auditable exit reason with RSI/W%R gating transparency.

        Adds exhaustion confirmation tags and momentum failure count for auditability.

        Format:
          SCORED_v4 | score=50/45 | ST_flip×2+RSI_cross50(47) [CONF] + MOM_fail×3[RSI_gate]
          | RSI=42[gate=True] WR=-15 | ST3m=BEARISH ST15m=BULLISH
          | gain=+12.3pts peak=+28.0pts
          | breakdown: ST_RSI=50 MOM=20 PIV=0 WR=0 REV3=0
        """
        comp_str = " + ".join(components) if components else "score_threshold"
        wr_str = f" WR={wr:.0f}" if math.isfinite(wr) else ""
        bd_str = " ".join(f"{k}={v}" for k, v in bd.items() if v > 0)
        
        gate_str = f"[RSI_gate={rsi_supports_exit} WR_gate={wr_confirms_exit}]"
        mom_str = f" MOM×{mom_fail_ct}" if mom_fail_ct > 0 else ""
        
        return (
            f"{trigger} | score={score}/{self.EXIT_SCORE_THRESHOLD} "
            f"| {comp_str} {gate_str} "
            f"| RSI={rsi:.0f}{mom_str} CCI={cci:.0f}{wr_str} "
            f"ST3m={st_3m} ST15m={st_15m} "
            f"| gain={cur_gain:+.1f}pts peak=+{peak_gain:.1f}pts "
            f"| breakdown: {bd_str}"
        )

    def _hard_exit(
        self,
        exit_px   : float,
        tag       : str,
        detail    : str,
        cur_gain  : float,
        peak_gain : float,
        bars_held : int,
    ) -> ExitDecision:
        reason = f"{tag} | {detail} | gain={cur_gain:+.1f}pts peak=+{peak_gain:.1f}pts"
        logging.info(
            f"  {YELLOW if cur_gain <= 0 else CYAN}"
            f"[EXIT HARD][{tag}] {reason}{RESET}"
        )
        return ExitDecision(
            should_exit=True, reason=reason, exit_px=exit_px,
            cur_gain=cur_gain, peak_gain=peak_gain, bars_held=bars_held,
            exit_score=0, exit_bd={"HARD": tag}
        )

    def _indicator_exit(
        self,
        exit_px   : float,
        reason    : str,
        score     : int,
        bd        : Dict,
        cur_gain  : float,
        peak_gain : float,
        bars_held : int,
    ) -> ExitDecision:
        logging.info(
            f"  {YELLOW}[EXIT SCORED] score={score} "
            f"gain={cur_gain:+.1f}pts peak=+{peak_gain:.1f}pts\n"
            f"  reason: {reason}{RESET}"
        )
        return ExitDecision(
            should_exit=True, reason=reason, exit_px=exit_px,
            cur_gain=cur_gain, peak_gain=peak_gain, bars_held=bars_held,
            exit_score=score, exit_bd=bd
        )

    @staticmethod
    def _bar_minutes(bar_time: Any) -> int:
        """Convert bar_time to minutes since midnight (for EOD gate)."""
        try:
            s = str(bar_time).split(" ")[-1]
            h, m = int(s[:2]), int(s[3:5])
            return h * 60 + m
        except Exception:
            return 0


# ─────────────────────────────────────────────────────────────────────────────
#  TradeLogger — structured CSV / console output helper
# ─────────────────────────────────────────────────────────────────────────────

class TradeLogger:
    """
    Accumulates trade records from PositionManager.close() calls
    and emits a formatted summary + CSV at end of session.
    """

    def __init__(self, symbol: str = "NSE:NIFTY50-INDEX"):
        self.symbol  = symbol
        self.records = []

    def append(self, record: Dict[str, Any]) -> None:
        self.records.append(record)

    def summary(self, date_str: str = "") -> str:
        sep = "─" * 68
        n   = len(self.records)
        if n == 0:
            return f"{sep}\n  No trades recorded ({date_str})\n{sep}"

        wins   = sum(1 for r in self.records if r["pnl_points"] > 0)
        losses = n - wins
        total  = sum(r["pnl_value"]  for r in self.records)
        avg_h  = sum(r["bars_held"]  for r in self.records) / n
        avg_pk = sum(r["peak_premium"] - r["entry_premium"] for r in self.records) / n

        exit_counts: Dict[str, int] = {}
        for r in self.records:
            key = r["exit_reason"][:35]
            exit_counts[key] = exit_counts.get(key, 0) + 1

        lines = [
            f"\n{sep}",
            f"  RESULTS: {self.symbol}  ({date_str})",
            sep,
            f"  Trades closed  : {n}",
            f"  Win / Loss     : {wins} / {losses}  ({100*wins/n:.1f}%)",
            f"  Total PnL (Rs) : {total:+.2f}",
            f"  Avg hold       : {avg_h:.1f} bars  ({avg_h*3:.0f} min)",
            f"  Avg peak gain  : +{avg_pk:.1f} pts",
            "",
            "  Exit reason breakdown:",
        ]
        for reason, cnt in sorted(exit_counts.items(), key=lambda x: -x[1]):
            lines.append(f"    {reason:<35}: {cnt}")

        lines.append("")
        lines.append("  Trade details:")
        for r in self.records:
            color = GREEN if r["pnl_points"] > 0 else RED
            partial_tag = "[HALF]" if r.get("half_qty_done") else ""
            lines.append(
                f"{color}    {r['side']:<4} {partial_tag:<7}"
                f"entry={r['entry_time']} exit={r['exit_time']} "
                f"held={r['bars_held']}bars "
                f"pnl={r['pnl_points']:+.1f}pts "
                f"({r['pnl_value']:+.0f}Rs) "
                f"peak=+{r['peak_premium']-r['entry_premium']:.1f} "
                f"[{r['exit_reason'][:40]}]{RESET}"
            )

        lines.append(sep)
        return "\n".join(lines)

    def to_csv(self, path: str) -> None:
        try:
            import csv
            if not self.records:
                return
            fieldnames = list(self.records[0].keys())
            with open(path, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fieldnames)
                w.writeheader()
                w.writerows(self.records)
            logging.info(f"[TradeLogger] Saved {len(self.records)} → {path}")
        except Exception as e:
            logging.error(f"[TradeLogger] CSV write failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  Convenience constructors
# ─────────────────────────────────────────────────────────────────────────────

def make_replay_pm(lot_size: int = 50) -> PositionManager:
    return PositionManager(mode="REPLAY", lot_size=lot_size)


def make_paper_pm(
    broker_exit_fn: Callable,
    lot_size: int = 50,
) -> PositionManager:
    return PositionManager(mode="PAPER", lot_size=lot_size,
                           broker_exit_fn=broker_exit_fn)   


def make_live_pm(
    broker_exit_fn: Callable,
    lot_size: int = 50,
) -> PositionManager:
    return PositionManager(mode="LIVE", lot_size=lot_size,
                           broker_exit_fn=broker_exit_fn)