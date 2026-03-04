"""contract_metadata.py — Fyers instruments API integration for option contract metadata.

Fetches lot sizes, expiry dates, and strike information for option contracts
from the Fyers `/data/instruments` API.

Supports NIFTY today; structured for future BANKNIFTY, FINNIFTY expansion.

Log tags
--------
[CONTRACT_METADATA] symbol=... lot=... expiry=...
[CONTRACT_FILTER]   symbol=... strike=... intrinsic=...  (zero-intrinsic skip)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Supported symbols — extend here for BANKNIFTY, FINNIFTY, etc. ──────────────
SUPPORTED_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"}

# Fyers exchange segment for NSE F&O
_FYERS_SEGMENT_NSE_FO = "NSE_FO"


# ── Contract data record ─────────────────────────────────────────────────────────

@dataclass
class ContractInfo:
    """Metadata for a single option contract."""

    symbol:       str    # Fyers full symbol, e.g. "NSE:NIFTY25MAR24500CE"
    underlying:   str    # e.g. "NIFTY"
    lot_size:     int
    expiry:       date
    strike_price: float
    option_type:  str    # "CE" or "PE"


# ── In-memory cache ──────────────────────────────────────────────────────────────

class ContractMetadataCache:
    """In-memory cache of Fyers instrument metadata for option contracts.

    Call ``refresh(fyers_client, underlying)`` at startup and whenever the
    underlying changes to reload data from the Fyers API.

    Multi-symbol support: the cache stores a separate list per underlying
    (NIFTY, BANKNIFTY, FINNIFTY, …).
    """

    def __init__(self) -> None:
        # underlying → list of ContractInfo (all strikes / all expiries)
        self._cache: Dict[str, List[ContractInfo]] = {}

    # ── public ──────────────────────────────────────────────────────────────────

    def refresh(self, fyers_client, underlying: str) -> int:
        """Fetch and cache all option contracts for ``underlying``.

        Parameters
        ----------
        fyers_client : Authenticated fyers_apiv3 FyersModel instance.
        underlying   : e.g. ``"NIFTY"``.

        Returns
        -------
        Number of contracts loaded into cache.
        """
        underlying = underlying.upper()
        contracts  = _fetch_instruments(fyers_client, underlying)
        self._cache[underlying] = contracts

        if contracts:
            lots     = sorted(set(c.lot_size  for c in contracts))
            expiries = sorted(set(c.expiry    for c in contracts))
            lot_repr = lots[0] if len(lots) == 1 else lots
            logger.info(
                f"[CONTRACT_METADATA] symbol={underlying} "
                f"lot={lot_repr} "
                f"expiry={expiries[0] if expiries else 'N/A'} "
                f"contracts_loaded={len(contracts)}"
            )

        return len(contracts)

    def get_lot_size(self, underlying: str) -> Optional[int]:
        """Return the lot size for ``underlying``, or None if cache is empty."""
        contracts = self._cache.get(underlying.upper(), [])
        return contracts[0].lot_size if contracts else None

    def get_expiry(
        self,
        underlying: str,
        reference_date: Optional[date] = None,
    ) -> Optional[date]:
        """Return the nearest upcoming expiry on or after ``reference_date``.

        Parameters
        ----------
        reference_date : Defaults to today.
        """
        contracts = self._cache.get(underlying.upper(), [])
        if not contracts:
            return None
        ref = reference_date or date.today()
        future = sorted(set(c.expiry for c in contracts if c.expiry >= ref))
        return future[0] if future else None

    def get_all_expiries(self, underlying: str) -> List[date]:
        """Return all known expiries (sorted ascending) for ``underlying``."""
        contracts = self._cache.get(underlying.upper(), [])
        return sorted(set(c.expiry for c in contracts))

    def get_contracts(
        self,
        underlying: str,
        expiry: Optional[date] = None,
        option_type: Optional[str] = None,
    ) -> List[ContractInfo]:
        """Return contracts, optionally filtered by expiry and/or option_type."""
        contracts = list(self._cache.get(underlying.upper(), []))
        if expiry is not None:
            contracts = [c for c in contracts if c.expiry == expiry]
        if option_type is not None:
            ot = option_type.upper()
            contracts = [c for c in contracts if c.option_type == ot]
        return contracts

    def filter_intrinsic(
        self,
        underlying: str,
        spot_price: float,
        option_type: str,
        expiry: Optional[date] = None,
    ) -> List[ContractInfo]:
        """Return contracts where intrinsic value > 0 (ATM / ITM).

        Intrinsic value:
          CALL (CE): max(spot − strike, 0)
          PUT  (PE): max(strike − spot, 0)

        Zero-intrinsic contracts are logged at DEBUG with [CONTRACT_FILTER].

        Parameters
        ----------
        underlying   : e.g. "NIFTY"
        spot_price   : Current underlying spot price.
        option_type  : "CE" / "CALL" or "PE" / "PUT".
        expiry       : Filter to a specific expiry; None = use active expiry contracts.
        """
        ot        = _normalise_option_type(option_type)
        contracts = self.get_contracts(underlying, expiry=expiry, option_type=ot)
        result: List[ContractInfo] = []
        skipped = 0

        for c in contracts:
            intrinsic = _calc_intrinsic(spot_price, c.strike_price, ot)
            if intrinsic > 0:
                result.append(c)
            else:
                skipped += 1
                logger.debug(
                    f"[CONTRACT_FILTER] symbol={c.symbol} "
                    f"strike={c.strike_price} "
                    f"intrinsic={intrinsic:.2f} → SKIPPED (zero intrinsic)"
                )

        logger.debug(
            f"[CONTRACT_FILTER] underlying={underlying} ot={ot} spot={spot_price:.2f} "
            f"expiry={expiry} total_checked={len(contracts)} "
            f"passed={len(result)} skipped={skipped}"
        )
        return result

    def validate_lot_size(
        self,
        underlying: str,
        manual_lot_size: Optional[int] = None,
    ) -> bool:
        """Return True when API lot size matches the configured default lot size.

        Compares the API-fetched lot size for ``underlying`` against
        ``config.DEFAULT_LOT_SIZE``.  When ``manual_lot_size`` is also provided
        it is checked separately as an additional sanity gate.

        Logs a WARNING on any mismatch — callers should treat this as a
        ``lot_size_mismatch`` event for dashboard tracking.
        """
        # Resolve config default (import deferred to avoid circular-import risk
        # at module load time when config.py sets up logging).
        try:
            import config as _cfg
            config_lot = _cfg.DEFAULT_LOT_SIZE
        except Exception:
            config_lot = None

        api_lot = self.get_lot_size(underlying)
        if api_lot is None:
            logger.warning(
                f"[CONTRACT_METADATA] lot validation skipped: "
                f"no data cached for {underlying}"
            )
            return True   # cannot validate — assume OK

        ok = True

        # Compare against config default
        if config_lot is not None and api_lot != config_lot:
            logger.warning(
                f"[CONTRACT_METADATA][LOT_MISMATCH] symbol={underlying} "
                f"api_lot={api_lot} config_default={config_lot}"
            )
            ok = False

        # Additional comparison against manually supplied value (if provided)
        if manual_lot_size is not None and api_lot != manual_lot_size:
            logger.warning(
                f"[CONTRACT_METADATA][LOT_MISMATCH] symbol={underlying} "
                f"api_lot={api_lot} manual_lot={manual_lot_size}"
            )
            ok = False

        return ok


# ── Private helpers ──────────────────────────────────────────────────────────────

def _normalise_option_type(raw: str) -> str:
    """Normalise option type string to "CE" or "PE"."""
    r = raw.upper().strip()
    if r in ("CALL", "CE", "C"):
        return "CE"
    if r in ("PUT", "PE", "P"):
        return "PE"
    return r


def _calc_intrinsic(spot: float, strike: float, ot: str) -> float:
    if ot == "CE":
        return max(spot - strike, 0.0)
    return max(strike - spot, 0.0)


def _fetch_instruments(fyers_client, underlying: str) -> List[ContractInfo]:
    """Call Fyers /data/instruments and parse option contracts for ``underlying``."""
    try:
        data = fyers_client.get_instruments(segment=_FYERS_SEGMENT_NSE_FO)
    except AttributeError:
        # Fallback: some SDK versions expose instruments differently
        try:
            data = fyers_client.instruments(segment=_FYERS_SEGMENT_NSE_FO)
        except Exception as exc:
            logger.error(
                f"[CONTRACT_METADATA] Fyers instruments API error (fallback): {exc}"
            )
            return []
    except Exception as exc:
        logger.error(f"[CONTRACT_METADATA] Fyers instruments API error: {exc}")
        return []

    # Fyers returns a list or a dict with a "data" key depending on SDK version
    raw_list = data if isinstance(data, list) else data.get("data", [])
    contracts: List[ContractInfo] = []

    for row in raw_list:
        sym = str(row.get("symbol", ""))
        # Filter to the requested underlying
        if underlying.upper() not in sym.upper():
            continue

        ot = str(row.get("option_type", "")).upper()
        if ot not in ("CE", "PE"):
            continue   # skip futures rows

        try:
            lot_size = int(
                row.get("lotsize") or row.get("lot_size") or 0
            )
            strike = float(
                row.get("strike") or row.get("strike_price") or 0
            )
            expiry_raw  = row.get("expiry") or row.get("expiry_date") or ""
            expiry_date = _parse_expiry(expiry_raw)
        except (ValueError, TypeError):
            continue

        if lot_size <= 0 or strike <= 0 or expiry_date is None:
            continue

        contracts.append(
            ContractInfo(
                symbol=sym,
                underlying=underlying.upper(),
                lot_size=lot_size,
                expiry=expiry_date,
                strike_price=strike,
                option_type=ot,
            )
        )

    return contracts


def _parse_expiry(raw) -> Optional[date]:
    """Parse expiry date from various formats returned by the Fyers API."""
    if raw is None:
        return None
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, (int, float)):
        try:
            return datetime.utcfromtimestamp(int(raw)).date()
        except (OSError, OverflowError, ValueError):
            return None
    s = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d/%m/%Y", "%Y%m%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


# ── Module-level singleton ────────────────────────────────────────────────────────

_metadata_cache = ContractMetadataCache()


def get_lot_size(underlying: str) -> Optional[int]:
    """Module-level convenience: lot size from the global cache."""
    return _metadata_cache.get_lot_size(underlying)


def get_expiry(
    underlying: str,
    reference_date: Optional[date] = None,
) -> Optional[date]:
    """Module-level convenience: nearest upcoming expiry from the global cache."""
    return _metadata_cache.get_expiry(underlying, reference_date)


def get_metadata_cache() -> ContractMetadataCache:
    """Return the module-level singleton cache (for refresh / advanced queries)."""
    return _metadata_cache
