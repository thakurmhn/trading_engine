"""Zerodha Kite Connect v3 adapter."""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from tbot_core.broker.base import BrokerAdapter


class ZerodhaKiteAdapter(BrokerAdapter):
    """Zerodha Kite Connect v3 adapter.

    Requires kiteconnect package: pip install kiteconnect
    """

    def __init__(self, kite_client) -> None:
        self._kite = kite_client

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
                f"[KITE ENTRY] symbol={symbol} side={txn} "
                f"limit_price={limit_price} stop={stop} target={target}"
            )
            order_id = self._kite.place_order(
                variety="regular",
                exchange="NFO",
                tradingsymbol=symbol,
                transaction_type=txn,
                quantity=qty,
                product="MIS",
                order_type="LIMIT" if limit_price > 0 else "MARKET",
                price=limit_price if limit_price > 0 else None,
                tag="ST_PULLBACK",
            )
            return True, str(order_id)
        except Exception as exc:
            logging.error(f"[KITE ENTRY ERROR] symbol={symbol} error={exc}")
            return False, None

    def place_exit(
        self,
        symbol: str,
        qty: int,
        reason: str,
    ) -> Tuple[bool, Optional[str]]:
        try:
            order_id = self._kite.place_order(
                variety="regular",
                exchange="NFO",
                tradingsymbol=symbol,
                transaction_type="SELL",
                quantity=qty,
                product="MIS",
                order_type="MARKET",
                tag=f"EXIT_{reason[:18]}",
            )
            return True, str(order_id)
        except Exception as exc:
            logging.error(f"[KITE EXIT ERROR] symbol={symbol} error={exc}")
            return False, None
