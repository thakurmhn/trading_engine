"""Exit management — bridges to root option_exit_manager.py.

Re-exports HFT exit engine classes.
"""
from __future__ import annotations

import importlib as _il

_oem = _il.import_module("option_exit_manager")

OptionExitManager = _oem.OptionExitManager
OptionExitConfig = _oem.OptionExitConfig

__all__ = ["OptionExitManager", "OptionExitConfig"]
