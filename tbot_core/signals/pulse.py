# ============================================================
#  pulse_module.py  — v1.0  (Tick-Rate Momentum Module)
# ============================================================
"""
ARCHITECTURE
────────────
Pulse Module calculates tick-rate momentum metrics:
  - Tick inter-arrival times (ms)
  - Tick rate (ticks/sec)
  - Burst detection (tick_rate > threshold)
  - Direction drift (UP/DOWN/NEUTRAL)

Signals generated:
  - [MOMENTUM_TICK_RATE][UP]   — bullish burst
  - [MOMENTUM_TICK_RATE][DOWN] — bearish burst

Integration:
  - Data Engine: feeds tick data to Pulse
  - Decision Engine: uses Pulse for scalp entry signals
"""

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Tuple

import pandas as pd
import pytz

IST = pytz.timezone("Asia/Kolkata")

# ── ANSI colours ─────────────────────────────────────────────────────────────
RESET  = "\033[0m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"

# ── Configuration ───────────────────────────────────────────────────────────
# Rolling window for tick rate calculation (seconds)
PULSE_WINDOW_SECONDS = 30

# Burst detection threshold (ticks per second)
# Above this rate = burst detected
BURST_THRESHOLD_TICKS_PER_SEC = 15.0

# Minimum ticks required in window to calculate reliable rate
MIN_TICKS_FOR_RATE = 5

# Direction drift threshold (% price change to declare UP/DOWN)
DIRECTION_DRIFT_THRESHOLD_PCT = 0.02  # 0.02% price change


@dataclass
class PulseMetrics:
    """Container for pulse metrics calculated from tick data."""
    tick_rate: float = 0.0           # ticks per second
    avg_interval_ms: float = 0.0     # average ms between ticks
    burst_flag: bool = False          # True if tick_rate > BURST_THRESHOLD
    direction_drift: str = "NEUTRAL"  # UP/DOWN/NEUTRAL
    last_tick_time_ms: float = 0.0   # timestamp of last tick
    price_change_pct: float = 0.0    # % price change in window
    tick_count: int = 0              # number of ticks in window

    def to_tag(self) -> str:
        """Generate signal tag from pulse metrics."""
        if self.burst_flag:
            return f"[MOMENTUM_TICK_RATE][{self.direction_drift}]"
        return ""


