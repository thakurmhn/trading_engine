"""Unit tests for restart persistence and startup suppression guards."""

from __future__ import annotations

import ast
import unittest
from datetime import datetime, timedelta

import pandas as pd


class DummyLogger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def info(self, msg: str) -> None:
        self.messages.append(str(msg))

    def warning(self, msg: str) -> None:
        self.messages.append(str(msg))

    def error(self, msg: str) -> None:
        self.messages.append(str(msg))

    def debug(self, msg: str) -> None:
        self.messages.append(str(msg))


def _load_functions(*names: str, extra_ns: dict | None = None):
    with open("execution.py", "r", encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    found = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            found[node.name] = node
    for name in names:
        assert name in found, f"{name} not found"

    logger = DummyLogger()
    ns = {
        "pd": pd,
        "dt": datetime,
        "datetime": datetime,
        "timedelta": timedelta,
        "time_zone": None,
        "logging": logger,
        "STARTUP_SUPPRESSION_MINUTES": 5,
        "_load_restart_state": lambda _acct: {},
        "_save_restart_state": lambda *_args, **_kwargs: None,
    }
    if extra_ns:
        ns.update(extra_ns)
    mod = ast.Module(body=[found[n] for n in names], type_ignores=[])
    exec(compile(mod, filename="<restart_guard_tests>", mode="exec"), ns)
    return {n: ns[n] for n in names}, logger


class RestartStateGuardTests(unittest.TestCase):
    def test_startup_suppression_blocks_entries(self):
        funcs, logger = _load_functions("_parse_ts", "_is_startup_suppression_active")
        is_blocked = funcs["_is_startup_suppression_active"]
        now = datetime(2026, 2, 26, 14, 30, 0)
        info = {"startup_suppression_until": now + timedelta(minutes=3)}
        blocked = is_blocked(info, now, "PAPER")
        self.assertTrue(blocked)
        self.assertTrue(any("Startup suppression active, entry ignored." in m for m in logger.messages))

    def test_hydrate_restores_open_position_and_logs(self):
        persisted = {
            "last_exit_time": datetime(2026, 2, 26, 14, 10, 0),
            "scalp_cooldown_until": datetime(2026, 2, 26, 14, 20, 0),
            "startup_suppression_until": datetime(2026, 2, 26, 14, 25, 0),
        }
        funcs, logger = _load_functions(
            "_parse_ts",
            "_hydrate_runtime_state",
            extra_ns={"_load_restart_state": lambda _acct: persisted},
        )
        hydrate = funcs["_hydrate_runtime_state"]
        info = {
            "call_buy": {
                "option_name": "NSE:NIFTY_TESTCE",
                "side": "CALL",
                "position_id": "POS_RESTORE_1",
                "trade_flag": 1,
                "is_open": False,
                "lifecycle_state": "EXIT",
            },
            "put_buy": {"trade_flag": 0, "is_open": False},
        }
        out = hydrate(info, "PAPER", "PAPER")
        self.assertTrue(out["call_buy"]["is_open"])
        self.assertEqual(out["call_buy"]["trade_flag"], 1)
        self.assertIsNotNone(out.get("startup_suppression_until"))
        self.assertTrue(any("[ENTRY][STATE_RESTORED]" in m for m in logger.messages))


if __name__ == "__main__":
    unittest.main()
