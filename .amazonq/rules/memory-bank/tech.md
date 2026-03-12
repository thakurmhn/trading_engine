# Trading Engine - Technology Stack

## Programming Language & Runtime
- **Python**: 3.x (3.9+)
- **Async Runtime**: asyncio for concurrent WebSocket handling
- **Encoding**: UTF-8 with fallback error handling

## Core Dependencies

### Data Processing & Analysis
- **pandas** (3.0.1) - DataFrames, time series, OHLC aggregation
- **numpy** (2.2.6) - Numerical computations, array operations
- **scipy** (1.17.1) - Scientific computing utilities

### Broker Integration
- **fyers_apiv3** (3.1.7) - Fyers broker API client (primary)
- **websocket-client** (1.6.1) - WebSocket streaming for ticks/orders
- **requests** (2.31.0) - HTTP client for REST API calls

### Time & Timezone
- **pendulum** (3.2.0) - Timezone-aware datetime handling (IST)
- **pytz** (2025.2) - Timezone database
- **python-dateutil** (2.9.0) - Date utilities

### Data Persistence
- **pickle** (built-in) - State ledger serialization
- **sqlite3** (built-in) - Tick database storage

### Configuration & Environment
- **python-dotenv** (1.0.1) - .env file loading for credentials
- **configparser** (built-in) - INI-style configuration

### Logging & Monitoring
- **colorama** (0.4.6) - ANSI color output for terminal logs
- **logging** (built-in) - Structured audit logging

### Testing & Validation
- **pytest** (9.0.2) - Unit and integration testing framework
- **pytest-asyncio** - Async test support

### Development Tools
- **setuptools** (80.9.0) - Package management
- **wheel** (0.46.3) - Binary package format

## Build & Deployment

### Package Management
- **pip** - Python package installer
- **requirements.txt** - Dependency pinning (see root directory)

### Environment Setup
```bash
# Install dependencies
pip install -r requirements.txt

# Run strategy
python main.py

# Run tests
pytest test_*.py -v

# Backtest
python run_replay_v7.py --date 2026-03-08
python run_multiday_replay.py --start 2026-03-01 --end 2026-03-10
```

## Configuration Files

### config.py
Central configuration hub with environment variable overrides:
- **Broker**: BROKER (fyers/zerodha), API credentials
- **Strategy**: symbols, strike_diff, quantity, account_type (PAPER/LIVE)
- **Indicators**: CANDLE_INTERVAL_MIN (3), ATR_PERIOD (14)
- **Risk**: MAX_DAILY_LOSS (-15000), MAX_DRAWDOWN (-10000)
- **Entry Gates**: TREND_ENTRY_ADX_MIN (18.0), SLOPE_ADX_GATE (20.0)
- **Timing**: start_hour (9:30), end_hour (15:15), time_zone (Asia/Kolkata)

### .env
Credentials (not in repo):
```
FYERS_CLIENT_ID=<client_id>
FYERS_SECRET_KEY=<secret_key>
FYERS_ACCESS_TOKEN=<access_token>
FYERS_REDIRECT_URI=<redirect_uri>
BROKER=fyers
DEFAULT_LOT_SIZE=2
```

## Data Storage

### Tick Database (SQLite)
- **Location**: `data/ticks.db/ticks_YYYY-MM-DD.db`
- **Schema**: timestamp, symbol, ltp, volume, bid, ask, etc.
- **Purpose**: Replay backtesting, historical analysis
- **Retention**: Daily files, auto-cleanup after 30 days

### State Ledger (Pickle)
- **Location**: `data-YYYY-MM-DD-PAPER.pickle` or `data-YYYY-MM-DD-LIVE.pickle`
- **Format**: List of snapshots with timestamp + state dict
- **Contents**: Open positions, P&L, trade counts, cooldowns
- **Recovery**: Loaded on startup for position hydration

### Restart State (Pickle)
- **Location**: `restart-state-paper.pickle` or `restart-state-live.pickle`
- **Contents**: Minimal state (cooldowns, active positions, suppression timers)
- **Purpose**: Fast recovery after crash/restart

