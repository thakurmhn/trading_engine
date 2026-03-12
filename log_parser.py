"""log_parser.py — Trading engine log parsing engine.

Parses structured log tags emitted by the trading engine:

  [TRADE OPEN][REPLAY|PAPER|LIVE]  — entry filled
  [TRADE EXIT]                     — exit filled (WIN/LOSS + P&L)
  [ENTRY BLOCKED][subtype]         — entry suppressed by a gate
  [ENTRY OK]                       — entry passed all gates
  [SIGNAL FIRED]                   — signal generated (pre-gate)
  [EXIT AUDIT]                     — legacy detailed exit record

Produces per-trade dicts and a SessionSummary for each log file.

Usage
-----
    from log_parser import LogParser, SessionSummary

    parser = LogParser("options_trade_engine_2026-02-24.log")
    summary: SessionSummary = parser.parse()

    for trade in summary.trades:
        print(trade["side"], trade["pnl_pts"], trade["bars_held"])

    print(summary.blocked_counts)   # {"ST_CONFLICT": 371, ...}
    print(summary.win_rate_pct)
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

# ── ANSI strip ────────────────────────────────────────────────────────────────
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _strip(line: str) -> str:
    return _ANSI.sub("", line)


# ── Regex patterns ─────────────────────────────────────────────────────────────

# Log-line timestamp prefix: "2026-02-24 11:02:48,553 - INFO - "
_RE_LOG_TS = re.compile(
    r"^(?P<log_ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
)

# [TRADE OPEN][REPLAY|PAPER|LIVE] CALL bar=791 2026-02-20 09:45:00
#   underlying=25497.75 premium=153.00 score=83 src=PIVOT pivot=BREAKOUT_R3
#   cpr=NARROW day=UNKNOWN max_hold=23bars trail_min=35pts trail_step=12% lot=130
_RE_TRADE_OPEN = re.compile(
    r"\[TRADE OPEN\]\[(?P<session_type>\w+)\] (?P<side>CALL|PUT) "
    r"bar=(?P<bar>\d+) "
    r"(?P<bar_ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) "
    r"underlying=(?P<underlying>[\d.]+) premium=(?P<premium>[\d.]+) "
    r"score=(?P<score>\d+) src=(?P<src>\S+)"
    r"(?:.*?pivot=(?P<pivot>\S+))?"
    r"(?:.*?cpr=(?P<cpr>\S+))?"
    r"(?:.*?day=(?P<day>[A-Z_]+))?"
    r"(?:.*?lot=(?P<lot>\d+))?"
    r"(?:.*?option_name=(?P<option_name>\S+))?",
    re.IGNORECASE,
)

# New format:  [TRADE EXIT] WIN/LOSS  CALL bar=798 2026-02-20 10:06:00
#               prem 153.00→143.61 P&L=-9.39pts (-1221₹) peak=165.56 held=7bars
# Legacy format: [TRADE EXIT][REASON] WIN/LOSS CALL bar=811 2026-02-20 10:45:00
#               premium 153.00→201.23 P&L=+48.23pts (+6269₹) peak=204.20 held=20bars
# Also handles ASCII arrow ->  and suffix Rs instead of ₹
_RE_TRADE_EXIT = re.compile(
    r"\[TRADE EXIT\](?:\[(?P<exit_reason>\w+)\])? "
    r"(?P<outcome>WIN|LOSS)\s+(?P<side>CALL|PUT) "
    r"bar=(?P<bar>\d+) "
    r"(?P<bar_ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) "
    r"prem(?:ium)? (?P<entry_prem>[\d.]+)(?:\u2192|->)(?P<exit_prem>[\d.]+) "
    r"P&L=(?P<pnl_pts>[+-][\d.]+)pts "
    r"\((?P<pnl_rs>[+-]\d+)[^)]*\) "
    r"peak=(?P<peak>[\d.]+) "
    r"held=(?P<bars_held>\d+)bars",
    re.IGNORECASE,
)

# Structured: [TRADE OPEN] time=YYYY-MM-DD HH:MM:SS side=CALL|PUT option_name=... entry=... lots=...
# Emitted by PositionManager.open() – no [MODE] bracket, no WIN/LOSS outcome
_RE_TRADE_OPEN_STRUCT = re.compile(
    r"\[TRADE OPEN\] time=(?P<bar_ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
    r" side=(?P<side>CALL|PUT)"
    r" option_name=(?P<option_name>\S+)"
    r" entry=(?P<entry>[\d.]+)"
    r" lots=(?P<lots>\d+)",
    re.IGNORECASE,
)

# Structured: [TRADE EXIT] time=... option_name=... exit=... pnl_pts=... pnl_rs=... bars=... reason=...
# Emitted by PositionManager.close() – no WIN/LOSS, no prem A->B format
_RE_TRADE_EXIT_STRUCT = re.compile(
    r"\[TRADE EXIT\] time=(?P<bar_ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
    r" option_name=(?P<option_name>\S+)"
    r" exit=(?P<exit_prem>[\d.]+)"
    r" pnl_pts=(?P<pnl_pts>[+-][\d.]+)"
    r" pnl_rs=(?P<pnl_rs>[+-][\d.]+)"
    r" bars=(?P<bars_held>\d+)"
    r" reason=(?P<exit_reason>\S+)",
    re.IGNORECASE,
)

# [EXIT][PAPER SL_HIT] PUT NSE:NIFTY2630225000PE Entry=102.50 Exit=94.25 Qty=130 PnL=-1072.50 (points=-8.25) BarsHeld=0 ...
# [EXIT][LIVE SCALP_PT_HIT] CALL NSE:NIFTY2630224700CE Entry=137.15 Exit=140.55 Qty=130 PnL=442.00 (points=3.40) BarsHeld=0 ...
# Emitted by execution.py cleanup_trade_exit — self-contained record with all trade details.
_RE_EXIT_RICH = re.compile(
    r"\[EXIT\]\[(?P<session_mode>PAPER|LIVE)\s+(?P<exit_reason>[\w]+)\]"
    r"\s+(?P<side>CALL|PUT)"
    r"\s+(?P<option_name>\S+)"
    r"\s+Entry=(?P<entry_prem>[\d.]+)"
    r"\s+Exit=(?P<exit_prem>[\d.]+)"
    r"\s+Qty=(?P<qty>\d+)"
    r"\s+PnL=(?P<pnl_rs>[+-]?[\d.]+)"
    r"\s+\(points=(?P<pnl_pts>[+-]?[\d.]+)\)"
    r"\s+BarsHeld=(?P<bars_held>\d+)",
    re.IGNORECASE,
)

# [ENTRY BLOCKED][ST_CONFLICT] ...
# [ENTRY BLOCKED][COOLDOWN] 0s < 120s
_RE_ENTRY_BLOCKED = re.compile(r"\[ENTRY BLOCKED\]\[(?P<subtype>\w+)\]")

# [SLOPE_CONFLICT][3m] — renamed tag (P6); maps to blocked["ST_SLOPE_CONFLICT"]
_RE_SLOPE_CONFLICT_3M = re.compile(r"\[SLOPE_CONFLICT\]\[3m\]")

# [ENTRY OK] CALL score=83/50 NORMAL HIGH | ST=20/20 RSI=61.7...
_RE_ENTRY_OK = re.compile(
    r"\[ENTRY OK\] (?P<side>CALL|PUT) score=(?P<score>\d+)/(?P<threshold>\d+)"
    r" (?P<regime>\w+) (?P<strength>\w+)"
    r"(?:.*?pivot=(?P<pivot>\S+))?",
    re.IGNORECASE,
)

# [SIGNAL FIRED] CALL score=83 strength=HIGH | ...
_RE_SIGNAL_FIRED = re.compile(
    r"\[SIGNAL FIRED\] (?P<side>CALL|PUT) score=(?P<score>\d+) "
    r"strength=(?P<strength>\w+)",
    re.IGNORECASE,
)

# Legacy [EXIT AUDIT] — emitted by execution.py
_RE_EXIT_AUDIT = re.compile(
    r"\[EXIT AUDIT\]"
    r".*?timestamp=(?P<timestamp>\S+)"
    r".*?option_type=(?P<option_type>CALL|PUT)"
    r".*?exit_type=(?P<exit_type>\S+)"
    r".*?reason=(?P<reason>\S+)"
    r"(?:.*?bars_held=(?P<bars_held>[-\d]+))?"
    r"(?:.*?position_id=(?P<position_id>\S+))?"
    r"(?:.*?premium_move=(?P<premium_move>[-\d.]+))?",
    re.IGNORECASE,
)

# P1–P5 tag presence detection (for tag-fired counts)
_P_TAGS = [
    "FALSE_BREAKOUT_COOLDOWN",
    "SCALP_OVERRIDE",
    "TG_PARTIAL_EXIT",
    "DYNAMIC_EXIT_THRESHOLD",
    "VOL_REVERSION_REFINED",
    "SLIPPAGE_MODELED",
    "MAX_TRADES_CAP",
    "BALANCE_ZONE",
    "CAMARILLA_BIAS",
    "CPR_PRECLASS",
    "COMPRESSION_FORECAST",
    "COMPRESSION_START",
    "EXPANSION_CONFIRMED",
    "COMPRESSION_DISSOLVED",
    "OPEN_POSITION",       # P5-A
    "OPEN_BIAS_SCORE",     # P5 scoring log
    "OPEN_ABOVE_CLOSE",    # P5-B
    "OPEN_BELOW_CLOSE",    # P5-B
    "OPEN_CLOSE_EQUAL",    # P5-B
    "GAP_UP",              # P5-C
    "GAP_DOWN",            # P5-C
    "NO_GAP",              # P5-C
    "BALANCE_OPEN",        # P5-D
    "OUTSIDE_BALANCE",     # P5-D
    # Reversal detector tags
    "REVERSAL_SIGNAL",
    "REVERSAL_OVERRIDE",
    "ST_SLOPE_OVERRIDE",
    # Day type tag
    "DAY_TYPE",
    # Trend-aware oscillator gating
    "OSC_OVERRIDE_PIVOT_BREAK",
    # S4/R4 breakout OSC relief
    "OSC_RELIEF",
    # Governance tags
    "OPEN_BIAS",
    "OSC_CONTEXT",
    "OSC_EXTREME",
    "OSC_REVERSAL",
    "OSC_CONTINUATION",
    "DAY_BIAS_ALIGN",
    "DAY_BIAS_MISALIGN",
    "ENTRY_GATE_CONTEXT",
    "FAILED_BREAKOUT",
    "EMA_STRETCH",
    "ZONE_REVISIT",
    "SLOPE_OVERRIDE_TREND",
    "PT_TG_UNREACHABLE_EXIT",
    "TG_HIT_SUPPRESSED",
    "ORB_ACTIVE",
    "ORB_EXPIRED",
    "SCALP_SL_HIT",
    "TREND_SL_HIT",
    "SCALP_ENTRY",
    "TREND_ENTRY",
    "EXIT_ATTRIBUTION",
    "TG_HIT_EXIT_SUPPRESSED",
    "SURVIVABILITY_OVERRIDE",
    "ENTRY_ALLOWED_BUT_NOT_EXECUTED",
    # Phase 4: Zone + Pulse scoring attribution
    "ZONE",
    "PULSE",
    # Phase 5: Regime context attribution
    "REGIME_CONTEXT",
    "REGIME_ADAPTIVE",
    # Phase 6: Bias alignment & microstructure
    "BAR_CLOSE_ALIGNMENT",
    "BAR_CLOSE_MISALIGNED",
    "BIAS_ALIGNMENT",
    "SLOPE_OVERRIDE_TIME",
    "CONFLICT_BLOCKED",
    "PULSE_EXHAUSTION",
    "ZONE_ABSORPTION",
    "ZONE_REJECTION",
    "SPREAD_NOISE",
    # Phase 6.1: Tilt-based governance
    "TILT_STATE",
    "GOVERNANCE_EASY",
    "GOVERNANCE_STRICT",
    # Phase 6.2: Trend continuation
    "TREND_CONTINUATION",
    "TREND_CONTINUATION_OVERRIDE",
    "REPLAY_TREND_REENTRY",
]
_RE_TAG_ANY = re.compile(r"\[(" + "|".join(_P_TAGS) + r")\]")

# [OSC_OVERRIDE][TREND_CONFIRMED] ADX=38.2 tier=ADX_MOD_30 RSI=... CCI=...
_RE_OSC_TREND_OVERRIDE = re.compile(
    r"\[OSC_OVERRIDE\]\[TREND_CONFIRMED\]"
    r"(?:.*?ADX=(?P<adx>[\d.]+))?"
    r"(?:.*?tier=(?P<tier>\w+))?",
    re.IGNORECASE,
)

# [OPEN_POSITION] tag=OPEN_HIGH open=25000.00 high=25000.00 low=24850.00 ...
_RE_OPEN_POSITION = re.compile(
    r"\[OPEN_POSITION\] tag=(?P<open_bias_tag>OPEN_HIGH|OPEN_LOW|NONE)"
    r".*?open=(?P<open_px>[\d.]+)"
    r".*?high=(?P<high_px>[\d.]+)"
    r".*?low=(?P<low_px>[\d.]+)",
    re.IGNORECASE,
)

# [OPEN_ABOVE_CLOSE] open=25100.00 prev_close=25000.00 ...
# [OPEN_BELOW_CLOSE] open=24900.00 ...
# [OPEN_CLOSE_EQUAL] open=25000.00 ...
_RE_OPEN_VS_CLOSE = re.compile(
    r"\[(?P<vs_close_tag>OPEN_ABOVE_CLOSE|OPEN_BELOW_CLOSE|OPEN_CLOSE_EQUAL)\]"
    r".*?open=(?P<open_px>[\d.]+)",
    re.IGNORECASE,
)

# [GAP_UP] open=25200.00 prev_high=25100.00 prev_low=24900.00 ...
# [GAP_DOWN] open=24800.00 ...
# [NO_GAP]  open=25000.00 ...
_RE_GAP = re.compile(
    r"\[(?P<gap_tag>GAP_UP|GAP_DOWN|NO_GAP)\]"
    r".*?open=(?P<open_px>[\d.]+)",
    re.IGNORECASE,
)

# [BALANCE_OPEN]    open=25000.00 bc=24990.00 tc=25010.00
# [OUTSIDE_BALANCE] open=25100.00 bc=24990.00 tc=25010.00
_RE_BALANCE_OPEN = re.compile(
    r"\[(?P<balance_tag>BALANCE_OPEN|OUTSIDE_BALANCE)\]"
    r".*?open=(?P<open_px>[\d.]+)",
    re.IGNORECASE,
)

# [DAY_TYPE] day_type_tag=TREND_DAY open_bias=OPEN_LOW gap=NO_GAP balance=OUTSIDE_BALANCE
# vs_close=OPEN_ABOVE_CLOSE cpr_width=NARROW ...
_RE_DAY_TYPE = re.compile(
    r"\[DAY_TYPE\](?:\[\S+\])?\s+"
    r"day_type_tag=(?P<day_type_tag>\w+)"
    r"(?:.*?cpr_width=(?P<cpr_width_tag>NARROW|NORMAL|WIDE))?",
    re.IGNORECASE,
)

# [DAY TYPE] TRENDING   confidence=HIGH   modifier=-8pts threshold | CPR_width=25.2 ...
# Emitted by replay DTC .log() / lock_classification() — no 'day_type_tag=' prefix.
# DayType enum values: TRENDING RANGE REVERSAL DOUBLE_DIST NEUTRAL NON_TREND UNKNOWN
_RE_DAY_TYPE_DTC = re.compile(
    r"\[DAY TYPE\]\s+(?P<dtc_name>TRENDING|RANGE|REVERSAL|DOUBLE_DIST|NEUTRAL|NON_TREND|UNKNOWN)"
    r".*?confidence=(?P<dtc_conf>LOW|MEDIUM|HIGH)"
    r"(?:.*?CPR_width=(?P<dtc_cpr>[\d.]+))?",
    re.IGNORECASE,
)
# Replay opening format: [DAY_TYPE] GAP_UP bias=Positive open=+0.80% vs prev close
_RE_DAY_TYPE_OPENING = re.compile(
    r"\[DAY_TYPE\]\s+(?P<gap_tag>GAP_UP|GAP_DOWN|NEUTRAL|UNKNOWN)\s+bias=(?P<bias>\w+)",
    re.IGNORECASE,
)

# [OSC_RELIEF][S4/R4_BREAK] side=PUT reason=... close=... s4=... s4_relief_thr=... atr=...
_RE_OSC_RELIEF = re.compile(r"\[OSC_RELIEF\]\[S4/R4_BREAK\]", re.IGNORECASE)

# [CONTRACT_ROLL] symbol=NIFTY old_expiry=2026-03-06 new_expiry=2026-03-13
_RE_CONTRACT_ROLL = re.compile(r"\[CONTRACT_ROLL\]", re.IGNORECASE)

# [CONTRACT_FILTER] symbol=... strike=... intrinsic=0.00 → SKIPPED
_RE_CONTRACT_FILTER_SKIP = re.compile(
    r"\[CONTRACT_FILTER\].*intrinsic=0\.00.*SKIPPED", re.IGNORECASE
)

# [EXPIRY_ROLL][SCORE_BONUS]  — emitted by entry_logic per side per bar
_RE_EXPIRY_ROLL_BONUS = re.compile(r"\[EXPIRY_ROLL\]\[SCORE_BONUS\]", re.IGNORECASE)

# [CONTRACT_METADATA][LOT_MISMATCH]
_RE_LOT_MISMATCH = re.compile(
    r"\[CONTRACT_METADATA\]\[LOT_MISMATCH\]", re.IGNORECASE
)

# [LOT_SIZE] symbol=NIFTY applied=2 source=config
_RE_LOT_SIZE = re.compile(r"\[LOT_SIZE\]", re.IGNORECASE)

# [VIX_CONTEXT] symbol=NSE:INDIAVIX-INDEX value=13.50 tier=CALM
_RE_VIX_CONTEXT = re.compile(r"\[VIX_CONTEXT\]", re.IGNORECASE)

# [GREEKS] symbol=... delta=... gamma=... theta=... vega=... iv=...
_RE_GREEKS = re.compile(r"\[GREEKS\]", re.IGNORECASE)

# [VOL_CONTEXT][SCORE_ADJUST][CALL/PUT] vix_tier=... vol_adj=... theta=... theta_adj=... vega=...
# theta_penalty fires when theta_adj is negative (e.g. theta_adj=-8)
_RE_VOL_CONTEXT_ADJUST = re.compile(
    r"\[VOL_CONTEXT\]\[SCORE_ADJUST\]"
    r"(?:.*?theta_adj=(?P<theta_adj>-?\d+))?",
    re.IGNORECASE,
)

# [POSITION_SIZE] equity=N/A score=... atr=... vix_tier=... vega_high=True/False lots=...
_RE_POSITION_SIZE_VOL = re.compile(
    r"\[POSITION_SIZE\]"
    r"(?:.*?vega_high=(?P<vega_high>True|False))?",
    re.IGNORECASE,
)

# [VOL_CONTEXT][ALIGN][CALL/PUT] indicators=RSI:... vix_tier=... adj=score:...
_RE_VOL_CONTEXT_ALIGN = re.compile(r"\[VOL_CONTEXT\]\[ALIGN\]", re.IGNORECASE)

# [GREEKS_ALIGN][CALL/PUT] symbol=... theta=... vega=... adj=theta:..._vega_risk:...
_RE_GREEKS_ALIGN = re.compile(r"\[GREEKS_ALIGN\]", re.IGNORECASE)

# [SCORE_MATRIX][CALL/PUT] base=... vol_adj=... theta_adj=... final=.../...
_RE_SCORE_MATRIX = re.compile(r"\[SCORE_MATRIX\]", re.IGNORECASE)

# [REVERSAL_SIGNAL] CALL score=72 strength=HIGH stretch=-1.87x ATR pivot=S4 ...
_RE_REVERSAL_SIGNAL = re.compile(
    r"\[REVERSAL_SIGNAL\] (?P<rev_side>CALL|PUT) "
    r"score=(?P<rev_score>\d+) "
    r"strength=(?P<rev_strength>\w+)",
    re.IGNORECASE,
)

# [ENTRY ALLOWED][ST_BIAS_OK] ... osc_context=ZoneA-Blocker ...
_RE_ENTRY_ALLOWED_ZONE = re.compile(
    r"\[ENTRY ALLOWED\]\[ST_BIAS_OK\].*?osc_context=(?P<zone>ZoneA|ZoneB|ZoneC)",
    re.IGNORECASE,
)

# [REGIME_CONTEXT] 2026-03-05 09:45:00 NSE:NIFTY ATR=120.5(ATR_MODERATE) ADX=28.3(ADX_DEFAULT) day=TREND_DAY cpr=NARROW ...
_RE_REGIME_CONTEXT = re.compile(
    r"\[REGIME_CONTEXT\]"
    r"(?:.*?ATR=[\d.]+\((?P<atr_regime>\w+)\))?"
    r"(?:.*?ADX=[\d.]+\((?P<adx_tier>\w+)\))?"
    r"(?:.*?day=(?P<day_type>\w+))?"
    r"(?:.*?cpr=(?P<cpr_width>\w+))?",
    re.IGNORECASE,
)

# [EXIT AUDIT][REGIME_ADAPTIVE] day_type=TREND_DAY adx_tier=ADX_STRONG_40 gap_tag=NO_GAP ...
_RE_EXIT_AUDIT_REGIME = re.compile(
    r"\[EXIT AUDIT\]\[REGIME_ADAPTIVE\]"
    r"(?:.*?day_type=(?P<day_type>\w+))?"
    r"(?:.*?adx_tier=(?P<adx_tier>\w+))?"
    r"(?:.*?gap_tag=(?P<gap_tag>\w+))?",
    re.IGNORECASE,
)

# [REVERSAL_OVERRIDE] RSI=22.3 oscillator extreme flipped ...
_RE_REVERSAL_OVERRIDE = re.compile(r"\[REVERSAL_OVERRIDE\]", re.IGNORECASE)

# Phase 6: Bias alignment & microstructure
# [BIAS_ALIGNMENT] side=CALL status=ALIGNED tf=3m ...
_RE_BIAS_ALIGNMENT = re.compile(
    r"\[BIAS_ALIGNMENT\] side=(?P<side>CALL|PUT) "
    r"status=(?P<status>ALIGNED|MISALIGNED|NEUTRAL)"
    r"(?: tf=(?P<tf>\w+))?",
    re.IGNORECASE,
)
# [SLOPE_OVERRIDE_TIME] timestamp=... symbol=... side=... bars=5
_RE_SLOPE_OVERRIDE_TIME = re.compile(r"\[SLOPE_OVERRIDE_TIME\]", re.IGNORECASE)
# [CONFLICT_BLOCKED] timestamp=... symbol=... side=... type=...
_RE_CONFLICT_BLOCKED = re.compile(r"\[CONFLICT_BLOCKED\]", re.IGNORECASE)
# [PULSE_EXHAUSTION] peak=... current=... decay_ratio=...
_RE_PULSE_EXHAUSTION = re.compile(r"\[PULSE_EXHAUSTION\]", re.IGNORECASE)
# [ZONE_ABSORPTION] zone=... type=... touches=... window=...
_RE_ZONE_ABSORPTION = re.compile(r"\[ZONE_ABSORPTION\]", re.IGNORECASE)
# [SPREAD_NOISE] close_open_drift=... or bar_range=...
_RE_SPREAD_NOISE = re.compile(r"\[SPREAD_NOISE\]", re.IGNORECASE)
# [BAR_CLOSE_ALIGNMENT][TF=3m] or [BAR_CLOSE_ALIGNMENT][TF=15m]
_RE_BAR_CLOSE_ALIGNMENT = re.compile(
    r"\[BAR_CLOSE_ALIGNMENT\]\[TF=(?P<tf>3m|15m)\]",
    re.IGNORECASE,
)

# Phase 6.1: Tilt-based governance
# [TILT_STATE=BULLISH_TILT] side=CALL close=22500.00
_RE_TILT_STATE = re.compile(
    r"\[TILT_STATE=(?P<tilt>BULLISH_TILT|BEARISH_TILT|NEUTRAL)\]",
    re.IGNORECASE,
)
# [GOVERNANCE_EASY] or [GOVERNANCE_STRICT]
_RE_GOVERNANCE_EASY = re.compile(r"\[GOVERNANCE_EASY\]", re.IGNORECASE)
_RE_GOVERNANCE_STRICT = re.compile(r"\[GOVERNANCE_STRICT\]", re.IGNORECASE)
# Phase 6.1.2: [GOVERNANCE_EASY][BIAS_MISALIGN_BYPASSED]
_RE_TILT_BIAS_OVERRIDE = re.compile(r"\[GOVERNANCE_EASY\]\[BIAS_MISALIGN_BYPASSED\]", re.IGNORECASE)

# [ENTRY ALLOWED][ST_SLOPE_OVERRIDE]
_RE_ST_SLOPE_OVERRIDE = re.compile(r"\[ENTRY ALLOWED\]\[ST_SLOPE_OVERRIDE\]", re.IGNORECASE)

# Phase 6.2: Trend continuation
# ── Phase 6.3 regex patterns ──────────────────────────────────────────────────
_RE_OSC_TREND_OVERRIDE = re.compile(r"\[OSC_TREND_OVERRIDE\]", re.IGNORECASE)
_RE_TREND_ALIGN_OVERRIDE = re.compile(r"\[TREND_ALIGN_OVERRIDE\]", re.IGNORECASE)
_RE_DAY_BIAS_PENALTY = re.compile(r"\[DAY_BIAS_PENALTY\]", re.IGNORECASE)
_RE_MOMENTUM_ENTRY = re.compile(r"\[MOMENTUM_ENTRY\]", re.IGNORECASE)
_RE_SIGNAL_SKIP = re.compile(r"\[SIGNAL_SKIP\]", re.IGNORECASE)
_RE_COOLDOWN_REDUCED = re.compile(r"\[COOLDOWN_REDUCED\]", re.IGNORECASE)
_RE_SIGNAL_FIRED = re.compile(
    r"\[SIGNAL FIRED\]\s+side=(?P<side>CALL|PUT)\s+reason=(?P<reason>[\w_]+)"
    r".*?score=(?P<score>\d+)/(?P<thresh>\d+)",
    re.IGNORECASE,
)
_RE_SIGNAL_BLOCKED = re.compile(
    r"\[SIGNAL BLOCKED\]\s+reason=(?P<reason>[^|]+)",
    re.IGNORECASE,
)
_RE_SIGNAL_OVERRIDE = re.compile(r"\[SIGNAL OVERRIDE\]", re.IGNORECASE)
_RE_SIGNAL_OVERRIDE_TREND = re.compile(r"\[SIGNAL OVERRIDE [–-] TREND MODE\]", re.IGNORECASE)
_RE_TREND_DAY_DETECTED = re.compile(r"\[TREND DAY DETECTED\]", re.IGNORECASE)
_RE_TREND_REENTRY = re.compile(r"\[TREND REENTRY\]", re.IGNORECASE)
_RE_PIVOT_ACCEPTED = re.compile(r"\[PIVOT ACCEPTED\]", re.IGNORECASE)
_RE_PIVOT_REJECTED = re.compile(r"\[PIVOT REJECTED\]", re.IGNORECASE)
_RE_REVERSAL_ENTRY = re.compile(r"\[REVERSAL ENTRY\]", re.IGNORECASE)
_RE_LS_HIGH = re.compile(r"\[LIQUIDITY SWEEP HIGH\]", re.IGNORECASE)
_RE_LS_LOW  = re.compile(r"\[LIQUIDITY SWEEP LOW\]", re.IGNORECASE)

# ── Phase 6.4 regex patterns ──────────────────────────────────────────────────
_RE_REVERSAL_EMA_STRETCH = re.compile(r"\[REVERSAL_EMA_STRETCH\]", re.IGNORECASE)
_RE_REVERSAL_PIVOT_CONFIRM = re.compile(r"\[REVERSAL_PIVOT_CONFIRM\]", re.IGNORECASE)
_RE_REVERSAL_OSC_CONFIRM = re.compile(r"\[REVERSAL_OSC_CONFIRM\]", re.IGNORECASE)
_RE_REVERSAL_BIAS_FLIP = re.compile(r"\[REVERSAL_BIAS_FLIP\]", re.IGNORECASE)
_RE_REVERSAL_COOLDOWN_RELAX = re.compile(r"\[REVERSAL_COOLDOWN_RELAX\]", re.IGNORECASE)
_RE_REVERSAL_PERSIST = re.compile(r"\[REVERSAL_PERSIST\]", re.IGNORECASE)
_RE_ST_OPENING_RELAX = re.compile(r"\[ST_OPENING_RELAX\]", re.IGNORECASE)
_RE_REVERSAL_CAPTURE = re.compile(r"\[REVERSAL_CAPTURE\]", re.IGNORECASE)

# [TREND_CONTINUATION][ACTIVATED] bar=120 side=PUT consec_bars=15 ADX=28.3 ...
_RE_TREND_CONT_ACTIVATED = re.compile(
    r"\[TREND_CONTINUATION\]\[ACTIVATED\].*?side=(?P<side>CALL|PUT)",
    re.IGNORECASE,
)
# [TREND_CONTINUATION][ENTRY] bar=130 PUT #2 close=22100.00 ...
_RE_TREND_CONT_ENTRY = re.compile(
    r"\[TREND_CONTINUATION\]\[ENTRY\].*?(?P<side>CALL|PUT)\s+#(?P<num>\d+)",
    re.IGNORECASE,
)
# [TREND_CONTINUATION][DEACTIVATED] bar=200 ...
_RE_TREND_CONT_DEACTIVATED = re.compile(
    r"\[TREND_CONTINUATION\]\[DEACTIVATED\]", re.IGNORECASE,
)


# ── SessionSummary dataclass ──────────────────────────────────────────────────

@dataclass
class SessionSummary:
    """Aggregated statistics for a single log-file session."""

    log_path:       str
    session_type:   str                  # REPLAY, PAPER, LIVE, or MIXED
    date_tag:       str                  # e.g. "2026-02-24"

    trades:         List[dict] = field(default_factory=list)
    blocked_counts: Dict[str, int] = field(default_factory=dict)
    tag_counts:     Dict[str, int] = field(default_factory=dict)
    signals_fired:  int = 0
    entry_ok_count: int = 0
    open_bias_tag:  str = "NONE"               # P5-A: OPEN_HIGH | OPEN_LOW | NONE
    vs_close_tag:   str = "OPEN_CLOSE_EQUAL"  # P5-B: OPEN_ABOVE_CLOSE | OPEN_BELOW_CLOSE | OPEN_CLOSE_EQUAL
    gap_tag:        str = "NO_GAP"            # P5-C: GAP_UP | GAP_DOWN | NO_GAP
    balance_tag:    str = "OUTSIDE_BALANCE"   # P5-D: BALANCE_OPEN | OUTSIDE_BALANCE
    day_type_tag:   str = "NEUTRAL_DAY"       # TREND_DAY | RANGE_DAY | GAP_DAY | BALANCE_DAY | NEUTRAL_DAY
    cpr_width_tag:  str = "NORMAL"            # NARROW | NORMAL | WIDE
    reversal_trades_count: int = 0            # trades opened via REVERSAL_OVERRIDE path
    reversal_signal_count: int = 0           # [REVERSAL_SIGNAL] detector firings
    st_slope_override_count: int = 0         # ST_SLOPE_CONFLICT → OVERRIDE count
    oscillator_blocks: int = 0               # [OSC_EXTREME] blocked entries
    oscillator_overrides: int = 0            # [OSC_OVERRIDE][TREND_CONFIRMED] allowed entries
    oscillator_relief_count: int = 0         # [OSC_RELIEF][S4/R4_BREAK] relief overrides
    trend_loss_count: int = 0               # [TREND_LOSS] — trend trade stopped out prematurely
    zone_entry_counts: Dict[str, int] = field(default_factory=dict)  # ZoneA/B/C allowed entries

    # ── Contract / expiry tracking ────────────────────────────────────────────
    expiry_roll_count:       int = 0   # [CONTRACT_ROLL] events
    lot_size_mismatch_count: int = 0   # [CONTRACT_METADATA][LOT_MISMATCH] events
    intrinsic_filter_count:  int = 0   # [CONTRACT_FILTER]...SKIPPED events

    # ── Regime context attribution (Phase 5) ────────────────────────────────
    regime_context_count: int = 0   # [REGIME_CONTEXT] per-bar log count
    regime_adaptive_count: int = 0  # [EXIT AUDIT][REGIME_ADAPTIVE] per-trade
    regime_trade_breakdown: Dict[str, Dict[str, list]] = field(default_factory=dict)
    # Structure: {"day_type": {"TREND_DAY": [pnl1, ...], ...}, "adx_tier": {...}, ...}

    # ── Phase 6: Bias alignment & microstructure ──────────────────────────────
    bias_alignment_count: int = 0        # [BIAS_ALIGNMENT] events
    bar_close_alignment_count: int = 0   # [BAR_CLOSE_ALIGNMENT][TF=...] events
    slope_override_time_count: int = 0   # [SLOPE_OVERRIDE_TIME] events
    conflict_blocked_count: int = 0      # [CONFLICT_BLOCKED] events
    pulse_exhaustion_count: int = 0      # [PULSE_EXHAUSTION] events
    zone_absorption_count: int = 0       # [ZONE_ABSORPTION] events
    spread_noise_count: int = 0          # [SPREAD_NOISE] events
    signals_fired: int = 0               # [SIGNAL FIRED]
    signals_blocked: int = 0             # [SIGNAL BLOCKED]

    # ── Phase 6.1: Tilt-based governance ────────────────────────────────────────
    tilt_state_count: int = 0            # [TILT_STATE=...] events
    governance_easy_count: int = 0       # [GOVERNANCE_EASY] events
    governance_strict_count: int = 0     # [GOVERNANCE_STRICT] events
    tilt_bias_override_count: int = 0    # [GOVERNANCE_EASY][BIAS_MISALIGN_BYPASSED] events

    # ── Phase 6.2: Trend continuation ────────────────────────────────────────
    trend_continuation_activations: int = 0  # [TREND_CONTINUATION][ACTIVATED]
    trend_continuation_entries: int = 0      # [TREND_CONTINUATION][ENTRY] count
    trend_continuation_deactivations: int = 0  # [TREND_CONTINUATION][DEACTIVATED]
    trend_continuation_side: str = ""        # last activated side (CALL/PUT)

    # ── Phase 6.3: Regime-aware fixes ──────────────────────────────────────────
    osc_trend_override_count: int = 0       # [OSC_TREND_OVERRIDE] RSI floor removed
    trend_align_override_count: int = 0     # [TREND_ALIGN_OVERRIDE] ADX-based alignment
    day_bias_penalty_count: int = 0         # [DAY_BIAS_PENALTY] counter-trend penalized
    momentum_entry_count: int = 0           # [MOMENTUM_ENTRY] momentum path fired
    signal_skip_count: int = 0              # [SIGNAL_SKIP] gate open but no signal
    cooldown_reduced_count: int = 0         # [COOLDOWN_REDUCED] regime cooldown applied

    # ── Phase 6.4: Reversal Detection + Opening ST Relaxation ─────────────────
    reversal_ema_stretch_count: int = 0    # [REVERSAL_EMA_STRETCH] EMA stretch reversal gate
    reversal_pivot_confirm_count: int = 0  # [REVERSAL_PIVOT_CONFIRM] pivot-confirmed reversal
    reversal_osc_confirm_count: int = 0    # [REVERSAL_OSC_CONFIRM] oscillator as confirmation
    reversal_bias_flip_count: int = 0      # [REVERSAL_BIAS_FLIP] ST bias flip near reversal
    reversal_cooldown_relax_count: int = 0 # [REVERSAL_COOLDOWN_RELAX] cooldown reduced for reversal
    reversal_persist_count: int = 0        # [REVERSAL_PERSIST] signal persisted across bars
    st_opening_relax_count: int = 0        # [ST_OPENING_RELAX] opening window ST relaxation
    reversal_capture_count: int = 0        # [REVERSAL_CAPTURE] reversal actually captured

    # ── Volatility context tracking ───────────────────────────────────────────
    vix_tier_count:     int = 0   # [VIX_CONTEXT] refreshes logged
    greeks_usage_count: int = 0   # [GREEKS] computations logged
    theta_penalty_count: int = 0  # [VOL_CONTEXT][SCORE_ADJUST] with theta_adj < 0
    vega_penalty_count:  int = 0  # [POSITION_SIZE] entries with vega_high=True
    vol_context_align_count:  int = 0  # [VOL_CONTEXT][ALIGN] per-side events
    greeks_align_count:       int = 0  # [GREEKS_ALIGN] per-side events
    score_matrix_usage_count: int = 0  # [SCORE_MATRIX] per-side audit events

    # Computed on demand
    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def winners(self) -> int:
        return sum(1 for t in self.trades if t.get("pnl_pts", 0.0) > 0)

    @property
    def losers(self) -> int:
        return sum(1 for t in self.trades if t.get("pnl_pts", 0.0) < 0)

    @property
    def breakeven(self) -> int:
        return sum(1 for t in self.trades if t.get("pnl_pts", 0.0) == 0)

    @property
    def win_rate_pct(self) -> float:
        return round(self.winners / self.total_trades * 100, 1) if self.total_trades else 0.0

    @property
    def net_pnl_pts(self) -> float:
        return round(sum(t.get("pnl_pts", 0.0) for t in self.trades), 2)

    @property
    def net_pnl_rs(self) -> float:
        return round(sum(t.get("pnl_rs", 0.0) for t in self.trades), 2)

    @property
    def call_trades(self) -> int:
        return sum(1 for t in self.trades if t.get("side") == "CALL")

    @property
    def put_trades(self) -> int:
        return sum(1 for t in self.trades if t.get("side") == "PUT")

    @property
    def total_blocked(self) -> int:
        return sum(self.blocked_counts.values())

    @property
    def exit_reason_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = defaultdict(int)
        for t in self.trades:
            reason = t.get("exit_reason") or t.get("outcome") or "UNKNOWN"
            counts[reason] += 1
        return dict(counts)

    @property
    def reversal_pnl_attribution(self) -> float:
        """Net P&L (pts) from trades opened via the reversal override path."""
        return round(
            sum(t.get("pnl_pts", 0.0) for t in self.trades
                if t.get("src", "").upper() == "REVERSAL_OVERRIDE"
                or t.get("reversal_override", False)),
            2,
        )

    @property
    def open_bias_stats(self) -> dict:
        """P5-F: Alignment of executed trades vs. session open bias (all P5 tags)."""
        aligned    = [t for t in self.trades if t.get("open_bias_aligned") == "ALIGNED"]
        misaligned = [t for t in self.trades if t.get("open_bias_aligned") == "MISALIGNED"]
        neutral    = [t for t in self.trades if t.get("open_bias_aligned") == "NEUTRAL"]
        aligned_pnl    = round(sum(t.get("pnl_pts", 0) for t in aligned), 2)
        misaligned_pnl = round(sum(t.get("pnl_pts", 0) for t in misaligned), 2)
        total = self.total_trades
        is_gap_day     = self.gap_tag in ("GAP_UP", "GAP_DOWN")
        is_balance_day = self.balance_tag == "BALANCE_OPEN"
        all_pnl        = round(sum(t.get("pnl_pts", 0) for t in self.trades), 2)
        return {
            "open_bias_tag":    self.open_bias_tag,
            "vs_close_tag":     self.vs_close_tag,
            "gap_tag":          self.gap_tag,
            "balance_tag":      self.balance_tag,
            "aligned_count":    len(aligned),
            "misaligned_count": len(misaligned),
            "neutral_count":    len(neutral),
            "aligned_pnl":      aligned_pnl,
            "misaligned_pnl":   misaligned_pnl,
            "pct_aligned":      round(len(aligned) / total * 100, 1) if total else 0.0,
            "is_gap_day":       is_gap_day,
            "is_balance_day":   is_balance_day,
            "gap_day_pnl":      all_pnl if is_gap_day else 0.0,
            "balance_day_pnl":  all_pnl if is_balance_day else 0.0,
        }

    @property
    def regime_performance(self) -> Dict[str, Dict[str, dict]]:
        """Performance breakdown by regime dimensions (Phase 5).

        Returns dict like:
        {
            "day_type": {
                "TREND_DAY": {"trades": 5, "winners": 3, "win_rate": 60.0, "net_pnl": 12.5},
                ...
            },
            "adx_tier": {...},
            "atr_regime": {...},
            "cpr_width": {...},
        }
        """
        result = {}
        for dim in ("day_type", "adx_tier", "atr_regime", "cpr_width"):
            dim_data = self.regime_trade_breakdown.get(dim, {})
            dim_perf = {}
            for label, pnl_list in dim_data.items():
                total = len(pnl_list)
                wins = sum(1 for p in pnl_list if p > 0)
                net = round(sum(pnl_list), 2)
                dim_perf[label] = {
                    "trades": total,
                    "winners": wins,
                    "win_rate": round(wins / total * 100, 1) if total else 0.0,
                    "net_pnl": net,
                }
            result[dim] = dim_perf
        return result

    @property
    def lot_cap_count(self) -> int:
        """Trades blocked (or capped) by MAX_TRADES_CAP gate."""
        return self.tag_counts.get("MAX_TRADES_CAP", 0)

    @property
    def survivability_count(self) -> int:
        """Trades that held for >= 3 bars (9 min on 3m chart)."""
        return sum(1 for t in self.trades if (t.get("bars_held") or 0) >= 3)

    @property
    def survivability_ratio(self) -> float:
        """Percentage of trades that survived >= 3 bars."""
        return (
            round(self.survivability_count / self.total_trades * 100, 1)
            if self.total_trades else 0.0
        )

    @property
    def avg_pnl_pts(self) -> float:
        """Average P&L per trade in points."""
        return (
            round(self.net_pnl_pts / self.total_trades, 2)
            if self.total_trades else 0.0
        )

    @property
    def bias_alignment_performance(self) -> Dict[str, dict]:
        """Performance breakdown by bias alignment status (Phase 6)."""
        groups: Dict[str, list] = {"ALIGNED": [], "MISALIGNED": [], "NEUTRAL": []}
        for t in self.trades:
            status = t.get("bias_alignment", "NEUTRAL")
            if status not in groups:
                status = "NEUTRAL"
            groups[status].append(t.get("pnl_pts", 0.0))
        result = {}
        for status, pnl_list in groups.items():
            total = len(pnl_list)
            if total == 0:
                continue
            wins = sum(1 for p in pnl_list if p > 0)
            net = round(sum(pnl_list), 2)
            result[status] = {
                "trades": total, "winners": wins,
                "win_rate": round(wins / total * 100, 1), "net_pnl": net,
            }
        return result

    @property
    def microstructure_counts(self) -> dict:
        """Microstructure proxy counts (Phase 6)."""
        return {
            "pulse_exhaustion": self.pulse_exhaustion_count,
            "zone_absorption": self.zone_absorption_count,
            "spread_noise": self.spread_noise_count,
        }

    @property
    def tilt_performance(self) -> Dict[str, dict]:
        """Performance breakdown by tilt state (Phase 6.1)."""
        groups: Dict[str, list] = {"BULLISH_TILT": [], "BEARISH_TILT": [], "NEUTRAL": []}
        for t in self.trades:
            tilt = t.get("tilt_state", "NEUTRAL")
            if tilt not in groups:
                tilt = "NEUTRAL"
            groups[tilt].append(t.get("pnl_pts", 0.0))
        result = {}
        for tilt, pnl_list in groups.items():
            total = len(pnl_list)
            if total == 0:
                continue
            wins = sum(1 for p in pnl_list if p > 0)
            net = round(sum(pnl_list), 2)
            result[tilt] = {
                "trades": total, "winners": wins,
                "win_rate": round(wins / total * 100, 1), "net_pnl": net,
            }
        return result

    def to_dict(self) -> dict:
        return {
            "log_path":        self.log_path,
            "session_type":    self.session_type,
            "date_tag":        self.date_tag,
            "total_trades":    self.total_trades,
            "winners":         self.winners,
            "losers":          self.losers,
            "breakeven":       self.breakeven,
            "win_rate_pct":    self.win_rate_pct,
            "net_pnl_pts":     self.net_pnl_pts,
            "net_pnl_rs":      self.net_pnl_rs,
            "call_trades":     self.call_trades,
            "put_trades":      self.put_trades,
            "signals_fired":   self.signals_fired,
            "entry_ok_count":  self.entry_ok_count,
            "total_blocked":   self.total_blocked,
            "blocked_counts":  self.blocked_counts,
            "tag_counts":      self.tag_counts,
            "exit_reasons":    self.exit_reason_counts,
            "open_bias_tag":            self.open_bias_tag,
            "vs_close_tag":             self.vs_close_tag,
            "gap_tag":                  self.gap_tag,
            "balance_tag":              self.balance_tag,
            "day_type_tag":             self.day_type_tag,
            "cpr_width_tag":            self.cpr_width_tag,
            "reversal_trades_count":    self.reversal_trades_count,
            "reversal_signal_count":    self.reversal_signal_count,
            "reversal_pnl_attribution": self.reversal_pnl_attribution,
            "st_slope_override_count":  self.st_slope_override_count,
            "oscillator_blocks":        self.oscillator_blocks,
            "oscillator_overrides":     self.oscillator_overrides,
            "oscillator_relief_count":   self.oscillator_relief_count,
            "trend_loss_count":          self.trend_loss_count,
            "zone_entry_counts":         self.zone_entry_counts,
            "open_bias_stats":           self.open_bias_stats,
            "expiry_roll_count":         self.expiry_roll_count,
            "lot_size_mismatch_count":   self.lot_size_mismatch_count,
            "intrinsic_filter_count":    self.intrinsic_filter_count,
            "vix_tier_count":            self.vix_tier_count,
            "greeks_usage_count":        self.greeks_usage_count,
            "theta_penalty_count":       self.theta_penalty_count,
            "vega_penalty_count":        self.vega_penalty_count,
            "vol_context_align_count":   self.vol_context_align_count,
            "greeks_align_count":        self.greeks_align_count,
            "score_matrix_usage_count":  self.score_matrix_usage_count,
            "lot_cap_count":             self.lot_cap_count,
            "survivability_count":       self.survivability_count,
            "survivability_ratio":       self.survivability_ratio,
            "avg_pnl_pts":               self.avg_pnl_pts,
            "regime_context_count":      self.regime_context_count,
            "regime_adaptive_count":     self.regime_adaptive_count,
            "regime_performance":        self.regime_performance,
            # Phase 6
            "bias_alignment_count":      self.bias_alignment_count,
            "bar_close_alignment_count": self.bar_close_alignment_count,
            "slope_override_time_count": self.slope_override_time_count,
            "conflict_blocked_count":    self.conflict_blocked_count,
            "bias_alignment_performance": self.bias_alignment_performance,
            "microstructure_counts":     self.microstructure_counts,
            # Phase 6.1
            "tilt_state_count":          self.tilt_state_count,
            "governance_easy_count":     self.governance_easy_count,
            "governance_strict_count":   self.governance_strict_count,
            "tilt_bias_override_count":  self.tilt_bias_override_count,
            "tilt_performance":          self.tilt_performance,
            # Phase 6.2
            "trend_continuation_activations": self.trend_continuation_activations,
            "trend_continuation_entries":     self.trend_continuation_entries,
            "trend_continuation_deactivations": self.trend_continuation_deactivations,
            "trend_continuation_side":       self.trend_continuation_side,
            # Phase 6.3
            "osc_trend_override_count":      self.osc_trend_override_count,
            "trend_align_override_count":    self.trend_align_override_count,
            "day_bias_penalty_count":        self.day_bias_penalty_count,
            "momentum_entry_count":          self.momentum_entry_count,
            "signal_skip_count":             self.signal_skip_count,
            "cooldown_reduced_count":        self.cooldown_reduced_count,
            # Phase 6.4
            "reversal_ema_stretch_count":    self.reversal_ema_stretch_count,
            "reversal_pivot_confirm_count":  self.reversal_pivot_confirm_count,
            "reversal_osc_confirm_count":    self.reversal_osc_confirm_count,
            "reversal_bias_flip_count":      self.reversal_bias_flip_count,
            "reversal_cooldown_relax_count": self.reversal_cooldown_relax_count,
            "reversal_persist_count":        self.reversal_persist_count,
            "st_opening_relax_count":        self.st_opening_relax_count,
            "reversal_capture_count":        self.reversal_capture_count,
        }


# ── LogParser ─────────────────────────────────────────────────────────────────

class LogParser:
    """Parse a single trading engine log file into a SessionSummary.

    Supports two trade-record formats:
      1. New format: [TRADE OPEN] + [TRADE EXIT] pairs (replay / paper / live)
      2. Legacy format: [EXIT AUDIT] lines only

    When both are present, new-format pairs take precedence and EXIT AUDIT
    records are merged in for any remaining unmatched exits.
    """

    def __init__(self, log_path: str | Path) -> None:
        self.log_path = Path(log_path)

    # ── public ────────────────────────────────────────────────────────────────

    def parse(self) -> SessionSummary:
        """Parse the log file and return a SessionSummary."""
        if not self.log_path.exists():
            return SessionSummary(
                log_path=str(self.log_path),
                session_type="UNKNOWN",
                date_tag="",
            )

        date_tag = self._extract_date_tag()
        (trades, session_types, blocked, tags, signals, signals_blocked, ok_count,
         open_bias_tag, vs_close_tag, gap_tag, balance_tag,
         day_type_tag, cpr_width_tag, reversal_count, slope_override_count,
         osc_blocks, osc_overrides, osc_relief_count, trend_loss_count,
         expiry_roll_count, lot_size_mismatch_count, intrinsic_filter_count,
         vix_tier_count, greeks_usage_count, theta_penalty_count, vega_penalty_count,
         vol_context_align_count, greeks_align_count, score_matrix_usage_count,
         reversal_signal_count, zone_entry_counts,
         regime_context_count, regime_adaptive_count, regime_trade_breakdown,
         # Phase 6
         p6_bias_align, p6_bar_close, p6_slope_time, p6_conflict_blocked,
         p6_pulse_exhaust, p6_zone_absorb, p6_spread_noise,
         # Phase 6.1
         p61_tilt_state, p61_gov_easy, p61_gov_strict,
         p612_tilt_bias_override,
         # Phase 6.2
         p62_tc_activations, p62_tc_entries,
         p62_tc_deactivations, p62_tc_side,
         # Phase 6.3
         osc_trend_override_count, trend_align_override_count,
         day_bias_penalty_count, momentum_entry_count,
         signal_skip_count, cooldown_reduced_count,
         # Phase 6.4
         reversal_ema_stretch_count, reversal_pivot_confirm_count,
         reversal_osc_confirm_count, reversal_bias_flip_count,
         reversal_cooldown_relax_count, reversal_persist_count,
         st_opening_relax_count, reversal_capture_count,
         ) = self._scan_file()

        if session_types:
            session_type = session_types.pop() if len(session_types) == 1 else "MIXED"
        else:
            session_type = "UNKNOWN"

        return SessionSummary(
            log_path=str(self.log_path),
            session_type=session_type,
            date_tag=date_tag,
            trades=trades,
            blocked_counts=dict(blocked),
            tag_counts=dict(tags),
            signals_fired=signals,
            signals_blocked=signals_blocked,
            entry_ok_count=ok_count,
            open_bias_tag=open_bias_tag,
            vs_close_tag=vs_close_tag,
            gap_tag=gap_tag,
            balance_tag=balance_tag,
            day_type_tag=day_type_tag,
            cpr_width_tag=cpr_width_tag,
            reversal_trades_count=reversal_count,
            reversal_signal_count=reversal_signal_count,
            st_slope_override_count=slope_override_count,
            oscillator_blocks=osc_blocks,
            oscillator_overrides=osc_overrides,
            oscillator_relief_count=osc_relief_count,
            trend_loss_count=trend_loss_count,
            expiry_roll_count=expiry_roll_count,
            lot_size_mismatch_count=lot_size_mismatch_count,
            intrinsic_filter_count=intrinsic_filter_count,
            vix_tier_count=vix_tier_count,
            greeks_usage_count=greeks_usage_count,
            theta_penalty_count=theta_penalty_count,
            vega_penalty_count=vega_penalty_count,
            vol_context_align_count=vol_context_align_count,
            greeks_align_count=greeks_align_count,
            score_matrix_usage_count=score_matrix_usage_count,
            zone_entry_counts=zone_entry_counts,
            regime_context_count=regime_context_count,
            regime_adaptive_count=regime_adaptive_count,
            regime_trade_breakdown=regime_trade_breakdown,
            # Phase 6
            bias_alignment_count=p6_bias_align,
            bar_close_alignment_count=p6_bar_close,
            slope_override_time_count=p6_slope_time,
            conflict_blocked_count=p6_conflict_blocked,
            pulse_exhaustion_count=p6_pulse_exhaust,
            zone_absorption_count=p6_zone_absorb,
            spread_noise_count=p6_spread_noise,
            # Phase 6.1
            tilt_state_count=p61_tilt_state,
            governance_easy_count=p61_gov_easy,
            governance_strict_count=p61_gov_strict,
            tilt_bias_override_count=p612_tilt_bias_override,
            # Phase 6.2
            trend_continuation_activations=p62_tc_activations,
            trend_continuation_entries=p62_tc_entries,
            trend_continuation_deactivations=p62_tc_deactivations,
            trend_continuation_side=p62_tc_side,
            # Phase 6.3
            osc_trend_override_count=osc_trend_override_count,
            trend_align_override_count=trend_align_override_count,
            day_bias_penalty_count=day_bias_penalty_count,
            momentum_entry_count=momentum_entry_count,
            signal_skip_count=signal_skip_count,
            cooldown_reduced_count=cooldown_reduced_count,
            # Phase 6.4
            reversal_ema_stretch_count=reversal_ema_stretch_count,
            reversal_pivot_confirm_count=reversal_pivot_confirm_count,
            reversal_osc_confirm_count=reversal_osc_confirm_count,
            reversal_bias_flip_count=reversal_bias_flip_count,
            reversal_cooldown_relax_count=reversal_cooldown_relax_count,
            reversal_persist_count=reversal_persist_count,
            st_opening_relax_count=st_opening_relax_count,
            reversal_capture_count=reversal_capture_count,
        )

    # ── private ───────────────────────────────────────────────────────────────

    def _extract_date_tag(self) -> str:
        """Return YYYY-MM-DD from the log filename, or empty string."""
        m = re.search(r"(\d{4}-\d{2}-\d{2})", self.log_path.name)
        return m.group(1) if m else ""

    def _scan_file(self):
        """Single-pass scan of the log file.

        Returns
        -------
        (trades, session_types, blocked, tags, signals_fired, signals_blocked, entry_ok_count,
         open_bias_tag, vs_close_tag, gap_tag, balance_tag,
         day_type_tag, cpr_width_tag, reversal_count, slope_override_count,
         osc_blocks, osc_overrides, osc_relief_count, trend_loss_count,
         expiry_roll_count, lot_size_mismatch_count, intrinsic_filter_count,
         vix_tier_count, greeks_usage_count, theta_penalty_count, vega_penalty_count,
         vol_context_align_count, greeks_align_count, score_matrix_usage_count,
         reversal_signal_count, zone_entry_counts,
         regime_context_count, regime_adaptive_count, regime_trade_breakdown)
        """
        trades: List[dict] = []
        open_queue: List[dict] = []   # pending TRADE OPEN records (FIFO)
        # Structured format queues (keyed by option_name for exact matching)
        open_queue_struct: Dict[str, dict] = {}
        trades_struct: List[dict] = []
        session_types: set = set()
        blocked: Dict[str, int] = defaultdict(int)
        tags: Dict[str, int] = defaultdict(int)
        signals_fired: int = 0
        signals_blocked: int = 0
        entry_ok_count: int = 0
        open_bias_tag: str = "NONE"               # P5-A: OPEN_HIGH | OPEN_LOW | NONE
        vs_close_tag:  str = "OPEN_CLOSE_EQUAL"   # P5-B
        gap_tag:       str = "NO_GAP"             # P5-C: GAP_UP | GAP_DOWN | NO_GAP
        balance_tag:   str = "OUTSIDE_BALANCE"    # P5-D: BALANCE_OPEN | OUTSIDE_BALANCE
        day_type_tag:  str = "NEUTRAL_DAY"        # TREND_DAY | RANGE_DAY | GAP_DAY | BALANCE_DAY
        cpr_width_tag: str = "NORMAL"             # NARROW | NORMAL | WIDE
        reversal_count: int = 0                   # [REVERSAL_OVERRIDE] fired count
        slope_override_count: int = 0             # [ST_SLOPE_OVERRIDE] fired count
        osc_blocks: int = 0                       # [ENTRY BLOCKED][OSC_EXTREME] count
        osc_overrides: int = 0                    # [OSC_OVERRIDE][TREND_CONFIRMED] count
        osc_relief_count: int = 0                 # [OSC_RELIEF][S4/R4_BREAK] count
        trend_loss_count: int = 0                 # [TREND_LOSS] trend trade SL count
        expiry_roll_count: int = 0                # [CONTRACT_ROLL] events
        lot_size_mismatch_count: int = 0          # [CONTRACT_METADATA][LOT_MISMATCH] events
        intrinsic_filter_count: int = 0           # [CONTRACT_FILTER]...SKIPPED events
        vix_tier_count: int = 0                   # [VIX_CONTEXT] refreshes
        greeks_usage_count: int = 0               # [GREEKS] computations
        theta_penalty_count: int = 0              # [VOL_CONTEXT][SCORE_ADJUST] theta_adj < 0
        vega_penalty_count: int = 0               # [POSITION_SIZE] vega_high=True
        vol_context_align_count: int = 0          # [VOL_CONTEXT][ALIGN] per-side events
        greeks_align_count: int = 0               # [GREEKS_ALIGN] per-side events
        score_matrix_usage_count: int = 0         # [SCORE_MATRIX] per-side events
        reversal_signal_count: int = 0            # [REVERSAL_SIGNAL] detector firings
        zone_entry_counts: Dict[str, int] = defaultdict(int)  # ZoneA/B/C allowed entries
        regime_context_count: int = 0            # [REGIME_CONTEXT] per-bar count
        regime_adaptive_count: int = 0           # [EXIT AUDIT][REGIME_ADAPTIVE] per-trade
        # Phase 6 counters
        bias_alignment_count: int = 0
        bar_close_alignment_count: int = 0
        slope_override_time_count: int = 0
        conflict_blocked_count: int = 0
        pulse_exhaustion_count: int = 0
        zone_absorption_count: int = 0
        spread_noise_count: int = 0
        # Phase 6.1 counters
        tilt_state_count: int = 0
        governance_easy_count: int = 0
        governance_strict_count: int = 0
        tilt_bias_override_count: int = 0
        # Phase 6.3 counters
        osc_trend_override_count: int = 0
        trend_align_override_count: int = 0
        day_bias_penalty_count: int = 0
        momentum_entry_count: int = 0
        signal_skip_count: int = 0
        cooldown_reduced_count: int = 0
        # Phase 6.4 counters
        reversal_ema_stretch_count: int = 0
        reversal_pivot_confirm_count: int = 0
        reversal_osc_confirm_count: int = 0
        reversal_bias_flip_count: int = 0
        reversal_cooldown_relax_count: int = 0
        reversal_persist_count: int = 0
        st_opening_relax_count: int = 0
        reversal_capture_count: int = 0
        # Phase 6.2 counters
        trend_cont_activations: int = 0
        trend_cont_entries: int = 0
        trend_cont_deactivations: int = 0
        trend_cont_side: str = ""
        _last_tilt_state: str = "NEUTRAL"        # last-seen for trade attribution
        _last_bias_alignment: str = "NEUTRAL"   # last-seen for trade attribution
        # Last-seen regime context for trade attribution
        _last_regime: dict = {"atr_regime": "UNKNOWN", "adx_tier": "UNKNOWN",
                              "day_type": "UNKNOWN", "cpr_width": "UNKNOWN"}
        # Regime breakdown: dim -> label -> [pnl_list]
        regime_breakdown: Dict[str, Dict[str, list]] = {
            "day_type": defaultdict(list), "adx_tier": defaultdict(list),
            "atr_regime": defaultdict(list), "cpr_width": defaultdict(list),
        }

        # Keep EXIT AUDIT records as fallback when no TRADE OPEN+EXIT pairs found
        audit_records: List[dict] = []
        # [EXIT][PAPER/LIVE ...] rich records — self-contained with all trade fields
        rich_exit_trades: List[dict] = []

        with self.log_path.open(encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                line = _strip(raw_line)

                # ── [TRADE OPEN][STRUCT] — structured format, exact option_name key ──
                m = _RE_TRADE_OPEN_STRUCT.search(line)
                if m:
                    d = m.groupdict()
                    log_ts = self._log_ts(line)
                    _oba = re.search(r"open_bias_aligned=(ALIGNED|MISALIGNED|NEUTRAL)", line)
                    _fb = re.search(r"\bfb=(0|1)\b", line)
                    _ema = re.search(r"\bema_stretch=(0|1)\b", line)
                    _zr = re.search(r"\bzone_revisit=(0|1)\b", line)
                    _zt = re.search(r"\bzone_type=([A-Z_]+)\b", line)
                    _za = re.search(r"\bzone_action=([A-Z_]+)\b", line)
                    _zage = re.search(r"\bzone_age=(\d+)\b", line)
                    open_queue_struct[d["option_name"]] = {
                        "side":        d["side"].upper(),
                        "bar_ts":      d["bar_ts"],
                        "log_ts":      log_ts,
                        "entry_prem":  float(d["entry"]),
                        "lot":         int(d["lots"]),
                        "option_name": d["option_name"],
                        "open_bias_aligned": _oba.group(1).upper() if _oba else "NEUTRAL",
                        "failed_breakout": bool(int(_fb.group(1))) if _fb else False,
                        "ema_stretch": bool(int(_ema.group(1))) if _ema else False,
                        "zone_revisit": bool(int(_zr.group(1))) if _zr else False,
                        "zone_revisit_type": _zt.group(1).upper() if _zt else "NONE",
                        "zone_revisit_action": _za.group(1).upper() if _za else "NONE",
                        "zone_age_bars": int(_zage.group(1)) if _zage else 0,
                        # Phase 5: regime at entry (snapshot of last-seen context)
                        "regime_at_entry": dict(_last_regime),
                        # Phase 6: bias alignment at entry
                        "bias_alignment": _last_bias_alignment,
                        "tilt_state": _last_tilt_state,
                    }
                    continue

                # ── [TRADE EXIT][STRUCT] — structured format, matched by option_name ──
                m = _RE_TRADE_EXIT_STRUCT.search(line)
                if m:
                    d = m.groupdict()
                    log_ts = self._log_ts(line)
                    exit_rec = {
                        "exit_bar_ts": d["bar_ts"],
                        "log_ts":      log_ts,
                        "option_name": d["option_name"],
                        "exit_prem":   float(d["exit_prem"]),
                        "pnl_pts":     float(d["pnl_pts"]),
                        "pnl_rs":      float(d["pnl_rs"]),
                        "bars_held":   int(d["bars_held"]),
                        "exit_reason": d["exit_reason"].upper(),
                    }
                    matched_open = open_queue_struct.pop(d["option_name"], None)
                    if matched_open:
                        trade = {**matched_open, **exit_rec}
                    else:
                        trade = exit_rec
                    trades_struct.append(trade)
                    continue

                # ── [EXIT][PAPER/LIVE ...] — rich self-contained record ───
                m = _RE_EXIT_RICH.search(line)
                if m:
                    d = m.groupdict()
                    log_ts = self._log_ts(line)
                    _mode = d.get("session_mode", "PAPER").upper()
                    session_types.add(_mode)
                    rich_exit_trades.append({
                        "session_type": _mode,
                        "side":         d["side"].upper(),
                        "bar_ts":       log_ts,
                        "log_ts":       log_ts,
                        "option_name":  d["option_name"],
                        "entry_prem":   float(d["entry_prem"]),
                        "exit_prem":    float(d["exit_prem"]),
                        "lot":          int(d["qty"]),
                        "pnl_pts":      float(d["pnl_pts"]),
                        "pnl_rs":       float(d["pnl_rs"]),
                        "bars_held":    int(d["bars_held"]),
                        "exit_reason":  d["exit_reason"].upper(),
                        "regime_at_entry": dict(_last_regime),
                        "bias_alignment": _last_bias_alignment,
                        "tilt_state": _last_tilt_state,
                    })
                    continue

                # ── [TRADE OPEN] ──────────────────────────────────────────
                m = _RE_TRADE_OPEN.search(line)
                if m:
                    d = m.groupdict()
                    log_ts = self._log_ts(line)
                    open_queue.append({
                        "session_type": d["session_type"].upper(),
                        "side":         d["side"].upper(),
                        "bar":          int(d["bar"]),
                        "bar_ts":       d["bar_ts"],
                        "log_ts":       log_ts,
                        "underlying":   float(d["underlying"]),
                        "entry_prem":   float(d["premium"]),
                        "score":        int(d["score"]),
                        "src":          d.get("src") or "",
                        "pivot":        d.get("pivot") or "",
                        "cpr":          d.get("cpr") or "",
                        "day_type":     d.get("day") or "",
                        "lot":          int(d["lot"]) if d.get("lot") else 0,
                        "option_name":  d.get("option_name") or "",
                        "regime_at_entry": dict(_last_regime),
                        "bias_alignment": _last_bias_alignment,
                        "tilt_state": _last_tilt_state,
                    })
                    session_types.add(d["session_type"].upper())
                    continue

                # ── [TRADE EXIT] ──────────────────────────────────────────
                m = _RE_TRADE_EXIT.search(line)
                if m:
                    d = m.groupdict()
                    log_ts = self._log_ts(line)
                    # exit_reason: prefer the bracket tag (ST_FLIP_2 etc.),
                    # fall back to WIN/LOSS outcome string
                    reason = (d.get("exit_reason") or "").upper() or d["outcome"].upper()
                    exit_rec = {
                        "outcome":     d["outcome"].upper(),
                        "side":        d["side"].upper(),
                        "exit_bar":    int(d["bar"]),
                        "exit_bar_ts": d["bar_ts"],
                        "log_ts":      log_ts,
                        "exit_prem":   float(d["exit_prem"]),
                        "entry_prem":  float(d["entry_prem"]),
                        "pnl_pts":     float(d["pnl_pts"]),
                        "pnl_rs":      float(d["pnl_rs"]),
                        "peak":        float(d["peak"]),
                        "bars_held":   int(d["bars_held"]),
                        "exit_reason": reason,
                    }
                    # Merge with matching open record (FIFO on same side)
                    matched_open = self._pop_open(open_queue, d["side"].upper())
                    if matched_open:
                        trade = {**matched_open, **exit_rec}
                    else:
                        trade = exit_rec
                    trades.append(trade)
                    continue

                # ── [ENTRY BLOCKED] ───────────────────────────────────────
                m = _RE_ENTRY_BLOCKED.search(line)
                if m:
                    _subtype = m.group("subtype").upper()
                    blocked[_subtype] += 1
                    if _subtype == "OSC_EXTREME":
                        osc_blocks += 1
                    continue

                # ── [SLOPE_CONFLICT][3m] (P6 renamed tag) ─────────────────
                if _RE_SLOPE_CONFLICT_3M.search(line):
                    blocked["ST_SLOPE_CONFLICT"] += 1
                    continue

                # ── [ENTRY OK] ────────────────────────────────────────────
                m = _RE_ENTRY_OK.search(line)
                if m:
                    entry_ok_count += 1
                    # Phase 6: [BAR_CLOSE_ALIGNMENT] may appear inside [ENTRY OK] line
                    if _RE_BAR_CLOSE_ALIGNMENT.search(line):
                        bar_close_alignment_count += 1
                        tags["BAR_CLOSE_ALIGNMENT"] = tags.get("BAR_CLOSE_ALIGNMENT", 0) + 1
                    continue

                # ── [SIGNAL FIRED] ────────────────────────────────────────
                m = _RE_SIGNAL_FIRED.search(line)
                if m:
                    signals_fired += 1
                    continue
                if _RE_SIGNAL_BLOCKED.search(line):
                    signals_blocked += 1
                    continue
                if _RE_SIGNAL_OVERRIDE.search(line):
                    tags["SIGNAL_OVERRIDE"] = tags.get("SIGNAL_OVERRIDE", 0) + 1
                    continue
                if _RE_SIGNAL_OVERRIDE_TREND.search(line):
                    tags["SIGNAL_OVERRIDE_TREND"] = tags.get("SIGNAL_OVERRIDE_TREND", 0) + 1
                    continue
                if _RE_TREND_DAY_DETECTED.search(line):
                    tags["TREND_DAY_DETECTED"] = tags.get("TREND_DAY_DETECTED", 0) + 1
                    continue
                if _RE_TREND_REENTRY.search(line):
                    tags["TREND_REENTRY"] = tags.get("TREND_REENTRY", 0) + 1
                    continue
                if _RE_PIVOT_ACCEPTED.search(line):
                    tags["PIVOT_ACCEPTED"] = tags.get("PIVOT_ACCEPTED", 0) + 1
                    continue
                if _RE_PIVOT_REJECTED.search(line):
                    tags["PIVOT_REJECTED"] = tags.get("PIVOT_REJECTED", 0) + 1
                    continue
                if _RE_REVERSAL_ENTRY.search(line):
                    tags["REVERSAL_ENTRY"] = tags.get("REVERSAL_ENTRY", 0) + 1
                    continue
                if _RE_LS_HIGH.search(line):
                    tags["LIQ_SWEEP_HIGH"] = tags.get("LIQ_SWEEP_HIGH", 0) + 1
                    continue
                if _RE_LS_LOW.search(line):
                    tags["LIQ_SWEEP_LOW"] = tags.get("LIQ_SWEEP_LOW", 0) + 1
                    continue

                # ── [OPEN_POSITION] (P5-A) — extract bias tag before generic check ──
                m = _RE_OPEN_POSITION.search(line)
                if m:
                    open_bias_tag = m.group("open_bias_tag").upper()
                    tags["OPEN_POSITION"] += 1
                    continue

                # ── [OPEN_ABOVE/BELOW_CLOSE / OPEN_CLOSE_EQUAL] (P5-B) ────
                m = _RE_OPEN_VS_CLOSE.search(line)
                if m:
                    vs_close_tag = m.group("vs_close_tag").upper()
                    tags[vs_close_tag] += 1
                    continue

                # ── [GAP_UP / GAP_DOWN / NO_GAP] (P5-C) ──────────────────
                m = _RE_GAP.search(line)
                if m:
                    gap_tag = m.group("gap_tag").upper()
                    tags[gap_tag] += 1
                    continue

                # ── [BALANCE_OPEN / OUTSIDE_BALANCE] (P5-D) ──────────────
                m = _RE_BALANCE_OPEN.search(line)
                if m:
                    balance_tag = m.group("balance_tag").upper()
                    tags[balance_tag] += 1
                    continue

                # ── [DAY_TYPE] / [DAY TYPE] (DTC locked) ─────────────────
                m = _RE_DAY_TYPE_OPENING.search(line)
                if m:
                    _gap = m.group("gap_tag").upper()
                    gap_tag = _gap if _gap in {"GAP_UP", "GAP_DOWN"} else "NO_GAP"
                    if day_type_tag in {"NEUTRAL_DAY", "UNKNOWN"}:
                        day_type_tag = "GAP_DAY" if _gap in {"GAP_UP", "GAP_DOWN"} else "NEUTRAL_DAY"
                    tags["DAY_TYPE"] += 1
                    continue
                m = _RE_DAY_TYPE.search(line)
                if m:
                    day_type_tag = m.group("day_type_tag").upper()
                    _cw = m.group("cpr_width_tag")
                    if _cw:
                        cpr_width_tag = _cw.upper()
                    tags["DAY_TYPE"] += 1
                    continue
                # Replay DTC locked format: [DAY TYPE] TRENDING confidence=HIGH ...
                m = _RE_DAY_TYPE_DTC.search(line)
                if m:
                    _dtc_name = m.group("dtc_name").upper()
                    if _dtc_name != "UNKNOWN":
                        day_type_tag = _dtc_name + "_DAY"
                    tags["DAY_TYPE"] += 1
                    continue

                # ── [REVERSAL_SIGNAL] ─────────────────────────────────────
                m = _RE_REVERSAL_SIGNAL.search(line)
                if m:
                    reversal_signal_count += 1
                    tags["REVERSAL_SIGNAL"] += 1
                    continue

                # ── [REVERSAL_OVERRIDE] ───────────────────────────────────
                m = _RE_REVERSAL_OVERRIDE.search(line)
                if m:
                    reversal_count += 1
                    tags["REVERSAL_OVERRIDE"] += 1
                    blocked["REVERSAL_OVERRIDE"] = blocked.get("REVERSAL_OVERRIDE", 0)  # ensure key exists
                    continue

                # ── [ENTRY ALLOWED][ST_SLOPE_OVERRIDE] ───────────────────
                m = _RE_ST_SLOPE_OVERRIDE.search(line)
                if m:
                    slope_override_count += 1
                    tags["ST_SLOPE_OVERRIDE"] += 1
                    continue

                # ── [ENTRY ALLOWED][ST_BIAS_OK] ... osc_context=ZoneX ─────
                m = _RE_ENTRY_ALLOWED_ZONE.search(line)
                if m:
                    zone = m.group("zone")
                    zone_entry_counts[zone] = zone_entry_counts.get(zone, 0) + 1
                    continue

                # ── [OSC_OVERRIDE][TREND_CONFIRMED] ──────────────────────
                m = _RE_OSC_TREND_OVERRIDE.search(line)
                if m:
                    osc_overrides += 1
                    tags["OSC_OVERRIDE"] = tags.get("OSC_OVERRIDE", 0) + 1
                    continue

                # ── [OSC_RELIEF][S4/R4_BREAK] ────────────────────────────
                m = _RE_OSC_RELIEF.search(line)
                if m:
                    osc_relief_count += 1
                    tags["OSC_RELIEF"] = tags.get("OSC_RELIEF", 0) + 1
                    continue

                # ── [TREND_LOSS] ──────────────────────────────────────────
                if "[TREND_LOSS]" in line:
                    trend_loss_count += 1
                    continue

                # ── [CONTRACT_ROLL] ───────────────────────────────────────
                m = _RE_CONTRACT_ROLL.search(line)
                if m:
                    expiry_roll_count += 1
                    tags["CONTRACT_ROLL"] = tags.get("CONTRACT_ROLL", 0) + 1
                    continue

                # ── [CONTRACT_METADATA][LOT_MISMATCH] ─────────────────────
                m = _RE_LOT_MISMATCH.search(line)
                if m:
                    lot_size_mismatch_count += 1
                    tags["LOT_MISMATCH"] = tags.get("LOT_MISMATCH", 0) + 1
                    continue

                # ── [CONTRACT_FILTER] ... SKIPPED ─────────────────────────
                m = _RE_CONTRACT_FILTER_SKIP.search(line)
                if m:
                    intrinsic_filter_count += 1
                    tags["INTRINSIC_FILTER"] = tags.get("INTRINSIC_FILTER", 0) + 1
                    continue

                # ── [EXPIRY_ROLL][SCORE_BONUS] ────────────────────────────
                m = _RE_EXPIRY_ROLL_BONUS.search(line)
                if m:
                    tags["EXPIRY_ROLL_BONUS"] = tags.get("EXPIRY_ROLL_BONUS", 0) + 1
                    continue

                # ── [LOT_SIZE] ────────────────────────────────────────────
                m = _RE_LOT_SIZE.search(line)
                if m:
                    tags["LOT_SIZE"] = tags.get("LOT_SIZE", 0) + 1
                    continue

                # ── [CONFIG] DEFAULT_LOT_SIZE ─────────────────────────────
                if "[CONFIG] DEFAULT_LOT_SIZE" in line:
                    tags["CONFIG_LOT_SIZE"] = tags.get("CONFIG_LOT_SIZE", 0) + 1
                    continue

                # ── [VIX_CONTEXT] ─────────────────────────────────────────
                m = _RE_VIX_CONTEXT.search(line)
                if m:
                    vix_tier_count += 1
                    tags["VIX_CONTEXT"] = tags.get("VIX_CONTEXT", 0) + 1
                    continue

                # ── [GREEKS] ──────────────────────────────────────────────
                m = _RE_GREEKS.search(line)
                if m:
                    greeks_usage_count += 1
                    tags["GREEKS"] = tags.get("GREEKS", 0) + 1
                    continue

                # ── [VOL_CONTEXT][SCORE_ADJUST] ───────────────────────────
                m = _RE_VOL_CONTEXT_ADJUST.search(line)
                if m:
                    tags["VOL_CONTEXT_ADJUST"] = tags.get("VOL_CONTEXT_ADJUST", 0) + 1
                    _ta = m.group("theta_adj")
                    if _ta is not None and int(_ta) < 0:
                        theta_penalty_count += 1
                    continue

                # ── [POSITION_SIZE] (vol-adjusted) ────────────────────────
                m = _RE_POSITION_SIZE_VOL.search(line)
                if m:
                    tags["POSITION_SIZE"] = tags.get("POSITION_SIZE", 0) + 1
                    _vh = m.group("vega_high")
                    if _vh is not None and _vh.lower() == "true":
                        vega_penalty_count += 1
                    continue

                # ── [VOL_CONTEXT][ALIGN] ──────────────────────────────────
                m = _RE_VOL_CONTEXT_ALIGN.search(line)
                if m:
                    vol_context_align_count += 1
                    continue

                # ── [GREEKS_ALIGN] ────────────────────────────────────────
                m = _RE_GREEKS_ALIGN.search(line)
                if m:
                    greeks_align_count += 1
                    continue

                # ── [SCORE_MATRIX] ────────────────────────────────────────
                m = _RE_SCORE_MATRIX.search(line)
                if m:
                    score_matrix_usage_count += 1
                    continue

                # ── [ENTRY BLOCKED][OSC_EXTREME] ──────────────────────────
                # Tracked via blocked_counts; also mirror into osc_blocks
                if "[ENTRY BLOCKED][OSC_EXTREME]" in line:
                    osc_blocks += 1
                    # Fall through to _RE_ENTRY_BLOCKED which handles it too

                # ── [REGIME_CONTEXT] per-bar regime snapshot (Phase 5) ─────
                m = _RE_REGIME_CONTEXT.search(line)
                if m:
                    regime_context_count += 1
                    _ar = m.group("atr_regime")
                    _at = m.group("adx_tier")
                    _dt = m.group("day_type")
                    _cw = m.group("cpr_width")
                    if _ar:
                        _last_regime["atr_regime"] = _ar.upper()
                    if _at:
                        _last_regime["adx_tier"] = _at.upper()
                    if _dt:
                        _last_regime["day_type"] = _dt.upper()
                    if _cw:
                        _last_regime["cpr_width"] = _cw.upper()
                    tags["REGIME_CONTEXT"] = tags.get("REGIME_CONTEXT", 0) + 1
                    continue

                # ── [EXIT AUDIT][REGIME_ADAPTIVE] per-trade regime (Phase 5)
                m = _RE_EXIT_AUDIT_REGIME.search(line)
                if m:
                    regime_adaptive_count += 1
                    _dt = m.group("day_type")
                    _at = m.group("adx_tier")
                    _gt = m.group("gap_tag")
                    if _dt:
                        _last_regime["day_type"] = _dt.upper()
                    if _at:
                        _last_regime["adx_tier"] = _at.upper()
                    tags["REGIME_ADAPTIVE"] = tags.get("REGIME_ADAPTIVE", 0) + 1
                    continue

                # ── Phase 6: Bias alignment & microstructure ────────────
                m = _RE_BIAS_ALIGNMENT.search(line)
                if m:
                    bias_alignment_count += 1
                    _last_bias_alignment = m.group("status").upper()
                    tags["BIAS_ALIGNMENT"] = tags.get("BIAS_ALIGNMENT", 0) + 1
                    continue

                m = _RE_BAR_CLOSE_ALIGNMENT.search(line)
                if m:
                    bar_close_alignment_count += 1
                    tags["BAR_CLOSE_ALIGNMENT"] = tags.get("BAR_CLOSE_ALIGNMENT", 0) + 1
                    continue

                if _RE_SLOPE_OVERRIDE_TIME.search(line):
                    slope_override_time_count += 1
                    tags["SLOPE_OVERRIDE_TIME"] = tags.get("SLOPE_OVERRIDE_TIME", 0) + 1
                    continue

                if _RE_CONFLICT_BLOCKED.search(line):
                    conflict_blocked_count += 1
                    tags["CONFLICT_BLOCKED"] = tags.get("CONFLICT_BLOCKED", 0) + 1
                    continue

                if _RE_PULSE_EXHAUSTION.search(line):
                    pulse_exhaustion_count += 1
                    tags["PULSE_EXHAUSTION"] = tags.get("PULSE_EXHAUSTION", 0) + 1
                    continue

                if _RE_ZONE_ABSORPTION.search(line):
                    zone_absorption_count += 1
                    tags["ZONE_ABSORPTION"] = tags.get("ZONE_ABSORPTION", 0) + 1
                    continue

                if _RE_SPREAD_NOISE.search(line):
                    spread_noise_count += 1
                    tags["SPREAD_NOISE"] = tags.get("SPREAD_NOISE", 0) + 1
                    continue

                # ── Phase 6.1: Tilt-based governance ─────────────────────
                m = _RE_TILT_STATE.search(line)
                if m:
                    tilt_state_count += 1
                    _last_tilt_state = m.group("tilt").upper()
                    tags["TILT_STATE"] = tags.get("TILT_STATE", 0) + 1
                    continue

                if _RE_GOVERNANCE_EASY.search(line):
                    governance_easy_count += 1
                    tags["GOVERNANCE_EASY"] = tags.get("GOVERNANCE_EASY", 0) + 1
                    # Phase 6.1.2: sub-count for bias misalignment bypass
                    if _RE_TILT_BIAS_OVERRIDE.search(line):
                        tilt_bias_override_count += 1
                        tags["TILT_BIAS_OVERRIDE"] = tags.get("TILT_BIAS_OVERRIDE", 0) + 1
                    continue

                if _RE_GOVERNANCE_STRICT.search(line):
                    governance_strict_count += 1
                    tags["GOVERNANCE_STRICT"] = tags.get("GOVERNANCE_STRICT", 0) + 1
                    continue

                # ── Phase 6.3: Regime-aware fixes ──────────────────────
                if _RE_OSC_TREND_OVERRIDE.search(line):
                    osc_trend_override_count += 1
                    tags["OSC_TREND_OVERRIDE"] = tags.get("OSC_TREND_OVERRIDE", 0) + 1
                    continue
                if _RE_TREND_ALIGN_OVERRIDE.search(line):
                    trend_align_override_count += 1
                    tags["TREND_ALIGN_OVERRIDE"] = tags.get("TREND_ALIGN_OVERRIDE", 0) + 1
                    continue
                if _RE_DAY_BIAS_PENALTY.search(line):
                    day_bias_penalty_count += 1
                    tags["DAY_BIAS_PENALTY"] = tags.get("DAY_BIAS_PENALTY", 0) + 1
                    continue
                if _RE_MOMENTUM_ENTRY.search(line):
                    momentum_entry_count += 1
                    tags["MOMENTUM_ENTRY"] = tags.get("MOMENTUM_ENTRY", 0) + 1
                    continue
                if _RE_SIGNAL_SKIP.search(line):
                    signal_skip_count += 1
                    tags["SIGNAL_SKIP"] = tags.get("SIGNAL_SKIP", 0) + 1
                    continue
                if _RE_COOLDOWN_REDUCED.search(line):
                    cooldown_reduced_count += 1
                    tags["COOLDOWN_REDUCED"] = tags.get("COOLDOWN_REDUCED", 0) + 1
                    continue

                # ── Phase 6.4: Reversal Detection + Opening ST Relaxation ─
                if _RE_REVERSAL_EMA_STRETCH.search(line):
                    reversal_ema_stretch_count += 1
                    tags["REVERSAL_EMA_STRETCH"] = tags.get("REVERSAL_EMA_STRETCH", 0) + 1
                    continue
                if _RE_REVERSAL_PIVOT_CONFIRM.search(line):
                    reversal_pivot_confirm_count += 1
                    tags["REVERSAL_PIVOT_CONFIRM"] = tags.get("REVERSAL_PIVOT_CONFIRM", 0) + 1
                    continue
                if _RE_REVERSAL_OSC_CONFIRM.search(line):
                    reversal_osc_confirm_count += 1
                    tags["REVERSAL_OSC_CONFIRM"] = tags.get("REVERSAL_OSC_CONFIRM", 0) + 1
                    continue
                if _RE_REVERSAL_BIAS_FLIP.search(line):
                    reversal_bias_flip_count += 1
                    tags["REVERSAL_BIAS_FLIP"] = tags.get("REVERSAL_BIAS_FLIP", 0) + 1
                    continue
                if _RE_REVERSAL_COOLDOWN_RELAX.search(line):
                    reversal_cooldown_relax_count += 1
                    tags["REVERSAL_COOLDOWN_RELAX"] = tags.get("REVERSAL_COOLDOWN_RELAX", 0) + 1
                    continue
                if _RE_REVERSAL_PERSIST.search(line):
                    reversal_persist_count += 1
                    tags["REVERSAL_PERSIST"] = tags.get("REVERSAL_PERSIST", 0) + 1
                    continue
                if _RE_ST_OPENING_RELAX.search(line):
                    st_opening_relax_count += 1
                    tags["ST_OPENING_RELAX"] = tags.get("ST_OPENING_RELAX", 0) + 1
                    continue
                if _RE_REVERSAL_CAPTURE.search(line):
                    reversal_capture_count += 1
                    tags["REVERSAL_CAPTURE"] = tags.get("REVERSAL_CAPTURE", 0) + 1
                    continue

                # ── Phase 6.2: Trend continuation ───────────────────────
                m = _RE_TREND_CONT_ACTIVATED.search(line)
                if m:
                    trend_cont_activations += 1
                    trend_cont_side = m.group("side")
                    tags["TREND_CONTINUATION"] = tags.get("TREND_CONTINUATION", 0) + 1
                    continue
                m = _RE_TREND_CONT_ENTRY.search(line)
                if m:
                    trend_cont_entries += 1
                    tags["TREND_CONTINUATION"] = tags.get("TREND_CONTINUATION", 0) + 1
                    continue
                if _RE_TREND_CONT_DEACTIVATED.search(line):
                    trend_cont_deactivations += 1
                    continue

                # ── P1–P5 tags ────────────────────────────────────────────
                m = _RE_TAG_ANY.search(line)
                if m:
                    tags[m.group(1)] += 1
                    continue

                # ── [EXIT AUDIT] (legacy) ─────────────────────────────────
                m = _RE_EXIT_AUDIT.search(line)
                if m:
                    d = m.groupdict()
                    audit_records.append({
                        "side":        (d.get("option_type") or "").upper(),
                        "exit_reason": d.get("reason") or "",
                        "bars_held":   int(d["bars_held"]) if d.get("bars_held") not in (None, "") else -1,
                        "pnl_pts":     float(d["premium_move"]) if d.get("premium_move") not in (None, "") else 0.0,
                        "pnl_rs":      0.0,
                    })

        # Select best trade list: struct > rich_exit > V1 pairs > EXIT AUDIT fallback
        # struct:      new PositionManager format (option_name-keyed open+exit pair)
        # rich_exit:   [EXIT][PAPER/LIVE ...] self-contained records (entry+exit+pnl all in one line)
        # trades:      legacy [TRADE OPEN][MODE]/[TRADE EXIT] pairs
        # audit_records: EXIT AUDIT only (no entry data, pnl only when premium_move present)
        if trades_struct:
            effective_trades = trades_struct
        elif rich_exit_trades:
            effective_trades = rich_exit_trades
        elif trades:
            effective_trades = trades
        elif audit_records:
            effective_trades = audit_records
        else:
            effective_trades = []

        # P5-F: annotate every trade with open_bias_aligned (extended P5-E logic)
        for trade in effective_trades:
            _existing = str(trade.get("open_bias_aligned", "")).upper()
            if _existing in {"ALIGNED", "MISALIGNED", "NEUTRAL"}:
                continue
            side = trade.get("side", "")
            call_aligned = side == "CALL" and (
                open_bias_tag == "OPEN_LOW" or gap_tag == "GAP_UP"
            )
            put_aligned = side == "PUT" and (
                open_bias_tag == "OPEN_HIGH" or gap_tag == "GAP_DOWN"
            )
            if open_bias_tag == "NONE" and gap_tag == "NO_GAP":
                trade["open_bias_aligned"] = "NEUTRAL"
            elif call_aligned or put_aligned:
                trade["open_bias_aligned"] = "ALIGNED"
            else:
                trade["open_bias_aligned"] = "MISALIGNED"

        # Phase 5: build regime trade breakdown from trade-level regime_at_entry
        for trade in effective_trades:
            _regime = trade.get("regime_at_entry", {})
            _pnl = trade.get("pnl_pts", 0.0)
            for dim in ("day_type", "adx_tier", "atr_regime", "cpr_width"):
                _label = _regime.get(dim, "UNKNOWN")
                regime_breakdown[dim][_label].append(_pnl)

        return (effective_trades, session_types, blocked, tags, signals_fired, signals_blocked, entry_ok_count,
                open_bias_tag, vs_close_tag, gap_tag, balance_tag,
                day_type_tag, cpr_width_tag, reversal_count, slope_override_count,
                osc_blocks, osc_overrides, osc_relief_count, trend_loss_count,
                expiry_roll_count, lot_size_mismatch_count, intrinsic_filter_count,
                vix_tier_count, greeks_usage_count, theta_penalty_count, vega_penalty_count,
                vol_context_align_count, greeks_align_count, score_matrix_usage_count,
                reversal_signal_count, dict(zone_entry_counts),
                regime_context_count, regime_adaptive_count,
                {dim: dict(labels) for dim, labels in regime_breakdown.items()},
                # Phase 6
                bias_alignment_count, bar_close_alignment_count,
                slope_override_time_count, conflict_blocked_count,
                pulse_exhaustion_count, zone_absorption_count, spread_noise_count,
                # Phase 6.1
                tilt_state_count, governance_easy_count, governance_strict_count,
                tilt_bias_override_count,
                # Phase 6.2
                trend_cont_activations, trend_cont_entries,
                trend_cont_deactivations, trend_cont_side,
                # Phase 6.3
                osc_trend_override_count, trend_align_override_count,
                day_bias_penalty_count, momentum_entry_count,
                signal_skip_count, cooldown_reduced_count,
                # Phase 6.4
                reversal_ema_stretch_count, reversal_pivot_confirm_count,
                reversal_osc_confirm_count, reversal_bias_flip_count,
                reversal_cooldown_relax_count, reversal_persist_count,
                st_opening_relax_count, reversal_capture_count)

    @staticmethod
    def _log_ts(line: str) -> str:
        m = _RE_LOG_TS.match(line)
        return m.group("log_ts") if m else ""

    @staticmethod
    def _pop_open(queue: List[dict], side: str) -> Optional[dict]:
        """Pop the first open record matching side (FIFO)."""
        for i, rec in enumerate(queue):
            if rec["side"] == side:
                return queue.pop(i)
        # If no same-side match, pop the oldest open regardless
        return queue.pop(0) if queue else None


# ── Convenience function ──────────────────────────────────────────────────────

def parse_session(log_path: str | Path) -> SessionSummary:
    """Parse a log file and return a SessionSummary.

    Equivalent to ``LogParser(log_path).parse()``.
    """
    return LogParser(log_path).parse()


def parse_multiple(log_paths: List[str | Path]) -> List[SessionSummary]:
    """Parse multiple log files and return a list of SessionSummary objects."""
    return [parse_session(p) for p in log_paths]
