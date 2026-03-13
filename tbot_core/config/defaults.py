"""Default constants extracted from config.py.

All values are importable and overridable. No side effects (no logging setup,
no env loading, no global state) — that stays in the root config.py.
"""

import os

# ── Strategy Parameters ──────────────────────────────────────────────────────
STRATEGY_NAME = "options_trade_engine"
INDEX_NAME = "NIFTY50"
DEFAULT_SYMBOLS = ["NSE:NIFTY50-INDEX"]
EXCHANGE = "NSE"

STRIKE_COUNT = 10
STRIKE_DIFF = 100
DEFAULT_ACCOUNT_TYPE = "PAPER"
DEFAULT_QUANTITY = 130
BUFFER = 5
PROFIT_LOSS_POINT = 25
MAX_TRADES_PER_DAY = 12
DEFAULT_LOT_SIZE = int(os.getenv("DEFAULT_LOT_SIZE", "2"))

CALL_MONEYNESS = "ITM"
PUT_MONEYNESS = "ITM"

# ── Time Parameters ──────────────────────────────────────────────────────────
TIME_ZONE = "Asia/Kolkata"
START_HOUR, START_MIN = 9, 30
END_HOUR, END_MIN = 15, 15
MARKET_OPEN_HOUR, MARKET_OPEN_MIN = 9, 15
MARKET_CLOSE_HOUR, MARKET_CLOSE_MIN = 15, 30

# ── Order Parameters ─────────────────────────────────────────────────────────
ORDER_TYPE = "LIMIT"
ENTRY_OFFSET = 5

# ── Indicator Parameters ─────────────────────────────────────────────────────
CANDLE_INTERVAL_MIN = 3
ATR_PERIOD = 14
CANDLE_BODY_RANGE = 0.54
ATR_VALUE = 15

# ── Risk Management ──────────────────────────────────────────────────────────
MAX_DAILY_LOSS = -15000
MAX_DRAWDOWN = -10000

# ── Oscillator Exit ──────────────────────────────────────────────────────────
OSCILLATOR_EXIT_MODE = "TRAIL"

# ── Indicator Tuning ─────────────────────────────────────────────────────────
TREND_ENTRY_ADX_MIN = float(os.getenv("TREND_ENTRY_ADX_MIN", "18.0"))
SLOPE_ADX_GATE = float(os.getenv("SLOPE_ADX_GATE", "20.0"))
TIME_SLOPE_ADX_GATE = float(os.getenv("TIME_SLOPE_ADX_GATE", "25.0"))
SLOPE_CONFLICT_TIME_BARS = int(os.getenv("SLOPE_CONFLICT_TIME_BARS", "5"))
ST_RR_RATIO = float(os.getenv("ST_RR_RATIO", "2.0"))
ST_TG_RR_RATIO = float(os.getenv("ST_TG_RR_RATIO", "1.0"))

# ── Mode ─────────────────────────────────────────────────────────────────────
MODE = "STRATEGY"
