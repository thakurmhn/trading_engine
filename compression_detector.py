# ===== compression_detector.py =====
"""
15-minute volatility compression detection module.

Detects contraction → expansion cycles on 15m candles using pure price + ATR
logic (no oscillators, moving averages, or volume).

State Machine:
  NEUTRAL → ENERGY_BUILDUP → VOLATILITY_EXPANSION → NEUTRAL

Compression conditions (all must pass on last 3 15m bars):
  1. Range contraction:     avg_range < 0.45 * ATR_15m
  2. Candle overlap/balance: cluster_range < 1.2 * max_single_range
  3. Directional neutrality: abs(close_last - open_first) < 0.5 * ATR_15m

Expansion conditions (on the breakout candle):
  1. candle_range > 1.3 * ATR_15m
  2. close outside compression_high or compression_low
"""

import logging
import numpy as np
import pandas as pd
from typing import Optional

RESET  = "\033[0m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"


# ─────────────────────────────────────────────────────────────────────────────
# COMPRESSION DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def detect_compression(df_15m: pd.DataFrame) -> Optional[dict]:
    """
    Detect volatility compression on the last 3 15m bars.

    Returns a zone dict if all 3 conditions pass, else None.

    Output:
        {
            "compression_active": True,
            "compression_high": float,
            "compression_low": float,
            "compression_start_index": int,
            "compression_strength": float,   # ATR / avg_range (higher = tighter)
            "atr_15m": float,
            "avg_range": float,
        }
    """
    if df_15m is None or len(df_15m) < 3:
        return None

    required = {"open", "high", "low", "close", "atr14"}
    if not required.issubset(df_15m.columns):
        return None

    atr_15m = float(df_15m["atr14"].iloc[-1])
    if not np.isfinite(atr_15m) or atr_15m <= 0:
        return None

    last_3 = df_15m.iloc[-3:]

    ranges = (last_3["high"] - last_3["low"]).values.astype(float)
    avg_range = float(np.mean(ranges))

    # Condition 1: range contraction
    if avg_range >= 0.45 * atr_15m:
        return None

    # Condition 2: candle overlap / balance
    cluster_range = float(last_3["high"].max() - last_3["low"].min())
    max_single = float(np.max(ranges))
    if max_single <= 0 or cluster_range >= 1.2 * max_single:
        return None

    # Condition 3: directional neutrality
    net_move = abs(float(df_15m["close"].iloc[-1]) - float(df_15m["open"].iloc[-3]))
    if net_move >= 0.5 * atr_15m:
        return None

    compression_high = float(last_3["high"].max())
    compression_low  = float(last_3["low"].min())
    compression_strength = round(atr_15m / avg_range, 2) if avg_range > 0 else 0.0
    start_index = len(df_15m) - 3

    return {
        "compression_active":      True,
        "compression_high":        compression_high,
        "compression_low":         compression_low,
        "compression_start_index": start_index,
        "compression_strength":    compression_strength,
        "atr_15m":                 atr_15m,
        "avg_range":               avg_range,
    }


# ─────────────────────────────────────────────────────────────────────────────
# EXPANSION DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def detect_expansion(df_15m: pd.DataFrame, zone: dict) -> Optional[dict]:
    """
    Validate an expansion candle against the locked compression zone.

    Conditions on df_15m.iloc[-1]:
      1. candle_range > 1.3 * zone["atr_15m"]
      2. close outside compression_high (LONG) or compression_low (SHORT)

    Returns expansion dict or None.
    """
    if df_15m is None or df_15m.empty or zone is None:
        return None

    last = df_15m.iloc[-1]
    atr_15m = zone["atr_15m"]
    if atr_15m <= 0:
        return None

    high  = float(last["high"])
    low   = float(last["low"])
    close = float(last["close"])
    candle_range = high - low

    # Condition 1: expansion range
    if candle_range <= 1.3 * atr_15m:
        return None

    # Condition 2: directional breakout
    if close > zone["compression_high"]:
        direction = "LONG"
    elif close < zone["compression_low"]:
        direction = "SHORT"
    else:
        return None

    return {
        "expansion_confirmed": True,
        "direction":           direction,
        "breakout_close":      close,
        "candle_range":        candle_range,
    }


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY SIGNAL BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def _build_entry_signal(zone: dict, expansion: dict) -> dict:
    """
    Build an entry signal dict compatible with PositionManager.open().

    LONG breakout:
      SL = compression_low
      TG = entry + 1.0 * ATR_15m
      PT = entry + 2.0 * ATR_15m

    SHORT breakout:
      SL = compression_high
      TG = entry - 1.0 * ATR_15m
      PT = entry - 2.0 * ATR_15m
    """
    direction = expansion["direction"]
    atr       = zone["atr_15m"]
    entry     = expansion["breakout_close"]
    strength  = zone["compression_strength"]

    if direction == "LONG":
        side = "CALL"
        sl   = zone["compression_low"]
        tg   = round(entry + 1.0 * atr, 2)
        pt   = round(entry + 2.0 * atr, 2)
    else:
        side = "PUT"
        sl   = zone["compression_high"]
        tg   = round(entry - 1.0 * atr, 2)
        pt   = round(entry - 2.0 * atr, 2)

    return {
        "side":             side,
        "entry_price":      entry,
        "sl":               round(sl, 2),
        "tg":               tg,
        "pt":               pt,
        "source":           "COMPRESSION_BREAKOUT",
        "reason":           f"Compression breakout {direction} | strength={strength:.1f}x ATR",
        "score":            75,
        "strength":         "HIGH",
        "compression_zone": zone,
    }


