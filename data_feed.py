# ===== data_feed.py =====
import logging
import pandas as pd
import pytz

from fyers_apiv3.FyersWebsocket import data_ws, order_ws
from setup import client_id, access_token, fyers, fyers_async, ticker, symbols, df
from setup import spot_price as _spot_price
from candle_builder import build_3min_candle
from order_utils import update_order_status, map_status_code
from tickdb import TickDatabase   # dedicated DB helper
from order_utils import update_order_status, map_status_code
from datetime import datetime


# ANSI COLORS
RESET   = "\033[0m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
RED     = "\033[91m"
MAGENTA = "\033[95m"
GRAY    = "\033[90m"
CYAN    = "\033[96m"

# Keep a module-level spot reference
spot_price = _spot_price

# Create a global TickDatabase instance
tick_db = TickDatabase()

# ─────────────────────────────────────────────────────────────────────────────
# TICK AGGREGATOR — in-memory OHLCV candle builder from live WebSocket ticks
# ─────────────────────────────────────────────────────────────────────────────

class TickAggregator:
    """
    Builds OHLCV candles from live ticks entirely in memory.

    Design:
        - One open candle per (symbol, interval) at a time.
        - When a new slot boundary is detected, the open candle is finalized
          and appended to the completed list.
        - Completed candles are available as DataFrames via get_candles().
        - SQLite write happens on finalization (backup only — not on hot path).

    Why in-memory and not SQLite:
        SQLite read/write adds latency and can produce inconsistent candles
        when ticks arrive out-of-order or the bot starts mid-session.
        Memory is instantaneous and always consistent for the current session.
        SQLite is written as an audit trail / replay source only.

    Usage:
        agg = TickAggregator()
        agg.add_tick("NSE:NIFTY50-INDEX", ltp=25600.0, ts=datetime.now(ist), vol=100)
        df_3m  = agg.get_candles("NSE:NIFTY50-INDEX", interval="3m")
        df_15m = agg.get_candles("NSE:NIFTY50-INDEX", interval="15m")
    """

    INTERVALS = {
        "3m":  3,
        "15m": 15,
    }

    def __init__(self, tick_db_ref=None):
        # open_candle[symbol][interval] = {open, high, low, close, volume, slot}
        self._open:      dict = {}
        # completed[symbol][interval] = list of OHLCV dicts
        self._completed: dict = {}
        # last tick timestamp per symbol — for staleness detection
        self._last_tick: dict = {}
        self._tick_db    = tick_db_ref   # optional SQLite backup
        self._ist        = pytz.timezone("Asia/Kolkata")

    def _slot(self, ts: datetime, minutes: int) -> datetime:
        """Round timestamp DOWN to nearest slot boundary."""
        ts = ts.replace(second=0, microsecond=0)
        slot_min = (ts.minute // minutes) * minutes
        return ts.replace(minute=slot_min)

    def add_tick(self, symbol: str, ltp: float, ts: datetime, vol: float = 0.0):
        """
        Process one incoming tick. Call from onmessage() for every LTP update.

        Args:
            symbol: e.g. "NSE:NIFTY50-INDEX"
            ltp:    last traded price
            ts:     tick timestamp (IST-aware datetime)
            vol:    tick volume (optional, 0 if not available)
        """
        if ltp is None or pd.isna(ltp):
            return

        self._last_tick[symbol] = ts

        if symbol not in self._open:
            self._open[symbol]      = {}
            self._completed[symbol] = {}

        for interval, mins in self.INTERVALS.items():
            current_slot = self._slot(ts, mins)

            if interval not in self._open[symbol]:
                # First tick — open the first candle
                self._open[symbol][interval] = {
                    "slot":   current_slot,
                    "open":   ltp,
                    "high":   ltp,
                    "low":    ltp,
                    "close":  ltp,
                    "volume": vol,
                }
                if interval not in self._completed[symbol]:
                    self._completed[symbol][interval] = []
                continue

            open_candle = self._open[symbol][interval]

            if current_slot != open_candle["slot"]:
                # Slot boundary crossed — finalize the completed candle
                self._finalize(symbol, interval, open_candle)
                # Open new candle for this slot
                self._open[symbol][interval] = {
                    "slot":   current_slot,
                    "open":   ltp,
                    "high":   ltp,
                    "low":    ltp,
                    "close":  ltp,
                    "volume": vol,
                }
            else:
                # Same slot — update running candle
                open_candle["high"]   = max(open_candle["high"], ltp)
                open_candle["low"]    = min(open_candle["low"],  ltp)
                open_candle["close"]  = ltp
                open_candle["volume"] += vol

    def _finalize(self, symbol: str, interval: str, candle: dict):
        """Finalize a completed candle — store in memory and write to SQLite backup."""
        slot      = candle["slot"]
        trade_date = slot.strftime("%Y-%m-%d")
        ist_slot   = slot.strftime("%H:%M:%S")

        row = {
            "trade_date": trade_date,
            "ist_slot":   ist_slot,
            "symbol":     symbol,
            "time":       f"{trade_date} {ist_slot}",
            "date":       slot.replace(tzinfo=self._ist) if slot.tzinfo is None
                          else slot,
            "open":  candle["open"],
            "high":  candle["high"],
            "low":   candle["low"],
            "close": candle["close"],
            "volume": candle["volume"],
        }

        self._completed[symbol][interval].append(row)

        logging.info(
            f"[TICK AGG] Finalized {interval} {symbol} "
            f"{trade_date} {ist_slot} "
            f"O={candle['open']:.2f} H={candle['high']:.2f} "
            f"L={candle['low']:.2f}  C={candle['close']:.2f} "
            f"V={candle['volume']:.0f}"
        )

        # SQLite backup (non-blocking — write happens, errors logged not raised)
        if self._tick_db is not None:
            try:
                if interval == "3m":
                    self._tick_db.insert_3m_candle(
                        trade_date, ist_slot,
                        candle["open"], candle["high"], candle["low"],
                        candle["close"], candle["volume"], symbol,
                        is_partial=False
                    )
                else:
                    self._tick_db.insert_15m_candle(
                        trade_date, ist_slot,
                        candle["open"], candle["high"], candle["low"],
                        candle["close"], candle["volume"], symbol,
                        is_partial=False
                    )
            except Exception as e:
                logging.warning(f"[TICK AGG] SQLite backup failed ({interval}): {e}")

    def get_candles(self, symbol: str, interval: str) -> pd.DataFrame:
        """
        Return all completed intraday candles for symbol/interval as a DataFrame.
        Does NOT include the current open (partial) candle.
        Format matches Fyers history DataFrame exactly (same columns/dtypes).
        """
        if symbol not in self._completed:
            return pd.DataFrame()
        rows = self._completed[symbol].get(interval, [])
        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["date"] = pd.to_datetime(df["date"], utc=False)
        if df["date"].dt.tz is None:
            df["date"] = df["date"].dt.tz_localize("Asia/Kolkata")
        return df.sort_values("date").reset_index(drop=True)

    def last_tick_age_seconds(self, symbol: str) -> float:
        """
        Seconds since the last tick was received for this symbol.
        Returns inf if no tick ever received (bot just started).
        Used for staleness detection.
        """
        if symbol not in self._last_tick:
            return float("inf")
        now = datetime.now(pytz.timezone("Asia/Kolkata"))
        last = self._last_tick[symbol]
        if last.tzinfo is None:
            last = pytz.timezone("Asia/Kolkata").localize(last)
        return (now - last).total_seconds()

    def candle_count(self, symbol: str, interval: str) -> int:
        """Number of completed candles for this symbol/interval."""
        if symbol not in self._completed:
            return 0
        return len(self._completed[symbol].get(interval, []))


# Global singleton — shared between data_feed and main
tick_aggregator = TickAggregator(tick_db_ref=tick_db)


# ===== Market data callbacks =====
import pytz
from datetime import datetime

# Track last finalized slots per symbol
last_slot_3m = {}
last_slot_15m = {}

def get_slot(ts, interval_minutes=3):
    """Round timestamp down to nearest slot boundary."""
    ts = ts.replace(second=0, microsecond=0)
    slot_minute = (ts.minute // interval_minutes) * interval_minutes
    return ts.replace(minute=slot_minute)

def onmessage(ticks):
    global df, spot_price

    if not ticks.get("symbol"):
        return

    symbol = ticks["symbol"]

    # ===== Option contracts → update df only =====
    if symbol not in ["NSE:NIFTY50-INDEX", "NSE:BANKNIFTY-INDEX", "NSE:FINNIFTY-INDEX"]:
        if symbol not in df.index:
            df.loc[symbol] = [None] * len(df.columns)
        for key, value in ticks.items():
            if key in df.columns:
                df.at[symbol, key] = value
        return

    # ===== Underlying indices → persistence + candle building =====
    ltp = ticks.get("ltp") or ticks.get("last_traded_price")
    bid = ticks.get("bid") or ticks.get("bid_price")
    ask = ticks.get("ask") or ticks.get("ask_price")
    vol = ticks.get("vol") or ticks.get("last_traded_qty", 0)

    if ltp is not None:
        try:
            tick_db.insert_tick(symbol, bid, ask, ltp, vol)
            logging.info(f"{GRAY}[TICK SAVED] {symbol} LTP={ltp} VOL={vol}{RESET}")
        except Exception as e:
            logging.error(f"{RED}[DB ERROR] Failed to insert tick: {e}{RESET}")

        spot_price = ltp

        # Current IST timestamp
        ts = datetime.now(pytz.timezone("Asia/Kolkata"))

        # ── PRIMARY: In-memory tick aggregator (consistent, no SQLite latency) ──
        # Feeds tick_aggregator which builds OHLCV candles in memory.
        # SQLite backup is written inside tick_aggregator._finalize() automatically.
        try:
            tick_aggregator.add_tick(symbol, ltp=ltp, ts=ts, vol=vol or 0.0)
        except Exception as e:
            logging.error(f"{RED}[TICK AGG ERROR] {symbol}: {e}{RESET}")

        # ── LEGACY SQLite candle builder kept for DB backup compatibility ────────
        # These build_candles_from_ticks() calls still run so the SQLite DB
        # stays current for replay/audit. They are NOT the primary candle source.
        current_slot_3m = get_slot(ts, 3)
        if last_slot_3m.get(symbol) != current_slot_3m:
            try:
                tick_db.build_candles_from_ticks(symbol, interval="3m")
                last_slot_3m[symbol] = current_slot_3m
                logging.debug(f"[CANDLE BACKUP] 3m backup written for {symbol} at {current_slot_3m}")
            except Exception as e:
                logging.error(f"{RED}[CANDLE BACKUP ERROR] 3m {symbol}: {e}{RESET}")

        current_slot_15m = get_slot(ts, 15)
        if last_slot_15m.get(symbol) != current_slot_15m:
            try:
                tick_db.build_candles_from_ticks(symbol, interval="15m")
                last_slot_15m[symbol] = current_slot_15m
                logging.debug(f"[CANDLE BACKUP] 15m backup written for {symbol} at {current_slot_15m}")
            except Exception as e:
                logging.error(f"{RED}[CANDLE BACKUP ERROR] 15m {symbol}: {e}{RESET}")

                

def onerror(message): logging.error(f"[SOCKET ERROR] {message}")
def onclose(message): logging.info(f"[SOCKET CLOSED] {message}")

def onopen():
    fyers_socket.subscribe(symbols=symbols, data_type="SymbolUpdate")
    fyers_socket.keep_running()
    logging.info("[SOCKET OPEN] Market data subscription started")

# ===== Data socket =====
fyers_socket = data_ws.FyersDataSocket(
    access_token=f"{client_id}:{access_token}",
    log_path=None,
    litemode=False,
    write_to_file=False,
    reconnect=True,
    on_connect=onopen,
    on_close=onclose,
    on_error=onerror,
    on_message=onmessage,
)

# ===== Order chasing =====
def chase_order(ord_df):
    if not ord_df.empty:
        ord_df = ord_df[ord_df["status"] == 6]  # pending orders
        for _, o1 in ord_df.iterrows():
            name = o1["symbol"]
            current_price = df.loc[name, "ltp"] if name in df.index else None
            if current_price is None or pd.isna(current_price):
                logging.warning(f"[CHASE] No LTP for {name}, skipping")
                continue
            try:
                if o1["type"] == 1:  # Limit order
                    id1 = o1["id"]
                    lmt_price = o1["limitPrice"]
                    qty = o1["qty"]
                    new_lmt_price = round(lmt_price + 0.1, 2) if current_price > lmt_price else round(lmt_price - 0.1, 2)
                    logging.info(f"[CHASE] {name}: old={lmt_price}, new={new_lmt_price}, qty={qty}")
                    data = {"id": id1, "type": 1, "limitPrice": new_lmt_price, "qty": qty}
                    response = fyers.modify_order(data=data)
                    logging.info(f"[CHASE RESPONSE] {response}")
            except Exception as e:
                logging.error(f"[CHASE ERROR] {e}")

# ===== Order status callbacks =====
def on_orders(message):
    logging.info(f"[ORDER UPDATE RAW] {message}")
    try:
        orders = message.get("orders", {})
        order_id = orders.get("id")
        status_code = orders.get("status")
        filled_qty = orders.get("filledQty", 0)
        traded_price = orders.get("tradedPrice", 0)
        symbol = orders.get("symbol")

        status = map_status_code(status_code)
        update_order_status(order_id, status, filled_qty, traded_price, symbol)

    except Exception as e:
        logging.error(f"[ORDER UPDATE ERROR] {e}")

def on_order_error(message): logging.error(f"[ORDER WS ERROR] {message}")
def on_order_close(message): logging.info(f"[ORDER WS CLOSED] {message}")

def on_order_open():
    logging.info("[ORDER WS CONNECTED] Subscribing to OnOrders...")
    fyers_order_socket.subscribe(data_type="OnOrders")
    fyers_order_socket.keep_running()

# ===== Order socket =====
fyers_order_socket = order_ws.FyersOrderSocket(
    access_token=f"{client_id}:{access_token}",
    write_to_file=False,
    log_path="",
    on_connect=on_order_open,
    on_close=on_order_close,
    on_error=on_order_error,
    on_orders=on_orders,
)