"""Broker adapter factory for the Supertrend Pullback strategy.

Reads BROKER from config.py and returns a configured BrokerAdapter instance
ready to be passed as ``broker_fn`` to ``place_st_pullback_entry /
place_st_pullback_exit``.

Usage
-----
    from broker_init import build_broker_adapter

    adapter = build_broker_adapter()          # uses config.BROKER
    # — or override for testing / CLI —
    adapter = build_broker_adapter("zerodha")

    # Route every order through the selected adapter
    place_st_pullback_entry(..., broker_fn=adapter)
    place_st_pullback_exit(...,  broker_fn=adapter)

Supported brokers
-----------------
"fyers"   → :class:`~st_pullback_cci.FyersAdapter`
             Requires ``fyers-apiv3`` (pip install fyers-apiv3).
             Credentials: FYERS_CLIENT_ID / FYERS_ACCESS_TOKEN in .env.

"zerodha" → :class:`~st_pullback_cci.ZerodhaKiteAdapter`
             Requires ``kiteconnect`` (pip install kiteconnect).
             Credentials: ZERODHA_API_KEY / ZERODHA_ACCESS_TOKEN in .env.

Adding a new broker
-------------------
1. Subclass :class:`~st_pullback_cci.BrokerAdapter` in st_pullback_cci.py.
2. Add a ``_build_<name>_adapter()`` function below.
3. Register the name in :data:`_ALLOWED_BROKERS` and the dispatch dict in
   :func:`build_broker_adapter`.
"""

from __future__ import annotations

import logging
from typing import Optional

from config import (
    BROKER,
    client_id,
    access_token,
    ZERODHA_API_KEY,
    ZERODHA_ACCESS_TOKEN,
)
from st_pullback_cci import BrokerAdapter, FyersAdapter, ZerodhaKiteAdapter

# All broker name strings that build_broker_adapter recognises.
_ALLOWED_BROKERS = {"fyers", "zerodha"}


def build_broker_adapter(
    broker_override: Optional[str] = None,
) -> BrokerAdapter:
    """Build and return the configured :class:`~st_pullback_cci.BrokerAdapter`.

    Parameters
    ----------
    broker_override:
        When provided, overrides :data:`config.BROKER`.
        Intended for tests and CLI one-off overrides; production code should
        rely on the config / environment variable.

    Returns
    -------
    BrokerAdapter
        A fully initialised adapter ready to place orders.

    Raises
    ------
    ValueError
        If the resolved broker name is not in :data:`_ALLOWED_BROKERS`.
    ImportError
        If the broker's SDK package is not installed.
    """
    selected = (broker_override if broker_override is not None else BROKER).lower().strip()

    logging.info(
        f"[BROKER CONFIG] build_broker_adapter selected={selected} "
        f"allowed={sorted(_ALLOWED_BROKERS)}"
    )

    _dispatch = {
        "fyers":   _build_fyers_adapter,
        "zerodha": _build_zerodha_adapter,
    }

    if selected not in _dispatch:
        raise ValueError(
            f"[BROKER CONFIG] Unrecognised broker={selected!r}. "
            f"Allowed values: {sorted(_ALLOWED_BROKERS)}"
        )

    return _dispatch[selected]()


# ---------------------------------------------------------------------------
# Fyers
# ---------------------------------------------------------------------------

def _build_fyers_adapter() -> FyersAdapter:
    """Construct an authenticated ``FyersModel`` and wrap it in a
    :class:`~st_pullback_cci.FyersAdapter`.

    The ``fyers_apiv3`` package is imported locally so that the rest of the
    engine does not depend on it being installed when Zerodha is selected.
    """
    try:
        from fyers_apiv3 import fyersModel  # type: ignore  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError(
            "[BROKER CONFIG] fyers_apiv3 is not installed. "
            "Run: pip install fyers-apiv3"
        ) from exc

    fyers_client = fyersModel.FyersModel(
        client_id=client_id,
        is_async=False,
        token=access_token,
        log_path="",
    )
    logging.info(
        f"[BROKER CONFIG] FyersAdapter initialised "
        f"client_id={str(client_id or '')[:6]}***"
    )
    return FyersAdapter(fyers_client)


# ---------------------------------------------------------------------------
# Zerodha Kite Connect
# ---------------------------------------------------------------------------

def _build_zerodha_adapter() -> ZerodhaKiteAdapter:
    """Construct an authenticated ``KiteConnect`` instance and wrap it in a
    :class:`~st_pullback_cci.ZerodhaKiteAdapter`.

    The ``kiteconnect`` package is imported locally so that the rest of the
    engine does not depend on it being installed when Fyers is selected.
    """
    try:
        from kiteconnect import KiteConnect  # type: ignore  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError(
            "[BROKER CONFIG] kiteconnect is not installed. "
            "Run: pip install kiteconnect"
        ) from exc

    kite = KiteConnect(api_key=ZERODHA_API_KEY)
    kite.set_access_token(ZERODHA_ACCESS_TOKEN)
    masked_key = (str(ZERODHA_API_KEY or "")[:4] + "***") if ZERODHA_API_KEY else "(unset)"
    logging.info(
        f"[BROKER CONFIG] ZerodhaKiteAdapter initialised api_key={masked_key}"
    )
    return ZerodhaKiteAdapter(kite)
