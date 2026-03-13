"""Angel One Smart API adapter."""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from tbot_core.broker.base import BrokerAdapter


class AngelOneAdapter(BrokerAdapter):
    """Angel One Smart API adapter.

    Requires smartapi-python package: pip install smartapi-python
    """

    def __init__(self, smart_connect_client) -> None:
        self._client = smart_connect_client

    def place_entry(
        self,
        symbol: str,
        qty: int,
        side_int: int,
        limit_price: float = 0.0,
        stop: float = 0.0,
        target: float = 0.0,
    ) -> Tuple[bool, Optional[str]]:
        try:
            txn = "BUY" if side_int == 1 else "SELL"
            logging.info(
                f"[ANGEL ENTRY] symbol={symbol} side={txn} "
                f"limit_price={limit_price} stop={stop} target={target}"
            )
            params = {
                "variety": "NORMAL",
                "tradingsymbol": symbol,
                "symboltoken": "",
                "transactiontype": txn,
                "exchange": "NFO",
                "ordertype": "LIMIT" if limit_price > 0 else "MARKET",
                "producttype": "INTRADAY",
                "duration": "DAY",
                "price": str(limit_price) if limit_price > 0 else "0",
                "squareoff": "0",
                "stoploss": "0",
                "quantity": str(qty),
                "ordertag": "ST_PULLBACK",
            }
            resp = self._client.placeOrder(params)
            if resp and resp.get("status"):
                return True, resp.get("data", {}).get("orderid")
            logging.error(f"[ANGEL ENTRY FAILED] symbol={symbol} response={resp}")
            return False, None
        except Exception as exc:
            logging.error(f"[ANGEL ENTRY ERROR] symbol={symbol} error={exc}")
            return False, None

    def place_exit(
        self,
        symbol: str,
        qty: int,
        reason: str,
    ) -> Tuple[bool, Optional[str]]:
        try:
            params = {
                "variety": "NORMAL",
                "tradingsymbol": symbol,
                "symboltoken": "",
                "transactiontype": "SELL",
                "exchange": "NFO",
                "ordertype": "MARKET",
                "producttype": "INTRADAY",
                "duration": "DAY",
                "price": "0",
                "squareoff": "0",
                "stoploss": "0",
                "quantity": str(qty),
                "ordertag": f"EXIT_{reason[:18]}",
            }
            resp = self._client.placeOrder(params)
            if resp and resp.get("status"):
                return True, resp.get("data", {}).get("orderid")
            logging.error(f"[ANGEL EXIT FAILED] symbol={symbol} response={resp}")
            return False, None
        except Exception as exc:
            logging.error(f"[ANGEL EXIT ERROR] symbol={symbol} error={exc}")
            return False, None
