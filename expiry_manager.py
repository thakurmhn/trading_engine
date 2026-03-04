"""expiry_manager.py — Expiry lifecycle manager for option contracts.

Manages expiry selection, automatic roll-over, and intrinsic value filtering
for the active trading symbol (currently NIFTY).

Structured for future multi-symbol support (BANKNIFTY, FINNIFTY, etc.).

Log tags
--------
[CONTRACT_ROLL]   symbol=... old_expiry=... new_expiry=...
[CONTRACT_FILTER] symbol=... strike=... intrinsic=...  (emitted by cache)
[EXPIRY_ROLL][SCORE_BONUS]  — emitted by entry_logic when roll applied
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional

from contract_metadata import ContractInfo, ContractMetadataCache, get_metadata_cache

logger = logging.getLogger(__name__)


# ── Per-symbol expiry tracking ────────────────────────────────────────────────────

@dataclass
class SymbolExpiryState:
    """Tracks the active / next expiry for a single underlying."""

    underlying:    str
    active_expiry: Optional[date] = None
    next_expiry:   Optional[date] = None
    roll_count:    int = 0          # rolls executed this session


# ── ExpiryManager ────────────────────────────────────────────────────────────────

class ExpiryManager:
    """Manages expiry selection and automatic roll-over for one or more symbols.

    Currently targets NIFTY; designed for multi-symbol expansion.

    Typical usage
    -------------
    ::

        mgr = ExpiryManager(["NIFTY"])
        mgr.refresh(fyers_client)           # once at session start

        # On each bar (or each day-open check):
        mgr.check_roll(date.today())

        lot_size = mgr.get_lot_size("NIFTY")
        expiry   = mgr.get_active_expiry("NIFTY")

        # When building a trade signal, get valid contracts (intrinsic > 0):
        contracts = mgr.get_valid_contracts("NIFTY", spot_price=24500.0, option_type="CE")
    """

    def __init__(self, symbols: Optional[List[str]] = None) -> None:
        self._symbols: List[str] = [s.upper() for s in (symbols or ["NIFTY"])]
        self._states: Dict[str, SymbolExpiryState] = {
            s: SymbolExpiryState(underlying=s) for s in self._symbols
        }
        self._cache: ContractMetadataCache = get_metadata_cache()

    # ── session initialisation ───────────────────────────────────────────────────

    def refresh(self, fyers_client) -> None:
        """Load/refresh metadata from Fyers API and set active expiries.

        Call once at session start (or after a forced cache invalidation).
        """
        for sym in self._symbols:
            n_loaded = self._cache.refresh(fyers_client, sym)
            self._init_expiry(sym)
            logger.debug(
                f"[EXPIRY_MANAGER] symbol={sym} loaded={n_loaded} "
                f"active_expiry={self._states[sym].active_expiry}"
            )

    def _init_expiry(self, sym: str) -> None:
        """Set active_expiry = nearest upcoming (or today's) expiry."""
        today  = date.today()
        expiry = self._cache.get_expiry(sym, reference_date=today)
        state  = self._states[sym]
        state.active_expiry = expiry
        state.next_expiry   = self._next_expiry_after(sym, expiry)

    def _next_expiry_after(self, sym: str, current: Optional[date]) -> Optional[date]:
        if current is None:
            return None
        return self._cache.get_expiry(sym, reference_date=current + timedelta(days=1))

    # ── per-bar roll check ───────────────────────────────────────────────────────

    def check_roll(
        self,
        today: Optional[date] = None,
        symbol: Optional[str] = None,
    ) -> bool:
        """Check whether a roll is needed and execute it.

        Parameters
        ----------
        today  : Current trading date; defaults to ``date.today()``.
        symbol : If None, checks all managed symbols.

        Returns
        -------
        True if at least one symbol was rolled.
        """
        ref     = today or date.today()
        targets = [symbol.upper()] if symbol else self._symbols
        rolled  = False
        for sym in targets:
            if self._should_roll(sym, ref):
                self._execute_roll(sym, ref)
                rolled = True
        return rolled

    def _should_roll(self, sym: str, today: date) -> bool:
        state = self._states.get(sym)
        if state is None or state.active_expiry is None:
            return False
        return today >= state.active_expiry

    def _execute_roll(self, sym: str, today: date) -> None:
        state      = self._states[sym]
        old_expiry = state.active_expiry
        new_expiry = state.next_expiry or self._cache.get_expiry(
            sym, reference_date=today + timedelta(days=1)
        )

        logger.info(
            f"[CONTRACT_ROLL] symbol={sym} "
            f"old_expiry={old_expiry} "
            f"new_expiry={new_expiry}"
        )

        state.active_expiry = new_expiry
        state.roll_count   += 1
        # Pre-compute the one after new so the next check is instant
        state.next_expiry   = self._next_expiry_after(sym, new_expiry)

    # ── accessors ────────────────────────────────────────────────────────────────

    def get_lot_size(self, symbol: str) -> Optional[int]:
        """Return the cached lot size for ``symbol``."""
        return self._cache.get_lot_size(symbol.upper())

    def get_active_expiry(self, symbol: str) -> Optional[date]:
        """Return the active (current) expiry for ``symbol``."""
        state = self._states.get(symbol.upper())
        return state.active_expiry if state else None

    def is_expiry_day(self, symbol: str, today: Optional[date] = None) -> bool:
        """Return True when today == active expiry (roll trigger day)."""
        state = self._states.get(symbol.upper())
        if state is None or state.active_expiry is None:
            return False
        return (today or date.today()) >= state.active_expiry

    def get_roll_count(self, symbol: str) -> int:
        """Return number of rolls executed this session for ``symbol``."""
        state = self._states.get(symbol.upper())
        return state.roll_count if state else 0

    def get_total_roll_count(self) -> int:
        """Total rolls across all managed symbols this session."""
        return sum(s.roll_count for s in self._states.values())

    def validate_lot_size(self, symbol: str, manual_lot_size: int) -> bool:
        """Check whether ``manual_lot_size`` matches the API lot size.

        Returns True on match (or if cache has no data yet).
        A mismatch triggers a WARNING log; callers should increment
        ``lot_size_mismatch_count`` in the session summary.
        """
        return self._cache.validate_lot_size(symbol.upper(), manual_lot_size)

    # ── contract selection ───────────────────────────────────────────────────────

    def get_valid_contracts(
        self,
        symbol: str,
        spot_price: float,
        option_type: str,           # "CE" / "CALL" or "PE" / "PUT"
    ) -> List[ContractInfo]:
        """Return ITM/ATM contracts for ``symbol`` at the active expiry.

        Filters to intrinsic > 0 only; zero-intrinsic contracts are logged
        via [CONTRACT_FILTER] (emitted by ``ContractMetadataCache``).

        Parameters
        ----------
        symbol      : e.g. "NIFTY"
        spot_price  : Current underlying spot price.
        option_type : "CE" / "CALL" or "PE" / "PUT".
        """
        state = self._states.get(symbol.upper())
        if state is None or state.active_expiry is None:
            logger.warning(
                f"[EXPIRY_MANAGER] get_valid_contracts: "
                f"no active expiry for {symbol} — returning empty"
            )
            return []

        return self._cache.filter_intrinsic(
            underlying=symbol,
            spot_price=spot_price,
            option_type=option_type,
            expiry=state.active_expiry,
        )

    def get_summary(self) -> dict:
        """Return a dict summarising current expiry state for all symbols."""
        return {
            sym: {
                "active_expiry": str(state.active_expiry),
                "next_expiry":   str(state.next_expiry),
                "roll_count":    state.roll_count,
                "lot_size":      self.get_lot_size(sym),
            }
            for sym, state in self._states.items()
        }


# ── Module-level singleton ────────────────────────────────────────────────────────

_expiry_manager = ExpiryManager(symbols=["NIFTY"])


def get_expiry_manager() -> ExpiryManager:
    """Return the module-level ExpiryManager singleton."""
    return _expiry_manager
