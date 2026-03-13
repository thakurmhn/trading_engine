"""Timeframe definitions for multi-timeframe support."""

from enum import Enum


class Timeframe(Enum):
    """Supported candle timeframes.

    The value is the string passed to broker APIs and used in resampling rules.
    """
    M1  = "1m"
    M3  = "3m"
    M5  = "5m"
    M15 = "15m"
    M30 = "30m"
    H1  = "1H"
    D   = "D"

    @property
    def minutes(self) -> int:
        """Return the number of minutes in this timeframe (daily = 375 for NSE)."""
        _map = {
            "1m": 1, "3m": 3, "5m": 5, "15m": 15,
            "30m": 30, "1H": 60, "D": 375,
        }
        return _map[self.value]

    @property
    def pandas_rule(self) -> str:
        """Return the pandas resample rule string."""
        _map = {
            "1m": "1min", "3m": "3min", "5m": "5min", "15m": "15min",
            "30m": "30min", "1H": "1h", "D": "D",
        }
        return _map[self.value]

    def __str__(self) -> str:
        return self.value
