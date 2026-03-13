"""Broker API — unified interface for order placement and data fetching."""

from tbot_core.broker.base import BrokerAdapter, OrderRequest, OrderResponse, Position  # noqa: F401
from tbot_core.broker.fyers import FyersAdapter  # noqa: F401
from tbot_core.broker.zerodha import ZerodhaKiteAdapter  # noqa: F401
from tbot_core.broker.angel import AngelOneAdapter  # noqa: F401
from tbot_core.broker.ccxt_adapter import CcxtAdapter  # noqa: F401
from tbot_core.broker.paper import PaperBroker  # noqa: F401
from tbot_core.broker.factory import build_broker_adapter  # noqa: F401
