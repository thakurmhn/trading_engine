"""Abstract BrokerAdapter — unified interface across all brokers.

Extracted from st_pullback_cci.py BrokerAdapter ABC and extended with
new methods for options selling, equity trading, and screener support.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, Tuple

import pandas as pd


@dataclass
class OrderRequest:
    """Standardised order request."""
    symbol: str
    side: str           # "BUY" or "SELL"
    qty: int
    order_type: str = "MARKET"   # "MARKET" | "LIMIT"
    product: str = "INTRADAY"    # "INTRADAY" | "CNC" | "MIS"
    limit_price: float = 0.0
    stop_price: float = 0.0
    tag: str = ""


@dataclass
class OrderResponse:
    """Standardised order response."""
    success: bool
    order_id: Optional[str] = None
    message: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class Position:
    """Standardised position representation."""
    symbol: str
    qty: int
    side: str           # "BUY" or "SELL"
    avg_price: float
    pnl: float = 0.0
    product: str = "INTRADAY"


class BrokerAdapter(ABC):
    """Abstract base class for broker-specific order placement.

    Concrete subclasses must implement at minimum:
    - place_entry() and place_exit() (original interface)

    Extended methods (get_historical_candles, get_option_chain, etc.)
    have default NotImplementedError implementations so existing
    adapters continue to work without changes.
    """

    # ── Core order interface (from original st_pullback_cci.py) ──────────────

    @abstractmethod
    def place_entry(
        self,
        symbol: str,
        qty: int,
        side_int: int,
        limit_price: float = 0.0,
        stop: float = 0.0,
        target: float = 0.0,
    ) -> Tuple[bool, Optional[str]]:
        """Place an entry order.

        side_int: 1 = BUY, -1 = SELL
        limit_price: 0.0 -> market order; >0 -> limit order.
        """

    @abstractmethod
    def place_exit(
        self,
        symbol: str,
        qty: int,
        reason: str,
    ) -> Tuple[bool, Optional[str]]:
        """Place a market exit (square-off) order."""

    # ── Extended interface (new for library) ──────────────────────────────────

    def authenticate(self, credentials: dict) -> bool:
        """Authenticate with broker. Override in subclass."""
        raise NotImplementedError

    def get_historical_candles(self, symbol: str, timeframe, from_dt, to_dt) -> pd.DataFrame:
        """Fetch historical OHLCV candles. Override in subclass."""
        raise NotImplementedError

    def get_option_chain(self, underlying: str, expiry) -> pd.DataFrame:
        """Fetch option chain. Override in subclass."""
        raise NotImplementedError

    def get_symbols(self, exchange: str, segment: str = "EQ") -> list:
        """List available symbols. Override in subclass."""
        raise NotImplementedError

    def get_ltp(self, symbols: list) -> dict:
        """Get last traded price for symbols. Override in subclass."""
        raise NotImplementedError

    def place_order(self, order: OrderRequest) -> OrderResponse:
        """Place a standardised order. Override in subclass."""
        raise NotImplementedError

    def modify_order(self, order_id: str, modifications: dict) -> OrderResponse:
        """Modify an existing order. Override in subclass."""
        raise NotImplementedError

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an existing order. Override in subclass."""
        raise NotImplementedError

    def get_positions(self) -> list:
        """Get current positions. Override in subclass."""
        raise NotImplementedError

    def get_orderbook(self) -> list:
        """Get order book. Override in subclass."""
        raise NotImplementedError

    def subscribe_ticks(self, symbols: list, callback) -> None:
        """Subscribe to tick data. Override in subclass."""
        raise NotImplementedError

    def unsubscribe_ticks(self, symbols: list) -> None:
        """Unsubscribe from tick data. Override in subclass."""
        raise NotImplementedError
