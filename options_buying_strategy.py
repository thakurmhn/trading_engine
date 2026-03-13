"""Options Buying Strategy — Complete implementation using tbot_core library.

Replicates the full options buying pipeline from execution.py as a clean
class-based strategy. Supports PAPER, LIVE, and REPLAY execution modes.

Usage:
    strategy = OptionsBuyingStrategy()
    # Paper mode
    strategy.run_paper(candles_3m, candles_15m, spot_price=spot)
    # Replay mode
    strategy.run_replay(db_path="path/to/ticks.db", date_str="2026-03-10")
    # Live mode
    strategy.run_live(candles_3m, candles_15m)
"""
from __future__ import annotations

import logging
import pickle
import pathlib
import re
import numpy as np
import pandas as pd
import pendulum as dt
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, time as dtime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

# ── tbot_core: Indicators ────────────────────────────────────────
from tbot_core.indicators import (
    supertrend,
    calculate_ema,
    calculate_adx,
    ema_bias,
    adx_bias,
    compute_rsi,
    calculate_cci,
    cci_bias,
    williams_r,
    momentum_ok,
    calculate_atr,
    calculate_atr_series,
    resolve_atr,
    daily_atr,
    calculate_cpr,
    calculate_traditional_pivots,
    calculate_camarilla_pivots,
    classify_cpr_width,
    calculate_typical_price_ma,
    build_indicator_dataframe,
)

# ── tbot_core: Broker ────────────────────────────────────────────
from tbot_core.broker import build_broker_adapter
from tbot_core.broker.base import BrokerAdapter

# ── tbot_core: Data ──────────────────────────────────────────────
from tbot_core.data.candle_builder import (
    build_3min_candle,
    prepare_intraday,
    resample_15m,
    resample_candles,
)
from tbot_core.data.candle_store import CandleAggregator, CandleStore
from tbot_core.data.tick_db import TickDatabase
from tbot_core.data.feed import DataFeed

# ── tbot_core: Config ────────────────────────────────────────────
from tbot_core.config.defaults import (
    STRATEGY_NAME,
    INDEX_NAME,
    DEFAULT_SYMBOLS,
    EXCHANGE,
    STRIKE_COUNT,
    STRIKE_DIFF,
    DEFAULT_ACCOUNT_TYPE,
    DEFAULT_QUANTITY,
    BUFFER,
    PROFIT_LOSS_POINT,
    MAX_TRADES_PER_DAY,
    CALL_MONEYNESS,
    PUT_MONEYNESS,
    TIME_ZONE,
    START_HOUR,
    START_MIN,
    END_HOUR,
    END_MIN,
    MARKET_OPEN_HOUR,
    MARKET_OPEN_MIN,
    MARKET_CLOSE_HOUR,
    MARKET_CLOSE_MIN,
    CANDLE_INTERVAL_MIN,
    ATR_PERIOD,
    CANDLE_BODY_RANGE,
    ATR_VALUE,
    MAX_DAILY_LOSS,
    MAX_DRAWDOWN,
    OSCILLATOR_EXIT_MODE,
    TREND_ENTRY_ADX_MIN,
    SLOPE_ADX_GATE,
    TIME_SLOPE_ADX_GATE,
    SLOPE_CONFLICT_TIME_BARS,
    ST_RR_RATIO,
    ST_TG_RR_RATIO,
    MODE,
)
from tbot_core.config.timeframes import Timeframe

# ── tbot_core: Signals ───────────────────────────────────────────
from tbot_core.signals import (
    detect_signal,
    TrendContinuationState,
    compute_tilt_state,
    calculate_vwap,
    get_opening_range,
    detect_reversal,
    CompressionState,
    detect_failed_breakout,
    detect_zones,
    detect_zone_revisit,
    update_zone_activity,
    get_pulse_module,
    PulseModule,
    PulseMetrics,
)

# ── tbot_core: Entry ─────────────────────────────────────────────
from tbot_core.entry import check_entry_condition

# ── tbot_core: Exit ──────────────────────────────────────────────
from tbot_core.exit import OptionExitManager, OptionExitConfig

# ── tbot_core: Position ──────────────────────────────────────────
from tbot_core.position import (
    PositionManager,
    make_replay_pm,
    make_paper_pm,
    ScalpTrade,
    TrendTrade,
    TradeLogger,
)

# ── tbot_core: Context ───────────────────────────────────────────
from tbot_core.context import (
    RegimeContext,
    compute_regime_context,
    DayTypeClassifier,
    DayTypeResult,
    apply_day_type_to_threshold,
    apply_day_type_to_pm,
    get_daily_sentiment,
    compute_intraday_sentiment,
    classify_day_type,
    get_opening_bias,
)

# ── Root modules (not yet in tbot_core) ──────────────────────────
try:
    from fyers_apiv3 import fyersModel
except ImportError:
    fyersModel = None

# Lazy import for setup.py (live/paper mode only)
import importlib as _il
try:
    _signals_module = _il.import_module("signals")
except ImportError:
    _signals_module = None

try:
    from orchestration import update_candles_and_signals
except ImportError:
    update_candles_and_signals = None

try:
    from regime_context import (
        compute_scalp_regime_context,
        log_regime_context,
        classify_atr_regime,
    )
except ImportError:
    compute_scalp_regime_context = None
    log_regime_context = None
    classify_atr_regime = None

try:
    from day_type import make_day_type_classifier, DayType
except ImportError:
    make_day_type_classifier = None
    DayType = None

try:
    from zone_detector import load_zones, save_zones
except ImportError:
    load_zones = None
    save_zones = None

# ─────────────────────────────────────────────────────────────────
# ANSI Colors and Logger
# ─────────────────────────────────────────────────────────────────

logger = logging.getLogger(__name__)

RESET   = "\033[0m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
RED     = "\033[91m"
MAGENTA = "\033[95m"
GRAY    = "\033[90m"
CYAN    = "\033[96m"


def _log_green(msg: str) -> None:
    logging.info(f"{GREEN}{msg}{RESET}")


def _log_yellow(msg: str) -> None:
    logging.info(f"{YELLOW}{msg}{RESET}")


def _log_red(msg: str) -> None:
    logging.info(f"{RED}{msg}{RESET}")


# ─────────────────────────────────────────────────────────────────
# Strategy Constants (ALL from execution.py)
# ─────────────────────────────────────────────────────────────────

# ── Scalp Settings ───────────────────────────────────────────────
SCALP_PT_POINTS = 18.0
SCALP_SL_POINTS = 10.0
SCALP_MIN_HOLD_BARS = 2
SCALP_EXTREME_MOVE_ATR_MULT = 0.90
SCALP_ATR_SL_MIN_MULT = 0.60
SCALP_ATR_SL_MAX_MULT = 0.80
SCALP_COOLDOWN_MINUTES = 20
SCALP_HISTORY_MAXLEN = 120
STARTUP_SUPPRESSION_MINUTES = 5
PULSE_TICKRATE_THRESHOLD = 15.0
PAPER_SLIPPAGE_POINTS = 1.5
TRADE_CLASS_SCALP = "SCALP"
TRADE_CLASS_TREND = "TREND"
PARTIAL_TG_QTY_FRAC = 0.50
RESTART_STATE_VERSION = 1

# ── Trend Settings ───────────────────────────────────────────────
TREND_MIN_HOLD_BARS = 3
TREND_EXTREME_MOVE_ATR_MULT = 1.15
MAX_TRADE_TREND = 8
MAX_TRADE_SCALP = 12

# ── Exit Defaults ────────────────────────────────────────────────
DEFAULT_TIME_EXIT_CANDLES = 16
DEFAULT_OSC_RSI_CALL = 75.0
DEFAULT_OSC_RSI_PUT = 25.0
DEFAULT_OSC_CCI_CALL = 130.0
DEFAULT_OSC_CCI_PUT = -130.0
DEFAULT_OSC_WR_CALL = -10.0
DEFAULT_OSC_WR_PUT = -88.0
EMA_STRETCH_BLOCK_MULT = 2.5
EMA_STRETCH_TAG_MULT = 1.8

# ── Partial Exit / Trail ─────────────────────────────────────────
PARTIAL_PT1_QTY_FRAC = 0.40
PARTIAL_PT2_QTY_FRAC = 0.30
TRAIL_STRONG_MULT = 1.5
TRAIL_WEAK_MULT = 0.8
TRAIL_TREND_DAY_MULT = 1.8
RISK_SCALING_TREND = 1.0
RISK_SCALING_RANGE = 0.6
RISK_SCALING_REVERSAL = 0.7

# ── Cooldown ─────────────────────────────────────────────────────
COOLDOWN_SECONDS = 120

# ── Regime Matrix ────────────────────────────────────────────────
REGIME_MATRIX = {
    "TRENDING_DAY": {
        "RSI_FLOOR": 0,
        "COUNTER_PENALTY": -15,
        "COOLDOWN_LOSS": 5,
        "COOLDOWN_WIN": 3,
        "ST_OPENING_RELAX": True,
        "REVERSAL_ALLOWED": True,
        "REVERSAL_SCORE_BONUS": 0,
    },
    "RANGE_DAY": {
        "RSI_FLOOR": None,
        "COUNTER_PENALTY": 0,
        "COOLDOWN_LOSS": 10,
        "COOLDOWN_WIN": 5,
        "ST_OPENING_RELAX": True,
        "REVERSAL_ALLOWED": True,
        "REVERSAL_SCORE_BONUS": 0,
    },
    "GAP_DAY": {
        "RSI_FLOOR": 5,
        "COUNTER_PENALTY": -10,
        "COOLDOWN_LOSS": 10,
        "COOLDOWN_WIN": 5,
        "ST_OPENING_RELAX": True,
        "REVERSAL_ALLOWED": True,
        "REVERSAL_SCORE_BONUS": 10,
    },
    "BALANCE_DAY": {
        "RSI_FLOOR": None,
        "COUNTER_PENALTY": 0,
        "COOLDOWN_LOSS": 7,
        "COOLDOWN_WIN": 5,
        "ST_OPENING_RELAX": False,
        "REVERSAL_ALLOWED": True,
        "REVERSAL_SCORE_BONUS": 0,
    },
    "HIGH_VOL": {
        "RSI_FLOOR": 0,
        "COUNTER_PENALTY": -5,
        "COOLDOWN_LOSS": 7,
        "COOLDOWN_WIN": 3,
        "ST_OPENING_RELAX": True,
        "REVERSAL_ALLOWED": True,
        "REVERSAL_SCORE_BONUS": 0,
    },
}
_REGIME_DEFAULT = {
    "RSI_FLOOR": None, "COUNTER_PENALTY": 0,
    "COOLDOWN_LOSS": 10, "COOLDOWN_WIN": 5,
    "ST_OPENING_RELAX": False, "REVERSAL_ALLOWED": True,
    "REVERSAL_SCORE_BONUS": 0,
}


