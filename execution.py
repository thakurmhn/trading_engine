    # ===== execution.py =====
import logging
import pickle
import pathlib
import re
import numpy as np
import pandas as pd
import pendulum as dt
from fyers_apiv3 import fyersModel
import time
from datetime import datetime, timedelta

from config import (
    time_zone, strategy_name, MAX_TRADES_PER_DAY, account_type, quantity,
    CALL_MONEYNESS, PUT_MONEYNESS, profit_loss_point, ENTRY_OFFSET, ORDER_TYPE,
    MAX_DAILY_LOSS, MAX_DRAWDOWN, OSCILLATOR_EXIT_MODE, symbols,
    TREND_ENTRY_ADX_MIN,
)
from setup import (
    df, fyers, ticker, option_chain, spot_price,
    start_time, end_time, hist_data
)
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

from signals import detect_signal, get_opening_range
# from tickdb import tick_db
from orchestration import update_candles_and_signals  # uses fixed ADX/CCI
from orchestration import build_indicator_dataframe   # uses fixed ADX/CCI
from position_manager import make_replay_pm
from option_exit_manager import OptionExitManager
from day_type import (make_day_type_classifier, apply_day_type_to_pm,
                      DayType, DayTypeResult)
from compression_detector import CompressionState

# ===========================================================
# ANSI COLORS for order logs
RESET   = "\033[0m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
RED     = "\033[91m"
MAGENTA = "\033[95m"
GRAY    = "\033[90m"
CYAN    = "\033[96m"

# ===== Momentum scalp settings =====
SCALP_PT_POINTS = 7.0
SCALP_SL_POINTS = 4.0
SCALP_COOLDOWN_MINUTES = 20
SCALP_HISTORY_MAXLEN = 120
STARTUP_SUPPRESSION_MINUTES = 5
RESTART_STATE_VERSION = 1
DEFAULT_TIME_EXIT_CANDLES = 8
DEFAULT_OSC_RSI_CALL = 75.0
DEFAULT_OSC_RSI_PUT = 25.0
DEFAULT_OSC_CCI_CALL = 130.0
DEFAULT_OSC_CCI_PUT = -130.0
DEFAULT_OSC_WR_CALL = -10.0
DEFAULT_OSC_WR_PUT = -88.0

#===========================================================
# Initalize filled_df
try:
    filled_df
except NameError:
    filled_df = pd.DataFrame(columns=["status", "filled_qty", "avg_price", "symbol"])


#===================================================================

today_str = datetime.now().strftime("%Y-%m-%d")

