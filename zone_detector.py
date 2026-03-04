"""Demand/supply zone detection and revisit classification.

This module is intentionally additive. It computes zones from 15m candles and
classifies active zone revisits as breakout vs reversal.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd


@dataclass
class Zone:
    zone_id: str
    zone_type: str  # DEMAND | SUPPLY
    low: float
    high: float
    origin_time: str
    active: bool = True
    breach_time: str = ""


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period, min_periods=3).mean()


def detect_zones(
    candles_15m: pd.DataFrame,
    consolidation_bars: int = 4,
    impulse_mult: float = 1.8,
) -> List[Zone]:
    """Detect demand/supply zones from consolidation -> impulse transitions."""
    if candles_15m is None or len(candles_15m) < consolidation_bars + 6:
        return []
    df = candles_15m.copy().reset_index(drop=True)
    a = _atr(df, 14)
    zones: List[Zone] = []
    for i in range(consolidation_bars, len(df)):
        atr_i = float(a.iloc[i]) if np.isfinite(a.iloc[i]) else float("nan")
        if not np.isfinite(atr_i) or atr_i <= 0:
            continue
        base = df.iloc[i - consolidation_bars : i]
        base_low = float(base["low"].min())
        base_high = float(base["high"].max())
        base_range = base_high - base_low
        consolidation = base_range <= (1.2 * atr_i)
        if not consolidation:
            continue
        row = df.iloc[i]
        body = abs(float(row["close"]) - float(row["open"]))
        if body < impulse_mult * atr_i:
            continue
        ts = str(row.get("time", row.get("datetime", i)))[:19]
        if float(row["close"]) > float(row["open"]):
            zones.append(
                Zone(
                    zone_id=f"D_{i}",
                    zone_type="DEMAND",
                    low=base_low,
                    high=base_high,
                    origin_time=ts,
                )
            )
        else:
            zones.append(
                Zone(
                    zone_id=f"S_{i}",
                    zone_type="SUPPLY",
                    low=base_low,
                    high=base_high,
                    origin_time=ts,
                )
            )
    return zones


def fetch_fyers_15m_history(fyers, symbol: str, days: int = 7) -> pd.DataFrame:
    """Fetch last N days of 15m candles from Fyers history API.

    Returns a normalized DataFrame with columns:
    time, open, high, low, close, volume
    """
    if fyers is None:
        return pd.DataFrame()
    try:
        now = pd.Timestamp.utcnow().floor("min")
        start = (now - pd.Timedelta(days=max(1, int(days)))).strftime("%Y-%m-%d")
        end = now.strftime("%Y-%m-%d")
        payload = {
            "symbol": symbol,
            "resolution": "15",
            "date_format": "1",
            "range_from": start,
            "range_to": end,
            "cont_flag": "1",
        }
        resp = fyers.history(payload)
        candles = (resp or {}).get("candles", [])
        if not candles:
            return pd.DataFrame()
        df = pd.DataFrame(candles, columns=["ts", "open", "high", "low", "close", "volume"])
        df["time"] = pd.to_datetime(df["ts"], unit="s", errors="coerce")
        return df[["time", "open", "high", "low", "close", "volume"]].dropna()
    except Exception as exc:
        logging.warning(f"[ZONE_CONTEXT] fyers history fetch failed symbol={symbol}: {exc}")
        return pd.DataFrame()


def save_zones(zones: List[Zone], path: Path) -> None:
    path.write_text(json.dumps([asdict(z) for z in zones], indent=2), encoding="utf-8")


def load_zones(path: Path) -> List[Zone]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [Zone(**x) for x in raw]


def update_zone_activity(
    zones: List[Zone],
    close_price: float,
    atr_value: float,
    bar_time: str,
    breach_atr_mult: float = 0.25,
) -> None:
    """Deactivate zones when decisively breached."""
    if not np.isfinite(close_price) or not np.isfinite(atr_value) or atr_value <= 0:
        return
    for z in zones:
        if not z.active:
            continue
        if z.zone_type == "DEMAND":
            if close_price < (z.low - breach_atr_mult * atr_value):
                z.active = False
                z.breach_time = str(bar_time)[:19]
        else:
            if close_price > (z.high + breach_atr_mult * atr_value):
                z.active = False
                z.breach_time = str(bar_time)[:19]


def detect_zone_revisit(
    candles_3m: pd.DataFrame,
    zones: List[Zone],
    atr_value: float,
    revisit_window: int = 3,
) -> Optional[dict]:
    """Classify revisit of active zones as BREAKOUT or REVERSAL."""
    if candles_3m is None or len(candles_3m) < max(6, revisit_window + 1):
        return None
    if not np.isfinite(atr_value) or atr_value <= 0:
        return None
    last = candles_3m.iloc[-1]
    close_now = float(last["close"])
    high_now = float(last["high"])
    low_now = float(last["low"])
    for z in zones:
        if not z.active:
            continue
        touched = (low_now <= z.high) and (high_now >= z.low)
        if not touched:
            continue
        if z.zone_type == "DEMAND":
            if close_now < (z.low - 0.10 * atr_value):
                logging.info(
                    "[ZONE_REVISIT][DEMAND] action=BREAKOUT side=PUT "
                    f"close={close_now:.2f} low={z.low:.2f} high={z.high:.2f}"
                )
                return {
                    "zone_type": "DEMAND",
                    "action": "BREAKOUT",
                    "side": "PUT",
                    "zone_id": z.zone_id,
                    "zone_age_bars": revisit_window,
                }
            if close_now > z.high:
                logging.info(
                    "[ZONE_REVISIT][DEMAND] action=REVERSAL side=CALL "
                    f"close={close_now:.2f} low={z.low:.2f} high={z.high:.2f}"
                )
                return {
                    "zone_type": "DEMAND",
                    "action": "REVERSAL",
                    "side": "CALL",
                    "zone_id": z.zone_id,
                    "zone_age_bars": revisit_window,
                }
        else:
            if close_now > (z.high + 0.10 * atr_value):
                logging.info(
                    "[ZONE_REVISIT][SUPPLY] action=BREAKOUT side=CALL "
                    f"close={close_now:.2f} low={z.low:.2f} high={z.high:.2f}"
                )
                return {
                    "zone_type": "SUPPLY",
                    "action": "BREAKOUT",
                    "side": "CALL",
                    "zone_id": z.zone_id,
                    "zone_age_bars": revisit_window,
                }
            if close_now < z.low:
                logging.info(
                    "[ZONE_REVISIT][SUPPLY] action=REVERSAL side=PUT "
                    f"close={close_now:.2f} low={z.low:.2f} high={z.high:.2f}"
                )
                return {
                    "zone_type": "SUPPLY",
                    "action": "REVERSAL",
                    "side": "PUT",
                    "zone_id": z.zone_id,
                    "zone_age_bars": revisit_window,
                }
    return None


__all__ = [
    "Zone",
    "fetch_fyers_15m_history",
    "detect_zones",
    "save_zones",
    "load_zones",
    "update_zone_activity",
    "detect_zone_revisit",
]
