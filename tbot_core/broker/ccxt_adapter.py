"""CCXT adapter for crypto exchanges (Binance, Bybit, OKX, etc.)."""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from tbot_core.broker.base import BrokerAdapter


class CcxtAdapter(BrokerAdapter):
    """Generic ccxt adapter for crypto exchanges.

    Requires ccxt package: pip install ccxt
    """

    def __init__(self, exchange, quote_currency: str = "USDT") -> None:
        self._exchange = exchange
        self._quote = quote_currency

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
            ccxt_side = "buy" if side_int == 1 else "sell"
            order_type = "limit" if limit_price > 0 else "market"
            price = limit_price if limit_price > 0 else None
            logging.info(
                f"[CCXT ENTRY] symbol={symbol} side={ccxt_side} "
                f"limit_price={limit_price} stop={stop} target={target}"
            )
            order = self._exchange.create_order(
                symbol=symbol,
                type=order_type,
                side=ccxt_side,
                amount=qty,
                price=price,
                params={"clientOrderId": "ST_PULLBACK"},
            )
            return True, order.get("id")
        except Exception as exc:
            logging.error(f"[CCXT ENTRY ERROR] symbol={symbol} error={exc}")
            return False, None

    def place_exit(
        self,
        symbol: str,
        qty: int,
        reason: str,
    ) -> Tuple[bool, Optional[str]]:
        try:
            order = self._exchange.create_order(
                symbol=symbol,
                type="market",
                side="sell",
                amount=qty,
                params={"clientOrderId": f"EXIT_{reason[:18]}"},
            )
            return True, order.get("id")
        except Exception as exc:
            logging.error(f"[CCXT EXIT ERROR] symbol={symbol} error={exc}")
            return False, None
