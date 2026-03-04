    # ============================================================
#  data_feed.py  — v2.1  (MarketData-aware, fully instrumented)
# ============================================================
"""
ARCHITECTURE
────────────
Tick flow (LIVE):
  WebSocket → onmessage() → market_data.on_tick()  ← IN-MEMORY aggregation
                          → tick_db.insert_tick()   ← SQLite audit only
                          → spot_price (module-level scalar, updated every tick)

Candle building:
  market_data.on_tick() → CandleAggregator (in-memory)  ← PRIMARY
  SQLite candles                                          ← AUDIT/REPLAY only

v2.1 fixes vs v2.0:
  - market_data reference initialised to None, wired by main.py after warmup
  - Explicit [TICK] log on every index tick with symbol/spot/time
  - [LIVE INIT] log emitted from onopen() so subscription is auditable
  - spot_price updated as module-level float (not the immutable _spot_price alias)
  - Defensive float coercion before insert_tick and on_tick calls
  - [TICK DROPPED] log when market_data not yet ready
  - Removed old candle_builder / slot-tracking logic entirely (was v1 dead code)
"""

import logging
import time
from datetime import datetime

import pandas as pd
import pytz

from fyers_apiv3.FyersWebsocket import data_ws, order_ws
from setup import client_id, access_token, fyers, fyers_async, ticker, symbols, df
from order_utils import update_order_status, map_status_code
from tickdb import TickDatabase
from pulse_module import get_pulse_module, PulseModule

# ── ANSI colours ─────────────────────────────────────────────────────────────
RESET  = "\033[0m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
GRAY   = "\033[90m"
CYAN   = "\033[96m"

IST = pytz.timezone("Asia/Kolkata")

# ── Singletons ────────────────────────────────────────────────────────────────
tick_db: TickDatabase = TickDatabase()          # audit / replay persistence

# market_data is wired by main.py after do_warmup() completes.
# Until then, ticks are persisted to SQLite but NOT fed to the aggregator.
market_data = None     # type: ignore[assignment]  (MarketData | None)

# Module-level spot price — updated on every index tick.
# Imported by execution.py as `from data_feed import spot_price`.
# NOTE: because Python ints/floats are immutable, callers must re-import
#       or use `import data_feed; data_feed.spot_price` for the live value.
spot_price: float = 0.0

# Index symbols we track as underlying
INDEX_SYMBOLS = {"NSE:NIFTY50-INDEX", "NSE:BANKNIFTY-INDEX", "NSE:FINNIFTY-INDEX"}

# ── Pulse Module (Tick-Rate Momentum) ────────────────────────────────────────
# Initialize the global pulse module for tick-rate momentum calculation
pulse: PulseModule = get_pulse_module()


# ─────────────────────────────────────────────────────────────────────────────
#  WEBSOCKET: Market data callback
# ─────────────────────────────────────────────────────────────────────────────

