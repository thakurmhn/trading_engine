# Trading Engine - Project Structure

## Directory Organization

```
trading_engine/
├── Core Trading Logic
│   ├── execution.py              # Main entry/exit orchestration (5000+ lines)
│   ├── entry_logic.py            # Entry signal detection and scoring
│   ├── option_exit_manager.py    # HFT-style exit management
│   ├── trade_classes.py          # Trade state and lifecycle management
│   └── position_manager.py       # Position tracking and P&L calculation
│
├── Market Data & Indicators
│   ├── market_data.py            # Real-time candle aggregation + indicator computation
│   ├── indicators.py             # Technical indicators (Supertrend, ADX, RSI, CCI, ATR, EMA)
│   ├── signals.py                # Signal detection (trend, reversal, compression)
│   ├── data_feed.py              # WebSocket tick streaming + CandleAggregator
│   ├── candle_builder.py         # OHLC aggregation from ticks
│   └── tickdb.py                 # SQLite tick persistence for replay
│
├── Pivot & Context Analysis
│   ├── compression_detector.py   # CPR compression state tracking
│   ├── reversal_detector.py      # S5/R5 pivot rejection scoring
│   ├── failed_breakout_detector.py # Breakout failure detection
│   ├── zone_detector.py          # Support/resistance zone tracking
│   ├── day_type.py               # Day classification (TRENDING/RANGE/GAP/BALANCE)
│   ├── regime_context.py         # Regime-aware parameter adaptation
│   ├── volatility_context.py     # ATR-based volatility regime classification
│   └── pulse_module.py           # Tick rate + burst detection for scalp confirmation
│
├── Broker Integration
│   ├── setup.py                  # Fyers API initialization + option chain loading
│   ├── broker_init.py            # Broker adapter pattern (Fyers/Zerodha support)
│   ├── order_utils.py            # Order placement + status tracking utilities
│   └── contract_metadata.py      # Option contract metadata management
│
├── Orchestration & Persistence
│   ├── main.py                   # Entry point: warmup → WebSocket → strategy loop
│   ├── orchestration.py          # Candle + indicator update pipeline
│   ├── config.py                 # Configuration parameters (ADX min, SL/PT ratios, etc.)
│   └── execution.py              # State persistence (pickle ledger + restart recovery)
│
├── Utilities & Analysis
│   ├── dashboard.py              # Real-time P&L + trade metrics dashboard
│   ├── eod_dashboard.py          # End-of-day performance summary
│   ├── log_parser.py             # Trade log analysis and diagnostics
│   ├── replay_analyzer_v7.py     # Backtest result analysis
│   ├── greeks_calculator.py      # Option Greeks computation
│   └── expiry_manager.py         # Option expiry handling
│
├── Testing & Validation
│   ├── conftest.py               # Pytest fixtures and test configuration
│   ├── test_*.py                 # Unit and integration tests (20+ test files)
│   ├── replay_validation_agent.py # Automated backtest validation
│   └── validation_v9_complete.py # End-to-end validation suite
│
├── Replay & Backtesting
│   ├── run_replay_v7.py          # Single-day replay runner
│   ├── run_multiday_replay.py    # Multi-day backtest orchestrator
│   ├── _build_replay_report.py   # Replay result aggregation
│   └── replay_option_exit_validation.py # Exit logic validation
│
├── Data & Reports
│   ├── data/                     # Tick database storage
│   ├── reports/                  # Daily P&L and trade reports
│   ├── replay_results/           # Backtest output (trades, signals, dashboards)
│   └── replay_validation_*/      # Validation run artifacts
│
├── Configuration & Documentation
│   ├── requirements.txt          # Python dependencies
│   ├── .env                      # Credentials (Fyers API keys)
│   ├── .gitignore                # Git exclusions
│   ├── Architecture.md           # System design documentation
│   ├── EXECUTION_REFACTORING_PLAN.md # Planned modularization roadmap
│   └── improvements/             # Enhancement proposals and examples
│
└── Logs & State
    ├── options_trade_engine_*.log # Daily execution logs
    ├── data-*-PAPER.pickle       # Paper mode state ledger
    ├── restart-state-*.pickle    # Restart recovery state
    └── *.db                      # SQLite tick databases
```

## Core Components

### 1. Execution Engine (execution.py)
**Responsibility**: Main trading orchestration
- Entry signal evaluation with multi-layer quality gates
- Exit condition checking with strict precedence
- Dynamic SL/PT/TG calculation based on ATR regimes
- State persistence and restart recovery
- Paper vs. Live mode routing

**Key Functions**:
- `paper_order()` - Paper mode entry/exit orchestration
- `live_order()` - Live mode entry/exit orchestration
- `_trend_entry_quality_gate()` - Multi-layer entry validation
- `check_exit_condition()` - Exit precedence evaluation
- `build_dynamic_levels()` - ATR-adaptive SL/PT/TG calculation
- `process_order()` - Trade lifecycle management

### 2. Market Data Pipeline (market_data.py)
**Responsibility**: Real-time candle aggregation and indicator computation
- WebSocket tick ingestion
- OHLC candle building (3m, 15m intervals)
- Indicator calculation (Supertrend, ADX, RSI, CCI, ATR, EMA)
- Historical data warmup from Fyers API

**Key Classes**:
- `MarketData` - Main aggregator and indicator engine
- `CandleAggregator` - Per-symbol candle state machine

