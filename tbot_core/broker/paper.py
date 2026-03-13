"""Paper broker — simulated fills for backtesting and paper trading."""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from tbot_core.broker.base import BrokerAdapter


class PaperBroker(BrokerAdapter):
    """Simulated broker for PAPER and REPLAY modes.

    Always succeeds with synthetic order IDs. Optionally models slippage.
    """

    def __init__(self, slippage_pts: float = 0.0) -> None:
        self._slippage = slippage_pts
        self._order_counter = 0

    def _next_id(self) -> str:
        self._order_counter += 1
        return f"PAPER-{self._order_counter:06d}"

    def place_entry(
        self,
        symbol: str,
        qty: int,
        side_int: int,
        limit_price: float = 0.0,
        stop: float = 0.0,
        target: float = 0.0,
    ) -> Tuple[bool, Optional[str]]:
        oid = self._next_id()
        logging.info(
            f"[PAPER ENTRY] {oid} symbol={symbol} side_int={side_int} "
            f"qty={qty} limit={limit_price} slippage={self._slippage}"
        )
        return True, oid

    def place_exit(
        self,
        symbol: str,
        qty: int,
        reason: str,
    ) -> Tuple[bool, Optional[str]]:
        oid = self._next_id()
        logging.info(
            f"[PAPER EXIT] {oid} symbol={symbol} qty={qty} reason={reason}"
        )
        return True, oid
