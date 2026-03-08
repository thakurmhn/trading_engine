    # ===== execution.py =====
import logging
import pickle
import pathlib
import re
import numpy as np
import pandas as pd
import pendulum as dt
try:
    from fyers_apiv3 import fyersModel
except ImportError:
    fyersModel = None  # offline replay doesn't need fyers SDK
import time
from datetime import UTC, datetime, timedelta

from config import (
    time_zone, strategy_name, MAX_TRADES_PER_DAY, account_type, quantity,
    CALL_MONEYNESS, PUT_MONEYNESS, profit_loss_point, ENTRY_OFFSET, ORDER_TYPE,
    MAX_DAILY_LOSS, MAX_DRAWDOWN, OSCILLATOR_EXIT_MODE, symbols,
    TREND_ENTRY_ADX_MIN,
    SLOPE_ADX_GATE,
    TIME_SLOPE_ADX_GATE,
)
# Lazy import: setup.py makes live API calls at module level.
# For offline replay (--db flag), these aren't needed and would fail
# without a valid Fyers session.  Imported on demand in _ensure_setup().
_setup_loaded = False
df = fyers = ticker = option_chain = spot_price = None
start_time = end_time = hist_data = None


def _ensure_setup():
    """Import setup.py globals on first use (live/paper mode only)."""
    global _setup_loaded, df, fyers, ticker, option_chain, spot_price
    global start_time, end_time, hist_data
    if _setup_loaded:
        return
    from setup import (
        df as _df, fyers as _fyers, ticker as _ticker,
        option_chain as _oc, spot_price as _sp,
        start_time as _st, end_time as _et, hist_data as _hd
    )
    df, fyers, ticker, option_chain, spot_price = _df, _fyers, _ticker, _oc, _sp
    start_time, end_time, hist_data = _st, _et, _hd
    _setup_loaded = True
from indicators import (
    calculate_cpr,
    calculate_traditional_pivots,
    calculate_camarilla_pivots,
    resolve_atr,
    daily_atr,
    williams_r,
    calculate_cci,
    momentum_ok,
    classify_cpr_width,
)

from signals import detect_signal, get_opening_range, compute_tilt_state
# from tickdb import tick_db
from orchestration import update_candles_and_signals  # uses fixed ADX/CCI
from orchestration import build_indicator_dataframe   # uses fixed ADX/CCI
from position_manager import make_replay_pm
from option_exit_manager import OptionExitManager
from day_type import (make_day_type_classifier, apply_day_type_to_pm,
                      DayType, DayTypeResult, DayTypeClassifier)
from compression_detector import CompressionState
from reversal_detector import detect_reversal
from failed_breakout_detector import detect_failed_breakout
from zone_detector import (
    detect_zones,
    load_zones,
    save_zones,
    update_zone_activity,
    detect_zone_revisit,
)
from pulse_module import get_pulse_module, PulseModule
from regime_context import (
    RegimeContext,
    compute_regime_context,
    compute_scalp_regime_context,
    log_regime_context,
    classify_atr_regime,
)

# ===========================================================
# ANSI COLORS for order logs
RESET   = "\033[0m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
RED     = "\033[91m"
MAGENTA = "\033[95m"
GRAY    = "\033[90m"
CYAN    = "\033[96m"


def log_entry_green(msg: str) -> None:
    """Emit entry-path logs in green while preserving message payload."""
    logging.info(f"{GREEN}{msg}{RESET}")

# ===== Dip/Rally scalp settings =====
SCALP_PT_POINTS = 18.0
SCALP_SL_POINTS = 10.0
SCALP_MIN_HOLD_BARS = 2
SCALP_EXTREME_MOVE_ATR_MULT = 0.90
# Phase 2: Scalp SL Tightening - ATR × 0.6–0.8 (tighter than previous 0.8–1.0)
SCALP_ATR_SL_MIN_MULT = 0.60
SCALP_ATR_SL_MAX_MULT = 0.80
TREND_MIN_HOLD_BARS = 3
TREND_EXTREME_MOVE_ATR_MULT = 1.15
SCALP_COOLDOWN_MINUTES = 20
SCALP_HISTORY_MAXLEN = 120
STARTUP_SUPPRESSION_MINUTES = 5
# Pulse module threshold for scalp entry (ticks per second)
PULSE_TICKRATE_THRESHOLD = 15.0
# P3-C: Paper mode slippage — models bid/ask spread + market impact on fills.
# Applied to ENTRY price in paper mode so paper P&L reflects realistic fills.
# Set to 0.0 to disable. Realistic for NIFTY ITM options: 1.5 pts per side.
PAPER_SLIPPAGE_POINTS = 1.5
# P2-D: Trade class labels — ensures scalp_mode never bleeds into trend trades.
TRADE_CLASS_SCALP = "SCALP"
TRADE_CLASS_TREND = "TREND"
# P2-C: Partial TG exit — fraction of quantity exited at first TG hit.
PARTIAL_TG_QTY_FRAC = 0.50
RESTART_STATE_VERSION = 1
DEFAULT_TIME_EXIT_CANDLES = 16
DEFAULT_OSC_RSI_CALL = 75.0
DEFAULT_OSC_RSI_PUT = 25.0
DEFAULT_OSC_CCI_CALL = 130.0
DEFAULT_OSC_CCI_PUT = -130.0
DEFAULT_OSC_WR_CALL = -10.0
DEFAULT_OSC_WR_PUT = -88.0
EMA_STRETCH_BLOCK_MULT = 2.5   # Block entries > 2.5x ATR from EMA (mean reversion risk)
EMA_STRETCH_TAG_MULT = 1.8
MAX_TRADE_TREND = 8
MAX_TRADE_SCALP = 12

#===========================================================
# Initalize filled_df
try:
    filled_df
except NameError:
    filled_df = pd.DataFrame(columns=["status", "filled_qty", "avg_price", "symbol"])


#===================================================================

today_str = dt.now(time_zone).strftime("%Y-%m-%d")

_compression_state = CompressionState()   # for paper_order / live_order

# Phase 6: Slope conflict persistence counter {symbol: consecutive_bars_blocked}
_slope_conflict_bars = {}

# Phase 4: Zone cache for paper/live order (loaded once per session)
_paper_zones = []        # List[Zone] for paper_order zone revisit detection
_paper_zones_date = ""   # date guard for daily reload
_live_zones = []         # List[Zone] for live_order zone revisit detection
_live_zones_date = ""    # date guard for daily reload

_paper_dtc = None        # DayTypeClassifier for paper_order per-session
_paper_dtc_date = ""     # date guard for daily reset
_live_dtc = None         # DayTypeClassifier for live_order per-session
_live_dtc_date = ""      # date guard for daily reset

def map_status_code(code):
    status_map = {
        1: "CANCELLED",
        2: "TRADED",
        4: "TRANSIT",
        5: "REJECTED",
        6: "PENDING",
        7: "EXPIRED"
    }
    return status_map.get(code, str(code))

def status_color(status):
    color_map = {"TRADED": GREEN, "PENDING": YELLOW, "CANCELLED": RED, "REJECTED": MAGENTA}
    return color_map.get(status, RESET)

# ===== Shared state =====
last_signal_candle_time = None

# # ===== Persistence =====
# def store(data, account_type_):
#     try:
#         pickle.dump(data, open(f'data-{dt.now(time_zone).date()}-{account_type_}.pickle', 'wb'))
#     except Exception as e:
#         logging.error(f"Failed to store state: {e}")

# def load(account_type_):
#     try:
#         return pickle.load(open(f'data-{dt.now(time_zone).date()}-{account_type_}.pickle', 'rb'))
#     except Exception as e:
#         logging.warning(f"State load failed (fresh start): {e}")
#         raise

# ===== Persistence with Ledger =====

def store(data, account_type_):
    """
    Append trading state to a ledger stored in a pickle file.
    Each call adds a new snapshot to the list instead of overwriting.
    Compatible with both legacy dict and new ledger format.
    """
    filename = f"data-{dt.now(time_zone).date()}-{account_type_}.pickle"
    try:
        # Try to load existing ledger
        try:
            with open(filename, "rb") as f:
                ledger = pickle.load(f)

            # Normalize legacy formats
            if isinstance(ledger, dict):
                # Old format: single dict snapshot
                ledger = [ledger]
            elif not isinstance(ledger, list):
                # Unexpected format: reset to empty list
                ledger = []
        except Exception:
            ledger = []

        # Append current snapshot with timestamp + state
        snapshot = {
            "timestamp": dt.now(time_zone),
            "state": data
        }
        ledger.append(snapshot)

        # Save back to pickle
        with open(filename, "wb") as f:
            pickle.dump(ledger, f, protocol=pickle.HIGHEST_PROTOCOL)
        _save_restart_state(data, account_type_)

    except Exception as e:
        logging.error(f"Failed to store state: {e}")


def load(account_type_):
    """
    Load the latest trading state from pickle file.
    Returns the most recent snapshot's state dict.
    """
    filename = f"data-{dt.now(time_zone).date()}-{account_type_}.pickle"
    try:
        with open(filename, "rb") as f:
            ledger = pickle.load(f)

        # Ledger is a list of snapshots; return the latest state
        if isinstance(ledger, list) and ledger:
            return ledger[-1]["state"]
        elif isinstance(ledger, dict):
            # Legacy single snapshot
            return ledger
        else:
            raise ValueError("Ledger format invalid or empty")
    except Exception as e:
        logging.warning(f"State load failed (fresh start): {e}")
        raise


def load_ledger(account_type_):
    """
    Load the full ledger (all snapshots) from pickle file.
    Useful for audit, replay, or debugging.
    """
    filename = f"data-{dt.now(time_zone).date()}-{account_type_}.pickle"
    try:
        with open(filename, "rb") as f:
            ledger = pickle.load(f)

        # Normalize legacy formats
        if isinstance(ledger, dict):
            return [ledger]
        elif isinstance(ledger, list):
            return ledger
        else:
            return []
    except Exception as e:
        logging.warning(f"Ledger load failed: {e}")
        return []


def _restart_state_file(account_type_: str) -> str:
    return f"restart-state-{account_type_.lower()}.pickle"


def _parse_ts(value):
    if value is None:
        return None
    pendulum_dt = getattr(dt, "DateTime", None)
    if isinstance(value, datetime) or (pendulum_dt is not None and isinstance(value, pendulum_dt)):
        return value
    try:
        return pd.Timestamp(value).to_pydatetime()
    except Exception:
        return None


def _save_restart_state(info: dict, account_type_: str) -> None:
    """Persist minimal restart state (cooldowns + open positions)."""
    try:
        payload = {
            "version": RESTART_STATE_VERSION,
            "saved_at": dt.now(time_zone),
            "last_exit_time": info.get("last_exit_time"),
            "scalp_cooldown_until": info.get("scalp_cooldown_until"),
            "startup_suppression_until": info.get("startup_suppression_until"),
            "active_positions": [],
        }
        for leg in ("call_buy", "put_buy"):
            st = info.get(leg, {}) if isinstance(info, dict) else {}
            if st.get("is_open", False) or st.get("trade_flag", 0) == 1:
                payload["active_positions"].append(
                    {
                        "leg": leg,
                        "symbol": st.get("option_name"),
                        "option_type": st.get("side"),
                        "position_side": st.get("position_side", "LONG"),
                        "position_id": st.get("position_id"),
                        "entry_time": st.get("entry_time"),
                    }
                )
        with open(_restart_state_file(account_type_), "wb") as f:
            pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception as e:
        logging.debug(f"[RESTART STATE] save failed: {e}")


def _load_restart_state(account_type_: str) -> dict:
    try:
        with open(_restart_state_file(account_type_), "rb") as f:
            payload = pickle.load(f)
        if isinstance(payload, dict):
            logging.info(
                f"[RESTART STATE] loaded account={account_type_} "
                f"saved_at={payload.get('saved_at')} active_positions={len(payload.get('active_positions', []))}"
            )
            return payload
        return {}
    except Exception:
        return {}


def _hydrate_runtime_state(info: dict, account_type_: str, mode_label: str) -> dict:
    """Ensure restart-safe fields and restore cooldown/open-trade context."""
    now = dt.now(time_zone)
    info.setdefault("last_exit_time", None)
    info.setdefault("scalp_cooldown_until", None)
    info.setdefault("scalp_last_burst_key", None)
    info.setdefault("osc_hold_until", None)  # Oscillator extreme hold timer
    info.setdefault("scalp_hist", {"CALL": [], "PUT": []})
    info.setdefault("trade_count", 0)
    info.setdefault("trend_trade_count", 0)
    info.setdefault("scalp_trade_count", 0)
    info.setdefault("max_trades", MAX_TRADES_PER_DAY)
    info.setdefault("max_trades_trend", MAX_TRADE_TREND)
    info.setdefault("max_trades_scalp", MAX_TRADE_SCALP)

    persisted = _load_restart_state(account_type_)
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
                logging.info(
                    "[ENTRY][STATE_RESTORED] "
                    f"timestamp={now} symbol={st.get('option_name', 'N/A')} "
                    f"option_type={st.get('side', 'N/A')} position_side={st.get('position_side', 'LONG')} "
                    f"position_id={restored_id} lifecycle={st.get('lifecycle_state')} "
                    f"regime={st.get('regime_context', 'RESTORED')}"
                )
                logging.info(f"[ENTRY][STATE_RESTORED] {restored_id} restored at startup.")
                st["_restored_audit_logged"] = True

    logging.info(
        f"[RESTART STATE] mode={mode_label} startup_suppression_until={info['startup_suppression_until']} "
        f"last_exit_time={info.get('last_exit_time')} scalp_cooldown_until={info.get('scalp_cooldown_until')}"
    )
    _save_restart_state(info, account_type_)
    return info


def _cap_used(info: dict, is_scalp: bool) -> int:
    return int(info.get("scalp_trade_count", 0) if is_scalp else info.get("trend_trade_count", 0))


def _cap_limit(info: dict, is_scalp: bool) -> int:
    return int(info.get("max_trades_scalp", MAX_TRADE_SCALP) if is_scalp else info.get("max_trades_trend", MAX_TRADE_TREND))


def _cap_available(info: dict, is_scalp: bool) -> bool:
    return _cap_used(info, is_scalp) < _cap_limit(info, is_scalp)


def _register_trade(info: dict, is_scalp: bool) -> None:
    info["trade_count"] = int(info.get("trade_count", 0)) + 1
    if is_scalp:
        info["scalp_trade_count"] = int(info.get("scalp_trade_count", 0)) + 1
    else:
        info["trend_trade_count"] = int(info.get("trend_trade_count", 0)) + 1


def _validate_restored_positions_on_startup(
    info,
    candles_3m,
    candles_15m,
    spot_px,
    account_type_,
    mode_label,
    symbol,
):
    """Validate restored open trades against current gates and close stale ones.

    Restored positions are checked exactly once after restart when live candle
    context is available. Trades conflicting with current bias/quality gates are
    exited immediately (minimum-hold safeguards are not applied here).
    """
    if info.get("_restored_validation_done", False):
        return
    if candles_3m is None or candles_3m.empty:
        return

    now_ts = pd.Timestamp(candles_3m.iloc[-1].get("time", dt.now(time_zone)))
    if now_ts.tzinfo is None:
        now_ts = now_ts.tz_localize(time_zone)
    else:
        now_ts = now_ts.tz_convert(time_zone)

    pivot_src = candles_3m.iloc[-2] if len(candles_3m) >= 2 else candles_3m.iloc[-1]
    cpr_pre = calculate_cpr(pivot_src["high"], pivot_src["low"], pivot_src["close"])
    cam_pre = calculate_camarilla_pivots(pivot_src["high"], pivot_src["low"], pivot_src["close"])

    _rev_sig_gate = detect_reversal(candles_3m, cam_pre, current_time=now_ts)
    quality_ok, allowed_side, gate_reason, st_details = _trend_entry_quality_gate(
        candles_3m=candles_3m,
        candles_15m=candles_15m if candles_15m is not None else pd.DataFrame(),
        timestamp=now_ts,
        symbol=symbol,
        adx_min=float(TREND_ENTRY_ADX_MIN),
        cpr_levels=cpr_pre,
        camarilla_levels=cam_pre,
        reversal_signal=_rev_sig_gate,
        day_type_result=None,
        open_bias_context=None,
    )

    for leg, side in (("call_buy", "CALL"), ("put_buy", "PUT")):
        st = info.get(leg, {}) if isinstance(info, dict) else {}
        if not isinstance(st, dict):
            continue
        if not st.get("_restored_from_restart", False):
            continue
        if not bool(st.get("is_open", False) or st.get("trade_flag", 0) == 1):
            st["_restored_from_restart"] = False
            continue

        side_matches = (allowed_side == side)
        stale = (not quality_ok) or (not side_matches)
        position_id = st.get("position_id", "UNKNOWN")
        option_symbol = st.get("option_name", "N/A")

        if stale:
            qty = st.get("quantity", 0)
            exit_price, _ = _get_option_market_snapshot(option_symbol, spot_px)
            if mode_label.upper() == "LIVE":
                success, _ = send_live_exit_order(option_symbol, qty, "STALE_RESTORE_CLEANUP")
            else:
                success, _ = send_paper_exit_order(option_symbol, qty, "STALE_RESTORE_CLEANUP")

            if success:
                cleanup_trade_exit(
                    info,
                    leg,
                    side,
                    option_symbol,
                    qty,
                    exit_price,
                    mode_label.upper(),
                    "STALE_RESTORE_CLEANUP",
                )
                info["last_exit_time"] = now_ts
                logging.info(
                    "[STATE RESTORED][STALE ENTRY CLOSED] "
                    f"timestamp={now_ts} mode={mode_label} symbol={option_symbol} "
                    f"option_type={side} position_side={st.get('position_side', 'LONG')} "
                    f"position_id={position_id} allowed_side={allowed_side} "
                    f"ST3m_bias={st_details.get('ST3m_bias')} ST15m_bias={st_details.get('ST15m_bias')} "
                    f"ADX={st_details.get('adx14')} RSI={st_details.get('rsi14')} CCI={st_details.get('cci20')} "
                    f"reason={gate_reason}"
                )
            else:
                logging.warning(
                    "[STATE RESTORED][STALE ENTRY CLOSE FAILED] "
                    f"timestamp={now_ts} mode={mode_label} symbol={option_symbol} "
                    f"position_id={position_id} reason=exit_order_failed"
                )
        else:
            logging.info(
                "[STATE RESTORED][VALID ENTRY CONTINUED] "
                f"timestamp={now_ts} mode={mode_label} symbol={option_symbol} "
                f"option_type={side} position_side={st.get('position_side', 'LONG')} "
                f"position_id={position_id} allowed_side={allowed_side} "
                f"ST3m_bias={st_details.get('ST3m_bias')} ST15m_bias={st_details.get('ST15m_bias')} "
                f"ADX={st_details.get('adx14')} RSI={st_details.get('rsi14')} CCI={st_details.get('cci20')}"
            )

        st["_restored_from_restart"] = False

    info["_restored_validation_done"] = True
    store(info, account_type_)

def get_option_by_moneyness(spot_price_, side, moneyness='ITM', points=0):
    """
    Select ITM option strike with strike_diff points inside ATM.
    Checks against live quote dataframe `df` to ensure liquidity/availability.
    """

    from config import strike_diff
    # df is already imported globally from setup

    if spot_price_ is None or pd.isna(spot_price_):
        logging.error("[get_option_by_moneyness] Invalid spot price")
        return None, None

    # Normalize side to CE/PE
    side = "CE" if side in ["CALL", "CE"] else "PE"

    # Round to nearest strike
    atm_strike = round(spot_price_ / strike_diff) * strike_diff

    if side == "CE":  # CALL
        strike = atm_strike - strike_diff
    else:             # PUT
        strike = atm_strike + strike_diff

    # Apply any manual offset
    strike += points

    # Debug logging
    logging.info(
        f"[DEBUG get_option_by_moneyness] spot={spot_price_}, atm={atm_strike}, "
        f"side={side}, requested_strike={strike}"
    )

    # Filter candidates by side
    candidates = option_chain[
        (option_chain['option_type'].isin([side, side.replace('E','ALL')]))
    ].copy()

    if candidates.empty:
        logging.error(f"[get_option_by_moneyness] No options available for side={side}")
        return None, None

    # Calculate distance to desired strike
    candidates['strike_diff_abs'] = (candidates['strike_price'] - strike).abs()
    candidates = candidates.sort_values('strike_diff_abs')

    # Liquidity-aware selection: prefer symbol present in live feed (df)
    selected_symbol = None
    selected_strike = None

    for _, row in candidates.iterrows():
        sym = row['symbol']
        # In LIVE/PAPER mode, df holds subscribed symbols. In REPLAY, df might be empty or irrelevant.
        # We check if df is populated (LIVE/PAPER) and if sym is in it.
        if not df.empty and sym in df.index:
            selected_symbol = sym
            selected_strike = row['strike_price']
            break
    
    # If no live symbol found (or REPLAY mode where df might be empty/irrelevant for selection),
    # fall back to the closest strike from option_chain.
    if not selected_symbol:
        best_match = candidates.iloc[0]
        selected_symbol = best_match['symbol']
        selected_strike = best_match['strike_price']
        if not df.empty: # Only warn if we expected live data
             logging.warning(
                f"[get_option_by_moneyness] No liquid option found in df for {side} near {strike}. "
                f"Using closest from chain: {selected_symbol}"
            )

    if selected_strike != strike:
         logging.warning(
            f"[get_option_by_moneyness] Strike adjustment: requested {strike}, using {selected_strike} ({selected_symbol})"
        )

    return selected_symbol, selected_strike


def _get_option_market_snapshot(symbol, fallback_price):
    """
    Return (option_price, option_volume) for the option symbol from `df`.

    All exit logic must operate in option-premium space. If option LTP is
    unavailable, fallback_price is used for continuity.
    """
    option_price = None
    option_volume = 0.0

    if symbol in df.index:
        try:
            row = df.loc[symbol]
            ltp = row.get("ltp", None) if hasattr(row, "get") else row["ltp"]
            if ltp is not None and not pd.isna(ltp):
                option_price = float(ltp)

            vol_candidates = [
                "volume",
                "vol_traded_today",
                "vtt",
                "last_traded_qty",
            ]
            for col in vol_candidates:
                v = row.get(col, None) if hasattr(row, "get") else None
                if v is not None and not pd.isna(v):
                    option_volume = float(v)
                    break
        except Exception:
            pass

    if option_price is None:
        option_price = float(fallback_price) if fallback_price is not None else 0.0

    return option_price, max(0.0, option_volume)


def _detect_scalp_dip_rally_signal(candles_3m, traditional_levels, atr):
    """Detect scalp opportunities with normalized long-option semantics.

    CALL represents buy-on-dip of a CALL option (long premium).
    PUT represents sell-on-rally setup executed as long PUT option (long premium).
    """
    if len(candles_3m) < 2 or traditional_levels is None or atr is None or atr <= 0:
        return None

    last = candles_3m.iloc[-1]
    rng = last.high - last.low
    if rng == 0:
        return None

    # Define support/resistance levels
    s1 = traditional_levels.get("s1")
    s2 = traditional_levels.get("s2")
    p = traditional_levels.get("pivot")
    r1 = traditional_levels.get("r1")
    r2 = traditional_levels.get("r2")

    atr_buf = 0.25 * atr  # Proximity buffer in underlying points

    # CALL: Buy on dip at support
    for level, name in [(s1, "S1"), (s2, "S2"), (p, "PIVOT")]:
        if level and last.low <= level + atr_buf and (last.close - last.low) > 0.5 * rng:
            # Touched support and rejected (closed in upper half)
            sl_zone = level - (0.5 * atr)
            logging.info(
                f"[SCALP_BUY_DIP] side=CALL reason=REJECTION_{name} "
                f"zone={name} level={level:.2f} atr={atr:.2f} sl_zone={sl_zone:.2f} "
                "long=True"
            )
            return {
                "side": "CALL",
                "position_type": "LONG",
                "reason": f"SCALP_BUY_DIP_{name}",
                "tag": "SCALP_BUY_DIP",
                "zone": name,
                "level": float(level),
                "sl_zone": float(sl_zone),
                "stop": float(sl_zone),
            }

    # PUT: Sell on rally at resistance
    for level, name in [(r1, "R1"), (r2, "R2"), (p, "PIVOT")]:
        if level and last.high >= level - atr_buf and (last.high - last.close) > 0.5 * rng:
            # Touched resistance and rejected (closed in lower half)
            sl_zone = level + (0.5 * atr)
            logging.info(
                f"[SCALP_SELL_RALLY] side=PUT reason=REJECTION_{name} "
                f"zone={name} level={level:.2f} atr={atr:.2f} sl_zone={sl_zone:.2f} "
                "long=True"
            )
            return {
                "side": "PUT",
                "position_type": "LONG",
                "reason": f"SCALP_SELL_RALLY_{name}",
                "tag": "SCALP_SELL_RALLY",
                "zone": name,
                "level": float(level),
                "sl_zone": float(sl_zone),
                "stop": float(sl_zone),
            }

    return None


def _can_enter_scalp(info, burst_key, now_ts):
    """Gate scalp re-entry by cool-down and burst uniqueness."""
    cooldown_until = info.get("scalp_cooldown_until")
    if cooldown_until and now_ts < cooldown_until:
        logging.info(
            f"[SCALP ENTRY BLOCKED][COOLDOWN] now={now_ts} "
            f"cooldown_until={cooldown_until} burst_key={burst_key}"
        )
        return False, "COOLDOWN"
    if info.get("scalp_last_burst_key") == burst_key:
        logging.info(f"[SCALP ENTRY BLOCKED][DUPLICATE_BURST] now={now_ts} burst_key={burst_key}")
        return False, "DUPLICATE_BURST"
    return True, "OK"


def _is_startup_suppression_active(info, now_ts, mode_label):
    """Block fresh entries during post-restart suppression window."""
    suppress_until = _parse_ts(info.get("startup_suppression_until"))
    if suppress_until is None:
        return False
    if now_ts >= suppress_until:
        return False

    last_logged = _parse_ts(info.get("startup_suppression_logged_at"))
    if last_logged is None or (now_ts - last_logged).total_seconds() >= 30:
        remaining = (suppress_until - now_ts).total_seconds()
        logging.info(
            f"[ENTRY BLOCKED][STARTUP_SUPPRESSION] mode={mode_label} "
            f"until={suppress_until} remaining_s={remaining:.0f} "
            "reason=Startup suppression active, entry ignored."
        )
        info["startup_suppression_logged_at"] = now_ts
    return True


def _opening_s4_breakdown_context(candles_3m, cpr_levels, camarilla_levels, atr, timestamp):
    """Structured context for opening S4/R4 breakout exception handling."""
    ctx = {
        "close": float("nan"),
        "rsi14": float("nan"),
        "s4": float("nan"),
        "r4": float("nan"),
        "atr": float("nan"),
        "threshold_s4_down": float("nan"),
        "threshold_r4_up": float("nan"),
        "cpr_width": "NORMAL",
        "compressed_cam": False,
        "close_below_s4": False,
        "close_above_r4": False,
        "opening_window": False,
        "opening_s4_breakdown": False,
        "opening_r4_breakout": False,
    }
    if candles_3m is None or candles_3m.empty:
        return ctx

    last = candles_3m.iloc[-1]
    close = float(last.get("close")) if pd.notna(last.get("close")) else float("nan")
    rsi = float(last.get("rsi14")) if pd.notna(last.get("rsi14")) else float("nan")
    s4 = float(camarilla_levels.get("s4")) if camarilla_levels and pd.notna(camarilla_levels.get("s4")) else float("nan")
    r4 = float(camarilla_levels.get("r4")) if camarilla_levels and pd.notna(camarilla_levels.get("r4")) else float("nan")
    atr_val = float(atr) if atr is not None and pd.notna(atr) else float("nan")
    threshold_down = s4 - (0.01 * atr_val) if np.isfinite(s4) and np.isfinite(atr_val) else float("nan")
    threshold_up = r4 + (0.01 * atr_val) if np.isfinite(r4) and np.isfinite(atr_val) else float("nan")
    close_below_s4 = bool(np.isfinite(close) and np.isfinite(threshold_down) and close < threshold_down)
    close_above_r4 = bool(np.isfinite(close) and np.isfinite(threshold_up) and close > threshold_up)

    _cpr_classifier = globals().get("classify_cpr_width")
    if callable(_cpr_classifier):
        cpr_width = _cpr_classifier(cpr_levels or {}, close_price=close if np.isfinite(close) else None)
    else:
        cpr_width = "NORMAL"
    r3 = float(camarilla_levels.get("r3")) if camarilla_levels and pd.notna(camarilla_levels.get("r3")) else float("nan")
    r4 = float(camarilla_levels.get("r4")) if camarilla_levels and pd.notna(camarilla_levels.get("r4")) else float("nan")
    s3 = float(camarilla_levels.get("s3")) if camarilla_levels and pd.notna(camarilla_levels.get("s3")) else float("nan")
    cam_span = min(abs(r4 - r3), abs(s3 - s4)) if np.isfinite(r3) and np.isfinite(r4) and np.isfinite(s3) and np.isfinite(s4) else float("nan")
    compressed_cam = bool(np.isfinite(cam_span) and np.isfinite(atr_val) and cam_span <= max(0.20 * atr_val, 5.0))

    ts = pd.Timestamp(timestamp)
    opening_window = bool(ts.hour == 9 and ts.minute <= 18)
    opening_s4_breakdown = bool(opening_window and close_below_s4 and cpr_width == "NARROW" and compressed_cam)
    opening_r4_breakout = bool(opening_window and close_above_r4 and cpr_width == "NARROW" and compressed_cam)

    ctx.update(
        {
            "close": close,
            "rsi14": rsi,
            "s4": s4,
            "r4": r4,
            "atr": atr_val,
            "threshold_s4_down": threshold_down,
            "threshold_r4_up": threshold_up,
            "cpr_width": cpr_width,
            "compressed_cam": compressed_cam,
            "close_below_s4": close_below_s4,
            "close_above_r4": close_above_r4,
            "opening_window": opening_window,
            "opening_s4_breakdown": opening_s4_breakdown,
            "opening_r4_breakout": opening_r4_breakout,
        }
    )
    return ctx


def _long_position_side(option_type):
    """All option trades are long-premium positions regardless of option type."""
    _ = option_type
    return "LONG"


def entry_gate_context(
    allowed_side,
    zone_tag,
    gap_tag,
    atr_stretch,
    rsi_bounds,
    cci_bounds,
    day_type_result=None,
    open_bias_context=None,
):
    """Merge day/opening context with oscillator zone context for entry gating."""
    rsi_lo, rsi_hi = float(rsi_bounds[0]), float(rsi_bounds[1])
    cci_lo, cci_hi = float(cci_bounds[0]), float(cci_bounds[1])

    day_type_tag = "UNKNOWN"
    if isinstance(day_type_result, str):
        day_type_tag = day_type_result or "UNKNOWN"
    elif day_type_result is not None:
        day_type_tag = getattr(getattr(day_type_result, "name", None), "value", "UNKNOWN") or "UNKNOWN"

    bias = "Neutral"
    open_bias = "UNKNOWN"
    if isinstance(open_bias_context, dict):
        bias = str(open_bias_context.get("bias", "Neutral"))
        open_bias = str(open_bias_context.get("open_bias", "UNKNOWN"))
        ctx_gap = open_bias_context.get("gap_tag")
        if ctx_gap:
            gap_tag = str(ctx_gap)

    if zone_tag == "ZoneA":
        # Enforce default strict ceiling/floor, but preserve ATR-based expansion
        # that was already applied before this call (rsi_bounds reflects it).
        rsi_lo = max(rsi_lo, 30.0)
        rsi_hi = min(rsi_hi, max(70.0, float(rsi_bounds[1])))   # keep ATR expansion
        cci_lo = max(cci_lo, min(-150.0, float(cci_bounds[0])))  # keep ATR expansion
        cci_hi = min(cci_hi, max(150.0, float(cci_bounds[1])))   # keep ATR expansion
        osc_context = "ZoneA-Blocker"
    elif zone_tag == "ZoneB":
        rsi_lo = min(rsi_lo, 25.0)
        rsi_hi = max(rsi_hi, 75.0)
        cci_lo = min(cci_lo, -220.0)
        cci_hi = max(cci_hi, 220.0)
        osc_context = "ZoneB-Reversal"
    else:
        rsi_lo = min(rsi_lo, 20.0)
        rsi_hi = max(rsi_hi, 80.0)
        cci_lo = min(cci_lo, -260.0)
        cci_hi = max(cci_hi, 260.0)
        osc_context = "ZoneC-Continuation"

    if gap_tag == "GAP_UP" and allowed_side == "CALL":
        rsi_hi += 5.0
        cci_hi += 30.0
    elif gap_tag == "GAP_DOWN" and allowed_side == "PUT":
        rsi_lo -= 5.0
        cci_lo -= 30.0

    if np.isfinite(atr_stretch) and atr_stretch >= 2.0:
        if allowed_side == "CALL":
            rsi_hi += 3.0
            cci_hi += 20.0
        elif allowed_side == "PUT":
            rsi_lo -= 3.0
            cci_lo -= 20.0

    bias_upper = bias.upper()
    bias_aligned = (
        (allowed_side == "CALL" and (gap_tag == "GAP_UP" or "POSITIVE" in bias_upper))
        or (allowed_side == "PUT" and (gap_tag == "GAP_DOWN" or "NEGATIVE" in bias_upper))
    )

    return {
        "rsi_bounds": (float(rsi_lo), float(rsi_hi)),
        "cci_bounds": (float(cci_lo), float(cci_hi)),
        "osc_context": osc_context,
        "zone_tag": zone_tag,
        "day_type_tag": day_type_tag,
        "open_bias": open_bias,
        "bias": bias,
        "gap_tag": gap_tag,
        "bias_aligned": bool(bias_aligned),
    }


