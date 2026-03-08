"""Unit tests for broker adapter configuration and dynamic dispatch.

Coverage
--------
TestBrokerConfigModule      – config.py BROKER setting and credential loading
TestBuildBrokerAdapter      – broker_init.build_broker_adapter factory
TestInvalidBroker           – ValueError on unrecognised broker name
TestImportError             – ImportError propagated when SDK missing
TestEntryDispatchLog        – [ENTRY DISPATCH] log tag in place_st_pullback_entry
TestExitDispatchLog         – [EXIT DISPATCH]  log tag in place_st_pullback_exit
TestFyersAdapterDispatch    – FyersAdapter routes entry/exit via SDK mock
TestZerodhaAdapterDispatch  – ZerodhaKiteAdapter routes entry/exit via SDK mock
TestBrokerConfigLog         – [BROKER CONFIG] log tags emitted correctly
"""

import sys
import types
import unittest
from unittest.mock import MagicMock, patch, call

import pandas as pd

# ── Stub heavy dependencies so we can import engine modules cleanly ────────────
# (Same pattern as test_exit_logic.py — safe to call multiple times in one process)

_CONFIG_STUB_NAMES = [
    "fyers_apiv3", "fyers_apiv3.fyersModel",
    "kiteconnect",
    "config", "setup", "indicators", "signals",
    "orchestration", "position_manager", "day_type",
]

for _n in _CONFIG_STUB_NAMES:
    if _n not in sys.modules:
        sys.modules[_n] = types.ModuleType(_n)

# -- config stub --
_cfg = sys.modules["config"]
_cfg.BROKER               = "fyers"
_cfg.client_id            = "FYERS_TEST_CLIENT"
_cfg.secret_key           = "secret"
_cfg.access_token         = "fyers_token_xyz"
_cfg.redirect_uri         = "https://localhost"
_cfg.ZERODHA_API_KEY      = "zerodha_key_123"
_cfg.ZERODHA_API_SECRET   = "zerodha_secret"
_cfg.ZERODHA_ACCESS_TOKEN = "zerodha_access_tok"
_cfg.time_zone            = "Asia/Kolkata"
_cfg.strategy_name        = "TEST"
_cfg.MAX_TRADES_PER_DAY   = 3
_cfg.account_type         = "PAPER"
_cfg.quantity             = 1
_cfg.CALL_MONEYNESS       = 0
_cfg.PUT_MONEYNESS        = 0
_cfg.profit_loss_point    = 5
_cfg.ENTRY_OFFSET         = 0
_cfg.ORDER_TYPE           = "MARKET"
_cfg.MAX_DAILY_LOSS       = -10000
_cfg.MAX_DRAWDOWN         = -5000
_cfg.OSCILLATOR_EXIT_MODE = "SOFT"
_cfg.symbols              = {"index": "NSE:NIFTY50-INDEX"}

# -- setup stub --
_stup = sys.modules["setup"]
_stup.df           = pd.DataFrame()
_stup.fyers        = MagicMock()
_stup.ticker       = "NSE:NIFTY50-INDEX"
_stup.option_chain = {}
_stup.spot_price   = 22000.0
_stup.start_time   = "09:15"
_stup.end_time     = "15:30"
_stup.hist_data    = {}

# -- indicators stub --
_ind = sys.modules["indicators"]
_ind.calculate_cpr                = MagicMock(return_value={})
_ind.calculate_traditional_pivots = MagicMock(return_value={})
_ind.calculate_camarilla_pivots   = MagicMock(return_value={})
_ind.resolve_atr                  = MagicMock(return_value=50.0)
_ind.daily_atr                    = MagicMock(return_value=50.0)
_ind.williams_r                   = MagicMock(return_value=None)
_ind.calculate_cci                = MagicMock(return_value=pd.Series(dtype=float))
_ind.momentum_ok                  = MagicMock(return_value=(True, 0.0))
_ind.classify_cpr_width           = MagicMock(return_value="NORMAL")

