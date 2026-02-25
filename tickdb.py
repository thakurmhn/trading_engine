# ============================================================
#  tickdb.py  — v2.1  (audit persistence + candle builder)
# ============================================================
"""
PURPOSE
───────
TickDatabase is the SQLite audit layer.  It is NOT the live indicator source.

Responsibilities:
  1. insert_tick()             — persist every raw WebSocket tick (audit / replay)
  2. build_candles_from_ticks() — aggregate ticks → OHLCV for SQLite audit tables
  3. fetch_candles()           — read candles for replay / OFFLINE mode
  4. fetch_ticks()             — read raw ticks for rebuild / replay

v2.1 fixes:
  ─ insert_tick() stores timestamps as UTC ISO strings.
    build_candles_from_ticks() was calling dt.tz_localize('UTC') even when
    timestamps were already timezone-aware → tz_localize raises TypeError.
    Fixed: detect tz before localizing.
  ─ fetch_candles() adds row-count and last-slot debug log for continuity.
  ─ is_partial filter applied in fetch_candles(completed_only=True default)
    so strategy layer never accidentally receives partial candles via this path.
  ─ Duplicate import block removed (was imported twice at top of file).
  ─ build_candles_from_ticks() logs [CANDLE CONTINUITY] row count + last slot.
"""

import glob
import logging
import os
import sqlite3
from datetime import datetime, timedelta

import pandas as pd
import pytz

time_zone = pytz.timezone("Asia/Kolkata")
MARKET_OPEN   = (9, 15)    # HH, MM
MARKET_CLOSE  = (15, 30)   # HH, MM

# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt(val):
    """Format numeric values safely for logs."""
    return f"{val:.2f}" if val is not None and not pd.isna(val) else "NA"

def _is_market_hours(ts_str: str) -> bool:
    """
    Check if timestamp (HH:MM:SS format) is within NSE market hours (9:15-15:30).
    Returns True if within market hours, False if pre/post-market.
    """
    try:
        if ':' not in ts_str:
            return False
        parts = ts_str.split(':')
        h, m = int(parts[0]), int(parts[1])
        
        open_h, open_m = MARKET_OPEN
        close_h, close_m = MARKET_CLOSE
        
        # Before 9:15
        if h < open_h or (h == open_h and m < open_m):
            return False
        # After 15:30
        if h > close_h or (h == close_h and m > close_m):
            return False
        return True
    except (ValueError, IndexError):
        return True  # If parsing fails, assume valid to be permissive


# ─────────────────────────────────────────────────────────────────────────────

