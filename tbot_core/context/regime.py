# ============================================================
#  regime_context.py — v1.0  (Unified Regime Context Engine)
# ============================================================
"""
ARCHITECTURE
────────────
RegimeContext is a frozen dataclass computed once per 3-minute bar.
It consolidates all regime signals that were previously fragmented
across st_details dicts, loose state keys, and individual detector
calls.

Frozen guarantee: entry-time context is preserved immutably for
exit decisions — no accidental mutation during the trade's lifetime.

Usage:
    rc = compute_regime_context(
        st_details=st_details,
        candles_3m=candles_3m,
        atr=atr,
        reversal_signal=rev_sig,
        failed_breakout_signal=fb_sig,
        zone_signal=zone_sig,
        pulse_metrics=pulse.get_pulse(),
        compression_state_str=comp_state.market_state,
    )
    state.update(rc.to_state_keys())   # merge into trade state

Integration:
    - Called after _trend_entry_quality_gate() returns
    - Stored in state["entry_regime_context"] at trade open
    - Read by check_exit_condition() for regime-adaptive exits
    - Logged per bar via [REGIME_CONTEXT] tag
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional, Tuple

# ── ANSI colours (mirror execution.py) ──────────────────────────────────────
CYAN = "\033[96m"
RESET = "\033[0m"


# ── ATR regime classification (eliminates duplication in 3+ places) ─────────
def classify_atr_regime(atr: float) -> str:
    """Map ATR value to regime tier string."""
    if atr <= 0 or not math.isfinite(atr):
        return "ATR_UNKNOWN"
    if atr <= 60:
        return "VERY_LOW"
    if atr <= 100:
        return "LOW"
    if atr <= 150:
        return "MODERATE"
    if atr <= 250:
        return "HIGH"
    return "EXTREME"


def classify_adx_tier(adx: float) -> str:
    """Map ADX value to tier string."""
    if not math.isfinite(adx) or adx <= 0:
        return "ADX_DEFAULT"
    if adx > 40:
        return "ADX_STRONG_40"
    if adx < 20:
        return "ADX_WEAK_20"
    return "ADX_DEFAULT"


# ═══════════════════════════════════════════════════════════════════════════════
# RegimeContext frozen dataclass
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class RegimeContext:
    """Immutable snapshot of all regime signals, computed once per 3m bar.

    Consolidates context previously fragmented across st_details dict,
    detector calls, and loose state dict keys.
    """

    # ── ATR regime ────────────────────────────────────────────────────────
    atr_value: float = 0.0
    atr_regime: str = "ATR_UNKNOWN"        # VERY_LOW / LOW / MODERATE / HIGH / EXTREME
    atr_expand_tier: str = "ATR_DEFAULT"   # ATR_DEFAULT / ATR_ELEVATED / ATR_HIGH

    # ── ADX tier ──────────────────────────────────────────────────────────
    adx_value: float = 0.0
    adx_tier: str = "ADX_DEFAULT"          # ADX_WEAK_20 / ADX_DEFAULT / ADX_STRONG_40

    # ── Day classification ────────────────────────────────────────────────
    day_type: str = "UNKNOWN"              # TREND_DAY / RANGE_DAY / GAP_DAY / BALANCE_DAY / NEUTRAL_DAY
    cpr_width: str = "NORMAL"              # NARROW / NORMAL / WIDE
    open_bias: str = "UNKNOWN"             # OPEN_HIGH / OPEN_LOW / NONE
    bias_tag: str = "Neutral"              # Bullish / Bearish / Neutral
    gap_tag: str = "NO_GAP"               # GAP_UP / GAP_DOWN / NO_GAP

    # ── Supertrend alignment ──────────────────────────────────────────────
    st_bias_3m: str = "NEUTRAL"            # BULLISH / BEARISH / NEUTRAL
    st_bias_15m: str = "NEUTRAL"
    st_slope_3m: str = "FLAT"              # UP / DOWN / FLAT
    st_slope_15m: str = "FLAT"
    st_aligned: bool = False

    # ── Oscillator snapshot ───────────────────────────────────────────────
    rsi14: float = 50.0
    cci20: float = 0.0
    osc_context: str = "UNKNOWN"           # ZoneA-Blocker / ZoneB-Reversal / ZoneC-Continuation
    osc_zone: str = "Unknown"              # ZoneA / ZoneB / ZoneC / Unknown
    osc_rsi_range: Tuple[float, float] = (30.0, 70.0)
    osc_cci_range: Tuple[float, float] = (-150.0, 150.0)

    # ── Detector signals ──────────────────────────────────────────────────
    reversal_signal: Optional[dict] = None
    failed_breakout_signal: Optional[dict] = None
    zone_signal: Optional[dict] = None     # from detect_zone_revisit

    # ── Pulse metrics ─────────────────────────────────────────────────────
    pulse_tick_rate: float = 0.0
    pulse_burst_flag: bool = False
    pulse_direction: str = "NEUTRAL"       # UP / DOWN / NEUTRAL

    # ── Compression state ─────────────────────────────────────────────────
    compression_state: str = "NEUTRAL"     # NEUTRAL / ENERGY_BUILDUP / VOLATILITY_EXPANSION

    # ── EMA stretch ───────────────────────────────────────────────────────
    ema_stretch_mult: float = 0.0
    ema_stretch_tagged: bool = False

    # ── Override flags (from quality gate) ────────────────────────────────
    osc_relief_override: bool = False
    osc_trend_override: bool = False
    slope_override_reason: Optional[str] = None
    st_conflict_override: bool = False
    bias_aligned: bool = False

    # ── Timestamp / Bar Reference ─────────────────────────────────────────
    bar_timestamp: Optional[str] = None
    symbol: str = ""

    # ─── Derived properties ───────────────────────────────────────────────

    @property
    def regime_label(self) -> str:
        """Human-readable summary: e.g. 'MODERATE|ADX_DEFAULT|RANGE_DAY|NARROW'"""
        return f"{self.atr_regime}|{self.adx_tier}|{self.day_type}|{self.cpr_width}"

    @property
    def has_reversal(self) -> bool:
        return self.reversal_signal is not None

    @property
    def has_failed_breakout(self) -> bool:
        return self.failed_breakout_signal is not None

    @property
    def has_zone_signal(self) -> bool:
        return self.zone_signal is not None

    @property
    def pulse_active(self) -> bool:
        return self.pulse_burst_flag and self.pulse_direction != "NEUTRAL"

    # ─── Export methods ───────────────────────────────────────────────────

    def to_state_keys(self) -> dict:
        """Export fields for trade state dict at entry time.

        Merges into state via: state.update(rc.to_state_keys())
        Backward-compatible: populates existing keys + adds new ones.
        """
        return {
            # Existing state keys (backward-compat)
            "regime_context":       self.atr_regime,
            "atr_value":            self.atr_value,
            "osc_context":          self.osc_context,
            "day_type":             self.day_type,
            "open_bias":            self.open_bias,
            "failed_breakout":      self.failed_breakout_signal is not None,
            "ema_stretch":          self.ema_stretch_tagged,
            "ema_stretch_mult":     self.ema_stretch_mult,
            # Oscillator thresholds from regime
            "osc_rsi_call":         self.osc_rsi_range[1],
            "osc_rsi_put":          self.osc_rsi_range[0],
            "osc_cci_call":         self.osc_cci_range[1],
            "osc_cci_put":          self.osc_cci_range[0],
            # New regime-specific keys
            "entry_regime_context": self,  # full frozen snapshot
            "adx_tier":             self.adx_tier,
            "cpr_width_at_entry":   self.cpr_width,
            "gap_tag":              self.gap_tag,
            "compression_state_at_entry": self.compression_state,
        }

    def to_log_tag(self) -> str:
        """Generate [REGIME_CONTEXT] log string for per-bar logging."""
        parts = [
            f"ATR={self.atr_value:.1f}({self.atr_regime})",
            f"ADX={self.adx_value:.1f}({self.adx_tier})",
            f"day={self.day_type}",
            f"cpr={self.cpr_width}",
            f"ST={self.st_bias_3m}/{self.st_bias_15m}",
            f"slope={self.st_slope_3m}",
            f"RSI={self.rsi14:.1f}",
            f"CCI={self.cci20:.1f}",
            f"osc={self.osc_context}",
        ]
        if self.has_reversal:
            parts.append(f"reversal={self.reversal_signal.get('side', '?')}")
        if self.has_failed_breakout:
            parts.append(f"fb={self.failed_breakout_signal.get('side', '?')}")
        if self.has_zone_signal:
            zt = self.zone_signal.get("zone_type", "?")
            za = self.zone_signal.get("action", "?")
            parts.append(f"zone={zt}_{za}")
        if self.pulse_active:
            parts.append(f"pulse={self.pulse_tick_rate:.1f}t/s_{self.pulse_direction}")
        if self.compression_state != "NEUTRAL":
            parts.append(f"comp={self.compression_state}")
        return " ".join(parts)

    def to_dict(self) -> dict:
        """Serializable dict representation (for JSON/pickle compat)."""
        return {
            "atr_value": self.atr_value,
            "atr_regime": self.atr_regime,
            "atr_expand_tier": self.atr_expand_tier,
            "adx_value": self.adx_value,
            "adx_tier": self.adx_tier,
            "day_type": self.day_type,
            "cpr_width": self.cpr_width,
            "open_bias": self.open_bias,
            "bias_tag": self.bias_tag,
            "gap_tag": self.gap_tag,
            "st_bias_3m": self.st_bias_3m,
            "st_bias_15m": self.st_bias_15m,
            "st_slope_3m": self.st_slope_3m,
            "st_slope_15m": self.st_slope_15m,
            "st_aligned": self.st_aligned,
            "rsi14": self.rsi14,
            "cci20": self.cci20,
            "osc_context": self.osc_context,
            "osc_zone": self.osc_zone,
            "osc_rsi_range": list(self.osc_rsi_range),
            "osc_cci_range": list(self.osc_cci_range),
            "pulse_tick_rate": self.pulse_tick_rate,
            "pulse_burst_flag": self.pulse_burst_flag,
            "pulse_direction": self.pulse_direction,
            "compression_state": self.compression_state,
            "ema_stretch_mult": self.ema_stretch_mult,
            "ema_stretch_tagged": self.ema_stretch_tagged,
            "osc_relief_override": self.osc_relief_override,
            "bar_timestamp": self.bar_timestamp,
            "symbol": self.symbol,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Builder: compute_regime_context()
# ═══════════════════════════════════════════════════════════════════════════════

def compute_regime_context(
    *,
    st_details: dict,
    atr: float = 0.0,
    reversal_signal: Optional[dict] = None,
    failed_breakout_signal: Optional[dict] = None,
    zone_signal: Optional[dict] = None,
    pulse_tick_rate: float = 0.0,
    pulse_burst_flag: bool = False,
    pulse_direction: str = "NEUTRAL",
    compression_state_str: str = "NEUTRAL",
    bar_timestamp: Optional[str] = None,
    symbol: str = "",
) -> RegimeContext:
    """Build RegimeContext from quality gate output + detector signals.

    Called AFTER _trend_entry_quality_gate() returns. The gate's st_details
    dict provides all indicator snapshots and computed context. This function
    maps those into the frozen RegimeContext.

    Args:
        st_details: Output dict from _trend_entry_quality_gate()
        atr: Current ATR(14) value
        reversal_signal: From detect_reversal() or None
        failed_breakout_signal: From detect_failed_breakout() or None
        zone_signal: From detect_zone_revisit() or None
        pulse_tick_rate: Tick rate from PulseModule
        pulse_burst_flag: Burst detected flag
        pulse_direction: UP/DOWN/NEUTRAL drift
        compression_state_str: CompressionState.market_state string
        bar_timestamp: ISO timestamp for this bar
        symbol: Trading symbol
    """
    if st_details is None:
        st_details = {}

    # Extract from st_details (populated by quality gate)
    _adx = float(st_details.get("adx14", 0.0) or 0.0)
    _rsi = float(st_details.get("rsi14", 50.0) or 50.0)
    _cci = float(st_details.get("cci20", 0.0) or 0.0)

    # ATR regime
    _atr = float(atr) if atr and math.isfinite(float(atr)) else 0.0
    _atr_regime = classify_atr_regime(_atr)

    # ADX tier
    _adx_tier = classify_adx_tier(_adx)

    # Supertrend alignment
    _st3m = str(st_details.get("ST3m_bias", "NEUTRAL"))
    _st15m = str(st_details.get("ST15m_bias", "NEUTRAL"))
    _aligned = (_st3m == _st15m and _st3m in {"BULLISH", "BEARISH"})

    # Oscillator ranges (effective thresholds after expansion)
    _rsi_range = st_details.get("eff_rsi_range", [30.0, 70.0])
    _cci_range = st_details.get("eff_cci_range", [-150.0, 150.0])
    if isinstance(_rsi_range, list) and len(_rsi_range) == 2:
        _rsi_range = tuple(_rsi_range)
    else:
        _rsi_range = (30.0, 70.0)
    if isinstance(_cci_range, list) and len(_cci_range) == 2:
        _cci_range = tuple(_cci_range)
    else:
        _cci_range = (-150.0, 150.0)

    # EMA stretch
    _ema_mult = float(st_details.get("ema_stretch_mult", 0.0) or 0.0)

    rc = RegimeContext(
        # ATR
        atr_value=_atr,
        atr_regime=_atr_regime,
        atr_expand_tier=str(st_details.get("atr_expand_tier", "ATR_DEFAULT")),
        # ADX
        adx_value=_adx,
        adx_tier=_adx_tier,
        # Day classification
        day_type=str(st_details.get("day_type_tag", "UNKNOWN")),
        cpr_width=str(st_details.get("cpr_width", "NORMAL")),
        open_bias=str(st_details.get("open_bias", "UNKNOWN")),
        bias_tag=str(st_details.get("bias", "Neutral")),
        gap_tag=str(st_details.get("gap_tag", "NO_GAP")),
        # Supertrend
        st_bias_3m=_st3m,
        st_bias_15m=_st15m,
        st_slope_3m=str(st_details.get("ST3m_slope", "FLAT")),
        st_slope_15m=str(st_details.get("ST15m_slope", "FLAT")),
        st_aligned=_aligned,
        # Oscillators
        rsi14=_rsi,
        cci20=_cci,
        osc_context=str(st_details.get("osc_context", "UNKNOWN")),
        osc_zone=str(st_details.get("osc_zone", "Unknown")),
        osc_rsi_range=_rsi_range,
        osc_cci_range=_cci_range,
        # Detectors
        reversal_signal=reversal_signal,
        failed_breakout_signal=failed_breakout_signal,
        zone_signal=zone_signal,
        # Pulse
        pulse_tick_rate=pulse_tick_rate,
        pulse_burst_flag=pulse_burst_flag,
        pulse_direction=pulse_direction,
        # Compression
        compression_state=compression_state_str,
        # EMA stretch
        ema_stretch_mult=_ema_mult,
        ema_stretch_tagged=bool(st_details.get("ema_stretch_tagged", False)),
        # Override flags
        osc_relief_override=bool(st_details.get("osc_relief_override", False)),
        osc_trend_override=bool(st_details.get("osc_trend_override", False)),
        slope_override_reason=st_details.get("slope_override_reason"),
        st_conflict_override=bool(st_details.get("st_conflict_override", False)),
        bias_aligned=bool(st_details.get("bias_aligned", False)),
        # Bar reference
        bar_timestamp=str(bar_timestamp) if bar_timestamp else None,
        symbol=symbol,
    )

    return rc


def log_regime_context(rc: RegimeContext) -> None:
    """Emit [REGIME_CONTEXT] log line for dashboard attribution."""
    logging.info(
        f"{CYAN}[REGIME_CONTEXT] {rc.bar_timestamp} {rc.symbol} "
        f"{rc.to_log_tag()}{RESET}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Scalp-specific builder (simplified — no quality gate)
# ═══════════════════════════════════════════════════════════════════════════════

def compute_scalp_regime_context(
    *,
    atr: float = 0.0,
    adx: float = 0.0,
    pulse_tick_rate: float = 0.0,
    pulse_burst_flag: bool = False,
    pulse_direction: str = "NEUTRAL",
    compression_state_str: str = "NEUTRAL",
    bar_timestamp: Optional[str] = None,
    symbol: str = "",
) -> RegimeContext:
    """Build a lightweight RegimeContext for scalp entries.

    Scalp entries bypass the quality gate but still need regime context
    for attribution and exit adaptation.
    """
    return RegimeContext(
        atr_value=float(atr) if atr and math.isfinite(float(atr)) else 0.0,
        atr_regime=classify_atr_regime(float(atr) if atr else 0.0),
        adx_value=float(adx) if adx and math.isfinite(float(adx)) else 0.0,
        adx_tier=classify_adx_tier(float(adx) if adx else 0.0),
        pulse_tick_rate=pulse_tick_rate,
        pulse_burst_flag=pulse_burst_flag,
        pulse_direction=pulse_direction,
        compression_state=compression_state_str,
        bar_timestamp=str(bar_timestamp) if bar_timestamp else None,
        symbol=symbol,
    )


__all__ = [
    "RegimeContext",
    "compute_regime_context",
    "compute_scalp_regime_context",
    "log_regime_context",
    "classify_atr_regime",
    "classify_adx_tier",
]