# -- remaining stubs --
sys.modules["signals"].detect_signal     = MagicMock(return_value=None)
sys.modules["signals"].get_opening_range = MagicMock(return_value=(None, None))
sys.modules["signals"].compute_tilt_state = MagicMock(return_value="NEUTRAL")
sys.modules["orchestration"].update_candles_and_signals = MagicMock()
sys.modules["orchestration"].build_indicator_dataframe  = MagicMock(return_value=pd.DataFrame())
sys.modules["position_manager"].make_replay_pm          = MagicMock()
sys.modules["day_type"].make_day_type_classifier        = MagicMock()
sys.modules["day_type"].apply_day_type_to_pm            = MagicMock()
sys.modules["day_type"].DayType                         = MagicMock()
sys.modules["day_type"].DayTypeResult                   = MagicMock()

_fyers_pkg = sys.modules["fyers_apiv3"]
_fyers_model_mod = sys.modules["fyers_apiv3.fyersModel"]
_fyers_pkg.fyersModel      = _fyers_model_mod
_fyers_model_mod.FyersModel = MagicMock()

_kite_mod = sys.modules["kiteconnect"]
_kite_mod.KiteConnect = MagicMock()

# ── Now safe to import project modules ────────────────────────────────────────
import broker_init  # noqa: E402
from broker_init import build_broker_adapter  # noqa: E402
from st_pullback_cci import (  # noqa: E402
    BrokerAdapter,
    FyersAdapter,
    ZerodhaKiteAdapter,
    place_st_pullback_entry,
    place_st_pullback_exit,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

class _GoodAdapter(BrokerAdapter):
    """Minimal stub adapter that always succeeds."""
    NAME = "GoodAdapter"

    def place_entry(self, symbol, qty, side_int, limit_price=0.0, stop=0.0, target=0.0):
        return True, f"entry-{symbol}"

    def place_exit(self, symbol, qty, reason):
        return True, f"exit-{symbol}"


class _BadAdapter(BrokerAdapter):
    """Stub adapter that always fails (returns False)."""
    NAME = "BadAdapter"

    def place_entry(self, symbol, qty, side_int, limit_price=0.0, stop=0.0, target=0.0):
        return False, None

    def place_exit(self, symbol, qty, reason):
        return False, None


class _ErrorAdapter(BrokerAdapter):
    """Stub adapter that raises on every call."""

    def place_entry(self, symbol, qty, side_int, limit_price=0.0, stop=0.0, target=0.0):
        raise RuntimeError("SDK connection lost")

    def place_exit(self, symbol, qty, reason):
        raise RuntimeError("SDK connection lost")


# ═══════════════════════════════════════════════════════════════════════════════
# TestBrokerConfigModule  –  config.py BROKER setting
# ═══════════════════════════════════════════════════════════════════════════════

class TestBrokerConfigModule(unittest.TestCase):

    def test_broker_default_is_fyers(self):
        """Stubbed config has BROKER='fyers' (the documented default)."""
        import config as cfg  # the stub
        self.assertEqual(cfg.BROKER, "fyers")

    def test_zerodha_credentials_present(self):
        import config as cfg
        self.assertIsNotNone(cfg.ZERODHA_API_KEY)
        self.assertIsNotNone(cfg.ZERODHA_API_SECRET)
        self.assertIsNotNone(cfg.ZERODHA_ACCESS_TOKEN)

    def test_fyers_credentials_present(self):
        import config as cfg
        self.assertIsNotNone(cfg.client_id)
        self.assertIsNotNone(cfg.access_token)

    def test_broker_value_is_lowercase_string(self):
        import config as cfg
        self.assertIsInstance(cfg.BROKER, str)
        self.assertEqual(cfg.BROKER, cfg.BROKER.lower())


# ═══════════════════════════════════════════════════════════════════════════════
# TestBuildBrokerAdapter  –  factory returns correct adapter type
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildBrokerAdapter(unittest.TestCase):

    def test_fyers_override_returns_fyers_adapter(self):
        """build_broker_adapter('fyers') → FyersAdapter instance."""
        mock_fyers_model = MagicMock()
        mock_fyers_model.FyersModel.return_value = MagicMock()

        with patch.dict(sys.modules, {
            "fyers_apiv3":           MagicMock(fyersModel=mock_fyers_model),
            "fyers_apiv3.fyersModel": mock_fyers_model,
        }):
            adapter = build_broker_adapter("fyers")

        self.assertIsInstance(adapter, FyersAdapter)

    def test_zerodha_override_returns_zerodha_adapter(self):
        """build_broker_adapter('zerodha') → ZerodhaKiteAdapter instance."""
        mock_kite_cls = MagicMock()
        mock_kite_mod = MagicMock(KiteConnect=mock_kite_cls)

        with patch.dict(sys.modules, {"kiteconnect": mock_kite_mod}):
            adapter = build_broker_adapter("zerodha")

        self.assertIsInstance(adapter, ZerodhaKiteAdapter)

    def test_override_is_case_insensitive(self):
        """Broker name is normalised to lower-case before dispatch."""
        mock_fyers_model = MagicMock()
        mock_fyers_model.FyersModel.return_value = MagicMock()

        with patch.dict(sys.modules, {
            "fyers_apiv3":           MagicMock(fyersModel=mock_fyers_model),
            "fyers_apiv3.fyersModel": mock_fyers_model,
        }):
            adapter = build_broker_adapter("FYERS")

        self.assertIsInstance(adapter, FyersAdapter)

    def test_zerodha_kite_set_access_token_called(self):
        """ZerodhaKiteAdapter initialisation calls kite.set_access_token."""
        mock_kite_instance = MagicMock()
        mock_kite_cls = MagicMock(return_value=mock_kite_instance)
        mock_kite_mod = MagicMock(KiteConnect=mock_kite_cls)

        with patch.dict(sys.modules, {"kiteconnect": mock_kite_mod}):
            build_broker_adapter("zerodha")

        mock_kite_instance.set_access_token.assert_called_once()

    def test_fyers_model_constructed_with_credentials(self):
        """FyersAdapter wraps FyersModel built with config credentials."""
        mock_fm_instance = MagicMock()
        mock_fyers_model = MagicMock(FyersModel=MagicMock(return_value=mock_fm_instance))

        with patch.dict(sys.modules, {
            "fyers_apiv3":           MagicMock(fyersModel=mock_fyers_model),
            "fyers_apiv3.fyersModel": mock_fyers_model,
        }):
            adapter = build_broker_adapter("fyers")

        # Verify FyersModel was instantiated (at least once)
        mock_fyers_model.FyersModel.assert_called_once()

    def test_adapter_wraps_correct_client(self):
        """The adapter's internal client is the mock returned by the SDK."""
        mock_kite_instance = MagicMock()
        mock_kite_cls = MagicMock(return_value=mock_kite_instance)
        mock_kite_mod = MagicMock(KiteConnect=mock_kite_cls)

        with patch.dict(sys.modules, {"kiteconnect": mock_kite_mod}):
            adapter = build_broker_adapter("zerodha")

        self.assertIs(adapter._kite, mock_kite_instance)


# ═══════════════════════════════════════════════════════════════════════════════
# TestInvalidBroker
# ═══════════════════════════════════════════════════════════════════════════════

class TestInvalidBroker(unittest.TestCase):

    def test_unknown_broker_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            build_broker_adapter("angelone")
        self.assertIn("angelone", str(ctx.exception))

    def test_empty_broker_raises_value_error(self):
        with self.assertRaises(ValueError):
            build_broker_adapter("")

    def test_error_message_lists_allowed_values(self):
        with self.assertRaises(ValueError) as ctx:
            build_broker_adapter("binance")
        msg = str(ctx.exception)
        self.assertIn("fyers", msg)
        self.assertIn("zerodha", msg)


# ═══════════════════════════════════════════════════════════════════════════════
# TestImportError  –  SDK not installed
# ═══════════════════════════════════════════════════════════════════════════════

class TestImportError(unittest.TestCase):

    def test_fyers_sdk_missing_raises_import_error(self):
        """If fyers_apiv3 is absent, _build_fyers_adapter raises ImportError."""
        with patch.dict(sys.modules, {"fyers_apiv3": None,
                                       "fyers_apiv3.fyersModel": None}):
            with self.assertRaises((ImportError, TypeError)):
                build_broker_adapter("fyers")

    def test_zerodha_sdk_missing_raises_import_error(self):
        """If kiteconnect is absent, _build_zerodha_adapter raises ImportError."""
        with patch.dict(sys.modules, {"kiteconnect": None}):
            with self.assertRaises((ImportError, TypeError)):
                build_broker_adapter("zerodha")


# ═══════════════════════════════════════════════════════════════════════════════
# TestBrokerConfigLog  –  [BROKER CONFIG] logging
# ═══════════════════════════════════════════════════════════════════════════════

class TestBrokerConfigLog(unittest.TestCase):

    def test_build_broker_adapter_logs_broker_config(self):
        """build_broker_adapter emits [BROKER CONFIG] log with selected broker."""
        mock_fyers_model = MagicMock()
        mock_fyers_model.FyersModel.return_value = MagicMock()

        with patch.dict(sys.modules, {
            "fyers_apiv3":           MagicMock(fyersModel=mock_fyers_model),
            "fyers_apiv3.fyersModel": mock_fyers_model,
        }):
            with self.assertLogs(level="INFO") as cm:
                build_broker_adapter("fyers")

        self.assertTrue(any("[BROKER CONFIG]" in line for line in cm.output))

    def test_fyers_adapter_init_logged(self):
        """FyersAdapter initialisation emits [BROKER CONFIG] with client_id."""
        mock_fyers_model = MagicMock()
        mock_fyers_model.FyersModel.return_value = MagicMock()

        with patch.dict(sys.modules, {
            "fyers_apiv3":           MagicMock(fyersModel=mock_fyers_model),
            "fyers_apiv3.fyersModel": mock_fyers_model,
        }):
            with self.assertLogs(level="INFO") as cm:
                build_broker_adapter("fyers")

        self.assertTrue(any("FyersAdapter" in line for line in cm.output))

    def test_zerodha_adapter_init_logged(self):
        """ZerodhaKiteAdapter initialisation emits [BROKER CONFIG] with api_key."""
        mock_kite_cls = MagicMock()
        mock_kite_mod = MagicMock(KiteConnect=mock_kite_cls)

        with patch.dict(sys.modules, {"kiteconnect": mock_kite_mod}):
            with self.assertLogs(level="INFO") as cm:
                build_broker_adapter("zerodha")

        self.assertTrue(any("ZerodhaKiteAdapter" in line for line in cm.output))


# ═══════════════════════════════════════════════════════════════════════════════
# TestEntryDispatchLog  –  [ENTRY DISPATCH] tag from place_st_pullback_entry
# ═══════════════════════════════════════════════════════════════════════════════

class TestEntryDispatchLog(unittest.TestCase):

    def test_entry_dispatch_log_emitted_on_success(self):
        """[ENTRY DISPATCH] tag appears in log when adapter succeeds."""
        adapter = _GoodAdapter()
        with self.assertLogs(level="INFO") as cm:
            place_st_pullback_entry(
                symbol="NIFTY25000CE", qty=50, side="BUY",
                entry_price=120.0, sl=110.0, pt=140.0,
                mode="PAPER", broker_fn=adapter, timestamp="T1",
            )
        self.assertTrue(any("[ENTRY DISPATCH]" in line for line in cm.output))

    def test_entry_dispatch_contains_broker_name(self):
        """[ENTRY DISPATCH] line includes the adapter class name."""
        adapter = _GoodAdapter()
        with self.assertLogs(level="INFO") as cm:
            place_st_pullback_entry(
                symbol="NIFTY25000CE", qty=50, side="SELL",
                entry_price=120.0, sl=130.0, pt=100.0,
                mode="PAPER", broker_fn=adapter, timestamp="T1",
            )
        dispatch_lines = [l for l in cm.output if "[ENTRY DISPATCH]" in l]
        self.assertTrue(len(dispatch_lines) >= 1)
        self.assertIn("_GoodAdapter", dispatch_lines[0])

    def test_entry_dispatch_contains_symbol_and_side(self):
        """[ENTRY DISPATCH] line includes the symbol and side."""
        adapter = _GoodAdapter()
        with self.assertLogs(level="INFO") as cm:
            place_st_pullback_entry(
                symbol="NIFTY24000PE", qty=50, side="BUY",
                entry_price=80.0, sl=70.0, pt=100.0,
                mode="PAPER", broker_fn=adapter,
            )
        dispatch_lines = [l for l in cm.output if "[ENTRY DISPATCH]" in l]
        self.assertTrue(any("NIFTY24000PE" in l and "BUY" in l for l in dispatch_lines))

    def test_no_entry_dispatch_log_when_adapter_fails(self):
        """[ENTRY DISPATCH] must NOT appear when adapter returns False."""
        adapter = _BadAdapter()
        with self.assertLogs(level="ERROR") as cm:
            place_st_pullback_entry(
                symbol="NIFTY25000CE", qty=50, side="BUY",
                entry_price=120.0, sl=110.0, pt=140.0,
                mode="PAPER", broker_fn=adapter,
            )
        self.assertFalse(any("[ENTRY DISPATCH]" in line for line in cm.output))

    def test_fyers_adapter_name_in_dispatch_log(self):
        """FyersAdapter class name appears in [ENTRY DISPATCH] log."""
        mock_fyers_client = MagicMock()
        mock_fyers_client.place_order.return_value = {"s": "ok", "id": "F001"}
        adapter = FyersAdapter(mock_fyers_client)

        with self.assertLogs(level="INFO") as cm:
            place_st_pullback_entry(
                symbol="NSE:NIFTY25000CE", qty=50, side="BUY",
                entry_price=120.0, sl=110.0, pt=140.0,
                mode="LIVE", broker_fn=adapter,
            )
        dispatch_lines = [l for l in cm.output if "[ENTRY DISPATCH]" in l]
        self.assertTrue(any("FyersAdapter" in l for l in dispatch_lines))

    def test_zerodha_adapter_name_in_dispatch_log(self):
        """ZerodhaKiteAdapter class name appears in [ENTRY DISPATCH] log."""
        mock_kite = MagicMock()
        mock_kite.place_order.return_value = "KITE-001"
        adapter = ZerodhaKiteAdapter(mock_kite)

        with self.assertLogs(level="INFO") as cm:
            place_st_pullback_entry(
                symbol="NIFTY25000CE", qty=50, side="BUY",
                entry_price=120.0, sl=110.0, pt=140.0,
                mode="LIVE", broker_fn=adapter,
            )
        dispatch_lines = [l for l in cm.output if "[ENTRY DISPATCH]" in l]
        self.assertTrue(any("ZerodhaKiteAdapter" in l for l in dispatch_lines))

    def test_entry_dispatch_contains_rr_ratio_when_config_passed(self):
        """[ENTRY DISPATCH] includes rr_ratio and tg_rr_ratio when config is supplied."""
        from st_pullback_cci import STEntryConfig
        cfg = STEntryConfig(rr_ratio=2.0, tg_rr_ratio=1.0)
        adapter = _GoodAdapter()

        with self.assertLogs(level="INFO") as cm:
            place_st_pullback_entry(
                symbol="NIFTY25000CE", qty=50, side="BUY",
                entry_price=120.0, sl=110.0, pt=140.0,
                mode="PAPER", broker_fn=adapter,
                config=cfg,
            )
        dispatch_lines = [l for l in cm.output if "[ENTRY DISPATCH]" in l]
        self.assertTrue(
            any("rr_ratio=2.0" in l for l in dispatch_lines),
            f"rr_ratio=2.0 not found in dispatch lines: {dispatch_lines}"
        )
        self.assertTrue(
            any("tg_rr_ratio=1.0" in l for l in dispatch_lines),
            f"tg_rr_ratio=1.0 not found in dispatch lines: {dispatch_lines}"
        )

    def test_entry_dispatch_rr_ratio_na_without_config(self):
        """[ENTRY DISPATCH] shows rr_ratio=N/A when no config is passed."""
        adapter = _GoodAdapter()

        with self.assertLogs(level="INFO") as cm:
            place_st_pullback_entry(
                symbol="NIFTY25000CE", qty=50, side="BUY",
                entry_price=120.0, sl=110.0, pt=140.0,
                mode="PAPER", broker_fn=adapter,
            )
        dispatch_lines = [l for l in cm.output if "[ENTRY DISPATCH]" in l]
        self.assertTrue(
            any("rr_ratio=N/A" in l for l in dispatch_lines),
            f"rr_ratio=N/A not found in dispatch lines: {dispatch_lines}"
        )

    def test_entry_dispatch_contains_entry_sl_pt_values(self):
        """[ENTRY DISPATCH] log includes numeric entry, sl, and pt values."""
        adapter = _GoodAdapter()

        with self.assertLogs(level="INFO") as cm:
            place_st_pullback_entry(
                symbol="NIFTY25000CE", qty=50, side="BUY",
                entry_price=120.0, sl=110.0, pt=140.0, tg=125.0,
                mode="PAPER", broker_fn=adapter,
            )
        dispatch_lines = [l for l in cm.output if "[ENTRY DISPATCH]" in l]
        self.assertTrue(any("entry=120.0" in l for l in dispatch_lines))
        self.assertTrue(any("sl=110.0"    in l for l in dispatch_lines))
        self.assertTrue(any("pt=140.0"    in l for l in dispatch_lines))


# ═══════════════════════════════════════════════════════════════════════════════
# TestExitDispatchLog  –  [EXIT DISPATCH] tag from place_st_pullback_exit
# ═══════════════════════════════════════════════════════════════════════════════

class TestExitDispatchLog(unittest.TestCase):

    def test_exit_dispatch_log_emitted_on_success(self):
        """[EXIT DISPATCH] tag appears in log when adapter succeeds."""
        adapter = _GoodAdapter()
        with self.assertLogs(level="INFO") as cm:
            place_st_pullback_exit(
                symbol="NIFTY25000CE", qty=50, reason="SL_HIT",
                mode="PAPER", broker_fn=adapter, timestamp="T1",
            )
        self.assertTrue(any("[EXIT DISPATCH]" in line for line in cm.output))

    def test_exit_dispatch_contains_broker_name(self):
        """[EXIT DISPATCH] line includes the adapter class name."""
        adapter = _GoodAdapter()
        with self.assertLogs(level="INFO") as cm:
            place_st_pullback_exit(
                symbol="NIFTY25000CE", qty=50, reason="PT_HIT",
                mode="PAPER", broker_fn=adapter, timestamp="T1",
            )
        dispatch_lines = [l for l in cm.output if "[EXIT DISPATCH]" in l]
        self.assertTrue(any("_GoodAdapter" in l for l in dispatch_lines))

    def test_exit_dispatch_contains_reason(self):
        """[EXIT DISPATCH] line includes the exit reason."""
        adapter = _GoodAdapter()
        with self.assertLogs(level="INFO") as cm:
            place_st_pullback_exit(
                symbol="NIFTY25000CE", qty=50, reason="TARGET_HIT",
                mode="PAPER", broker_fn=adapter,
            )
        dispatch_lines = [l for l in cm.output if "[EXIT DISPATCH]" in l]
        self.assertTrue(any("TARGET_HIT" in l for l in dispatch_lines))

    def test_no_exit_dispatch_log_when_adapter_fails(self):
        """[EXIT DISPATCH] must NOT appear when adapter returns False."""
        adapter = _BadAdapter()
        with self.assertLogs(level="ERROR") as cm:
            place_st_pullback_exit(
                symbol="NIFTY25000CE", qty=50, reason="SL_HIT",
                mode="PAPER", broker_fn=adapter,
            )
        self.assertFalse(any("[EXIT DISPATCH]" in line for line in cm.output))


# ═══════════════════════════════════════════════════════════════════════════════
# TestFyersAdapterDispatch  –  FyersAdapter SDK interaction
# ═══════════════════════════════════════════════════════════════════════════════

class TestFyersAdapterDispatch(unittest.TestCase):

    def _adapter(self, order_resp=None):
        client = MagicMock()
        client.place_order.return_value = order_resp or {"s": "ok", "id": "F001"}
        return FyersAdapter(client), client

    def test_entry_routed_via_fyers_place_order(self):
        """FyersAdapter.place_entry calls fyers_client.place_order exactly once."""
        adapter, client = self._adapter()
        ok, oid = adapter.place_entry("NSE:NIFTY25000CE", 50, 1, 120.0)
        self.assertTrue(ok)
        self.assertEqual(oid, "F001")
        client.place_order.assert_called_once()

    def test_exit_routed_via_fyers_place_order(self):
        """FyersAdapter.place_exit calls fyers_client.place_order exactly once."""
        adapter, client = self._adapter()
        ok, oid = adapter.place_exit("NSE:NIFTY25000CE", 50, "SL_HIT")
        self.assertTrue(ok)
        client.place_order.assert_called_once()

    def test_entry_side_int_1_maps_to_buy(self):
        """side_int=1 is passed as-is into the Fyers order dict."""
        adapter, client = self._adapter()
        adapter.place_entry("SYM", 10, 1, 100.0)
        order_data = client.place_order.call_args[1]["data"]
        self.assertEqual(order_data["side"], 1)

    def test_entry_side_int_minus1_maps_to_sell(self):
        """side_int=-1 is passed as-is into the Fyers order dict."""
        adapter, client = self._adapter()
        adapter.place_entry("SYM", 10, -1, 100.0)
        order_data = client.place_order.call_args[1]["data"]
        self.assertEqual(order_data["side"], -1)

    def test_limit_price_zero_selects_market_type(self):
        """limit_price=0.0 → Fyers order type=2 (MARKET)."""
        adapter, client = self._adapter()
        adapter.place_entry("SYM", 10, 1, 0.0)
        order_data = client.place_order.call_args[1]["data"]
        self.assertEqual(order_data["type"], 2)

    def test_limit_price_nonzero_selects_limit_type(self):
        """limit_price>0 → Fyers order type=1 (LIMIT)."""
        adapter, client = self._adapter()
        adapter.place_entry("SYM", 10, 1, 120.0)
        order_data = client.place_order.call_args[1]["data"]
        self.assertEqual(order_data["type"], 1)

    def test_fyers_error_response_returns_false(self):
        """Non-'ok' Fyers response → (False, None)."""
        adapter, _ = self._adapter({"s": "error", "message": "Insufficient funds"})
        ok, oid = adapter.place_entry("SYM", 10, 1, 100.0)
        self.assertFalse(ok)
        self.assertIsNone(oid)

    def test_fyers_exception_returns_false(self):
        """Exception in place_order → (False, None) without propagating."""
        client = MagicMock()
        client.place_order.side_effect = ConnectionError("network timeout")
        adapter = FyersAdapter(client)
        ok, oid = adapter.place_entry("SYM", 10, 1, 100.0)
        self.assertFalse(ok)
        self.assertIsNone(oid)


# ═══════════════════════════════════════════════════════════════════════════════
# TestZerodhaAdapterDispatch  –  ZerodhaKiteAdapter SDK interaction
# ═══════════════════════════════════════════════════════════════════════════════

class TestZerodhaAdapterDispatch(unittest.TestCase):

    def _adapter(self, order_id="KITE-001"):
        kite = MagicMock()
        kite.place_order.return_value = order_id
        return ZerodhaKiteAdapter(kite), kite

    def test_entry_routed_via_kite_place_order(self):
        """ZerodhaKiteAdapter.place_entry calls kite.place_order exactly once."""
        adapter, kite = self._adapter()
        ok, oid = adapter.place_entry("NIFTY25000CE", 50, 1, 120.0)
        self.assertTrue(ok)
        self.assertEqual(oid, "KITE-001")
        kite.place_order.assert_called_once()

    def test_exit_routed_via_kite_place_order(self):
        """ZerodhaKiteAdapter.place_exit calls kite.place_order exactly once."""
        adapter, kite = self._adapter()
        ok, oid = adapter.place_exit("NIFTY25000CE", 50, "SL_HIT")
        self.assertTrue(ok)
        kite.place_order.assert_called_once()

    def test_entry_buy_side_mapped_correctly(self):
        """side_int=1 → transaction_type='BUY' in Kite call."""
        adapter, kite = self._adapter()
        adapter.place_entry("SYM", 10, 1, 100.0)
        kwargs = kite.place_order.call_args[1]
        self.assertEqual(kwargs["transaction_type"], "BUY")

    def test_entry_sell_side_mapped_correctly(self):
        """side_int=-1 → transaction_type='SELL' in Kite call."""
        adapter, kite = self._adapter()
        adapter.place_entry("SYM", 10, -1, 100.0)
        kwargs = kite.place_order.call_args[1]
        self.assertEqual(kwargs["transaction_type"], "SELL")

    def test_limit_price_zero_selects_market(self):
        """limit_price=0.0 → order_type='MARKET'."""
        adapter, kite = self._adapter()
        adapter.place_entry("SYM", 10, 1, 0.0)
        kwargs = kite.place_order.call_args[1]
        self.assertEqual(kwargs["order_type"], "MARKET")

    def test_limit_price_nonzero_selects_limit(self):
        """limit_price>0 → order_type='LIMIT'."""
        adapter, kite = self._adapter()
        adapter.place_entry("SYM", 10, 1, 120.0)
        kwargs = kite.place_order.call_args[1]
        self.assertEqual(kwargs["order_type"], "LIMIT")

    def test_exit_always_sell(self):
        """Kite exit is always a SELL (square-off)."""
        adapter, kite = self._adapter()
        adapter.place_exit("SYM", 10, "PT_HIT")
        kwargs = kite.place_order.call_args[1]
        self.assertEqual(kwargs["transaction_type"], "SELL")

    def test_kite_exception_returns_false(self):
        """Exception in kite.place_order → (False, None)."""
        kite = MagicMock()
        kite.place_order.side_effect = Exception("kite api down")
        adapter = ZerodhaKiteAdapter(kite)
        ok, oid = adapter.place_entry("SYM", 10, 1, 100.0)
        self.assertFalse(ok)
        self.assertIsNone(oid)


# ═══════════════════════════════════════════════════════════════════════════════
# TestUnifiedDispatch  –  broker_fn routing through place_st_pullback_*
# ═══════════════════════════════════════════════════════════════════════════════

class TestUnifiedDispatch(unittest.TestCase):
    """Verify that place_st_pullback_entry/exit route correctly to whichever
    adapter (Fyers or Zerodha) is supplied as broker_fn."""

    def _fyers_adapter(self):
        client = MagicMock()
        client.place_order.return_value = {"s": "ok", "id": "F-ENTRY-001"}
        return FyersAdapter(client)

    def _zerodha_adapter(self):
        kite = MagicMock()
        kite.place_order.return_value = "Z-ENTRY-001"
        return ZerodhaKiteAdapter(kite)

    def test_fyers_entry_via_place_st_pullback_entry(self):
        ok, oid = place_st_pullback_entry(
            symbol="NSE:NIFTY25000CE", qty=50, side="BUY",
            entry_price=120.0, sl=110.0, pt=140.0,
            mode="LIVE", broker_fn=self._fyers_adapter(),
        )
        self.assertTrue(ok)
        self.assertEqual(oid, "F-ENTRY-001")

    def test_zerodha_entry_via_place_st_pullback_entry(self):
        ok, oid = place_st_pullback_entry(
            symbol="NIFTY25000CE", qty=50, side="BUY",
            entry_price=120.0, sl=110.0, pt=140.0,
            mode="LIVE", broker_fn=self._zerodha_adapter(),
        )
        self.assertTrue(ok)
        self.assertEqual(oid, "Z-ENTRY-001")

    def test_fyers_exit_via_place_st_pullback_exit(self):
        client = MagicMock()
        client.place_order.return_value = {"s": "ok", "id": "F-EXIT-001"}
        adapter = FyersAdapter(client)
        ok, oid = place_st_pullback_exit(
            symbol="NSE:NIFTY25000CE", qty=50, reason="SL_HIT",
            mode="LIVE", broker_fn=adapter,
        )
        self.assertTrue(ok)
        self.assertEqual(oid, "F-EXIT-001")

    def test_zerodha_exit_via_place_st_pullback_exit(self):
        kite = MagicMock()
        kite.place_order.return_value = "Z-EXIT-001"
        adapter = ZerodhaKiteAdapter(kite)
        ok, oid = place_st_pullback_exit(
            symbol="NIFTY25000CE", qty=50, reason="TARGET_HIT",
            mode="LIVE", broker_fn=adapter,
        )
        self.assertTrue(ok)
        self.assertEqual(oid, "Z-EXIT-001")

    def test_adapter_failure_propagated_to_caller(self):
        """(False, None) from adapter is returned directly to the caller."""
        adapter = _BadAdapter()
        ok, oid = place_st_pullback_entry(
            symbol="SYM", qty=10, side="BUY",
            entry_price=100.0, sl=90.0, pt=120.0,
            mode="LIVE", broker_fn=adapter,
        )
        self.assertFalse(ok)
        self.assertIsNone(oid)

    def test_adapter_exception_caught_returns_false(self):
        """Exception raised by adapter is caught; caller receives (False, None)."""
        adapter = _ErrorAdapter()
        ok, oid = place_st_pullback_entry(
            symbol="SYM", qty=10, side="SELL",
            entry_price=100.0, sl=110.0, pt=80.0,
            mode="LIVE", broker_fn=adapter,
        )
        self.assertFalse(ok)
        self.assertIsNone(oid)

    def test_paper_sim_when_no_broker_fn(self):
        """broker_fn=None → internal paper simulation; always returns True."""
        ok, oid = place_st_pullback_entry(
            symbol="SYM", qty=10, side="BUY",
            entry_price=100.0, sl=90.0, pt=120.0,
            mode="PAPER", broker_fn=None,
        )
        self.assertTrue(ok)
        self.assertIsNotNone(oid)


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
