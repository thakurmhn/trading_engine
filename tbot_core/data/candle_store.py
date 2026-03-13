"""In-memory candle buffer + resampler.

Extracted from market_data.py: CandleAggregator + MarketData candle logic.
"""

from __future__ import annotations

import logging
from datetime import datetime, time
from typing import Dict, List, Optional, Tuple

import pandas as pd
import pytz

from tbot_core.indicators.builder import build_indicator_dataframe

IST = pytz.timezone("Asia/Kolkata")
MARKET_OPEN = (9, 15)
MARKET_CLOSE = (15, 30)


def _is_market_hours(ts: datetime) -> bool:
    """Return True if ts is within NSE market hours (9:15-15:30 IST)."""
    if ts.tzinfo is None:
        ts = IST.localize(ts)
    t = ts.time()
    open_ = t.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1], second=0, microsecond=0)
    close_ = t.replace(hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1], second=0, microsecond=0)
    return open_ <= t <= close_


class CandleAggregator:
    """Stateful, in-memory candle builder from tick stream.

    Maintains ring-buffers for any minute-based timeframes.
    On each tick, checks slot boundaries and emits completed OHLCV dicts.
    """

    def __init__(self, symbol: str, intervals: tuple = (3, 15)):
        self.symbol = symbol
        self._intervals = intervals

        # Per-interval state
        self._candles: Dict[int, List[dict]] = {m: [] for m in intervals}
        self._acc: Dict[int, Optional[dict]] = {m: None for m in intervals}
        self._current_slot: Dict[int, Optional[datetime]] = {m: None for m in intervals}

    @staticmethod
    def _slot(ts: datetime, minutes: int) -> datetime:
        s = ts.replace(second=0, microsecond=0)
        return s.replace(minute=(s.minute // minutes) * minutes)

    @staticmethod
    def _new_acc(ts: datetime, ltp: float) -> dict:
        return {"open": ltp, "high": ltp, "low": ltp, "close": ltp,
                "volume": 0.0, "slot": ts}

    @staticmethod
    def _update_acc(acc: dict, ltp: float, vol: float) -> dict:
        acc["high"] = max(acc["high"], ltp)
        acc["low"] = min(acc["low"], ltp)
        acc["close"] = ltp
        acc["volume"] += vol
        return acc

    @staticmethod
    def _acc_to_row(acc: dict, symbol: str) -> dict:
        slot: datetime = acc["slot"]
        return {
            "trade_date": slot.strftime("%Y-%m-%d"),
            "ist_slot": slot.strftime("%H:%M:%S"),
            "time": slot.strftime("%Y-%m-%d %H:%M:%S"),
            "open": acc["open"],
            "high": acc["high"],
            "low": acc["low"],
            "close": acc["close"],
            "volume": acc["volume"],
            "symbol": symbol,
        }

    def on_tick(self, ltp: float, ts: datetime, vol: float = 0.0) -> None:
        """Feed one tick. Emits completed candles automatically."""
        if not _is_market_hours(ts):
            return

        for minutes in self._intervals:
            slot = self._slot(ts, minutes)

            if self._current_slot[minutes] is None:
                self._current_slot[minutes] = slot
                self._acc[minutes] = self._new_acc(slot, ltp)
            elif slot != self._current_slot[minutes]:
                self._candles[minutes].append(
                    self._acc_to_row(self._acc[minutes], self.symbol)
                )
                self._current_slot[minutes] = slot
                self._acc[minutes] = self._new_acc(slot, ltp)
            else:
                self._update_acc(self._acc[minutes], ltp, vol)

    def get_completed_candles(self, interval_minutes: int) -> List[dict]:
        return self._candles.get(interval_minutes, [])

    def candle_count(self, interval_minutes: int) -> int:
        return len(self._candles.get(interval_minutes, []))

    def reset(self) -> None:
        for m in self._intervals:
            self._candles[m].clear()
            self._acc[m] = None
            self._current_slot[m] = None


class CandleStore:
    """Multi-timeframe candle store with indicator caching.

    High-level wrapper combining CandleAggregator (tick -> candles)
    with warmup data and indicator enrichment.
    """

    def __init__(self, symbol: str, intervals: tuple = (3, 15)):
        self.symbol = symbol
        self._agg = CandleAggregator(symbol, intervals)
        self._warmup: Dict[int, pd.DataFrame] = {m: pd.DataFrame() for m in intervals}
        self._cache: Dict[int, Tuple[int, pd.DataFrame]] = {}

    def set_warmup(self, interval_minutes: int, df: pd.DataFrame) -> None:
        """Set historical warmup data for an interval."""
        self._warmup[interval_minutes] = df

    def on_tick(self, ltp: float, ts: datetime, vol: float = 0.0) -> None:
        self._agg.on_tick(ltp, ts, vol)

    def get_candles(self, interval_minutes: int = 3) -> pd.DataFrame:
        """Get indicator-enriched candles (warmup + live, cached)."""
        count = self._agg.candle_count(interval_minutes)
        cached_count, cached_df = self._cache.get(interval_minutes, (-1, pd.DataFrame()))

        if count != cached_count:
            warmup = self._warmup.get(interval_minutes, pd.DataFrame())
            live = self._agg.get_completed_candles(interval_minutes)

            if not live:
                raw = warmup.copy() if not warmup.empty else pd.DataFrame()
            else:
                live_df = pd.DataFrame(live)
                if warmup.empty:
                    raw = live_df
                else:
                    raw = pd.concat([warmup, live_df], ignore_index=True)
                    raw = (raw.drop_duplicates(subset=["time"], keep="last")
                           .sort_values("time")
                           .reset_index(drop=True))

            if not raw.empty:
                interval_str = f"{interval_minutes}m"
                cached_df = build_indicator_dataframe(self.symbol, raw, interval=interval_str)

            self._cache[interval_minutes] = (count, cached_df)

        return cached_df

    def reset(self) -> None:
        self._agg.reset()
        self._cache.clear()
