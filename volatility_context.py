"""volatility_context.py — India VIX ingestion and volatility regime classification.

Fetches India VIX from Fyers using NSE:INDIAVIX-INDEX and classifies the
market into regime tiers consumed by entry/exit logic.

VIX Tiers
---------
  VIX < 15         → CALM    (low vol; tighten stops, stricter oscillator gating)
  15 ≤ VIX < 20   → NEUTRAL  (normal conditions)
  VIX ≥ 20         → HIGH    (high vol; widen ATR stops, relax oscillator gating)

Log Tags
--------
  [VIX_CONTEXT] symbol=NSE:INDIAVIX-INDEX value=... tier=...
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Fyers symbol for India VIX
VIX_SYMBOL = "NSE:INDIAVIX-INDEX"

# Tier thresholds
VIX_CALM_MAX    = 15.0
VIX_NEUTRAL_MAX = 20.0


# ── VIX context data record ───────────────────────────────────────────────────

@dataclass
class VIXContext:
    """Holds current VIX reading and derived volatility tier."""

    value: float
    tier:  str    # "CALM" | "NEUTRAL" | "HIGH"


def _classify_vix_tier(vix_value: float) -> str:
    """Classify a VIX value into a regime tier string."""
    if vix_value < VIX_CALM_MAX:
        return "CALM"
    if vix_value < VIX_NEUTRAL_MAX:
        return "NEUTRAL"
    return "HIGH"


# ── Core class ────────────────────────────────────────────────────────────────

class VolatilityContext:
    """Fetches and caches India VIX from the Fyers API.

    Call ``refresh(fyers_client)`` at session start (09:15–09:30) and
    periodically (every 15–30 minutes) to keep the tier current.
    All callers consume ``get_vix_tier()`` — a simple string gate.
    """

    def __init__(self) -> None:
        self._ctx: Optional[VIXContext] = None

    # ── public ────────────────────────────────────────────────────────────────

    def refresh(self, fyers_client) -> Optional[VIXContext]:
        """Fetch the latest India VIX and update the cached context.

        Parameters
        ----------
        fyers_client : Authenticated fyers_apiv3 FyersModel instance.

        Returns
        -------
        VIXContext if fetch succeeded, previous VIXContext if failed,
        or None if no value has ever been fetched.
        """
        vix_value = _fetch_vix(fyers_client)
        if vix_value is None:
            logger.warning(
                f"[VIX_CONTEXT] Failed to fetch VIX from {VIX_SYMBOL}; "
                "previous tier retained"
            )
            return self._ctx

        tier      = _classify_vix_tier(vix_value)
        self._ctx = VIXContext(value=vix_value, tier=tier)
        logger.info(
            f"[VIX_CONTEXT] symbol={VIX_SYMBOL} value={vix_value:.2f} tier={tier}"
        )
        return self._ctx

    def get_vix_tier(self) -> Optional[str]:
        """Return the current VIX tier string, or None if not yet fetched."""
        return self._ctx.tier if self._ctx else None

    def get_vix_value(self) -> Optional[float]:
        """Return the raw VIX value, or None if not yet fetched."""
        return self._ctx.value if self._ctx else None

    def get_context(self) -> Optional[VIXContext]:
        """Return the full VIXContext dataclass, or None."""
        return self._ctx


# ── Private helpers ───────────────────────────────────────────────────────────

def _fetch_vix(fyers_client) -> Optional[float]:
    """Call the Fyers quotes API and extract the last traded price for INDIAVIX."""
    try:
        resp = fyers_client.quotes(data={"symbols": VIX_SYMBOL})
    except AttributeError:
        # Some SDK versions expose get_quotes instead
        try:
            resp = fyers_client.get_quotes(symbols=VIX_SYMBOL)
        except Exception as exc:
            logger.error(
                f"[VIX_CONTEXT] Fyers quotes API error (fallback): {exc}"
            )
            return None
    except Exception as exc:
        logger.error(f"[VIX_CONTEXT] Fyers quotes API error: {exc}")
        return None

    # Fyers response format:
    # {"s": "ok", "d": [{"n": "NSE:INDIAVIX-INDEX", "s": "ok",
    #                     "v": {"lp": 13.5, "c": 13.5, ...}}]}
    try:
        data = resp.get("d", [])
        if data:
            v  = data[0].get("v", {})
            lp = v.get("lp") or v.get("last_price") or v.get("c")
            if lp is not None:
                return float(lp)
        logger.warning(
            f"[VIX_CONTEXT] Unexpected response structure from Fyers: {resp}"
        )
    except Exception as exc:
        logger.error(
            f"[VIX_CONTEXT] Failed to parse VIX response: {exc} | resp={resp}"
        )

    return None


# ── Module-level singleton ────────────────────────────────────────────────────

_volatility_context = VolatilityContext()


def refresh_vix(fyers_client) -> Optional[VIXContext]:
    """Module-level convenience: refresh VIX on the global singleton."""
    return _volatility_context.refresh(fyers_client)


def get_vix_tier() -> Optional[str]:
    """Module-level convenience: current VIX tier from the global singleton."""
    return _volatility_context.get_vix_tier()


def get_vix_value() -> Optional[float]:
    """Module-level convenience: current raw VIX value from the global singleton."""
    return _volatility_context.get_vix_value()


def get_volatility_context() -> VolatilityContext:
    """Return the module-level singleton (for refresh / advanced queries)."""
    return _volatility_context