def onmessage(ticks: dict) -> None:
    """
    Called by the Fyers WebSocket on every market data update.

    For index symbols:
      1. Updates module-level `spot_price`
      2. Persists raw tick to SQLite (audit / replay)
      3. Feeds MarketData in-memory CandleAggregator (indicators source)

    For option contracts:
      - Updates the quote `df` DataFrame only (used by order chasing)
    """
    global spot_price

    sym = ticks.get("symbol")
    if not sym:
        return

    # ── Option contracts → quote df only ────────────────────────────────────
    if sym not in INDEX_SYMBOLS:
        if sym not in df.index:
            df.loc[sym] = [None] * len(df.columns)
        for key, value in ticks.items():
            if key in df.columns:
                df.at[sym, key] = value
        return

    # ── Underlying index ─────────────────────────────────────────────────────
    ltp = ticks.get("ltp") or ticks.get("last_traded_price")
    if ltp is None:
        return

    try:
        ltp = float(ltp)
    except (TypeError, ValueError):
        logging.warning(f"[TICK] Bad ltp value for {sym}: {ticks.get('ltp')!r}")
        return

    vol = 0.0
    try:
        vol = float(ticks.get("vol") or ticks.get("last_traded_qty") or 0)
    except (TypeError, ValueError):
        pass

    # 1. Update module-level spot price (checked every strategy loop iteration)
    spot_price = ltp

    # 2. Timestamp in IST
    ts = datetime.now(IST)

    # 3. Audit log — every tick (INFO level for production visibility)
    ts_str = ts.strftime('%Y-%m-%d %H:%M:%S')
    logging.info(
        f"[TICK] {sym} LTP={ltp:.2f} time={ts_str}"
    )

    # 4. Persist raw tick to SQLite
    bid = ticks.get("bid") or ticks.get("bid_price")
    ask = ticks.get("ask") or ticks.get("ask_price")
    try:
        tick_db.insert_tick(sym, bid, ask, ltp, vol)
    except Exception as exc:
        logging.error(f"{RED}[TICK DB ERROR] {sym}: {exc}{RESET}")

    # 5. Feed in-memory CandleAggregator via MarketData
    if market_data is not None:
        try:
            market_data.on_tick(sym, ltp, ts, vol)
        except Exception as exc:
            logging.error(f"{RED}[MARKET_DATA TICK ERROR] {sym}: {exc}{RESET}")
    else:
        # market_data is wired after warmup — early ticks (pre-09:15) are
        # persisted to SQLite and will be included in the warmup merge.
        logging.debug(
            f"[TICK DROPPED→MD] {sym} ltp={ltp:.2f} — market_data not yet wired"
        )

    # 6. Feed Pulse Module (Tick-Rate Momentum)
    try:
        timestamp_ms = time.time() * 1000  # Current time in milliseconds
        pulse.on_tick(timestamp_ms, ltp)
    except Exception as exc:
        logging.debug(f"[PULSE][TICK ERROR] {sym}: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
#  WEBSOCKET: Control callbacks
# ─────────────────────────────────────────────────────────────────────────────

def onerror(message):
    logging.error(f"{RED}[SOCKET ERROR] {message}{RESET}")


def onclose(message):
    logging.info(f"[SOCKET CLOSED] {message}")


def onopen():
    """
    Called when the Fyers data WebSocket connects.
    Subscribes to all configured symbols and emits the [LIVE INIT] audit log.
    """
    fyers_socket.subscribe(symbols=symbols, data_type="SymbolUpdate")
    fyers_socket.keep_running()
    logging.info(
        f"{GREEN}[LIVE INIT] mode=LIVE "
        f"symbols={symbols} "
        f"subscribed=SymbolUpdate "
        f"time={datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}{RESET}"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  DATA SOCKET
# ─────────────────────────────────────────────────────────────────────────────
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
#  ORDER CHASING
# ─────────────────────────────────────────────────────────────────────────────

def chase_order(ord_df: pd.DataFrame) -> None:
    """Adjust limit price of pending orders toward the current LTP."""
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
                id1        = o1["id"]
                lmt_price  = o1["limitPrice"]
                qty        = o1["qty"]
                new_price  = (
                    round(lmt_price + 0.1, 2)
                    if current_price > lmt_price
                    else round(lmt_price - 0.1, 2)
                )
                logging.info(
                    f"[CHASE] {name}: old={lmt_price} new={new_price} qty={qty}"
                )
                fyers.modify_order(data={
                    "id": id1, "type": 1,
                    "limitPrice": new_price, "qty": qty,
                })
        except Exception as exc:
            logging.error(f"[CHASE ERROR] {exc}")


# ─────────────────────────────────────────────────────────────────────────────
#  ORDER STATUS CALLBACKS
# ─────────────────────────────────────────────────────────────────────────────

def on_orders(message):
    logging.info(f"[ORDER UPDATE RAW] {message}")
    try:
        orders       = message.get("orders", {})
        order_id     = orders.get("id")
        status_code  = orders.get("status")
        filled_qty   = orders.get("filledQty", 0)
        traded_price = orders.get("tradedPrice", 0)
        symbol       = orders.get("symbol")
        status       = map_status_code(status_code)
        update_order_status(order_id, status, filled_qty, traded_price, symbol)
    except Exception as exc:
        logging.error(f"[ORDER UPDATE ERROR] {exc}")


def on_order_error(message):
    logging.error(f"[ORDER WS ERROR] {message}")


def on_order_close(message):
    logging.info(f"[ORDER WS CLOSED] {message}")


def on_order_open():
    logging.info("[ORDER WS CONNECTED] Subscribing to OnOrders...")
    fyers_order_socket.subscribe(data_type="OnOrders")
    fyers_order_socket.keep_running()


# ─────────────────────────────────────────────────────────────────────────────
#  ORDER SOCKET
# ─────────────────────────────────────────────────────────────────────────────
fyers_order_socket = order_ws.FyersOrderSocket(
    access_token=f"{client_id}:{access_token}",
    write_to_file=False,
    log_path="",
    on_connect=on_order_open,
    on_close=on_order_close,
    on_error=on_order_error,
    on_orders=on_orders,
)