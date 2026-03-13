"""SQLite tick persistence — audit layer for ticks and candles.

Extracted from tickdb.py. This is NOT the live indicator source.
SQLite is used for: persisting raw ticks, replay/backtest mode.
"""

from __future__ import annotations

import glob
import logging
import os
import sqlite3
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytz

TIME_ZONE = pytz.timezone("Asia/Kolkata")
MARKET_OPEN = (9, 15)
MARKET_CLOSE = (15, 30)


def _fmt(val):
    return f"{val:.2f}" if val is not None and not pd.isna(val) else "NA"


def _is_market_hours(ts_str: str) -> bool:
    try:
        if ':' not in ts_str:
            return False
        parts = ts_str.split(':')
        h, m = int(parts[0]), int(parts[1])
        if h < MARKET_OPEN[0] or (h == MARKET_OPEN[0] and m < MARKET_OPEN[1]):
            return False
        if h > MARKET_CLOSE[0] or (h == MARKET_CLOSE[0] and m > MARKET_CLOSE[1]):
            return False
        return True
    except (ValueError, IndexError):
        return True


class TickDatabase:
    """SQLite-based tick and candle persistence."""

    def __init__(self, base_path=None, max_lookback=5):
        if base_path is None:
            base_path = os.environ.get("TICK_DB_PATH", r"C:\SQLite\ticks")
        base_path = os.path.abspath(base_path)
        os.makedirs(base_path, exist_ok=True)

        today_str = datetime.now().strftime("%Y-%m-%d")
        db_file = os.path.join(base_path, f"ticks_{today_str}.db")

        self.conn = sqlite3.connect(db_file, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self._candle_log_throttle = {}
        self._create_tables()

        self.base_path = base_path
        self.max_lookback = max_lookback

        logging.info(f"[TICKDB] Using database: {db_file}")

    def _create_tables(self):
        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME NOT NULL,
            trade_date DATE NOT NULL,
            symbol TEXT NOT NULL,
            bid REAL, ask REAL, last_price REAL, volume REAL
        )""")
        self.cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_symbol_time ON ticks(symbol, timestamp)"
        )
        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS candles_3m_ist (
            trade_date TEXT NOT NULL, ist_slot TEXT NOT NULL, symbol TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume REAL DEFAULT 0,
            is_partial INTEGER DEFAULT 0,
            PRIMARY KEY (trade_date, ist_slot, symbol)
        )""")
        self.cursor.execute("""
        CREATE TABLE IF NOT EXISTS candles_15m_ist (
            trade_date TEXT NOT NULL, ist_slot TEXT NOT NULL, symbol TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume REAL DEFAULT 0,
            is_partial INTEGER DEFAULT 0,
            PRIMARY KEY (trade_date, ist_slot, symbol)
        )""")
        self.conn.commit()

    def insert_tick(self, symbol, bid, ask, last_price, volume):
        ts = datetime.now(UTC).isoformat()
        ts_ist = datetime.now(TIME_ZONE)
        trade_date = ts_ist.strftime("%Y-%m-%d")
        try:
            px = float(last_price) if last_price is not None else None
            vol = float(volume) if volume is not None else 0.0
            self.cursor.execute("""
                INSERT INTO ticks (timestamp, trade_date, symbol, bid, ask, last_price, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (str(ts), str(trade_date), str(symbol),
                  float(bid) if bid is not None else None,
                  float(ask) if ask is not None else None,
                  px, vol))
            self.conn.commit()
        except Exception as exc:
            logging.error(f"[TICKDB INSERT ERROR] {symbol}: {exc}")

    def insert_3m_candle(self, trade_date, ist_slot, open_price, high_price,
                         low_price, close_price, volume, symbol, is_partial=False):
        try:
            self.cursor.execute("""
                INSERT OR REPLACE INTO candles_3m_ist
                    (trade_date, ist_slot, symbol, open, high, low, close, volume, is_partial)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (str(trade_date), str(ist_slot), str(symbol),
                  float(open_price) if open_price is not None else None,
                  float(high_price) if high_price is not None else None,
                  float(low_price) if low_price is not None else None,
                  float(close_price) if close_price is not None else None,
                  float(volume) if volume is not None else None,
                  int(is_partial)))
            self.conn.commit()
        except Exception as exc:
            logging.error(f"[TICKDB 3M INSERT ERROR] {symbol}: {exc}")

    def insert_15m_candle(self, trade_date, ist_slot, open_, high, low, close,
                          volume, symbol, is_partial=False):
        try:
            self.cursor.execute("""
                INSERT OR REPLACE INTO candles_15m_ist
                    (trade_date, ist_slot, symbol, open, high, low, close, volume, is_partial)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (str(trade_date), str(ist_slot), str(symbol),
                  float(open_) if open_ is not None else None,
                  float(high) if high is not None else None,
                  float(low) if low is not None else None,
                  float(close) if close is not None else None,
                  float(volume) if volume is not None else None,
                  int(is_partial)))
            self.conn.commit()
        except Exception as exc:
            logging.error(f"[TICKDB 15M INSERT ERROR] {symbol}: {exc}")

    def fetch_candles(self, resolution="3m", start_time=None, end_time=None,
                      use_yesterday=False, symbol=None, completed_only=False):
        table_map = {"3m": "candles_3m_ist", "15m": "candles_15m_ist"}
        table = table_map.get(resolution)
        if not table:
            return pd.DataFrame()

        ist_now = datetime.now(TIME_ZONE)
        today = ist_now.date()

        def _run_query(conn, trade_date_str):
            q = f"SELECT * FROM {table} WHERE trade_date = ?"
            params = [trade_date_str]
            if symbol:
                q += " AND symbol = ?"
                params.append(symbol)
            if start_time:
                q += " AND ist_slot >= ?"
                params.append(start_time)
            if end_time:
                q += " AND ist_slot <= ?"
                params.append(end_time)
            if completed_only:
                q += " AND is_partial = 0"
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
                except Exception:
                    pass
        else:
            trade_date = today.isoformat()
            try:
                df = _run_query(self.conn, trade_date)
            except Exception as exc:
                logging.error(f"[TICKDB FETCH] today={trade_date}: {exc}")

        if df.empty:
            return pd.DataFrame(columns=[
                "trade_date", "ist_slot", "open", "high", "low", "close", "volume", "symbol"
            ])

        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        if "ist_slot" in df.columns:
            df = df[df["ist_slot"].apply(_is_market_hours)].copy()

        return df

    def fetch_ticks(self, symbol, start_time=None, end_time=None):
        try:
            query = "SELECT timestamp AS time, last_price AS price, volume FROM ticks WHERE symbol=?"
            params = [symbol]
            if start_time:
                query += " AND timestamp >= ?"
                params.append(start_time)
            if end_time:
                query += " AND timestamp <= ?"
                params.append(end_time)
            df = pd.read_sql_query(query, self.conn, params=params)
            if df.empty:
                return pd.DataFrame(columns=["time", "price", "volume"])
            df["price"] = pd.to_numeric(df["price"], errors="coerce")
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
            return df
        except Exception as exc:
            logging.error(f"[TICKDB FETCH TICKS] {symbol}: {exc}")
            return pd.DataFrame(columns=["time", "price", "volume"])

    @staticmethod
    def load_sessions(base_path=None):
        if base_path is None:
            base_path = os.environ.get("TICK_DB_PATH", r"C:\SQLite\ticks")
        base_path = os.path.abspath(base_path)
        db_files = sorted(glob.glob(os.path.join(base_path, "ticks_*.db")))
        dfs = []
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
            except Exception as exc:
                logging.error(f"[TICKDB LOAD ERROR] {db_file}: {exc}")
        if dfs:
            return pd.concat(dfs, ignore_index=True)
        return pd.DataFrame(columns=["timestamp", "trade_date", "symbol", "bid", "ask", "last_price", "volume"])