def _supertrend_alignment_gate(candles_3m, candles_15m, timestamp, symbol):
    """Validate 3m/15m Supertrend alignment before trend-signal scoring.

    Returns
    -------
    tuple[bool, str, dict]
        aligned:
            True only when 3m and 15m biases are both BULLISH or both BEARISH.
        allowed_side:
            "CALL" when both BULLISH, "PUT" when both BEARISH, else None.
        details:
            Structured audit details including bias/slope/line snapshots.
    """
    def _norm_bias_local(raw):
        txt = str(raw).upper()
        if txt in {"BULLISH", "UP"}:
            return "BULLISH"
        if txt in {"BEARISH", "DOWN"}:
            return "BEARISH"
        return "NEUTRAL"

    last_3m = candles_3m.iloc[-1] if candles_3m is not None and not candles_3m.empty else None
    last_15m = candles_15m.iloc[-1] if candles_15m is not None and not candles_15m.empty else None

    st3m_bias = _norm_bias_local(last_3m.get("supertrend_bias", "NEUTRAL")) if last_3m is not None else "NEUTRAL"
    st15m_bias = _norm_bias_local(last_15m.get("supertrend_bias", "NEUTRAL")) if last_15m is not None else "NEUTRAL"

    details = {
        "timestamp": timestamp,
        "symbol": symbol,
        "ST3m_bias": st3m_bias,
        "ST15m_bias": st15m_bias,
        "ST3m_slope": str(last_3m.get("supertrend_slope", "FLAT")) if last_3m is not None else "FLAT",
        "ST15m_slope": str(last_15m.get("supertrend_slope", "FLAT")) if last_15m is not None else "FLAT",
        "ST3m_line": float(last_3m.get("supertrend_line")) if last_3m is not None and pd.notna(last_3m.get("supertrend_line")) else None,
        "ST15m_line": float(last_15m.get("supertrend_line")) if last_15m is not None and pd.notna(last_15m.get("supertrend_line")) else None,
    }

    aligned = (
        st3m_bias in {"BULLISH", "BEARISH"}
        and st15m_bias in {"BULLISH", "BEARISH"}
        and st3m_bias == st15m_bias
    )
    allowed_side = "CALL" if st3m_bias == "BULLISH" and aligned else ("PUT" if st3m_bias == "BEARISH" and aligned else None)
    details["alignment_status"] = bool(aligned)

    logging.info(
        "[ST ALIGNMENT] "
        f"timestamp={details['timestamp']} symbol={details['symbol']} "
        f"ST3m_bias={details['ST3m_bias']} ST15m_bias={details['ST15m_bias']} "
        f"ST3m_slope={details['ST3m_slope']} ST15m_slope={details['ST15m_slope']} "
        f"ST3m_line={details['ST3m_line']} ST15m_line={details['ST15m_line']} "
        f"alignment_status={details['alignment_status']}"
    )
    if not aligned:
        logging.info(
            "[ENTRY DIAG][ST_CONFLICT_CANDIDATE] "
            f"timestamp={details['timestamp']} symbol={details['symbol']} "
            f"ST3m_bias={details['ST3m_bias']} ST15m_bias={details['ST15m_bias']} "
            "reason=Conflict candidate detected; evaluating overrides before blocking."
        )

    return aligned, allowed_side, details


def _trend_entry_quality_gate(
    candles_3m,
    candles_15m,
    timestamp,
    symbol,
    adx_min=18.0,
    rsi_min=30.0,
    rsi_max=70.0,
    cci_min=-150.0,
    cci_max=150.0,
    cpr_levels=None,
    camarilla_levels=None,
    reversal_signal=None,
    failed_breakout_signal=None,
    day_type_result=None,
    open_bias_context=None,
    daily_camarilla_levels=None,
):
    """Hard quality gate for trend entries.

    Conditions:
    1) Supertrend bias alignment: 3m and 15m must both be BULLISH (CALL) or BEARISH (PUT).
    2) 3m Supertrend slope must confirm bias direction (UP for BULLISH, DOWN for BEARISH).
       15m slope is not checked.
    3) ADX must be > adx_min.
    4) Oscillators must not be in extremes:
       RSI in [30, 70], CCI in [-150, 150] (default; widened from [35,65]/[-120,120]).
    """
    logging.info(
        "[ENTRY CONFIG] "
        f"timestamp={timestamp} symbol={symbol} "
        f"adx_min={adx_min} rsi_range=[{rsi_min},{rsi_max}] "
        f"cci_range=[{cci_min},{cci_max}]"
    )
    _entry_log = globals().get("log_entry_green")
    if not callable(_entry_log):
        _entry_log = logging.info
    _adx_gate_min = float(adx_min)
    logging.info(f"[ENTRY GATE][TREND] Evaluating trend entry for {symbol} @ {timestamp}")
    aligned, allowed_side, st_details = _supertrend_alignment_gate(
        candles_3m=candles_3m,
        candles_15m=candles_15m,
        timestamp=timestamp,
        symbol=symbol,
    )

    # Extract indicator snapshot up front so every block reason log has values.
    last_3m = candles_3m.iloc[-1] if candles_3m is not None and not candles_3m.empty else None
    adx_val = float(last_3m.get("adx14")) if last_3m is not None and pd.notna(last_3m.get("adx14")) else float("nan")
    rsi_val = float(last_3m.get("rsi14")) if last_3m is not None and pd.notna(last_3m.get("rsi14")) else float("nan")
    cci_val = float(last_3m.get("cci20")) if last_3m is not None and pd.notna(last_3m.get("cci20")) else float("nan")
    st_details["adx14"] = adx_val
    st_details["rsi14"] = rsi_val
    st_details["cci20"] = cci_val

    close_val = float(last_3m.get("close")) if last_3m is not None and pd.notna(last_3m.get("close")) else float("nan")
    atr_val = float(last_3m.get("atr14")) if last_3m is not None and pd.notna(last_3m.get("atr14")) else float("nan")
    if not np.isfinite(atr_val):
        try:
            atr_val, _ = resolve_atr(candles_3m)
            atr_val = float(atr_val) if atr_val is not None and pd.notna(atr_val) else float("nan")
        except Exception:
            atr_val = float("nan")
    s4_val = float(camarilla_levels.get("s4")) if camarilla_levels and pd.notna(camarilla_levels.get("s4")) else float("nan")
    r4_val = float(camarilla_levels.get("r4")) if camarilla_levels and pd.notna(camarilla_levels.get("r4")) else float("nan")
    s4_thr = s4_val - (0.01 * atr_val) if np.isfinite(s4_val) and np.isfinite(atr_val) else float("nan")
    r4_thr = r4_val + (0.01 * atr_val) if np.isfinite(r4_val) and np.isfinite(atr_val) else float("nan")
    close_below_s4 = bool(np.isfinite(close_val) and np.isfinite(s4_thr) and close_val < s4_thr)
    close_above_r4 = bool(np.isfinite(close_val) and np.isfinite(r4_thr) and close_val > r4_thr)
    _gap_ctx = str((open_bias_context or {}).get("gap_tag", "UNKNOWN")).upper()
    _bias_aligned_fast = bool(
        (allowed_side == "CALL" and _gap_ctx == "GAP_UP")
        or (allowed_side == "PUT" and _gap_ctx == "GAP_DOWN")
    )
    _cpr_classifier = globals().get("classify_cpr_width")
    if callable(_cpr_classifier):
        cpr_width = _cpr_classifier(cpr_levels or {}, close_price=close_val if np.isfinite(close_val) else None)
    else:
        cpr_width = "NORMAL"
    r3_val = float(camarilla_levels.get("r3")) if camarilla_levels and pd.notna(camarilla_levels.get("r3")) else float("nan")
    s3_val = float(camarilla_levels.get("s3")) if camarilla_levels and pd.notna(camarilla_levels.get("s3")) else float("nan")
    cam_span = min(abs(r4_val - r3_val), abs(s3_val - s4_val)) if np.isfinite(r3_val) and np.isfinite(r4_val) and np.isfinite(s3_val) and np.isfinite(s4_val) else float("nan")
    compressed_cam = bool(np.isfinite(cam_span) and np.isfinite(atr_val) and cam_span <= max(0.20 * atr_val, 5.0))
    osc_override_put = bool(
        allowed_side == "PUT"
        and np.isfinite(rsi_val)
        and rsi_val < 30.0
        and close_below_s4
        and compressed_cam
    )
    osc_override_call = bool(
        allowed_side == "CALL"
        and np.isfinite(rsi_val)
        and rsi_val > 70.0
        and close_above_r4
        and compressed_cam
    )
    osc_override = bool(osc_override_put or osc_override_call)
    st_details["s4"] = s4_val
    st_details["r4"] = r4_val
    st_details["s4_threshold"] = s4_thr
    st_details["r4_threshold"] = r4_thr
    st_details["close"] = close_val
    st_details["cpr_width"] = cpr_width
    st_details["compressed_cam"] = compressed_cam
    st_details["close_below_s4"] = close_below_s4
    st_details["close_above_r4"] = close_above_r4
    st_details["osc_override_s4"] = osc_override

    # Phase 6.1: Compute tilt state for governance relaxation
    _tilt_state = compute_tilt_state(close_val, cpr_levels, camarilla_levels)
    st_details["tilt_state"] = _tilt_state
    _tilt_aligned = (
        (_tilt_state == "BULLISH_TILT" and allowed_side == "CALL")
        or (_tilt_state == "BEARISH_TILT" and allowed_side == "PUT")
    )
    st_details["tilt_aligned"] = _tilt_aligned

    logging.info(
        "[ENTRY DIAG][S4_R4_BREAK] "
        f"timestamp={timestamp} symbol={symbol} close={close_val} s4={s4_val} r4={r4_val} "
        f"atr={atr_val} s4_threshold={s4_thr} r4_threshold={r4_thr} "
        f"put_ok={allowed_side == 'PUT'} call_ok={allowed_side == 'CALL'} "
        f"close_below_s4={close_below_s4} close_above_r4={close_above_r4} "
        f"cpr_width={cpr_width} compressed_cam={compressed_cam}"
    )

    # ── Daily Camarilla S4/R4 directional filter (hard block) ──────────────
    # Uses FIXED previous-day Camarilla levels (not rolling 3m recalculation).
    # If price is below daily S4 → bearish regime → block CALL entries.
    # If price is above daily R4 → bullish regime → block PUT entries.
    if daily_camarilla_levels and np.isfinite(close_val):
        _daily_s4 = float(daily_camarilla_levels.get("s4", float("nan")))
        _daily_r4 = float(daily_camarilla_levels.get("r4", float("nan")))
        st_details["daily_s4"] = _daily_s4
        st_details["daily_r4"] = _daily_r4
        if np.isfinite(_daily_s4) and close_val < _daily_s4 and allowed_side == "CALL":
            logging.info(
                f"[ENTRY BLOCKED][DAILY_S4_FILTER] timestamp={timestamp} symbol={symbol} "
                f"side=CALL close={close_val:.1f} daily_s4={_daily_s4:.1f} "
                f"reason=Price below daily S4, bearish regime — CALL blocked"
            )
            st_details["blocked_by"] = "DAILY_S4_FILTER"
            return False, allowed_side, "Price below daily S4 — CALL blocked in bearish regime.", st_details
        if np.isfinite(_daily_r4) and close_val > _daily_r4 and allowed_side == "PUT":
            logging.info(
                f"[ENTRY BLOCKED][DAILY_R4_FILTER] timestamp={timestamp} symbol={symbol} "
                f"side=PUT close={close_val:.1f} daily_r4={_daily_r4:.1f} "
                f"reason=Price above daily R4, bullish regime — PUT blocked"
            )
            st_details["blocked_by"] = "DAILY_R4_FILTER"
            return False, allowed_side, "Price above daily R4 — PUT blocked in bullish regime.", st_details

    if not aligned:
        candidate_side = None
        if st_details.get("ST3m_bias") == "BULLISH":
            candidate_side = "CALL"
        elif st_details.get("ST3m_bias") == "BEARISH":
            candidate_side = "PUT"
        elif close_above_r4:
            candidate_side = "CALL"
        elif close_below_s4:
            candidate_side = "PUT"

        # Apply daily S4/R4 filter to conflict candidates too
        if daily_camarilla_levels and np.isfinite(close_val) and candidate_side:
            _daily_s4 = float(daily_camarilla_levels.get("s4", float("nan")))
            _daily_r4 = float(daily_camarilla_levels.get("r4", float("nan")))
            if np.isfinite(_daily_s4) and close_val < _daily_s4 and candidate_side == "CALL":
                logging.info(
                    f"[ENTRY BLOCKED][DAILY_S4_FILTER] timestamp={timestamp} symbol={symbol} "
                    f"side=CALL close={close_val:.1f} daily_s4={_daily_s4:.1f} "
                    f"reason=ST conflict candidate CALL blocked — price below daily S4"
                )
                st_details["blocked_by"] = "DAILY_S4_FILTER"
                return False, candidate_side, "Price below daily S4 — CALL blocked.", st_details
            if np.isfinite(_daily_r4) and close_val > _daily_r4 and candidate_side == "PUT":
                logging.info(
                    f"[ENTRY BLOCKED][DAILY_R4_FILTER] timestamp={timestamp} symbol={symbol} "
                    f"side=PUT close={close_val:.1f} daily_r4={_daily_r4:.1f} "
                    f"reason=ST conflict candidate PUT blocked — price above daily R4"
                )
                st_details["blocked_by"] = "DAILY_R4_FILTER"
                return False, candidate_side, "Price above daily R4 — PUT blocked.", st_details

        st_conflict_override = False
        st_conflict_reason = None
        if candidate_side and reversal_signal is not None:
            if reversal_signal.get("side") == candidate_side and reversal_signal.get("score", 0) >= 55:
                st_conflict_override = True
                st_conflict_reason = (
                    f"REVERSAL_OVERRIDE side={candidate_side} score={reversal_signal.get('score')}"
                )
        if not st_conflict_override and candidate_side == "CALL" and close_above_r4 and compressed_cam:
            st_conflict_override = True
            st_conflict_reason = "CPR_PIVOT_OVERRIDE CALL close_above_r4 with compressed_cam"
        if not st_conflict_override and candidate_side == "PUT" and close_below_s4 and compressed_cam:
            st_conflict_override = True
            st_conflict_reason = "CPR_PIVOT_OVERRIDE PUT close_below_s4 with compressed_cam"
        if not st_conflict_override and candidate_side and np.isfinite(adx_val) and adx_val >= _adx_gate_min + 6.0:
            st_conflict_override = True
            st_conflict_reason = f"ADX_OVERRIDE adx={adx_val:.1f} >= {_adx_gate_min + 6.0:.1f}"

        if st_conflict_override:
            allowed_side = candidate_side
            st_details["st_conflict_override"] = True
            st_details["st_conflict_override_reason"] = st_conflict_reason
            _entry_log(
                "[ENTRY ALLOWED][ST_CONFLICT_OVERRIDE] "
                f"timestamp={timestamp} symbol={symbol} allowed_side={allowed_side} "
                f"reason={st_conflict_reason}"
            )
        else:
            logging.info(
                "[ENTRY BLOCKED][ST_CONFLICT] "
                f"timestamp={timestamp} symbol={symbol} allowed_side={candidate_side} "
                "reason=Supertrend conflict, no override qualified."
            )
            return False, candidate_side, "Supertrend conflict, entry suppressed.", st_details

    # Failed breakout governance:
    # - block entries in failed breakout opposite direction
    # - allow aligned reversal direction and tag for attribution
    if isinstance(failed_breakout_signal, dict) and failed_breakout_signal.get("side") in {"CALL", "PUT"}:
        fb_side = str(failed_breakout_signal.get("side"))
        st_details["failed_breakout"] = True
        st_details["failed_breakout_side"] = fb_side
        st_details["failed_breakout_pivot"] = failed_breakout_signal.get("pivot", "")
        st_details["failed_breakout_tag"] = failed_breakout_signal.get("tag", "FAILED_BREAKOUT_REVERSAL")
        if allowed_side != fb_side:
            logging.info(
                "[ENTRY BLOCKED][FAILED_BREAKOUT_MISMATCH] "
                f"timestamp={timestamp} symbol={symbol} allowed_side={allowed_side} "
                f"failed_breakout_side={fb_side} pivot={st_details['failed_breakout_pivot']}"
            )
            return False, allowed_side, "Failed breakout opposite direction, entry suppressed.", st_details
        logging.info(
            "[FAILED_BREAKOUT][REVERSAL] "
            f"timestamp={timestamp} symbol={symbol} side={allowed_side} "
            f"pivot={st_details['failed_breakout_pivot']} tag={st_details['failed_breakout_tag']}"
        )

    slope_ok_3m = (
        (st_details["ST3m_bias"] == "BULLISH" and str(st_details["ST3m_slope"]).upper() == "UP")
        or (st_details["ST3m_bias"] == "BEARISH" and str(st_details["ST3m_slope"]).upper() == "DOWN")
    )
    if not slope_ok_3m:
        # ── ST_SLOPE_CONFLICT override check ──────────────────────────────────
        # Allow override when reversal detector strongly confirms direction OR
        # when CPR compression + pivot breakout aligns with the trade side.
        _slope_override_reason = None

        # Path A: reversal signal aligned with allowed_side
        if (reversal_signal is not None
                and reversal_signal.get("side") == allowed_side
                and reversal_signal.get("score", 0) >= 50):
            _slope_override_reason = (
                f"REVERSAL_OVERRIDE: reversal_score={reversal_signal['score']} "
                f"strength={reversal_signal.get('strength')} "
                f"pivot_zone={reversal_signal.get('pivot_zone')} "
                f"osc_confirmed={reversal_signal.get('osc_confirmed')}"
            )

        # Path B: CPR compression + pivot breakout (NARROW CPR + close near R4/S4)
        if _slope_override_reason is None:
            if (cpr_width == "NARROW" and compressed_cam
                    and allowed_side == "CALL" and close_above_r4):
                _slope_override_reason = (
                    "CPR_PIVOT_OVERRIDE: NARROW_CPR + compressed_cam + CALL close_above_r4"
                )
            elif (cpr_width == "NARROW" and compressed_cam
                    and allowed_side == "PUT" and close_below_s4):
                _slope_override_reason = (
                    "CPR_PIVOT_OVERRIDE: NARROW_CPR + compressed_cam + PUT close_below_s4"
                )

        # Path C: weak-trend ADX gate — slope reading is unreliable in low-trend environments
        # Below SLOPE_ADX_GATE, slope can flip randomly; block would suppress real signals.
        _slope_adx_gate = float(globals().get("SLOPE_ADX_GATE", 17.0))
        if _slope_override_reason is None and np.isfinite(adx_val) and adx_val < _slope_adx_gate:
            _slope_override_reason = (
                f"ADX_WEAK_SLOPE_GATE: adx={adx_val:.1f} < gate={_slope_adx_gate:.1f} "
                "slope conflict suppressed in low-trend environment"
            )

        # Path D: time-based override — after 11:00 IST, flat slope acceptable if ADX < TIME_SLOPE_ADX_GATE
        if _slope_override_reason is None:
            try:
                _ts_str = str(timestamp).replace("T", " ")
                _ts_hour = int(_ts_str.split(" ")[-1].split(":")[0])
            except (ValueError, IndexError):
                _ts_hour = 0
            _time_slope_gate = float(globals().get("TIME_SLOPE_ADX_GATE", 25.0))
            if _ts_hour >= 11 and np.isfinite(adx_val) and adx_val < _time_slope_gate:
                _slope_override_reason = (
                    f"TIME_SLOPE_OVERRIDE: post-11:00 flat slope "
                    f"adx={adx_val:.1f} < time_gate={_time_slope_gate:.1f}"
                )
        # Path E: strong trend continuation override
        # If ADX is very strong and opening bias is aligned, allow slope conflict.
        if _slope_override_reason is None and np.isfinite(adx_val) and adx_val > 40.0 and _bias_aligned_fast:
            _slope_override_reason = (
                f"TREND_ADX_BIAS_OVERRIDE: adx={adx_val:.1f} > 40 and open_bias_aligned=True"
            )

        # Path F: persistent slope conflict override — if slope conflict has persisted
        # > N bars, allow entry since the signal is likely still valid but slope is lagging.
        if _slope_override_reason is None:
            _sym_key = str(symbol)
            _slope_conflict_bars[_sym_key] = _slope_conflict_bars.get(_sym_key, 0) + 1
            _slope_time_limit = int(globals().get("SLOPE_CONFLICT_TIME_BARS", 5))
            if _slope_conflict_bars[_sym_key] >= _slope_time_limit:
                _slope_override_reason = (
                    f"SLOPE_OVERRIDE_TIME: conflict persisted {_slope_conflict_bars[_sym_key]} bars "
                    f">= limit={_slope_time_limit}"
                )
                _slope_conflict_bars[_sym_key] = 0  # reset after override

        # Path G: tilt-based governance relaxation — when price is decisively
        # above R3+CPR (BULLISH_TILT) or below S3+CPR (BEARISH_TILT), slope
        # conflict is less meaningful because price structure confirms direction.
        if _slope_override_reason is None and _tilt_aligned:
            _slope_override_reason = (
                f"TILT_GOVERNANCE_OVERRIDE: tilt={_tilt_state} side={allowed_side} "
                f"close={close_val:.2f} r3={r3_val} s3={s3_val}"
            )

        if _slope_override_reason:
            # Reset slope conflict counter on override
            _slope_conflict_bars[str(symbol)] = 0
            _entry_log(
                "[ENTRY ALLOWED][ST_SLOPE_OVERRIDE] "
                f"timestamp={timestamp} symbol={symbol} allowed_side={allowed_side} "
                f"ST3m_bias={st_details['ST3m_bias']} ST3m_slope={st_details['ST3m_slope']} "
                f"override_reason={_slope_override_reason}"
            )
            logging.info(
                "[SLOPE_OVERRIDE_TREND] "
                f"timestamp={timestamp} symbol={symbol} side={allowed_side} "
                f"adx={adx_val if np.isfinite(adx_val) else 'N/A'} "
                f"open_bias_aligned={_bias_aligned_fast} "
                f"reason={_slope_override_reason}"
            )
            if "SLOPE_OVERRIDE_TIME" in _slope_override_reason:
                logging.info(
                    f"[SLOPE_OVERRIDE_TIME] timestamp={timestamp} symbol={symbol} "
                    f"side={allowed_side} bars={_slope_time_limit}"
                )
            st_details["slope_override_reason"] = _slope_override_reason
        else:
            logging.info(
                "[SLOPE_CONFLICT][3m] "
                f"timestamp={timestamp} symbol={symbol} allowed_side={allowed_side} "
                f"ST3m_bias={st_details['ST3m_bias']} ST3m_slope={st_details['ST3m_slope']} "
                "reason=3m slope does not confirm bias direction, entry suppressed."
            )
            logging.info(
                "[ENTRY BLOCKED][ST_SLOPE_CONFLICT] "
                f"timestamp={timestamp} symbol={symbol} allowed_side={allowed_side} "
                f"ST3m_bias={st_details['ST3m_bias']} ST3m_slope={st_details['ST3m_slope']}"
            )
            logging.info(
                f"[CONFLICT_BLOCKED] timestamp={timestamp} symbol={symbol} "
                f"side={allowed_side} type=ST_SLOPE_CONFLICT "
                f"conflict_bars={_slope_conflict_bars.get(str(symbol), 0)}"
            )
            return False, allowed_side, "Slope mismatch, entry suppressed.", st_details
    # Reset slope conflict counter when slope is aligned
    _slope_conflict_bars[str(symbol)] = 0
    logging.info(
        "[ENTRY CHECK][ST_BIAS_OK] "
        f"timestamp={timestamp} symbol={symbol} allowed_side={allowed_side} "
        f"ST15m_bias={st_details['ST15m_bias']} ST3m_bias={st_details['ST3m_bias']} "
        f"ST3m_slope={st_details['ST3m_slope']}"
    )

    if not np.isfinite(adx_val) or adx_val <= _adx_gate_min:
        adx_override = False
        adx_override_reason = None
        if reversal_signal is not None and reversal_signal.get("side") == allowed_side and reversal_signal.get("score", 0) >= 60:
            adx_override = True
            adx_override_reason = f"REVERSAL_ADX_OVERRIDE score={reversal_signal.get('score')}"
        elif allowed_side == "CALL" and close_above_r4 and compressed_cam:
            adx_override = True
            adx_override_reason = "CPR_PIVOT_ADX_OVERRIDE CALL close_above_r4 with compressed_cam"
        elif allowed_side == "PUT" and close_below_s4 and compressed_cam:
            adx_override = True
            adx_override_reason = "CPR_PIVOT_ADX_OVERRIDE PUT close_below_s4 with compressed_cam"
        elif _tilt_aligned:
            adx_override = True
            adx_override_reason = f"TILT_ADX_OVERRIDE: tilt={_tilt_state} side={allowed_side}"

        if adx_override:
            st_details["weak_adx_override"] = True
            st_details["weak_adx_override_reason"] = adx_override_reason
            _entry_log(
                "[ENTRY ALLOWED][WEAK_ADX_OVERRIDE] "
                f"timestamp={timestamp} symbol={symbol} allowed_side={allowed_side} "
                f"ADX={adx_val} adx_min={_adx_gate_min} reason={adx_override_reason}"
            )
        else:
            logging.info(
                "[ENTRY BLOCKED][WEAK_ADX] "
                f"timestamp={timestamp} symbol={symbol} allowed_side={allowed_side} "
                f"ADX={adx_val} adx_min={_adx_gate_min} reason=Weak trend strength, entry suppressed."
            )
            return False, allowed_side, "Weak trend strength, entry suppressed.", st_details

    # Standalone EMA stretch governance (independent of reversal detector score generation).
    ema9_val = float("nan")
    ema13_val = float("nan")
    if last_3m is not None:
        ema9_val = float(last_3m.get("ema9")) if pd.notna(last_3m.get("ema9")) else float("nan")
        ema13_val = float(last_3m.get("ema13")) if pd.notna(last_3m.get("ema13")) else float("nan")
    if (not np.isfinite(ema9_val) or not np.isfinite(ema13_val)) and candles_3m is not None and "close" in candles_3m.columns:
        _cl = candles_3m["close"].astype(float)
        if len(_cl) >= 1:
            ema9_val = float(_cl.ewm(span=9, adjust=False).mean().iloc[-1])
        if len(_cl) >= 1:
            ema13_val = float(_cl.ewm(span=13, adjust=False).mean().iloc[-1])
    ema_ref = ema9_val if np.isfinite(ema9_val) else ema13_val
    ema_stretch_mult = float("nan")
    if np.isfinite(close_val) and np.isfinite(ema_ref) and np.isfinite(atr_val) and atr_val > 0:
        ema_stretch_mult = (close_val - ema_ref) / atr_val
    st_details["ema_stretch_mult"] = ema_stretch_mult
    st_details["ema_stretch_blocked"] = False
    st_details["ema_stretch_tagged"] = False
    _ema_block_mult = float(globals().get("EMA_STRETCH_BLOCK_MULT", 3.0))
    _ema_tag_mult = float(globals().get("EMA_STRETCH_TAG_MULT", 2.0))
    _side_extreme = (
        (allowed_side == "CALL" and np.isfinite(ema_stretch_mult) and ema_stretch_mult >= _ema_block_mult)
        or (allowed_side == "PUT" and np.isfinite(ema_stretch_mult) and ema_stretch_mult <= -_ema_block_mult)
    )
    _side_tag = (
        (allowed_side == "CALL" and np.isfinite(ema_stretch_mult) and ema_stretch_mult >= _ema_tag_mult)
        or (allowed_side == "PUT" and np.isfinite(ema_stretch_mult) and ema_stretch_mult <= -_ema_tag_mult)
    )
    if _side_tag:
        st_details["ema_stretch_tagged"] = True
        logging.info(
            "[EMA_STRETCH][TAG] "
            f"timestamp={timestamp} symbol={symbol} side={allowed_side} "
            f"stretch={ema_stretch_mult:.2f}x threshold={_ema_tag_mult:.1f}x"
        )
    if _side_extreme:
        _ema_override = (
            isinstance(reversal_signal, dict)
            and reversal_signal.get("side") == allowed_side
            and float(reversal_signal.get("score", 0)) >= 70.0
        )
        # Daily Camarilla override: if price below daily S4 (PUT) or above daily R4 (CALL),
        # the EMA stretch is trend continuation on a gap/breakout day, not overextension.
        _ema_daily_override = False
        if daily_camarilla_levels and np.isfinite(close_val):
            _d_s4 = float(daily_camarilla_levels.get("s4", float("nan")))
            _d_r4 = float(daily_camarilla_levels.get("r4", float("nan")))
            if allowed_side == "PUT" and np.isfinite(_d_s4) and close_val < _d_s4:
                _ema_daily_override = True
            elif allowed_side == "CALL" and np.isfinite(_d_r4) and close_val > _d_r4:
                _ema_daily_override = True
        if _ema_override or _ema_daily_override:
            _ovr_reason = (
                "Aligned high-score reversal override."
                if _ema_override
                else f"Daily Camarilla trend continuation: {'below S4' if allowed_side == 'PUT' else 'above R4'}"
            )
            st_details["ema_stretch_override"] = True
            logging.info(
                "[EMA_STRETCH][OVERRIDE] "
                f"timestamp={timestamp} symbol={symbol} side={allowed_side} "
                f"stretch={ema_stretch_mult:.2f}x reason={_ovr_reason}"
            )
        else:
            st_details["ema_stretch_blocked"] = True
            logging.info(
                "[ENTRY BLOCKED][EMA_STRETCH] "
                f"timestamp={timestamp} symbol={symbol} side={allowed_side} "
                f"stretch={ema_stretch_mult:.2f}x threshold={_ema_block_mult:.1f}x "
                "reason=Distance from EMA exceeded standalone stretch gate."
            )
            return False, allowed_side, "EMA stretch gate, entry suppressed.", st_details

    # ── Trend-aware oscillator thresholds ─────────────────────────────────────
    # Strong trends produce naturally extreme RSI/CCI — blocking on fixed ranges
    # mis-categorises trend momentum as "exhausted".  Expand the acceptable band
    # when ADX confirms a genuine trend.
    #
    #   ADX ≤ 30   : default [30–70] RSI, [-150, +150] CCI
    #   ADX  30–40 : moderate expansion [25–75] RSI, [-200, +200] CCI
    #   ADX > 40   : wide expansion     [20–80] RSI, [-250, +250] CCI
    # Additionally, ATR-based expansion adds up to 5 RSI / 30 CCI pts in high-vol regimes.
    #
    # When an oscillator falls inside the expanded (not default) window, the
    # trade is still allowed but tagged [OSC_OVERRIDE][TREND_CONFIRMED].
    # When outside even the expanded window, fall through to the existing
    # pivot-break override or block.

    _default_rsi_min, _default_rsi_max = float(rsi_min), float(rsi_max)
    _default_cci_min, _default_cci_max = float(cci_min), float(cci_max)
    gap_tag = "NO_GAP"
    if np.isfinite(close_val) and np.isfinite(r4_val) and close_val > r4_val:
        gap_tag = "GAP_UP"
    elif np.isfinite(close_val) and np.isfinite(s4_val) and close_val < s4_val:
        gap_tag = "GAP_DOWN"

    if np.isfinite(adx_val) and adx_val > 40:
        _eff_rsi_min, _eff_rsi_max = 20.0, 80.0
        _eff_cci_min, _eff_cci_max = -250.0, 250.0
        _adx_osc_tier = "ADX_STRONG_40"
    elif np.isfinite(adx_val) and adx_val > 30:
        _eff_rsi_min, _eff_rsi_max = 25.0, 75.0
        _eff_cci_min, _eff_cci_max = -200.0, 200.0
        _adx_osc_tier = "ADX_MOD_30"
    else:
        _eff_rsi_min, _eff_rsi_max = _default_rsi_min, _default_rsi_max
        _eff_cci_min, _eff_cci_max = _default_cci_min, _default_cci_max
        _adx_osc_tier = "ADX_DEFAULT"

    st_details["adx_osc_tier"]           = _adx_osc_tier
    st_details["eff_rsi_range"]          = [_eff_rsi_min, _eff_rsi_max]
    st_details["eff_cci_range"]          = [_eff_cci_min, _eff_cci_max]
    st_details["osc_threshold_expanded"] = (_adx_osc_tier != "ADX_DEFAULT")

    # ATR expansion: high-volatility regimes push oscillators to more extreme readings
    # without signifying exhaustion — expand proportionally above the ADX tier base.
    _atr_rsi_exp = 0.0
    _atr_cci_exp = 0.0
    _atr_expand_tier = "ATR_DEFAULT"
    _atr_series = (
        candles_3m["atr14"].dropna()
        if candles_3m is not None and "atr14" in candles_3m.columns
        else pd.Series(dtype=float)
    )
    if len(_atr_series) >= 10 and np.isfinite(atr_val) and atr_val > 0:
        _atr_ma = float(_atr_series.tail(10).mean())
        if _atr_ma > 0 and atr_val > 1.5 * _atr_ma:
            _atr_rsi_exp, _atr_cci_exp = 5.0, 30.0
            _atr_expand_tier = "ATR_HIGH"
        elif _atr_ma > 0 and atr_val > 1.3 * _atr_ma:
            _atr_rsi_exp, _atr_cci_exp = 3.0, 20.0
            _atr_expand_tier = "ATR_ELEVATED"
    _eff_rsi_min -= _atr_rsi_exp
    _eff_rsi_max += _atr_rsi_exp
    _eff_cci_min -= _atr_cci_exp
    _eff_cci_max += _atr_cci_exp
    st_details["atr_expand_tier"] = _atr_expand_tier
    st_details["eff_rsi_range"] = [_eff_rsi_min, _eff_rsi_max]
    st_details["eff_cci_range"] = [_eff_cci_min, _eff_cci_max]

    atr_stretch = float("nan")
    if len(_atr_series) >= 10 and np.isfinite(atr_val):
        _atr_ma = float(_atr_series.tail(10).mean())
        if np.isfinite(_atr_ma) and _atr_ma > 0:
            atr_stretch = atr_val / _atr_ma

    # Gap-aware and ATR-stretch aware relaxation for fast sessions.
    _stretch_relax = 0.0
    if np.isfinite(atr_stretch) and atr_stretch > 1.0:
        _stretch_relax = min(4.0, (atr_stretch - 1.0) * 4.0)

    if gap_tag == "GAP_UP" and allowed_side == "CALL":
        _eff_rsi_max += 2.0 + _stretch_relax
        _eff_cci_max += 10.0 + (_stretch_relax * 6.0)
    elif gap_tag == "GAP_DOWN" and allowed_side == "PUT":
        _eff_rsi_min -= 2.0 + _stretch_relax
        _eff_cci_min -= 10.0 + (_stretch_relax * 6.0)

    st_details["eff_rsi_range"] = [_eff_rsi_min, _eff_rsi_max]
    st_details["eff_cci_range"] = [_eff_cci_min, _eff_cci_max]

    if np.isfinite(close_val) and np.isfinite(r3_val) and np.isfinite(s3_val) and s3_val <= close_val <= r3_val:
        zone_tag = "ZoneA"
    elif (
        np.isfinite(close_val)
        and (
            (np.isfinite(r3_val) and np.isfinite(r4_val) and r3_val < close_val <= r4_val)
            or (np.isfinite(s4_val) and np.isfinite(s3_val) and s4_val <= close_val < s3_val)
        )
    ):
        zone_tag = "ZoneB"
    elif np.isfinite(close_val) and (
        (np.isfinite(r4_val) and close_val > r4_val)
        or (np.isfinite(s4_val) and close_val < s4_val)
    ):
        zone_tag = "ZoneC"
    else:
        zone_tag = "ZoneA"

    st_details["osc_zone"] = zone_tag
    st_details["gap_tag"] = gap_tag
    st_details["atr_stretch"] = atr_stretch

    _entry_gate_ctx_fn = globals().get("entry_gate_context")
    if callable(_entry_gate_ctx_fn):
        merged_ctx = _entry_gate_ctx_fn(
            allowed_side=allowed_side,
            zone_tag=zone_tag,
            gap_tag=gap_tag,
            atr_stretch=atr_stretch,
            rsi_bounds=(_eff_rsi_min, _eff_rsi_max),
            cci_bounds=(_eff_cci_min, _eff_cci_max),
            day_type_result=day_type_result,
            open_bias_context=open_bias_context,
        )
    else:
        merged_ctx = {
            "rsi_bounds": (float(_eff_rsi_min), float(_eff_rsi_max)),
            "cci_bounds": (float(_eff_cci_min), float(_eff_cci_max)),
            "osc_context": (
                "ZoneA-Blocker" if zone_tag == "ZoneA"
                else ("ZoneB-Reversal" if zone_tag == "ZoneB" else "ZoneC-Continuation")
            ),
            "zone_tag": zone_tag,
            "day_type_tag": "UNKNOWN",
            "open_bias": "UNKNOWN",
            "bias": "Neutral",
            "gap_tag": gap_tag,
            "bias_aligned": False,
        }
    _eff_rsi_min, _eff_rsi_max = merged_ctx["rsi_bounds"]
    _eff_cci_min, _eff_cci_max = merged_ctx["cci_bounds"]
    st_details["eff_rsi_range"] = [_eff_rsi_min, _eff_rsi_max]
    st_details["eff_cci_range"] = [_eff_cci_min, _eff_cci_max]
    st_details["osc_context"] = merged_ctx["osc_context"]
    st_details["day_type_tag"] = merged_ctx["day_type_tag"]
    st_details["open_bias"] = merged_ctx["open_bias"]
    st_details["bias"] = merged_ctx["bias"]
    st_details["bias_aligned"] = merged_ctx["bias_aligned"]

    _bias_unknown = (merged_ctx.get("day_type_tag", "UNKNOWN") == "UNKNOWN"
                     or merged_ctx.get("gap_tag", "UNKNOWN") == "UNKNOWN")
    if merged_ctx["bias_aligned"]:
        logging.info(
            "[DAY_BIAS_ALIGN] "
            f"timestamp={timestamp} symbol={symbol} side={allowed_side} "
            f"day_type={merged_ctx['day_type_tag']} bias={merged_ctx['bias']} gap={merged_ctx['gap_tag']}"
        )
    elif _bias_unknown:
        # P4: pre-09:30 or pre-DTC bars — suppress noisy MISALIGN, emit UNKNOWN instead
        logging.debug(
            "[DAY_BIAS_UNKNOWN] "
            f"timestamp={timestamp} symbol={symbol} side={allowed_side} "
            f"day_type={merged_ctx['day_type_tag']} bias={merged_ctx['bias']} gap={merged_ctx['gap_tag']}"
        )
    else:
        logging.info(
            "[DAY_BIAS_MISALIGN] "
            f"timestamp={timestamp} symbol={symbol} side={allowed_side} "
            f"day_type={merged_ctx['day_type_tag']} bias={merged_ctx['bias']} gap={merged_ctx['gap_tag']}"
        )

    _entry_log(
        "[ENTRY ALLOWED][ST_BIAS_OK] "
        f"timestamp={timestamp} symbol={symbol} side={allowed_side} "
        f"osc_context={merged_ctx['osc_context']} day_type={merged_ctx['day_type_tag']} "
        f"bias={merged_ctx['bias']} "
        f"thresholds=RSI[{_eff_rsi_min:.1f},{_eff_rsi_max:.1f}] "
        f"CCI[{_eff_cci_min:.1f},{_eff_cci_max:.1f}]"
    )
    # Phase 6.1: Governance attribution
    _gov_tag = "GOVERNANCE_EASY" if _tilt_aligned else "GOVERNANCE_STRICT"
    st_details["governance"] = "EASY" if _tilt_aligned else "STRICT"
    logging.info(
        f"[{_gov_tag}] timestamp={timestamp} symbol={symbol} "
        f"side={allowed_side} tilt={_tilt_state}"
    )
    logging.info(
        "[ENTRY_GATE_CONTEXT] "
        f"timestamp={timestamp} symbol={symbol} side={allowed_side} "
        f"osc_context={merged_ctx['osc_context']} day_type={merged_ctx['day_type_tag']} "
        f"open_bias={merged_ctx['open_bias']} bias={merged_ctx['bias']} "
        f"gap={merged_ctx['gap_tag']} atr_stretch={atr_stretch if np.isfinite(atr_stretch) else 'N/A'} "
        f"rsi_range=[{_eff_rsi_min:.1f},{_eff_rsi_max:.1f}] cci_range=[{_eff_cci_min:.1f},{_eff_cci_max:.1f}]"
    )

    logging.info(
        "[OSC_CONTEXT] "
        f"timestamp={timestamp} symbol={symbol} zone={zone_tag} gap={merged_ctx['gap_tag']} "
        f"atr_stretch={atr_stretch if np.isfinite(atr_stretch) else 'N/A'} "
        f"RSI={rsi_val if np.isfinite(rsi_val) else 'N/A'} CCI={cci_val if np.isfinite(cci_val) else 'N/A'} "
        f"rsi_range=[{_eff_rsi_min},{_eff_rsi_max}] cci_range=[{_eff_cci_min},{_eff_cci_max}] "
        f"close={close_val if np.isfinite(close_val) else 'N/A'} r3={r3_val if np.isfinite(r3_val) else 'N/A'} "
        f"r4={r4_val if np.isfinite(r4_val) else 'N/A'} s3={s3_val if np.isfinite(s3_val) else 'N/A'} "
        f"s4={s4_val if np.isfinite(s4_val) else 'N/A'}"
    )

    _rsi_in_default = (not np.isfinite(rsi_val)) or (_default_rsi_min <= rsi_val <= _default_rsi_max)
    _cci_in_default = (not np.isfinite(cci_val)) or (_default_cci_min <= cci_val <= _default_cci_max)
    _rsi_in_expanded = (not np.isfinite(rsi_val)) or (_eff_rsi_min <= rsi_val <= _eff_rsi_max)
    _cci_in_expanded = (not np.isfinite(cci_val)) or (_eff_cci_min <= cci_val <= _eff_cci_max)

    # Case 1: within default thresholds — no issue, proceed
    if _rsi_in_default and _cci_in_default:
        return True, allowed_side, "OK", st_details

    # Case 2: outside default but within expanded (ADX or ATR tier override)
    if _rsi_in_expanded and _cci_in_expanded:
        if _adx_osc_tier != "ADX_DEFAULT" or _atr_expand_tier != "ATR_DEFAULT":
            logging.info(
                "[OSC_OVERRIDE][TREND_CONFIRMED] "
                f"timestamp={timestamp} symbol={symbol} allowed_side={allowed_side} "
                f"ADX={adx_val:.1f} tier={_adx_osc_tier} atr_tier={_atr_expand_tier} "
                f"RSI={rsi_val:.1f} (expanded [{_eff_rsi_min},{_eff_rsi_max}]) "
                f"CCI={cci_val:.1f} (expanded [{_eff_cci_min},{_eff_cci_max}]) "
                f"default_rsi=[{_default_rsi_min},{_default_rsi_max}] "
                f"default_cci=[{_default_cci_min},{_default_cci_max}] "
                "reason=ADX/ATR trend confirms oscillator extreme is momentum not exhaustion."
            )
            if zone_tag == "ZoneB":
                logging.info(
                    "[OSC_REVERSAL][ZoneB][TRIGGER] "
                    f"timestamp={timestamp} symbol={symbol} side={allowed_side} "
                    "reason=Zone B extreme treated as reversal trigger."
                )
            elif zone_tag == "ZoneC":
                logging.info(
                    "[OSC_CONTINUATION][ZoneC][RELAXED] "
                    f"timestamp={timestamp} symbol={symbol} side={allowed_side} "
                    "reason=Zone C extreme treated as continuation context."
                )
            st_details["osc_trend_override"] = True
            return True, allowed_side, "OK", st_details

    # Case 3: outside even expanded thresholds — check existing pivot-break override
    if osc_override:
        _entry_log(
            "[ENTRY ALLOWED][OSC_OVERRIDE_PIVOT_BREAK] "
            f"timestamp={timestamp} symbol={symbol} allowed_side={allowed_side} "
            f"close={close_val} s4={s4_val} r4={r4_val} "
            f"s4_threshold={s4_thr} r4_threshold={r4_thr} atr={atr_val} "
            f"RSI={rsi_val} CCI={cci_val} compressed_cam={compressed_cam}"
        )
        return True, allowed_side, "OK", st_details

    # ── Bias Misalignment Filter ─────────────────────────────────────────────────
    # Block entries when bias is misaligned AND oscillator is in extreme zone.
    # This prevents fighting the opening bias when indicators also suggest exhaustion.
    # Allow if: bias aligned OR oscillator NOT in extreme (still has room).
    _osc_in_extreme = not (_rsi_in_expanded and _cci_in_expanded)
    _bias_misaligned = not merged_ctx.get("bias_aligned", True)

    if _bias_misaligned and _osc_in_extreme:
        # Phase 6.1.2: Tilt-aligned entries bypass bias misalignment block —
        # price structure (above R3+TC or below S3+BC) overrides day bias when
        # both tilt and entry side agree.
        if _tilt_aligned:
            logging.info(
                "[GOVERNANCE_EASY][BIAS_MISALIGN_BYPASSED] "
                f"timestamp={timestamp} symbol={symbol} side={allowed_side} "
                f"tilt={_tilt_state} bias={merged_ctx.get('bias', 'UNKNOWN')} "
                f"RSI={rsi_val:.1f} CCI={cci_val:.1f} "
                "reason=Tilt-aligned, bias misalignment block bypassed"
            )
            st_details["governance"] = "EASY"
            st_details["tilt_bias_override"] = True
        else:
            logging.info(
                "[ENTRY BLOCKED][BIAS_MISALIGN_BLOCKED] "
                f"timestamp={timestamp} symbol={symbol} allowed_side={allowed_side} "
                f"bias={merged_ctx.get('bias', 'UNKNOWN')} gap={merged_ctx.get('gap_tag', 'UNKNOWN')} "
                f"RSI={rsi_val:.1f} CCI={cci_val:.1f} "
                f"rsi_expanded={_rsi_in_expanded} cci_expanded={_cci_in_expanded} "
                "reason=Bias misaligned AND oscillator in extreme zone, entry suppressed."
            )
            return False, allowed_side, "Bias misalignment with oscillator extreme, entry suppressed.", st_details

    # Case 4: S4/R4 breakout relief — price below S4−ATR (PUT) or above R4+ATR (CALL)
    # When price trades decisively outside the Camarilla extreme levels, oscillator
    # exhaustion reflects momentum continuation, not a reversal — allow entry.
    _s4_relief_thr = (s4_val - atr_val) if np.isfinite(s4_val) and np.isfinite(atr_val) else float("nan")
    _r4_relief_thr = (r4_val + atr_val) if np.isfinite(r4_val) and np.isfinite(atr_val) else float("nan")

    _put_relief = bool(
        allowed_side == "PUT"
        and np.isfinite(close_val) and np.isfinite(_s4_relief_thr)
        and close_val < _s4_relief_thr
    )
    _call_relief = bool(
        allowed_side == "CALL"
        and np.isfinite(close_val) and np.isfinite(_r4_relief_thr)
        and close_val > _r4_relief_thr
    )

    if _put_relief:
        logging.info(
            "[OSC_RELIEF][S4/R4_BREAK] "
            f"side=PUT reason=Price below S4, oscillator extreme bypassed "
            f"timestamp={timestamp} symbol={symbol} "
            f"close={close_val:.2f} s4={s4_val:.2f} s4_relief_thr={_s4_relief_thr:.2f} "
            f"atr={atr_val:.2f} RSI={rsi_val:.1f} CCI={cci_val:.1f}"
        )
        logging.info(
            "[OSC_CONTINUATION][ZoneC][RELAXED] "
            f"timestamp={timestamp} symbol={symbol} side=PUT "
            "reason=Price below S4-ATR relief."
        )
        st_details["osc_relief_override"] = True
        return True, allowed_side, "OK", st_details

    if _call_relief:
        logging.info(
            "[OSC_RELIEF][S4/R4_BREAK] "
            f"side=CALL reason=Price above R4, oscillator extreme bypassed "
            f"timestamp={timestamp} symbol={symbol} "
            f"close={close_val:.2f} r4={r4_val:.2f} r4_relief_thr={_r4_relief_thr:.2f} "
            f"atr={atr_val:.2f} RSI={rsi_val:.1f} CCI={cci_val:.1f}"
        )
        logging.info(
            "[OSC_CONTINUATION][ZoneC][RELAXED] "
            f"timestamp={timestamp} symbol={symbol} side=CALL "
            "reason=Price above R4+ATR relief."
        )
        st_details["osc_relief_override"] = True
        return True, allowed_side, "OK", st_details

    # Phase 6.1: Tilt-based oscillator relaxation — when tilt is aligned with side,
    # oscillator extremes reflect momentum continuation, not exhaustion.
    if _tilt_aligned:
        logging.info(
            f"[GOVERNANCE_EASY] timestamp={timestamp} symbol={symbol} "
            f"side={allowed_side} tilt={_tilt_state} "
            f"RSI={rsi_val:.1f} CCI={cci_val:.1f} "
            "reason=Tilt-aligned oscillator extreme bypassed"
        )
        st_details["governance"] = "EASY"
        st_details["tilt_osc_override"] = True
        return True, allowed_side, "OK", st_details

    # All cases exhausted — genuinely blocked (log only here so relief is not counted as block)
    logging.info(
        "[ENTRY BLOCKED][OSC_EXTREME] "
        f"timestamp={timestamp} symbol={symbol} allowed_side={allowed_side} "
        f"RSI={rsi_val} CCI={cci_val} "
        f"rsi_range=[{_eff_rsi_min},{_eff_rsi_max}] cci_range=[{_eff_cci_min},{_eff_cci_max}] "
        f"tier={_adx_osc_tier} atr_tier={_atr_expand_tier} ADX={adx_val:.1f} "
        "reason=Oscillator extreme outside all expanded thresholds, entry suppressed."
    )
    if zone_tag == "ZoneA":
        logging.info(
            "[OSC_EXTREME][ZoneA][BLOCKER] "
            f"timestamp={timestamp} symbol={symbol} side={allowed_side} "
            "reason=Inside S3-R3 strict threshold blocker."
        )
    elif zone_tag == "ZoneB":
        logging.info(
            "[OSC_REVERSAL][ZoneB][TRIGGER] "
            f"timestamp={timestamp} symbol={symbol} side={allowed_side} "
            "reason=Zone B extreme observed while blocked."
        )
    else:
        logging.info(
            "[OSC_CONTINUATION][ZoneC][RELAXED] "
            f"timestamp={timestamp} symbol={symbol} side={allowed_side} "
            "reason=Zone C extreme observed while blocked."
        )
    return False, allowed_side, "Oscillator extreme, entry suppressed.", st_details


