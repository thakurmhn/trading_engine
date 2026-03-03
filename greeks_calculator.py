"""greeks_calculator.py — Black-Scholes-Merton Greeks and implied volatility.

Uses py_vollib (BSM model with continuous dividend yield) to compute
Delta, Gamma, Theta, and Vega for NSE option contracts, and back-solves
implied volatility (IV) from the current market premium.

py_vollib conventions (used throughout this module)
----------------------------------------------------
  theta  — change in option value per calendar day (negative for long positions)
  vega   — change in option value per 1% change in implied volatility
  delta  — change in option value per 1-point move in the underlying
  gamma  — change in delta per 1-point move in the underlying

Log Tags
--------
  [GREEKS] symbol=... delta=... gamma=... theta=... vega=... iv=...
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# ── Model constants ───────────────────────────────────────────────────────────

# Annualised risk-free rate (approx. 91-day T-bill yield for India)
_RISK_FREE_RATE = 0.065   # 6.5 %

# Continuous dividend yield for NSE benchmark indices
_DIVIDEND_YIELD = 0.015   # 1.5 %

# Thresholds for risk classification (entry_logic.py uses these)
THETA_PENALTY_THRESHOLD = 5.0    # |theta| pts/day > 5 → theta penalty applies
VEGA_HIGH_THRESHOLD     = 15.0   # vega pts per 1 % vol > 15 → high vega risk


# ── Greeks result dataclass ───────────────────────────────────────────────────

@dataclass
class GreeksResult:
    """Black-Scholes-Merton Greeks for a single option contract."""

    symbol:      str
    delta:       float    # Δ — per 1-pt move in underlying
    gamma:       float    # Γ — change in delta per 1-pt move
    theta:       float    # Θ — change in value per calendar day (negative = decay)
    vega:        float    # ν — change in value per 1 % vol move
    iv:          float    # implied volatility (decimal, e.g. 0.18)
    iv_pct:      float    # implied volatility as percent (e.g. 18.0)
    option_type: str      # "CE" or "PE"


# ── Public API ────────────────────────────────────────────────────────────────

def get_greeks(
    symbol:       str,
    spot:         float,
    strike:       float,
    expiry_days:  int,
    option_type:  str,
    option_price: float,
) -> Optional[GreeksResult]:
    """Compute Delta, Gamma, Theta, Vega and implied volatility via BSM.

    Parameters
    ----------
    symbol       : Fyers symbol string, e.g. ``"NSE:NIFTY25MAR24500CE"``
    spot         : Current underlying spot price
    strike       : Option strike price
    expiry_days  : Calendar days until expiry (minimum 1)
    option_type  : ``"CE"`` / ``"CALL"`` or ``"PE"`` / ``"PUT"``
    option_price : Current option market price (LTP / premium)

    Returns
    -------
    ``GreeksResult`` dataclass on success, ``None`` if py_vollib is missing
    or if the IV solver fails (e.g. deeply OTM / expired contract).
    """
    flag = _norm_flag(option_type)
    if flag not in ("c", "p"):
        logger.warning(
            f"[GREEKS] Unsupported option_type={option_type!r} for {symbol}"
        )
        return None

    expiry_days = max(expiry_days, 1)   # guard against zero/negative
    t = expiry_days / 365.0

    try:
        from py_vollib.black_scholes_merton.greeks.analytical import (
            delta, gamma, theta, vega,
        )
        from py_vollib.black_scholes_merton.implied_volatility import (
            implied_volatility as _iv_calc,
        )
    except ImportError:
        logger.warning(
            "[GREEKS] py_vollib not installed — Greeks unavailable. "
            "Install with: pip install py_vollib"
        )
        return None

    # ── 1. Back-solve implied volatility ──────────────────────────────────────
    try:
        iv = _iv_calc(
            option_price,
            spot, strike, t,
            _RISK_FREE_RATE, _DIVIDEND_YIELD,
            flag,
        )
        if iv is None or iv != iv:   # NaN guard
            raise ValueError("IV solver returned NaN / None")
    except Exception as exc:
        logger.debug(
            f"[GREEKS] IV solve failed for {symbol}: {exc} | "
            f"spot={spot} strike={strike} t={t:.4f} price={option_price}"
        )
        return None

    # ── 2. Compute Greeks at solved IV ────────────────────────────────────────
    try:
        d  = delta(flag, spot, strike, t, _RISK_FREE_RATE, iv, _DIVIDEND_YIELD)
        g  = gamma(flag, spot, strike, t, _RISK_FREE_RATE, iv, _DIVIDEND_YIELD)
        th = theta(flag, spot, strike, t, _RISK_FREE_RATE, iv, _DIVIDEND_YIELD)
        v  = vega( flag, spot, strike, t, _RISK_FREE_RATE, iv, _DIVIDEND_YIELD)
    except Exception as exc:
        logger.debug(f"[GREEKS] Greeks computation failed for {symbol}: {exc}")
        return None

    # py_vollib theta: already per calendar day (negative for long options)
    # py_vollib vega:  already per 1 % change in implied volatility
    result = GreeksResult(
        symbol=symbol,
        delta=round(d,  4),
        gamma=round(g,  6),
        theta=round(th, 4),
        vega=round(v,   4),
        iv=round(iv,    4),
        iv_pct=round(iv * 100, 2),
        option_type=flag,
    )

    logger.info(
        f"[GREEKS] symbol={symbol} "
        f"delta={result.delta:.4f} "
        f"gamma={result.gamma:.6f} "
        f"theta={result.theta:.4f} "
        f"vega={result.vega:.4f} "
        f"iv={result.iv_pct:.2f}%"
    )
    return result


def is_high_theta(greeks: Optional[GreeksResult]) -> bool:
    """Return True when daily theta decay exceeds the penalty threshold."""
    return (
        greeks is not None
        and abs(greeks.theta) > THETA_PENALTY_THRESHOLD
    )


def is_high_vega(greeks: Optional[GreeksResult]) -> bool:
    """Return True when vega risk exceeds the high-vega threshold.

    Threshold is expressed in index points per 1 % vol move.
    Typical NIFTY ATM 1-week option: vega ≈ 10–15 pts / 1 % move.
    Typical NIFTY ATM 1-month option: vega ≈ 25–40 pts / 1 % move.
    """
    return (
        greeks is not None
        and abs(greeks.vega) > VEGA_HIGH_THRESHOLD
    )


# ── Private helpers ───────────────────────────────────────────────────────────

def _norm_flag(raw: str) -> str:
    """Normalise option type to py_vollib single-char flag ('c' or 'p')."""
    r = raw.upper().strip()
    if r in ("CE", "CALL", "C"):
        return "c"
    if r in ("PE", "PUT", "P"):
        return "p"
    return r.lower()
