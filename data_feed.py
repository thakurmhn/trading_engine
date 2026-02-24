# ============================================================
#  data_feed.py  — v2.0  (MarketData-aware)
# ============================================================
"""
CHANGES FROM v1:
  - Websocket tick callback now calls market_data.on_tick()
    instead of tick_db.build_candles_from_ticks().
  - Candle building is NO LONGER done here.
  - tick_db.insert_tick() still runs for audit/replay persistence.
  - spot_price is updated via market_data.on_tick() as a side-effect.
  - CandleAggregator (in-memory) is the live candle source.
  - SQLite is audit log only.

Why this matters:
  Previously, build_candles_from_ticks() was called on every tick,
  writing partial candles to SQLite, then get_live_candles() read those
  partial rows back and computed indicators on them. This caused:
    - Indicator computed on partial (in-progress) candle OHLCV
    - Flat post-market candles corrupting EMA/RSI/ST state
    - CCI returning NA on zero-range bars
  Now indicators are ONLY computed on completed candles from memory.
"""

import logging
from datetime import datetime

import pandas as pd
import pytz

from fyers_apiv3.FyersWebsocket import data_ws, order_ws
from setup import client_id, access_token, fyers, fyers_async, ticker, symbols
from setup import df, spot_price as _spot_price
from market_data import MarketData
from order_utils import update_order_status, map_status_code
from tickdb import TickDatabase

# ANSI COLORS
RESET   = "\033[0m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
RED     = "\033[91m"
GRAY    = "\033[90m"

IST = pytz.timezone("Asia/Kolkata")

# ── Singletons ────────────────────────────────────────────────────────────────
# tick_db: audit/replay persistence only
tick_db = TickDatabase()

# market_data: the single authoritative data source for strategy
# Populated by warmup() in main.py before market opens
market_data: MarketData = None   # assigned by main.py at startup

# Spot price mirror (kept for backward compat with existing strategy code)
spot_price = _spot_price


# ─────────────────────────────────────────────────────────────────────────────
#  WEBSOCKET: Market data callback
# ─────────────────────────────────────────────────────────────────────────────
INDEX_SYMBOLS = {"NSE:NIFTY50-INDEX", "NSE:BANKNIFTY-INDEX", "NSE:FINNIFTY-INDEX"}

def onmessage(ticks):
    global df, spot_price

    sym = ticks.get("symbol")
    if not sym:
        return

    # ── Option contracts → update quote df only ──────────────────────────────
    if sym not in INDEX_SYMBOLS:
        if sym not in df.index:
            df.loc[sym] = [None] * len(df.columns)
        for key, value in ticks.items():
            if key in df.columns:
                df.at[sym, key] = value
        return

    # ── Underlying index → feed MarketData + persist raw tick ────────────────
    ltp = ticks.get("ltp") or ticks.get("last_traded_price")
    vol = ticks.get("vol") or ticks.get("last_traded_qty", 0) or 0.0

    if ltp is None:
        return

    try:
        ltp = float(ltp)
        vol = float(vol)
    except (TypeError, ValueError):
        return

    # Update module-level spot (backward compat)
    spot_price = ltp

    # Persist raw tick to SQLite (audit / replay)
    bid = ticks.get("bid") or ticks.get("bid_price")
    ask = ticks.get("ask") or ticks.get("ask_price")
    try:
        tick_db.insert_tick(sym, bid, ask, ltp, vol)
    except Exception as e:
        logging.error(f"{RED}[DB TICK ERROR] {sym}: {e}{RESET}")

    # Feed in-memory CandleAggregator via MarketData
    # This is the ONLY place candle state is updated during live trading
    ts = datetime.now(IST)
    if market_data is not None:
        try:
            market_data.on_tick(sym, ltp, ts, vol)
        except Exception as e:
            logging.error(f"{RED}[MARKET_DATA TICK ERROR] {sym}: {e}{RESET}")
    else:
        logging.warning(
            f"[data_feed] market_data not initialized yet — "
            f"tick dropped for {sym} ltp={ltp}"
        )

    logging.debug(f"{GRAY}[TICK] {sym} ltp={ltp} vol={vol}{RESET}")


def onerror(message):  logging.error(f"[SOCKET ERROR] {message}")
def onclose(message):  logging.info(f"[SOCKET CLOSED] {message}")

def onopen():
    fyers_socket.subscribe(symbols=list(symbols), data_type="SymbolUpdate")
    fyers_socket.keep_running()
    logging.info(f"{GREEN}[SOCKET OPEN] Subscribed to {symbols}{RESET}")


# ── Data socket ───────────────────────────────────────────────────────────────
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


# ─────────────────────────────────────────────────────────────────────────────
#  Order chasing
# ─────────────────────────────────────────────────────────────────────────────
def chase_order(ord_df: pd.DataFrame) -> None:
    if ord_df.empty:
        return
    pending = ord_df[ord_df["status"] == 6]
    for _, o1 in pending.iterrows():
        name = o1["symbol"]
        current_price = df.loc[name, "ltp"] if name in df.index else None
        if current_price is None or pd.isna(current_price):
            logging.warning(f"[CHASE] No LTP for {name}, skipping")
            continue
        try:
            if o1["type"] == 1:  # Limit order
                id1       = o1["id"]
                lmt_price = o1["limitPrice"]
                qty       = o1["qty"]
                new_price = round(lmt_price + 0.1 if current_price > lmt_price else lmt_price - 0.1, 2)
                logging.info(f"[CHASE] {name}: {lmt_price} → {new_price} qty={qty}")
                fyers.modify_order(data={"id": id1, "type": 1, "limitPrice": new_price, "qty": qty})
        except Exception as e:
            logging.error(f"[CHASE ERROR] {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  Order socket
# ─────────────────────────────────────────────────────────────────────────────
def on_orders(message):
    logging.info(f"[ORDER UPDATE] {message}")
    try:
        orders       = message.get("orders", {})
        order_id     = orders.get("id")
        status_code  = orders.get("status")
        filled_qty   = orders.get("filledQty", 0)
        traded_price = orders.get("tradedPrice", 0)
        symbol       = orders.get("symbol")
        update_order_status(order_id, map_status_code(status_code),
                            filled_qty, traded_price, symbol)
    except Exception as e:
        logging.error(f"[ORDER UPDATE ERROR] {e}")

def on_order_error(msg): logging.error(f"[ORDER WS ERROR] {msg}")
def on_order_close(msg): logging.info(f"[ORDER WS CLOSED] {msg}")
def on_order_open():
    logging.info("[ORDER WS] Connected — subscribing OnOrders")
    fyers_order_socket.subscribe(data_type="OnOrders")
    fyers_order_socket.keep_running()

fyers_order_socket = order_ws.FyersOrderSocket(
    access_token=f"{client_id}:{access_token}",
    write_to_file=False,
    log_path="",
    on_connect=on_order_open,
    on_close=on_order_close,
    on_error=on_order_error,
    on_orders=on_orders,
)