def check_exit_condition(df_slice, state, option_price=None, option_volume=None, timestamp=None):
    """Evaluate exits with strict precedence and structured audit logs.

    Precedence:
    1) HFT override
    2) Stop loss
    3) PT/TG structured profit checks
    4) Minimum bar maturity gate
    5) Contextual exits (ATR/CPR/CAMARILLA mapped by signal source)
    """
    i = len(df_slice) - 1
    side = state["side"]
    position_side = state.get("position_side", "LONG")
    symbol = state.get("option_name", "N/A")
    position_id = state.get("position_id", "UNKNOWN")
    entry_price = state.get("buy_price", 0.0)
    entry_candle = state.get("entry_candle", i)
    current_ltp = option_price if option_price is not None else df_slice["close"].iloc[-1]
    option_volume = option_volume if option_volume is not None else 0.0
    timestamp = timestamp if timestamp is not None else dt.now(time_zone)

    atr_for_hold = float(state.get("atr_value", 0.0) or 0.0)
    min_bars_for_pt_tg = 2 if atr_for_hold < 30.0 else 3
    time_exit_candles = int(state.get("time_exit_candles", 8))
    osc_rsi_call = float(state.get("osc_rsi_call", 75.0))
    osc_rsi_put = float(state.get("osc_rsi_put", 25.0))
    osc_cci_call = float(state.get("osc_cci_call", 130.0))
    osc_cci_put = float(state.get("osc_cci_put", -130.0))
    osc_wr_call = float(state.get("osc_wr_call", -10.0))
    osc_wr_put = float(state.get("osc_wr_put", -88.0))
    bars_held = i - entry_candle
    stop = state.get("stop")
    pt = state.get("pt")
    tg = state.get("tg")
    trail_step = state.get("trail_step", 5)
    regime_ctx = state.get("regime_context", f"ATR={state.get('atr_value', 'N/A')}")
    trade_class = state.get("trade_class", "TREND")
    qty = state.get("quantity", 0)
    # Phase 3: frozen RegimeContext from entry time (None for legacy state dicts)
    _entry_rc = state.get("entry_regime_context")

    # ── Phase 4: Regime-Adaptive Exit Parameters ───────────────────────────
    # Derive adaptive hold times, trailing, and thresholds from entry regime.
    _rc_day_type = getattr(_entry_rc, "day_type", "UNKNOWN") if _entry_rc else state.get("day_type", "UNKNOWN")
    _rc_adx_tier = getattr(_entry_rc, "adx_tier", "ADX_DEFAULT") if _entry_rc else state.get("adx_tier", "ADX_DEFAULT")
    _rc_gap_tag = getattr(_entry_rc, "gap_tag", "NO_GAP") if _entry_rc else state.get("gap_tag", "NO_GAP")

    # Day type → min_hold adjustment (TREND_DAY: hold longer; RANGE_DAY: exit faster)
    _regime_min_hold_adj = 0
    if _rc_day_type == "TREND_DAY":
        _regime_min_hold_adj = 1      # +1 bar minimum hold
    elif _rc_day_type == "RANGE_DAY":
        _regime_min_hold_adj = -1     # -1 bar: quicker exit in range
    elif _rc_day_type == "GAP_DAY":
        _regime_min_hold_adj = 1      # +1 bar: let gap momentum develop

    # ADX tier → trailing step adjustment (strong ADX: wider trail; weak: tighter)
    if _rc_adx_tier == "ADX_STRONG_40":
        trail_step = max(trail_step, 8)   # wider trail for strong trends
    elif _rc_adx_tier == "ADX_WEAK_20":
        trail_step = max(1, trail_step - 2)  # tighter trail in chop

    # ADX tier → time_exit candles adjustment
    if _rc_adx_tier == "ADX_STRONG_40":
        time_exit_candles = max(time_exit_candles, time_exit_candles + 4)   # hold longer in strong trends
    elif _rc_adx_tier == "ADX_WEAK_20":
        time_exit_candles = max(4, time_exit_candles - 3)                  # exit sooner in weak trends

    # Gap days → suppress premature oscillator exits
    _gap_day_active = _rc_gap_tag in ("GAP_UP", "GAP_DOWN")

    # Apply min_hold adjustment from day type
    min_bars_for_pt_tg = max(1, min_bars_for_pt_tg + _regime_min_hold_adj)

    # Log regime-adaptive parameters once per exit check
    if _entry_rc is not None and not state.get("_regime_exit_logged", False):
        logging.info(
            f"[EXIT AUDIT][REGIME_ADAPTIVE] day_type={_rc_day_type} adx_tier={_rc_adx_tier} "
            f"gap_tag={_rc_gap_tag} min_hold_adj={_regime_min_hold_adj:+d} "
            f"trail_step={trail_step} time_exit={time_exit_candles} "
            f"gap_suppress={'ON' if _gap_day_active else 'OFF'}"
        )
        state["_regime_exit_logged"] = True

    if not state.get("is_open", False):
        logging.info(
            f"[EXIT SKIP] symbol={symbol} option_type={side} position_side={position_side} "
            f"position_id={position_id} reason=POSITION_CLOSED"
        )
        return False, None

    def contextual_exit_type() -> str:
        src = str(state.get("source", "")).upper()
        if "CPR" in src:
            return "CPR"
        if "CAMARILLA" in src:
            return "CAMARILLA"
        return "ATR"

    def audit(exit_type: str, reason: str, triggering_condition: str, premium_move=None) -> None:
        state["last_exit_type"] = exit_type
        state["last_triggering_condition"] = triggering_condition
        pm = f" premium_move={premium_move:.2f}" if premium_move is not None else ""
        _rc_label = _entry_rc.regime_label if _entry_rc is not None else regime_ctx
        _regime_note = (
            f" day={_rc_day_type} adx={_rc_adx_tier} gap={_rc_gap_tag}"
            f" min_hold={min_bars_for_pt_tg} trail={trail_step}"
            if _entry_rc is not None else ""
        )
        logging.info(
            "[EXIT AUDIT] "
            f"timestamp={timestamp} symbol={symbol} option_type={side} position_side={position_side} "
            f"exit_type={exit_type} "
            f"reason={reason} triggering_condition={triggering_condition} "
            f"candle={i} bars_held={bars_held} regime={_rc_label} position_id={position_id}{pm}{_regime_note}"
        )

    # 1) HFT exit - highest precedence override
    hf_mgr = state.get("hf_exit_manager")
    if hf_mgr is not None:
        try:
            if hf_mgr.check_exit(current_ltp, timestamp, current_volume=option_volume, bars_held=bars_held):
                hf_reason = hf_mgr.last_reason or "HF_EXIT"
                if hf_reason == "MOMENTUM_EXHAUSTION" and bars_held < 3:
                    logging.info(
                        "[EXIT SUPPRESSED] "
                        f"symbol={symbol} option_type={side} position_side={position_side} "
                        f"reason=Premature exit suppressed (MOMENTUM_EXHAUSTION), minimum hold enforced. bars_held={bars_held}"
                    )
                elif bars_held <= 0 and hf_reason not in {"SL_HIT", "PT_HIT"}:
                    logging.info(
                        "[EXIT SUPPRESSED] "
                        f"symbol={symbol} option_type={side} position_side={position_side} "
                        f"reason=Premature exit suppressed, minimum hold enforced. bars_held={bars_held}"
                    )
                else:
                    audit("HFT", hf_reason, f"hf_condition={hf_reason}")
                    logging.info(
                        f"{YELLOW}[EXIT][HF] {side} {hf_reason} ltp={current_ltp:.2f} "
                        f"entry={entry_price:.2f} bars_held={bars_held}{RESET}"
                    )
                    return True, hf_reason
        except Exception as e:
            logging.warning(f"[HF EXIT] manager error: {e}")

    # 2) Stop loss - with scalp survivability guardrail.
    # Scalp trades should survive initial noise for >=2 bars unless move is extreme.
    if stop is not None and current_ltp <= stop:
        if state.get("scalp_mode", False):
            atr_val = float(state.get("atr_value", 0.0) or 0.0)
            min_hold = 2 if atr_val < 30.0 else 3
            extreme_mul = float(state.get("scalp_extreme_move_atr_mult", SCALP_EXTREME_MOVE_ATR_MULT))
            extreme_pts = max(2.0, atr_val * extreme_mul * 0.06)
            emergency_stop = float(stop) - extreme_pts
            if bars_held < min_hold and current_ltp > emergency_stop:
                logging.info(
                    "[EXIT SUPPRESSED][SCALP_MIN_HOLD] "
                    f"symbol={symbol} option_type={side} bars_held={bars_held} "
                    f"min_hold={min_hold} ltp={current_ltp:.2f} stop={stop:.2f} "
                    f"emergency_stop={emergency_stop:.2f}"
                )
                return False, None
            if bars_held < min_hold and current_ltp <= emergency_stop:
                logging.info(
                    "[EXIT ALLOWED][SCALP_EXTREME_MOVE] "
                    f"symbol={symbol} option_type={side} bars_held={bars_held} "
                    f"ltp={current_ltp:.2f} emergency_stop={emergency_stop:.2f}"
                )
        else:
            atr_val = float(state.get("atr_value", 0.0) or 0.0)
            min_hold = 2 if atr_val < 30.0 else 3
            extreme_mul = float(state.get("trend_extreme_move_atr_mult", TREND_EXTREME_MOVE_ATR_MULT))
            extreme_pts = max(3.0, atr_val * extreme_mul * 0.06)
            emergency_stop = float(stop) - extreme_pts
            if bars_held < min_hold and current_ltp > emergency_stop:
                logging.info(
                    "[EXIT SUPPRESSED][TREND_MIN_HOLD] "
                    f"symbol={symbol} option_type={side} bars_held={bars_held} "
                    f"min_hold={min_hold} ltp={current_ltp:.2f} stop={stop:.2f} "
                    f"emergency_stop={emergency_stop:.2f}"
                )
                return False, None
            if bars_held < min_hold and current_ltp <= emergency_stop:
                logging.info(
                    "[EXIT ALLOWED][TREND_EXTREME_MOVE] "
                    f"symbol={symbol} option_type={side} bars_held={bars_held} "
                    f"ltp={current_ltp:.2f} emergency_stop={emergency_stop:.2f}"
                )
        audit("SL", "SL_HIT", f"ltp<={stop:.2f}")
        logging.info(
            f"{RED}[EXIT][SL_HIT] {side} ltp={current_ltp:.2f} stop={stop:.2f} bars_held={bars_held}{RESET}"
        )
        if state.get("scalp_mode", False):
            logging.info(
                f"[SCALP_SL_HIT] {side} trade_class={state.get('trade_class', 'SCALP')} "
                f"bars_held={bars_held} entry={state.get('buy_price', '?')} stop={stop:.2f} ltp={current_ltp:.2f}"
            )
        else:
            logging.info(
                f"[TREND_SL_HIT] {side} trade_class={state.get('trade_class', 'TREND')} "
                f"bars_held={bars_held} entry={state.get('buy_price', '?')} stop={stop:.2f} ltp={current_ltp:.2f}"
            )
            logging.info(
                f"[TREND_LOSS] {side} trade_class={state.get('trade_class', 'TREND')} "
                f"bars_held={bars_held} entry={state.get('buy_price', '?')} "
                f"stop={stop:.2f} ltp={current_ltp:.2f}"
            )
        return True, "SL_HIT"

    # 2B) Dip/rally scalp exits — only for SCALP trade class (P1-B / P2-D).
    if state.get("scalp_mode", False):
        # Survivability guardrail: must hold for at least 2 bars
        if bars_held >= 2:
            scalp_pt = float(state.get("scalp_pt_points", SCALP_PT_POINTS))
            premium_move = float(current_ltp - entry_price)
            logging.debug(
                f"[SCALP_EXIT_CHECK] bars_held={bars_held} "
                f"premium_move={premium_move:.2f} scalp_pt={scalp_pt:.2f}"
            )
            if premium_move >= scalp_pt:
                audit("SCALP_PT_HIT", "SCALP_PT_HIT", f"premium_move>={scalp_pt:.2f}", premium_move=premium_move)
                logging.info(
                    f"{GREEN}[EXIT][SCALP_PT_HIT] {side} "
                    f"premium_move={premium_move:.2f} target={scalp_pt:.2f} ltp={current_ltp:.2f}{RESET}"
                )
                return True, "SCALP_PT_HIT"
        # No SCALP_SL_HIT logic here. The main SL_HIT check at the top of the function
        # will use the ATR-based `state['stop']`.
        # Do not return, allow fall-through to other exit checks like HFT.

    # 3) PT/TG structured checks
    tg_hit = tg is not None and current_ltp >= tg
    pt_hit = pt is not None and current_ltp >= pt and not state.get("partial_booked", False)

    if bars_held <= 0 and not pt_hit:
        logging.info(
            "[EXIT SUPPRESSED] "
            f"symbol={symbol} option_type={side} position_side={position_side} "
            "reason=Premature exit suppressed, minimum hold enforced."
        )
        return False, None

    # 4) Min-bar maturity gate
    last_adx = float(df_slice["adx14"].iloc[-1]) if ("adx14" in df_slice.columns and len(df_slice) > 0 and pd.notna(df_slice["adx14"].iloc[-1])) else float("nan")
    tg_adx_override = bool(tg_hit and np.isfinite(last_adx) and last_adx > 40.0)
    if bars_held < min_bars_for_pt_tg and (tg_hit or pt_hit):
        if tg_adx_override:
            logging.info(
                "[EXIT ALLOWED][TG_ADX_OVERRIDE] "
                f"symbol={symbol} option_type={side} bars_held={bars_held} "
                f"min_hold={min_bars_for_pt_tg} adx={last_adx:.1f} tg={tg:.2f}"
            )
            audit("TG", "TARGET_HIT", f"ltp>={tg:.2f} adx={last_adx:.1f}")
            return True, "TARGET_HIT"
        if bars_held <= 0 and pt_hit:
            audit("PT", "PT_HIT", f"ltp>={pt:.2f}")
            state["partial_booked"] = True
            if (state.get("stop") or 0) < entry_price:
                state["stop"] = entry_price
            return True, "PT_HIT"
        audit("MIN_BAR", "DEFERRED", f"bars_held<{min_bars_for_pt_tg}")
        if tg_hit:
            logging.info(
                "[TG_HIT_EXIT_SUPPRESSED] "
                f"symbol={symbol} option_type={side} bars_held={bars_held} "
                f"min_hold={min_bars_for_pt_tg} adx={last_adx if np.isfinite(last_adx) else 'N/A'}"
            )
            logging.info(
                f"[SURVIVABILITY_OVERRIDE] Minimum hold enforced. "
                f"{YELLOW}[EXIT DEFERRED] TG hit before min bars ({bars_held} < {min_bars_for_pt_tg}). "
                f"ltp={current_ltp:.2f} tg={tg:.2f} defer_until={entry_candle + min_bars_for_pt_tg}{RESET}"
            )
        elif state.get("pt_deferred_logged", 0) == 0:
            logging.info(
                f"[SURVIVABILITY_OVERRIDE] Minimum hold enforced. "
                f"{YELLOW}[EXIT DEFERRED] PT hit before min bars ({bars_held} < {min_bars_for_pt_tg}). "
                f"ltp={current_ltp:.2f} pt={pt:.2f} defer_until={entry_candle + min_bars_for_pt_tg}{RESET}"
            )
            state["pt_deferred_logged"] = 1
        return False, None

    if tg_hit:
        # P2-C: Partial TG exit — first TG hit exits 50% of quantity.
        # The remaining 50% continues with SL ratcheted to TG (break-even+).
        # On second trigger (full_tg_booked=True already consumed), full exit.
        if not state.get("partial_tg_booked", False):
            partial_qty = max(1, int(state.get("quantity", 0) * PARTIAL_TG_QTY_FRAC))
            state["partial_tg_booked"] = True
            state["partial_tg_qty"]    = partial_qty
            state["stop"]              = tg   # ratchet SL to TG level
            state["pt_deferred_logged"] = 0
            audit("TG", "TG_PARTIAL_EXIT", f"ltp>={tg:.2f} qty={partial_qty}")
            logging.info(
                f"{GREEN}[EXIT][PARTIAL_EXIT][TG] {side} ltp={current_ltp:.2f} "
                f"tg={tg:.2f} partial_qty={partial_qty} "
                f"remaining_qty={state.get('quantity', 0) - partial_qty} "
                f"SL_ratcheted_to={tg:.2f} bars_held={bars_held}{RESET}"
            )
            return True, "TG_PARTIAL_EXIT"
        else:
            # Remaining 50% has now run beyond TG — full close
            audit("TG", "TARGET_HIT", f"ltp>={tg:.2f} full_exit")
            logging.info(
                f"{GREEN}[EXIT][TG_HIT][FULL] {side} ltp={current_ltp:.2f} "
                f"tg={tg:.2f} bars_held={bars_held}{RESET}"
            )
            return True, "TARGET_HIT"

    if pt_hit and bars_held >= min_bars_for_pt_tg:
        audit("PT", "PT_HIT", f"ltp>={pt:.2f}")
        state["partial_booked"] = True
        state["pt_deferred_logged"] = 0
        if (state.get("stop") or 0) < entry_price:
            state["stop"] = entry_price
        logging.info(
            f"{GREEN}[PARTIAL] {side} ltp={current_ltp:.2f} >= pt={pt:.2f} bars_held={bars_held} "
            f"stop_locked={entry_price:.2f}{RESET}"
        )

    # Trailing stop update only after maturity.
    pnl = current_ltp - entry_price
    if bars_held >= min_bars_for_pt_tg and pnl >= 5 and trail_step > 0:
        new_stop = current_ltp - trail_step
        if new_stop > state.get("stop", 0):
            state["stop"] = new_stop
            state["trail_updates"] = state.get("trail_updates", 0) + 1
            logging.info(
                f"{CYAN}[TRAIL] {side} stop={new_stop:.2f} ltp={current_ltp:.2f} bars_held={bars_held}{RESET}"
            )

    # 5) Contextual exits (ATR/CPR/CAMARILLA)
    osc_hits = []
    try:
        cci_s = calculate_cci(df_slice) if "cci20" not in df_slice.columns else df_slice["cci20"]
        cci = float(cci_s.iloc[-1]) if not cci_s.empty else None
        if cci and not pd.isna(cci):
            if side == "CALL" and cci > osc_cci_call:
                osc_hits.append(f"CCI={cci:.0f}")
            if side == "PUT" and cci < osc_cci_put:
                osc_hits.append(f"CCI={cci:.0f}")
    except Exception:
        pass

    try:
        rsi_col = df_slice["rsi14"] if "rsi14" in df_slice.columns else pd.Series(dtype=float)
        rsi = float(rsi_col.iloc[-1]) if not rsi_col.empty else None
        if rsi and not pd.isna(rsi):
            if side == "CALL" and rsi > osc_rsi_call:
                osc_hits.append(f"RSI={rsi:.0f}")
            if side == "PUT" and rsi < osc_rsi_put:
                osc_hits.append(f"RSI={rsi:.0f}")
    except Exception:
        pass

    try:
        wr = williams_r(df_slice)
        if wr and not pd.isna(wr):
            if side == "CALL" and wr > osc_wr_call:
                osc_hits.append(f"WR={wr:.0f}")
            if side == "PUT" and wr < osc_wr_put:
                osc_hits.append(f"WR={wr:.0f}")
    except Exception:
        pass

    # Contextual structure exits keyed by source/pivot context.
    ctx_text = (
        f"{str(state.get('source', '')).upper()}|"
        f"{str(state.get('pivot', '')).upper()}|"
        f"{str(state.get('regime_context', '')).upper()}"
    )
    if bars_held >= min_bars_for_pt_tg:
        if "CPR" in ctx_text and len(df_slice) >= 2:
            prev_close = float(df_slice["close"].iloc[-2])
            if side == "CALL" and current_ltp < prev_close:
                audit("CPR", "CPR_CONTEXT_EXIT", "close<prev_close")
                logging.info(f"{YELLOW}[EXIT][CPR] CALL context breakdown bars_held={bars_held}{RESET}")
                return True, "CPR_CONTEXT_EXIT"
            if side == "PUT" and current_ltp > prev_close:
                audit("CPR", "CPR_CONTEXT_EXIT", "close>prev_close")
                logging.info(f"{YELLOW}[EXIT][CPR] PUT context breakdown bars_held={bars_held}{RESET}")
                return True, "CPR_CONTEXT_EXIT"
        if "CAMARILLA" in ctx_text and len(df_slice) >= 2:
            prev_open = float(df_slice["open"].iloc[-2])
            if side == "CALL" and current_ltp < prev_open:
                audit("CAMARILLA", "CAM_CONTEXT_EXIT", "close<prev_open")
                logging.info(f"{YELLOW}[EXIT][CAMARILLA] CALL context breakdown bars_held={bars_held}{RESET}")
                return True, "CAM_CONTEXT_EXIT"
            if side == "PUT" and current_ltp > prev_open:
                audit("CAMARILLA", "CAM_CONTEXT_EXIT", "close>prev_open")
                logging.info(f"{YELLOW}[EXIT][CAMARILLA] PUT context breakdown bars_held={bars_held}{RESET}")
                return True, "CAM_CONTEXT_EXIT"

    # Phase 4: Gap day suppression — on gap days, require 3+ osc_hits instead of 2
    # to avoid premature exits when gap momentum may persist
    _osc_exit_threshold = 3 if _gap_day_active else 2

    if len(osc_hits) >= _osc_exit_threshold and bars_held >= min_bars_for_pt_tg:
        if OSCILLATOR_EXIT_MODE == "HARD":
            x_type = contextual_exit_type()
            audit(x_type, "OSC_EXHAUSTION", f"osc_hits={'+'.join(osc_hits)} gap_suppress={_gap_day_active}")
            logging.info(f"{YELLOW}[EXIT][OSC] {side} {'+'.join(osc_hits)} bars_held={bars_held}{RESET}")
            return True, "OSC_EXHAUSTION"
        # TRAIL mode: lock SL at entry (breakeven) instead of closing
        _prev_stop = state.get("stop", 0) or 0
        if _prev_stop < entry_price:
            state["stop"] = entry_price
            logging.info(
                f"{YELLOW}[OSC_TRAIL] {side} osc_hits={'+'.join(osc_hits)} "
                f"SL locked at entry={entry_price:.2f} (was {_prev_stop:.2f}) "
                f"bars_held={bars_held}{RESET}"
            )

    if "supertrend_bias" in df_slice.columns and len(df_slice) >= 2 and bars_held >= min_bars_for_pt_tg:
        def norm(b):
            return "UP" if b in ("UP", "BULLISH") else ("DOWN" if b in ("DOWN", "BEARISH") else "N")
        b1 = norm(df_slice["supertrend_bias"].iloc[-1])
        b2 = norm(df_slice["supertrend_bias"].iloc[-2])
        if side == "CALL" and b1 == "DOWN" and b2 == "DOWN":
            x_type = contextual_exit_type()
            audit(x_type, "ST_FLIP", "supertrend=DOWNx2")
            logging.info(f"{YELLOW}[EXIT][ST_FLIP] CALL bearish x2 bars_held={bars_held}{RESET}")
            return True, "ST_FLIP"
        if side == "PUT" and b1 == "UP" and b2 == "UP":
            x_type = contextual_exit_type()
            audit(x_type, "ST_FLIP", "supertrend=UPx2")
            logging.info(f"{YELLOW}[EXIT][ST_FLIP] PUT bullish x2 bars_held={bars_held}{RESET}")
            return True, "ST_FLIP"

    last_c = df_slice.iloc[-1]
    is_reversal = ((side == "CALL" and last_c["close"] < last_c["open"]) or
                   (side == "PUT" and last_c["close"] > last_c["open"]))
    state["consec_count"] = (state.get("consec_count", 0) + 1) if is_reversal else 0
    rev_atr = float(state.get("atr_value", 0.0) or 0.0)
    rev_threshold = 3
    if np.isfinite(rev_atr) and rev_atr >= 30.0 and np.isfinite(last_adx) and last_adx >= 25.0:
        rev_threshold = 2
    # Phase 4: Strong ADX → need more consecutive reversals before exiting (trend protects)
    if _rc_adx_tier == "ADX_STRONG_40":
        rev_threshold = max(rev_threshold, 3)
    if state["consec_count"] >= rev_threshold and bars_held >= min_bars_for_pt_tg:
        x_type = contextual_exit_type()
        audit(x_type, "REVERSAL_EXIT", f"reversal_count={state['consec_count']} threshold={rev_threshold}")
        logging.info(
            f"{YELLOW}[EXIT][REVERSAL] {side} {state['consec_count']} "
            f"bars_held={bars_held} threshold={rev_threshold} "
            f"atr={rev_atr if np.isfinite(rev_atr) else 'N/A'} "
            f"adx={last_adx if np.isfinite(last_adx) else 'N/A'}{RESET}"
        )
        return True, "REVERSAL_EXIT"

    ema9 = df_slice["close"].ewm(span=9, adjust=False).mean().iloc[-1]
    ema13 = df_slice["close"].ewm(span=13, adjust=False).mean().iloc[-1]
    ema_gap = abs(ema9 - ema13)
    _, momentum = momentum_ok(df_slice, side)
    momentum = momentum or 0

    prev_gap = state.get("prev_gap", ema_gap)
    peak_momentum = state.get("peak_momentum", abs(momentum))
    if ema_gap > prev_gap:
        state["prev_gap"] = ema_gap
        state["peak_momentum"] = max(peak_momentum, abs(momentum))
        state["plateau_count"] = 0
        logging.debug(
            f"[EMA PLATEAU RESET] symbol={symbol} option_type={side} "
            f"ema_gap={ema_gap:.4f} peak_momentum={state['peak_momentum']:.4f}"
        )
        return False, None

    state["plateau_count"] = state.get("plateau_count", 0) + 1
    state["prev_gap"] = ema_gap
    if state["plateau_count"] >= 2 and abs(momentum) < peak_momentum * 0.4 and len(osc_hits) >= 1:
        x_type = contextual_exit_type()
        momentum_reason = "MOMENTUM_EXHAUSTION" if not state.get("scalp_mode", False) else "MOMENTUM_EXIT"
        audit(x_type, momentum_reason, "ema_plateau+momentum_drop")
        logging.info(
            f"{YELLOW}[EXIT][MOMENTUM] {side} reason={momentum_reason} "
            f"plateau+drop+osc bars_held={bars_held}{RESET}"
        )
        return True, momentum_reason

    if i - entry_candle >= time_exit_candles and state.get("trail_updates", 0) == 0:
        audit("ATR", "TIME_EXIT", f"no_trail_for_{time_exit_candles}_candles")
        logging.info(f"{YELLOW}[EXIT][TIME] {side} {i-entry_candle} candles no trail{RESET}")
        return True, "TIME_EXIT"

    return False, None