### Trade Reports (CSV/JSON)
- **Location**: `reports/trades_YYYY-MM-DD.csv` and `.json`
- **Contents**: Entry/exit prices, P&L, exit reason, bars held
- **Frequency**: Updated after each trade exit

## Logging Architecture

### Log Files
- **Main Log**: `options_trade_engine_YYYY-MM-DD.log`
- **Broker Logs**: `fyersApi.log`, `fyersDataSocket.log`, `fyersOrderSocket.log`
- **Requests Log**: `fyersRequests.log`

### Log Levels
- **INFO**: Entry/exit signals, order status, risk events
- **DEBUG**: Indicator calculations, state updates, tick details
- **WARNING**: Fallback logic, missing data, API errors
- **ERROR**: Critical failures, exception traces

### ANSI Color Coding
- **GREEN**: Entry signals, successful orders
- **YELLOW**: Exit signals, warnings, deferred actions
- **RED**: Stop losses, errors, risk halts
- **CYAN**: Debug info, state updates
- **MAGENTA**: Rejected orders
- **GRAY**: Verbose debug output

## API Integration

### Fyers API v3
**Endpoints Used**:
- `quotes()` - Real-time LTP, volume, Greeks
- `orderbook()` - Order status polling
- `place_order()` - Entry/exit order placement
- `positions()` - Open position tracking
- `holdings()` - Portfolio holdings

**WebSocket Streams**:
- **Data Socket**: Tick streaming (LTP, volume, bid/ask)
- **Order Socket**: Order status updates (TRADED, PENDING, REJECTED)

**Rate Limits**:
- Quote API: 100 req/min
- Order API: 50 req/min
- WebSocket: Unlimited (connection-based)

## Development Commands

### Running the Strategy
```bash
# Paper mode (default)
python main.py

# Live mode (requires .env credentials)
ACCOUNT_TYPE=LIVE python main.py

# With custom lot size
DEFAULT_LOT_SIZE=1 python main.py

# With custom ADX gate
TREND_ENTRY_ADX_MIN=20 python main.py
```

### Backtesting
```bash
# Single day replay
python run_replay_v7.py --date 2026-03-08 --mode REPLAY

# Multi-day backtest
python run_multiday_replay.py --start 2026-03-01 --end 2026-03-10

# Build replay report
python _build_replay_report.py
```

### Testing
```bash
# Run all tests
pytest test_*.py -v

# Run specific test file
pytest test_exit_logic.py -v

# Run with coverage
pytest --cov=. test_*.py
```

### Analysis & Diagnostics
```bash
# Parse trade logs
python log_parser.py options_trade_engine_2026-03-08.log

# Analyze replay results
python replay_analyzer_v7.py

# Dashboard
python dashboard.py

# End-of-day summary
python eod_dashboard.py
```

## Performance Considerations

### Memory Usage
- **Candle Storage**: ~1MB per 1000 3m candles (in-memory)
- **Indicator Cache**: ~500KB per symbol
- **State Ledger**: ~100KB per day
- **Total Footprint**: ~50-100MB for full session

### CPU Usage
- **Tick Processing**: <1ms per tick (async)
- **Indicator Calculation**: <10ms per candle
- **Signal Detection**: <5ms per candle
- **Exit Checks**: <2ms per position per second

### Network
- **WebSocket**: Persistent connection (low bandwidth)
- **API Calls**: ~1-2 per second during trading hours
- **Bandwidth**: <100KB/hour typical

## Deployment Checklist

- [ ] Python 3.9+ installed
- [ ] Virtual environment created and activated
- [ ] Dependencies installed: `pip install -r requirements.txt`
- [ ] .env file configured with Fyers credentials
- [ ] config.py reviewed and customized
- [ ] Broker connection tested: `python setup.py`
- [ ] Paper mode validated with historical data
- [ ] Live mode credentials verified
- [ ] Logging directory writable
- [ ] Database directory writable
- [ ] Cron job scheduled for daily startup (optional)
