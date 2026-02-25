"""High-frequency option exit algorithms for fast premium profit-booking.

This module is designed for long option positions (CALL/PUT premium buying),
where all calculations are done in option premium space (e.g., 200-400 range).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

import numpy as np
import pandas as pd


@dataclass
class OptionExitConfig:
    """Configuration for high-frequency exit algorithms.

    Attributes:
        dynamic_trail_lo:
            Initial trailing drawdown allowance when profit is small.
            A value of 0.10 means "allow up to 10% pullback from peak premium".
        dynamic_trail_hi:
            Tightened allowance once premium profit exceeds
            ``trail_tighten_profit_frac``.
        trail_tighten_profit_frac:
            Profit threshold relative to entry premium that triggers tighter
            trailing. Example: 0.50 means tighten once profit > 50%.
        roc_window_ticks:
            Window length used in rate-of-change estimation for momentum
            exhaustion. Recommended range is 5-10 ticks.
        roc_drop_fraction:
            Momentum exhaustion trigger. A value of 0.60 means exit when current
            ROC drops by 60% from the observed ROC peak while in profit.
        ma_window:
            Number of recent ticks used for mean-reversion statistics.
        std_threshold:
            Standard deviation multiplier used for over-extension detection.
        min_1m_bars_for_structure:
            Minimum number of completed 1-minute bars required to evaluate
            lower-high structure confirmation.
    """

    dynamic_trail_lo: float = 0.10
    dynamic_trail_hi: float = 0.03
    trail_tighten_profit_frac: float = 0.50
    roc_window_ticks: int = 8
    roc_drop_fraction: float = 0.60
    ma_window: int = 20
    std_threshold: float = 2.0
    min_1m_bars_for_structure: int = 3


class OptionExitManager:
    """High-frequency exit engine for long option premium positions.

    Mathematical summary:
    1) DynamicTrailingStop
       Let ``P_t`` be current premium and ``P_max`` be max premium seen since
       entry. Trail percentage ``tau`` is adaptive:
       - ``tau = 10%`` for early profits
       - ``tau = 3%`` once ``(P_t - P_entry)/P_entry > 50%``
       Trail stop is ``P_max * (1 - tau)``. Exit when price breaches this stop
       beyond ``risk_buffer`` to reduce wash-out exits from minor gaps.

    2) MomentumExhaustion
       Compute rolling ROC over N ticks:
       ``ROC_t = (P_t / P_{t-N}) - 1``.
       Maintain ``ROC_peak`` while in position. Trigger exit in profit when:
       ``ROC_t <= ROC_peak * (1 - 0.60)``, i.e. at least a 60% collapse in
       short-term momentum.

    3) VolatilityMeanReversion
       On recent tick prices, compute 20-period moving mean ``mu`` and std
       ``sigma``. Detect over-extension when:
       ``P_t > mu + 2*sigma``.
       Confirm with lower-high structure from 1-minute bar highs:
       ``High_{t-1} < High_{t-2}``.
       Exit when both conditions hold in profit.

    Notes:
    - All prices are option premiums; no spot/index values are used.
    - ``check_exit`` is O(window) on tiny windows and non-blocking for
      sub-second loops.
    """

    def __init__(
        self,
        entry_price: float,
        side: str = "CALL",
        risk_buffer: float = 1.0,
        config: Optional[OptionExitConfig] = None,
    ) -> None:
        self.entry_price = float(entry_price)
        self.side = side
        self.risk_buffer = float(max(0.0, risk_buffer))
        self.cfg = config or OptionExitConfig()

        max_points = max(120, self.cfg.ma_window * 6)
        self._prices: Deque[float] = deque(maxlen=max_points)
        self._volumes: Deque[float] = deque(maxlen=max_points)
        self._timestamps: Deque[pd.Timestamp] = deque(maxlen=max_points)
        self._roc_peak = 0.0
        self._peak_price = self.entry_price
        self.last_reason: str = ""

    def update_tick(
        self,
        price: float,
        volume: Optional[float],
        timestamp: pd.Timestamp | str,
    ) -> None:
        """Append one market tick into local rolling buffers.

        Parameters
        ----------
        price:
            Current option premium.
        volume:
            Tick volume associated with the same premium update.
        timestamp:
            Tick time used for 1-minute bar reconstruction.
        """
        px = float(price)
        ts = pd.Timestamp(timestamp)
        vol = 0.0 if volume is None else float(volume)

        self._prices.append(px)
        self._volumes.append(max(0.0, vol))
        self._timestamps.append(ts)
        self._peak_price = max(self._peak_price, px)

    def check_exit(
        self,
        current_price: float,
        timestamp: pd.Timestamp | str,
        current_volume: Optional[float] = None,
        ingest_tick: bool = True,
    ) -> bool:
        """Evaluate all high-frequency exits for the latest tick.

        The method is intentionally self-sufficient for streaming loops:
        every call appends the latest tick and then evaluates exit criteria.
        Existing integrations that call ``update_tick`` separately remain
        compatible by setting ``ingest_tick=False`` after manual ``update_tick``.
        """
        if ingest_tick:
            self.update_tick(current_price, current_volume, timestamp)

        px = float(current_price)
        self.last_reason = ""

        if px <= self.entry_price:
            return False

        if self._dynamic_trailing_stop(px):
            self.last_reason = "DYNAMIC_TRAILING_STOP"
            return True

        if self._momentum_exhaustion():
            self.last_reason = "MOMENTUM_EXHAUSTION"
            return True

        if self._volatility_mean_reversion(px):
            self.last_reason = "VOLATILITY_MEAN_REVERSION"
            return True

        return False

    def _dynamic_trailing_stop(self, price: float) -> bool:
        profit_frac = (price - self.entry_price) / max(self.entry_price, 1e-9)
        if profit_frac <= 0:
            return False

        if profit_frac >= self.cfg.trail_tighten_profit_frac:
            trail_frac = self.cfg.dynamic_trail_hi
        else:
            span = self.cfg.dynamic_trail_lo - self.cfg.dynamic_trail_hi
            tighten = profit_frac / max(self.cfg.trail_tighten_profit_frac, 1e-9)
            trail_frac = self.cfg.dynamic_trail_lo - (span * tighten)
            trail_frac = float(np.clip(trail_frac, self.cfg.dynamic_trail_hi, self.cfg.dynamic_trail_lo))

        trail_stop = self._peak_price * (1.0 - trail_frac)
        return price <= (trail_stop - self.risk_buffer)

    def _momentum_exhaustion(self) -> bool:
        n = self.cfg.roc_window_ticks
        if len(self._prices) < (n + 1):
            return False

        arr = np.asarray(self._prices, dtype=np.float64)
        base = arr[-(n + 1)]
        if base <= 0:
            return False

        roc = (arr[-1] / base) - 1.0
        self._roc_peak = max(self._roc_peak, roc)
        if self._roc_peak <= 0:
            return False

        collapse_level = self._roc_peak * (1.0 - self.cfg.roc_drop_fraction)
        return roc <= collapse_level

    def _volatility_mean_reversion(self, price: float) -> bool:
        window = self.cfg.ma_window
        if len(self._prices) < window:
            return False

        s = pd.Series(self._prices, dtype="float64")
        tail = s.iloc[-window:]
        mu = float(tail.mean())
        sigma = float(tail.std(ddof=0))
        if not np.isfinite(mu) or not np.isfinite(sigma) or sigma <= 0:
            return False

        stretched = price > (mu + self.cfg.std_threshold * sigma + self.risk_buffer)
        if not stretched:
            return False

        ticks = pd.DataFrame(
            {"price": list(self._prices), "ts": pd.to_datetime(list(self._timestamps))}
        ).set_index("ts")
        one_min = ticks["price"].resample("1min").ohlc().dropna()
        if len(one_min) < self.cfg.min_1m_bars_for_structure:
            return False

        prev_high = float(one_min["high"].iloc[-2])
        last_high = float(one_min["high"].iloc[-1])
        lower_high = last_high < (prev_high - self.risk_buffer)
        return lower_high


__all__ = ["OptionExitConfig", "OptionExitManager"]


def websocket_integration_example() -> str:
    """Return a copy-paste example for WebSocket feed integration.

    Example
    -------
    ```python
    import json
    import pandas as pd
    from option_exit_manager import OptionExitManager

    manager = OptionExitManager(entry_price=245.50, side="CALL", risk_buffer=1.0)

    def on_tick(message: str) -> None:
        data = json.loads(message)
        ltp = float(data["ltp"])               # option premium
        vol = float(data.get("volume", 0.0))   # tick/last traded volume
        ts = pd.Timestamp(data["timestamp"])

        if manager.check_exit(ltp, ts, current_volume=vol):
            reason = manager.last_reason
            print(f"EXIT -> {reason} @ {ltp:.2f}")
            # place exit order here
    ```
    """
    return "See docstring for integration example."