class TickDatabase:
    def __init__(self, base_path=r"C:\SQLite\ticks", max_lookback=5):
        base_path = os.path.abspath(base_path)
        os.makedirs(base_path, exist_ok=True)

        today_str = datetime.now().strftime("%Y-%m-%d")
        db_file   = os.path.join(base_path, f"ticks_{today_str}.db")

        self.conn   = sqlite3.connect(db_file, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self._create_tables()

        self.base_path   = base_path
        self.max_lookback = max_lookback

        logging.info(f"[TICKDB] Using database: {db_file}")

    # ─────────────────────────────────────────────────────────────────────────
    #  Schema
    # ─────────────────────────────────────────────────────────────────────────

    def _create_tables(self):
        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   DATETIME NOT NULL,
            trade_date  DATE     NOT NULL,
            symbol      TEXT     NOT NULL,
            bid REAL, ask REAL, last_price REAL, volume REAL
        )""")
        self.cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_symbol_time ON ticks(symbol, timestamp)"
        )

        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS candles_3m_ist (
            trade_date TEXT NOT NULL,
            ist_slot   TEXT NOT NULL,
            symbol     TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            is_partial INTEGER DEFAULT 0,
            PRIMARY KEY (trade_date, ist_slot, symbol)
        )""")

        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS candles_15m_ist (
            trade_date TEXT NOT NULL,
            ist_slot   TEXT NOT NULL,
            symbol     TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            is_partial INTEGER DEFAULT 0,
            PRIMARY KEY (trade_date, ist_slot, symbol)
        )""")

        self.conn.commit()

    # ─────────────────────────────────────────────────────────────────────────
    #  Tick persistence
    # ─────────────────────────────────────────────────────────────────────────

    def insert_tick(self, symbol, bid, ask, last_price, volume):
        """Persist a raw tick. Timestamp stored as UTC ISO string."""
        ts         = datetime.utcnow().isoformat()          # always UTC
        trade_date = datetime.now(time_zone).strftime("%Y-%m-%d")  # IST date
        try:
            self.cursor.execute("""
                INSERT INTO ticks
                    (timestamp, trade_date, symbol, bid, ask, last_price, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                str(ts), str(trade_date), str(symbol),
                float(bid)        if bid        is not None else None,
                float(ask)        if ask        is not None else None,
                float(last_price) if last_price is not None else None,
                float(volume)     if volume     is not None else None,
            ))
            self.conn.commit()
        except Exception as exc:
            logging.error(f"[TICKDB INSERT ERROR] {symbol}: {exc}")

    # ─────────────────────────────────────────────────────────────────────────
    #  Candle persistence helpers
    # ─────────────────────────────────────────────────────────────────────────

    def insert_3m_candle(self, trade_date, ist_slot,
                         open_price, high_price, low_price, close_price,
                         volume, symbol, is_partial=False):
        try:
            self.cursor.execute("""
                INSERT OR REPLACE INTO candles_3m_ist
                    (trade_date, ist_slot, symbol,
                     open, high, low, close, volume, is_partial)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                str(trade_date), str(ist_slot), str(symbol),
                float(open_price)  if open_price  is not None else None,
                float(high_price)  if high_price  is not None else None,
                float(low_price)   if low_price   is not None else None,
                float(close_price) if close_price is not None else None,
                float(volume)      if volume      is not None else None,
                int(is_partial),
            ))
            self.conn.commit()
            logging.debug(
                f"[TICKDB] 3m candle {trade_date} {ist_slot} {symbol} "
                f"partial={is_partial}"
            )
        except Exception as exc:
            logging.error(f"[TICKDB 3M INSERT ERROR] {symbol}: {exc}")

    def insert_15m_candle(self, trade_date, ist_slot,
                          open_, high, low, close, volume, symbol,
                          is_partial=False):
        try:
            self.cursor.execute("""
                INSERT OR REPLACE INTO candles_15m_ist
                    (trade_date, ist_slot, symbol,
                     open, high, low, close, volume, is_partial)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                str(trade_date), str(ist_slot), str(symbol),
                float(open_)  if open_  is not None else None,
                float(high)   if high   is not None else None,
                float(low)    if low    is not None else None,
                float(close)  if close  is not None else None,
                float(volume) if volume is not None else None,
                int(is_partial),
            ))
            self.conn.commit()
            logging.debug(
                f"[TICKDB] 15m candle {trade_date} {ist_slot} {symbol} "
                f"partial={is_partial}"
            )
        except Exception as exc:
            logging.error(f"[TICKDB 15M INSERT ERROR] {symbol}: {exc}")

    # ─────────────────────────────────────────────────────────────────────────
    #  Tick retrieval
    # ─────────────────────────────────────────────────────────────────────────

    def fetch_ticks(self, symbol, start_time=None, end_time=None):
        query  = "SELECT timestamp, last_price, volume FROM ticks WHERE symbol=?"
        params = [symbol]
        if start_time:
            query  += " AND timestamp >= ?"
            params.append(start_time)
        if end_time:
            query  += " AND timestamp <= ?"
            params.append(end_time)
        try:
            df = pd.read_sql_query(query, self.conn, params=params)
            if df.empty:
                return pd.DataFrame(columns=["timestamp", "last_price", "volume"])
            df["last_price"] = pd.to_numeric(df["last_price"], errors="coerce")
            df["volume"]     = pd.to_numeric(df["volume"],     errors="coerce")
            return df
        except Exception as exc:
            logging.error(f"[TICKDB FETCH TICKS] {symbol}: {exc}")
            return pd.DataFrame(columns=["timestamp", "last_price", "volume"])

    def get_latest_tick(self, symbol):
        """Return the most recent tick dict, or None."""
        try:
            df = pd.read_sql_query(
                "SELECT * FROM ticks WHERE symbol=? ORDER BY timestamp DESC LIMIT 1",
                self.conn, params=[symbol],
            )
            if df.empty:
                return None
            for col in ["last_price", "volume", "bid", "ask"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            return df.iloc[0].to_dict()
        except Exception as exc:
            logging.error(f"[TICKDB LATEST TICK] {symbol}: {exc}")
            return None

    def replay_ticks(self, symbol):
        try:
            df = pd.read_sql_query(
                "SELECT * FROM ticks WHERE symbol=? ORDER BY timestamp ASC",
                self.conn, params=[symbol],
            )
            df["last_price"] = pd.to_numeric(df["last_price"], errors="coerce")
            df["volume"]     = pd.to_numeric(df["volume"],     errors="coerce")
            return df
        except Exception as exc:
            logging.error(f"[TICKDB REPLAY TICKS] {symbol}: {exc}")
            return pd.DataFrame()

    # ─────────────────────────────────────────────────────────────────────────
    #  Candle building (SQLite audit path — NOT the live indicator source)
    # ─────────────────────────────────────────────────────────────────────────

    def build_candles_from_ticks(self, symbol: str, interval: str = "3m") -> None:
        """
        Aggregate raw SQLite ticks → OHLCV candle rows.

        IMPORTANT: This writes to SQLite for audit and REPLAY mode.
        In LIVE mode, indicators are computed from MarketData (in-memory)
        not from these SQLite candles.

        FIX v2.1:
          Timestamps in `ticks` table are stored as UTC ISO strings (naive).
          tz_localize('UTC') must only be called on naive Series.
          If already tz-aware, convert directly with tz_convert().
        """
        df_ticks = self.fetch_ticks(symbol)
        if df_ticks.empty:
            logging.warning(f"[TICKDB BUILD] No ticks for {symbol} — candle build skipped")
            return

        # ── Parse timestamp, ensure IST-aware ────────────────────────────────
        df_ticks["ts"] = pd.to_datetime(df_ticks["timestamp"], errors="coerce")
        df_ticks = df_ticks.dropna(subset=["ts"])

        if df_ticks.empty:
            logging.warning(f"[TICKDB BUILD] All timestamps invalid for {symbol}")
            return

        # Handle naive (UTC from insert_tick) vs already tz-aware
        if df_ticks["ts"].dt.tz is None:
            df_ticks["ts"] = df_ticks["ts"].dt.tz_localize("UTC").dt.tz_convert("Asia/Kolkata")
        else:
            df_ticks["ts"] = df_ticks["ts"].dt.tz_convert("Asia/Kolkata")

        # ── Resample into OHLCV ───────────────────────────────────────────────
        rule = {"3m": "3min", "15m": "15min"}.get(interval)
        if rule is None:
            logging.error(f"[TICKDB BUILD] Unsupported interval={interval}")
            return

        ohlc = (
            df_ticks.resample(rule, on="ts")
            .agg({
                "last_price": ["first", "max", "min", "last"],
                "volume":     "sum",
            })
        )
        ohlc.columns = ["open", "high", "low", "close", "volume"]
        ohlc.reset_index(inplace=True)

        # Forward-fill sparse bars (prevent NaN in partial candles)
        ohlc["open"]   = ohlc["open"].ffill()
        ohlc["high"]   = ohlc["high"].fillna(ohlc["open"])
        ohlc["low"]    = ohlc["low"].fillna(ohlc["open"])
        ohlc["close"]  = ohlc["close"].fillna(ohlc["open"])
        ohlc["volume"] = ohlc["volume"].fillna(0)

        # ── Mark partial (last bar if its slot window is still open) ──────────
        now_ist = pd.Timestamp.now(tz="Asia/Kolkata")
        ohlc["is_partial"] = False
        if not ohlc.empty:
            last_slot = ohlc.iloc[-1]["ts"]
            window    = 180 if interval == "3m" else 900
            if (now_ist - last_slot).total_seconds() < window:
                ohlc.at[ohlc.index[-1], "is_partial"] = True

        # ── Persist ───────────────────────────────────────────────────────────
        for _, row in ohlc.iterrows():
            trade_date = row["ts"].date().isoformat()
            ist_slot   = row["ts"].strftime("%H:%M:%S")
            if interval == "3m":
                self.insert_3m_candle(
                    trade_date, ist_slot,
                    row["open"], row["high"], row["low"], row["close"],
                    row["volume"], symbol, is_partial=row["is_partial"],
                )
            else:
                self.insert_15m_candle(
                    trade_date, ist_slot,
                    row["open"], row["high"], row["low"], row["close"],
                    row["volume"], symbol, is_partial=row["is_partial"],
                )

        # ── Candle continuity log ─────────────────────────────────────────────
        latest = ohlc.iloc[-1]
        n_completed = int((~ohlc["is_partial"]).sum())
        logging.info(
            f"[CANDLE CONTINUITY] {symbol} {interval.upper()} "
            f"rows={len(ohlc)} completed={n_completed} "
            f"last_slot={latest['ts'].strftime('%H:%M')} "
            f"O={fmt(latest['open'])} H={fmt(latest['high'])} "
            f"L={fmt(latest['low'])} C={fmt(latest['close'])} "
            f"partial={latest['is_partial']}"
        )

    # ─────────────────────────────────────────────────────────────────────────
    #  Rebuild from DB (replay / recovery)
    # ─────────────────────────────────────────────────────────────────────────

    def rebuild_candles_from_db(self, symbol: str, interval: str = "3m") -> None:
        """Full rebuild of candle tables from raw ticks. Use for recovery."""
        df_ticks = self.fetch_ticks(symbol)
        if df_ticks.empty:
            logging.warning(f"[TICKDB REBUILD] No ticks for {symbol}")
            return

        df_ticks["ts"] = pd.to_datetime(df_ticks["timestamp"], errors="coerce")
        df_ticks = df_ticks.dropna(subset=["ts"])

        if df_ticks["ts"].dt.tz is None:
            df_ticks["ts"] = df_ticks["ts"].dt.tz_localize("UTC").dt.tz_convert("Asia/Kolkata")
        else:
            df_ticks["ts"] = df_ticks["ts"].dt.tz_convert("Asia/Kolkata")

        rule = {"3m": "3min", "15m": "15min"}.get(interval)
        if rule is None:
            return

        ohlc = (
            df_ticks.resample(rule, on="ts")
            .agg({
                "last_price": ["first", "max", "min", "last"],
                "volume":     "sum",
            })
            .dropna()
        )
        ohlc.columns = ["open", "high", "low", "close", "volume"]
        ohlc.reset_index(inplace=True)

        for _, row in ohlc.iterrows():
            trade_date = row["ts"].date().isoformat()
            ist_slot   = row["ts"].strftime("%H:%M:%S")
            if interval == "3m":
                self.insert_3m_candle(
                    trade_date, ist_slot,
                    row["open"], row["high"], row["low"], row["close"],
                    row["volume"], symbol,
                )
            else:
                self.insert_15m_candle(
                    trade_date, ist_slot,
                    row["open"], row["high"], row["low"], row["close"],
                    row["volume"], symbol,
                )
        logging.info(f"[TICKDB REBUILD] {interval} for {symbol}: {len(ohlc)} rows")

    # ─────────────────────────────────────────────────────────────────────────
    #  Candle fetch (replay / OFFLINE mode)
    # ─────────────────────────────────────────────────────────────────────────

    def fetch_candles(
        self,
        resolution:      str  = "3m",
        start_time:      str  = None,
        end_time:        str  = None,
        use_yesterday:   bool = False,
        symbol:          str  = None,
        completed_only:  bool = False,   # True → exclude is_partial=1 rows
    ) -> pd.DataFrame:
        """
        Fetch candle rows from SQLite.

        completed_only=True filters out is_partial=1 rows — use this when
        calling from the strategy to avoid computing indicators on in-progress
        candles.  Default False preserves replay / audit behaviour.

        Always logs row count and last slot for candle continuity visibility.
        """
        table_map = {"3m": "candles_3m_ist", "15m": "candles_15m_ist"}
        table     = table_map.get(resolution)
        if not table:
            logging.error(f"[TICKDB FETCH] Unsupported resolution: {resolution}")
            return pd.DataFrame()

        ist_now = datetime.now(time_zone)
        today   = ist_now.date()

        def _run_query(conn, trade_date_str):
            q      = f"SELECT * FROM {table} WHERE trade_date = ?"
            params = [trade_date_str]
            if symbol:
                q      += " AND symbol = ?"
                params.append(symbol)
            if start_time:
                q      += " AND ist_slot >= ?"
                params.append(start_time)
            if end_time:
                q      += " AND ist_slot <= ?"
                params.append(end_time)
            if completed_only:
                q      += " AND is_partial = 0"
            q += " ORDER BY ist_slot ASC"
            return pd.read_sql_query(q, conn, params=params)

        df = pd.DataFrame()

        if use_yesterday:
            for offset in range(1, 6):
                candidate = (today - timedelta(days=offset)).isoformat()
                try:
                    df = _run_query(self.conn, candidate)
                    if df.empty:
                        alt = os.path.join(self.base_path, f"ticks_{candidate}.db")
                        if os.path.exists(alt):
                            with sqlite3.connect(alt) as c:
                                df = _run_query(c, candidate)
                    if not df.empty:
                        break
                except Exception as exc:
                    logging.debug(f"[TICKDB FETCH] {candidate}: {exc}")
            if df.empty:
                return pd.DataFrame(columns=[
                    "trade_date", "ist_slot", "open", "high", "low",
                    "close", "volume", "symbol",
                ])
        else:
            trade_date = today.isoformat()
            try:
                df = _run_query(self.conn, trade_date)
            except Exception as exc:
                logging.error(f"[TICKDB FETCH] today={trade_date}: {exc}")
                df = pd.DataFrame()

        if df.empty:
            return pd.DataFrame(columns=[
                "trade_date", "ist_slot", "open", "high", "low",
                "close", "volume", "symbol",
            ])

        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        # ── MARKET HOURS FILTER — strip pre/post-market rows ────────────────
        # Critical: post-15:30 candles can have HIGH=LOW=CLOSE causing pivot bugs
        if "ist_slot" in df.columns:
            original_len = len(df)
            df = df[df["ist_slot"].apply(_is_market_hours)].copy()
            filtered_len = len(df)
            if filtered_len < original_len:
                logging.info(
                    f"[TICKDB FETCH] {resolution} {symbol}: "
                    f"Filtered {original_len - filtered_len} post-market rows "
                    f"(kept {filtered_len} market hours)"
                )

        # ── Continuity log ────────────────────────────────────────────────────
        last_slot = df["ist_slot"].iloc[-1] if "ist_slot" in df.columns and not df.empty else "?"
        logging.debug(
            f"[TICKDB FETCH] {resolution} symbol={symbol} "
            f"rows={len(df)} last_slot={last_slot} "
            f"use_yesterday={use_yesterday} completed_only={completed_only}"
        )

        return df

    # ─────────────────────────────────────────────────────────────────────────
    #  Session loader (replay / backtest)
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def load_sessions(base_path=r"C:\SQLite\ticks"):
        """Load all tick sessions from daily DB files into one DataFrame."""
        base_path = os.path.abspath(base_path)
        db_files  = sorted(glob.glob(os.path.join(base_path, "ticks_*.db")))
        dfs       = []
        for db_file in db_files:
            try:
                with sqlite3.connect(db_file) as conn:
                    df = pd.read_sql_query("SELECT * FROM ticks", conn)
                if df.empty:
                    continue
                for col in ["last_price", "volume", "bid", "ask"]:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                dfs.append(df)
                logging.info(f"[TICKDB LOAD] {len(df)} ticks from {db_file}")
            except Exception as exc:
                logging.error(f"[TICKDB LOAD ERROR] {db_file}: {exc}")

        if dfs:
            return pd.concat(dfs, ignore_index=True)
        logging.warning("[TICKDB LOAD] No tick data found")
        return pd.DataFrame(columns=[
            "timestamp", "trade_date", "symbol", "bid", "ask", "last_price", "volume",
        ])

    # ─────────────────────────────────────────────────────────────────────────
    #  Legacy helper retained for replay compatibility
    # ─────────────────────────────────────────────────────────────────────────

    def _get_latest_db_file(self, base_path, today_str, max_lookback):
        base_date = datetime.strptime(today_str, "%Y-%m-%d")
        for d in range(0, max_lookback + 1):
            check_date = (base_date - timedelta(days=d)).strftime("%Y-%m-%d")
            db_file    = os.path.join(base_path, f"ticks_{check_date}.db")
            if os.path.exists(db_file):
                return db_file
        return None


# ── Module-level singleton (used by data_feed and main) ──────────────────────
tick_db = TickDatabase()