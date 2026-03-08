import os
import logging
import pendulum as dt
import sys

from dotenv import load_dotenv, find_dotenv

# ===== Credentials ================================================

env_path = find_dotenv(r"C:\Users\mohan\mhn-fyers-algo\.env")

load_dotenv(dotenv_path=env_path)

client_id    = os.getenv("FYERS_CLIENT_ID")
secret_key   = os.getenv("FYERS_SECRET_KEY")
access_token = os.getenv("FYERS_ACCESS_TOKEN")
redirect_uri = os.getenv("FYERS_REDIRECT_URI")

# ===== Broker Selection ============================================
# Set BROKER via env variable or .env file.
# Supported values: "fyers" (default) | "zerodha"
# The active adapter is constructed by broker_init.build_broker_adapter().
# ==================================================================
BROKER = os.getenv("BROKER", "fyers").lower().strip()

# Zerodha Kite Connect credentials (loaded only when BROKER="zerodha")
ZERODHA_API_KEY      = os.getenv("ZERODHA_API_KEY",      "")
ZERODHA_API_SECRET   = os.getenv("ZERODHA_API_SECRET",   "")
ZERODHA_ACCESS_TOKEN = os.getenv("ZERODHA_ACCESS_TOKEN", "")

# ===================================================================

# ===== Strategy parameters ===========================================

strategy_name = 'options_trade_engine'
index_name = 'NIFTY50'
symbols = ["NSE:NIFTY50-INDEX"]
# # ["NSE:NIFTY50-INDEX", "NSE:BANKNIFTY-INDEX", "NSE:FINNIFTY-INDEX"]
exchange = 'NSE'
ticker = f"{exchange}:{index_name}-INDEX"

strike_count = 10               # Options strikes from Option chain
strike_diff = 100               # Difference from ATM Option
account_type = 'PAPER'          # 'PAPER' or 'LIVE'
quantity = 130                  # lot size - Nifty - 65
buffer = 5
profit_loss_point = 25          # Used for target profit/stoploss 
MAX_TRADES_PER_DAY = 8          # P3-D: Reduced from 20 → 8. Options buying requires
                                # high-conviction setups. Lower frequency → lower theta
                                # drag and spread cost across the session.

# ── Default Lot Size ────────────────────────────────────────────────────────────
# All trade sizing must reference DEFAULT_LOT_SIZE — never hard-code quantities.
# Override at runtime:  DEFAULT_LOT_SIZE=1 python execution.py
DEFAULT_LOT_SIZE = int(os.getenv("DEFAULT_LOT_SIZE", "2"))

CALL_MONEYNESS = 'ITM'          # strike/contract type - ITM/OTM  
PUT_MONEYNESS  = 'ITM'

# =======================================================================

# ================ Time Params ==========================================

time_zone = "Asia/Kolkata"
start_hour, start_min = 9, 30
end_hour, end_min = 15, 15

# Proper timezone-aware datetime objects
today = dt.today(time_zone)
start_time = today.replace(hour=start_hour, minute=start_min, second=0, microsecond=0)
end_time   = today.replace(hour=end_hour, minute=end_min, second=0, microsecond=0)

# =======================================================================

# ========== Live Entry Params ==================

ORDER_TYPE = "LIMIT"   # options: "LIMIT" or "MARKET"
ENTRY_OFFSET = 5       # only used if LIMIT, e.g. ltp - 5

# ========= Indicator Params =====================

CANDLE_INTERVAL_MIN = 3
ATR_PERIOD = 14

# ===============================================

# ============ Signal Parameters =============================

CANDLE_BODY_RANGE = 0.54            # Default is 0.6
ATR_VALUE = 15                      # Default is 20

# ============================================================

# ============ Risk Management ===========================

MAX_DAILY_LOSS = -15000     # stop trading if net PnL < -15000 (scaled to position size)
MAX_DRAWDOWN   = -10000     # stop trading if drawdown exceeds 10000

# =============================================================

# ===================== Oscilater Exit condition ====================
# 	HARD → when oscillator exit triggers, you close the entire position immediately.
# 	TRAIL → instead of closing, you tighten stop-loss to entry and let trailing logic handle the exit.

OSCILLATOR_EXIT_MODE = "TRAIL"   # "HARD" → close immediately; "TRAIL" → lock SL at entry

# ===================================================================

# ============ Indicator Tuning =====================================
# TREND_ENTRY_ADX_MIN: minimum ADX required to enter a trend trade.
#   Lowered from 25.0 → 18.0 to capture entries in moderate-strength
#   trends that a strict 25-point gate would filter out.
#   Override via env: TREND_ENTRY_ADX_MIN=20
#
# ST_RR_RATIO     : ST-pullback profit-target reward:risk ratio (PT).
# ST_TG_RR_RATIO  : ST-pullback conservative first-target ratio  (TG).
#   Both overridable via env vars of the same name.
# ==================================================================

TREND_ENTRY_ADX_MIN  = float(os.getenv("TREND_ENTRY_ADX_MIN",  "18.0"))
SLOPE_ADX_GATE       = float(os.getenv("SLOPE_ADX_GATE",       "20.0"))
TIME_SLOPE_ADX_GATE  = float(os.getenv("TIME_SLOPE_ADX_GATE",  "25.0"))   # Path D: post-11:00 flat slope allowed if ADX < this
SLOPE_CONFLICT_TIME_BARS = int(os.getenv("SLOPE_CONFLICT_TIME_BARS", "5"))  # Phase 6: bars of slope conflict before time override
ST_RR_RATIO         = float(os.getenv("ST_RR_RATIO",         "2.0"))
ST_TG_RR_RATIO      = float(os.getenv("ST_TG_RR_RATIO",      "1.0"))

# ===================================================================

# ========================= MODE ====================

MODE =   "STRATEGY"                         # "COLLECT" or "STRATEGY"      # Collet = and build db with tick data #Strategy = run the whole bot strategy 




# ===== Logging =====
log_file = f"{strategy_name}_{dt.now(time_zone).date()}.log"
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(stream=sys.stdout),  # force stdout
        logging.FileHandler(log_file, mode="a", encoding="utf-8")  # ensure UTF-8 for file
    ]
)

logging.info(f"[BROKER CONFIG] active_broker={BROKER}")
logging.info(
    f"[ENTRY CONFIG] TREND_ENTRY_ADX_MIN={TREND_ENTRY_ADX_MIN} "
    f"ST_RR_RATIO={ST_RR_RATIO} ST_TG_RR_RATIO={ST_TG_RR_RATIO}"
)
logging.info(f"[CONFIG] DEFAULT_LOT_SIZE={DEFAULT_LOT_SIZE}")