### 3. Signal Detection (signals.py)
**Responsibility**: Multi-source signal generation
- Trend continuation signals (Supertrend + ADX)
- Reversal signals (pivot rejection scoring)
- Compression breakout signals
- Zone revisit signals
- Pulse-confirmed scalp signals

**Key Functions**:
- `detect_signal()` - Main signal dispatcher
- `detect_reversal()` - S5/R5 rejection scoring
- `detect_failed_breakout()` - Breakout failure detection
- `detect_zone_revisit()` - Zone re-entry detection

### 4. Indicator Library (indicators.py)
**Responsibility**: Technical indicator computation
- Supertrend (trend direction + line)
- ADX (trend strength)
- RSI, CCI, Williams%R (oscillators)
- ATR (volatility)
- EMA (trend confirmation)
- CPR, Traditional, Camarilla pivots

### 5. Broker Integration (setup.py, broker_init.py)
**Responsibility**: Broker API abstraction
- Fyers API v3 initialization
- Option chain loading and caching
- Order placement and status tracking
- Quote fetching and position management

**Supported Brokers**:
- Fyers (primary)
- Zerodha (adapter pattern support)

### 6. Data Feed (data_feed.py)
**Responsibility**: WebSocket streaming and tick persistence
- Fyers WebSocket connection management
- Tick ingestion and routing to MarketData
- SQLite tick database persistence
- Order socket monitoring

### 7. State Management (execution.py)
**Responsibility**: Trade state persistence and recovery
- Pickle-based ledger storage (daily snapshots)
- Restart state recovery (cooldowns, open positions)
- Position hydration and validation on startup
- Stale trade cleanup

## Data Flow Architecture

```
WebSocket Ticks
    ↓
data_feed.onmessage()
    ↓
market_data.on_tick()
    ↓
CandleAggregator (3m, 15m)
    ↓
Indicator Computation
    ↓
market_data.get_candles()
    ↓
paper_order() / live_order()
    ├─ Entry: detect_signal() → _trend_entry_quality_gate()
    ├─ Exit: check_exit_condition() (11-point precedence)
    └─ Levels: build_dynamic_levels() (ATR-adaptive)
    ↓
process_order() → send_paper/live_exit_order()
    ↓
State Persistence (pickle ledger)
```

## Trade Lifecycle

```
ENTRY PHASE
├─ Signal Detection (detect_signal)
├─ Quality Gate (trend_entry_quality_gate)
├─ Option Selection (get_option_by_moneyness)
├─ Level Calculation (build_dynamic_levels)
└─ Order Placement (send_live_entry_order)
    ↓
OPEN PHASE
├─ Exit Check (check_exit_condition) - every second
├─ Trailing Stop Update (update_trailing_stop)
├─ Partial Exits (PT1 @ 40%, PT2 @ 30%)
└─ State Persistence (store)
    ↓
EXIT PHASE
├─ Exit Trigger (SL/PT/TG/OSC/TIME/etc.)
├─ Order Placement (send_live_exit_order)
├─ P&L Calculation
├─ Cooldown Activation (scalp: 20min)
└─ State Cleanup (trade_flag=0, is_open=False)
```

## Configuration Hierarchy

1. **config.py** - Base parameters (ADX min, SL/PT ratios, trade caps)
2. **Day Type Classifier** - Per-day regime overrides (trail_step, max_hold)
3. **Regime Context** - Per-trade adaptive parameters (ATR tier, gap context)
4. **Entry Gate Context** - Per-signal oscillator bounds (zone-aware)
5. **Runtime State** - Per-position tracking (entry_time, trail_updates, etc.)

## Key Architectural Patterns

### 1. Multi-Layer Entry Gating
- Supertrend alignment (3m + 15m bias match)
- ADX strength confirmation (min 18.0, expandable)
- Oscillator bounds (RSI, CCI, Williams%R with ATR expansion)
- Pivot structure (S4/R4 daily levels, CPR compression)
- Reversal scoring (S5/R5 rejection confidence ≥80)
- Failed breakout governance (reversal direction alignment)

### 2. Strict Exit Precedence
11-point precedence ensures deterministic exit behavior:
1. HFT override (highest priority)
2. Stop loss (with survivability guardrails)
3. Profit targets (partial exits)
4. Minimum bar maturity (2-3 bars)
5. Contextual structure breaks
6. Oscillator exhaustion
7. Supertrend flip
8. Reversal exit
9. Momentum exhaustion
10. Time exit

### 3. Regime-Adaptive Risk Management
- ATR-based SL tiers (0.75x-1.3x ATR depending on ADX)
- Volatility-aware PT/TG (1.7x-2.7x ATR depending on regime)
- Day type modifiers (TRENDING_DAY: hold longer, RANGE_DAY: exit faster)
- Gap-aware oscillator suppression (3+ hits on gap days vs 2+ normal)

### 4. State Persistence & Recovery
- Pickle ledger (daily snapshots with timestamps)
- Restart state file (cooldowns, open positions)
- Position hydration (restore open trades on startup)
- Stale trade validation (close trades conflicting with current gates)

### 5. Dual-Mode Architecture
- **Paper Mode**: Simulated fills with slippage modeling (1.5 pts)
- **Live Mode**: Real Fyers API orders with market impact
- **Replay Mode**: Historical tick replay for backtesting
- Unified entry/exit logic across all modes
