# ===== trade_classes.py =====
"""
P2-D: Trade class definitions.

ScalpTrade — short-duration burst entry governed by SCALP_PT/SL thresholds.
             Always sets scalp_mode=True; HFT override still runs first.
TrendTrade — Supertrend / compression-breakout entry governed by TG/PT logic
             with full HFT protection. Never sets scalp_mode.

These dataclasses are purely descriptive/documentation aids. The actual runtime
state dict in execution.py stores ``trade_class`` as a string constant
(TRADE_CLASS_SCALP / TRADE_CLASS_TREND) and ``scalp_mode`` as a bool.
The classes below provide a typed reference and can be used for validation.
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ScalpTrade:
    """State contract for ExitScalp trades (SCALP class).

    Rules:
      - scalp_mode = True always
      - Exits governed by SCALP_PT_POINTS and SCALP_SL_POINTS thresholds
      - HFT override (OptionExitManager) still runs before scalp check
      - Partial TG booking NOT applicable (too short a hold for ratchet)
      - trade_class = "SCALP"
    """
    side:           str                  # "CALL" | "PUT"
    option_name:    str
    entry_price:    float
    stop:           float
    scalp_pt:       float                # entry + SCALP_PT_POINTS
    scalp_sl:       float                # entry - SCALP_SL_POINTS
    quantity:       int
    position_id:    str
    scalp_mode:     bool = True          # always True for ScalpTrade
    trade_class:    str  = "SCALP"
    partial_tg_booked: bool = False      # unused in scalp; kept for schema compat
    is_open:        bool = True

    def validate(self):
        """Assert invariants that must hold for a ScalpTrade state dict."""
        assert self.scalp_mode is True, "ScalpTrade must have scalp_mode=True"
        assert self.trade_class == "SCALP", "ScalpTrade must have trade_class='SCALP'"
        assert self.scalp_pt > self.entry_price, "scalp_pt must be above entry"
        assert self.scalp_sl < self.entry_price, "scalp_sl must be below entry"


@dataclass
class TrendTrade:
    """State contract for Supertrend / compression-breakout trend trades (TREND class).

    Rules:
      - scalp_mode = False always (ExitScalp thresholds never fire)
      - Exits governed by TG (first target), PT (full target) + HFT protection
      - Partial TG exit: 50% qty exits at TG; SL ratcheted to TG; remaining runs to PT
      - trade_class = "TREND"
    """
    side:              str
    option_name:       str
    entry_price:       float
    stop:              float
    tg:                float             # first target (50% exit)
    pt:                float             # full target (remaining exit)
    quantity:          int
    position_id:       str
    scalp_mode:        bool = False      # always False for TrendTrade
    trade_class:       str  = "TREND"
    partial_tg_booked: bool = False      # flips to True after first TG hit
    is_open:           bool = True
    source:            str  = "TREND"   # e.g. "COMPRESSION_BREAKOUT" for cooldown

    def validate(self):
        """Assert invariants that must hold for a TrendTrade state dict."""
        assert self.scalp_mode is False, "TrendTrade must have scalp_mode=False"
        assert self.trade_class == "TREND", "TrendTrade must have trade_class='TREND'"
        assert self.tg  > self.entry_price, "tg must be above entry for CALL"
        assert self.pt  > self.tg,          "pt must be above tg"
        assert self.stop < self.entry_price, "stop must be below entry"