class PulseModule:
    """
    Tick-rate momentum calculator.

    Maintains a rolling window of ticks and calculates:
    - Tick rate (ticks/sec)
    - Burst detection
    - Direction drift

    Usage:
        pulse = PulseModule()
        pulse.on_tick(timestamp_ms, price)
        metrics = pulse.get_pulse()
        if metrics.burst_flag:
            print(f"Burst detected: {metrics.to_tag()}")
    """

    def __init__(
        self,
        window_seconds: int = PULSE_WINDOW_SECONDS,
        burst_threshold: float = BURST_THRESHOLD_TICKS_PER_SEC,
        min_ticks: int = MIN_TICKS_FOR_RATE,
        direction_threshold_pct: float = DIRECTION_DRIFT_THRESHOLD_PCT,
    ):
        self.window_ms = window_seconds * 1000
        self.burst_threshold = burst_threshold
        self.min_ticks = min_ticks
        self.direction_threshold = direction_threshold_pct

        # Rolling buffer: (timestamp_ms, price)
        self._tick_buffer: deque = deque(maxlen=1000)

        # Price at start of window
        self._window_start_price: Optional[float] = None

        # Last calculated metrics (cached)
        self._cached_metrics: Optional[PulseMetrics] = None
        self._last_calc_time_ms: float = 0

        # Counters for dashboard
        self.burst_count: int = 0
        self.upward_bursts: int = 0
        self.downward_bursts: int = 0

        logging.info(
            f"[PULSE] Initialized: window={window_seconds}s "
            f"burst_threshold={burst_threshold} ticks/sec"
        )

    def on_tick(self, timestamp_ms: float, price: float) -> None:
        """
        Process a new tick and update internal buffers.

        Args:
            timestamp_ms: Unix timestamp in milliseconds
            price: Tick price (LTP)
        """
        # Initialize window start price on first tick
        if self._window_start_price is None:
            self._window_start_price = price

        # Add tick to buffer
        self._tick_buffer.append((timestamp_ms, price))

        # Invalidate cached metrics (will be recalculated on next get_pulse)
        self._cached_metrics = None

    def get_pulse(self, current_time_ms: Optional[float] = None) -> PulseMetrics:
        """
        Calculate pulse metrics from the rolling tick buffer.

        Args:
            current_time_ms: Current timestamp (default: now)

        Returns:
            PulseMetrics with calculated values
        """
        # Use current time if not provided
        if current_time_ms is None:
            current_time_ms = time.time() * 1000

        # Return cached if calculated recently (within 100ms)
        if (self._cached_metrics is not None and
            current_time_ms - self._last_calc_time_ms < 100):
            return self._cached_metrics

        # Clean old ticks outside window
        cutoff_ms = current_time_ms - self.window_ms
        while self._tick_buffer and self._tick_buffer[0][0] < cutoff_ms:
            self._tick_buffer.popleft()

        # Need minimum ticks for reliable calculation
        if len(self._tick_buffer) < self.min_ticks:
            self._cached_metrics = PulseMetrics()
            self._last_calc_time_ms = current_time_ms
            return self._cached_metrics

        # Calculate metrics
        metrics = self._calculate_metrics(current_time_ms)

        # Check for new burst
        if metrics.burst_flag and metrics.direction_drift != "NEUTRAL":
            # Check if this is a new burst (previous wasn't burst)
            if (self._cached_metrics is None or
                not self._cached_metrics.burst_flag):
                self.burst_count += 1
                if metrics.direction_drift == "UP":
                    self.upward_bursts += 1
                else:
                    self.downward_bursts += 1
                logging.info(
                    f"[PULSE][BURST] {metrics.direction_drift} "
                    f"tick_rate={metrics.tick_rate:.1f} ticks/sec "
                    f"price_change={metrics.price_change_pct:+.3f}%"
                )

        self._cached_metrics = metrics
        self._last_calc_time_ms = current_time_ms

        return metrics

    def _calculate_metrics(self, current_time_ms: float) -> PulseMetrics:
        """Calculate all pulse metrics from the tick buffer."""
        ticks = list(self._tick_buffer)

        if not ticks:
            return PulseMetrics()

        # Tick rate calculation
        time_span_ms = ticks[-1][0] - ticks[0][0]
        tick_count = len(ticks)

        if time_span_ms > 0:
            tick_rate = (tick_count / time_span_ms) * 1000  # ticks per second
            avg_interval_ms = time_span_ms / (tick_count - 1) if tick_count > 1 else 0
        else:
            tick_rate = 0
            avg_interval_ms = 0

        # Burst detection
        burst_flag = tick_rate >= self.burst_threshold

        # Direction drift calculation
        start_price = ticks[0][1]
        end_price = ticks[-1][1]

        if start_price > 0:
            price_change_pct = ((end_price - start_price) / start_price) * 100
        else:
            price_change_pct = 0

        # Determine direction drift
        if price_change_pct > self.direction_threshold:
            direction_drift = "UP"
        elif price_change_pct < -self.direction_threshold:
            direction_drift = "DOWN"
        else:
            direction_drift = "NEUTRAL"

        return PulseMetrics(
            tick_rate=round(tick_rate, 2),
            avg_interval_ms=round(avg_interval_ms, 2),
            burst_flag=burst_flag,
            direction_drift=direction_drift,
            last_tick_time_ms=ticks[-1][0],
            price_change_pct=round(price_change_pct, 4),
            tick_count=tick_count,
        )

    def reset(self) -> None:
        """Reset the pulse module (e.g., at start of new trading session)."""
        self._tick_buffer.clear()
        self._window_start_price = None
        self._cached_metrics = None
        self._last_calc_time_ms = 0
        logging.info("[PULSE] Module reset")

    def detect_exhaustion(self, current_time_ms: Optional[float] = None) -> Optional[str]:
        """Detect burst exhaustion: tick-rate spike followed by decay.

        Returns:
            "PULSE_EXHAUSTION" if burst was active and rate decayed below 50% of burst peak.
            "PULSE_SUSTAINED" if burst is still active.
            None if no burst context.
        """
        if current_time_ms is None:
            current_time_ms = time.time() * 1000

        metrics = self.get_pulse(current_time_ms)

        if not hasattr(self, "_peak_tick_rate"):
            self._peak_tick_rate = 0.0
            self._exhaustion_count = 0
            self._sustained_count = 0

        if metrics.burst_flag:
            if metrics.tick_rate > self._peak_tick_rate:
                self._peak_tick_rate = metrics.tick_rate
            self._sustained_count += 1
            return "PULSE_SUSTAINED"

        if self._peak_tick_rate > 0 and metrics.tick_rate < 0.5 * self._peak_tick_rate:
            self._exhaustion_count += 1
            logging.info(
                f"[PULSE_EXHAUSTION] peak={self._peak_tick_rate:.1f} "
                f"current={metrics.tick_rate:.1f} decay_ratio="
                f"{metrics.tick_rate / self._peak_tick_rate:.2f}"
            )
            self._peak_tick_rate = 0.0
            return "PULSE_EXHAUSTION"

        if self._peak_tick_rate > 0:
            return "PULSE_SUSTAINED"

        return None

    def get_stats(self) -> dict:
        """Get dashboard statistics."""
        return {
            "burst_count": self.burst_count,
            "upward_bursts": self.upward_bursts,
            "downward_bursts": self.downward_bursts,
            "exhaustion_count": getattr(self, "_exhaustion_count", 0),
            "sustained_count": getattr(self, "_sustained_count", 0),
        }

    def log_stats(self) -> None:
        """Log current pulse statistics."""
        stats = self.get_stats()
        logging.info(
            f"[PULSE][STATS] total_bursts={stats['burst_count']} "
            f"up={stats['upward_bursts']} down={stats['downward_bursts']}"
        )


# ── Singleton instance for global access ─────────────────────────────────────
_pulse_instance: Optional[PulseModule] = None


def get_pulse_module(
    window_seconds: int = PULSE_WINDOW_SECONDS,
    burst_threshold: float = BURST_THRESHOLD_TICKS_PER_SEC,
) -> PulseModule:
    """
    Get or create the global PulseModule singleton.

    Args:
        window_seconds: Rolling window size in seconds
        burst_threshold: Burst detection threshold (ticks/sec)

    Returns:
        PulseModule instance
    """
    global _pulse_instance

    if _pulse_instance is None:
        _pulse_instance = PulseModule(
            window_seconds=window_seconds,
            burst_threshold=burst_threshold,
        )
        logging.info("[PULSE] Global PulseModule singleton created")

    return _pulse_instance


def reset_pulse_module() -> None:
    """Reset the global PulseModule singleton."""
    global _pulse_instance

    if _pulse_instance is not None:
        _pulse_instance.reset()
        logging.info("[PULSE] Global PulseModule reset")