def build_dynamic_levels(entry_price, atr, side, entry_candle,
                         rr_ratio=2.0, profit_loss_point=5, candles_df=None,
                         trail_start_frac=0.5, adx_value: float = 0.0):
    """
    Build SL/PT/TG/trail for OPTIONS BUYING (long call or long put).

    ✅ v4.0 REGIME-ADAPTIVE SL/TG MODEL — R:R > 1.0 INVARIANT
    ═════════════════════════════════════════════════════════════

    Core principle: SL < TG in EVERY regime row, guaranteeing positive
    expectancy at achievable win rates (≥ 50%).

    SL tiers (ADX-adaptive — tighter than v3):
    - ADX > 40 (strong trend):  SL = 1.5 × ATR  (trend protects position)
    - ADX 20–40 (moderate):     SL = 1.2 × ATR
    - ADX < 20  (choppy):       SL = 0.8 × ATR  (quick exit in noise)

    PT/TG (ATR-regime — wider than v3):
    - Regime 1 (ATR ≤ 60):    PT=1.5×ATR  TG=2.0×ATR  R:R ≥ 1.67
    - Regime 2 (60< ATR≤100): PT=1.8×ATR  TG=2.5×ATR  R:R ≥ 1.67
    - Regime 3 (100<ATR≤150): PT=2.0×ATR  TG=3.0×ATR  R:R ≥ 2.00
    - Regime 4 (150<ATR≤250): PT=2.5×ATR  TG=3.5×ATR  R:R ≥ 2.33
    - Regime 5 (ATR > 250):   Skip (too volatile)
    """
    if entry_price is None or entry_price <= 0:
        logging.warning(f"[LEVELS] Invalid entry_price={entry_price}")
        return {"valid": False}

    if atr is None or pd.isna(atr):
        logging.warning("[LEVELS] ATR unavailable")
        return {"valid": False}

    # ════════ ATR-SCALED SL — ADX TIER (v4: tighter SL, R:R > 1.0) ════════
    adx_val_f = float(adx_value) if adx_value and np.isfinite(float(adx_value)) else 0.0
    if adx_val_f > 40:
        sl_mult  = 1.5
        sl_tier  = "ADX_STRONG_40"
    elif adx_val_f > 0 and adx_val_f < 20:
        sl_mult  = 0.8
        sl_tier  = "ADX_WEAK_20"
    else:
        sl_mult  = 1.2
        sl_tier  = "ADX_DEFAULT"

    # ════════ ATR EXPANSION — volatile regimes need wider SL breathing room ════════
    # Mirrors the oscillator gate ATR tier logic (same thresholds: 1.3× and 1.5× MA).
    # In high-volatility sessions, a 1.2×ATR stop gets hit on the first bar.
    # Expanding sl_mult proportionally reduces premature stop-outs without widening
    # in calm/normal conditions.
    _atr_sl_expand = 1.0
    _sl_atr_tier   = "ATR_DEFAULT"
    _sl_atr_series = (
        candles_df["atr14"].dropna()
        if candles_df is not None and not candles_df.empty and "atr14" in candles_df.columns
        else pd.Series(dtype=float)
    )
    if len(_sl_atr_series) >= 10 and np.isfinite(atr) and atr > 0:
        _atr_sl_ma = float(_sl_atr_series.tail(10).mean())
        if _atr_sl_ma > 0 and atr > 1.5 * _atr_sl_ma:
            _atr_sl_expand = 1.75
            _sl_atr_tier   = "ATR_HIGH"
        elif _atr_sl_ma > 0 and atr > 1.2 * _atr_sl_ma:
            _atr_sl_expand = 1.35
            _sl_atr_tier   = "ATR_ELEVATED"
    sl_mult = min(round(sl_mult * _atr_sl_expand, 3), 2.0)

    stop = max(round(entry_price - sl_mult * atr, 2), 1.0)

    # ════════ VOLATILITY REGIME (PT / TG / TRAIL) — v4: wider targets ════════
    # PT/TG are ATR-multiple based. Widened to guarantee SL < PT < TG.
    if atr <= 60:
        regime   = "VERY_LOW"
        pt_mult  = 1.5
        tg_mult  = 2.0
        step_pct = 0.02
    elif atr <= 100:
        regime   = "LOW"
        pt_mult  = 1.8
        tg_mult  = 2.5
        step_pct = 0.025
    elif atr <= 150:
        regime   = "MODERATE"
        pt_mult  = 2.0
        tg_mult  = 3.0
        step_pct = 0.03
    elif atr <= 250:
        regime   = "HIGH"
        pt_mult  = 2.5
        tg_mult  = 3.5
        step_pct = 0.035
    else:
        logging.warning(f"[LEVELS][EXTREME_ATR] {atr:.0f} — skipping trade (too volatile for quick booking)")
        return {"valid": False}

    # v4: Strong ADX (>40) — let winners run further in strong trends
    if adx_val_f > 40:
        pt_mult = max(pt_mult, 2.5)  # At least 2.5x ATR for PT
        tg_mult = max(tg_mult, 3.5)  # At least 3.5x ATR for TG
        regime = regime + "_STRONG_ADX"
        logging.info(
            f"[LEVELS][SURVIVABILITY_GUARD] ADX={adx_val_f:.1f} > 40 - "
            f"scaled PT={pt_mult:.1f}xATR TG={tg_mult:.1f}xATR"
        )

    partial_target = round(entry_price + (pt_mult * atr), 2)
    full_target    = round(entry_price + (tg_mult * atr), 2)
    trail_start    = round((pt_mult * atr) * float(trail_start_frac), 2)
    trail_step     = round(max(entry_price * step_pct, 1.5), 2)

    sl_dist_pct = (entry_price - stop) / entry_price * 100

    # ════════ AUDIT LOG ════════
    logging.info(
        f"{CYAN}[LEVELS][ATR_SL] {regime:12} | {side} entry={entry_price:.2f} | "
        f"SL={stop:.2f}(-{sl_dist_pct:.1f}% | {sl_mult}×ATR tier={sl_tier}/{_sl_atr_tier}) "
        f"PT={partial_target:.2f}(+{pt_mult:.2f}xATR) "
        f"TG={full_target:.2f}(+{tg_mult:.2f}xATR) "
        f"| TrailStart={trail_start:.2f} TrailStep={trail_step:.2f} "
        f"ATR={atr:.1f} ADX={adx_val_f:.1f}{RESET}"
    )

    return {
        "valid":      True,
        "stop":       stop,
        "pt":         partial_target,
        "tg":         full_target,
        "trail_start": trail_start,
        "trail_step": trail_step,
        "regime":     regime,
        "sl_mult":    sl_mult,
        "sl_tier":    sl_tier,
        "sl_atr_tier": _sl_atr_tier,
    }

def update_trailing_stop(current_price, entry_price, current_stop,
                         trail_start_pnl, trail_step_points, buffer_points=12,
                         atr=None, side="CALL", state=None):
    """
    Update trailing stop once partial target booked.
    Adjustments for live market:
    - Buffered trailing (≥ buffer_points move in favor)
    - Ratchets stop upward/downward depending on side
    """

    _ = trail_start_pnl
    pnl = current_price - entry_price
    if buffer_points is None:
        if atr is None or pd.isna(atr):
            buffer_points = 12.0
        elif atr <= 60:
            buffer_points = 8.0
        elif atr <= 100:
            buffer_points = 10.0
        elif atr <= 150:
            buffer_points = 12.0
        else:
            buffer_points = 14.0

    # Only trail if price has moved enough in favor
    if abs(pnl) >= buffer_points and trail_step_points > 0:
        candidate = current_price - trail_step_points if pnl > 0 else current_price + trail_step_points
        new_stop = max(current_stop, candidate) if pnl > 0 else min(current_stop, candidate)

        if new_stop != current_stop:
            logging.info(
                f"{YELLOW}[TRAIL UPDATE] side={side} stop_from={current_stop:.2f} "
                f"stop_to={new_stop:.2f} pnl={pnl:.2f} buffer={buffer_points:.2f}{RESET}"
            )
            if isinstance(state, dict):
                state["stop"] = new_stop
        return new_stop

    return current_stop


# ===== PAPER/LIVE STATE INIT =====

risk_info = {
    "session_pnl": 0,
    "peak_equity": 0,
    "halt_trading": False
}

# Detect offline replay mode: skip paper/live init that needs setup.py (Fyers API)
import sys as _sys
_is_offline_replay = ("--db" in _sys.argv) if hasattr(_sys, "argv") else False

paper_info = None
live_info = None

if not _is_offline_replay and account_type == 'PAPER':
    try:
        paper_info = load(account_type)
    except Exception:
        column_names = ['time', 'ticker', 'price', 'action', 'stop_price', 'take_profit', 'spot_price', 'quantity']
        filled_df = pd.DataFrame(columns=column_names)
        filled_df.set_index('time', inplace=True)

        from setup import option_chain
        from config import strike_diff

        def _init_otm(spot_price_, side, points=0):
            if spot_price_ is None:
                return None, None
            base_strike = round(spot_price_ / strike_diff) * strike_diff
            otm_strike = base_strike + points if side == 'CE' else base_strike - points
            sel = option_chain[
                (option_chain['strike_price'] == otm_strike) &
                (option_chain['option_type'] == side)
            ]['symbol']
            if sel.empty:
                side_df = option_chain[option_chain['option_type'] == side].copy()
                if side_df.empty:
                    logging.error(f"No options available for side={side} in option_chain")
                    return None, None
                side_df['strike_diff_abs'] = (side_df['strike_price'] - otm_strike).abs()
                side_df = side_df.sort_values('strike_diff_abs')
                symbol = side_df.iloc[0]['symbol']
                strike = side_df.iloc[0]['strike_price']
                logging.warning(f"Fallback OTM for {side}: requested {otm_strike}, using {strike}")
                return symbol, strike
            symbol = sel.squeeze()
            return symbol, otm_strike

        call_option, call_buy_strike = _init_otm(spot_price, 'CE', 0)
        put_option, put_buy_strike   = _init_otm(spot_price, 'PE', 0)
        logging.info('[PAPER INIT] started')

        paper_info = {
            'call_buy': {
                'option_name': call_option,
                'trade_flag': 0,
                'buy_price': 0,
                'current_stop_price': 0,
                'current_profit_price': 0,
                'target_method': "auto",
                'target_reached': False,
                'filled_df': filled_df.copy(),
                'underlying_price_level': 0,
                'quantity': quantity,
                'pnl': 0,
                'trail_start_pnl': 0,
                'trail_step_points': 0,
                'reason': None,
                'confidence': 0,
                'order_id': None,
                'entry_time': None,
                'position_id': None,
                'is_open': False,
                'lifecycle_state': 'EXIT',
                'position_side': 'LONG',
                'scalp_mode': False,
                'scalp_pt_points': SCALP_PT_POINTS,
                'scalp_sl_points': SCALP_SL_POINTS,
                'partial_booked': False,
            },
            'put_buy': {
                'option_name': put_option,
                'trade_flag': 0,
                'buy_price': 0,
                'current_stop_price': 0,
                'current_profit_price': 0,
                'target_method': "auto",
                'target_reached': False,
                'filled_df': filled_df.copy(),
                'underlying_price_level': 0,
                'quantity': quantity,
                'pnl': 0,
                'trail_start_pnl': 0,
                'trail_step_points': 0,
                'reason': None,
                'confidence': 0,
                'order_id': None,
                'entry_time': None,
                'position_id': None,
                'is_open': False,
                'lifecycle_state': 'EXIT',
                'position_side': 'LONG',
                'scalp_mode': False,
                'scalp_pt_points': SCALP_PT_POINTS,
                'scalp_sl_points': SCALP_SL_POINTS,
                'partial_booked': False,
            },
            'condition': False,
            'total_pnl': 0,
            'trade_count': 0,
            'max_trades': MAX_TRADES_PER_DAY,
            'trend_trade_count': 0,
            'scalp_trade_count': 0,
            'max_trades_trend': MAX_TRADE_TREND,
            'max_trades_scalp': MAX_TRADE_SCALP,
            'last_exit_time': None,
            'scalp_cooldown_until': None,
            'scalp_last_burst_key': None,
            'scalp_hist': {'CALL': [], 'PUT': []},
        }
    paper_info = _hydrate_runtime_state(paper_info, account_type, "PAPER")

elif not _is_offline_replay:
    try:
        live_info = load(account_type)
    except Exception:
        column_names = ['time', 'ticker', 'price', 'action', 'stop_price', 'take_profit', 'spot_price', 'quantity']
        filled_df = pd.DataFrame(columns=column_names)
        filled_df.set_index('time', inplace=True)

        from setup import option_chain
        from config import strike_diff

        def _init_otm(spot_price_, side, points=0):
            if spot_price_ is None:
                return None, None
            base_strike = round(spot_price_ / strike_diff) * strike_diff
            otm_strike = base_strike + points if side == 'CE' else base_strike - points
            sel = option_chain[
                (option_chain['strike_price'] == otm_strike) &
                (option_chain['option_type'] == side)
            ]['symbol']
            if sel.empty:
                side_df = option_chain[option_chain['option_type'] == side].copy()
                if side_df.empty:
                    logging.error(f"No options available for side={side} in option_chain")
                    return None, None
                side_df['strike_diff_abs'] = (side_df['strike_price'] - otm_strike).abs()
                side_df = side_df.sort_values('strike_diff_abs')
                symbol = side_df.iloc[0]['symbol']
                strike = side_df.iloc[0]['strike_price']
                logging.warning(f"Fallback OTM for {side}: requested {otm_strike}, using {strike}")
                return symbol, strike
            symbol = sel.squeeze()
            return symbol, otm_strike

        call_option, call_buy_strike = _init_otm(spot_price, 'CE', 0)
        put_option, put_buy_strike   = _init_otm(spot_price, 'PE', 0)
        logging.info('[LIVE INIT] started')

        live_info = {
            'call_buy': {
                'option_name': call_option,
                'trade_flag': 0,
                'buy_price': 0,
                'current_stop_price': 0,
                'current_profit_price': 0,
                'target_method': "auto",
                'target_reached': False,
                'filled_df': filled_df.copy(),
                'underlying_price_level': 0,
                'quantity': quantity,
                'pnl': 0,
                'trail_start_pnl': 0,
                'trail_step_points': 0,
                'reason': None,
                'confidence': 0,
                'order_id': None,
                'entry_time': None,
                'position_id': None,
                'is_open': False,
                'lifecycle_state': 'EXIT',
                'position_side': 'LONG',
                'scalp_mode': False,
                'scalp_pt_points': SCALP_PT_POINTS,
                'scalp_sl_points': SCALP_SL_POINTS,
                'partial_booked': False,
            },
            'put_buy': {
                'option_name': put_option,
                'trade_flag': 0,
                'buy_price': 0,
                'current_stop_price': 0,
                'current_profit_price': 0,
                'target_method': "auto",
                'target_reached': False,
                'filled_df': filled_df.copy(),
                'underlying_price_level': 0,
                'quantity': quantity,
                'pnl': 0,
                'trail_start_pnl': 0,
                'trail_step_points': 0,
                'reason': None,
                'confidence': 0,
                'order_id': None,
                'entry_time': None,
                'position_id': None,
                'is_open': False,
                'lifecycle_state': 'EXIT',
                'position_side': 'LONG',
                'scalp_mode': False,
                'scalp_pt_points': SCALP_PT_POINTS,
                'scalp_sl_points': SCALP_SL_POINTS,
                'partial_booked': False,
            },
            'condition': False,
            'total_pnl': 0,
            'trade_count': 0,
            'max_trades': MAX_TRADES_PER_DAY,
            'trend_trade_count': 0,
            'scalp_trade_count': 0,
            'max_trades_trend': MAX_TRADE_TREND,
            'max_trades_scalp': MAX_TRADE_SCALP,
            'last_exit_time': None,
            'scalp_cooldown_until': None,
            'scalp_last_burst_key': None,
            'scalp_hist': {'CALL': [], 'PUT': []},
        }
    live_info = _hydrate_runtime_state(live_info, account_type, "LIVE")


# ===== Broker order functions =====

def send_live_entry_order(symbol, qty, side, buffer=ENTRY_OFFSET):
    """
    Place a live LIMIT entry order via Fyers API.
    Baseline logic: entry price = LTP - buffer (min 0.05).
    """
    try:
        # Get LTP
        quote = fyers.quotes({"symbols": symbol})
        ltp = quote["d"][0]["v"]["lp"]

        # Calculate limit price with buffer
        limit_price = max(ltp - buffer, 0.05)

        order_data = {
            "symbol": symbol,
            "qty": qty,
            "type": 1,              # LIMIT
            "side": side,           # 1=BUY, -1=SELL
            "productType": "INTRADAY",
            "limitPrice": limit_price,
            "stopPrice": 0,
            "validity": "DAY",
            "stopLoss": 0,
            "takeProfit": 0,
            "offlineOrder": False,
            "disclosedQty": 0,
            "isSliceOrder": False,
            "orderTag": str(side)
        }

        response = fyers.place_order(data=order_data)

        if response.get("s") == "ok":
            logging.info(f"{YELLOW}[LIVE ENTRY] {symbol} Qty={qty}{RESET}")
            return True, response.get("id")

        else:
            logging.error(f"{CYAN}[LIVE ENTRY FAILED] {symbol} {response}{RESET}")
            return False, None

    except Exception as e:
        logging.error(f"{CYAN}[LIVE ENTRY ERROR] {symbol} {e}{RESET}")
        return False, None


def send_live_exit_order(symbol, qty, reason):
    """
    Place a live MARKET exit order via Fyers API.
    Baseline logic (8th Jan):
    - Always SELL (-1 side)
    - MARKET type (type=2)
    - Tag order with exit reason for audit trail
    """
    try:
        order_data = {
            "symbol": symbol,
            "qty": qty,
            "type": 2,              # MARKET
            "side": -1,             # SELL
            "productType": "INTRADAY",
            "limitPrice": 0,
            "stopPrice": 0,
            "validity": "DAY",
            "stopLoss": 0,
            "takeProfit": 0,
            "offlineOrder": False,
            "disclosedQty": 0,
            "isSliceOrder": False,
            "orderTag": str(reason)  # ensure string tag
        }

        response = fyers.place_order(data=order_data)

        if response.get("s") == "ok":
            logging.info(
                f"{YELLOW}[LIVE EXIT][{reason}] {symbol} Qty={qty}{RESET}"
                f"OrderID={response.get('id')}{RESET}"
            )
            return True, response.get("id")
        else:
            logging.error(f"{RED}[LIVE EXIT FAILED] {symbol} {response}{RESET}")
            return False, None

    except Exception as e:
        logging.error(f"{RED}{RED}[LIVE EXIT ERROR] {symbol} {e}{RESET}{RESET}")
        return False, None
    
def send_paper_exit_order(symbol, qty, reason):
    """
    Simulated exit for paper mode.
    Baseline logic (8th Jan):
    - Always log the exit with reason and quantity
    - Return success flag and synthetic order_id
    """
    logging.info(f"{CYAN}[PAPER EXIT][{reason}] {symbol} Qty={qty}{RESET}")
    return True, f"paper_exit_{symbol}_{reason}"

def update_order_status(order_id, status, filled_qty, avg_price, symbol):
    """
    Update the global filled_df ledger with order status.
    Baseline logic (8th Jan):
    - If order_id exists, update row
    - Else, append new row
    - Log every update for audit trail
    """
    global filled_df
    color = status_color(status)

    if order_id in filled_df.index:
        filled_df.loc[order_id, "status"] = status
        filled_df.loc[order_id, "filled_qty"] = filled_qty
        filled_df.loc[order_id, "avg_price"] = avg_price
        logging.info(f"{YELLOW}[LEDGER UPDATED] {order_id} -> {status}{RESET}")
    else:
        new_row = pd.DataFrame({
            "status": [status],
            "filled_qty": [filled_qty],
            "avg_price": [avg_price],
            "symbol": [symbol]
        }, index=[order_id])
        filled_df = pd.concat([filled_df, new_row])
        logging.info(f"{YELLOW}[LEDGER APPENDED] {order_id} -> {status}{RESET}")

# ===== Order status polling =====
def check_order_status(order_id, fyers):
    """
    Poll broker for order status and update ledger.
    Baseline logic (8th Jan):
    - Query orderbook by order_id
    - Map status code to human-readable string
    - Update global filled_df via update_order_status
    - Return (status, traded_price)
    """
    try:
        response = fyers.orderbook(data={"id": order_id})

        if response.get("s") == "ok":
            order = response.get("orderBook", [{}])[0]

            status_code   = order.get("status")
            filled_qty    = order.get("filledQty", 0)
            traded_price  = order.get("tradedPrice", 0)
            symbol        = order.get("symbol")

            status = map_status_code(status_code)
            update_order_status(order_id, status, filled_qty, traded_price, symbol)

            return status, traded_price

        else:
            logging.warning(
                f"{RED}[ORDER STATUS] Failed for {order_id}: {response}{RESET}"
            )
            return None, None

    except Exception as e:
        logging.error(f"{RED}[ORDER STATUS ERROR] {e}{RESET}")
        return None, None

# =================== Dynamic Order Processing / ATR based SL/PT/TG ==========================

