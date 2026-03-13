"""Broker adapter factory — build the configured adapter by name.

Usage:
    from tbot_core.broker import build_broker_adapter
    adapter = build_broker_adapter("fyers", credentials={...})
"""

from __future__ import annotations

import logging
from typing import Optional

from tbot_core.broker.base import BrokerAdapter


_ALLOWED_BROKERS = {"fyers", "zerodha", "angel", "ccxt", "paper"}


def build_broker_adapter(
    broker_name: str = "fyers",
    credentials: Optional[dict] = None,
    **kwargs,
) -> BrokerAdapter:
    """Build and return the configured BrokerAdapter.

    Parameters
    ----------
    broker_name : str
        One of: "fyers", "zerodha", "angel", "ccxt", "paper".
    credentials : dict, optional
        Broker-specific credentials (client_id, access_token, etc.).
    **kwargs
        Additional arguments passed to the adapter constructor.
    """
    creds = credentials or {}
    selected = broker_name.lower().strip()

    logging.info(
        f"[BROKER CONFIG] build_broker_adapter selected={selected} "
        f"allowed={sorted(_ALLOWED_BROKERS)}"
    )

    if selected == "paper":
        from tbot_core.broker.paper import PaperBroker
        return PaperBroker(slippage_pts=kwargs.get("slippage_pts", 0.0))

    if selected == "fyers":
        return _build_fyers(creds)
    if selected == "zerodha":
        return _build_zerodha(creds)
    if selected == "angel":
        return _build_angel(creds)
    if selected == "ccxt":
        return _build_ccxt(creds, **kwargs)

    raise ValueError(
        f"[BROKER CONFIG] Unrecognised broker={selected!r}. "
        f"Allowed: {sorted(_ALLOWED_BROKERS)}"
    )


def _build_fyers(creds: dict) -> BrokerAdapter:
    try:
        from fyers_apiv3 import fyersModel
    except ImportError as exc:
        raise ImportError(
            "[BROKER] fyers_apiv3 not installed. Run: pip install fyers-apiv3"
        ) from exc

    from tbot_core.broker.fyers import FyersAdapter

    fyers_client = fyersModel.FyersModel(
        client_id=creds.get("client_id", ""),
        is_async=False,
        token=creds.get("access_token", ""),
        log_path="",
    )
    return FyersAdapter(fyers_client)


def _build_zerodha(creds: dict) -> BrokerAdapter:
    try:
        from kiteconnect import KiteConnect
    except ImportError as exc:
        raise ImportError(
            "[BROKER] kiteconnect not installed. Run: pip install kiteconnect"
        ) from exc

    from tbot_core.broker.zerodha import ZerodhaKiteAdapter

    kite = KiteConnect(api_key=creds.get("api_key", ""))
    kite.set_access_token(creds.get("access_token", ""))
    return ZerodhaKiteAdapter(kite)


def _build_angel(creds: dict) -> BrokerAdapter:
    try:
        from SmartApi import SmartConnect
    except ImportError as exc:
        raise ImportError(
            "[BROKER] smartapi-python not installed. Run: pip install smartapi-python"
        ) from exc

    from tbot_core.broker.angel import AngelOneAdapter

    client = SmartConnect(api_key=creds.get("api_key", ""))
    return AngelOneAdapter(client)


def _build_ccxt(creds: dict, **kwargs) -> BrokerAdapter:
    try:
        import ccxt
    except ImportError as exc:
        raise ImportError(
            "[BROKER] ccxt not installed. Run: pip install ccxt"
        ) from exc

    from tbot_core.broker.ccxt_adapter import CcxtAdapter

    exchange_name = creds.get("exchange", "binance")
    exchange_cls = getattr(ccxt, exchange_name)
    exchange = exchange_cls({
        "apiKey": creds.get("api_key", ""),
        "secret": creds.get("secret", ""),
        **creds.get("options", {}),
    })
    return CcxtAdapter(exchange, quote_currency=kwargs.get("quote_currency", "USDT"))