# ─────────────────────────────────────────────────────────────────
# StrategyConfig dataclass
# ─────────────────────────────────────────────────────────────────

@dataclass
class StrategyConfig:
    """All tunable strategy parameters in one place."""
    # Identity
    strategy_name: str = STRATEGY_NAME
    index_name: str = INDEX_NAME
    symbols: list = field(default_factory=lambda: list(DEFAULT_SYMBOLS))
    exchange: str = EXCHANGE

    # Trade sizing
    quantity: int = DEFAULT_QUANTITY
    lot_size: int = DEFAULT_QUANTITY
    account_type: str = DEFAULT_ACCOUNT_TYPE

    # Moneyness
    call_moneyness: str = CALL_MONEYNESS
    put_moneyness: str = PUT_MONEYNESS
    strike_count: int = STRIKE_COUNT
    strike_diff: int = STRIKE_DIFF

    # Time bounds
    time_zone: Any = TIME_ZONE
    start_hour: int = START_HOUR
    start_min: int = START_MIN
    end_hour: int = END_HOUR
    end_min: int = END_MIN

    # Risk
    max_daily_loss: float = MAX_DAILY_LOSS
    max_drawdown: float = MAX_DRAWDOWN
    max_trades_per_day: int = MAX_TRADES_PER_DAY
    max_trades_trend: int = MAX_TRADE_TREND
    max_trades_scalp: int = MAX_TRADE_SCALP

    # Entry quality
    trend_entry_adx_min: float = TREND_ENTRY_ADX_MIN
    slope_adx_gate: float = SLOPE_ADX_GATE
    time_slope_adx_gate: float = TIME_SLOPE_ADX_GATE
    slope_conflict_time_bars: int = SLOPE_CONFLICT_TIME_BARS
    rr_ratio: float = ST_RR_RATIO
    tg_rr_ratio: float = ST_TG_RR_RATIO

    # Paper
    paper_slippage_pts: float = PAPER_SLIPPAGE_POINTS

    # Oscillator exit
    oscillator_exit_mode: str = OSCILLATOR_EXIT_MODE

    # Scalp
    scalp_pt_points: float = SCALP_PT_POINTS
    scalp_sl_points: float = SCALP_SL_POINTS
    scalp_cooldown_minutes: int = SCALP_COOLDOWN_MINUTES
    pulse_tickrate_threshold: float = PULSE_TICKRATE_THRESHOLD

    # Mode
    mode: str = MODE


# ─────────────────────────────────────────────────────────────────
# Helper functions
# ─────────────────────────────────────────────────────────────────

def _parse_ts(value):
    """Convert various datetime formats to Python datetime."""
    if value is None:
        return None
    pendulum_dt = getattr(dt, "DateTime", None)
    if isinstance(value, datetime) or (pendulum_dt and isinstance(value, pendulum_dt)):
        return value
    try:
        return pd.Timestamp(value).to_pydatetime()
    except Exception:
        return None


def _safe_float(val, default=None):
    """NaN-safe float conversion."""
    if val is None:
        return default
    try:
        f = float(val)
        return default if (np.isnan(f) or np.isinf(f)) else f
    except (ValueError, TypeError):
        return default


def map_status_code(code):
    status_map = {1: "CANCELLED", 2: "TRADED", 4: "TRANSIT",
                  5: "REJECTED", 6: "PENDING", 7: "EXPIRED"}
    return status_map.get(code, str(code))


# ─────────────────────────────────────────────────────────────────
# OptionsBuyingStrategy
# ─────────────────────────────────────────────────────────────────