# ===== process_order =====
def process_order(state, df_slice, info, spot_price,
                  account_type="paper", mode="LIVE"):
    """
    Manage exits for an active trade using SL/Target + hybrid exit logic.
    - mode="LIVE": full DB persistence + live orders
    - mode="REPLAY": skip DB writes, only simulate exits
    """

    side   = state["side"]
    position_side = state.get("position_side", "LONG")
    symbol = state.get("option_name", "N/A")
    position_id = state.get("position_id", "UNKNOWN")
    entry  = state.get("buy_price", 0)
    qty    = state.get("quantity", 0)
    entry_candle = state.get("entry_candle", 0)

    current_candle = df_slice.iloc[-1]
    bars_held = len(df_slice) - 1 - entry_candle
    
    buffer = 2.0
    exit_reason = None

    if not state.get("is_open", False):
        logging.info(
            f"[EXIT REJECTED] symbol={symbol} option_type={side} position_side={position_side} "
            f"position_id={position_id} reason=POSITION_ALREADY_CLOSED"
        )
        return False, None
    state["lifecycle_state"] = "HOLD"

    # --- Get option premium + volume snapshot from df (not spot candles) ---
    current_option_price, option_volume = _get_option_market_snapshot(symbol, spot_price)
    timestamp = df_slice.iloc[-1].get("time", dt.now(time_zone)) if not df_slice.empty else dt.now(time_zone)

    # --- Hybrid exit logic (all precedence handled in check_exit_condition) ---
    triggered, reason = check_exit_condition(
        df_slice,
        state,
        option_price=current_option_price,
        option_volume=option_volume,
        timestamp=timestamp,
    )
    if triggered and reason:
        exit_reason = reason

    if not exit_reason:
        # Show periodic exit check status (once per 5 bars to avoid spam)
        check_count = state.get("exit_check_count", 0)
        if check_count % 5 == 0:
            logging.info(
                f"{CYAN}[EXIT CHECK] {side} {symbol} bars_held={bars_held} "
                f"ltp={current_option_price:.2f} SL={state.get('stop','N/A')} "
                f"PT={state.get('pt','N/A')} TG={state.get('tg','N/A')}{RESET}"
            )
        state["exit_check_count"] = check_count + 1
        return False, None

    # P2-C: Partial TG exit — route only the partial quantity; keep position open
    if exit_reason == "TG_PARTIAL_EXIT":
        partial_qty = state.get("partial_tg_qty", max(1, qty // 2))
        remaining   = max(0, qty - partial_qty)

        if account_type.lower() == "paper":
            success, order_id = send_paper_exit_order(symbol, partial_qty, exit_reason)
        elif mode == "LIVE":
            success, order_id = send_live_exit_order(symbol, partial_qty, exit_reason)
        else:
            success, order_id = True, "REPLAY_PARTIAL"

        if success:
            exit_price  = current_option_price if current_option_price else current_candle["close"]
            # P3-C: apply paper slippage to partial exit (conservative — exit gets worse fill)
            if account_type.lower() == "paper":
                raw_exit = exit_price
                exit_price = max(0.05, exit_price - PAPER_SLIPPAGE_POINTS)
                logging.debug(
                    f"[SLIPPAGE_MODELED] PARTIAL_EXIT raw={raw_exit:.2f} "
                    f"slippage=-{PAPER_SLIPPAGE_POINTS:.1f} "
                    f"effective={exit_price:.2f} qty={partial_qty}"
                )
            pnl_points  = exit_price - entry
            pnl_value   = pnl_points * partial_qty

            trade = info["call_buy"] if side == "CALL" else info["put_buy"]
            trade["pnl"]     += pnl_value
            trade["quantity"] = remaining
            info["total_pnl"] = info["call_buy"].get("pnl", 0) + info["put_buy"].get("pnl", 0)

            trade["filled_df"].loc[dt.now(time_zone)] = {
                "ticker":      symbol,
                "price":       exit_price,
                "action":      "PARTIAL_EXIT",
                "stop_price":  entry,
                "take_profit": pnl_value,
                "spot_price":  spot_price,
                "quantity":    partial_qty,
            }
            logging.info(
                f"{GREEN}[PARTIAL_EXIT][{account_type.upper()} TG_PARTIAL_EXIT] "
                f"{side} {symbol} partial_qty={partial_qty} remaining_qty={remaining} "
                f"Entry={entry:.2f} Exit={exit_price:.2f} PnL={pnl_value:.2f} "
                f"SL_now={state.get('stop', 'N/A')} PositionId={position_id}{RESET}"
            )
            # Position stays OPEN with reduced quantity — SL was already ratcheted
            # to TG level inside check_exit_condition before returning TG_PARTIAL_EXIT
            if mode == "LIVE":
                update_order_status(order_id, "PENDING", partial_qty, exit_price, symbol)
        return success, exit_reason

    # --- Route full exit order ---
    if account_type.lower() == "paper":
        success, order_id = send_paper_exit_order(symbol, qty, exit_reason)
    else:
        if mode == "LIVE":
            success, order_id = send_live_exit_order(symbol, qty, exit_reason)
        else:
            # REPLAY mode -> simulate success, no DB
            success, order_id = True, "REPLAY_ORDER"

    if success:
        # Use the option's actual traded price (from df), not spot candle close
        exit_price = current_option_price if current_option_price else current_candle["close"]
        # P3-C: apply paper slippage to paper exit fills
        if account_type.lower() == "paper":
            raw_exit = exit_price
            exit_price = max(0.05, exit_price - PAPER_SLIPPAGE_POINTS)
            logging.debug(
                f"[SLIPPAGE_MODELED] FULL_EXIT raw={raw_exit:.2f} "
                f"slippage=-{PAPER_SLIPPAGE_POINTS:.1f} "
                f"effective={exit_price:.2f} qty={qty}"
            )
        pnl_points = exit_price - entry
        pnl_value  = pnl_points * qty

        trade = info["call_buy"] if side == "CALL" else info["put_buy"]
        trade["pnl"] += pnl_value
        info["total_pnl"] = info["call_buy"].get("pnl", 0) + info["put_buy"].get("pnl", 0)
        trade["trade_flag"] = 0
        trade["quantity"] = 0

        trade["filled_df"].loc[dt.now(time_zone)] = {
            'ticker': symbol,
            'price': exit_price,
            'action': 'EXIT',
            'stop_price': entry,
            'take_profit': pnl_value,
            'spot_price': spot_price,
            'quantity': qty
        }

        bars_held = len(df_slice) - 1 - state.get("entry_candle", len(df_slice) - 1)
        exit_type = state.get("last_exit_type", "N/A")
        trigger_cond = state.get("last_triggering_condition", "N/A")
        logging.info(
            f"{YELLOW}[EXIT][{account_type.upper()} {exit_reason}] {side} {symbol} "
            f"Entry={entry:.2f} Exit={exit_price:.2f} Qty={qty} PnL={pnl_value:.2f} (points={pnl_points:.2f}) "
            f"BarsHeld={bars_held} ExitType={exit_type} Trigger={trigger_cond} PositionId={position_id} "
            f"PositionSide={position_side} "
            f"Levels: SL={state.get('stop', 'N/A')} "
            f"PT={state.get('pt','N/A')} TG={state.get('tg','N/A')}{RESET}"
        )
        _is_tg_exit = str(exit_reason).upper() in {"TARGET_HIT", "TG_PARTIAL_EXIT"}
        _is_rev_exit = str(exit_reason).upper() in {"REVERSAL_EXIT", "MOMENTUM_EXHAUSTION"}
        _is_atr_exit = str(exit_reason).upper() in {"SL_HIT", "TIME_EXIT", "ST_FLIP", "OSC_EXHAUSTION", "MOMENTUM_EXIT"}
        logging.info(f"[EXIT_ATTRIBUTION] type={'TG' if _is_tg_exit else ('REVERSAL' if _is_rev_exit else ('ATR' if _is_atr_exit else 'OTHER'))} reason={exit_reason}")
        _tg = state.get("tg")
        _pt = state.get("pt")
        _tg_hit_now = bool(_tg is not None and exit_price >= float(_tg))
        _pt_hit_now = bool(_pt is not None and exit_price >= float(_pt))
        if (not _tg_hit_now) and (not _pt_hit_now) and str(exit_reason).upper() not in {"TARGET_HIT", "PT_HIT", "TG_PARTIAL_EXIT"}:
            logging.info(
                "[PT_TG_UNREACHABLE_EXIT] "
                f"symbol={symbol} side={side} reason={exit_reason} exit_price={exit_price:.2f} "
                f"pt={_pt if _pt is not None else 'N/A'} tg={_tg if _tg is not None else 'N/A'}"
            )
        # Strict lifecycle transition OPEN -> HOLD -> EXIT.
        state["is_open"] = False
        state["lifecycle_state"] = "EXIT"

        # P1-C: false-breakout cooldown — activate when a compression breakout trade closes at loss
        if state.get("source") == "COMPRESSION_BREAKOUT" and pnl_value < 0:
            _compression_state.notify_trade_result(is_loss=True)
            logging.info(
                f"[FALSE_BREAKOUT_COOLDOWN] Compression breakout trade closed at loss "
                f"pnl={pnl_value:.2f} symbol={symbol} PositionId={position_id}"
            )

        if state.get("scalp_mode", False):
            cooldown_until = dt.now(time_zone) + timedelta(minutes=SCALP_COOLDOWN_MINUTES)
            info["scalp_cooldown_until"] = cooldown_until
            logging.info(
                f"[SCALP COOLDOWN] symbol={symbol} until={cooldown_until} "
                f"position_id={position_id}"
            )

        if mode == "LIVE":
            update_order_status(order_id, "PENDING", qty, exit_price, symbol)
        else:
            logging.info("[REPLAY] Skipping DB update")

        return True, exit_reason

    return False, None


def cleanup_trade_exit(info, leg, side, name, qty, exit_price, mode, reason):
    """
    Unified cleanup for any exit (STOPLOSS, TARGET, PARTIAL, EOD, FORCE).
    Ensures trade_flag reset to 0 so new entries are allowed.
    
    FIX: exit_price should always be the option's traded price, not spot_price.
    If exit_price is None or invalid, try to fetch from df as last resort.
    """
    ct = dt.now(time_zone)
    
    # Ensure exit_price is the option's traded price, not spot
    if exit_price is None or (isinstance(exit_price, float) and pd.isna(exit_price)):
        # Fallback: try to get from df dataframe
        if name in df.index:
            try:
                exit_price = float(df.loc[name, "ltp"])
            except Exception:
                exit_price = spot_price if spot_price else 0
        else:
            exit_price = spot_price if spot_price else 0
    
    info[leg]["trade_flag"] = 0        # ✅ always reset
    info[leg]["quantity"] = 0
    info[leg]["is_open"] = False
    info[leg]["lifecycle_state"] = "EXIT"
    info[leg]["filled_df"].loc[ct] = {
        'ticker': name,
        'price': exit_price,
        'action': 'EXIT',
        'stop_price': None,
        'take_profit': None,
        'spot_price': spot_price,
        'quantity': qty
    }
    logging.info(
        f"{RED}[EXIT][{mode}] {side} {name} Qty={qty} Price={exit_price:.2f} Reason={reason}{RESET}"
    )
    logging.info(
        "[EXIT AUDIT] "
        f"timestamp={ct} symbol={name} option_type={side} "
        f"position_side={info[leg].get('position_side', 'LONG')} "
        f"exit_type=ATR reason={reason} "
        f"triggering_condition=cleanup_trade_exit candle=-1 bars_held=-1 "
        f"regime={info[leg].get('regime_context', 'N/A')} "
        f"position_id={info[leg].get('position_id', 'UNKNOWN')}"
    )

def force_close_old_trades(info, mode):
    """Force close any open positions. Retrieves option's actual price from df."""
    ct = dt.now(time_zone)
    for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
        if info[leg]["trade_flag"] == 1:  # still active
            name = info[leg]["option_name"]
            qty  = info[leg]["quantity"]

            if mode.upper() == "PAPER":
                success, order_id = send_paper_exit_order(name, qty, "FORCE_CLEANUP")
            else:
                success, order_id = send_live_exit_order(name, qty, "FORCE_CLEANUP")

            if success:
                # FIX: Ensure we get the option's traded price, with safe fallback
                exit_price = None
                if name in df.index:
                    try:
                        ltp = df.loc[name, "ltp"]
                        if ltp and not (isinstance(ltp, float) and pd.isna(ltp)):
                            exit_price = float(ltp)
                    except Exception as e:
                        logging.warning(f"[FORCE_CLOSE] Failed to get LTP for {name}: {e}")
                
                if exit_price is None:
                    exit_price = spot_price if spot_price else 0
                    logging.warning(f"[FORCE_CLOSE] {name} not in df, using fallback price={exit_price}")
                
                cleanup_trade_exit(info, leg, side, name, qty, exit_price, mode, "FORCE_CLEANUP")


def update_risk(trade_info, risk_info):
    """
    Update risk metrics after each exit.
    trade_info: paper_info or live_info dict
    risk_info: session-level dict
    """
    # Calculate cumulative PnL
    total_pnl = sum([
        trade_info["call_buy"].get("pnl", 0),
        trade_info["put_buy"].get("pnl", 0)
    ])
    risk_info["session_pnl"] = total_pnl

    # Update peak equity
    risk_info["peak_equity"] = max(risk_info["peak_equity"], total_pnl)

    # Check daily max loss
    if total_pnl <= MAX_DAILY_LOSS:
        risk_info["halt_trading"] = True
        logging.warning(f"[RISK HALT] Daily loss limit breached: {total_pnl:.2f}")

    # Check drawdown
    if (total_pnl - risk_info["peak_equity"]) <= MAX_DRAWDOWN:
        risk_info["halt_trading"] = True
        logging.warning(
            f"[RISK HALT] Max drawdown breached: {total_pnl:.2f} vs peak {risk_info['peak_equity']:.2f}"
        )

def paper_order(candles_3m, hist_yesterday_15m=None, exit=False, mode="REPLAY", spot_price=None):
    global quantity, paper_info, df, last_signal_candle_time, risk_info

    COOLDOWN_SECONDS = 120
    ct = dt.now(time_zone)

    # 1. Safety reset
    for leg in ["call_buy", "put_buy"]:
        if paper_info[leg].get("trade_flag", 0) == 2:
            logging.warning(f"[RESET] lingering trade_flag=2 for {leg}")
            paper_info[leg]["trade_flag"] = 0

    # 2. Spot price — prefer live candle close; fall back to passed spot_price arg
    if not candles_3m.empty:
        spot_price = candles_3m.iloc[-1]["close"]
        logging.info(f"[PAPER] Spot={spot_price}")
    elif spot_price is not None:
        logging.info(f"[PAPER] Spot={spot_price} (from caller — no candles yet)")

    # 3. End-of-day force exit
    if ct > end_time:
        logging.info("[PAPER] EOD — closing open positions")
        for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
            if paper_info[leg]["trade_flag"] == 1:
                name = paper_info[leg]["option_name"]
                qty  = paper_info[leg]["quantity"]
                
                # FIX: Retrieve option's actual traded price with safe fallback
                ep = None
                if name in df.index:
                    try:
                        ltp = df.loc[name, "ltp"]
                        if ltp and not (isinstance(ltp, float) and pd.isna(ltp)):
                            ep = float(ltp)
                    except Exception:
                        pass
                
                if ep is None:
                    ep = spot_price if spot_price else 0
                    logging.warning(f"[PAPER EOD] {name} not in df, using fallback price={ep}")
                
                send_paper_exit_order(name, qty, "EOD")
                cleanup_trade_exit(paper_info, leg, side, name, qty, ep, "PAPER", "EOD")
        store(paper_info, account_type)
        return

    # 4. EXIT MANAGEMENT — runs every call when position is open
    # FIX: was gated by  if exit:  which was never True. Now unconditional.
    # FIX: runs BEFORE de-dup so exit is checked every second, not once per candle.
    for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
        if paper_info[leg].get("trade_flag", 0) == 1:
            state = paper_info[leg]
            triggered, reason = process_order(
                state, candles_3m, paper_info, spot_price,
                account_type="paper", mode=mode
            )
            if triggered:
                paper_info["last_exit_time"] = ct
                logging.info(f"[EXIT DONE][PAPER] {side} reason={reason}")

    if candles_3m is None or candles_3m.empty:
        return

    # 5. De-duplication — only gates ENTRY signals (not exit, which ran above)
    last_candle_time = str(candles_3m.iloc[-1].get("time", len(candles_3m)))
    if last_signal_candle_time == last_candle_time:
        return   # already processed entry signal for this candle
    last_signal_candle_time = last_candle_time

    # Risk gate
    if risk_info.get("halt_trading", False):
        logging.info("[ENTRY BLOCKED][RISK] Halt active")
        return

    pre_atr, _ = resolve_atr(candles_3m)
    pivot_src_pre = candles_3m.iloc[-2] if len(candles_3m) >= 2 else candles_3m.iloc[-1]
    cpr_pre = calculate_cpr(pivot_src_pre["high"], pivot_src_pre["low"], pivot_src_pre["close"])
    trad_pre = calculate_traditional_pivots(pivot_src_pre["high"], pivot_src_pre["low"], pivot_src_pre["close"])
    cam_pre = calculate_camarilla_pivots(pivot_src_pre["high"], pivot_src_pre["low"], pivot_src_pre["close"])

    # ── Day Type Classifier (paper mode) — init once per calendar day ──────
    global _paper_dtc, _paper_dtc_date
    _today_paper = ct.strftime("%Y-%m-%d")
    if _paper_dtc is None or _paper_dtc_date != _today_paper:
        try:
            _paper_dtc = make_day_type_classifier(
                cam_pre, cpr_pre,
                float(pivot_src_pre["high"]),
                float(pivot_src_pre["low"]),
                float(pivot_src_pre["close"]),
            )
            _paper_dtc_date = _today_paper
            logging.info(
                f"[DAY TYPE] Paper classifier initialized "
                f"R3={cam_pre.get('r3',float('nan')):.0f} R4={cam_pre.get('r4',float('nan')):.0f} "
                f"S3={cam_pre.get('s3',float('nan')):.0f} S4={cam_pre.get('s4',float('nan')):.0f}"
            )
        except Exception as _dtc_err:
            logging.debug(f"[DAY TYPE] Paper DTC init error: {_dtc_err}")

    _paper_day_type = DayTypeResult()   # UNKNOWN default
    if _paper_dtc is not None:
        try:
            _paper_day_type = _paper_dtc.update(candles_3m)
            # Lock classification at midday when confidence is stable
            _bar_t_paper = ct.hour * 60 + ct.minute
            if _bar_t_paper >= 12 * 60 and _paper_day_type.confidence in ("MEDIUM", "HIGH"):
                _paper_dtc.lock_classification()
                _paper_day_type.log()
        except Exception as _dtc_upd_err:
            logging.debug(f"[DAY TYPE] Paper DTC update error: {_dtc_upd_err}")

    breakdown_ctx = _opening_s4_breakdown_context(candles_3m, cpr_pre, cam_pre, pre_atr, ct)

    # Cooldown
    if paper_info.get("last_exit_time"):
        elapsed = (ct - paper_info["last_exit_time"]).total_seconds()
        if elapsed < COOLDOWN_SECONDS:
            if breakdown_ctx.get("opening_s4_breakdown", False) or breakdown_ctx.get("opening_r4_breakout", False):
                tag = "OPENING_S4_BREAKDOWN" if breakdown_ctx.get("opening_s4_breakdown", False) else "OPENING_R4_BREAKOUT"
                logging.info(
                    f"[ENTRY COOLDOWN BYPASS][{tag}] elapsed={elapsed:.0f}s "
                    f"close={breakdown_ctx.get('close')} s4={breakdown_ctx.get('s4')} "
                    f"r4={breakdown_ctx.get('r4')} "
                    f"s4_threshold={breakdown_ctx.get('threshold_s4_down')} "
                    f"r4_threshold={breakdown_ctx.get('threshold_r4_up')} "
                    f"cpr_width={breakdown_ctx.get('cpr_width')}"
                )
            else:
                logging.info(f"[ENTRY BLOCKED][COOLDOWN] {elapsed:.0f}s < {COOLDOWN_SECONDS}s")
                return

    if _is_startup_suppression_active(paper_info, ct, "PAPER"):
        store(paper_info, account_type)
        return
        
    # ── Oscillator Extreme Hold Logic ────────────────────────────────────────
    osc_hold_until = paper_info.get("osc_hold_until")
    if osc_hold_until:
        if ct < osc_hold_until:
            # Check for reversal confirmation during hold
            # If price fades back below pivot (approx check via close vs prev close direction)
            # This is a simplified check; robust check requires pivot levels.
            # For now, we just log and block.
            logging.info(f"[OSC_EXTREME_HOLD] Active until {osc_hold_until}. Entry suppressed.")
            return
        else:
            # Hold expired
            logging.info(f"[OSC_EXTREME_OVERRIDE] Hold expired. Checking for continuation.")
            paper_info["osc_hold_until"] = None
            # Allow entry to proceed (will be gated by quality check)

    # ── Conflict Governance: Check Opposing Position ─────────────────────────
    # If CALL is open, block PUT entry, and vice versa.
    # This prevents hedging/locking in a directional strategy.
    call_open = paper_info["call_buy"].get("trade_flag", 0) == 1
    put_open = paper_info["put_buy"].get("trade_flag", 0) == 1
    
    if call_open:
        logging.debug("[CONFLICT_BLOCKED] CALL position open. Blocking PUT signals.")
        # We can return here if we want to strictly enforce single-direction.
        # But we need to allow exit processing for the open leg.
        # Since exit processing is done above, we are in entry section.
        # We will enforce this check inside the signal handling block.
        pass 

    atr = pre_atr

        # 5A. Scalp entry flow (buy-on-dip CALL / sell-on-rally PUT with own cool-down)
    scalp_cd_until = paper_info.get("scalp_cooldown_until")
    
    # Initialize Pulse module for scalp entry confirmation
    pulse = get_pulse_module()

    # ── Pulse Check ───────────────────────────────────────────────
    # First check: pulse rate must exceed threshold
    pulse_metrics = pulse.get_pulse()
    if isinstance(pulse_metrics, dict):
        tick_rate = pulse_metrics.get("tick_rate", 0)
        burst_flag = pulse_metrics.get("burst_flag", False)
        direction_drift = pulse_metrics.get("direction_drift", "NEUTRAL")
    else:
        tick_rate = getattr(pulse_metrics, "tick_rate", 0)
        burst_flag = getattr(pulse_metrics, "burst_flag", False)
        direction_drift = getattr(pulse_metrics, "direction_drift", "NEUTRAL")

    if tick_rate > 0:
        logging.info(f"[PULSE_TICKRATE_VALID] tick_rate={tick_rate:.2f}")
        logging.info(f"[PULSE_CHECK] tick_rate={tick_rate:.2f} burst={burst_flag} drift={direction_drift}")
    else:
        logging.info("[PULSE_TICKRATE_DEFAULT] tick_rate=0 (fallback)")

    pulse_passed = False
    if not burst_flag or tick_rate < PULSE_TICKRATE_THRESHOLD:
        logging.info(
            f"[SCALP SKIP][PULSE_FAILED_SKIP] tick_rate={tick_rate}, burst_flag={burst_flag}, drift={direction_drift}"
        )
    else:
        # Pulse rate OK - now detect scalp signal and check direction alignment
        scalp_sig = _detect_scalp_dip_rally_signal(candles_3m, trad_pre, atr)
        if scalp_sig:
            scalp_side = scalp_sig["side"]
            
            if direction_drift == "UP" and scalp_side != "CALL":
                logging.info(
                    f"[SCALP SKIP][PULSE_FAILED_SKIP][DIRECTION_MISMATCH] drift=UP but scalp_side={scalp_side}"
                )
            elif direction_drift == "DOWN" and scalp_side != "PUT":
                logging.info(
                    f"[SCALP SKIP][PULSE_FAILED_SKIP][DIRECTION_MISMATCH] drift=DOWN but scalp_side={scalp_side}"
                )
            else:
                # Pulse direction matches scalp side - proceed with entry
                pulse_passed = True
                logging.info("[SCALP ALLOWED][PULSE_PASSED_ALLOW] Pulse check passed, proceeding with scalp entry")
                log_entry_green(
                    f"[ENTRY ATTEMPT][SCALP][PAPER] side={scalp_sig.get('side')} "
                    f"reason={scalp_sig.get('reason')} zone={scalp_sig.get('zone')} "
                    f"atr={atr:.2f} position_type={scalp_sig.get('position_type', 'LONG')} "
                    "long=True"
                )

    if not pulse_passed:
        logging.info("[SCALP SKIP][PULSE_FAILED_SKIP] Pulse check failed, skipping scalp entry")
    
    elif scalp_cd_until and ct < scalp_cd_until:
        logging.info(
            f"[SCALP SIGNAL IGNORED][COOLDOWN] now={ct} cooldown_until={scalp_cd_until}"
        )
    else:
        scalp_sig = _detect_scalp_dip_rally_signal(candles_3m, trad_pre, atr)
        if scalp_sig:
            log_entry_green(
                f"[ENTRY ATTEMPT][SCALP][PAPER] side={scalp_sig.get('side')} "
                f"reason={scalp_sig.get('reason')} zone={scalp_sig.get('zone')} "
                f"atr={atr:.2f} position_type={scalp_sig.get('position_type', 'LONG')} "
                "long=True"
            )
            scalp_side = scalp_sig["side"]
            scalp_leg = "call_buy" if scalp_side == "CALL" else "put_buy"
            burst_key = f"{scalp_side}:{last_candle_time}"
            if paper_info.get("scalp_last_burst_key") == burst_key:
                logging.info(f"[SCALP SKIP] duplicate burst {burst_key}")
            elif paper_info[scalp_leg].get("trade_flag", 0) == 0 and paper_info[scalp_leg].get("is_open", False) is False:
                if _cap_available(paper_info, is_scalp=True):
                    opt_type = "CE" if scalp_side == "CALL" else "PE"
                    opt_name, _strike = get_option_by_moneyness(
                        spot_price,
                        opt_type,
                        moneyness=CALL_MONEYNESS if scalp_side == "CALL" else PUT_MONEYNESS
                    )
                    if not opt_name:
                        logging.info(f"[ENTRY_ALLOWED_BUT_NOT_EXECUTED] no option found for {scalp_side}")
                        return

                    ltp_val = df.loc[opt_name, "ltp"] if opt_name in df.index else None
                    base_entry = float(ltp_val) if (ltp_val is not None and not pd.isna(ltp_val)) else float(spot_price or 0)
                    if base_entry <= 0:
                        logging.info(f"[SCALP SKIP] invalid entry premium for {opt_name}")
                        return

                    # P3-C: apply slippage — scalp entries also get worse fills
                    entry_price = base_entry + PAPER_SLIPPAGE_POINTS
                    atr_sl_mult = float(np.clip(0.9, SCALP_ATR_SL_MIN_MULT, SCALP_ATR_SL_MAX_MULT))
                    atr_sl_points = max(SCALP_SL_POINTS, round(float(atr) * atr_sl_mult, 2))
                    stop = round(entry_price - atr_sl_points, 2)
                    pt = round(entry_price + SCALP_PT_POINTS, 2)
                    position_id = f"scalp_{opt_name}_{int(ct.timestamp())}_{paper_info.get('trade_count', 0) + 1}"
                    paper_info[scalp_leg].update({
                        "option_name":       opt_name,
                        "quantity":          quantity,
                        "buy_price":         entry_price,
                        "order_type":        ORDER_TYPE,
                        "trade_flag":        1,
                        "pnl":               0,
                        "reason":            scalp_sig["reason"],
                        "source":            scalp_sig.get("tag", "SCALP"),
                        "order_id":          f"paper_scalp_{opt_name}_{ct}",
                        "position_id":       position_id,
                        "entry_time":        ct,
                        "entry_candle":      len(candles_3m) - 1,
                        "side":              scalp_side,
                        "position_type":     scalp_sig.get("position_type", "LONG"),
                        "position_side":     _long_position_side(scalp_side),
                        "stop":              stop,
                        "pt":                pt,
                        "tg":                pt,
                        "trail_start":       0,
                        "trail_step":        0,
                        "trail_updates":     0,
                        "consec_count":      0,
                        "prev_gap":          0,
                        "peak_momentum":     0,
                        "peak_candle":       len(candles_3m) - 1,
                        "plateau_count":     0,
                        "atr_value":         atr,
                        "regime_context":    "SCALP",
                        "is_open":           True,
                        "lifecycle_state":   "OPEN",
                        "scalp_mode":        True,
                        "trade_class":       TRADE_CLASS_SCALP,
                        "scalp_pt_points":   SCALP_PT_POINTS,
                        "scalp_sl_points":   atr_sl_points,
                        "scalp_min_hold_bars": SCALP_MIN_HOLD_BARS,
                        "scalp_extreme_move_atr_mult": SCALP_EXTREME_MOVE_ATR_MULT,
                        "scalp_zone":        scalp_sig.get("zone"),
                        "scalp_zone_level":  scalp_sig.get("level"),
                        "scalp_zone_stop":   scalp_sig.get("sl_zone"),
                        "partial_booked":    False,
                        "partial_tg_booked": False,
                        "hf_exit_manager":   OptionExitManager(
                            entry_price=entry_price,
                            side=scalp_side,
                            risk_buffer=1.0,
                        ),
                        "hf_deferred_logged": 0,
                    })
                    paper_info[scalp_leg]["filled_df"].loc[ct] = {
                        "ticker": opt_name,
                        "price": entry_price,
                        "action": scalp_side,
                        "stop_price": stop,
                        "take_profit": pt,
                        "spot_price": spot_price,
                        "quantity": quantity,
                    }
                    _register_trade(paper_info, is_scalp=True)
                    paper_info["scalp_last_burst_key"] = burst_key
                    logging.info(
                        f"[SCALP_ENTRY] used={_cap_used(paper_info, True)} cap={_cap_limit(paper_info, True)} "
                        f"trend_used={_cap_used(paper_info, False)} trend_cap={_cap_limit(paper_info, False)}"
                    )
                    log_entry_green(
                        f"[SCALP ENTRY][PAPER] {scalp_side} {opt_name} @ {entry_price:.2f} "
                        f"PT=+{SCALP_PT_POINTS:.1f} SL=-{atr_sl_points:.2f} "
                        f"reason={scalp_sig['reason']} zone={scalp_sig.get('zone')} "
                        f"position_type={paper_info[scalp_leg].get('position_type', 'LONG')} "
                        "long=True "
                        f"position_id={position_id}"
                    )
                    logging.info(
                        f"[SCALP_TRADE] class={TRADE_CLASS_SCALP} side={scalp_side} "
                        f"symbol={opt_name} scalp_mode=True trend_mode=False "
                        f"position_type={paper_info[scalp_leg].get('position_type', 'LONG')} long=True "
                        f"tag={scalp_sig.get('tag')} "
                        f"position_id={position_id}"
                    )
                    logging.info(
                        "[ENTRY][NEW] "
                        f"timestamp={ct} symbol={opt_name} option_type={scalp_side} "
                        f"position_side={paper_info[scalp_leg].get('position_side', 'LONG')} "
                        f"position_id={position_id} "
                        "lifecycle=OPEN regime=SCALP"
                    )
                    _save_trades_paper()
                    store(paper_info, account_type)
                    return
                logging.info(
                    f"[ENTRY_ALLOWED_BUT_NOT_EXECUTED][MAX_TRADES_CAP][SCALP] used={_cap_used(paper_info, True)} cap={_cap_limit(paper_info, True)} "
                    f"trade_count={paper_info.get('trade_count', 0)} side={scalp_side} entry blocked"
                )

    # 6. Signal evaluation
    _fb_sig_paper = detect_failed_breakout(candles_3m, cam_pre)

    # Calculate gap/bias context (consistent with live_order)
    _paper_close = float(candles_3m.iloc[-1]["close"]) if len(candles_3m) else float("nan")
    _paper_gap = "NO_GAP"
    if np.isfinite(_paper_close) and np.isfinite(cam_pre.get("r3", float("nan"))) and _paper_close > float(cam_pre.get("r3")):
        _paper_gap = "GAP_UP"
    elif np.isfinite(_paper_close) and np.isfinite(cam_pre.get("s3", float("nan"))) and _paper_close < float(cam_pre.get("s3")):
        _paper_gap = "GAP_DOWN"
    _paper_bias = "Positive" if _paper_gap == "GAP_UP" else ("Negative" if _paper_gap == "GAP_DOWN" else "Neutral")
    
    _paper_open_bias = "UNKNOWN"
    if np.isfinite(_paper_close):
        if np.isfinite(cam_pre.get("r4", float("nan"))) and _paper_close > float(cam_pre.get("r4")):
            _paper_open_bias = "Above R4, continuation likely"
        elif np.isfinite(cam_pre.get("r3", float("nan"))) and _paper_close > float(cam_pre.get("r3")):
            _paper_open_bias = "Above R3, expected momentum continuation"
        elif np.isfinite(cam_pre.get("s4", float("nan"))) and _paper_close < float(cam_pre.get("s4")):
            _paper_open_bias = "Below S4, downside continuation likely"
        elif np.isfinite(cam_pre.get("s3", float("nan"))) and _paper_close < float(cam_pre.get("s3")):
            _paper_open_bias = "Below S3, downside pressure active"
        else:
            _paper_open_bias = "Inside S3-R3, balanced open"

    # Retrieve day_type tag safely with audit logging
    _dt_name_obj = getattr(_paper_day_type, "name", None)
    _paper_dtc_name = getattr(_dt_name_obj, "value", None) if _dt_name_obj else None
    _paper_day_type_tag = (
        _paper_dtc_name if (_paper_dtc_name and _paper_dtc_name != "UNKNOWN")
        else ("GAP_DAY" if _paper_gap in ("GAP_UP", "GAP_DOWN") else "NEUTRAL_DAY")
    )
    
    if _paper_day_type_tag != "NEUTRAL_DAY" and _paper_day_type_tag != "UNKNOWN":
        logging.info(f"[DAYTYPE_TAG_VALID] tag={_paper_day_type_tag}")
    else:
        logging.info(f"[DAYTYPE_TAG_DEFAULT] tag={_paper_day_type_tag} (fallback)")

    # Define _rev_sig_paper with audit logging
    _rev_sig_paper = None
    try:
        _rev_sig_paper = detect_reversal(
            candles_3m, cam_pre,
            current_time=ct,
            day_type_tag=_paper_day_type_tag,
        )
    except Exception as e:
        logging.warning(f"[REVERSAL_DETECTOR_ERROR] {e}")

    if _rev_sig_paper:
        logging.info(f"[REVERSAL_SIGNAL_VALID] score={_rev_sig_paper.get('score')} side={_rev_sig_paper.get('side')}")
    else:
        logging.info("[REVERSAL_SIGNAL_DEFAULT] No reversal signal detected")
        
    # Conflict Governance: Arbitration
    # If we have a trend signal AND a reversal signal on opposite sides
    # Prioritize the one with higher score.
    # This logic is implicitly handled by _trend_entry_quality_gate which takes reversal_signal
    # and allows overrides. We just need to ensure we don't fire conflicting orders.

    # ── LATE-ENTRY GATE (paper/live) — no entries within 25 min of EOD exit ──
    _bar_min_now = ct.hour * 60 + ct.minute
    if _bar_min_now >= 14 * 60 + 45:
        logging.info(
            f"[ENTRY BLOCKED][LATE_ENTRY] timestamp={ct} symbol={ticker} "
            f"time={ct.hour:02d}:{ct.minute:02d} reason=Too close to EOD (15:10), entry suppressed"
        )
        _save_trades_paper()
        store(paper_info, account_type)
        return

    log_entry_green(
        f"[ENTRY ATTEMPT][TREND][PAPER] symbol={ticker} "
        f"atr={atr:.2f} trade_count={paper_info.get('trade_count', 0)} "
        f"day_type={_paper_day_type_tag} confidence={getattr(_paper_day_type, 'confidence', 'N/A')}"
    )
    quality_ok, allowed_side, gate_reason, st_details = _trend_entry_quality_gate(
        candles_3m=candles_3m,
        candles_15m=hist_yesterday_15m if hist_yesterday_15m is not None else pd.DataFrame(),
        timestamp=ct,
        symbol=ticker,
        adx_min=float(TREND_ENTRY_ADX_MIN),
        cpr_levels=cpr_pre,
        camarilla_levels=cam_pre,
        reversal_signal=_rev_sig_paper,
        failed_breakout_signal=_fb_sig_paper,
        day_type_result=_paper_day_type,
        open_bias_context={"gap_tag": _paper_gap, "bias": _paper_bias, "open_bias": _paper_open_bias},
    )
    if not quality_ok:
        tag = (
            "ST_CONFLICT" if "Supertrend conflict" in gate_reason else
            "SLOPE_MISMATCH" if "Slope mismatch" in gate_reason else
            "WEAK_ADX" if "Weak trend strength" in gate_reason else
            "FAILED_BREAKOUT" if "Failed breakout" in gate_reason else
            "EMA_STRETCH" if "EMA stretch" in gate_reason else
            "OSC_EXTREME"
        )
        logging.info(
            f"[ENTRY BLOCKED][{tag}] "
            f"timestamp={st_details['timestamp']} symbol={st_details['symbol']} "
            f"ST3m_bias={st_details['ST3m_bias']} ST15m_bias={st_details['ST15m_bias']} "
            f"allowed_side={allowed_side} "
            f"close={st_details.get('close')} s4={st_details.get('s4')} r4={st_details.get('r4')} "
            f"s4_threshold={st_details.get('s4_threshold')} r4_threshold={st_details.get('r4_threshold')} "
            f"put_ok={allowed_side == 'PUT'} call_ok={allowed_side == 'CALL'} "
            f"ADX={st_details.get('adx14')} RSI={st_details.get('rsi14')} CCI={st_details.get('cci20')} "
            f"reason={gate_reason}"
        )
        _save_trades_paper()
        store(paper_info, account_type)
        return

    # TPMA (stored as "vwap" column by build_indicator_dataframe)
    tpma = float(candles_3m["vwap"].iloc[-1]) if "vwap" in candles_3m.columns and not pd.isna(candles_3m["vwap"].iloc[-1]) else None

    # Opening range (first 5 bars of session)
    orb_h, orb_l = get_opening_range(candles_3m)

    # FIX: pivots from previous completed candle (iloc[-2]), not current (iloc[-1])
    cpr = cpr_pre
    trad = trad_pre
    cam = cam_pre

    # ── Phase 4: Zone revisit detection (paper) ──────────────────────────────
    global _paper_zones, _paper_zones_date
    _today_str = dt.now(time_zone).strftime("%Y-%m-%d")
    if _paper_zones_date != _today_str:
        _paper_zones = []
        if hist_yesterday_15m is not None and len(hist_yesterday_15m) >= 10:
            _paper_zones = detect_zones(hist_yesterday_15m)
        if _paper_zones:
            logging.info(f"[ZONE_CONTEXT][PAPER] zones_loaded={len(_paper_zones)}")
        _paper_zones_date = _today_str

    _zone_revisit_signal = None
    if _paper_zones and atr > 0:
        if np.isfinite(atr):
            update_zone_activity(_paper_zones, float(candles_3m["close"].iloc[-1]), float(atr),
                                 str(candles_3m.index[-1]) if hasattr(candles_3m.index[-1], 'strftime') else str(len(candles_3m)))
            _zone_revisit_signal = detect_zone_revisit(candles_3m, _paper_zones, float(atr))

    # Pulse metrics dict for scoring (normalize PulseMetrics dataclass to dict)
    _pulse_dict = None
    if pulse_metrics is not None:
        if isinstance(pulse_metrics, dict):
            _pulse_dict = pulse_metrics
        else:
            _pulse_dict = {
                "tick_rate": getattr(pulse_metrics, "tick_rate", 0.0),
                "burst_flag": getattr(pulse_metrics, "burst_flag", False),
                "direction_drift": getattr(pulse_metrics, "direction_drift", "NEUTRAL"),
            }

    # ── Compression breakout entry ────────────────────────────────────────────
    if hist_yesterday_15m is not None and len(hist_yesterday_15m) >= 3:
        _compression_state.update(hist_yesterday_15m)

    if _compression_state.has_entry:
        comp_sig = _compression_state.entry_signal
        leg = "call_buy" if comp_sig["side"] == "CALL" else "put_buy"
        if paper_info[leg]["trade_flag"] == 0 and not risk_info.get("halt_trading", False):
            if _cap_available(paper_info, is_scalp=False):
                logging.info(
                    f"[ENTRY DISPATCH] COMPRESSION_BREAKOUT {comp_sig['side']} "
                    f"sl={comp_sig['sl']:.2f} tg={comp_sig['tg']:.2f} pt={comp_sig['pt']:.2f} | "
                    f"{comp_sig['reason']}"
                )
        _compression_state.consume_entry()
        _save_trades_paper()
        store(paper_info, account_type)
        return

    signal = detect_signal(
        candles_3m=candles_3m,
        candles_15m=hist_yesterday_15m if hist_yesterday_15m is not None else pd.DataFrame(),
        cpr_levels=cpr,
        camarilla_levels=cam,
        traditional_levels=trad,
        atr=atr,
        include_partial=False,
        current_time=ct,
        vwap=tpma,
        orb_high=orb_h,
        orb_low=orb_l,
        osc_relief_active=st_details.get("osc_relief_override", False),
        zone_signal=_zone_revisit_signal,
        pulse_metrics=_pulse_dict,
    )

    # 7. Entry
    if not signal:
        _save_trades_paper()
        store(paper_info, account_type)
        return

    side   = signal["side"]
    reason = signal["reason"]
    source = signal.get("source", "UNKNOWN")
    signal["osc_context"] = st_details.get("osc_context", "UNKNOWN")
    signal["day_type"] = st_details.get("day_type_tag", "UNKNOWN")
    signal["open_bias"] = st_details.get("open_bias", "UNKNOWN")
    signal["failed_breakout"] = bool(st_details.get("failed_breakout", False))
    signal["ema_stretch"] = bool(st_details.get("ema_stretch_tagged", False))
    signal["ema_stretch_mult"] = st_details.get("ema_stretch_mult")
    tpma_str = f"{tpma:.1f}" if tpma else "N/A"  # Format tpma conditionally
    logging.info(
        f"[SIGNAL][PAPER] {side} score={signal.get('score','?')} "
        f"source={source} tpma={tpma_str} | {reason}"
    )
    if side != allowed_side:
        logging.info(
            "[ENTRY BLOCKED][ST_SIDE_MISMATCH] "
            f"timestamp={ct} symbol={ticker} ST3m_bias={st_details['ST3m_bias']} "
            f"ST15m_bias={st_details['ST15m_bias']} allowed_side={allowed_side} "
            f"signal_side={side} reason=Supertrend conflict, entry suppressed."
        )
        _save_trades_paper()
        store(paper_info, account_type)
        return

    # Log 15m bias (FIX: no longer hard-blocks on NEUTRAL — scoring handles it)
    if hist_yesterday_15m is not None and not hist_yesterday_15m.empty:
        bias15 = hist_yesterday_15m.iloc[-1].get("supertrend_bias", "NEUTRAL")
        logging.info(f"[BIAS][15m] {bias15}")

    if risk_info.get("halt_trading", False):
        return

    leg = "call_buy" if side == "CALL" else "put_buy"
    try:
        if paper_info[leg]["trade_flag"] == 0:
            if not _cap_available(paper_info, is_scalp=False):
                logging.info(
                    f"[ENTRY_ALLOWED_BUT_NOT_EXECUTED][MAX_TRADES_CAP][TREND] used={_cap_used(paper_info, False)} cap={_cap_limit(paper_info, False)} "
                    f"scalp_used={_cap_used(paper_info, True)} scalp_cap={_cap_limit(paper_info, True)} "
                    f"trade_count={paper_info.get('trade_count', 0)} side={side} entry blocked"
                )
            else:
                opt_type = "CE" if side == "CALL" else "PE"
                opt_name, strike = get_option_by_moneyness(
                    spot_price, opt_type,
                    moneyness=CALL_MONEYNESS if side == "CALL" else PUT_MONEYNESS
                )

                if opt_name and opt_name in df.index:
                    ltp_val = df.loc[opt_name, "ltp"]
                    raw_price = float(ltp_val) if (ltp_val and not pd.isna(ltp_val)) else spot_price
                    # P3-C: apply slippage to paper entry fills (models bid-ask spread)
                    entry_price = raw_price + PAPER_SLIPPAGE_POINTS
                    logging.debug(
                        f"[SLIPPAGE_MODELED] ENTRY raw={raw_price:.2f} "
                        f"slippage=+{PAPER_SLIPPAGE_POINTS:.1f} "
                        f"effective={entry_price:.2f} side={side}"
                    )
                    if not entry_price or entry_price <= 0:
                        logging.warning(f"[ENTRY SKIP] invalid entry_price={entry_price}")
                        return

                    # pass candles_df so build_dynamic_levels can resolve entry candle
                    _last_bar = candles_3m.iloc[-1] if candles_3m is not None and not candles_3m.empty else None
                    _adx_entry = float(_last_bar.get("adx14", 0)) if _last_bar is not None and pd.notna(_last_bar.get("adx14")) else 0.0
                    levels = build_dynamic_levels(
                        entry_price, atr, side,
                        entry_candle=len(candles_3m) - 1,
                        candles_df=candles_3m,
                        adx_value=_adx_entry,
                    )
                    if not levels.get("valid", False):
                        logging.warning(f"[ENTRY SKIP] {side} levels failed (ATR extreme?)")
                        return
                    stop = levels["stop"]
                    pt = levels["pt"]
                    tg = levels["tg"]
                    trail_start = levels["trail_start"]
                    trail_step = levels["trail_step"]

                    # Apply DTC PM modifiers: override trail_step / record pm_max_hold
                    _pm_max_hold_paper = None
                    if _paper_day_type.pm_trail_step is not None:
                        trail_step = _paper_day_type.pm_trail_step
                        logging.debug(
                            f"[DAY_TYPE][PM] PAPER trail_step→{trail_step:.3f} "
                            f"day_type={_paper_day_type_tag} confidence={_paper_day_type.confidence}"
                        )
                    if _paper_day_type.pm_max_hold is not None:
                        _pm_max_hold_paper = _paper_day_type.pm_max_hold
                        logging.debug(
                            f"[DAY_TYPE][PM] PAPER pm_max_hold→{_pm_max_hold_paper} "
                            f"day_type={_paper_day_type_tag} confidence={_paper_day_type.confidence}"
                        )

                    # ── Build RegimeContext (Phase 3) ──────────────────────────
                    _rc = compute_regime_context(
                        st_details=st_details,
                        atr=atr,
                        reversal_signal=_rev_sig_paper if '_rev_sig_paper' in dir() else None,
                        failed_breakout_signal=_fb_sig_paper if '_fb_sig_paper' in dir() else None,
                        zone_signal=_zone_revisit_signal if '_zone_revisit_signal' in dir() else None,
                        pulse_tick_rate=tick_rate if 'tick_rate' in dir() else 0.0,
                        pulse_burst_flag=burst_flag if 'burst_flag' in dir() else False,
                        pulse_direction=direction_drift if 'direction_drift' in dir() else "NEUTRAL",
                        compression_state_str=_compression_state.market_state if hasattr(_compression_state, 'market_state') else "NEUTRAL",
                        bar_timestamp=str(ct),
                        symbol=ticker,
                    )
                    log_regime_context(_rc)
                    regime_context = _rc.atr_regime

                    position_id = f"{opt_name}_{int(ct.timestamp())}_{paper_info.get('trade_count', 0) + 1}"
                    paper_info[leg].update({
                        "option_name":       opt_name,
                        "quantity":          quantity,
                        "buy_price":         entry_price,
                        "order_type":        ORDER_TYPE,
                        "trade_flag":        1,
                        "pnl":               0,
                        "reason":            reason,
                        "source":            source,
                        "order_id":          f"paper_{opt_name}_{ct}",
                        "position_id":       position_id,
                        "entry_time":        ct,
                        "entry_candle":      len(candles_3m) - 1,
                        "side":              side,
                        "position_side":     _long_position_side(side),
                        "stop":              stop,
                        "pt":                pt,
                        "tg":                tg,
                        "trail_start":       trail_start,
                        "trail_step":        trail_step,
                        "trail_updates":     0,
                        "consec_count":      0,
                        "prev_gap":          0,
                        "peak_momentum":     0,
                        "peak_candle":       len(candles_3m) - 1,
                        "plateau_count":     0,
                        "is_open":           True,
                        "lifecycle_state":   "OPEN",
                        "scalp_mode":        False,   # P2-D: TREND class never uses scalp exits
                        "trade_class":       TRADE_CLASS_TREND,
                        "scalp_pt_points":   SCALP_PT_POINTS,
                        "scalp_sl_points":   SCALP_SL_POINTS,
                        "trend_min_hold_bars": TREND_MIN_HOLD_BARS,
                        "trend_extreme_move_atr_mult": TREND_EXTREME_MOVE_ATR_MULT,
                        "partial_booked":    False,
                        "partial_tg_booked": False,   # P2-C: partial TG exit tracking
                        "pm_max_hold":       _pm_max_hold_paper,  # DTC override (None = use default)
                        "day_type_modifier": getattr(_paper_day_type, "signal_modifier", 0),
                        "hf_exit_manager":   OptionExitManager(
                            entry_price=entry_price,
                            side=side,
                            risk_buffer=1.0,
                        ),
                        "hf_deferred_logged": 0,
                    })
                    # Merge regime context keys (replaces manual osc_context/day_type/etc.)
                    paper_info[leg].update(_rc.to_state_keys())

                    paper_info[leg]["filled_df"].loc[ct] = {
                        'ticker': opt_name,
                        'price': entry_price,
                        'action': side,
                        'stop_price': stop,
                        'take_profit': pt,
                        'spot_price': spot_price,
                        'quantity': quantity
                    }
                    _register_trade(paper_info, is_scalp=False)
                    logging.info(
                        f"[TREND_ENTRY] used={_cap_used(paper_info, False)} cap={_cap_limit(paper_info, False)} "
                        f"scalp_used={_cap_used(paper_info, True)} scalp_cap={_cap_limit(paper_info, True)}"
                    )

                    logging.info(
                        f"{GREEN}[ENTRY][PAPER] {side} {opt_name} @ {entry_price:.2f} "
                        f"SL={stop:.2f} PT={pt:.2f} TG={tg:.2f} "
                        f"ATR={atr:.1f} step={trail_step:.2f} "
                        f"score={signal.get('score','?')} source={source}{RESET}"
                    )
                    logging.info(
                        "[ENTRY][NEW] "
                        f"timestamp={ct} symbol={opt_name} option_type={side} "
                        f"position_side={paper_info[leg].get('position_side', 'LONG')} "
                        f"position_id={position_id} "
                        f"lifecycle=OPEN regime={regime_context}"
                    )
                else:
                    logging.warning(f"[ENTRY_ALLOWED_BUT_NOT_EXECUTED] no option for {side} strike={strike}")
    except Exception as e:
        logging.error(f"[ENTRY ERROR][PAPER] {e}", exc_info=True)

    _save_trades_paper()
    store(paper_info, account_type)


def _save_trades_paper():
    frames = [paper_info["call_buy"]["filled_df"], paper_info["put_buy"]["filled_df"]]
    frames = [f for f in frames if not f.empty]
    if frames:
        pd.concat(frames).to_csv(
            f"trades_{strategy_name}_{dt.now(time_zone).date()}_PAPER.csv", index=True
        )

# =============================== Live Trading =======================================

# ===== real_order =====
def live_order(candles_3m, hist_yesterday_15m=None, exit=False):
    global quantity, live_info, df, spot_price, last_signal_candle_time, risk_info

    COOLDOWN_SECONDS = 120
    ct = dt.now(time_zone)

    # 1. Safety reset
    for leg in ["call_buy", "put_buy"]:
        if live_info[leg].get("trade_flag", 0) == 2:
            logging.warning(f"[RESET] lingering trade_flag=2 for {leg}")
            live_info[leg]["trade_flag"] = 0

    # 2. Refresh spot price
    try:
        quote      = fyers.quotes(data={"symbols": ticker})
        spot_price = quote["d"][0]["v"]["lp"]
        logging.info(f"[LIVE] Spot={spot_price}")
    except Exception as e:
        logging.warning(f"[LIVE] Spot fetch failed: {e}")

    # 3. End-of-day force exit
    if ct > end_time:
        logging.info("[LIVE] EOD — closing positions")
        for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
            if live_info[leg]["trade_flag"] == 1:
                name = live_info[leg]["option_name"]
                qty  = live_info[leg]["quantity"]
                success, order_id = send_live_exit_order(name, qty, "EOD")
                if success:
                    # FIX: Retrieve option's actual traded price with safe fallback
                    ep = None
                    if name in df.index:
                        try:
                            ltp = df.loc[name, "ltp"]
                            if ltp and not (isinstance(ltp, float) and pd.isna(ltp)):
                                ep = float(ltp)
                        except Exception:
                            pass
                    
                    if ep is None:
                        ep = spot_price if spot_price else 0
                        logging.warning(f"[LIVE EOD] {name} not in df, using fallback price={ep}")
                    
                    cleanup_trade_exit(live_info, leg, side, name, qty, ep, "LIVE", "EOD")
                    update_order_status(order_id, "PENDING", qty, ep, name)
        return

    # 4. EXIT MANAGEMENT — unconditional, before de-dup
    # FIX: was gated by  if exit:  which was never True.
    for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
        if live_info[leg].get("trade_flag", 0) == 1:
            state = live_info[leg]
            triggered, reason = process_order(
                state, candles_3m, live_info, spot_price,
                account_type="live", mode="LIVE"
            )
            if triggered:
                live_info["last_exit_time"] = ct
                logging.info(f"[EXIT DONE][LIVE] {side} reason={reason}")

    if candles_3m is None or candles_3m.empty:
        return

    # 5. De-duplication — only gates entry signals
    last_candle_time = str(candles_3m.iloc[-1].get("time", len(candles_3m)))
    if last_signal_candle_time == last_candle_time:
        return
    last_signal_candle_time = last_candle_time

    if risk_info.get("halt_trading", False):
        return

    pre_atr, _ = resolve_atr(candles_3m)
    pivot_src_pre = candles_3m.iloc[-2] if len(candles_3m) >= 2 else candles_3m.iloc[-1]
    cpr_pre = calculate_cpr(pivot_src_pre["high"], pivot_src_pre["low"], pivot_src_pre["close"])
    trad_pre = calculate_traditional_pivots(pivot_src_pre["high"], pivot_src_pre["low"], pivot_src_pre["close"])
    cam_pre = calculate_camarilla_pivots(pivot_src_pre["high"], pivot_src_pre["low"], pivot_src_pre["close"])

    # ── Day Type Classifier (live mode) — init once per calendar day ───────
    global _live_dtc, _live_dtc_date
    _today_live = ct.strftime("%Y-%m-%d")
    if _live_dtc is None or _live_dtc_date != _today_live:
        try:
            _live_dtc = make_day_type_classifier(
                cam_pre, cpr_pre,
                float(pivot_src_pre["high"]),
                float(pivot_src_pre["low"]),
                float(pivot_src_pre["close"]),
            )
            _live_dtc_date = _today_live
            logging.info(
                f"[DAY TYPE] Live classifier initialized "
                f"R3={cam_pre.get('r3',float('nan')):.0f} R4={cam_pre.get('r4',float('nan')):.0f} "
                f"S3={cam_pre.get('s3',float('nan')):.0f} S4={cam_pre.get('s4',float('nan')):.0f}"
            )
        except Exception as _dtc_err:
            logging.debug(f"[DAY TYPE] Live DTC init error: {_dtc_err}")

    _live_day_type = DayTypeResult()   # UNKNOWN default
    if _live_dtc is not None:
        try:
            _live_day_type = _live_dtc.update(candles_3m)
            # Lock classification at midday when confidence is stable
            _bar_t_live = ct.hour * 60 + ct.minute
            if _bar_t_live >= 12 * 60 and _live_day_type.confidence in ("MEDIUM", "HIGH"):
                _live_dtc.lock_classification()
                _live_day_type.log()
        except Exception as _dtc_upd_err:
            logging.debug(f"[DAY TYPE] Live DTC update error: {_dtc_upd_err}")

    breakdown_ctx = _opening_s4_breakdown_context(candles_3m, cpr_pre, cam_pre, pre_atr, ct)

    if live_info.get("last_exit_time"):
        elapsed = (ct - live_info["last_exit_time"]).total_seconds()
        if elapsed < COOLDOWN_SECONDS:
            if breakdown_ctx.get("opening_s4_breakdown", False) or breakdown_ctx.get("opening_r4_breakout", False):
                tag = "OPENING_S4_BREAKDOWN" if breakdown_ctx.get("opening_s4_breakdown", False) else "OPENING_R4_BREAKOUT"
                logging.info(
                    f"[ENTRY COOLDOWN BYPASS][{tag}] elapsed={elapsed:.0f}s "
                    f"close={breakdown_ctx.get('close')} s4={breakdown_ctx.get('s4')} "
                    f"r4={breakdown_ctx.get('r4')} "
                    f"s4_threshold={breakdown_ctx.get('threshold_s4_down')} "
                    f"r4_threshold={breakdown_ctx.get('threshold_r4_up')} "
                    f"cpr_width={breakdown_ctx.get('cpr_width')}"
                )
            else:
                logging.info(f"[ENTRY BLOCKED][COOLDOWN] {elapsed:.0f}s")
                return

    if _is_startup_suppression_active(live_info, ct, "LIVE"):
        store(live_info, account_type)
        return
        
    # ── Oscillator Extreme Hold Logic (Live) ────────────────────────────────
    osc_hold_until = live_info.get("osc_hold_until")
    if osc_hold_until:
        if ct < osc_hold_until:
            logging.info(f"[OSC_EXTREME_HOLD] Active until {osc_hold_until}. Entry suppressed.")
            return
        else:
            logging.info(f"[OSC_EXTREME_OVERRIDE] Hold expired.")
            live_info["osc_hold_until"] = None
            
    # ── Conflict Governance (Live) ──────────────────────────────────────────
    call_open = live_info["call_buy"].get("trade_flag", 0) == 1
    put_open = live_info["put_buy"].get("trade_flag", 0) == 1

    atr = pre_atr

    # 5A. Scalp entry flow (buy-on-dip CALL / sell-on-rally PUT with own cool-down)
    scalp_cd_until = live_info.get("scalp_cooldown_until")
    
    # Initialize Pulse module for scalp entry confirmation
    pulse = get_pulse_module()

    # ── Pulse Check ───────────────────────────────────────────────
    pulse_metrics = pulse.get_pulse()
    if isinstance(pulse_metrics, dict):
        tick_rate = pulse_metrics.get("tick_rate", 0)
        burst_flag = pulse_metrics.get("burst_flag", False)
        direction_drift = pulse_metrics.get("direction_drift", "NEUTRAL")
    else:
        tick_rate = getattr(pulse_metrics, "tick_rate", 0)
        burst_flag = getattr(pulse_metrics, "burst_flag", False)
        direction_drift = getattr(pulse_metrics, "direction_drift", "NEUTRAL")

    if tick_rate > 0:
        logging.info(f"[PULSE_TICKRATE_VALID] tick_rate={tick_rate:.2f}")
        logging.info(f"[PULSE_CHECK] tick_rate={tick_rate:.2f} burst={burst_flag} drift={direction_drift}")
    else:
        logging.info("[PULSE_TICKRATE_DEFAULT] tick_rate=0 (fallback)")

    pulse_passed = False
    if not burst_flag or tick_rate < PULSE_TICKRATE_THRESHOLD:
        logging.info(
            f"[SCALP SKIP][PULSE_FAIL] tick_rate={tick_rate}, drift={direction_drift}"
        )
    else:
        # Pulse rate OK - now detect scalp signal and check direction alignment
        scalp_sig = _detect_scalp_dip_rally_signal(candles_3m, trad_pre, atr)
        if scalp_sig:
            scalp_side = scalp_sig["side"]
            
            if direction_drift == "UP" and scalp_side != "CALL":
                logging.info(
                    f"[SCALP SKIP][PULSE_FAILED_SKIP][DIRECTION_MISMATCH] drift=UP but scalp_side={scalp_side}"
                )
            elif direction_drift == "DOWN" and scalp_side != "PUT":
                logging.info(
                    f"[SCALP SKIP][PULSE_FAILED_SKIP][DIRECTION_MISMATCH] drift=DOWN but scalp_side={scalp_side}"
                )
            else:
                # Pulse direction matches scalp side - proceed with entry
                pulse_passed = True
                logging.info("[SCALP ALLOWED][PULSE_PASSED_ALLOW] Pulse check passed, proceeding with scalp entry")
                log_entry_green(
                    f"[ENTRY ATTEMPT][SCALP][LIVE] side={scalp_sig.get('side')} "
                    f"reason={scalp_sig.get('reason')} zone={scalp_sig.get('zone')} "
                    f"atr={atr:.2f} position_type={scalp_sig.get('position_type', 'LONG')} "
                    "long=True"
                )
    
    if not pulse_passed:
        logging.info("[SCALP SKIP][PULSE_FAILED_SKIP] Pulse check failed, skipping scalp entry")
    
    elif scalp_cd_until and ct < scalp_cd_until:
        logging.info(
            f"[SCALP SIGNAL IGNORED][COOLDOWN] now={ct} cooldown_until={scalp_cd_until}"
        )
    else:
        scalp_sig = _detect_scalp_dip_rally_signal(candles_3m, trad_pre, atr)
        if scalp_sig:
            log_entry_green(
                f"[ENTRY ATTEMPT][SCALP][LIVE] side={scalp_sig.get('side')} "
                f"reason={scalp_sig.get('reason')} zone={scalp_sig.get('zone')} "
                f"atr={atr:.2f} position_type={scalp_sig.get('position_type', 'LONG')} "
                "long=True"
            )
            scalp_side = scalp_sig["side"]
            scalp_leg = "call_buy" if scalp_side == "CALL" else "put_buy"
            burst_key = f"{scalp_side}:{last_candle_time}"
            if live_info.get("scalp_last_burst_key") == burst_key:
                logging.info(f"[SCALP SKIP] duplicate burst {burst_key}")
            elif live_info[scalp_leg].get("trade_flag", 0) == 0 and live_info[scalp_leg].get("is_open", False) is False:
                if _cap_available(live_info, is_scalp=True):
                    opt_type = "CE" if scalp_side == "CALL" else "PE"
                    opt_name, _strike = get_option_by_moneyness(
                        spot_price,
                        opt_type,
                        moneyness=CALL_MONEYNESS if scalp_side == "CALL" else PUT_MONEYNESS
                    )
                    if not opt_name:
                        logging.info(f"[ENTRY_ALLOWED_BUT_NOT_EXECUTED] no option found for {scalp_side}")
                        return

                    ltp_val = df.loc[opt_name, "ltp"] if opt_name in df.index else None
                    entry_price = float(ltp_val) if (ltp_val is not None and not pd.isna(ltp_val)) else float(spot_price or 0)
                    if entry_price <= 0:
                        logging.info(f"[SCALP SKIP] invalid entry premium for {opt_name}")
                        return

                    atr_sl_mult = float(np.clip(0.9, SCALP_ATR_SL_MIN_MULT, SCALP_ATR_SL_MAX_MULT))
                    atr_sl_points = max(SCALP_SL_POINTS, round(float(atr) * atr_sl_mult, 2))
                    stop = round(entry_price - atr_sl_points, 2)
                    pt = round(entry_price + SCALP_PT_POINTS, 2)
                    success, order_id = send_live_entry_order(opt_name, quantity, 1)
                    if success:
                        position_id = f"scalp_{opt_name}_{int(ct.timestamp())}_{live_info.get('trade_count', 0) + 1}"
                        live_info[scalp_leg].update({
                            "option_name":     opt_name,
                            "quantity":        quantity,
                            "buy_price":       entry_price,
                            "order_type":      ORDER_TYPE,
                            "trade_flag":      1,
                            "pnl":             0,
                            "reason":          scalp_sig["reason"],
                            "source":          scalp_sig.get("tag", "SCALP"),
                            "order_id":        order_id,
                            "position_id":     position_id,
                            "entry_time":      ct,
                            "entry_candle":    len(candles_3m) - 1,
                            "side":            scalp_side,
                            "position_type":   scalp_sig.get("position_type", "LONG"),
                            "position_side":   _long_position_side(scalp_side),
                            "stop":            stop,
                            "pt":              pt,
                            "tg":              pt,
                            "trail_start":     0,
                            "trail_step":      0,
                            "trail_updates":   0,
                            "consec_count":    0,
                            "prev_gap":        0,
                            "peak_momentum":   0,
                            "peak_candle":     len(candles_3m) - 1,
                            "plateau_count":   0,
                            "atr_value":       atr,
                            "regime_context":  "SCALP",
                            "is_open":         True,
                            "lifecycle_state": "OPEN",
                            "scalp_mode":      True,
                            "scalp_pt_points": SCALP_PT_POINTS,
                            "scalp_sl_points": atr_sl_points,
                            "scalp_min_hold_bars": SCALP_MIN_HOLD_BARS,
                            "scalp_extreme_move_atr_mult": SCALP_EXTREME_MOVE_ATR_MULT,
                            "scalp_zone":      scalp_sig.get("zone"),
                            "scalp_zone_level": scalp_sig.get("level"),
                            "scalp_zone_stop": scalp_sig.get("sl_zone"),
                            "partial_booked":  False,
                            "hf_exit_manager": OptionExitManager(
                                entry_price=entry_price,
                                side=scalp_side,
                                risk_buffer=1.0,
                            ),
                            "hf_deferred_logged": 0,
                        })
                        live_info[scalp_leg]["filled_df"].loc[ct] = {
                            "ticker": opt_name,
                            "price": entry_price,
                            "action": scalp_side,
                            "stop_price": stop,
                            "take_profit": pt,
                            "spot_price": spot_price,
                            "quantity": quantity,
                        }
                        _register_trade(live_info, is_scalp=True)
                        live_info["scalp_last_burst_key"] = burst_key
                        logging.info(
                            f"[SCALP_ENTRY] used={_cap_used(live_info, True)} cap={_cap_limit(live_info, True)} "
                            f"trend_used={_cap_used(live_info, False)} trend_cap={_cap_limit(live_info, False)}"
                        )
                        log_entry_green(
                            f"[SCALP ENTRY][LIVE] {scalp_side} {opt_name} @ {entry_price:.2f} "
                            f"PT=+{SCALP_PT_POINTS:.1f} SL=-{atr_sl_points:.2f} "
                            f"reason={scalp_sig['reason']} zone={scalp_sig.get('zone')} "
                            f"position_type={live_info[scalp_leg].get('position_type', 'LONG')} "
                            "long=True "
                            f"position_id={position_id}"
                        )
                        logging.info(
                            f"[SCALP_TRADE] class={TRADE_CLASS_SCALP} side={scalp_side} "
                            f"symbol={opt_name} scalp_mode=True trend_mode=False "
                            f"position_type={live_info[scalp_leg].get('position_type', 'LONG')} long=True "
                            f"tag={scalp_sig.get('tag')} "
                            f"position_id={position_id}"
                        )
                        logging.info(
                            "[ENTRY][NEW] "
                            f"timestamp={ct} symbol={opt_name} option_type={scalp_side} "
                            f"position_side={live_info[scalp_leg].get('position_side', 'LONG')} "
                            f"position_id={position_id} "
                            f"lifecycle=OPEN regime=SCALP"
                        )
                        _save_trades_live()
                        store(live_info, account_type)
                        return
                logging.info(
                    f"[ENTRY_ALLOWED_BUT_NOT_EXECUTED][MAX_TRADES_CAP][SCALP] used={_cap_used(live_info, True)} cap={_cap_limit(live_info, True)} "
                    f"trade_count={live_info.get('trade_count', 0)} side={scalp_side} entry blocked"
                )

    # 6. Signal evaluation
    _live_close = float(candles_3m.iloc[-1]["close"]) if len(candles_3m) else float("nan")
    _live_gap = "NO_GAP"
    if np.isfinite(_live_close) and np.isfinite(cam_pre.get("r3", float("nan"))) and _live_close > float(cam_pre.get("r3")):
        _live_gap = "GAP_UP"
    elif np.isfinite(_live_close) and np.isfinite(cam_pre.get("s3", float("nan"))) and _live_close < float(cam_pre.get("s3")):
        _live_gap = "GAP_DOWN"
    _live_bias = "Positive" if _live_gap == "GAP_UP" else ("Negative" if _live_gap == "GAP_DOWN" else "Neutral")
    _live_open_bias = "UNKNOWN"
    if np.isfinite(_live_close):
        if np.isfinite(cam_pre.get("r4", float("nan"))) and _live_close > float(cam_pre.get("r4")):
            _live_open_bias = "Above R4, continuation likely"
        elif np.isfinite(cam_pre.get("r3", float("nan"))) and _live_close > float(cam_pre.get("r3")):
            _live_open_bias = "Above R3, expected momentum continuation"
        elif np.isfinite(cam_pre.get("s4", float("nan"))) and _live_close < float(cam_pre.get("s4")):
            _live_open_bias = "Below S4, downside continuation likely"
        elif np.isfinite(cam_pre.get("s3", float("nan"))) and _live_close < float(cam_pre.get("s3")):
            _live_open_bias = "Below S3, downside pressure active"
        else:
            _live_open_bias = "Inside S3-R3, balanced open"

    # Prefer DTC classification; fall back to gap-based tag for reversal_detector string param
    _dt_name_obj_live = getattr(_live_day_type, "name", None)
    _live_dtc_name = getattr(_dt_name_obj_live, "value", None) if _dt_name_obj_live else None
    _live_day_type_tag = (
        _live_dtc_name if (_live_dtc_name and _live_dtc_name != "UNKNOWN")
        else ("GAP_DAY" if _live_gap in ("GAP_UP", "GAP_DOWN") else "NEUTRAL_DAY")
    )
    
    if _live_day_type_tag != "NEUTRAL_DAY" and _live_day_type_tag != "UNKNOWN":
        logging.info(f"[DAYTYPE_TAG_VALID] tag={_live_day_type_tag}")
    else:
        logging.info(f"[DAYTYPE_TAG_DEFAULT] tag={_live_day_type_tag} (fallback)")

    _rev_sig_live = None
    try:
        _rev_sig_live = detect_reversal(
            candles_3m, cam_pre,
            current_time=ct,
            day_type_tag=_live_day_type_tag,
        )
    except Exception as e:
        logging.warning(f"[REVERSAL_DETECTOR_ERROR] {e}")
    
    if _rev_sig_live:
        logging.info(f"[REVERSAL_SIGNAL_VALID] score={_rev_sig_live.get('score')} side={_rev_sig_live.get('side')}")
    else:
        logging.info("[REVERSAL_SIGNAL_DEFAULT] No reversal signal detected")
        
    _fb_sig_live = detect_failed_breakout(candles_3m, cam_pre)

    # ── LATE-ENTRY GATE (live) — no entries within 25 min of EOD exit ──────
    _bar_min_live = ct.hour * 60 + ct.minute
    if _bar_min_live >= 14 * 60 + 45:
        logging.info(
            f"[ENTRY BLOCKED][LATE_ENTRY] timestamp={ct} symbol={ticker} "
            f"time={ct.hour:02d}:{ct.minute:02d} reason=Too close to EOD (15:10), entry suppressed"
        )
        store(live_info, account_type)
        return

    log_entry_green(
        f"[ENTRY ATTEMPT][TREND][LIVE] symbol={ticker} "
        f"atr={atr:.2f} trade_count={live_info.get('trade_count', 0)} "
        f"day_type={_live_day_type_tag} confidence={getattr(_live_day_type, 'confidence', 'N/A')}"
    )
    quality_ok, allowed_side, gate_reason, st_details = _trend_entry_quality_gate(
        candles_3m=candles_3m,
        candles_15m=hist_yesterday_15m if hist_yesterday_15m is not None else pd.DataFrame(),
        timestamp=ct,
        symbol=ticker,
        adx_min=float(TREND_ENTRY_ADX_MIN),
        cpr_levels=cpr_pre,
        camarilla_levels=cam_pre,
        reversal_signal=_rev_sig_live,
        failed_breakout_signal=_fb_sig_live,
        day_type_result=_live_day_type,
        open_bias_context={"gap_tag": _live_gap, "bias": _live_bias, "open_bias": _live_open_bias},
    )
    if not quality_ok:
        tag = (
            "ST_CONFLICT" if "Supertrend conflict" in gate_reason else
            "SLOPE_MISMATCH" if "Slope mismatch" in gate_reason else
            "WEAK_ADX" if "Weak trend strength" in gate_reason else
            "FAILED_BREAKOUT" if "Failed breakout" in gate_reason else
            "EMA_STRETCH" if "EMA stretch" in gate_reason else
            "OSC_EXTREME"
        )
        logging.info(
            f"[ENTRY BLOCKED][{tag}] "
            f"timestamp={st_details['timestamp']} symbol={st_details['symbol']} "
            f"ST3m_bias={st_details['ST3m_bias']} ST15m_bias={st_details['ST15m_bias']} "
            f"allowed_side={allowed_side} "
            f"close={st_details.get('close')} s4={st_details.get('s4')} r4={st_details.get('r4')} "
            f"s4_threshold={st_details.get('s4_threshold')} r4_threshold={st_details.get('r4_threshold')} "
            f"put_ok={allowed_side == 'PUT'} call_ok={allowed_side == 'CALL'} "
            f"ADX={st_details.get('adx14')} RSI={st_details.get('rsi14')} CCI={st_details.get('cci20')} "
            f"reason={gate_reason}"
        )
        _save_trades_live()
        store(live_info, account_type)
        return

    # TPMA (stored as "vwap" column by build_indicator_dataframe)
    tpma = float(candles_3m["vwap"].iloc[-1]) if "vwap" in candles_3m.columns and not pd.isna(candles_3m["vwap"].iloc[-1]) else None

    # Opening range (first 5 bars of session)
    orb_h, orb_l = get_opening_range(candles_3m)

    # FIX: pivots from previous completed candle
    cpr = cpr_pre
    trad = trad_pre
    cam = cam_pre

    # ── Phase 4: Zone revisit detection (live) ───────────────────────────────
    global _live_zones, _live_zones_date
    _today_str_live = dt.now(time_zone).strftime("%Y-%m-%d")
    if _live_zones_date != _today_str_live:
        _live_zones = []
        if hist_yesterday_15m is not None and len(hist_yesterday_15m) >= 10:
            _live_zones = detect_zones(hist_yesterday_15m)
        if _live_zones:
            logging.info(f"[ZONE_CONTEXT][LIVE] zones_loaded={len(_live_zones)}")
        _live_zones_date = _today_str_live

    _zone_revisit_signal = None
    if _live_zones and atr > 0:
        if np.isfinite(atr):
            update_zone_activity(_live_zones, float(candles_3m["close"].iloc[-1]), float(atr),
                                 str(candles_3m.index[-1]) if hasattr(candles_3m.index[-1], 'strftime') else str(len(candles_3m)))
            _zone_revisit_signal = detect_zone_revisit(candles_3m, _live_zones, float(atr))

    # Pulse metrics dict for scoring
    _pulse_dict = None
    if pulse_metrics is not None:
        if isinstance(pulse_metrics, dict):
            _pulse_dict = pulse_metrics
        else:
            _pulse_dict = {
                "tick_rate": getattr(pulse_metrics, "tick_rate", 0.0),
                "burst_flag": getattr(pulse_metrics, "burst_flag", False),
                "direction_drift": getattr(pulse_metrics, "direction_drift", "NEUTRAL"),
            }

    # ── Compression breakout entry ────────────────────────────────────────────
    if hist_yesterday_15m is not None and len(hist_yesterday_15m) >= 3:
        _compression_state.update(hist_yesterday_15m)

    if _compression_state.has_entry:
        comp_sig = _compression_state.entry_signal
        leg = "call_buy" if comp_sig["side"] == "CALL" else "put_buy"
        if live_info[leg]["trade_flag"] == 0 and not risk_info.get("halt_trading", False):
            if _cap_available(live_info, is_scalp=False):
                logging.info(
                    f"[ENTRY DISPATCH] COMPRESSION_BREAKOUT {comp_sig['side']} "
                    f"sl={comp_sig['sl']:.2f} tg={comp_sig['tg']:.2f} pt={comp_sig['pt']:.2f} | "
                    f"{comp_sig['reason']}"
                )
        _compression_state.consume_entry()
        _save_trades_live()
        store(live_info, account_type)
        return

    signal = detect_signal(
        candles_3m=candles_3m,
        candles_15m=hist_yesterday_15m if hist_yesterday_15m is not None else pd.DataFrame(),
        cpr_levels=cpr,
        camarilla_levels=cam,
        traditional_levels=trad,
        atr=atr,
        include_partial=False,
        current_time=ct,
        vwap=tpma,
        orb_high=orb_h,
        orb_low=orb_l,
        osc_relief_active=st_details.get("osc_relief_override", False),
        zone_signal=_zone_revisit_signal,
        pulse_metrics=_pulse_dict,
    )

    # 7. Entry
    if not signal:
        _save_trades_live()
        store(live_info, account_type)
        return

    side   = signal["side"]
    reason = signal["reason"]
    source = signal.get("source", "UNKNOWN")
    signal["osc_context"] = st_details.get("osc_context", "UNKNOWN")
    signal["day_type"] = st_details.get("day_type_tag", "UNKNOWN")
    signal["open_bias"] = st_details.get("open_bias", "UNKNOWN")
    signal["failed_breakout"] = bool(st_details.get("failed_breakout", False))
    signal["ema_stretch"] = bool(st_details.get("ema_stretch_tagged", False))
    signal["ema_stretch_mult"] = st_details.get("ema_stretch_mult")
    tpma_str = f"{tpma:.1f}" if tpma else "N/A"  # Format tpma conditionally
    logging.info(
        f"[SIGNAL][LIVE] {side} score={signal.get('score','?')} "
        f"source={source} tpma={tpma_str} | {reason}"
    )
    if side != allowed_side:
        logging.info(
            "[ENTRY BLOCKED][ST_SIDE_MISMATCH] "
            f"timestamp={ct} symbol={ticker} ST3m_bias={st_details['ST3m_bias']} "
            f"ST15m_bias={st_details['ST15m_bias']} allowed_side={allowed_side} "
            f"signal_side={side} reason=Supertrend conflict, entry suppressed."
        )
        _save_trades_live()
        store(live_info, account_type)
        return
                
    if (side == "CALL" and put_open) or (side == "PUT" and call_open):
        logging.info(f"[CONFLICT_BLOCKED] Opposing position already open. Blocking {side} entry.")
        return
            
    if hist_yesterday_15m is not None and not hist_yesterday_15m.empty:
        bias15 = hist_yesterday_15m.iloc[-1].get("supertrend_bias", "NEUTRAL")
        logging.info(f"[BIAS][15m] {bias15}")

    if risk_info.get("halt_trading", False):
        return

    leg = "call_buy" if side == "CALL" else "put_buy"
    try:
        if live_info[leg]["trade_flag"] == 0:
            if not _cap_available(live_info, is_scalp=False):
                logging.info(
                    f"[ENTRY_ALLOWED_BUT_NOT_EXECUTED][MAX_TRADES_CAP][TREND] used={_cap_used(live_info, False)} cap={_cap_limit(live_info, False)} "
                    f"scalp_used={_cap_used(live_info, True)} scalp_cap={_cap_limit(live_info, True)} "
                    f"trade_count={live_info.get('trade_count', 0)} side={side} entry blocked"
                )
                return

            opt_type = "CE" if side == "CALL" else "PE"
            opt_name, strike = get_option_by_moneyness(
                spot_price, opt_type,
                moneyness=CALL_MONEYNESS if side == "CALL" else PUT_MONEYNESS
            )

            if opt_name and opt_name in df.index:
                ltp_val = df.loc[opt_name, "ltp"]
                entry_price = float(ltp_val) if (ltp_val and not pd.isna(ltp_val)) else spot_price
                if not entry_price or entry_price <= 0:
                    return

                # FIX: pass candles_df
                _last_bar_live = candles_3m.iloc[-1] if candles_3m is not None and not candles_3m.empty else None
                _adx_entry_live = float(_last_bar_live.get("adx14", 0)) if _last_bar_live is not None and pd.notna(_last_bar_live.get("adx14")) else 0.0
                levels = build_dynamic_levels(
                    entry_price, atr, side,
                    entry_candle=len(candles_3m) - 1,
                    candles_df=candles_3m,
                    adx_value=_adx_entry_live,
                )
                if not levels.get("valid", False):
                    logging.warning(f"[ENTRY SKIP] {side} levels failed")
                    return
                stop = levels["stop"]
                pt = levels["pt"]
                tg = levels["tg"]
                trail_start = levels["trail_start"]
                trail_step = levels["trail_step"]

                # Apply DTC PM modifiers: override trail_step / record pm_max_hold
                _pm_max_hold_live = None
                if _live_day_type.pm_trail_step is not None:
                    trail_step = _live_day_type.pm_trail_step
                    logging.debug(
                        f"[DAY_TYPE][PM] LIVE trail_step→{trail_step:.3f} "
                        f"day_type={_live_day_type_tag} confidence={_live_day_type.confidence}"
                    )
                if _live_day_type.pm_max_hold is not None:
                    _pm_max_hold_live = _live_day_type.pm_max_hold
                    logging.debug(
                        f"[DAY_TYPE][PM] LIVE pm_max_hold→{_pm_max_hold_live} "
                        f"day_type={_live_day_type_tag} confidence={_live_day_type.confidence}"
                    )

                # ── Build RegimeContext (Phase 3) ──────────────────────────
                _rc = compute_regime_context(
                    st_details=st_details,
                    atr=atr,
                    reversal_signal=_rev_sig_live if '_rev_sig_live' in dir() else None,
                    failed_breakout_signal=_fb_sig_live if '_fb_sig_live' in dir() else None,
                    compression_state_str=_compression_state.market_state if hasattr(_compression_state, 'market_state') else "NEUTRAL",
                    bar_timestamp=str(ct),
                    symbol=ticker,
                )
                log_regime_context(_rc)
                regime_context = _rc.atr_regime

                success, order_id = send_live_entry_order(opt_name, quantity, 1)
                if not success:
                    logging.warning(f"[ENTRY FAILED][LIVE] {side} {opt_name}")
                    return

                # FIX: all required exit-state keys
                position_id = f"{opt_name}_{int(ct.timestamp())}_{live_info.get('trade_count', 0) + 1}"
                live_info[leg].update({
                    "option_name":    opt_name,
                    "quantity":       quantity,
                    "buy_price":      entry_price,
                    "order_type":     ORDER_TYPE,
                    "trade_flag":     1,
                    "pnl":            0,
                    "reason":         reason,
                    "source":         source,
                    "order_id":       order_id,
                    "position_id":    position_id,
                    "entry_time":     ct,
                    "entry_candle":   len(candles_3m) - 1,
                    "side":           side,
                    "position_side":  _long_position_side(side),
                    "stop":           stop,
                    "pt":             pt,
                    "tg":             tg,
                    "trail_start":    trail_start,
                    "trail_step":     trail_step,
                    "trail_updates":  0,
                    "consec_count":   0,
                    "prev_gap":       0,
                    "peak_momentum":  0,
                    "peak_candle":    len(candles_3m) - 1,
                    "plateau_count":  0,
                    "is_open":        True,
                    "lifecycle_state": "OPEN",
                    "scalp_mode":     False,
                    "scalp_pt_points": SCALP_PT_POINTS,
                    "scalp_sl_points": SCALP_SL_POINTS,
                    "trend_min_hold_bars": TREND_MIN_HOLD_BARS,
                    "trend_extreme_move_atr_mult": TREND_EXTREME_MOVE_ATR_MULT,
                    "partial_booked": False,
                    "pm_max_hold":    _pm_max_hold_live,   # DTC override (None = use default)
                    "day_type_modifier": getattr(_live_day_type, "signal_modifier", 0),
                    "hf_exit_manager": OptionExitManager(
                        entry_price=entry_price,
                        side=side,
                        risk_buffer=1.0,
                    ),
                    "hf_deferred_logged": 0,
                })
                # Merge regime context keys (replaces manual osc_context/day_type/etc.)
                live_info[leg].update(_rc.to_state_keys())

                live_info[leg]["filled_df"].loc[ct] = {
                    'ticker': opt_name,
                    'price': entry_price,
                    'action': side,
                    'stop_price': stop,
                    'take_profit': pt,
                    'spot_price': spot_price,
                    'quantity': quantity
                }
                _register_trade(live_info, is_scalp=False)
                logging.info(
                    f"[TREND_ENTRY] used={_cap_used(live_info, False)} cap={_cap_limit(live_info, False)} "
                    f"scalp_used={_cap_used(live_info, True)} scalp_cap={_cap_limit(live_info, True)}"
                )

                logging.info(
                    f"{GREEN}[ENTRY][LIVE] {side} {opt_name} @ {entry_price:.2f} "
                    f"SL={stop:.2f} PT={pt:.2f} TG={tg:.2f} "
                    f"ATR={atr:.1f} score={signal.get('score','?')} source={source}{RESET}"
                )
                logging.info(
                    "[ENTRY][NEW] "
                    f"timestamp={ct} symbol={opt_name} option_type={side} "
                    f"position_side={live_info[leg].get('position_side', 'LONG')} "
                    f"position_id={position_id} "
                    f"lifecycle=OPEN regime={regime_context}"
                )
            else:
                logging.warning(f"[ENTRY_ALLOWED_BUT_NOT_EXECUTED] {side} no option. opt_name={opt_name}")
    except Exception as e:
        logging.error(f"[ENTRY ERROR][LIVE] {e}", exc_info=True)

    _save_trades_live()
    store(live_info, account_type)


def _save_trades_live():
    frames = [live_info["call_buy"]["filled_df"], live_info["put_buy"]["filled_df"]]
    frames = [f for f in frames if not f.empty]
    if frames:
        pd.concat(frames).to_csv(
            f"trades_{strategy_name}_{dt.now(time_zone).date()}_LIVE.csv", index=True
        )
# ============================================== RUN Strategy ==============================================


# --- Helper: sleep until next boundary ---
def sleep_until_next_boundary(interval=180, tz="Asia/Kolkata"):
    """Sleep until next candle boundary. Only used in LIVE fallback mode."""
    now           = dt.now(tz)
    seconds       = now.minute * 60 + now.second
    next_boundary = ((seconds // interval) + 1) * interval
    sleep_secs    = next_boundary - seconds
    logging.debug(f"[SLEEP] {sleep_secs}s until next {interval}s boundary")
    time.sleep(sleep_secs)


# ─────────────────────────────────────────────────────────────────────────────
# OFFLINE REPLAY ENGINE
# ─────────────────────────────────────────────────────────────────────────────

def _print_replay_summary():
    """Print trade summary after a replay run."""
    frames = [paper_info["call_buy"]["filled_df"], paper_info["put_buy"]["filled_df"]]
    frames = [f for f in frames if not f.empty]
    if not frames:
        logging.info("[REPLAY SUMMARY] No trades taken.")
        return

    combined = pd.concat(frames)
    total    = len(combined)
    winners  = (combined["pnl_points"] > 0).sum() if "pnl_points" in combined.columns else 0
    losers   = (combined["pnl_points"] < 0).sum() if "pnl_points" in combined.columns else 0
    total_pnl = combined["pnl_value"].sum() if "pnl_value" in combined.columns else 0
    win_rate  = winners / total * 100 if total > 0 else 0

    logging.info(
        f"\n{'='*60}\n"
        f"  REPLAY SUMMARY\n"
        f"{'='*60}\n"
        f"  Total trades : {total}\n"
        f"  Winners      : {winners}  ({win_rate:.1f}%)\n"
        f"  Losers       : {losers}\n"
        f"  Total PnL    : {total_pnl:.2f}\n"
        f"{'='*60}"
    )

    if "exit_reason" in combined.columns:
        logging.info("[REPLAY] Exit reasons:")
        for reason, count in combined["exit_reason"].value_counts().items():
            logging.info(f"  {reason:20s}: {count}")

    if "source" in combined.columns:
        logging.info("[REPLAY] Signal sources:")
        for src, count in combined["source"].value_counts().items():
            logging.info(f"  {src:20s}: {count}")

    if "side" in combined.columns:
        logging.info("[REPLAY] By side:")
        for side, count in combined["side"].value_counts().items():
            logging.info(f"  {side:20s}: {count}")


def run_offline_replay(tick_db, symbols_list=None, date_str=None,
                       min_warmup_candles=35, signal_only=False,
                       output_dir=".", db_path=None):
    """
    Candle-by-candle offline replay using tick_db data. No live connection needed.

    Simulates exactly how the live bot sees data: at each 3m bar boundary
    only candles[0..i] are visible — no lookahead. Indicators are rebuilt
    incrementally on each slice.

    Args:
        tick_db             TickDatabase instance
        symbols_list        list of symbols; defaults to config symbols
        date_str            'YYYY-MM-DD' to filter replay to a specific date.
                            Warmup bars from the previous day are always prepended
                            for indicator accuracy regardless of this filter.
                            None = use all data in DB.
        min_warmup_candles  3m bars needed before evaluation starts.
                            Supertrend/ADX need 14+; 30 is a safe default.
        signal_only         True  → log signals, skip trade simulation.
                            False → simulate entries, SL/PT/TG exits, log PnL.
        output_dir          Directory for CSV trade log. Default = current dir.

    Two CSVs are saved when signal_only=False:
        signals_<sym>_<date>.csv   — every signal that fired (bar, time, side, score, reason)
        trades_<sym>_<date>.csv    — every completed trade (entry, exit, PnL)

    Typical usage:
        # Terminal:
        python execution.py                  # trade simulation
        python execution.py --signal-only    # signals only

        # From Python / notebook:
        from tickdb import tick_db
        from execution import run_strategy
        run_strategy(["NSE:NIFTY50-INDEX"], mode="OFFLINE", tick_db=tick_db,
                     date_str="2026-02-20", signal_only=True)
    """
    import os
    from collections import Counter
    # build_indicator_dataframe imported at top of module (from orchestration_v3)
    from signals import detect_signal

    if symbols_list is None:
        symbols_list = symbols if isinstance(symbols, list) else [symbols]

    os.makedirs(output_dir, exist_ok=True)

    logging.info(
        f"\n{'='*64}\n"
        f"  OFFLINE REPLAY\n"
        f"  date        = {date_str or 'all available'}\n"
        f"  signal_only = {signal_only}\n"
        f"  warmup      = {min_warmup_candles} bars\n"
        f"{'='*64}\n"
    )

    # ── Helper: find the sort/align column (prefer tz-aware "date") ───────────
    def _time_col(df):
        for c in ("date", "time", "timestamp", "candle_time"):
            if c in df.columns:
                return c
        return df.columns[0]

    # ── Helper: find string column for date-prefix filtering (YYYY-MM-DD...) ──
    # Direct SQL output has both "date" (datetime) and "time" (string).
    # Use "time" for startswith-style date filtering.
    def _str_col(df):
        return "time" if "time" in df.columns else _time_col(df)

    # ── Helper: merge and dedup two DataFrames on their time column ───────────
    def _merge_candles(hist, today):
        dfs = [d for d in [hist, today] if d is not None and not d.empty]
        if not dfs:
            return pd.DataFrame()
        out  = pd.concat(dfs, ignore_index=True)
        tcol = _time_col(out)
        return (out.drop_duplicates(subset=[tcol])
                   .sort_values(tcol)
                   .reset_index(drop=True))

    # ── Helper: option premium proxy from underlying move ─────────────────────
    # tick_db has no option prices. We simulate a 1.5× leveraged option premium.
    # Entry premium = ATM option rough value (underlying * 0.006).
    # Subsequent moves: delta ≈ 0.5, gamma amplification ≈ 1.5×.
    # This is intentionally approximate — the signal quality is what matters.
    def _option_premium(underlying_close, entry_underlying, entry_premium, side):
        move = underlying_close - entry_underlying
        if side == "PUT":
            move = -move                         # PUT profits when market falls
        delta_pnl = move * 0.5 * 1.5            # delta 0.5, 1.5× leverage
        return max(entry_premium + delta_pnl, 1.0)

    # ── Resolve DB path: explicit arg → tick_db attributes → give up ──────────
    _db_path = db_path
    # Auto-append .db extension if missing — prevents connecting to empty stub files
    if _db_path and not str(_db_path).endswith(".db"):
        _db_path = str(_db_path) + ".db"
    if _db_path is None:
        for _attr in ("db_path", "path", "database", "db_file", "_db_path"):
            _val = getattr(tick_db, _attr, None)
            if isinstance(_val, str) and _val:
                _db_path = _val
                break

    def _db_date_from_path(path_str):
        if not path_str:
            return None
        m = re.search(r"ticks_(\d{4}-\d{2}-\d{2})\.db$", str(path_str))
        return m.group(1) if m else None

    if _db_path and date_str:
        db_date = _db_date_from_path(_db_path)
        if db_date and db_date != date_str:
            db_dir = str(pathlib.Path(_db_path).parent)
            suggested = os.path.join(db_dir, f"ticks_{date_str}.db")
            if not os.path.exists(suggested):
                fallback = os.path.join(r"C:\SQLite\ticks", f"ticks_{date_str}.db")
                suggested = fallback if os.path.exists(fallback) else suggested
            logging.error(
                f"[REPLAY ERROR] DB file date ({db_date}) does not match replay date ({date_str})"
            )
            if os.path.exists(suggested):
                logging.error(f"[REPLAY ERROR] Suggested DB path: {suggested}")
            else:
                logging.error(
                    f"[REPLAY ERROR] Suggested DB path not found: {suggested}"
                )
            return

    if _db_path:
        logging.info(f"[REPLAY] DB path: {_db_path}")
    else:
        logging.warning(
            "[REPLAY] Could not determine DB path from tick_db. "
            "Pass it explicitly: run_strategy(..., db_path=r'C:\\SQLite\\ticks\\ticks_2026-02-20.db')"
        )

    # ── Helper: query SQLite directly using confirmed schema ─────────────────
    # Schema (from DB inspection):
    #   candles_3m_ist / candles_15m_ist
    #   Columns: trade_date TEXT, ist_slot TEXT, symbol TEXT,
    #            open REAL, high REAL, low REAL, close REAL, volume REAL
    #
    # Output column "date": tz-aware IST datetime — required by
    # build_indicator_dataframe and detect_signal.
    # "time": "YYYY-MM-DD HH:MM:SS" string — used for de-dup and date filtering.
    def _load_direct(interval, sym):
        if not _db_path:
            return pd.DataFrame()
        table = "candles_3m_ist" if "3m" in interval else "candles_15m_ist"
        try:
            import sqlite3
            conn  = sqlite3.connect(_db_path)
            query = f"""
                SELECT
                    trade_date || 'T' || ist_slot  AS _dt,
                    trade_date || ' ' || ist_slot  AS time,
                    trade_date, ist_slot,
                    open, high, low, close,
                    COALESCE(volume, 0) AS volume
                FROM {table}
                WHERE symbol = ?
                ORDER BY trade_date, ist_slot
            """
            df   = pd.read_sql_query(query, conn, params=(sym,))
            conn.close()

            if df.empty:
                logging.warning(f"  [{table}] 0 rows for symbol={sym}")
                return pd.DataFrame()

            # Build tz-aware "date" column (IST) — this is what downstream expects
            ist       = "Asia/Kolkata"
            df["date"] = (pd.to_datetime(df["_dt"])
                            .dt.tz_localize(ist))
            df = df.drop(columns=["_dt"])

            for col in ("open", "high", "low", "close", "volume"):
                df[col] = pd.to_numeric(df[col], errors="coerce")

            logging.info(
                f"  [{table}] {len(df)} rows  "
                f"({df['time'].iloc[0]} -> {df['time'].iloc[-1]})"
            )
            return df.reset_index(drop=True)

        except Exception as e:
            logging.error(f"  Direct SQL failed ({table}): {e}")
            return pd.DataFrame()

    def _load_best(interval, sym):
        # Direct SQL first — always correct, bypasses use_yesterday date filtering
        df = _load_direct(interval, sym)
        if not df.empty:
            return df
        # Fallback to fetch_candles (works during live session)
        for flag in (False, True):
            try:
                df = tick_db.fetch_candles(interval, use_yesterday=flag, symbol=sym)
                if df is not None and not df.empty:
                    logging.debug(f"  fetch_candles(use_yesterday={flag}) -> {len(df)} rows")
                    return df
            except Exception as e:
                logging.debug(f"  fetch_candles(use_yesterday={flag}): {e}")
        return pd.DataFrame()

    for sym in symbols_list:
        logging.info(f"[REPLAY] Loading candles for {sym} ...")

        # Load using best-of-both strategy (handles post-market and live sessions)
        try:
            df_3m_all  = _load_best("3m",  sym)
            df_15m_all = _load_best("15m", sym)
        except Exception as e:
            logging.error(f"[REPLAY] DB load failed for {sym}: {e}")
            continue

        # ── Load previous trading day(s) of 15m data for indicator warmup ───────
        # ADX14 needs 28 bars, CCI20 needs 20 bars → need >28 15m rows before today.
        # Scan up to 5 prev trading days from the replay DB date and prepend them.
        if _db_path and date_str:
            import sqlite3 as _sql2, os as _os2
            _db_dir   = str(pathlib.Path(_db_path).parent)
            _ref_date = pd.Timestamp(date_str)
            _prev_frames_15m = []
            _prev_frames_3m  = []
            _days_back        = 0
            _days_found       = 0
            while _days_back < 14 and _days_found < 5:
                _days_back += 1
                _cand = (_ref_date - pd.Timedelta(days=_days_back))
                if _cand.weekday() >= 5:          # skip Sat/Sun
                    continue
                _cand_str  = _cand.strftime("%Y-%m-%d")
                _cand_path = _os2.path.join(_db_dir, f"ticks_{_cand_str}.db")
                if not _os2.path.exists(_cand_path):
                    continue
                try:
                    for _tbl, _lst in [("candles_15m_ist", _prev_frames_15m),
                                        ("candles_3m_ist",  _prev_frames_3m)]:
                        _q = f"""
                            SELECT
                                trade_date || 'T' || ist_slot  AS _dt,
                                trade_date || ' ' || ist_slot  AS time,
                                trade_date, ist_slot,
                                open, high, low, close,
                                COALESCE(volume, 0) AS volume
                            FROM {_tbl}
                            WHERE symbol = ?
                            ORDER BY trade_date, ist_slot
                        """
                        with _sql2.connect(_cand_path) as _c2:
                            _tmp = pd.read_sql_query(_q, _c2, params=(sym,))
                        if not _tmp.empty:
                            _ist = "Asia/Kolkata"
                            _tmp["date"] = pd.to_datetime(_tmp["_dt"]).dt.tz_localize(_ist)
                            _tmp = _tmp.drop(columns=["_dt"])
                            for _col in ("open","high","low","close","volume"):
                                _tmp[_col] = pd.to_numeric(_tmp[_col], errors="coerce")
                            _lst.append(_tmp)
                except Exception as _ex:
                    logging.debug(f"[REPLAY WARMUP] {_cand_str}: {_ex}")
                    continue
                _days_found += 1

            if _prev_frames_15m:
                _prev_15m = pd.concat(_prev_frames_15m, ignore_index=True)
                df_15m_all = pd.concat([_prev_15m, df_15m_all], ignore_index=True)
                df_15m_all = (df_15m_all.drop_duplicates(subset=["time"])
                                         .sort_values("time")
                                         .reset_index(drop=True))
                logging.info(f"[REPLAY] 15m warmup: prepended {len(_prev_15m)} rows "
                             f"from {_days_found} prev day(s) -> total {len(df_15m_all)} 15m rows)")

            if _prev_frames_3m:
                _prev_3m = pd.concat(_prev_frames_3m, ignore_index=True)
                df_3m_all = pd.concat([_prev_3m, df_3m_all], ignore_index=True)
                df_3m_all = (df_3m_all.drop_duplicates(subset=["time"])
                                       .sort_values("time")
                                       .reset_index(drop=True))

        if df_3m_all.empty:
            logging.warning(
                f"[REPLAY] No 3m candles found for {sym}. "
                f"Check that the DB file has session data."
            )
            continue

        tc3  = _time_col(df_3m_all)          # datetime col for sorting/alignment
        sc3  = _str_col(df_3m_all)           # string col for date-prefix filtering
        tc15 = _time_col(df_15m_all) if not df_15m_all.empty else tc3
        sc15 = _str_col(df_15m_all)  if not df_15m_all.empty else sc3
        total_bars = len(df_3m_all)

        # Show available range
        logging.info(
            f"[REPLAY] {sym}: {total_bars} total 3m bars  "
            f"({df_3m_all.iloc[0][sc3]} -> {df_3m_all.iloc[-1][sc3]})"
        )

        # Auto-reduce warmup if DB has fewer bars than requested
        effective_warmup = min_warmup_candles
        if total_bars <= min_warmup_candles:
            effective_warmup = max(14, total_bars // 3)
            logging.warning(
                f"[REPLAY] Only {total_bars} bars but warmup={min_warmup_candles}. "
                f"Auto-reduced to {effective_warmup}."
            )
            if total_bars <= effective_warmup + 1:
                logging.warning(
                    f"[REPLAY] Still not enough bars ({total_bars}). "
                    f"The DB may only contain post-market data."
                )
                continue

        # ── Optional date filter ───────────────────────────────────────────────
        replay_start_idx = effective_warmup
        if date_str:
            try:
                mask    = df_3m_all[sc3].astype(str).str.startswith(date_str)
                matches = mask[mask].index
                if len(matches) == 0:
                    available = sorted(df_3m_all[sc3].astype(str).str[:10].unique())
                    logging.warning(
                        f"[REPLAY] No bars for date={date_str}. "
                        f"Dates in DB: {available}"
                    )
                    continue
                first_match      = int(matches[0])
                replay_start_idx = max(first_match, effective_warmup)
                logging.info(
                    f"[REPLAY] Date {date_str}: bars {first_match}–{total_bars-1} "
                    f"(replay from bar {replay_start_idx})"
                )
            except Exception as e:
                logging.warning(f"[REPLAY] Date filter error: {e}")

        replay_bars = total_bars - replay_start_idx
        logging.info(
            f"[REPLAY] Warmup={replay_start_idx} | Evaluating={replay_bars} bars\n"
        )

        # ── Per-run state ─────────────────────────────────────────────────────
        signals_fired  = []
        trade_log      = []
        blocker_counts = Counter()

        global last_signal_candle_time
        last_signal_candle_time = None  # reset dedup so replay is clean

        # ── PositionManager — single source of truth for open position ─────────
        # Prevents repeated orders: once a trade is open, detect_signal is
        # bypassed entirely. Each bar instead runs pm.update() to monitor the
        # position and decide HOLD vs EXIT using:
        #   HARD_STOP   — premium drops to 50% of entry
        #   TRAIL_STOP  — ratcheting stop after 40% gain
        #   PARTIAL     — book half at 60% gain, move stop to breakeven
        #   MOMENTUM_PEAK — RSI extreme + (CCI extreme OR slope reversal)
        #   ST_FLIP     — SuperTrend flips against position
        #   MAX_HOLD    — safety valve at MAX_HOLD_BARS bars
        #   EOD_EXIT    — force close at 15:15 IST

        # ── Position Manager (imported from position_manager.py) ──────────────
        # Single source of truth for all exit logic across replay, paper, live.
        # Replacing the former inline _PM class — thresholds live in position_manager.py
        pm = make_replay_pm(lot_size=quantity)
        cooldown_until = 0   # bar index after which new entries are allowed again

        # ── Day Type Classifier — initialized once per session ────────────
        # Uses previous day OHLC from the first replay bar to build pivot context.
        # DTC is updated every bar; locked at 12:00 (midday — classification stable).
        _dtc      = None   # populated on first bar below
        _day_type = DayTypeResult()   # UNKNOWN until DTC initializes
        _daily_cam = None  # fixed daily Camarilla levels from previous day OHLC
        _opening_bias_logged = False
        _session_open_price = None
        _session_prev_close = None
        _open_bias_context = {"gap_tag": "UNKNOWN", "bias": "Unknown", "open_bias": "UNKNOWN"}
        _zone_revisit_signal = None
        _zone_cache = pathlib.Path(f"zones_{sym.replace(':', '_')}_{date_str or 'session'}.json")
        _zones = load_zones(_zone_cache)
        if not _zones:
            _hist_15m = detect_zones(df_15m_all)
            _zones = _hist_15m
        if not _zones:
            _fyers_df_15m = pd.DataFrame()
            try:
                from zone_detector import fetch_fyers_15m_history
                _fyers_df_15m = fetch_fyers_15m_history(fyers, sym, days=10)
            except Exception:
                _fyers_df_15m = pd.DataFrame()
            if not _fyers_df_15m.empty:
                _zones = detect_zones(_fyers_df_15m)
        if _zones:
            save_zones(_zones, _zone_cache)
        if _zones:
            logging.info(f"[ZONE_CONTEXT] zones_loaded={len(_zones)} cache={_zone_cache}")

        # ── Compression state — fresh per symbol/session ───────────────────────
        _comp_state = CompressionState()

        # Pre-build a simple _FakeTime factory (avoids class-inside-loop issues)
        class _FakeTime:
            __slots__ = ("hour", "minute")
            def __init__(self, h, m):
                self.hour, self.minute = h, m

        # ── Main loop ─────────────────────────────────────────────────────────
        for i in range(replay_start_idx, total_bars):
            slice_3m = df_3m_all.iloc[:i + 1].copy()
            cur_time = slice_3m.iloc[-1][tc3]    # tz-aware datetime for 15m alignment

            # Align 15m: bars whose datetime <= current 3m datetime
            if not df_15m_all.empty:
                slice_15m = df_15m_all[df_15m_all[tc15] <= cur_time].copy()
            else:
                slice_15m = pd.DataFrame()

            # Build indicators
            try:
                slice_3m = build_indicator_dataframe(sym, slice_3m, interval="3m")
                if not slice_15m.empty:
                    slice_15m = build_indicator_dataframe(sym, slice_15m, interval="15m")
            except Exception as e:
                logging.debug(f"[REPLAY bar={i}] indicator error: {e}")
                continue

            last_row  = slice_3m.iloc[-1]
            bar_time  = last_row.get(sc3, str(cur_time))
            bar_close = float(last_row["close"])

            # ── Bar time gate ─────────────────────────────────────────────────
            ts    = pd.Timestamp(bar_time)
            bar_t = ts.hour * 60 + ts.minute
            if _session_open_price is None and np.isfinite(bar_close):
                _session_open_price = float(bar_close)

            # ── Day Type Classifier — update every bar ─────────────────────────
            # Initialize DTC on first bar using previous-session OHLC
            if _dtc is None and len(slice_3m) >= 2:
                try:
                    prev_bar = slice_3m.iloc[-2]
                    _cpr0  = calculate_cpr(float(prev_bar["high"]),
                                           float(prev_bar["low"]),
                                           float(prev_bar["close"]))
                    _cam0  = calculate_camarilla_pivots(float(prev_bar["high"]),
                                                        float(prev_bar["low"]),
                                                        float(prev_bar["close"]))
                    _dtc   = make_day_type_classifier(
                        _cam0, _cpr0,
                        float(prev_bar["high"]),
                        float(prev_bar["low"]),
                        float(prev_bar["close"]),
                    )
                    _session_prev_close = float(prev_bar["close"])
                    _daily_cam = _cam0  # fixed for entire session
                    logging.info(
                        f"[DAY TYPE] Classifier initialized "
                        f"R3={_cam0['r3']:.0f} R4={_cam0['r4']:.0f} "
                        f"S3={_cam0['s3']:.0f} S4={_cam0['s4']:.0f} "
                        f"NarrowCPR={_dtc.pc.is_narrow_cpr} "
                        f"CompressedCam={_dtc.pc.is_compressed_camarilla}"
                    )
                except Exception as _e:
                    logging.debug(f"[DAY TYPE] init error: {_e}")

            if _dtc is not None:
                _day_type = _dtc.update(slice_3m)
                if (not _opening_bias_logged) and bar_t >= (9 * 60 + 30):
                    _opening_bias_logged = True
                    _gap_pct = float("nan")
                    if (
                        _session_open_price is not None
                        and _session_prev_close is not None
                        and _session_prev_close != 0
                    ):
                        _gap_pct = ((_session_open_price - _session_prev_close) / _session_prev_close) * 100.0

                    if np.isfinite(_gap_pct):
                        if _gap_pct >= 0.5:
                            _gap_tag = "GAP_UP"
                            _bias_txt = "Positive"
                        elif _gap_pct <= -0.5:
                            _gap_tag = "GAP_DOWN"
                            _bias_txt = "Negative"
                        else:
                            _gap_tag = "NEUTRAL"
                            _bias_txt = "Neutral"
                    else:
                        _gap_tag = "UNKNOWN"
                        _bias_txt = "Unknown"

                    _open_bias_msg = "Inside S3-R3, balanced open"
                    _close_ref = float(slice_3m["close"].iloc[-1]) if len(slice_3m) else float("nan")
                    _cam_ctx = getattr(_dtc, "pc", None)
                    if _cam_ctx is not None and np.isfinite(_close_ref):
                        if np.isfinite(_cam_ctx.r4) and _close_ref > _cam_ctx.r4:
                            _open_bias_msg = "Above R4, continuation likely"
                        elif np.isfinite(_cam_ctx.r3) and _close_ref > _cam_ctx.r3:
                            _open_bias_msg = "Above R3, expected momentum continuation"
                        elif np.isfinite(_cam_ctx.s4) and _close_ref < _cam_ctx.s4:
                            _open_bias_msg = "Below S4, downside continuation likely"
                        elif np.isfinite(_cam_ctx.s3) and _close_ref < _cam_ctx.s3:
                            _open_bias_msg = "Below S3, downside pressure active"

                    logging.info(
                        "[DAY_TYPE] "
                        f"{_gap_tag} bias={_bias_txt} open={_gap_pct:+.2f}% vs prev close"
                    )
                    logging.info(f"[OPEN_BIAS] {_open_bias_msg}")
                    _open_bias_context = {
                        "gap_tag": _gap_tag,
                        "bias": _bias_txt,
                        "open_bias": _open_bias_msg,
                    }
                # Lock classification at 12:00 (midday — stable for rest of session)
                if bar_t == 12 * 60 and _day_type.confidence in ("MEDIUM", "HIGH"):
                    _dtc.lock_classification()
                    _day_type.log()

            # ── Compression state update (15m aligned) ───────────────────────────
            if not slice_15m.empty and len(slice_15m) >= 3:
                _comp_state.update(slice_15m)

            # ── POSITION MONITOR — runs every bar when a trade is open ─────────
            # Works in both signal_only=True AND False modes.
            # While pm.is_open(): detect_signal is bypassed — no repeated orders.
            if pm.is_open():
                # Discard any pending compression entry — can't act while in a trade
                if _comp_state.has_entry:
                    _comp_state.consume_entry()

                # Enrich 3m row with 15m bias so ST_FLIP_2 can check HTF alignment
                last_row_enriched = last_row.copy()
                if not slice_15m.empty:
                    last_15m = slice_15m.iloc[-1]
                    last_row_enriched["st_bias_15m"]    = str(last_15m.get("supertrend_bias", "NEUTRAL"))
                    last_row_enriched["st_slope_15m"]   = str(last_15m.get("supertrend_slope", "FLAT"))
                    last_row_enriched["adx14_15m"]      = last_15m.get("adx14", float("nan"))
                else:
                    last_row_enriched["st_bias_15m"]  = "NEUTRAL"
                    last_row_enriched["st_slope_15m"] = "FLAT"
                    last_row_enriched["adx14_15m"]    = float("nan")

                decision = pm.update(i, bar_time, bar_close, last_row_enriched)
                if decision.should_exit:
                    record = pm.close(i, bar_time, bar_close,
                                      decision.exit_px, decision.reason, quantity)
                    trade_log.append(record)
                    # Cooldown: 5 bars minimum (15 min).
                    # After a LOSING trade (pnl_points <= 0): extend to 10 bars (30 min).
                    # Prevents immediate revenge entries in choppy/ranging conditions.
                    _is_loss = record.get("pnl_points", 0) <= 0
                    cooldown_until = i + (10 if _is_loss else 5)
                    if _is_loss:
                        logging.info(
                            f"  [LOSS COOLDOWN] {record.get('exit_reason','?')} "
                            f"pnl={record.get('pnl_points',0):.1f} — "
                            f"next entry allowed after bar {cooldown_until} (30 min)"
                        )
                    # After exit: don't evaluate entry on the same bar
                continue

            # ── COOLDOWN — bars immediately after an exit ─────────────────────
            if i < cooldown_until:
                blocker_counts["COOLDOWN"] = blocker_counts.get("COOLDOWN", 0) + 1
                continue

            # ── POST-MARKET GATE — no entries after close ─────────────────────
            if bar_t >= 15 * 60 + 30:
                blocker_counts["POST_MARKET"] = blocker_counts.get("POST_MARKET", 0) + 1
                continue

            # ── LATE-ENTRY GATE — no new entries within 25 min of EOD exit ────
            # EOD exit at 15:10, PRE_EOD at ~15:01. Entries after 14:45 have
            # insufficient time to reach profit targets before forced exit.
            if bar_t >= 14 * 60 + 45:
                blocker_counts["LATE_ENTRY"] = blocker_counts.get("LATE_ENTRY", 0) + 1
                continue

            # ── ENTRY EVALUATION — only runs when no position is open ──────────
            atr, _ = resolve_atr(slice_3m)

            # ── Compression breakout entry — bypasses scoring gate ────────────
            if _comp_state.has_entry:
                comp_sig = _comp_state.entry_signal
                entry_premium = round(bar_close * 0.006, 1)
                logging.info(
                    f"  {GREEN}[ENTRY DISPATCH] bar={i} {bar_time} | COMPRESSION_BREAKOUT "
                    f"{comp_sig['side']} "
                    f"strength={comp_sig['compression_zone']['compression_strength']:.1f}x "
                    f"sl={comp_sig['sl']:.2f} tg={comp_sig['tg']:.2f} pt={comp_sig['pt']:.2f}{RESET}"
                )
                # P3: bias alignment for compression trades
                _comp_gap = _open_bias_context.get("gap_tag", "UNKNOWN")
                _comp_side = comp_sig["side"]
                if _comp_gap == "UNKNOWN":
                    comp_sig["open_bias_aligned"] = "NEUTRAL"
                elif (_comp_side == "CALL" and _comp_gap == "GAP_UP") or (_comp_side == "PUT" and _comp_gap == "GAP_DOWN"):
                    comp_sig["open_bias_aligned"] = "ALIGNED"
                else:
                    comp_sig["open_bias_aligned"] = "MISALIGNED"
                apply_day_type_to_pm(pm, _day_type)
                pm.open(i, bar_time, bar_close, entry_premium, comp_sig)
                signals_fired.append({
                    "bar":         i,
                    "time":        bar_time,
                    "side":        comp_sig["side"],
                    "score":       comp_sig["score"],
                    "reason":      comp_sig["reason"],
                    "source":      comp_sig["source"],
                    "pivot":       "",
                    "underlying":  bar_close,
                    "est_premium": entry_premium,
                })
                _comp_state.consume_entry()
                continue

            # TPMA (stored as "vwap" by build_indicator_dataframe)
            tpma = (float(slice_3m["vwap"].iloc[-1])
                    if "vwap" in slice_3m.columns
                    and not pd.isna(slice_3m["vwap"].iloc[-1]) else None)

            orb_h, orb_l = get_opening_range(slice_3m)

            pivot_src = slice_3m.iloc[-2] if len(slice_3m) >= 2 else slice_3m.iloc[-1]
            cpr  = calculate_cpr(pivot_src["high"], pivot_src["low"], pivot_src["close"])
            trad = calculate_traditional_pivots(pivot_src["high"], pivot_src["low"], pivot_src["close"])
            cam  = calculate_camarilla_pivots(pivot_src["high"], pivot_src["low"], pivot_src["close"])
            if np.isfinite(atr):
                update_zone_activity(_zones, bar_close, float(atr), bar_time)
                _zone_revisit_signal = detect_zone_revisit(slice_3m, _zones, float(atr))

            _rev_day_type_tag = (
                getattr(getattr(_day_type, "name", None), "value", None)
                if _day_type is not None else None
            )
            _rev_sig_replay = detect_reversal(
                slice_3m, cam,
                current_time=bar_time,
                day_type_tag=_rev_day_type_tag,
            )
            _fb_sig_replay = detect_failed_breakout(slice_3m, cam)
            quality_ok, allowed_side, gate_reason, st_details = _trend_entry_quality_gate(
                candles_3m=slice_3m,
                candles_15m=slice_15m,
                timestamp=bar_time,
                symbol=sym,
                adx_min=float(TREND_ENTRY_ADX_MIN),
                cpr_levels=cpr,
                camarilla_levels=cam,
                reversal_signal=_rev_sig_replay,
                failed_breakout_signal=_fb_sig_replay,
                day_type_result=_day_type,
                open_bias_context=_open_bias_context,
                daily_camarilla_levels=_daily_cam,
            )
            if not quality_ok:
                blocker_key = (
                    "DAILY_CAM_FILTER"
                    if "daily S4" in gate_reason or "daily R4" in gate_reason
                    else (
                        "ST_CONFLICT"
                        if "Supertrend conflict" in gate_reason
                        else (
                            "SLOPE_MISMATCH"
                            if "Slope mismatch" in gate_reason
                            else (
                                "WEAK_ADX" if "Weak trend strength" in gate_reason else (
                                    "FAILED_BREAKOUT" if "Failed breakout" in gate_reason else (
                                        "EMA_STRETCH" if "EMA stretch" in gate_reason else "OSC_EXTREME"
                                    )
                                )
                            )
                        )
                    )
                )
                blocker_counts[blocker_key] = blocker_counts.get(blocker_key, 0) + 1
                logging.info(
                    "[SIGNAL BLOCKED] "
                    f"reason={gate_reason} "
                    f"timestamp={bar_time} symbol={sym} "
                    f"ST3m_bias={st_details['ST3m_bias']} ST15m_bias={st_details['ST15m_bias']} "
                    f"allowed_side={allowed_side} "
                    f"close={st_details.get('close')} s4={st_details.get('s4')} r4={st_details.get('r4')} "
                    f"s4_threshold={st_details.get('s4_threshold')} r4_threshold={st_details.get('r4_threshold')} "
                    f"put_ok={allowed_side == 'PUT'} call_ok={allowed_side == 'CALL'} "
                    f"ADX={st_details.get('adx14')} RSI={st_details.get('rsi14')} CCI={st_details.get('cci20')}"
                )
                continue

            fake_time = _FakeTime(ts.hour, ts.minute)

            # Phase 4: zone_revisit_signal already computed at line ~5060
            _zone_dict = _zone_revisit_signal if isinstance(_zone_revisit_signal, dict) else None


            try:
                signal = detect_signal(
                    candles_3m=slice_3m,
                    candles_15m=slice_15m,
                    cpr_levels=cpr,
                    camarilla_levels=cam,
                    traditional_levels=trad,
                    atr=atr,
                    include_partial=False,
                    current_time=fake_time,
                    vwap=tpma,
                    orb_high=orb_h,
                    orb_low=orb_l,
                    day_type_result=_day_type,
                    osc_relief_active=st_details.get("osc_relief_override", False),
                    zone_signal=_zone_dict,
                    daily_camarilla_levels=_daily_cam,
                )
            except Exception as e:
                logging.warning(f"[REPLAY bar={i}] detect_signal error: {e}")
                blocker_counts["SIGNAL_ERROR"] = blocker_counts.get("SIGNAL_ERROR", 0) + 1
                continue

            if not signal:
                blocked_by = signal.get("reason", "SCORE_LOW") if signal else "NO_SIGNAL"
                blocker_counts[blocked_by] += 1
                continue

            # Block WEAK signals — only HIGH/MEDIUM strength allowed
            sig_strength = signal.get("strength", "MEDIUM")
            if sig_strength == "WEAK":
                blocker_counts["SCORE_LOW"] = blocker_counts.get("SCORE_LOW", 0) + 1
                logging.info(
                    f"[SIGNAL BLOCKED] WEAK score={signal.get('score','?')} "
                    f"src={signal.get('source','?')} — HIGH/MEDIUM required"
                )
                continue

            side   = signal["side"]
            score  = signal.get("score", "?")
            reason = signal["reason"]
            source = signal.get("source", "?")
            if side != allowed_side:
                # Conflict Governance
                if _rev_sig_paper and _rev_sig_paper.get("side") == allowed_side and _rev_sig_paper.get("score", 0) > score:
                     logging.info(f"[CONFLICT_BLOCKED] Trend signal {side} blocked by stronger Reversal signal {allowed_side}")
                else:
                     logging.info(f"[CONFLICT_BLOCKED] Trend signal {side} blocked by ST alignment {allowed_side}")
                     
                blocker_counts["ST_SIDE_MISMATCH"] = blocker_counts.get("ST_SIDE_MISMATCH", 0) + 1
                logging.info(
                    "[SIGNAL BLOCKED] "
                    f"reason=Supertrend conflict, entry suppressed. "
                    f"timestamp={bar_time} symbol={sym} "
                    f"ST3m_bias={st_details['ST3m_bias']} ST15m_bias={st_details['ST15m_bias']} "
                    f"allowed_side={allowed_side} signal_side={side}"
                )
                continue
                
            # # Conflict Governance: Check Open Positions
            # if (side == "CALL" and put_open) or (side == "PUT" and call_open):
            #     logging.info(f"[CONFLICT_BLOCKED] Opposing position already open. Blocking {side} entry.")
            #     continue
            call_open = False
            put_open = False

            if side == "CALL":
                call_open = True
            elif side == "PUT":
                put_open = True

            # … later when closing positions …
            if side == "CALL":
                call_open = False
            elif side == "PUT":
                put_open = False

            # ── Build RegimeContext (Phase 3) ─────────────────────────────
            _rc = compute_regime_context(
                st_details=st_details,
                atr=atr,
                reversal_signal=_rev_sig if '_rev_sig' in dir() else None,
                failed_breakout_signal=_fb_sig if '_fb_sig' in dir() else None,
                zone_signal=_zone_revisit_signal if isinstance(_zone_revisit_signal, dict) else None,
                compression_state_str=_comp_state.market_state if hasattr(_comp_state, 'market_state') else "NEUTRAL",
                bar_timestamp=str(bar_time),
                symbol=sym,
            )
            log_regime_context(_rc)

            # Enrich signal with current ST and day type for PM entry tracking
            signal["st_bias"]  = str(last_row.get("supertrend_bias", "?"))
            signal["pivot"]    = signal.get("pivot", "")
            signal["day_type"] = _rc.day_type
            signal["osc_context"] = _rc.osc_context
            signal["open_bias"] = _rc.open_bias
            signal["failed_breakout"] = _rc.has_failed_breakout
            signal["ema_stretch"] = _rc.ema_stretch_tagged
            signal["ema_stretch_mult"] = _rc.ema_stretch_mult
            signal["entry_regime_context"] = _rc  # frozen snapshot for exit-time access
            if isinstance(_zone_revisit_signal, dict):
                signal["zone_revisit"] = True
                signal["zone_revisit_type"] = _zone_revisit_signal.get("zone_type", "UNKNOWN")
                signal["zone_revisit_action"] = _zone_revisit_signal.get("action", "UNKNOWN")
                signal["zone_age_bars"] = _zone_revisit_signal.get("zone_age_bars", 0)

            # Phase 6.1.2: Attach tilt + governance state for replay attribution
            signal["tilt_state"] = st_details.get("tilt_state", "NEUTRAL")
            signal["governance"] = st_details.get("governance", "STRICT")

            # P3: Attach open_bias_aligned for log_parser trade attribution.
            # A CALL trade aligns with a bullish open (GAP_UP); PUT aligns with GAP_DOWN.
            _gap = _open_bias_context.get("gap_tag", "UNKNOWN")
            if _gap == "UNKNOWN":
                signal["open_bias_aligned"] = "NEUTRAL"
            elif (side == "CALL" and _gap == "GAP_UP") or (side == "PUT" and _gap == "GAP_DOWN"):
                signal["open_bias_aligned"] = "ALIGNED"
            else:
                signal["open_bias_aligned"] = "MISALIGNED"

            # Apply day type overrides to PM (trail step, max hold)
            apply_day_type_to_pm(pm, _day_type)

            # Option premium approximation: ATM ≈ 0.6% of underlying
            entry_premium = round(bar_close * 0.006, 1)

            # ── Log the entry signal ───────────────────────────────────────────
            logging.info(
                f"  {GREEN}[SIGNAL→ENTRY] bar={i} {bar_time} | "
                f"{side} score={score} src={source} pivot={signal.get('pivot','')}{RESET}"
            )
            logging.info(
                f"    underlying={bar_close:.2f}  "
                f"premium≈{entry_premium:.1f}  "
                f"ST={last_row.get('supertrend_bias','?')}  "
                f"EMA9={last_row.get('ema9', float('nan')):.1f}  "
                f"CCI={last_row.get('cci20', float('nan')):.1f}  "
                f"RSI={last_row.get('rsi14', float('nan')):.1f}"
            )
            logging.info(f"    reason: {reason}")

            # Track in signals_fired (first entry only per trade)
            signals_fired.append({
                "bar":         i,
                "time":        bar_time,
                "side":        side,
                "score":       score,
                "reason":      reason,
                "source":      source,
                "pivot":       signal.get("pivot", ""),
                "osc_context": signal.get("osc_context", "UNKNOWN"),
                "day_type":    signal.get("day_type", "UNKNOWN"),
                "open_bias":   signal.get("open_bias", "UNKNOWN"),
                "failed_breakout": signal.get("failed_breakout", False),
                "ema_stretch": signal.get("ema_stretch", False),
                "zone_revisit": signal.get("zone_revisit", False),
                "zone_revisit_action": signal.get("zone_revisit_action", "NONE"),
                "underlying":  bar_close,
                "est_premium": entry_premium,
            })

            # ── Open position via PositionManager ─────────────────────────────
            # This locks out all subsequent bars from calling detect_signal
            # until this trade is exited.
            pm.open(i, bar_time, bar_close, entry_premium, signal)

        # ── Force close if still open at end of data ──────────────────────────
        if pm.is_open():
            last_ul = float(df_3m_all.iloc[-1]["close"])
            record  = pm.force_close_eod(
                total_bars - 1, df_3m_all.iloc[-1][sc3], last_ul
            )
            if record:
                trade_log.append(record)

        # Summary
        safe_sym  = sym.replace(":", "_")
        date_tag  = date_str or "all"
        sep       = "-" * 64

        logging.info(f"\n{sep}")
        logging.info(f"  RESULTS: {sym}  ({date_tag})")
        logging.info(sep)
        logging.info(f"  Trades taken  : {len(signals_fired)}  "
                     f"(one entry per trade — PM locks out re-entry while open)")

        if signals_fired:
            call_ct = sum(1 for s in signals_fired if s["side"] == "CALL")
            put_ct  = sum(1 for s in signals_fired if s["side"] == "PUT")
            scores  = [s["score"] for s in signals_fired if isinstance(s["score"], (int, float))]
            avg_sc  = sum(scores) / len(scores) if scores else 0
            logging.info(f"    CALL={call_ct}  PUT={put_ct}  avg_score={avg_sc:.1f}")
            sig_csv = os.path.join(output_dir, f"signals_{safe_sym}_{date_tag}.csv")
            pd.DataFrame(signals_fired).to_csv(sig_csv, index=False)
            logging.info(f"    -> {sig_csv}")

        # Trade log shown in all modes (PM tracks exits even in signal_only)
        if trade_log:
            wins     = sum(1 for t in trade_log if t["pnl_points"] > 0)
            losses   = len(trade_log) - wins
            tot_pnl  = sum(t["pnl_value"] for t in trade_log)
            win_rate = wins / len(trade_log) * 100
            logging.info(f"\n  Trades closed : {len(trade_log)}")
            logging.info(f"  Win / Loss    : {wins} / {losses}  ({win_rate:.1f}%)")
            logging.info(f"  Total PnL (Rs): {tot_pnl:+.2f}")
            logging.info(f"\n  Exit breakdown:")
            for reason, cnt in Counter(t["exit_reason"] for t in trade_log).most_common():
                logging.info(f"    {reason:22s}: {cnt}")
            logging.info(f"\n  Trade details:")
            for t in trade_log:
                color = GREEN if t["pnl_points"] >= 0 else RED
                # Sanitize exit_reason to remove unicode characters
                reason_safe = t['exit_reason'].replace('→', '->').replace('₹', 'Rs').replace('≥', '>=').replace('≤', '<=')
                logging.info(
                    f"    {color}{t['side']:4s} entry={t['entry_time']} "
                    f"exit={t['exit_time']} held={t['bars_held']}bars "
                    f"pnl={t['pnl_points']:+.1f}pts ({t['pnl_value']:+.0f}Rs) "
                    f"[{reason_safe}]{RESET}"
                )
            trd_csv = os.path.join(output_dir, f"trades_{safe_sym}_{date_tag}.csv")
            pd.DataFrame(trade_log).to_csv(trd_csv, index=False)
            logging.info(f"\n  -> {trd_csv}")
        else:
            logging.info("  Trades closed : 0")

        if blocker_counts:
            logging.info(f"\n  Signal blockers:")
            for reason, cnt in blocker_counts.most_common(10):
                logging.info(f"    {reason:30s}: {cnt} bars")

        logging.info("-" * 80 + "\n")

        # Auto-generate dashboard report
        if not signal_only:
            try:
                from config import log_file
                from dashboard import generate_full_report
                if os.path.exists(log_file):
                    logging.info(f"[REPLAY] Auto-generating dashboard for {log_file}...")
                    generate_full_report(log_file, output_dir=output_dir)
            except Exception as e:
                logging.warning(f"[REPLAY] Dashboard generation failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# RUN STRATEGY — LIVE fallback + entry point for run_offline_replay
# ─────────────────────────────────────────────────────────────────────────────

def run_strategy(symbols, tz=time_zone, end_time=None,
                 replay_data=None, mode="LIVE",
                 tick_db=None, date_str=None,
                 signal_only=False, min_warmup_candles=35,
                 output_dir=".", db_path=None):
    """
    Unified strategy entry point.

    mode="LIVE"
        Single-pass fallback. Primary live execution runs via main.py.

    mode="REPLAY"
        Snapshot replay — pass replay_data={sym: (df_3m, df_15m)}.
        Calls paper_order() once with the full snapshot.

    mode="OFFLINE"
        Candle-by-candle walk-through using tick_db. No live connection needed.

        Required:
            tick_db             TickDatabase instance

        Optional:
            date_str            'YYYY-MM-DD' to replay a specific date
            signal_only         True = signals only, no trade simulation
            min_warmup_candles  bars before evaluation starts (default 30)
            output_dir          directory for CSV output (default '.')

        Usage:
            from tickdb import tick_db
            from execution import run_strategy

            # Signal evaluation only — fastest way to check what fires
            run_strategy(["NSE:NIFTY50-INDEX"], mode="OFFLINE",
                         tick_db=tick_db, date_str="2026-02-20",
                         signal_only=True)

            # Full trade simulation with PnL
            run_strategy(["NSE:NIFTY50-INDEX"], mode="OFFLINE",
                         tick_db=tick_db, date_str="2026-02-20",
                         signal_only=False, output_dir="./replay_results")
    """

    # ── OFFLINE ──────────────────────────────────────────────────────────────
    if mode == "OFFLINE":
        if tick_db is None:
            logging.error("[run_strategy] mode=OFFLINE requires tick_db argument")
            return
        run_offline_replay(
            tick_db=tick_db,
            symbols_list=symbols if isinstance(symbols, list) else [symbols],
            date_str=date_str,
            min_warmup_candles=min_warmup_candles,
            signal_only=signal_only,
            output_dir=output_dir,
            db_path=db_path,
        )
        return

    # ── LIVE / REPLAY need setup.py (API connection) ─────────────────────────
    _ensure_setup()

    # ── LIVE (single-pass fallback) ───────────────────────────────────────────
    if mode == "LIVE":
        for sym in symbols:
            logging.info(f"{GRAY}[STRATEGY][LIVE] {sym}{RESET}")
            spot_local = None
            try:
                q = fyers.quotes(data={"symbols": sym})
                spot_local = q["d"][0]["v"]["lp"]
            except Exception as e:
                logging.debug(f"[STRATEGY] Quote API: {e}")

            candles_3m, candles_15m = update_candles_and_signals(
                symbol=sym, spot_price=spot_local, tick_db=tick_db
            )
            if candles_3m is None or candles_3m.empty:
                logging.warning(f"[STRATEGY] No candles for {sym}")
                continue

            if account_type.upper() == "PAPER":
                paper_order(candles_3m, hist_yesterday_15m=candles_15m, mode=mode)
            else:
                live_order(candles_3m, hist_yesterday_15m=candles_15m)
        return

    # ── REPLAY (snapshot) ─────────────────────────────────────────────────────
    for sym in symbols:
        if not replay_data or sym not in replay_data:
            continue
        candles_3m, candles_15m = replay_data[sym]
        if candles_3m is None or candles_3m.empty:
            continue
        logging.info(
            f"[REPLAY] {sym}: "
            f"3m={len(candles_3m)} "
            f"15m={len(candles_15m) if not candles_15m.empty else 0}"
        )
        if account_type.upper() == "PAPER":
            paper_order(candles_3m, hist_yesterday_15m=candles_15m, mode="REPLAY")
        else:
            live_order(candles_3m, hist_yesterday_15m=candles_15m)

    _print_replay_summary()


# ─────────────────────────────────────────────────────────────────────────────
# STANDALONE ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    """
    Run offline replay from the terminal — no live connection needed.

    Usage:
        python execution.py                           # trade simulation, today's DB data
        python execution.py --signal-only             # signals only
        python execution.py --date 2026-02-20         # specific date
        python execution.py --date 2026-02-20 --signal-only
        python execution.py --out ./results           # custom output directory
        python execution.py --warmup 50               # more warmup bars
    """
    import sys, os, argparse

    parser = argparse.ArgumentParser(description="Offline replay using tick_db data")
    parser.add_argument("--date",        default=None,  help="Date to replay YYYY-MM-DD")
    parser.add_argument("--signal-only", action="store_true", help="Log signals only, no trade sim")
    parser.add_argument("--out",         default=".",   help="Output directory for CSVs")
    parser.add_argument("--warmup",      default=30,    type=int, help="Warmup bars (default 30)")
    parser.add_argument("--sym",         default="NSE:NIFTY50-INDEX", help="Symbol to replay")
    parser.add_argument("--db",          default=None,
                        help=r"Direct SQLite DB path, e.g. C:\SQLite\ticks\ticks_2026-02-20.db "
                             "(use when fetch_candles returns too few rows post-market)")
    args = parser.parse_args()

    try:
        from tickdb import tick_db as _tick_db
    except ImportError:
        logging.error("tickdb module not found — cannot run offline replay")
        sys.exit(1)

    run_strategy(
        symbols=[args.sym],
        mode="OFFLINE",
        tick_db=_tick_db,
        date_str=args.date,
        signal_only=args.signal_only,
        min_warmup_candles=args.warmup,
        output_dir=args.out,
        db_path=args.db,
    )