_compression_state = CompressionState()   # for paper_order / live_order

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
    info.setdefault("scalp_hist", {"CALL": [], "PUT": []})

    persisted = _load_restart_state(account_type_)
    last_exit = _parse_ts(info.get("last_exit_time")) or _parse_ts(persisted.get("last_exit_time"))
    scalp_cd = _parse_ts(info.get("scalp_cooldown_until")) or _parse_ts(persisted.get("scalp_cooldown_until"))
    startup_until = _parse_ts(info.get("startup_suppression_until"))
    if startup_until is None:
        startup_until = now + timedelta(minutes=STARTUP_SUPPRESSION_MINUTES)
    persisted_startup = _parse_ts(persisted.get("startup_suppression_until"))
    if persisted_startup is not None and persisted_startup > startup_until:
        startup_until = persisted_startup

    info["last_exit_time"] = last_exit
    info["scalp_cooldown_until"] = scalp_cd
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

    quality_ok, allowed_side, gate_reason, st_details = _trend_entry_quality_gate(
        candles_3m=candles_3m,
        candles_15m=candles_15m if candles_15m is not None else pd.DataFrame(),
        timestamp=now_ts,
        symbol=symbol,
        adx_min=float(TREND_ENTRY_ADX_MIN),
        cpr_levels=cpr_pre,
        camarilla_levels=cam_pre,
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
    Jan 8th baseline: always ITM with 100-point difference.
    CALL: ATM - strike_diff
    PUT:  ATM + strike_diff
    """

    from config import strike_diff

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

    sel = option_chain[
        (option_chain['strike_price'] == strike) &
        (option_chain['option_type'].isin([side, side.replace('E','ALL')]))  # CE/PE or CALL/PUT
    ]['symbol']

    if sel.empty:
        side_df = option_chain[option_chain['option_type'].isin([side, side.replace('E','ALL')])].copy()
        if side_df.empty:
            logging.error(f"[get_option_by_moneyness] No options available for side={side}")
            return None, None
        side_df['strike_diff_abs'] = (side_df['strike_price'] - strike).abs()
        side_df = side_df.sort_values('strike_diff_abs')
        symbol = side_df.iloc[0]['symbol']
        strike = side_df.iloc[0]['strike_price']
        logging.warning(
            f"[get_option_by_moneyness] Fallback ITM for {side}: requested {strike}, using nearest available"
        )
        return symbol, strike

    return sel.squeeze(), strike


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


def _update_scalp_premium_history(info, side, price, ts):
    """Store bounded premium history for momentum scalp detection."""
    if "scalp_hist" not in info or not isinstance(info["scalp_hist"], dict):
        info["scalp_hist"] = {"CALL": [], "PUT": []}
    side_hist = info["scalp_hist"].setdefault(side, [])
    ts_obj = pd.Timestamp(ts)
    if ts_obj.tzinfo is None:
        ts_obj = ts_obj.tz_localize(time_zone)
    else:
        ts_obj = ts_obj.tz_convert(time_zone)
    side_hist.append({"ts": ts_obj, "price": float(price)})
    if len(side_hist) > SCALP_HISTORY_MAXLEN:
        trimmed = len(side_hist) - SCALP_HISTORY_MAXLEN
        del side_hist[: len(side_hist) - SCALP_HISTORY_MAXLEN]
        logging.debug(f"[SCALP HIST] side={side} trimmed={trimmed} maxlen={SCALP_HISTORY_MAXLEN}")


def _rsi_series(series, period=8):
    """Lightweight RSI for premium momentum checks."""
    s = pd.Series(series, dtype="float64")
    diff = s.diff()
    gain = diff.clip(lower=0).rolling(period, min_periods=period).mean()
    loss = (-diff.clip(upper=0)).rolling(period, min_periods=period).mean()
    rs = gain / loss.replace(0, pd.NA)
    return (100 - (100 / (1 + rs))).astype(float)


def _detect_scalp_momentum_signal(info, spot_px, ts):
    """Detect premium momentum burst on CALL/PUT candidates.

    Trigger if premium expansion > baseline volatility and at least one
    momentum trigger is true:
      1) EMA gap/slope threshold,
      2) ATR-like premium spike,
      3) RSI momentum-zone cross.
    """
    signals = []
    for side, opt_side in [("CALL", "CE"), ("PUT", "PE")]:
        opt_name, _ = get_option_by_moneyness(
            spot_px,
            opt_side,
            moneyness=CALL_MONEYNESS if side == "CALL" else PUT_MONEYNESS,
        )
        if not opt_name or opt_name not in df.index:
            continue
        ltp = df.loc[opt_name, "ltp"]
        if ltp is None or (isinstance(ltp, float) and pd.isna(ltp)):
            continue
        px = float(ltp)
        if px <= 0:
            continue
        _update_scalp_premium_history(info, side, px, ts)
        hist = info.get("scalp_hist", {}).get(side, [])
        if len(hist) < 10:
            continue

        s = pd.Series([x["price"] for x in hist], dtype="float64")
        ret = s.diff().fillna(0.0)
        baseline_vol = float(ret.tail(20).std(ddof=0)) if len(ret) >= 5 else 0.0
        baseline_vol = max(0.25, baseline_vol)
        expansion = abs(float(ret.iloc[-1])) > (1.1 * baseline_vol)

        ema_fast = s.ewm(span=5, adjust=False).mean()
        ema_slow = s.ewm(span=13, adjust=False).mean()
        gap_now = float(ema_fast.iloc[-1] - ema_slow.iloc[-1])
        gap_prev = float(ema_fast.iloc[-2] - ema_slow.iloc[-2])
        gap_slope = gap_now - gap_prev
        gap_thr = max(0.5, 0.003 * float(s.iloc[-1]))

        atr_like = float(ret.tail(5).abs().mean())
        atr_base = float(ret.tail(20).abs().mean()) if len(ret) >= 20 else baseline_vol
        atr_base = max(0.25, atr_base)
        atr_spike = atr_like > (1.35 * atr_base)

        rsi = _rsi_series(s, period=8)
        rsi_now = float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50.0
        rsi_prev = float(rsi.iloc[-2]) if not pd.isna(rsi.iloc[-2]) else 50.0

        if side == "CALL":
            ema_ok = gap_now > gap_thr and gap_slope > 0
            osc_ok = rsi_prev <= 60 and rsi_now > 60
            score = abs(gap_now)
        else:
            ema_ok = gap_now < -gap_thr and gap_slope < 0
            osc_ok = rsi_prev >= 60 and rsi_now < 60
            score = abs(gap_now)

        if expansion and (ema_ok or atr_spike or osc_ok):
            reasons = []
            if ema_ok:
                reasons.append("EMA_GAP")
            if atr_spike:
                reasons.append("ATR_SPIKE")
            if osc_ok:
                reasons.append("OSC_CROSS")
            signals.append(
                {
                    "side": side,
                    "symbol": opt_name,
                    "price": px,
                    "reason": "+".join(reasons),
                    "score": score,
                }
            )

    if not signals:
        return None
    signals.sort(key=lambda x: x["score"], reverse=True)
    return signals[0]


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
            "[ENTRY BLOCKED][ST_CONFLICT] "
            f"timestamp={details['timestamp']} symbol={details['symbol']} "
            f"ST3m_bias={details['ST3m_bias']} ST15m_bias={details['ST15m_bias']} "
            "reason=Supertrend bias conflict, entry suppressed."
        )

    return aligned, allowed_side, details


def _trend_entry_quality_gate(
    candles_3m,
    candles_15m,
    timestamp,
    symbol,
    adx_min=18.0,
    rsi_min=35.0,
    rsi_max=65.0,
    cci_min=-120.0,
    cci_max=120.0,
    cpr_levels=None,
    camarilla_levels=None,
):
    """Hard quality gate for trend entries.

    Conditions:
    1) Supertrend bias alignment: 3m and 15m must both be BULLISH (CALL) or BEARISH (PUT).
    2) 3m Supertrend slope must confirm bias direction (UP for BULLISH, DOWN for BEARISH).
       15m slope is not checked.
    3) ADX must be > adx_min.
    4) Oscillators must not be in extremes:
       RSI in [35, 65], CCI in [-120, 120].
    """
    logging.info(
        "[ENTRY CONFIG] "
        f"timestamp={timestamp} symbol={symbol} "
        f"adx_min={adx_min} rsi_range=[{rsi_min},{rsi_max}] "
        f"cci_range=[{cci_min},{cci_max}]"
    )
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
        and cpr_width == "NARROW"
        and compressed_cam
    )
    osc_override_call = bool(
        allowed_side == "CALL"
        and np.isfinite(rsi_val)
        and rsi_val > 70.0
        and close_above_r4
        and cpr_width == "NARROW"
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
    logging.info(
        "[ENTRY DIAG][S4_R4_BREAK] "
        f"timestamp={timestamp} symbol={symbol} close={close_val} s4={s4_val} r4={r4_val} "
        f"atr={atr_val} s4_threshold={s4_thr} r4_threshold={r4_thr} "
        f"put_ok={allowed_side == 'PUT'} call_ok={allowed_side == 'CALL'} "
        f"close_below_s4={close_below_s4} close_above_r4={close_above_r4} "
        f"cpr_width={cpr_width} compressed_cam={compressed_cam}"
    )

    if not aligned:
        logging.info(
            "[ENTRY BLOCKED][ST_CONFLICT] "
            f"timestamp={timestamp} symbol={symbol} allowed_side={allowed_side} "
            "reason=Supertrend conflict, entry suppressed."
        )
        return False, allowed_side, "Supertrend conflict, entry suppressed.", st_details

    slope_ok_3m = (
        (st_details["ST3m_bias"] == "BULLISH" and str(st_details["ST3m_slope"]).upper() == "UP")
        or (st_details["ST3m_bias"] == "BEARISH" and str(st_details["ST3m_slope"]).upper() == "DOWN")
    )
    if not slope_ok_3m:
        logging.info(
            "[ENTRY BLOCKED][ST_SLOPE_CONFLICT] "
            f"timestamp={timestamp} symbol={symbol} allowed_side={allowed_side} "
            f"ST3m_bias={st_details['ST3m_bias']} ST3m_slope={st_details['ST3m_slope']} "
            "reason=3m slope does not confirm bias direction, entry suppressed."
        )
        return False, allowed_side, "3m slope does not confirm bias direction, entry suppressed.", st_details
    logging.info(
        "[ENTRY ALLOWED][ST_BIAS_OK] "
        f"timestamp={timestamp} symbol={symbol} allowed_side={allowed_side} "
        f"ST15m_bias={st_details['ST15m_bias']} ST3m_bias={st_details['ST3m_bias']} "
        f"ST3m_slope={st_details['ST3m_slope']} "
        "reason=15m/3m biases aligned and 3m slope confirmed."
    )

    if not np.isfinite(adx_val) or adx_val <= float(adx_min):
        logging.info(
            "[ENTRY BLOCKED][WEAK_ADX] "
            f"timestamp={timestamp} symbol={symbol} allowed_side={allowed_side} "
            f"ADX={adx_val} adx_min={adx_min} reason=Weak trend strength, entry suppressed."
        )
        return False, allowed_side, "Weak trend strength, entry suppressed.", st_details

    if (
        (np.isfinite(rsi_val) and (rsi_val < float(rsi_min) or rsi_val > float(rsi_max)))
        or (np.isfinite(cci_val) and (cci_val < float(cci_min) or cci_val > float(cci_max)))
    ):
        if osc_override:
            logging.info(
                "[ENTRY ALLOWED][OSC_OVERRIDE_PIVOT_BREAK] "
                f"timestamp={timestamp} symbol={symbol} allowed_side={allowed_side} "
                f"close={close_val} s4={s4_val} r4={r4_val} "
                f"s4_threshold={s4_thr} r4_threshold={r4_thr} atr={atr_val} "
                f"RSI={rsi_val} CCI={cci_val} cpr_width={cpr_width} compressed_cam={compressed_cam}"
            )
            return True, allowed_side, "OK", st_details
        logging.info(
            "[ENTRY BLOCKED][OSC_EXTREME] "
            f"timestamp={timestamp} symbol={symbol} allowed_side={allowed_side} "
            f"RSI={rsi_val} CCI={cci_val} "
            f"rsi_range=[{rsi_min},{rsi_max}] cci_range=[{cci_min},{cci_max}] "
            "reason=Oscillator extreme, entry suppressed."
        )
        return False, allowed_side, "Oscillator extreme, entry suppressed.", st_details

    return True, allowed_side, "OK", st_details


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

    min_bars_for_pt_tg = 3
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
        logging.info(
            "[EXIT AUDIT] "
            f"timestamp={timestamp} symbol={symbol} option_type={side} position_side={position_side} "
            f"exit_type={exit_type} "
            f"reason={reason} triggering_condition={triggering_condition} "
            f"candle={i} bars_held={bars_held} regime={regime_ctx} position_id={position_id}{pm}"
        )

    # 1) HFT exit - highest precedence override
    hf_mgr = state.get("hf_exit_manager")
    if hf_mgr is not None:
        try:
            if hf_mgr.check_exit(current_ltp, timestamp, current_volume=option_volume):
                hf_reason = hf_mgr.last_reason or "HF_EXIT"
                if hf_reason == "MOMENTUM_EXHAUSTION" and bars_held < 2:
                    logging.info(
                        "[EXIT SUPPRESSED] "
                        f"symbol={symbol} option_type={side} position_side={position_side} "
                        f"reason=Premature exit suppressed, minimum hold enforced. bars_held={bars_held}"
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

    # 2) Stop loss - always active catastrophic backstop
    if stop is not None and current_ltp <= stop:
        audit("SL", "SL_HIT", f"ltp<={stop:.2f}")
        logging.info(
            f"{RED}[EXIT][SL_HIT] {side} ltp={current_ltp:.2f} stop={stop:.2f} bars_held={bars_held}{RESET}"
        )
        return True, "SL_HIT"

    # 2B) Momentum scalp exits (only for scalp trades, no min-bar gate)
    if state.get("scalp_mode", False):
        scalp_pt = float(state.get("scalp_pt_points", SCALP_PT_POINTS))
        scalp_sl = float(state.get("scalp_sl_points", SCALP_SL_POINTS))
        premium_move = float(current_ltp - entry_price)
        if premium_move >= scalp_pt:
            audit("SCALP_PT_HIT", "SCALP_PT_HIT", f"premium_move>={scalp_pt:.2f}", premium_move=premium_move)
            logging.info(
                f"{GREEN}[EXIT][SCALP_PT_HIT] {side} premium_move={premium_move:.2f} "
                f"target={scalp_pt:.2f} ltp={current_ltp:.2f}{RESET}"
            )
            return True, "SCALP_PT_HIT"
        if premium_move <= -scalp_sl:
            audit("SCALP_SL_HIT", "SCALP_SL_HIT", f"premium_move<=-{scalp_sl:.2f}", premium_move=premium_move)
            logging.info(
                f"{RED}[EXIT][SCALP_SL_HIT] {side} premium_move={premium_move:.2f} "
                f"stop=-{scalp_sl:.2f} ltp={current_ltp:.2f}{RESET}"
            )
            return True, "SCALP_SL_HIT"
        return False, None

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
    if bars_held < min_bars_for_pt_tg and (tg_hit or pt_hit):
        if bars_held <= 0 and pt_hit:
            audit("PT", "PT_HIT", f"ltp>={pt:.2f}")
            state["partial_booked"] = True
            if (state.get("stop") or 0) < entry_price:
                state["stop"] = entry_price
            return True, "PT_HIT"
        audit("MIN_BAR", "DEFERRED", f"bars_held<{min_bars_for_pt_tg}")
        if tg_hit:
            logging.info(
                f"{YELLOW}[EXIT DEFERRED] TG hit before min bars ({bars_held} < {min_bars_for_pt_tg}). "
                f"ltp={current_ltp:.2f} tg={tg:.2f} defer_until={entry_candle + min_bars_for_pt_tg}{RESET}"
            )
        elif state.get("pt_deferred_logged", 0) == 0:
            logging.info(
                f"{YELLOW}[EXIT DEFERRED] PT hit before min bars ({bars_held} < {min_bars_for_pt_tg}). "
                f"ltp={current_ltp:.2f} pt={pt:.2f} defer_until={entry_candle + min_bars_for_pt_tg}{RESET}"
            )
            state["pt_deferred_logged"] = 1
        return False, None

    if tg_hit:
        audit("TG", "TARGET_HIT", f"ltp>={tg:.2f}")
        logging.info(
            f"{GREEN}[EXIT][TG_HIT] {side} ltp={current_ltp:.2f} tg={tg:.2f} bars_held={bars_held}{RESET}"
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

    if len(osc_hits) >= 2 and bars_held >= min_bars_for_pt_tg:
        if OSCILLATOR_EXIT_MODE == "HARD":
            x_type = contextual_exit_type()
            audit(x_type, "OSC_EXHAUSTION", f"osc_hits={'+'.join(osc_hits)}")
            logging.info(f"{YELLOW}[EXIT][OSC] {side} {'+'.join(osc_hits)} bars_held={bars_held}{RESET}")
            return True, "OSC_EXHAUSTION"
        if state.get("stop", 0) < entry_price:
            state["stop"] = entry_price

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
    if state["consec_count"] >= 3 and bars_held >= min_bars_for_pt_tg:
        x_type = contextual_exit_type()
        audit(x_type, "REVERSAL_EXIT", f"reversal_count={state['consec_count']}")
        logging.info(f"{YELLOW}[EXIT][REVERSAL] {side} {state['consec_count']} bars_held={bars_held}{RESET}")
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
        audit(x_type, "MOMENTUM_EXIT", "ema_plateau+momentum_drop")
        logging.info(f"{YELLOW}[EXIT][MOMENTUM] {side} plateau+drop+osc{RESET}")
        return True, "MOMENTUM_EXIT"

    if i - entry_candle >= time_exit_candles and state.get("trail_updates", 0) == 0:
        audit("ATR", "TIME_EXIT", f"no_trail_for_{time_exit_candles}_candles")
        logging.info(f"{YELLOW}[EXIT][TIME] {side} {i-entry_candle} candles no trail{RESET}")
        return True, "TIME_EXIT"

    return False, None


def build_dynamic_levels(entry_price, atr, side, entry_candle,
                         rr_ratio=2.0, profit_loss_point=5, candles_df=None,
                         trail_start_frac=0.5):
    """
    Build SL/PT/TG/trail for OPTIONS BUYING (long call or long put).
    
    ✅ v2.0 QUICK PROFIT BOOKING MODEL (3–5 bars target)
    ═════════════════════════════════════════════════════════════
    
    Design principles:
    - Tighter targets for quick profit booking vs long-hold strategies
    - Dynamic scaling based on ATR volatility regime
    - SL ≈ -8–11%, PT ≈ +10–13%, TG ≈ +15–20% (vs old 18%/25%/45%)
    - Trail step scales with volatility: 2–3% of entry
    
    Volatility Regimes (based on Nifty ATR):
    - Regime 1 (ATR ≤ 60):    Very Low   → SL=-8%  PT=+10% TG=+15%
    - Regime 2 (60< ATR≤100): Low        → SL=-9%  PT=+11% TG=+16%
    - Regime 3 (100<ATR≤150): Moderate  → SL=-10% PT=+12% TG=+18%
    - Regime 4 (150<ATR≤250): High      → SL=-11% PT=+13% TG=+20%
    - Regime 5 (ATR > 250):   Extreme   → Skip (too risky for quick booking)
    
    Example (Entry=300 in Regime 3):
    SL=270 (-10%), PT=336 (+12%), TG=354 (+18%), Trail=6 (2%)
    """
    if entry_price is None or entry_price <= 0:
        logging.warning(f"[LEVELS] Invalid entry_price={entry_price}")
        return {"valid": False}

    if atr is None or pd.isna(atr):
        logging.warning("[LEVELS] ATR unavailable")
        return {"valid": False}

    # ════════ VOLATILITY REGIME CLASSIFICATION ════════
    if atr <= 60:
        regime = "VERY_LOW"
        sl_pct   = 0.08   # 8% stop loss
        pt_pct   = 0.10   # 10% partial target
        tg_pct   = 0.15   # 15% full target
        step_pct = 0.02   # 2% trail step
    elif atr <= 100:
        regime = "LOW"
        sl_pct   = 0.09
        pt_pct   = 0.11
        tg_pct   = 0.16
        step_pct = 0.025
    elif atr <= 150:
        regime = "MODERATE"
        sl_pct   = 0.10
        pt_pct   = 0.12
        tg_pct   = 0.18
        step_pct = 0.03
    elif atr <= 250:
        regime = "HIGH"
        sl_pct   = 0.11
        pt_pct   = 0.13
        tg_pct   = 0.20
        step_pct = 0.035
    else:
        logging.warning(f"[LEVELS][EXTREME_ATR] {atr:.0f} — skipping trade (too volatile for quick booking)")
        return {"valid": False}

    # ════════ CALCULATE LEVELS ════════
    stop           = round(entry_price * (1 - sl_pct),  2)
    partial_target = round(entry_price * (1 + pt_pct),  2)
    full_target    = round(entry_price * (1 + tg_pct),  2)
    trail_start    = round(entry_price * pt_pct * float(trail_start_frac), 2)
    trail_step     = round(max(entry_price * step_pct, 1.5), 2)
    
    # ════════ AUDIT LOG WITH PERCENTAGES ════════
    logging.info(
        f"{CYAN}[LEVELS] {regime:12} | {side} entry={entry_price:.2f} | "
        f"SL={stop:.2f}({-sl_pct*100:5.1f}%) "
        f"PT={partial_target:.2f}({pt_pct*100:+5.1f}%) "
        f"TG={full_target:.2f}({tg_pct*100:+5.1f}%) "
        f"| TrailStart={trail_start:.2f} TrailStep={trail_step:.2f} "
        f"ATR={atr:.1f} trail_start_frac={trail_start_frac:.2f}{RESET}"
    )

    return {
        "valid": True,
        "stop": stop,
        "pt": partial_target,
        "tg": full_target,
        "trail_start": trail_start,
        "trail_step": trail_step,
        "regime": regime,
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

if account_type == 'PAPER':
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
            'last_exit_time': None,
            'scalp_cooldown_until': None,
            'scalp_last_burst_key': None,
            'scalp_hist': {'CALL': [], 'PUT': []},
        }
    paper_info = _hydrate_runtime_state(paper_info, account_type, "PAPER")

else:
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

    # --- Route exit order ---
    if account_type.lower() == "paper":
        success, order_id = send_paper_exit_order(symbol, qty, exit_reason)
    else:
        if mode == "LIVE":
            success, order_id = send_live_exit_order(symbol, qty, exit_reason)
        else:
            # REPLAY mode -> simulate success, no DB
            success, order_id = True, "REPLAY_ORDER"

    if success:
        # FIX: Use the option's actual traded price (from df), not spot candle close
        exit_price = current_option_price if current_option_price else current_candle["close"]
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
        # Strict lifecycle transition OPEN -> HOLD -> EXIT.
        state["is_open"] = False
        state["lifecycle_state"] = "EXIT"
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

    atr = pre_atr

    # 5A. Momentum scalp entry flow (premium burst capture with own cool-down)
    scalp_cd_until = paper_info.get("scalp_cooldown_until")
    if scalp_cd_until and ct < scalp_cd_until:
        logging.info(
            f"[SCALP SIGNAL IGNORED][COOLDOWN] now={ct} cooldown_until={scalp_cd_until}"
        )
    else:
        scalp_sig = _detect_scalp_momentum_signal(paper_info, spot_price, ct)
        if scalp_sig:
            scalp_side = scalp_sig["side"]
            scalp_leg = "call_buy" if scalp_side == "CALL" else "put_buy"
            burst_key = f"{scalp_side}:{last_candle_time}"
            if paper_info.get("scalp_last_burst_key") == burst_key:
                logging.info(f"[SCALP SKIP] duplicate burst {burst_key}")
            elif paper_info[scalp_leg].get("trade_flag", 0) == 0 and paper_info[scalp_leg].get("is_open", False) is False:
                if paper_info.get("trade_count", 0) < paper_info.get("max_trades", MAX_TRADES_PER_DAY):
                    opt_name = scalp_sig["symbol"]
                    entry_price = float(scalp_sig["price"])
                    stop = round(entry_price - SCALP_SL_POINTS, 2)
                    pt = round(entry_price + SCALP_PT_POINTS, 2)
                    position_id = f"scalp_{opt_name}_{int(ct.timestamp())}_{paper_info.get('trade_count', 0) + 1}"
                    paper_info[scalp_leg].update({
                        "option_name":     opt_name,
                        "quantity":        quantity,
                        "buy_price":       entry_price,
                        "order_type":      ORDER_TYPE,
                        "trade_flag":      1,
                        "pnl":             0,
                        "reason":          "MOMENTUM_SCALP",
                        "source":          "MOMENTUM_SCALP",
                        "order_id":        f"paper_scalp_{opt_name}_{ct}",
                        "position_id":     position_id,
                        "entry_time":      ct,
                        "entry_candle":    len(candles_3m) - 1,
                        "side":            scalp_side,
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
                        "scalp_sl_points": SCALP_SL_POINTS,
                        "partial_booked":  False,
                        "hf_exit_manager": OptionExitManager(
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
                    paper_info["trade_count"] = paper_info.get("trade_count", 0) + 1
                    paper_info["scalp_last_burst_key"] = burst_key
                    logging.info(
                        f"[SCALP ENTRY][PAPER] {scalp_side} {opt_name} @ {entry_price:.2f} "
                        f"PT=+{SCALP_PT_POINTS:.1f} SL=-{SCALP_SL_POINTS:.1f} "
                        f"reason={scalp_sig['reason']} position_id={position_id}"
                    )
                    logging.info(
                        "[ENTRY][NEW] "
                        f"timestamp={ct} symbol={opt_name} option_type={scalp_side} "
                        f"position_side={paper_info[scalp_leg].get('position_side', 'LONG')} "
                        f"position_id={position_id} "
                        f"lifecycle=OPEN regime=SCALP"
                    )
                    _save_trades_paper()
                    store(paper_info, account_type)
                    return

    # 6. Signal evaluation
    quality_ok, allowed_side, gate_reason, st_details = _trend_entry_quality_gate(
        candles_3m=candles_3m,
        candles_15m=hist_yesterday_15m if hist_yesterday_15m is not None else pd.DataFrame(),
        timestamp=ct,
        symbol=ticker,
        adx_min=float(TREND_ENTRY_ADX_MIN),
        cpr_levels=cpr_pre,
        camarilla_levels=cam_pre,
    )
    if not quality_ok:
        tag = "ST_CONFLICT" if "Supertrend conflict" in gate_reason else (
            "SLOPE_MISMATCH" if "Slope mismatch" in gate_reason else (
                "WEAK_ADX" if "Weak trend strength" in gate_reason else "OSC_EXTREME"
            )
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

    # ── Compression breakout entry ────────────────────────────────────────────
    if hist_yesterday_15m is not None and len(hist_yesterday_15m) >= 3:
        _compression_state.update(hist_yesterday_15m)

    if _compression_state.has_entry:
        comp_sig = _compression_state.entry_signal
        leg = "call_buy" if comp_sig["side"] == "CALL" else "put_buy"
        if paper_info[leg]["trade_flag"] == 0 and not risk_info.get("halt_trading", False):
            if paper_info.get("trade_count", 0) < paper_info.get("max_trades", MAX_TRADES_PER_DAY):
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
    )

    # 7. Entry
    if not signal:
        _save_trades_paper()
        store(paper_info, account_type)
        return

    side   = signal["side"]
    reason = signal["reason"]
    source = signal.get("source", "UNKNOWN")
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
            if paper_info.get("trade_count", 0) >= paper_info.get("max_trades", MAX_TRADES_PER_DAY):
                logging.info("[ENTRY SKIP] Max trades reached")
            else:
                opt_type = "CE" if side == "CALL" else "PE"
                opt_name, strike = get_option_by_moneyness(
                    spot_price, opt_type,
                    moneyness=CALL_MONEYNESS if side == "CALL" else PUT_MONEYNESS
                )

                if opt_name and opt_name in df.index:
                    ltp_val = df.loc[opt_name, "ltp"]
                    entry_price = float(ltp_val) if (ltp_val and not pd.isna(ltp_val)) else spot_price
                    if not entry_price or entry_price <= 0:
                        logging.warning(f"[ENTRY SKIP] invalid entry_price={entry_price}")
                        return

                    # FIX: pass candles_df so build_dynamic_levels can resolve entry candle
                    levels = build_dynamic_levels(
                        entry_price, atr, side,
                        entry_candle=len(candles_3m) - 1,
                        candles_df=candles_3m
                    )
                    if not levels.get("valid", False):
                        logging.warning(f"[ENTRY SKIP] {side} levels failed (ATR extreme?)")
                        return
                    stop = levels["stop"]
                    pt = levels["pt"]
                    tg = levels["tg"]
                    trail_start = levels["trail_start"]
                    trail_step = levels["trail_step"]

                    if atr is None or pd.isna(atr):
                        regime_context = "ATR_UNKNOWN"
                    elif atr <= 60:
                        regime_context = "VERY_LOW"
                    elif atr <= 100:
                        regime_context = "LOW"
                    elif atr <= 150:
                        regime_context = "MODERATE"
                    elif atr <= 250:
                        regime_context = "HIGH"
                    else:
                        regime_context = "EXTREME"

                    # FIX: all required exit-state keys initialised
                    position_id = f"{opt_name}_{int(ct.timestamp())}_{paper_info.get('trade_count', 0) + 1}"
                    paper_info[leg].update({
                        "option_name":    opt_name,
                        "quantity":       quantity,
                        "buy_price":      entry_price,
                        "order_type":     ORDER_TYPE,
                        "trade_flag":     1,
                        "pnl":            0,
                        "reason":         reason,
                        "source":         source,
                        "order_id":       f"paper_{opt_name}_{ct}",
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
                        "atr_value":      atr,
                        "regime_context": regime_context,
                        "is_open":        True,
                        "lifecycle_state": "OPEN",
                        "scalp_mode":     False,
                        "scalp_pt_points": SCALP_PT_POINTS,
                        "scalp_sl_points": SCALP_SL_POINTS,
                        "partial_booked": False,
                        "hf_exit_manager": OptionExitManager(
                            entry_price=entry_price,
                            side=side,
                            risk_buffer=1.0,
                        ),
                        "hf_deferred_logged": 0,
                    })

                    paper_info[leg]["filled_df"].loc[ct] = {
                        'ticker': opt_name,
                        'price': entry_price,
                        'action': side,
                        'stop_price': None,
                        'take_profit': None,
                        'spot_price': spot_price,
                        'quantity': quantity
                    }
                    paper_info["trade_count"] = paper_info.get("trade_count", 0) + 1

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
                    logging.warning(f"[ENTRY SKIP] no option for {side} strike={strike}")
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

    atr = pre_atr

    # 5A. Momentum scalp entry flow (premium burst capture with own cool-down)
    scalp_cd_until = live_info.get("scalp_cooldown_until")
    if scalp_cd_until and ct < scalp_cd_until:
        logging.info(
            f"[SCALP SIGNAL IGNORED][COOLDOWN] now={ct} cooldown_until={scalp_cd_until}"
        )
    else:
        scalp_sig = _detect_scalp_momentum_signal(live_info, spot_price, ct)
        if scalp_sig:
            scalp_side = scalp_sig["side"]
            scalp_leg = "call_buy" if scalp_side == "CALL" else "put_buy"
            burst_key = f"{scalp_side}:{last_candle_time}"
            if live_info.get("scalp_last_burst_key") == burst_key:
                logging.info(f"[SCALP SKIP] duplicate burst {burst_key}")
            elif live_info[scalp_leg].get("trade_flag", 0) == 0 and live_info[scalp_leg].get("is_open", False) is False:
                if live_info.get("trade_count", 0) < live_info.get("max_trades", MAX_TRADES_PER_DAY):
                    opt_name = scalp_sig["symbol"]
                    entry_price = float(scalp_sig["price"])
                    stop = round(entry_price - SCALP_SL_POINTS, 2)
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
                            "reason":          "MOMENTUM_SCALP",
                            "source":          "MOMENTUM_SCALP",
                            "order_id":        order_id,
                            "position_id":     position_id,
                            "entry_time":      ct,
                            "entry_candle":    len(candles_3m) - 1,
                            "side":            scalp_side,
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
                            "scalp_sl_points": SCALP_SL_POINTS,
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
                        live_info["trade_count"] = live_info.get("trade_count", 0) + 1
                        live_info["scalp_last_burst_key"] = burst_key
                        logging.info(
                            f"[SCALP ENTRY][LIVE] {scalp_side} {opt_name} @ {entry_price:.2f} "
                            f"PT=+{SCALP_PT_POINTS:.1f} SL=-{SCALP_SL_POINTS:.1f} "
                            f"reason={scalp_sig['reason']} position_id={position_id}"
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

    # 6. Signal evaluation
    quality_ok, allowed_side, gate_reason, st_details = _trend_entry_quality_gate(
        candles_3m=candles_3m,
        candles_15m=hist_yesterday_15m if hist_yesterday_15m is not None else pd.DataFrame(),
        timestamp=ct,
        symbol=ticker,
        adx_min=float(TREND_ENTRY_ADX_MIN),
        cpr_levels=cpr_pre,
        camarilla_levels=cam_pre,
    )
    if not quality_ok:
        tag = "ST_CONFLICT" if "Supertrend conflict" in gate_reason else (
            "SLOPE_MISMATCH" if "Slope mismatch" in gate_reason else (
                "WEAK_ADX" if "Weak trend strength" in gate_reason else "OSC_EXTREME"
            )
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

    # ── Compression breakout entry ────────────────────────────────────────────
    if hist_yesterday_15m is not None and len(hist_yesterday_15m) >= 3:
        _compression_state.update(hist_yesterday_15m)

    if _compression_state.has_entry:
        comp_sig = _compression_state.entry_signal
        leg = "call_buy" if comp_sig["side"] == "CALL" else "put_buy"
        if live_info[leg]["trade_flag"] == 0 and not risk_info.get("halt_trading", False):
            if live_info.get("trade_count", 0) < live_info.get("max_trades", MAX_TRADES_PER_DAY):
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
    )

    # 7. Entry
    if not signal:
        _save_trades_live()
        store(live_info, account_type)
        return

    side   = signal["side"]
    reason = signal["reason"]
    source = signal.get("source", "UNKNOWN")
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

    if hist_yesterday_15m is not None and not hist_yesterday_15m.empty:
        bias15 = hist_yesterday_15m.iloc[-1].get("supertrend_bias", "NEUTRAL")
        logging.info(f"[BIAS][15m] {bias15}")

    if risk_info.get("halt_trading", False):
        return

    leg = "call_buy" if side == "CALL" else "put_buy"
    try:
        if live_info[leg]["trade_flag"] == 0:
            if live_info.get("trade_count", 0) >= live_info.get("max_trades", MAX_TRADES_PER_DAY):
                logging.info("[ENTRY SKIP] Max trades reached")
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
                levels = build_dynamic_levels(
                    entry_price, atr, side,
                    entry_candle=len(candles_3m) - 1,
                    candles_df=candles_3m
                )
                if not levels.get("valid", False):
                    logging.warning(f"[ENTRY SKIP] {side} levels failed")
                    return
                stop = levels["stop"]
                pt = levels["pt"]
                tg = levels["tg"]
                trail_start = levels["trail_start"]
                trail_step = levels["trail_step"]

                if atr is None or pd.isna(atr):
                    regime_context = "ATR_UNKNOWN"
                elif atr <= 60:
                    regime_context = "VERY_LOW"
                elif atr <= 100:
                    regime_context = "LOW"
                elif atr <= 150:
                    regime_context = "MODERATE"
                elif atr <= 250:
                    regime_context = "HIGH"
                else:
                    regime_context = "EXTREME"

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
                    "atr_value":      atr,
                    "regime_context": regime_context,
                    "is_open":        True,
                    "lifecycle_state": "OPEN",
                    "scalp_mode":     False,
                    "scalp_pt_points": SCALP_PT_POINTS,
                    "scalp_sl_points": SCALP_SL_POINTS,
                    "partial_booked": False,
                    "hf_exit_manager": OptionExitManager(
                        entry_price=entry_price,
                        side=side,
                        risk_buffer=1.0,
                    ),
                    "hf_deferred_logged": 0,
                })

                live_info[leg]["filled_df"].loc[ct] = {
                    'ticker': opt_name,
                    'price': entry_price,
                    'action': side,
                    'stop_price': None,
                    'take_profit': None,
                    'spot_price': spot_price,
                    'quantity': quantity
                }
                live_info["trade_count"] = live_info.get("trade_count", 0) + 1

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
                logging.warning(f"[ENTRY SKIP] {side} no option. opt_name={opt_name}")
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

            quality_ok, allowed_side, gate_reason, st_details = _trend_entry_quality_gate(
                candles_3m=slice_3m,
                candles_15m=slice_15m,
                timestamp=bar_time,
                symbol=sym,
                adx_min=float(TREND_ENTRY_ADX_MIN),
                cpr_levels=cpr,
                camarilla_levels=cam,
            )
            if not quality_ok:
                blocker_key = (
                    "ST_CONFLICT"
                    if "Supertrend conflict" in gate_reason
                    else (
                        "SLOPE_MISMATCH"
                        if "Slope mismatch" in gate_reason
                        else ("WEAK_ADX" if "Weak trend strength" in gate_reason else "OSC_EXTREME")
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
                )
            except Exception as e:
                logging.debug(f"[REPLAY bar={i}] detect_signal error: {e}")
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
                blocker_counts["ST_SIDE_MISMATCH"] = blocker_counts.get("ST_SIDE_MISMATCH", 0) + 1
                logging.info(
                    "[SIGNAL BLOCKED] "
                    f"reason=Supertrend conflict, entry suppressed. "
                    f"timestamp={bar_time} symbol={sym} "
                    f"ST3m_bias={st_details['ST3m_bias']} ST15m_bias={st_details['ST15m_bias']} "
                    f"allowed_side={allowed_side} signal_side={side}"
                )
                continue

            # Enrich signal with current ST and day type for PM entry tracking
            signal["st_bias"]  = str(last_row.get("supertrend_bias", "?"))
            signal["pivot"]    = signal.get("pivot", "")
            signal["day_type"] = _day_type.name.value

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
