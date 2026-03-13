"""Fyers API v3 adapter."""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from tbot_core.broker.base import BrokerAdapter


class FyersAdapter(BrokerAdapter):
    """Fyers API v3 adapter.

    Wraps fyers.place_order() into the BrokerAdapter interface.
    """

    def __init__(self, fyers_client) -> None:
        self._fyers = fyers_client

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
            logging.info(
                f"[FYERS ENTRY] symbol={symbol} side_int={side_int} "
                f"limit_price={limit_price} stop={stop} target={target}"
            )
            order_data = {
                "symbol": symbol,
                "qty": qty,
                "type": 1 if limit_price > 0 else 2,
                "side": side_int,
                "productType": "INTRADAY",
                "limitPrice": limit_price,
                "stopPrice": 0,
                "validity": "DAY",
                "offlineOrder": False,
                "disclosedQty": 0,
                "isSliceOrder": False,
                "orderTag": "ST_PULLBACK",
            }
            resp = self._fyers.place_order(data=order_data)
            if resp.get("s") == "ok":
                return True, resp.get("id")
            logging.error(f"[FYERS ENTRY FAILED] symbol={symbol} response={resp}")
            return False, None
        except Exception as exc:
            logging.error(f"[FYERS ENTRY ERROR] symbol={symbol} error={exc}")
            return False, None

    def place_exit(
        self,
        symbol: str,
        qty: int,
        reason: str,
    ) -> Tuple[bool, Optional[str]]:
        try:
            order_data = {
                "symbol": symbol,
                "qty": qty,
                "type": 2,
                "side": -1,
                "productType": "INTRADAY",
                "limitPrice": 0,
                "stopPrice": 0,
                "validity": "DAY",
                "offlineOrder": False,
                "disclosedQty": 0,
                "isSliceOrder": False,
                "orderTag": str(reason),
            }
            resp = self._fyers.place_order(data=order_data)
            if resp.get("s") == "ok":
                return True, resp.get("id")
            logging.error(f"[FYERS EXIT FAILED] symbol={symbol} response={resp}")
            return False, None
        except Exception as exc:
            logging.error(f"[FYERS EXIT ERROR] symbol={symbol} error={exc}")
            return False, None
