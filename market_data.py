# ============================================================
#  market_data.py  — v1.0
#  Authoritative Market Data Layer for NSE Options Bot
# ============================================================
"""
ARCHITECTURE MANDATE
────────────────────
Primary source (LIVE mode):
  1. Pre-market warmup  → Fyers historical API (candles before market open)
  2. Intraday           → Tick-by-tick from WebSocket, aggregated in-memory
  3. Indicators         → Always recomputed on the full warmup+intraday series

SQLite is NEVER the indicator source in LIVE mode.
SQLite is ONLY used for:
  - Persisting raw ticks (audit/replay)
  - Replay/backtest mode (explicit flag required)

Why SQLite is unreliable for indicators:
  - Candle rows may be missing (websocket gaps)
  - is_partial=1 candles pollute rolling calculations
  - post-market flat candles corrupt EMA/RSI/Supertrend state
  - No transaction guarantee on concurrent writes from data_feed.py

Data flow (LIVE):
  [Fyers API historical]     →  warmup_df (N×3m, N×15m completed bars)
            +
  [WebSocket ticks in RAM]   →  live_df   (today's completed 3m/15m bars)
            =
  [Full series]              →  build_indicator_dataframe()
                                → strategy receives indicator-enriched df

Data flow (REPLAY):
  [SQLite candles]           →  replay_df (filtered by date, market hours only)
            =
  [Full series]              →  build_indicator_dataframe()

Public API
──────────
  md = MarketData(fyers_client, mode="LIVE")   # or "REPLAY"
  await md.warmup(symbols)                      # call once before market open

  # In websocket tick callback:
  md.on_tick(symbol, ltp, ts)

  # In strategy loop (every new 3m candle):
  df_3m, df_15m = md.get_candles(symbol)       # always indicator-enriched
  spot          = md.get_spot(symbol)
  prev_day_ohlc = md.get_prev_day_ohlc(symbol)

  # For replay only:
  md_replay = MarketData(fyers_client=None, mode="REPLAY", db_path="ticks_DATE.db")
  await md_replay.warmup(symbols, date_str="2026-02-20")
  df_3m, df_15m = md_replay.get_candles(symbol)
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import pytz

from orchestration import build_indicator_dataframe

IST = pytz.timezone("Asia/Kolkata")
MARKET_OPEN   = (9, 15)    # HH, MM
MARKET_CLOSE  = (15, 30)   # HH, MM  — hard stop for candle evaluation
WARMUP_3M_DAYS  = 3        # Fyers API days for 3m history (covers ~225 bars)
WARMUP_15M_DAYS = 10       # Fyers API days for 15m history (covers ~250 bars)

GREEN  = "\033[92m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RED    = "\033[91m"
RESET  = "\033[0m"


# ─────────────────────────────────────────────────────────────────────────────
#  Tick  — lightweight named tuple stored in RAM
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(slots=True)
class Tick:
    symbol    : str
    ltp       : float
    ts        : datetime          # timezone-aware IST


# ─────────────────────────────────────────────────────────────────────────────
#  CandleAggregator  — builds OHLCV candles from a stream of Ticks in memory
# ─────────────────────────────────────────────────────────────────────────────
class CandleAggregator:
    """
    Stateful, in-memory candle builder.

    Maintains two ring-buffers:
      _ticks_3m  — ticks since last 3m boundary
      _ticks_15m — ticks since last 15m boundary

    On each tick the aggregator checks whether the current slot has changed.
    When a slot closes it emits a completed OHLCV dict and appends it to
    _candles_3m / _candles_15m.

    No I/O, no SQLite — pure in-memory arithmetic.
    """

    def __init__(self, symbol: str):
        self.symbol = symbol

        # Completed candles  (list of dicts — cheap to convert to DataFrame)
        self._candles_3m  : List[dict] = []
        self._candles_15m : List[dict] = []

        # In-progress accumulators
        self._acc_3m  : Optional[dict] = None
        self._acc_15m : Optional[dict] = None

        # Slot tracking
        self._current_slot_3m  : Optional[datetime] = None
        self._current_slot_15m : Optional[datetime] = None

    # ── helpers ──────────────────────────────────────────────────────────────
    @staticmethod
    def _slot(ts: datetime, minutes: int) -> datetime:
        """Round ts down to nearest N-minute boundary."""
        s = ts.replace(second=0, microsecond=0)
        return s.replace(minute=(s.minute // minutes) * minutes)

    @staticmethod
    def _new_acc(ts: datetime, ltp: float) -> dict:
        return {"open": ltp, "high": ltp, "low": ltp, "close": ltp,
                "volume": 0.0, "slot": ts}

    @staticmethod
    def _update_acc(acc: dict, ltp: float, vol: float) -> dict:
        acc["high"]   = max(acc["high"], ltp)
        acc["low"]    = min(acc["low"],  ltp)
        acc["close"]  = ltp
        acc["volume"] += vol
        return acc

    @staticmethod
    def _acc_to_row(acc: dict, symbol: str) -> dict:
        slot: datetime = acc["slot"]
        return {
            "trade_date": slot.strftime("%Y-%m-%d"),
            "ist_slot":   slot.strftime("%H:%M:%S"),
            "time":       slot.strftime("%Y-%m-%d %H:%M:%S"),
            "open":       acc["open"],
            "high":       acc["high"],
            "low":        acc["low"],
            "close":      acc["close"],
            "volume":     acc["volume"],
            "symbol":     symbol,
        }

    # ── public ───────────────────────────────────────────────────────────────
    def on_tick(self, ltp: float, ts: datetime, vol: float = 0.0) -> None:
        """
        Feed one tick.  Emits completed candles automatically.
        Call from websocket callback — no locking needed (GIL-safe for CPython).
        """
        if not _is_market_hours(ts):
            return

        slot_3m  = self._slot(ts, 3)
        slot_15m = self._slot(ts, 15)

        # ── 3m ──────────────────────────────────────────────────────────────
        if self._current_slot_3m is None:
            self._current_slot_3m = slot_3m
            self._acc_3m = self._new_acc(slot_3m, ltp)
        elif slot_3m != self._current_slot_3m:
            # Slot closed — emit completed candle
            self._candles_3m.append(self._acc_to_row(self._acc_3m, self.symbol))
            self._current_slot_3m = slot_3m
            self._acc_3m = self._new_acc(slot_3m, ltp)
        else:
            self._update_acc(self._acc_3m, ltp, vol)

        # ── 15m ─────────────────────────────────────────────────────────────
        if self._current_slot_15m is None:
            self._current_slot_15m = slot_15m
            self._acc_15m = self._new_acc(slot_15m, ltp)
        elif slot_15m != self._current_slot_15m:
            self._candles_15m.append(self._acc_to_row(self._acc_15m, self.symbol))
            self._current_slot_15m = slot_15m
            self._acc_15m = self._new_acc(slot_15m, ltp)
        else:
            self._update_acc(self._acc_15m, ltp, vol)

    def get_completed_candles(self, interval: str) -> List[dict]:
        """Return only completed (closed) candles — never the in-progress one."""
        return self._candles_3m if interval == "3m" else self._candles_15m

    def candle_count(self, interval: str) -> int:
        return len(self._candles_3m if interval == "3m" else self._candles_15m)

    def reset(self) -> None:
        """Call at start of each session."""
        self._candles_3m.clear()
        self._candles_15m.clear()
        self._acc_3m  = self._acc_15m  = None
        self._current_slot_3m = self._current_slot_15m = None


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _is_market_hours(ts: datetime) -> bool:
    """Return True if ts is within NSE market hours (9:15–15:30 IST)."""
    if ts.tzinfo is None:
        ts = IST.localize(ts)
    t = ts.time()
    open_  = t.replace(hour=MARKET_OPEN[0],  minute=MARKET_OPEN[1],  second=0, microsecond=0)
    close_ = t.replace(hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1], second=0, microsecond=0)
    return open_ <= t <= close_


def _fyers_to_df(candles: list, symbol: str) -> pd.DataFrame:
    """Convert Fyers API candles list to a normalised DataFrame."""
    if not candles:
        return pd.DataFrame()
    df = pd.DataFrame(candles, columns=["date", "open", "high", "low", "close", "volume"])
    df["date"] = (
        pd.to_datetime(df["date"], unit="s")
        .dt.tz_localize("UTC")
        .dt.tz_convert(IST)
    )
    # Keep only market-hours rows
    df = df[df["date"].apply(_is_market_hours)].copy()
    df["trade_date"] = df["date"].dt.strftime("%Y-%m-%d")
    df["ist_slot"]   = df["date"].dt.strftime("%H:%M:%S")
    df["time"]       = df["trade_date"] + " " + df["ist_slot"]
    df["symbol"]     = symbol
    return df.drop(columns=["date"]).reset_index(drop=True)


def _merge_warmup_and_live(warmup: pd.DataFrame, live: List[dict]) -> pd.DataFrame:
    """
    Concatenate historical warmup rows with in-memory live candles.
    Deduplicates on 'time' column — live wins over warmup for same slot.
    """
    if not live:
        return warmup.copy() if not warmup.empty else pd.DataFrame()

    live_df = pd.DataFrame(live)
    if warmup.empty:
        return live_df

    combined = pd.concat([warmup, live_df], ignore_index=True)
    # Drop duplicates — keep last (live_df rows come after warmup, so they win)
    combined = (combined
                .drop_duplicates(subset=["time"], keep="last")
                .sort_values("time")
                .reset_index(drop=True))
    return combined


# ─────────────────────────────────────────────────────────────────────────────
#  MarketData  — top-level singleton used by the strategy
# ─────────────────────────────────────────────────────────────────────────────
class MarketData:
    """
    Single source of truth for all OHLCV and indicator data.

    Usage (live):
        md = MarketData(fyers_client=fyers, mode="LIVE")
        md.warmup(symbols)                   # blocking call at startup
        md.on_tick(sym, ltp, ts)             # called from websocket thread
        df3, df15 = md.get_candles(sym)      # indicator-enriched, market-hours only
        spot = md.get_spot(sym)

    Usage (replay):
        md = MarketData(fyers_client=None, mode="REPLAY",
                        db_path="ticks_2026-02-20.db")
        md.warmup(symbols, replay_date="2026-02-20")
        df3, df15 = md.get_candles(sym)
    """

    def __init__(
        self,
        fyers_client=None,
        mode: str = "LIVE",
        db_path: Optional[str] = None,
    ):
        assert mode in ("LIVE", "REPLAY"), f"Invalid mode: {mode}"
        self.mode         = mode
        self._fyers       = fyers_client
        self._db_path     = db_path          # only used in REPLAY mode

        # Per-symbol state
        self._warmup_3m  : Dict[str, pd.DataFrame] = {}  # Fyers history (completed bars only)
        self._warmup_15m : Dict[str, pd.DataFrame] = {}
        self._agg        : Dict[str, CandleAggregator] = {}
        self._spot       : Dict[str, float] = {}
        self._prev_ohlc  : Dict[str, dict]  = {}         # prev trading day H/L/C

        # Indicator cache — invalidated when live candle count changes
        self._indicator_cache_3m  : Dict[str, Tuple[int, pd.DataFrame]] = {}
        self._indicator_cache_15m : Dict[str, Tuple[int, pd.DataFrame]] = {}

        logging.info(f"{GREEN}[MarketData] Initialized mode={mode}{RESET}")

    # ─────────────────────────────────────────────────────────────────────────
    #  WARMUP  (call once before market open, or at replay start)
    # ─────────────────────────────────────────────────────────────────────────
    def warmup(
        self,
        symbols: List[str],
        replay_date: Optional[str] = None,
    ) -> None:
        """
        Populate historical candles from Fyers API (LIVE) or SQLite (REPLAY).

        LIVE mode:
          Fetches the last WARMUP_3M_DAYS of 3m bars and WARMUP_15M_DAYS of
          15m bars via fyers.history().  Strips today's incomplete data.
          Call this BEFORE market opens (e.g., 9:00–9:14 IST).

        REPLAY mode:
          Reads from SQLite, prepends warmup rows from previous days,
          and loads the target date's candles.  The in-memory aggregators
          are pre-populated so get_candles() works immediately.
        """
        if self.mode == "LIVE":
            self._warmup_live(symbols)
        else:
            self._warmup_replay(symbols, replay_date)

    def _warmup_live(self, symbols: List[str]) -> None:
        today_str = datetime.now(IST).strftime("%Y-%m-%d")

        for sym in symbols:
            self._agg[sym] = CandleAggregator(sym)

            # ── 3m warmup ───────────────────────────────────────────────────
            df3 = self._fetch_fyers_history(sym, resolution="3",
                                            days=WARMUP_3M_DAYS,
                                            include_today=False)
            self._warmup_3m[sym] = df3
            logging.info(
                f"{CYAN}[WARMUP] {sym} 3m: {len(df3)} historical bars "
                f"({WARMUP_3M_DAYS} days){RESET}"
            )

            # ── 15m warmup ──────────────────────────────────────────────────
            df15 = self._fetch_fyers_history(sym, resolution="15",
                                             days=WARMUP_15M_DAYS,
                                             include_today=False)
            self._warmup_15m[sym] = df15
            logging.info(
                f"{CYAN}[WARMUP] {sym} 15m: {len(df15)} historical bars "
                f"({WARMUP_15M_DAYS} days){RESET}"
            )

            # ── Previous day OHLC for pivot levels ──────────────────────────
            if not df15.empty:
                prev = df15[df15["trade_date"] == df15["trade_date"].iloc[-1]]
                if not prev.empty:
                    self._prev_ohlc[sym] = {
                        "high":  prev["high"].max(),
                        "low":   prev["low"].min(),
                        "close": prev["close"].iloc[-1],
                        "date":  prev["trade_date"].iloc[-1],
                    }
                    logging.info(
                        f"[WARMUP] {sym} prev_day "
                        f"H={self._prev_ohlc[sym]['high']} "
                        f"L={self._prev_ohlc[sym]['low']} "
                        f"C={self._prev_ohlc[sym]['close']} "
                        f"({self._prev_ohlc[sym]['date']})"
                    )

    def _warmup_replay(self, symbols: List[str], replay_date: str) -> None:
        """Load warmup + target-date candles from SQLite for replay."""
        import sqlite3
        if not self._db_path:
            raise ValueError("[REPLAY] db_path must be set for REPLAY mode")

        target = date.fromisoformat(replay_date)

        for sym in symbols:
            self._agg[sym] = CandleAggregator(sym)

            conn = sqlite3.connect(self._db_path, check_same_thread=False)

            # ── Load ALL rows for this symbol then filter ────────────────────
            df3_all  = self._read_candles_from_db(conn, "candles_3m_ist",  sym)
            df15_all = self._read_candles_from_db(conn, "candles_15m_ist", sym)
            conn.close()

            # ── Market-hours filter — CRITICAL: strip post-15:30 rows ────────
            df3_all  = _filter_market_hours(df3_all)
            df15_all = _filter_market_hours(df15_all)

            # ── Split into warmup (pre-target) and replay (target date) ──────
            df3_all["_date"]  = pd.to_datetime(df3_all["time"]).dt.date
            df15_all["_date"] = pd.to_datetime(df15_all["time"]).dt.date

            warmup_3m  = df3_all[df3_all["_date"] < target].drop(columns=["_date"])
            today_3m   = df3_all[df3_all["_date"] == target].drop(columns=["_date"])
            warmup_15m = df15_all[df15_all["_date"] < target].drop(columns=["_date"])
            today_15m  = df15_all[df15_all["_date"] == target].drop(columns=["_date"])

            self._warmup_3m[sym]  = warmup_3m.reset_index(drop=True)
            self._warmup_15m[sym] = warmup_15m.reset_index(drop=True)

            # Pre-populate aggregator with today's completed candles
            for row in today_3m.to_dict("records"):
                self._agg[sym]._candles_3m.append(row)
            for row in today_15m.to_dict("records"):
                self._agg[sym]._candles_15m.append(row)

            # Previous day OHLC
            if not warmup_15m.empty:
                prev_date = warmup_15m["trade_date"].iloc[-1]
                prev = warmup_15m[warmup_15m["trade_date"] == prev_date]
                self._prev_ohlc[sym] = {
                    "high":  prev["high"].max(),
                    "low":   prev["low"].min(),
                    "close": prev["close"].iloc[-1],
                    "date":  prev_date,
                }

            logging.info(
                f"{CYAN}[REPLAY WARMUP] {sym} "
                f"warmup_3m={len(warmup_3m)} warmup_15m={len(warmup_15m)} "
                f"replay_3m={len(today_3m)} replay_15m={len(today_15m)}{RESET}"
            )

    @staticmethod
    def _read_candles_from_db(conn, table: str, symbol: str) -> pd.DataFrame:
        try:
            df = pd.read_sql_query(
                f"SELECT * FROM {table} WHERE symbol=? ORDER BY trade_date, ist_slot",
                conn, params=[symbol]
            )
            if "time" not in df.columns and "trade_date" in df.columns:
                df["time"] = df["trade_date"] + " " + df["ist_slot"]
            return df
        except Exception as e:
            logging.warning(f"[REPLAY DB] {table}: {e}")
            return pd.DataFrame()

    # ─────────────────────────────────────────────────────────────────────────
    #  TICK INGESTION  (live mode — called from websocket callback)
    # ─────────────────────────────────────────────────────────────────────────
    def on_tick(self, symbol: str, ltp: float, ts: datetime, vol: float = 0.0) -> None:
        """
        Feed a live tick.  Thread-safe under CPython GIL.
        Updates spot price and delegates to CandleAggregator.
        """
        self._spot[symbol] = ltp

        if symbol not in self._agg:
            self._agg[symbol] = CandleAggregator(symbol)

        self._agg[symbol].on_tick(ltp, ts, vol)

    # ─────────────────────────────────────────────────────────────────────────
    #  CANDLE RETRIEVAL  (called by strategy every loop iteration)
    # ─────────────────────────────────────────────────────────────────────────
    def get_candles(
        self,
        symbol: str,
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        Returns (df_3m, df_15m) — both indicator-enriched.

        Indicator cache is keyed on completed-candle-count.
        Indicators are only recomputed when a new candle closes (every ~3 min).
        Between candle closes this returns the same cached df — zero compute cost.

        The returned dataframe always has:
          - Warmup history prepended (for correct ST/ADX/CCI state)
          - Only market-hours completed candles (9:15–15:30)
          - Columns: open, high, low, close, volume, time, ema9, ema13,
                     adx14, cci20, rsi14, atr14, supertrend_bias,
                     supertrend_slope, supertrend_line, vwap
        """
        agg = self._agg.get(symbol)
        if agg is None:
            return pd.DataFrame(), pd.DataFrame()

        count_3m  = agg.candle_count("3m")
        count_15m = agg.candle_count("15m")

        # ── 3m indicators ────────────────────────────────────────────────────
        cached_count_3m, cached_df_3m = self._indicator_cache_3m.get(symbol, (-1, pd.DataFrame()))
        if count_3m != cached_count_3m:
            raw_3m = _merge_warmup_and_live(
                self._warmup_3m.get(symbol, pd.DataFrame()),
                agg.get_completed_candles("3m"),
            )
            if not raw_3m.empty:
                cached_df_3m = build_indicator_dataframe(symbol, raw_3m, interval="3m")
            self._indicator_cache_3m[symbol] = (count_3m, cached_df_3m)

        # ── 15m indicators ───────────────────────────────────────────────────
        cached_count_15m, cached_df_15m = self._indicator_cache_15m.get(symbol, (-1, pd.DataFrame()))
        if count_15m != cached_count_15m:
            raw_15m = _merge_warmup_and_live(
                self._warmup_15m.get(symbol, pd.DataFrame()),
                agg.get_completed_candles("15m"),
            )
            if not raw_15m.empty:
                cached_df_15m = build_indicator_dataframe(symbol, raw_15m, interval="15m")
            self._indicator_cache_15m[symbol] = (count_15m, cached_df_15m)

        return cached_df_3m, cached_df_15m

    def get_spot(self, symbol: str) -> Optional[float]:
        return self._spot.get(symbol)

    def get_prev_day_ohlc(self, symbol: str) -> Optional[dict]:
        return self._prev_ohlc.get(symbol)

    # ─────────────────────────────────────────────────────────────────────────
    #  FYERS HISTORY FETCH  (internal)
    # ─────────────────────────────────────────────────────────────────────────
    def _fetch_fyers_history(
        self,
        symbol: str,
        resolution: str,
        days: int,
        include_today: bool = False,
    ) -> pd.DataFrame:
        """
        Fetch historical candles from Fyers API.
        Always excludes post-15:30 rows and partial/current candles.
        """
        if self._fyers is None:
            logging.warning(f"[WARMUP] No fyers client — skipping API fetch for {symbol}")
            return pd.DataFrame()

        today     = datetime.now(IST).date()
        range_from = (today - timedelta(days=days)).strftime("%Y-%m-%d")
        range_to   = (today + timedelta(days=1) if include_today else today).strftime("%Y-%m-%d")

        req = {
            "symbol":      symbol,
            "resolution":  resolution,
            "date_format": "1",
            "range_from":  range_from,
            "range_to":    range_to,
            "cont_flag":   "1",
        }
        try:
            resp    = self._fyers.history(data=req)
            candles = resp.get("candles", [])
            if not candles:
                logging.warning(f"[WARMUP] Fyers returned 0 candles for {symbol} res={resolution}")
                return pd.DataFrame()

            df = _fyers_to_df(candles, symbol)

            # Strip today's bars (include_today=False → warmup only)
            if not include_today:
                df = df[df["trade_date"] < today.isoformat()].copy()

            logging.info(
                f"[WARMUP] Fyers API {symbol} res={resolution} "
                f"rows={len(df)} last={df['time'].iloc[-1] if not df.empty else 'none'}"
            )
            return df.reset_index(drop=True)

        except Exception as e:
            logging.error(f"[WARMUP ERROR] {symbol} res={resolution}: {e}", exc_info=True)
            return pd.DataFrame()


# ─────────────────────────────────────────────────────────────────────────────
#  Utility: market-hours filter — ALWAYS applied to SQLite data
# ─────────────────────────────────────────────────────────────────────────────
def _filter_market_hours(df: pd.DataFrame) -> pd.DataFrame:
    """
    Strip rows outside 9:15–15:30 IST.
    This is the single defence against post-market flat-candle corruption.
    """
    if df.empty:
        return df

    time_col = "time" if "time" in df.columns else None
    slot_col = "ist_slot" if "ist_slot" in df.columns else None

    if time_col:
        times = pd.to_datetime(df[time_col], errors="coerce")
        hhmm  = times.dt.hour * 100 + times.dt.minute
        mask  = (hhmm >= 915) & (hhmm <= 1530)
        return df[mask].copy().reset_index(drop=True)

    if slot_col:
        def _hhmm(s):
            try:
                h, m, *_ = str(s).split(":")
                return int(h) * 100 + int(m)
            except Exception:
                return 0
        mask = df[slot_col].apply(_hhmm).between(915, 1530)
        return df[mask].copy().reset_index(drop=True)

    return df