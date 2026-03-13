"""Position and order management — bridges to root modules.

Re-exports PositionManager, trade classes, and factories.
"""
from __future__ import annotations

import importlib as _il

_pm = _il.import_module("position_manager")

PositionManager = _pm.PositionManager
ExitDecision = _pm.ExitDecision
TradeLogger = _pm.TradeLogger
make_replay_pm = _pm.make_replay_pm
make_paper_pm = _pm.make_paper_pm

# Trade classes
_tc = _il.import_module("trade_classes")
ScalpTrade = _tc.ScalpTrade
TrendTrade = _tc.TrendTrade

__all__ = [
    "PositionManager", "ExitDecision", "TradeLogger",
    "make_replay_pm", "make_paper_pm",
    "ScalpTrade", "TrendTrade",
]
