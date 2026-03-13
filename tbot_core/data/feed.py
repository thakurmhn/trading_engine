"""Broker-agnostic WebSocket tick streaming.

Provides a DataFeed class that wraps broker-specific WebSocket implementations.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable, Optional

import pytz

IST = pytz.timezone("Asia/Kolkata")


class DataFeed:
    """Broker-agnostic data feed manager.

    Wraps a broker's WebSocket tick feed and routes ticks to callbacks.

    Usage:
        feed = DataFeed(on_tick=my_callback)
        feed.start(broker_socket)  # broker-specific socket
    """

    def __init__(self, on_tick: Optional[Callable] = None):
        self._on_tick = on_tick
        self._spot: dict = {}
        self._running = False

    @property
    def spot_price(self) -> dict:
        """Current spot prices by symbol."""
        return self._spot.copy()

    def on_message(self, ticks: dict) -> None:
        """Process incoming tick message."""
        sym = ticks.get("symbol")
        if not sym:
            return

        ltp = ticks.get("ltp") or ticks.get("last_traded_price")
        if ltp is None:
            return

        try:
            ltp = float(ltp)
        except (TypeError, ValueError):
            return

        self._spot[sym] = ltp

        vol = 0.0
        try:
            vol = float(ticks.get("vol") or ticks.get("last_traded_qty") or 0)
        except (TypeError, ValueError):
            pass

        ts = datetime.now(IST)

        if self._on_tick:
            try:
                self._on_tick(sym, ltp, ts, vol)
            except Exception as exc:
                logging.error(f"[DATA FEED] tick callback error: {exc}")

    def start(self, socket) -> None:
        """Start the data feed (broker-specific socket)."""
        self._running = True
        logging.info("[DATA FEED] Started")

    def stop(self) -> None:
        """Stop the data feed."""
        self._running = False
        logging.info("[DATA FEED] Stopped")
