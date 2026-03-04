"""High-frequency option exit algorithms for fast premium profit-booking.

This module is designed for long option positions (CALL/PUT premium buying),
where all calculations are done in option premium space (e.g., 200-400 range).
"""

from __future__ import annotations

import logging
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
        theta_decay_bars:
            Minimum bars held before time-decay gate activates. Default 6
            (18 minutes on 3m bars). If premium_move < 0 after this many bars
            and session is past theta_decay_cutoff, trigger a [THETA_EXIT] review.
        theta_decay_cutoff_hour:
            Hour (IST 24h) after which theta decay accelerates (default 11).
        theta_decay_cutoff_min:
            Minute component of the cutoff (default 30 → 11:30 IST).
        vol_reversion_min_bars:
            Minimum bars_held required before volatility mean-reversion exit
            fires. Guards against premature exits on early momentum. Default 4.
        vol_reversion_lower_high_bars:
            Number of consecutive lower-high bars required to confirm mean
            reversion structure. Default 3 (was 2 — raised to reduce false exits
            during genuine trending option premium moves).
    """

    dynamic_trail_lo: float = 0.10
    dynamic_trail_hi: float = 0.03
    trail_tighten_profit_frac: float = 0.50
    roc_window_ticks: int = 8
    roc_drop_fraction: float = 0.60
    ma_window: int = 20
    std_threshold: float = 2.0
    min_1m_bars_for_structure: int = 3
    # P1-A: Theta / time-decay gate
    theta_decay_bars: int = 6
    theta_decay_cutoff_hour: int = 11
    theta_decay_cutoff_min: int = 30
    # P3-E: Stricter volatility mean-reversion guards
    vol_reversion_min_bars: int = 4
    vol_reversion_lower_high_bars: int = 3
    # P3-B: Dynamic exit threshold (composite score gate)
    exit_threshold_base: int = 45
    dynamic_threshold_enabled: bool = True


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
        self._bars_held: int = 0          # incremented by caller via check_exit

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
        bars_held: int = 0,
        adx_value: float = 0.0,
    ) -> bool:
        """Evaluate all high-frequency exits for the latest tick.

        The method is intentionally self-sufficient for streaming loops:
        every call appends the latest tick and then evaluates exit criteria.
        Existing integrations that call ``update_tick`` separately remain
        compatible by setting ``ingest_tick=False`` after manual ``update_tick``.

        Parameters
        ----------
        bars_held:
            Number of 3m bars elapsed since entry. Used by the theta-decay
            gate (P1-A) and the volatility mean-reversion guard (P3-E).
        adx_value:
            Current 14-period ADX from the 3m or 15m timeframe. Used by the
            P3-B dynamic exit threshold to adjust conviction requirements.
        """
        if ingest_tick:
            self.update_tick(current_price, current_volume, timestamp)

        self._bars_held = max(self._bars_held, bars_held)
        px = float(current_price)
        self.last_reason = ""

        # P1-A: Theta / time-decay gate — fires only when position is losing
        # time value after the mid-session cutoff.  Runs regardless of profit.
        if self._time_decay_gate(px, timestamp):
            self.last_reason = "THETA_EXIT"
            return True

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

        # P3-B: Composite scored exit — fires when weighted position state
        # crosses a dynamic threshold adjusted for ADX and unrealized profit.
        if self._check_composite_exit_score(px, adx_value=adx_value):
            self.last_reason = "COMPOSITE_SCORE_EXIT"
            return True

        return False

    # ------------------------------------------------------------------
    # P1-A  Theta / time-decay gate
    # ------------------------------------------------------------------

    def _time_decay_gate(self, price: float, timestamp: pd.Timestamp | str) -> bool:
        """Exit if position is losing premium AND session is past mid-day cutoff.

        Conditions (all must hold):
          1. bars_held >= theta_decay_bars  (default 6 × 3m = 18 minutes)
          2. Session time > cutoff (default 11:30 IST)
          3. premium_move < 0  (position is currently at a loss vs entry)

        Rationale: option theta decay is non-linear and accelerates toward
        afternoon.  A position that is still red after 6 bars past 11:30
        is unlikely to recover enough to overcome growing theta drag.
        """
        bars = self._bars_held
        if bars < self.cfg.theta_decay_bars:
            return False

        premium_move = price - self.entry_price
        if premium_move >= 0:
            return False

        ts = pd.Timestamp(timestamp)
        cutoff_minutes = (
            self.cfg.theta_decay_cutoff_hour * 60 + self.cfg.theta_decay_cutoff_min
        )
        session_minutes = ts.hour * 60 + ts.minute
        if session_minutes < cutoff_minutes:
            return False

        return True

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
        """Exit when premium is over-extended AND lower-high structure confirmed.

        P3-E refinements vs original:
          - Requires bars_held >= vol_reversion_min_bars (default 4) to avoid
            premature exits during early momentum bursts.
          - Requires vol_reversion_lower_high_bars (default 3) consecutive
            lower highs instead of just 1, reducing false positives on normal
            intraday microstructure noise.
        """
        # Guard: don't fire on fresh positions
        if self._bars_held < self.cfg.vol_reversion_min_bars:
            return False

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
        required_bars = max(self.cfg.vol_reversion_lower_high_bars + 1,
                            self.cfg.min_1m_bars_for_structure)
        if len(one_min) < required_bars:
            return False

        # Require N consecutive lower highs (default 3) for structure confirmation
        highs = one_min["high"].iloc[-(self.cfg.vol_reversion_lower_high_bars + 1):]
        lower_high_streak = all(
            float(highs.iloc[k + 1]) < (float(highs.iloc[k]) - self.risk_buffer)
            for k in range(len(highs) - 1)
        )
        if lower_high_streak:
            logging.debug(
                f"[VOL_REVERSION_REFINED] bars_held={self._bars_held} "
                f"lower_high_bars={self.cfg.vol_reversion_lower_high_bars} "
                f"price={float(highs.iloc[-1]):.2f} structure=confirmed"
            )
        return lower_high_streak

    # ------------------------------------------------------------------
    # P3-B  Dynamic exit threshold (composite scored gate)
    # ------------------------------------------------------------------

    def _check_composite_exit_score(
        self, price: float, adx_value: float = 0.0
    ) -> bool:
        """Additional exit gate using a composite score against a dynamic threshold.

        P3-B spec: replace static 45/100 threshold with:
            exit_threshold = base(45) + adx_bonus - profit_bonus

        ADX bonus (stronger trend = need more conviction to exit = RAISE bar):
            ADX ≥ 35 → +10  (strong trend; hold longer)
            ADX ≥ 25 → +5   (established trend; moderate hold)
            ADX <  25 → 0

        Profit bonus (protect unrealised gains = LOWER bar to exit):
            P&L ≥ 80% of entry premium → -10 (easier exit to lock gains)

        Score components (0–100):
            profit_pts   : 0–40  scaled from unrealised P&L fraction
            maturity_pts : 0–15  from bars held (maturity of position)
            momentum_pts : 0–20  if ROC dropped >50% from peak
            vol_pts      : 0–25  if price statistically over-extended (>2σ / >1.5σ)

        Gate fires when score >= threshold AND position is in profit.
        """
        if not self.cfg.dynamic_threshold_enabled:
            return False

        profit_frac = (price - self.entry_price) / max(self.entry_price, 1e-9)
        if profit_frac <= 0:
            return False   # only exit in profit via this gate

        # ── Dynamic threshold ─────────────────────────────────────────────────
        adx_bonus    = 10 if adx_value >= 35 else (5 if adx_value >= 25 else 0)
        profit_bonus = 10 if profit_frac >= 0.80 else 0
        threshold    = self.cfg.exit_threshold_base + adx_bonus - profit_bonus

        # ── Score components ──────────────────────────────────────────────────
        profit_pts   = min(40, int(profit_frac * 50))
        maturity_pts = min(15, self._bars_held * 2)

        momentum_pts = 0
        n = self.cfg.roc_window_ticks
        if len(self._prices) >= n + 1:
            arr  = np.asarray(self._prices, dtype=np.float64)
            base = arr[-(n + 1)]
            if base > 0:
                roc = (arr[-1] / base) - 1.0
                if self._roc_peak > 0:
                    if roc < self._roc_peak * 0.50:
                        momentum_pts = 20   # ROC dropped >50% from peak
                    elif roc < self._roc_peak * 0.70:
                        momentum_pts = 10   # ROC dropped >30% from peak

        vol_pts = 0
        window  = self.cfg.ma_window
        if len(self._prices) >= window:
            s     = pd.Series(self._prices, dtype="float64")
            tail  = s.iloc[-window:]
            mu    = float(tail.mean())
            sigma = float(tail.std(ddof=0))
            if np.isfinite(mu) and np.isfinite(sigma) and sigma > 0:
                if price > mu + 2.0 * sigma:
                    vol_pts = 25
                elif price > mu + 1.5 * sigma:
                    vol_pts = 15

        score = profit_pts + maturity_pts + momentum_pts + vol_pts

        logging.debug(
            f"[DYNAMIC_EXIT_THRESHOLD] score={score}/{threshold} "
            f"adx={adx_value:.1f} adx_bonus={adx_bonus:+d} "
            f"profit_bonus={profit_bonus:+d} profit={profit_frac:.1%} | "
            f"profit_pts={profit_pts} maturity_pts={maturity_pts} "
            f"momentum_pts={momentum_pts} vol_pts={vol_pts}"
        )

        if score >= threshold:
            logging.info(
                f"[DYNAMIC_EXIT_THRESHOLD] FIRED score={score}>={threshold} "
                f"adx={adx_value:.1f} profit={profit_frac:.1%} "
                f"bars_held={self._bars_held}"
            )
            return True
        return False


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