class OptionsBuyingStrategy:
    """Complete options buying strategy using tbot_core library.

    Replicates all execution.py functionality in a class-based design.
    All module-level globals from execution.py are now instance attributes.
    """

    def __init__(self, config: Optional[StrategyConfig] = None):
        self.config = config or StrategyConfig()
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

        # ── Lazy-loaded live resources ───────────────────────────
        self._setup_loaded = False
        self.df = None
        self.fyers = None
        self.ticker = None
        self.option_chain = None
        self.spot_price = None
        self.start_time = None
        self.end_time = None
        self.hist_data = None

        # ── Session state ────────────────────────────────────────
        self.paper_info: Dict[str, Any] = {}
        self.live_info: Dict[str, Any] = {}
        self.risk_info: Dict[str, Any] = {}
        self.last_signal_candle_time = None
        self.filled_df = pd.DataFrame(
            columns=["status", "filled_qty", "avg_price", "symbol"]
        )
        self.today_str = dt.now(self.config.time_zone).strftime("%Y-%m-%d")

        # ── Module-level state (no longer globals) ───────────────
        self._compression_state = CompressionState()
        self._slope_conflict_bars: Dict[str, int] = {}
        self._paper_zones: list = []
        self._paper_zones_date: str = ""
        self._live_zones: list = []
        self._live_zones_date: str = ""
        self._paper_dtc: Optional[Any] = None
        self._paper_dtc_date: str = ""
        self._live_dtc: Optional[Any] = None
        self._live_dtc_date: str = ""

        # ── Initialize trade info dicts ──────────────────────────
        self._init_trade_info()
        self._init_risk_info()

    def _ensure_setup(self):
        """Import setup.py globals on first use (live/paper mode only)."""
        if self._setup_loaded:
            return
        try:
            from setup import (
                df as _df, fyers as _fyers, ticker as _ticker,
                option_chain as _oc, spot_price as _sp,
                start_time as _st, end_time as _et, hist_data as _hd,
            )
            self.df = _df
            self.fyers = _fyers
            self.ticker = _ticker
            self.option_chain = _oc
            self.spot_price = _sp
            self.start_time = _st
            self.end_time = _et
            self.hist_data = _hd
            self._setup_loaded = True
        except ImportError:
            self.logger.warning("[SETUP] setup.py not available (replay mode?)")

    def _init_trade_info(self):
        """Initialize paper_info and live_info with empty leg dicts."""
        for info_dict in (self.paper_info, self.live_info):
            info_dict.setdefault("call_buy", self._empty_leg_state())
            info_dict.setdefault("put_buy", self._empty_leg_state())
            info_dict.setdefault("trade_count", 0)
            info_dict.setdefault("trend_trade_count", 0)
            info_dict.setdefault("scalp_trade_count", 0)
            info_dict.setdefault("max_trades", self.config.max_trades_per_day)
            info_dict.setdefault("max_trades_trend", self.config.max_trades_trend)
            info_dict.setdefault("max_trades_scalp", self.config.max_trades_scalp)
            info_dict.setdefault("last_exit_time", None)
            info_dict.setdefault("scalp_cooldown_until", None)
            info_dict.setdefault("scalp_last_burst_key", None)
            info_dict.setdefault("osc_hold_until", None)
            info_dict.setdefault("scalp_hist", {"CALL": [], "PUT": []})
            info_dict.setdefault("startup_suppression_until", None)
            info_dict.setdefault("startup_suppression_logged_at", None)

    def _init_risk_info(self):
        """Initialize risk tracking."""
        self.risk_info = {
            "session_pnl": 0.0,
            "peak_equity": 0.0,
            "halt_trading": False,
            "total_pnl": 0.0,
        }

    @staticmethod
    def _empty_leg_state() -> dict:
        """Return a clean trade-leg state dict."""
        return {
            "option_name": None,
            "side": None,
            "buy_price": 0.0,
            "quantity": 0,
            "position_id": None,
            "entry_time": None,
            "entry_candle": 0,
            "position_side": "LONG",
            "trade_flag": 0,
            "is_open": False,
            "lifecycle_state": "EXIT",
            "scalp_mode": False,
            "trade_class": None,
            "stop": None,
            "pt": None,
            "tg": None,
            "trail_step": 0.0,
            "trail_start": 0.0,
            "trail_updates": 0,
            "atr_value": 0.0,
            "time_exit_candles": DEFAULT_TIME_EXIT_CANDLES,
            "hf_exit_manager": None,
            "partial_booked": False,
            "partial_pt_qty": 0,
            "partial_tg_booked": False,
            "partial_tg_qty": 0,
            "consec_count": 0,
            "prev_gap": 0.0,
            "peak_momentum": 0.0,
            "plateau_count": 0,
            "pnl": 0.0,
            "entry_regime_context": None,
            "regime_context": None,
            "day_type": None,
            "adx_tier": None,
            "gap_tag": None,
            "cpr_width": None,
            "filled_df": pd.DataFrame(
                columns=["status", "filled_qty", "avg_price", "symbol"]
            ),
            "osc_rsi_call": DEFAULT_OSC_RSI_CALL,
            "osc_rsi_put": DEFAULT_OSC_RSI_PUT,
            "osc_cci_call": DEFAULT_OSC_CCI_CALL,
            "osc_cci_put": DEFAULT_OSC_CCI_PUT,
            "osc_wr_call": DEFAULT_OSC_WR_CALL,
            "osc_wr_put": DEFAULT_OSC_WR_PUT,
            "scalp_pt_points": SCALP_PT_POINTS,
            "scalp_sl_points": SCALP_SL_POINTS,
            "scalp_extreme_move_atr_mult": SCALP_EXTREME_MOVE_ATR_MULT,
            "trend_extreme_move_atr_mult": TREND_EXTREME_MOVE_ATR_MULT,
        }

    # ── Persistence ──────────────────────────────────────────────

    def _restart_state_file(self, account_type_: str) -> str:
        return f"restart-state-{account_type_.lower()}.pickle"

    def store(self, data: dict, account_type_: str):
        """Append trading state to pickle ledger."""
        filename = f"data-{dt.now(self.config.time_zone).date()}-{account_type_}.pickle"
        try:
            try:
                with open(filename, "rb") as f:
                    ledger = pickle.load(f)
                if isinstance(ledger, dict):
                    ledger = [ledger]
                elif not isinstance(ledger, list):
                    ledger = []
            except Exception:
                ledger = []
            snapshot = {"timestamp": dt.now(self.config.time_zone), "state": data}
            ledger.append(snapshot)
            with open(filename, "wb") as f:
                pickle.dump(ledger, f, protocol=pickle.HIGHEST_PROTOCOL)
            self._save_restart_state(data, account_type_)
        except Exception as e:
            self.logger.error(f"Failed to store state: {e}")

    def load(self, account_type_: str) -> dict:
        """Restore latest trading state from pickle file."""
        filename = f"data-{dt.now(self.config.time_zone).date()}-{account_type_}.pickle"
        with open(filename, "rb") as f:
            ledger = pickle.load(f)
        if isinstance(ledger, list) and ledger:
            return ledger[-1]["state"]
        elif isinstance(ledger, dict):
            return ledger
        raise ValueError("Empty or invalid ledger")

    def _save_restart_state(self, info: dict, account_type_: str):
        """Persist minimal restart state (cooldowns + open positions)."""
        try:
            payload = {
                "version": RESTART_STATE_VERSION,
                "saved_at": dt.now(self.config.time_zone),
                "last_exit_time": info.get("last_exit_time"),
                "scalp_cooldown_until": info.get("scalp_cooldown_until"),
                "startup_suppression_until": info.get("startup_suppression_until"),
                "active_positions": [],
            }
            for leg in ("call_buy", "put_buy"):
                st = info.get(leg, {}) if isinstance(info, dict) else {}
                if st.get("is_open", False) or st.get("trade_flag", 0) == 1:
                    payload["active_positions"].append({
                        "leg": leg,
                        "symbol": st.get("option_name"),
                        "option_type": st.get("side"),
                        "position_side": st.get("position_side", "LONG"),
                        "position_id": st.get("position_id"),
                        "entry_time": st.get("entry_time"),
                    })
            with open(self._restart_state_file(account_type_), "wb") as f:
                pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception as e:
            self.logger.debug(f"[RESTART STATE] save failed: {e}")

    def _load_restart_state(self, account_type_: str) -> dict:
        try:
            with open(self._restart_state_file(account_type_), "rb") as f:
                payload = pickle.load(f)
            if isinstance(payload, dict):
                self.logger.info(
                    f"[RESTART STATE] loaded account={account_type_} "
                    f"saved_at={payload.get('saved_at')} "
                    f"active_positions={len(payload.get('active_positions', []))}"
                )
                return payload
            return {}
        except Exception:
            return {}

    def _hydrate_runtime_state(self, info: dict, account_type_: str, mode_label: str) -> dict:
        """Ensure restart-safe fields and restore cooldown/open-trade context."""
        now = dt.now(self.config.time_zone)
        info.setdefault("last_exit_time", None)
        info.setdefault("scalp_cooldown_until", None)
        info.setdefault("scalp_last_burst_key", None)
        info.setdefault("osc_hold_until", None)
        info.setdefault("scalp_hist", {"CALL": [], "PUT": []})
        info.setdefault("trade_count", 0)
        info.setdefault("trend_trade_count", 0)
        info.setdefault("scalp_trade_count", 0)
        info.setdefault("max_trades", self.config.max_trades_per_day)
        info.setdefault("max_trades_trend", self.config.max_trades_trend)
        info.setdefault("max_trades_scalp", self.config.max_trades_scalp)

        persisted = self._load_restart_state(account_type_)
        last_exit = _parse_ts(info.get("last_exit_time")) or _parse_ts(persisted.get("last_exit_time"))
        scalp_cd = _parse_ts(info.get("scalp_cooldown_until")) or _parse_ts(persisted.get("scalp_cooldown_until"))
        osc_hold = _parse_ts(info.get("osc_hold_until"))
        startup_until = _parse_ts(info.get("startup_suppression_until"))
        if startup_until is None:
            startup_until = now + timedelta(minutes=STARTUP_SUPPRESSION_MINUTES)
        persisted_startup = _parse_ts(persisted.get("startup_suppression_until"))
        if persisted_startup is not None and persisted_startup > startup_until:
            startup_until = persisted_startup

        info["last_exit_time"] = last_exit
        info["scalp_cooldown_until"] = scalp_cd
        info["osc_hold_until"] = osc_hold
        info["startup_suppression_until"] = startup_until
        info.setdefault("startup_suppression_logged_at", None)

        for leg in ("call_buy", "put_buy"):
            st = info.get(leg, {})
            if not isinstance(st, dict):
                continue
            st.setdefault("position_side", "LONG")
            st.setdefault("lifecycle_state", "EXIT")
            st.setdefault("scalp_mode", False)
            is_open = bool(st.get("is_open", False) or st.get("trade_flag", 0) == 1)
            if is_open:
                st["is_open"] = True
                st["trade_flag"] = 1
                st["lifecycle_state"] = "OPEN" if st.get("lifecycle_state") != "HOLD" else "HOLD"
                st["_restored_from_restart"] = True
                if not st.get("_restored_audit_logged", False):
                    restored_id = st.get("position_id", "UNKNOWN")
                    self.logger.info(
                        "[ENTRY][STATE_RESTORED] "
                        f"timestamp={now} symbol={st.get('option_name', 'N/A')} "
                        f"option_type={st.get('side', 'N/A')} position_side={st.get('position_side', 'LONG')} "
                        f"position_id={restored_id} lifecycle={st.get('lifecycle_state')} "
                        f"regime={st.get('regime_context', 'RESTORED')}"
                    )
                    st["_restored_audit_logged"] = True

        self.logger.info(
            f"[RESTART STATE] mode={mode_label} startup_suppression_until={info['startup_suppression_until']} "
            f"last_exit_time={info.get('last_exit_time')} scalp_cooldown_until={info.get('scalp_cooldown_until')}"
        )
        self._save_restart_state(info, account_type_)
        return info

    # ── Trade Capacity ───────────────────────────────────────────

    def _cap_used(self, info: dict, is_scalp: bool) -> int:
        return int(info.get("scalp_trade_count", 0) if is_scalp else info.get("trend_trade_count", 0))

    def _cap_limit(self, info: dict, is_scalp: bool) -> int:
        return int(info.get("max_trades_scalp", MAX_TRADE_SCALP) if is_scalp
                   else info.get("max_trades_trend", MAX_TRADE_TREND))

    def _cap_available(self, info: dict, is_scalp: bool) -> bool:
        return self._cap_used(info, is_scalp) < self._cap_limit(info, is_scalp)

    def _register_trade(self, info: dict, is_scalp: bool):
        info["trade_count"] = int(info.get("trade_count", 0)) + 1
        if is_scalp:
            info["scalp_trade_count"] = int(info.get("scalp_trade_count", 0)) + 1
        else:
            info["trend_trade_count"] = int(info.get("trend_trade_count", 0)) + 1

    # ── Risk Management ──────────────────────────────────────────

    def _update_risk(self, pnl_value: float):
        """Update session PnL and check halt conditions."""
        self.risk_info["session_pnl"] = self.risk_info.get("session_pnl", 0.0) + pnl_value
        self.risk_info["total_pnl"] = self.risk_info.get("total_pnl", 0.0) + pnl_value
        if self.risk_info["total_pnl"] > self.risk_info.get("peak_equity", 0.0):
            self.risk_info["peak_equity"] = self.risk_info["total_pnl"]
        drawdown = self.risk_info["peak_equity"] - self.risk_info["total_pnl"]
        if self.risk_info["total_pnl"] <= self.config.max_daily_loss:
            self.risk_info["halt_trading"] = True
            self.logger.warning(f"[RISK] Daily loss limit hit: {self.risk_info['total_pnl']:.2f}")
        elif drawdown >= abs(self.config.max_drawdown):
            self.risk_info["halt_trading"] = True
            self.logger.warning(f"[RISK] Max drawdown hit: {drawdown:.2f}")

    # ── Startup Suppression ──────────────────────────────────────

    def _is_startup_suppression_active(self, info: dict, now_ts, mode_label: str) -> bool:
        """Check if startup suppression window is active."""
        sup_until = _parse_ts(info.get("startup_suppression_until"))
        if sup_until is None:
            return False
        try:
            now_dt = _parse_ts(now_ts) or dt.now(self.config.time_zone)
            if now_dt < sup_until:
                last_logged = info.get("startup_suppression_logged_at")
                if last_logged is None or (now_dt - last_logged).total_seconds() > 60:
                    self.logger.info(
                        f"[STARTUP_SUPPRESSION] mode={mode_label} active until {sup_until}"
                    )
                    info["startup_suppression_logged_at"] = now_dt
                return True
        except Exception:
            pass
        return False

    # ══════════════════════════════════════════════════════════════════
    # PART 2: Option Selection, Broker Orders, Helper Methods
    # ══════════════════════════════════════════════════════════════════

    def get_option_by_moneyness(self, spot_price_, side, moneyness="ITM", points=0):
        """Select ITM option strike closest to ATM with liquidity check."""
        self._ensure_setup()
        from config import strike_diff

        if spot_price_ is None or pd.isna(spot_price_):
            self.logger.error("[get_option_by_moneyness] Invalid spot price")
            return None, None

        side_ce = "CE" if side in ["CALL", "CE"] else "PE"
        atm_strike = round(spot_price_ / strike_diff) * strike_diff
        strike = (atm_strike - strike_diff) if side_ce == "CE" else (atm_strike + strike_diff)
        strike += points

        self.logger.info(
            f"[DEBUG get_option_by_moneyness] spot={spot_price_}, atm={atm_strike}, "
            f"side={side_ce}, requested_strike={strike}"
        )

        candidates = self.option_chain[
            self.option_chain["option_type"].isin([side_ce, side_ce.replace("E", "ALL")])
        ].copy()
        if candidates.empty:
            self.logger.error(f"[get_option_by_moneyness] No options for side={side_ce}")
            return None, None

        candidates["strike_diff_abs"] = (candidates["strike_price"] - strike).abs()
        candidates = candidates.sort_values("strike_diff_abs")

        selected_symbol = selected_strike = None
        for _, row in candidates.iterrows():
            sym = row["symbol"]
            if self.df is not None and not self.df.empty and sym in self.df.index:
                selected_symbol = sym
                selected_strike = row["strike_price"]
                break

        if not selected_symbol:
            best = candidates.iloc[0]
            selected_symbol = best["symbol"]
            selected_strike = best["strike_price"]

        return selected_symbol, selected_strike

    def _get_option_market_snapshot(self, symbol, fallback_price):
        """Return (option_price, option_volume) from live df."""
        option_price = None
        option_volume = 0.0

        if self.df is not None and symbol in self.df.index:
            try:
                row = self.df.loc[symbol]
                ltp = row.get("ltp", None) if hasattr(row, "get") else row["ltp"]
                if ltp is not None and not pd.isna(ltp):
                    option_price = float(ltp)
                for col in ("volume", "vol_traded_today", "vtt", "last_traded_qty"):
                    v = row.get(col, None) if hasattr(row, "get") else None
                    if v is not None and not pd.isna(v):
                        option_volume = float(v)
                        break
            except Exception:
                pass

        if option_price is None:
            option_price = float(fallback_price) if fallback_price is not None else 0.0
        return option_price, max(0.0, option_volume)

    # ── Scalp Helpers ────────────────────────────────────────────

    def _detect_scalp_dip_rally_signal(self, candles_3m, traditional_levels, atr):
        """Detect scalp dip/rally setups at pivot levels."""
        if len(candles_3m) < 2 or traditional_levels is None or atr is None or atr <= 0:
            return None

        last = candles_3m.iloc[-1]
        rng = last.high - last.low
        if rng == 0:
            return None

        s1 = traditional_levels.get("s1")
        s2 = traditional_levels.get("s2")
        p = traditional_levels.get("pivot")
        r1 = traditional_levels.get("r1")
        r2 = traditional_levels.get("r2")
        atr_buf = 0.25 * atr

        # CALL: Buy on dip at support
        for level, name in [(s1, "S1"), (s2, "S2"), (p, "PIVOT")]:
            if level and last.low <= level + atr_buf and (last.close - last.low) > 0.5 * rng:
                sl_zone = level - (0.5 * atr)
                self.logger.info(
                    f"[SCALP_BUY_DIP] side=CALL reason=REJECTION_{name} "
                    f"zone={name} level={level:.2f} atr={atr:.2f}"
                )
                return {
                    "side": "CALL", "position_type": "LONG",
                    "reason": f"SCALP_BUY_DIP_{name}", "tag": "SCALP_BUY_DIP",
                    "zone": name, "level": float(level),
                    "sl_zone": float(sl_zone), "stop": float(sl_zone),
                }

        # PUT: Sell on rally at resistance
        for level, name in [(r1, "R1"), (r2, "R2"), (p, "PIVOT")]:
            if level and last.high >= level - atr_buf and (last.high - last.close) > 0.5 * rng:
                sl_zone = level + (0.5 * atr)
                self.logger.info(
                    f"[SCALP_SELL_RALLY] side=PUT reason=REJECTION_{name} "
                    f"zone={name} level={level:.2f} atr={atr:.2f}"
                )
                return {
                    "side": "PUT", "position_type": "LONG",
                    "reason": f"SCALP_SELL_RALLY_{name}", "tag": "SCALP_SELL_RALLY",
                    "zone": name, "level": float(level),
                    "sl_zone": float(sl_zone), "stop": float(sl_zone),
                }
        return None

    def _can_enter_scalp(self, info, burst_key, now_ts):
        """Gate scalp re-entry by cooldown and burst uniqueness."""
        cooldown_until = info.get("scalp_cooldown_until")
        if cooldown_until and now_ts < cooldown_until:
            self.logger.info(f"[SCALP ENTRY BLOCKED][COOLDOWN] burst_key={burst_key}")
            return False, "COOLDOWN"
        if info.get("scalp_last_burst_key") == burst_key:
            self.logger.info(f"[SCALP ENTRY BLOCKED][DUPLICATE_BURST] burst_key={burst_key}")
            return False, "DUPLICATE_BURST"
        return True, "OK"

    # ── Broker Order Functions ───────────────────────────────────

    def _send_paper_exit_order(self, symbol, qty, reason):
        """Simulated exit for paper mode."""
        self.logger.info(f"{CYAN}[PAPER EXIT][{reason}] {symbol} Qty={qty}{RESET}")
        return True, f"paper_exit_{symbol}_{reason}"

    def _send_live_entry_order(self, symbol, qty, side, buffer=None):
        """Place a live LIMIT entry order via Fyers API."""
        self._ensure_setup()
        if buffer is None:
            from config import ENTRY_OFFSET
            buffer = ENTRY_OFFSET
        try:
            quote = self.fyers.quotes({"symbols": symbol})
            ltp = quote["d"][0]["v"]["lp"]
            limit_price = max(ltp - buffer, 0.05)
            order_data = {
                "symbol": symbol, "qty": qty, "type": 1, "side": side,
                "productType": "INTRADAY", "limitPrice": limit_price,
                "stopPrice": 0, "validity": "DAY", "stopLoss": 0,
                "takeProfit": 0, "offlineOrder": False,
                "disclosedQty": 0, "isSliceOrder": False,
                "orderTag": str(side),
            }
            response = self.fyers.place_order(data=order_data)
            if response.get("s") == "ok":
                self.logger.info(f"{YELLOW}[LIVE ENTRY] {symbol} Qty={qty}{RESET}")
                return True, response.get("id")
            else:
                self.logger.error(f"[LIVE ENTRY FAILED] {symbol} {response}")
                return False, None
        except Exception as e:
            self.logger.error(f"[LIVE ENTRY ERROR] {symbol} {e}")
            return False, None

    def _send_live_exit_order(self, symbol, qty, reason):
        """Place a live MARKET exit order via Fyers API."""
        self._ensure_setup()
        try:
            order_data = {
                "symbol": symbol, "qty": qty, "type": 2, "side": -1,
                "productType": "INTRADAY", "limitPrice": 0,
                "stopPrice": 0, "validity": "DAY", "stopLoss": 0,
                "takeProfit": 0, "offlineOrder": False,
                "disclosedQty": 0, "isSliceOrder": False,
                "orderTag": str(reason),
            }
            response = self.fyers.place_order(data=order_data)
            if response.get("s") == "ok":
                self.logger.info(
                    f"{YELLOW}[LIVE EXIT][{reason}] {symbol} Qty={qty} "
                    f"OrderID={response.get('id')}{RESET}"
                )
                return True, response.get("id")
            else:
                self.logger.error(f"[LIVE EXIT FAILED] {symbol} {response}")
                return False, None
        except Exception as e:
            self.logger.error(f"[LIVE EXIT ERROR] {symbol} {e}")
            return False, None

    def _update_order_status(self, order_id, status, filled_qty, avg_price, symbol):
        """Update the filled_df ledger with order status."""
        if order_id in self.filled_df.index:
            self.filled_df.loc[order_id, "status"] = status
            self.filled_df.loc[order_id, "filled_qty"] = filled_qty
            self.filled_df.loc[order_id, "avg_price"] = avg_price
        else:
            new_row = pd.DataFrame(
                {"status": [status], "filled_qty": [filled_qty],
                 "avg_price": [avg_price], "symbol": [symbol]},
                index=[order_id],
            )
            self.filled_df = pd.concat([self.filled_df, new_row])

    # ══════════════════════════════════════════════════════════════════
    # PART 3: Core Strategy Logic — Quality Gate, Exits, Levels
    # ══════════════════════════════════════════════════════════════════
    #
    # These delegate to execution.py's battle-tested implementations
    # to ensure identical behavior. The delegation imports are done
    # lazily to avoid circular imports and setup.py side effects.
    # ══════════════════════════════════════════════════════════════════

    def _get_execution_module(self):
        """Lazy-load execution module for delegation."""
        if not hasattr(self, "_exec_module"):
            self._exec_module = _il.import_module("execution")
        return self._exec_module

    def quality_gate(
        self,
        candles_3m,
        candles_15m,
        timestamp,
        symbol,
        adx_min=None,
        rsi_min=30,
        rsi_max=70,
        cci_min=-150,
        cci_max=150,
        cpr_levels=None,
        camarilla_levels=None,
        reversal_signal=None,
        failed_breakout_signal=None,
        day_type_result=None,
        open_bias_context=None,
        daily_camarilla_levels=None,
    ) -> Tuple[bool, Optional[str], str, dict]:
        """Trend entry quality gate — delegates to execution._trend_entry_quality_gate.

        Returns: (quality_ok, allowed_side, reason, st_details)
        """
        if adx_min is None:
            adx_min = float(self.config.trend_entry_adx_min)
        exe = self._get_execution_module()
        return exe._trend_entry_quality_gate(
            candles_3m=candles_3m,
            candles_15m=candles_15m,
            timestamp=timestamp,
            symbol=symbol,
            adx_min=adx_min,
            rsi_min=rsi_min,
            rsi_max=rsi_max,
            cci_min=cci_min,
            cci_max=cci_max,
            cpr_levels=cpr_levels,
            camarilla_levels=camarilla_levels,
            reversal_signal=reversal_signal,
            failed_breakout_signal=failed_breakout_signal,
            day_type_result=day_type_result,
            open_bias_context=open_bias_context,
            daily_camarilla_levels=daily_camarilla_levels,
        )

    def check_exit_condition(
        self, df_slice, state, option_price=None, option_volume=0.0, timestamp=None
    ) -> Tuple[bool, Optional[str]]:
        """Check all exit conditions with strict precedence.

        Delegates to execution.check_exit_condition for identical behavior.
        Returns: (triggered, reason)
        """
        exe = self._get_execution_module()
        return exe.check_exit_condition(
            df_slice, state,
            option_price=option_price,
            option_volume=option_volume,
            timestamp=timestamp,
        )

    def build_dynamic_levels(
        self,
        entry_price,
        atr,
        side,
        entry_candle,
        rr_ratio=2.0,
        profit_loss_point=5,
        candles_df=None,
        trail_start_frac=0.5,
        adx_value=0.0,
    ) -> dict:
        """Build regime-adaptive SL/PT/TG levels.

        Delegates to execution.build_dynamic_levels for identical behavior.
        """
        exe = self._get_execution_module()
        return exe.build_dynamic_levels(
            entry_price, atr, side, entry_candle,
            rr_ratio=rr_ratio,
            profit_loss_point=profit_loss_point,
            candles_df=candles_df,
            trail_start_frac=trail_start_frac,
            adx_value=adx_value,
        )

    def _update_trailing_stop(
        self, current_price, entry_price, current_stop,
        trail_start_pnl, trail_step_points, buffer_points=None,
        atr=None, side="CALL", state=None,
    ):
        """Update trailing stop once partial target booked."""
        exe = self._get_execution_module()
        return exe.update_trailing_stop(
            current_price, entry_price, current_stop,
            trail_start_pnl, trail_step_points,
            buffer_points=buffer_points,
            atr=atr, side=side, state=state,
        )

    # ══════════════════════════════════════════════════════════════════
    # PART 4: Process Order — Exit Management
    # ══════════════════════════════════════════════════════════════════

    def process_order(
        self, state, df_slice, info, spot_price,
        account_type="paper", mode="LIVE",
    ) -> Tuple[bool, Optional[str]]:
        """Manage exits for an active trade using SL/Target + hybrid exit logic.

        Replicates execution.process_order as an instance method.
        """
        side = state["side"]
        position_side = state.get("position_side", "LONG")
        symbol = state.get("option_name", "N/A")
        position_id = state.get("position_id", "UNKNOWN")
        entry = state.get("buy_price", 0)
        qty = state.get("quantity", 0)
        entry_candle = state.get("entry_candle", 0)
        current_candle = df_slice.iloc[-1]
        bars_held = len(df_slice) - 1 - entry_candle

        if not state.get("is_open", False):
            self.logger.info(
                f"[EXIT REJECTED] symbol={symbol} position_id={position_id} "
                f"reason=POSITION_ALREADY_CLOSED"
            )
            return False, None
        state["lifecycle_state"] = "HOLD"

        # Get option premium + volume snapshot
        current_option_price, option_volume = self._get_option_market_snapshot(
            symbol, spot_price
        )
        timestamp = df_slice.iloc[-1].get(
            "time", dt.now(self.config.time_zone)
        ) if not df_slice.empty else dt.now(self.config.time_zone)

        # Hybrid exit logic
        triggered, reason = self.check_exit_condition(
            df_slice, state,
            option_price=current_option_price,
            option_volume=option_volume,
            timestamp=timestamp,
        )
        exit_reason = reason if triggered and reason else None

        if not exit_reason:
            check_count = state.get("exit_check_count", 0)
            if check_count % 5 == 0:
                self.logger.info(
                    f"{CYAN}[EXIT CHECK] {side} {symbol} bars_held={bars_held} "
                    f"ltp={current_option_price:.2f} SL={state.get('stop', 'N/A')} "
                    f"PT={state.get('pt', 'N/A')} TG={state.get('tg', 'N/A')}{RESET}"
                )
            state["exit_check_count"] = check_count + 1
            return False, None

        # ── Partial exits ────────────────────────────────────────
        if exit_reason in {"TG_PARTIAL_EXIT", "PT1_PARTIAL_EXIT", "PT2_PARTIAL_EXIT"}:
            if exit_reason == "PT1_PARTIAL_EXIT":
                partial_qty = state.get("partial_pt_qty", max(1, qty // 2))
            elif exit_reason == "PT2_PARTIAL_EXIT":
                partial_qty = state.get("partial_tg_qty", max(1, qty // 3))
            else:
                partial_qty = state.get("partial_tg_qty", max(1, qty // 2))
            if qty > 1:
                partial_qty = min(qty - 1, partial_qty)
            remaining = max(0, qty - partial_qty)

            if account_type.lower() == "paper":
                success, order_id = self._send_paper_exit_order(symbol, partial_qty, exit_reason)
            elif mode == "LIVE":
                success, order_id = self._send_live_exit_order(symbol, partial_qty, exit_reason)
            else:
                success, order_id = True, "REPLAY_PARTIAL"

            if success:
                exit_price = current_option_price or current_candle["close"]
                if account_type.lower() == "paper":
                    exit_price = max(0.05, exit_price - self.config.paper_slippage_pts)
                pnl_points = exit_price - entry
                pnl_value = pnl_points * partial_qty

                trade = info["call_buy"] if side == "CALL" else info["put_buy"]
                trade["pnl"] = trade.get("pnl", 0) + pnl_value
                trade["quantity"] = remaining
                state["quantity"] = remaining
                info["total_pnl"] = info["call_buy"].get("pnl", 0) + info["put_buy"].get("pnl", 0)

                trade["filled_df"].loc[dt.now(self.config.time_zone)] = {
                    "ticker": symbol, "price": exit_price, "action": "PARTIAL_EXIT",
                    "stop_price": entry, "take_profit": pnl_value,
                    "spot_price": spot_price, "quantity": partial_qty,
                }
                self.logger.info(
                    f"{GREEN}[PARTIAL_EXIT][{account_type.upper()} {exit_reason}] "
                    f"{side} {symbol} partial_qty={partial_qty} remaining={remaining} "
                    f"Entry={entry:.2f} Exit={exit_price:.2f} PnL={pnl_value:.2f}{RESET}"
                )
                if mode == "LIVE":
                    self._update_order_status(order_id, "PENDING", partial_qty, exit_price, symbol)
            return success, exit_reason

        # ── Full exit ────────────────────────────────────────────
        if account_type.lower() == "paper":
            success, order_id = self._send_paper_exit_order(symbol, qty, exit_reason)
        elif mode == "LIVE":
            success, order_id = self._send_live_exit_order(symbol, qty, exit_reason)
        else:
            success, order_id = True, "REPLAY_ORDER"

        if success:
            exit_price = current_option_price or current_candle["close"]
            if account_type.lower() == "paper":
                exit_price = max(0.05, exit_price - self.config.paper_slippage_pts)
            pnl_points = exit_price - entry
            pnl_value = pnl_points * qty

            trade = info["call_buy"] if side == "CALL" else info["put_buy"]
            trade["pnl"] = trade.get("pnl", 0) + pnl_value
            info["total_pnl"] = info["call_buy"].get("pnl", 0) + info["put_buy"].get("pnl", 0)
            trade["trade_flag"] = 0
            trade["quantity"] = 0

            trade["filled_df"].loc[dt.now(self.config.time_zone)] = {
                "ticker": symbol, "price": exit_price, "action": "EXIT",
                "stop_price": entry, "take_profit": pnl_value,
                "spot_price": spot_price, "quantity": qty,
            }

            bars_held = len(df_slice) - 1 - state.get("entry_candle", len(df_slice) - 1)
            self.logger.info(
                f"{YELLOW}[EXIT][{account_type.upper()} {exit_reason}] {side} {symbol} "
                f"Entry={entry:.2f} Exit={exit_price:.2f} Qty={qty} PnL={pnl_value:.2f} "
                f"BarsHeld={bars_held} PositionId={position_id}{RESET}"
            )

            state["is_open"] = False
            state["lifecycle_state"] = "EXIT"

            # P1-C: compression breakout loss → cooldown
            if state.get("source") == "COMPRESSION_BREAKOUT" and pnl_value < 0:
                self._compression_state.notify_trade_result(is_loss=True)

            if state.get("scalp_mode", False):
                info["scalp_cooldown_until"] = (
                    dt.now(self.config.time_zone) + timedelta(minutes=SCALP_COOLDOWN_MINUTES)
                )

            if mode == "LIVE":
                self._update_order_status(order_id, "PENDING", qty, exit_price, symbol)

            self._update_risk(pnl_value)
            return True, exit_reason

        return False, None

    def cleanup_trade_exit(
        self, info, leg, side, name, qty, exit_price, mode, reason,
    ):
        """Unified cleanup for any exit (SL, Target, EOD, Force)."""
        ct = dt.now(self.config.time_zone)

        if exit_price is None or (isinstance(exit_price, float) and pd.isna(exit_price)):
            if self.df is not None and name in self.df.index:
                try:
                    exit_price = float(self.df.loc[name, "ltp"])
                except Exception:
                    exit_price = self.spot_price if self.spot_price else 0
            else:
                exit_price = self.spot_price if self.spot_price else 0

        st = info[leg]
        st["trade_flag"] = 0
        st["quantity"] = 0
        st["is_open"] = False
        st["lifecycle_state"] = "EXIT"

        st["filled_df"].loc[ct] = {
            "ticker": name, "price": exit_price, "action": "EXIT",
            "stop_price": st.get("buy_price", 0), "take_profit": 0,
            "spot_price": self.spot_price or 0, "quantity": qty,
        }

        self.logger.info(
            f"{RED}[EXIT][{mode}] {side} {name} Qty={qty} Price={exit_price:.2f} "
            f"Reason={reason}{RESET}"
        )
        self.logger.info(
            f"[EXIT AUDIT] timestamp={ct} symbol={name} option_type={side} "
            f"position_side={st.get('position_side', 'LONG')} "
            f"exit_type={reason} reason={reason} "
            f"position_id={st.get('position_id', 'UNKNOWN')}"
        )

    # ══════════════════════════════════════════════════════════════════
    # PART 5: Opening Context & Day Type Helpers
    # ══════════════════════════════════════════════════════════════════

    def _opening_s4_breakdown_context(self, candles_3m, cpr_levels, camarilla_levels, atr, timestamp):
        """Analyze opening S4 breakdown for early-session trades."""
        exe = self._get_execution_module()
        return exe._opening_s4_breakdown_context(candles_3m, cpr_levels, camarilla_levels, atr, timestamp)

    def _entry_gate_context(self, allowed_side, zone_tag, gap_tag, atr_stretch,
                            rsi_bounds, cci_bounds, day_type_result, open_bias_context):
        """Merge day/opening context with oscillator zone context."""
        exe = self._get_execution_module()
        return exe.entry_gate_context(
            allowed_side, zone_tag, gap_tag, atr_stretch,
            rsi_bounds, cci_bounds, day_type_result, open_bias_context,
        )

    # ══════════════════════════════════════════════════════════════════
    # PART 6: Paper Order — Paper Trading Orchestrator
    # ══════════════════════════════════════════════════════════════════

    def run_paper(
        self,
        candles_3m: pd.DataFrame,
        candles_15m: Optional[pd.DataFrame] = None,
        exit: bool = False,
        mode: str = "REPLAY",
        spot_price: Optional[float] = None,
    ) -> Optional[dict]:
        """Paper trading orchestrator — handles exits, entries, risk gates.

        Equivalent to execution.paper_order() but as an instance method.
        """
        info = self.paper_info
        tz = self.config.time_zone

        # Safety: clear lingering trade_flag=2
        for leg in ("call_buy", "put_buy"):
            if info.get(leg, {}).get("trade_flag") == 2:
                info[leg]["trade_flag"] = 0

        # Resolve spot price
        if candles_3m is not None and not candles_3m.empty:
            spot = candles_3m.iloc[-1]["close"]
        elif spot_price is not None:
            spot = spot_price
        else:
            spot = self.spot_price or 0.0

        ct = dt.now(tz) if mode != "REPLAY" else (
            pd.Timestamp(candles_3m.iloc[-1].get("time", dt.now(tz)))
            if candles_3m is not None and not candles_3m.empty else dt.now(tz)
        )

        # EOD force exit
        end_t = self.end_time
        if end_t is None:
            end_t = dt.now(tz).set(hour=self.config.end_hour, minute=self.config.end_min, second=0)
        if ct > end_t:
            for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
                st = info.get(leg, {})
                if st.get("trade_flag", 0) == 1:
                    name = st.get("option_name", "")
                    qty = st.get("quantity", 0)
                    self.cleanup_trade_exit(info, leg, side, name, qty, spot, mode, "EOD")
            return None

        # ── EXIT MANAGEMENT (unconditional — every call) ─────────
        for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
            st = info.get(leg, {})
            if st.get("trade_flag", 0) == 1:
                success, reason = self.process_order(
                    st, candles_3m, info, spot,
                    account_type="paper", mode=mode,
                )
                if success:
                    info["last_exit_time"] = ct
                    self.store(info, self.config.account_type)
                    return {"exit": True, "reason": reason, "side": side}

        # ── ENTRY SIGNAL EVALUATION ──────────────────────────────

        # De-duplication gate
        candle_time = candles_3m.iloc[-1].get("time") if not candles_3m.empty else None
        if candle_time and candle_time == self.last_signal_candle_time:
            return None
        self.last_signal_candle_time = candle_time

        # Risk gate
        if self.risk_info.get("halt_trading", False):
            return None

        # ATR resolution
        atr_val, atr_src = resolve_atr(candles_3m)
        if atr_val is None or atr_val <= 0:
            return None

        # Pivot levels from previous bar
        pivot_src = candles_3m.iloc[-2] if len(candles_3m) >= 2 else candles_3m.iloc[-1]
        cpr_pre = calculate_cpr(pivot_src["high"], pivot_src["low"], pivot_src["close"])
        trad_pre = calculate_traditional_pivots(pivot_src["high"], pivot_src["low"], pivot_src["close"])
        cam_pre = calculate_camarilla_pivots(pivot_src["high"], pivot_src["low"], pivot_src["close"])

        # Day type classifier (once per day)
        today_date = ct.strftime("%Y-%m-%d") if hasattr(ct, "strftime") else str(ct)[:10]
        if self._paper_dtc_date != today_date:
            if make_day_type_classifier is not None:
                prev = candles_3m.iloc[-2] if len(candles_3m) >= 2 else candles_3m.iloc[-1]
                self._paper_dtc = make_day_type_classifier(
                    cam_pre, cpr_pre,
                    prev["high"], prev["low"], prev["close"],
                )
            self._paper_dtc_date = today_date
        if self._paper_dtc is not None:
            self._paper_dtc.update(candles_3m)
        day_type_result = self._paper_dtc.result if self._paper_dtc else DayTypeResult()
        day_type_tag = str(getattr(day_type_result, "name", "UNKNOWN"))
        if hasattr(day_type_tag, "value"):
            day_type_tag = day_type_tag.value

        # Opening S4/R4 breakdown
        breakdown_ctx = self._opening_s4_breakdown_context(
            candles_3m, cpr_pre, cam_pre, atr_val, ct,
        )

        # Cooldown enforcement
        last_exit_t = _parse_ts(info.get("last_exit_time"))
        if last_exit_t is not None:
            elapsed = (ct - last_exit_t).total_seconds() if hasattr(ct, "__sub__") else COOLDOWN_SECONDS + 1
            if elapsed < COOLDOWN_SECONDS:
                # Check reversal/opening bypass
                rev_sig = detect_reversal(candles_3m, cam_pre, current_time=ct, day_type_tag=day_type_tag)
                if rev_sig and rev_sig.get("score", 0) >= 70 and elapsed >= 30:
                    self.logger.info("[COOLDOWN_BYPASS][REVERSAL]")
                elif breakdown_ctx and breakdown_ctx.get("active"):
                    self.logger.info("[COOLDOWN_BYPASS][OPENING_BREAKDOWN]")
                else:
                    return None

        # Startup suppression
        if self._is_startup_suppression_active(info, ct, "PAPER"):
            return None

        # OSC extreme hold
        osc_hold = _parse_ts(info.get("osc_hold_until"))
        if osc_hold and ct < osc_hold:
            return None

        # Position conflict
        for leg in ("call_buy", "put_buy"):
            if info.get(leg, {}).get("trade_flag", 0) == 1:
                return None  # Already have an open position

        # ── SCALP ENTRY FLOW ─────────────────────────────────────
        pulse = get_pulse_module()
        pulse_metrics = pulse.get_pulse() if pulse else PulseMetrics(
            tick_rate=0, avg_interval_ms=0, burst_flag=False,
            direction_drift="NEUTRAL", last_tick_time_ms=0,
            price_change_pct=0, tick_count=0,
        )

        if pulse_metrics.burst_flag and pulse_metrics.tick_rate >= PULSE_TICKRATE_THRESHOLD:
            scalp_sig = self._detect_scalp_dip_rally_signal(candles_3m, trad_pre, atr_val)
            if scalp_sig:
                scalp_side = scalp_sig["side"]
                drift = pulse_metrics.direction_drift
                if (scalp_side == "CALL" and drift == "UP") or (scalp_side == "PUT" and drift == "DOWN"):
                    last_ct = candles_3m.iloc[-1].get("time", "")
                    burst_key = f"{scalp_side}:{last_ct}"
                    can_enter, block_reason = self._can_enter_scalp(info, burst_key, ct)
                    if can_enter and self._cap_available(info, is_scalp=True):
                        opt_name, opt_strike = self.get_option_by_moneyness(
                            spot, scalp_side,
                            moneyness=self.config.call_moneyness if scalp_side == "CALL" else self.config.put_moneyness,
                        )
                        if opt_name:
                            leg = "call_buy" if scalp_side == "CALL" else "put_buy"
                            base_entry = spot
                            entry_price = base_entry + self.config.paper_slippage_pts
                            atr_sl = max(SCALP_SL_POINTS, atr_val * SCALP_ATR_SL_MIN_MULT)

                            st = info[leg]
                            st.update({
                                "option_name": opt_name,
                                "side": scalp_side,
                                "buy_price": entry_price,
                                "quantity": self.config.quantity,
                                "trade_flag": 1,
                                "is_open": True,
                                "lifecycle_state": "OPEN",
                                "position_side": "LONG",
                                "entry_time": ct,
                                "entry_candle": len(candles_3m) - 1,
                                "scalp_mode": True,
                                "trade_class": TRADE_CLASS_SCALP,
                                "stop": max(1.0, entry_price - atr_sl),
                                "pt": entry_price + SCALP_PT_POINTS,
                                "tg": entry_price + SCALP_PT_POINTS * 1.5,
                                "atr_value": atr_val,
                                "position_id": f"SCALP-{scalp_side}-{ct}",
                                "hf_exit_manager": OptionExitManager(entry_price, scalp_side),
                                "pnl": 0.0,
                            })
                            info["scalp_last_burst_key"] = burst_key
                            self._register_trade(info, is_scalp=True)
                            _log_green(
                                f"[SCALP ENTRY][PAPER] {scalp_side} {opt_name} "
                                f"entry={entry_price:.2f} SL={st['stop']:.2f}"
                            )
                            return {"entry": True, "side": scalp_side, "type": "SCALP"}

        # ── TREND ENTRY FLOW ─────────────────────────────────────

        # Failed breakout detection
        fb_sig = detect_failed_breakout(candles_3m, cam_pre)

        # Gap/bias classification
        last_close = candles_3m.iloc[-1]["close"]
        r3 = cam_pre.get("r3", 0)
        s3 = cam_pre.get("s3", 99999)
        if last_close > r3:
            gap_tag = "GAP_UP"
        elif last_close < s3:
            gap_tag = "GAP_DOWN"
        else:
            gap_tag = "NO_GAP"

        # Reversal detection
        rev_sig = detect_reversal(candles_3m, cam_pre, current_time=ct, day_type_tag=day_type_tag)

        # Late entry gate (14:45+)
        late_hour = 14
        late_min = 45
        if hasattr(ct, "hour") and (ct.hour > late_hour or (ct.hour == late_hour and ct.minute >= late_min)):
            self.logger.info("[ENTRY BLOCKED][LATE_ENTRY]")
            return None

        # Quality gate
        quality_ok, allowed_side, gate_reason, st_details = self.quality_gate(
            candles_3m=candles_3m,
            candles_15m=candles_15m if candles_15m is not None else pd.DataFrame(),
            timestamp=ct,
            symbol=self.config.symbols[0] if self.config.symbols else "NSE:NIFTY50-INDEX",
            cpr_levels=cpr_pre,
            camarilla_levels=cam_pre,
            reversal_signal=rev_sig,
            failed_breakout_signal=fb_sig,
            day_type_result=day_type_result,
            open_bias_context=breakdown_ctx,
            daily_camarilla_levels=cam_pre,
        )

        if not quality_ok:
            return None

        # Reversal override for allowed side
        if rev_sig and rev_sig.get("score", 0) >= 80:
            rev_side = rev_sig.get("side")
            if rev_side and rev_side != allowed_side:
                allowed_side = rev_side
                self.logger.info(
                    f"[ENTRY ALLOWED][REVERSAL_OVERRIDE_ALLOWEDSIDE] "
                    f"flipped to {allowed_side}"
                )

        # Zone revisit detection (once per day)
        zone_signal = None
        if candles_15m is not None and not candles_15m.empty:
            if self._paper_zones_date != today_date:
                self._paper_zones = detect_zones(candles_15m)
                self._paper_zones_date = today_date
            if self._paper_zones:
                update_zone_activity(self._paper_zones, last_close, atr_val, str(ct))
                zone_signal = detect_zone_revisit(candles_3m, self._paper_zones, atr_val)

        # Compression breakout
        if candles_15m is not None and len(candles_15m) >= 3:
            self._compression_state.update(candles_15m)
        if self._compression_state.has_entry:
            comp_sig = self._compression_state.entry_signal
            comp_side = comp_sig.get("side")
            if comp_side == allowed_side or allowed_side is None:
                self.logger.info(f"[COMPRESSION_ENTRY] dispatching {comp_side}")
                self._compression_state.consume_entry()
                # Route compression entry same as trend entry below

        # VWAP + ORB
        tpma = calculate_vwap(candles_3m) if len(candles_3m) >= 5 else None
        orb_h, orb_l = get_opening_range(candles_3m) if len(candles_3m) >= 5 else (None, None)

        # Main signal detection
        signal = detect_signal(
            candles_3m, candles_15m if candles_15m is not None else pd.DataFrame(),
            cpr_pre, cam_pre, trad_pre, atr_val,
            include_partial=False,
            current_time=ct,
            vwap=tpma,
            orb_high=orb_h,
            orb_low=orb_l,
            day_type_result=day_type_result,
            osc_relief_active=st_details.get("osc_relief_override", False),
            zone_signal=zone_signal,
            pulse_metrics=pulse_metrics,
            daily_camarilla_levels=cam_pre,
            st_details=st_details,
        )

        if signal is None:
            return None

        sig_side = signal.get("side")
        if sig_side != allowed_side:
            if rev_sig and rev_sig.get("side") == sig_side and rev_sig.get("score", 0) >= 70:
                self.logger.info(f"[ENTRY ALLOWED][REVERSAL_OVERRIDE_ST] {sig_side}")
            else:
                self.logger.info(f"[ENTRY BLOCKED][ST_SIDE_MISMATCH] signal={sig_side} allowed={allowed_side}")
                return None

        # Capacity check
        if not self._cap_available(info, is_scalp=False):
            self.logger.info("[ENTRY BLOCKED][MAX_TRADES_CAP]")
            return None

        # Get option
        opt_name, opt_strike = self.get_option_by_moneyness(
            spot, sig_side,
            moneyness=self.config.call_moneyness if sig_side == "CALL" else self.config.put_moneyness,
        )
        if not opt_name:
            return None

        # Entry price + slippage
        base_entry = spot
        entry_price = base_entry + self.config.paper_slippage_pts

        # ADX for level building
        _adx = _safe_float(candles_3m.iloc[-1].get("adx14", 0), 0)

        # Build dynamic levels
        levels = self.build_dynamic_levels(
            entry_price, atr_val, sig_side,
            entry_candle=len(candles_3m) - 1,
            candles_df=candles_3m,
            adx_value=_adx,
        )
        if not levels.get("valid", False):
            self.logger.info("[ENTRY BLOCKED][INVALID_LEVELS]")
            return None

        # RegimeContext
        _rc = None
        if compute_regime_context is not None:
            try:
                _rc = compute_regime_context(st_details, day_type_result, atr_val, _adx)
            except Exception:
                _rc = None

        # Populate state
        leg = "call_buy" if sig_side == "CALL" else "put_buy"
        st = info[leg]
        st.update({
            "option_name": opt_name,
            "side": sig_side,
            "buy_price": entry_price,
            "quantity": self.config.quantity,
            "trade_flag": 1,
            "is_open": True,
            "lifecycle_state": "OPEN",
            "position_side": "LONG",
            "entry_time": ct,
            "entry_candle": len(candles_3m) - 1,
            "scalp_mode": False,
            "trade_class": TRADE_CLASS_TREND,
            "stop": levels["stop"],
            "pt": levels["pt"],
            "tg": levels["tg"],
            "trail_step": levels["trail_step"],
            "trail_start": levels["trail_start"],
            "trail_updates": 0,
            "atr_value": atr_val,
            "position_id": f"TREND-{sig_side}-{ct}",
            "hf_exit_manager": OptionExitManager(entry_price, sig_side),
            "entry_regime_context": _rc,
            "regime_context": levels.get("regime", ""),
            "day_type": day_type_tag,
            "adx_tier": levels.get("sl_tier", ""),
            "gap_tag": gap_tag,
            "cpr_width": classify_cpr_width(cpr_pre, spot),
            "pnl": 0.0,
            "partial_booked": False,
            "partial_pt_qty": max(1, self.config.quantity * PARTIAL_PT1_QTY_FRAC),
            "partial_tg_qty": max(1, self.config.quantity * PARTIAL_PT2_QTY_FRAC),
            "partial_tg_booked": False,
            "consec_count": 0,
            "prev_gap": 0.0,
            "peak_momentum": signal.get("momentum", 0),
            "plateau_count": 0,
            "score": signal.get("score", 0),
            "strength": signal.get("strength", ""),
            "source": signal.get("source", "PIVOT"),
            "pivot_reason": signal.get("pivot_reason"),
        })

        # Apply day type to oscillator thresholds
        if self._paper_dtc and apply_day_type_to_threshold:
            try:
                apply_day_type_to_threshold(st, day_type_result, sig_side)
            except Exception:
                pass

        self._register_trade(info, is_scalp=False)
        self.store(info, self.config.account_type)

        _log_green(
            f"[ENTRY][PAPER] {sig_side} {opt_name} entry={entry_price:.2f} "
            f"SL={levels['stop']:.2f} PT={levels['pt']:.2f} TG={levels['tg']:.2f} "
            f"score={signal.get('score', 0)} strength={signal.get('strength', '')} "
            f"regime={levels.get('regime', '')}"
        )
        self.logger.info(
            f"[ENTRY][NEW] timestamp={ct} symbol={opt_name} "
            f"option_type={sig_side} position_side=LONG "
            f"position_id={st['position_id']} "
            f"score={signal.get('score', 0)} regime={levels.get('regime', '')}"
        )

        return {"entry": True, "side": sig_side, "type": "TREND", "signal": signal}

    # ══════════════════════════════════════════════════════════════════
    # PART 7: Live Order — Live Trading Orchestrator
    # ══════════════════════════════════════════════════════════════════

    def run_live(
        self,
        candles_3m: pd.DataFrame,
        candles_15m: Optional[pd.DataFrame] = None,
        exit: bool = False,
    ) -> Optional[dict]:
        """Live trading orchestrator — real broker orders via Fyers API.

        Structure mirrors run_paper() but with live order routing.
        """
        self._ensure_setup()
        info = self.live_info
        tz = self.config.time_zone

        # Resolve spot from broker
        try:
            quote = self.fyers.quotes({"symbols": self.ticker})
            spot = quote["d"][0]["v"]["lp"]
        except Exception:
            spot = candles_3m.iloc[-1]["close"] if candles_3m is not None and not candles_3m.empty else 0.0

        ct = dt.now(tz)

        # EOD force exit
        if self.end_time and ct > self.end_time:
            for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
                st = info.get(leg, {})
                if st.get("trade_flag", 0) == 1:
                    name = st.get("option_name", "")
                    qty = st.get("quantity", 0)
                    self._send_live_exit_order(name, qty, "EOD")
                    self.cleanup_trade_exit(info, leg, side, name, qty, None, "LIVE", "EOD")
            return None

        # EXIT MANAGEMENT (unconditional)
        for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
            st = info.get(leg, {})
            if st.get("trade_flag", 0) == 1:
                success, reason = self.process_order(
                    st, candles_3m, info, spot,
                    account_type="live", mode="LIVE",
                )
                if success:
                    info["last_exit_time"] = ct
                    self.store(info, self.config.account_type)
                    return {"exit": True, "reason": reason, "side": side}

        # Entry: delegate to paper logic with live order routing
        # (The entry logic is identical to paper, only the order routing differs)
        candle_time = candles_3m.iloc[-1].get("time") if not candles_3m.empty else None
        if candle_time and candle_time == self.last_signal_candle_time:
            return None
        self.last_signal_candle_time = candle_time

        if self.risk_info.get("halt_trading", False):
            return None

        # ATR + pivots
        atr_val, _ = resolve_atr(candles_3m)
        if atr_val is None or atr_val <= 0:
            return None

        pivot_src = candles_3m.iloc[-2] if len(candles_3m) >= 2 else candles_3m.iloc[-1]
        cpr_pre = calculate_cpr(pivot_src["high"], pivot_src["low"], pivot_src["close"])
        trad_pre = calculate_traditional_pivots(pivot_src["high"], pivot_src["low"], pivot_src["close"])
        cam_pre = calculate_camarilla_pivots(pivot_src["high"], pivot_src["low"], pivot_src["close"])

        # Day type
        today_date = ct.strftime("%Y-%m-%d")
        if self._live_dtc_date != today_date:
            if make_day_type_classifier is not None:
                prev = candles_3m.iloc[-2] if len(candles_3m) >= 2 else candles_3m.iloc[-1]
                self._live_dtc = make_day_type_classifier(
                    cam_pre, cpr_pre, prev["high"], prev["low"], prev["close"],
                )
            self._live_dtc_date = today_date
        if self._live_dtc is not None:
            self._live_dtc.update(candles_3m)
        day_type_result = self._live_dtc.result if self._live_dtc else DayTypeResult()
        day_type_tag = str(getattr(day_type_result, "name", "UNKNOWN"))

        # Cooldown
        last_exit_t = _parse_ts(info.get("last_exit_time"))
        if last_exit_t:
            elapsed = (ct - last_exit_t).total_seconds()
            if elapsed < COOLDOWN_SECONDS:
                return None

        if self._is_startup_suppression_active(info, ct, "LIVE"):
            return None

        # Position conflict
        for leg in ("call_buy", "put_buy"):
            if info.get(leg, {}).get("trade_flag", 0) == 1:
                return None

        # Reversal + failed breakout
        rev_sig = detect_reversal(candles_3m, cam_pre, current_time=ct, day_type_tag=day_type_tag)
        fb_sig = detect_failed_breakout(candles_3m, cam_pre)

        # Late entry gate
        if ct.hour > 14 or (ct.hour == 14 and ct.minute >= 45):
            return None

        # Quality gate
        quality_ok, allowed_side, gate_reason, st_details = self.quality_gate(
            candles_3m=candles_3m,
            candles_15m=candles_15m if candles_15m is not None else pd.DataFrame(),
            timestamp=ct,
            symbol=self.config.symbols[0] if self.config.symbols else "NSE:NIFTY50-INDEX",
            cpr_levels=cpr_pre,
            camarilla_levels=cam_pre,
            reversal_signal=rev_sig,
            failed_breakout_signal=fb_sig,
            day_type_result=day_type_result,
            daily_camarilla_levels=cam_pre,
        )
        if not quality_ok:
            return None

        # Signal detection
        tpma = calculate_vwap(candles_3m) if len(candles_3m) >= 5 else None
        orb_h, orb_l = get_opening_range(candles_3m) if len(candles_3m) >= 5 else (None, None)

        signal = detect_signal(
            candles_3m, candles_15m if candles_15m is not None else pd.DataFrame(),
            cpr_pre, cam_pre, trad_pre, atr_val,
            include_partial=False, current_time=ct,
            vwap=tpma, orb_high=orb_h, orb_low=orb_l,
            day_type_result=day_type_result,
            osc_relief_active=st_details.get("osc_relief_override", False),
            daily_camarilla_levels=cam_pre,
            st_details=st_details,
        )
        if signal is None:
            return None

        sig_side = signal.get("side")
        if sig_side != allowed_side:
            return None

        if not self._cap_available(info, is_scalp=False):
            return None

        # Get option + place live entry
        opt_name, _ = self.get_option_by_moneyness(spot, sig_side)
        if not opt_name:
            return None

        success, order_id = self._send_live_entry_order(opt_name, self.config.quantity, 1)
        if not success:
            return None

        # Get actual entry price from broker
        time.sleep(0.5)
        try:
            quote = self.fyers.quotes({"symbols": opt_name})
            entry_price = float(quote["d"][0]["v"]["lp"])
        except Exception:
            entry_price = spot

        _adx = _safe_float(candles_3m.iloc[-1].get("adx14", 0), 0)
        levels = self.build_dynamic_levels(
            entry_price, atr_val, sig_side,
            entry_candle=len(candles_3m) - 1,
            candles_df=candles_3m,
            adx_value=_adx,
        )
        if not levels.get("valid", False):
            return None

        leg = "call_buy" if sig_side == "CALL" else "put_buy"
        st = info[leg]
        st.update({
            "option_name": opt_name,
            "side": sig_side,
            "buy_price": entry_price,
            "quantity": self.config.quantity,
            "trade_flag": 1,
            "is_open": True,
            "lifecycle_state": "OPEN",
            "position_side": "LONG",
            "entry_time": ct,
            "entry_candle": len(candles_3m) - 1,
            "scalp_mode": False,
            "trade_class": TRADE_CLASS_TREND,
            "stop": levels["stop"],
            "pt": levels["pt"],
            "tg": levels["tg"],
            "trail_step": levels["trail_step"],
            "trail_start": levels["trail_start"],
            "trail_updates": 0,
            "atr_value": atr_val,
            "position_id": f"LIVE-{sig_side}-{ct}",
            "hf_exit_manager": OptionExitManager(entry_price, sig_side),
            "pnl": 0.0,
            "order_id": order_id,
        })
        self._register_trade(info, is_scalp=False)
        self.store(info, self.config.account_type)

        _log_green(
            f"[ENTRY][LIVE] {sig_side} {opt_name} entry={entry_price:.2f} "
            f"SL={levels['stop']:.2f} PT={levels['pt']:.2f} TG={levels['tg']:.2f}"
        )
        return {"entry": True, "side": sig_side, "type": "TREND"}

    # ══════════════════════════════════════════════════════════════════
    # PART 8: Offline Replay — Candle-by-Candle Simulation
    # ══════════════════════════════════════════════════════════════════

    def run_replay(
        self,
        db_path: Optional[str] = None,
        symbols: Optional[List[str]] = None,
        date_str: Optional[str] = None,
        min_warmup_candles: int = 35,
        signal_only: bool = False,
        output_dir: str = ".",
        tick_db_instance=None,
    ) -> dict:
        """Offline replay simulation using SQLite tick_db.

        Delegates to execution.run_offline_replay for complete replay logic
        including trend continuation, compression state, regime-aware cooldowns,
        and reversal persistence — ensuring identical replay behavior.
        """
        exe = self._get_execution_module()
        return exe.run_offline_replay(
            tick_db=tick_db_instance,
            symbols_list=symbols or self.config.symbols,
            date_str=date_str,
            min_warmup_candles=min_warmup_candles,
            signal_only=signal_only,
            output_dir=output_dir,
            db_path=db_path,
        )

    # ══════════════════════════════════════════════════════════════════
    # PART 9: Unified Entry Point
    # ══════════════════════════════════════════════════════════════════

    def run(
        self,
        symbols: Optional[List[str]] = None,
        mode: str = "PAPER",
        candles_3m: Optional[pd.DataFrame] = None,
        candles_15m: Optional[pd.DataFrame] = None,
        spot_price: Optional[float] = None,
        # Replay-specific
        db_path: Optional[str] = None,
        date_str: Optional[str] = None,
        tick_db_instance=None,
        min_warmup_candles: int = 35,
        signal_only: bool = False,
        output_dir: str = ".",
    ) -> Optional[dict]:
        """Unified strategy entry point.

        Routes to the appropriate execution mode:
        - OFFLINE/REPLAY: run_replay() for candle-by-candle backtesting
        - PAPER: run_paper() for simulated trading
        - LIVE: run_live() for real broker execution
        """
        mode_upper = mode.upper()

        if mode_upper in ("OFFLINE", "REPLAY"):
            return self.run_replay(
                db_path=db_path,
                symbols=symbols,
                date_str=date_str,
                min_warmup_candles=min_warmup_candles,
                signal_only=signal_only,
                output_dir=output_dir,
                tick_db_instance=tick_db_instance,
            )
        elif mode_upper == "LIVE":
            return self.run_live(
                candles_3m=candles_3m,
                candles_15m=candles_15m,
            )
        else:
            # Default: PAPER
            return self.run_paper(
                candles_3m=candles_3m,
                candles_15m=candles_15m,
                spot_price=spot_price,
            )

    # ══════════════════════════════════════════════════════════════════
    # PART 10: Convenience & Trade Reporting
    # ══════════════════════════════════════════════════════════════════

    def get_trade_summary(self) -> dict:
        """Return current session trade summary."""
        info = self.paper_info
        trades = []
        for leg in ("call_buy", "put_buy"):
            fdf = info.get(leg, {}).get("filled_df")
            if fdf is not None and not fdf.empty:
                trades.append(fdf)
        if not trades:
            return {"total_trades": 0, "total_pnl": 0.0}

        all_trades = pd.concat(trades)
        exits = all_trades[all_trades["action"].isin(["EXIT", "PARTIAL_EXIT"])]
        return {
            "total_trades": len(exits),
            "total_pnl": float(exits["take_profit"].sum()) if not exits.empty else 0.0,
            "winners": int((exits["take_profit"] > 0).sum()) if not exits.empty else 0,
            "losers": int((exits["take_profit"] < 0).sum()) if not exits.empty else 0,
            "win_rate": (
                float((exits["take_profit"] > 0).sum() / len(exits) * 100)
                if not exits.empty else 0.0
            ),
        }

    def save_trades_csv(self, suffix: str = "PAPER"):
        """Save filled trades to CSV."""
        info = self.paper_info if suffix == "PAPER" else self.live_info
        frames = []
        for leg in ("call_buy", "put_buy"):
            fdf = info.get(leg, {}).get("filled_df")
            if fdf is not None and not fdf.empty:
                frames.append(fdf)
        if frames:
            combined = pd.concat(frames)
            filename = f"trades_{self.config.strategy_name}_{self.today_str}_{suffix}.csv"
            combined.to_csv(filename, index=True)
            self.logger.info(f"[TRADES SAVED] {filename} ({len(combined)} records)")

    def reset_session(self):
        """Reset all session state for a new trading day."""
        self.paper_info = {}
        self.live_info = {}
        self._init_trade_info()
        self._init_risk_info()
        self.last_signal_candle_time = None
        self._compression_state = CompressionState()
        self._slope_conflict_bars = {}
        self._paper_zones = []
        self._paper_zones_date = ""
        self._live_zones = []
        self._live_zones_date = ""
        self._paper_dtc = None
        self._paper_dtc_date = ""
        self._live_dtc = None
        self._live_dtc_date = ""
        self.today_str = dt.now(self.config.time_zone).strftime("%Y-%m-%d")
        self.filled_df = pd.DataFrame(
            columns=["status", "filled_qty", "avg_price", "symbol"]
        )
        self.logger.info("[SESSION RESET] All state cleared for new day")


# ══════════════════════════════════════════════════════════════════════
# Convenience factory
# ══════════════════════════════════════════════════════════════════════


def create_strategy(
    mode: str = "PAPER",
    config: Optional[StrategyConfig] = None,
    **kwargs,
) -> OptionsBuyingStrategy:
    """Create and configure an OptionsBuyingStrategy instance.

    Args:
        mode: Execution mode — "PAPER", "LIVE", or "REPLAY"
        config: Optional StrategyConfig override
        **kwargs: Additional config overrides (e.g., quantity=100)

    Returns:
        Configured OptionsBuyingStrategy instance
    """
    if config is None:
        config = StrategyConfig(mode=mode, **kwargs)
    else:
        config.mode = mode
    return OptionsBuyingStrategy(config=config)


# ══════════════════════════════════════════════════════════════════════
# CLI entry point for replay testing
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Options Buying Strategy — Replay")
    parser.add_argument("--date", type=str, help="Replay date (YYYY-MM-DD)")
    parser.add_argument("--db", type=str, help="SQLite tick database path")
    parser.add_argument("--signal-only", action="store_true", help="Log signals only, skip trades")
    parser.add_argument("--output-dir", type=str, default=".", help="Output directory for CSV")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    strategy = create_strategy(mode="REPLAY")
    result = strategy.run(
        mode="OFFLINE",
        db_path=args.db,
        date_str=args.date,
        signal_only=args.signal_only,
        output_dir=args.output_dir,
    )

    if result:
        summary = strategy.get_trade_summary()
        print(f"\n{'='*60}")
        print(f"Replay Complete: {args.date}")
        print(f"Total Trades: {summary['total_trades']}")
        print(f"Total PnL: {summary['total_pnl']:.2f}")
        print(f"Win Rate: {summary['win_rate']:.1f}%")
        print(f"{'='*60}")