# ─────────────────────────────────────────────────────────────────────────────
# STATE MACHINE
# ─────────────────────────────────────────────────────────────────────────────

class CompressionState:
    """
    Stateful 15m compression/expansion tracker.

    Usage:
        state = CompressionState()
        # call once per bar after slice_15m is updated:
        state.update(slice_15m)

        if state.has_entry and not pm.is_open():
            pm.open(..., state.entry_signal)
            state.consume_entry()
        elif state.has_entry:
            state.consume_entry()   # stale — position already open
    """

    def __init__(self):
        self.market_state: str           = "NEUTRAL"
        self.zone:         Optional[dict] = None
        self.entry_signal: Optional[dict] = None

    @property
    def has_entry(self) -> bool:
        return self.entry_signal is not None

    def consume_entry(self) -> None:
        """Call after dispatching (or discarding) the entry signal."""
        self.entry_signal = None
        self.market_state = "NEUTRAL"
        self.zone         = None

    def update(self, df_15m: pd.DataFrame) -> None:
        """
        Advance the state machine by one 15m bar.

        Transitions:
          NEUTRAL        → detect_compression → ENERGY_BUILDUP (if zone found)
          ENERGY_BUILDUP → detect_expansion   → VOLATILITY_EXPANSION (if breakout)
                         → detect_compression → ENERGY_BUILDUP (still compressed)
                         → (else)             → NEUTRAL (zone dissolved)
          VOLATILITY_EXPANSION → entry_signal held; caller must call consume_entry()
        """
        if df_15m is None or df_15m.empty:
            return

        if self.market_state == "NEUTRAL":
            if len(df_15m) < 3:
                return
            zone = detect_compression(df_15m)
            if zone:
                self.zone         = zone
                self.market_state = "ENERGY_BUILDUP"
                logging.info(
                    f"{CYAN}[COMPRESSION_START] "
                    f"high={zone['compression_high']:.2f} low={zone['compression_low']:.2f} "
                    f"strength={zone['compression_strength']:.1f}x "
                    f"avg_range={zone['avg_range']:.2f} ATR={zone['atr_15m']:.2f}{RESET}"
                )

        elif self.market_state == "ENERGY_BUILDUP":
            expansion = detect_expansion(df_15m, self.zone)
            if expansion:
                self.entry_signal = _build_entry_signal(self.zone, expansion)
                self.market_state = "VOLATILITY_EXPANSION"
                logging.info(
                    f"{GREEN}[EXPANSION_CONFIRMED] "
                    f"direction={expansion['direction']} "
                    f"breakout_close={expansion['breakout_close']:.2f} "
                    f"candle_range={expansion['candle_range']:.2f} "
                    f"ATR={self.zone['atr_15m']:.2f} "
                    f"sl={self.entry_signal['sl']:.2f} "
                    f"tg={self.entry_signal['tg']:.2f} "
                    f"pt={self.entry_signal['pt']:.2f}{RESET}"
                )
            else:
                # Check if still in compression (zone may drift)
                zone = detect_compression(df_15m)
                if zone:
                    self.zone = zone
                    logging.info(
                        f"{YELLOW}[COMPRESSION_ACTIVE] "
                        f"high={zone['compression_high']:.2f} low={zone['compression_low']:.2f} "
                        f"strength={zone['compression_strength']:.1f}x{RESET}"
                    )
                else:
                    logging.info(
                        "[COMPRESSION_DISSOLVED] Zone broke down without expansion — resetting to NEUTRAL"
                    )
                    self.market_state = "NEUTRAL"
                    self.zone         = None

        # VOLATILITY_EXPANSION: entry_signal held for caller to consume
