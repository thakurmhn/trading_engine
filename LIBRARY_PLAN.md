# Trading Engine Core Library — Architecture & Execution Plan

> **Date**: 2026-03-13
> **Source codebase**: `c:\Users\mohan\trading_engine` (82 Python files, ~45,800 LOC)
> **Goal**: Extract a reusable, modular library (`tbot_core`) from the existing monolith so that **any** trading strategy — Options Buying, Options Selling, Equity Intraday, Swing, Screeners — can be built on top.

---

## Table of Contents

1. [Existing Feature Inventory](#1-existing-feature-inventory)
2. [Target Library Architecture](#2-target-library-architecture)
3. [Engine Breakdown](#3-engine-breakdown)
4. [Detailed Module Mapping (Old → New)](#4-detailed-module-mapping-old--new)
5. [New Capabilities to Add](#5-new-capabilities-to-add)
6. [Execution Plan (Phased)](#6-execution-plan-phased)
7. [API Surface — Key Callables](#7-api-surface--key-callables)
8. [Strategy Builder Examples](#8-strategy-builder-examples)
9. [Testing Strategy](#9-testing-strategy)
10. [Risks & Mitigations](#10-risks--mitigations)

---

## 1. Existing Feature Inventory

### 1.1 Broker Integration (4 brokers)

| Feature | Current File | Functions/Classes |
|---------|-------------|-------------------|
| Fyers API v3 auth + session | `setup.py` | `fyers` client, `fyers_async` |
| Fyers history fetch | `orchestration.py` | `fetch_fyers_history()` |
| Fyers option chain | `setup.py` | `option_chain`, `spot_price` |
| Fyers instruments API | `contract_metadata.py` | `ContractMetadataCache.refresh()` |
| Fyers WebSocket ticks | `data_feed.py` | `fyers_socket`, `onmessage()` |
| Fyers order placement | `st_pullback_cci.py` | `FyersAdapter.place_entry/exit()` |
| Zerodha Kite adapter | `st_pullback_cci.py` | `ZerodhaKiteAdapter` |
| Angel One SmartAPI adapter | `st_pullback_cci.py` | `AngelOneAdapter` |
| CCXT (crypto exchanges) | `st_pullback_cci.py` | `CcxtAdapter` |
| Broker factory | `broker_init.py` | `build_broker_adapter()` |

### 1.2 Data Pipeline

| Feature | Current File | Functions/Classes |
|---------|-------------|-------------------|
| Tick ingestion → OHLCV (3m) | `candle_builder.py` | `build_3min_candle()` |
| 3m → 15m resampling | `candle_builder.py` | `resample_15m()` |
| In-memory candle aggregation | `market_data.py` | `MarketData` / `CandleAggregator` |
| SQLite tick persistence | `tickdb.py` | `TickDatabase` |
| Fyers history → DataFrame | `orchestration.py` | `fetch_fyers_history()` |
| Indicator enrichment on DF | `orchestration.py` | `build_indicator_dataframe()` |
| Data feed preparation | `candle_builder.py` | `prepare_intraday()` |

### 1.3 Indicators (11 indicators)

| Indicator | Period(s) | Current File | Function |
|-----------|-----------|-------------|----------|
| Supertrend | 10/3x | `indicators.py`, `orchestration.py` | `supertrend()`, `_compute_supertrend()` |
| ATR | 14 | `indicators.py` | `calculate_atr()`, `resolve_atr()`, `daily_atr()` |
| RSI | 14 | `indicators.py` | `compute_rsi()` |
| CCI | 14, 20 | `indicators.py`, `orchestration.py` | `calculate_cci()` |
| EMA | 9, 13, 20 | `indicators.py` | `calculate_ema()` |
| ADX (+DI/-DI) | 14 | `orchestration.py` | `calculate_adx()` |
| Williams %R | 14 | `indicators.py` | `williams_r()` |
| VWAP (typical price proxy) | 20 | `orchestration.py` | `calculate_typical_price_ma()` |
| CPR (Central Pivot Range) | daily | `indicators.py` | `calculate_cpr()` |
| Traditional Pivots | daily | `indicators.py` | `calculate_traditional_pivots()` |
| Camarilla Pivots | daily | `indicators.py` | `calculate_camarilla_pivots()` |
| Momentum (dual-EMA gate) | 9/13 | `indicators.py` | `momentum_ok()` |
| CPR Width classification | daily | `indicators.py` | `classify_cpr_width()` |
| ECI Bias | 20 | `indicators.py` | `eci_bias()` |

### 1.4 Signal Detection & Entry Logic

| Feature | Current File | Functions/Classes |
|---------|-------------|-------------------|
| Supertrend pullback detection | `st_pullback_cci.py` | `check_entry_signal()`, `PullbackTracker` |
| CCI rejection trigger | `st_pullback_cci.py` | within `check_entry_signal()` |
| SL/TG/PT computation | `st_pullback_cci.py` | `compute_stop_loss()`, `compute_profit_target()`, `compute_trailing_target()` |
| 100-pt entry scoring | `entry_logic.py` | `check_entry_condition()` |
| Pivot acceptance/rejection | `signals.py` | `detect_signal()`, `_detect_pivot_accept_reject()` |
| Tilt state (BULLISH/BEARISH) | `signals.py` | `compute_tilt_state()` |
| Opening range detection | `signals.py` | `get_opening_range()` |
| Trend continuation | `signals.py` | `TrendContinuationState` |
| Compression breakout | `compression_detector.py` | `CompressionState.update()` |
| Reversal detection | `reversal_detector.py` | `detect_reversal()` |
| Failed breakout detection | `failed_breakout_detector.py` | `detect_failed_breakout()` |
| Demand/supply zones | `zone_detector.py` | `detect_zones()`, `detect_zone_revisit()` |

### 1.5 Exit Management

| Feature | Current File | Functions/Classes |
|---------|-------------|-------------------|
| Exit precedence engine | `execution.py` | `check_exit_condition()` |
| HFT exit (DTS, momentum, vol) | `option_exit_manager.py` | `OptionExitManager.check_exit()` |
| Position lifecycle | `position_manager.py` | `PositionManager.update()`, `ExitDecision` |
| Scalp PT/SL (fixed points) | `execution.py` | constants + logic in `check_exit_condition()` |
| Partial TG exit (50% qty) | `execution.py` | `PARTIAL_TG_QTY_FRAC` logic |
| ATR-scaled trailing stop | `execution.py` | trail stop logic per regime |
| Composite exit score | `option_exit_manager.py` | `_check_composite_exit_score()` |
| Theta decay gate | `option_exit_manager.py` | theta_decay_bars logic |
| Time exit | `execution.py` | `DEFAULT_TIME_EXIT_CANDLES` |
| Oscillator relief override | `execution.py` | S4/R4 break logic |

### 1.6 Context & Regime

| Feature | Current File | Functions/Classes |
|---------|-------------|-------------------|
| Regime context (frozen) | `regime_context.py` | `RegimeContext`, `compute_regime_context()` |
| ATR regime classification | `regime_context.py` | `classify_atr_regime()` |
| ADX tier classification | `regime_context.py` | `classify_adx_tier()` |
| Day-type classification | `daily_sentiment.py`, `day_type.py` | `classify_day_type()`, `compute_intraday_sentiment()` |
| Daily sentiment scoring | `daily_sentiment.py` | `compute_daily_sentiment()` |
| VIX context | `volatility_context.py` | `VIXContext`, `refresh()`, `get_vix_tier()` |

### 1.7 Contract & Expiry Management

| Feature | Current File | Functions/Classes |
|---------|-------------|-------------------|
| Contract metadata cache | `contract_metadata.py` | `ContractMetadataCache` |
| Lot size lookup | `contract_metadata.py` | `get_lot_size()` |
| Expiry lookup | `contract_metadata.py` | `get_expiry()` |
| Valid contracts filter | `contract_metadata.py` | `get_valid_contracts()` |
| Expiry roll management | `expiry_manager.py` | `ExpiryManager.check_roll()` |

### 1.8 Greeks & Option Analytics

| Feature | Current File | Functions/Classes |
|---------|-------------|-------------------|
| BSM Greeks (δ, γ, θ, ν) | `greeks_calculator.py` | `get_greeks()` → `GreeksResult` |
| Implied volatility | `greeks_calculator.py` | IV back-solve from market premium |

### 1.9 Micro-structure

| Feature | Current File | Functions/Classes |
|---------|-------------|-------------------|
| Tick-rate pulse detection | `pulse_module.py` | `PulseModule.on_tick()`, `get_pulse()` → `PulseMetrics` |
| Pivot reaction engine | `pivot_reaction_engine.py` | `PivotLevel`, `PivotInteraction`, `PivotCluster` |

### 1.10 Analytics & Dashboard

| Feature | Current File | Functions/Classes |
|---------|-------------|-------------------|
| Log parsing → trades | `log_parser.py` | `parse()` → `SessionSummary` |
| Text/HTML report generation | `dashboard.py` | `generate_full_report()`, `write_text_report()` |
| EOD dashboard CLI | `eod_dashboard.py` | CLI wrapper for dashboard |
| Trade CSV analysis | `analyze_trades.py` | standalone script |
| Replay validation | `replay_analyzer_v7.py` | multi-day backtesting |

### 1.11 Order Execution Modes

| Mode | Current File | Functions |
|------|-------------|-----------|
| REPLAY (backtest) | `execution.py`, `run_replay_v7.py` | simulated fills from historical candles |
| PAPER (paper trading) | `execution.py` | live WebSocket, no real money, slippage modeled |
| LIVE (real trading) | `execution.py` | routes to broker adapter |

---

## 2. Target Library Architecture

```
tbot_core/
├── __init__.py                    # Public API exports
│
├── broker/                        # ENGINE 1: Broker API
│   ├── __init__.py
│   ├── base.py                    # BrokerAdapter ABC
│   ├── fyers.py                   # FyersAdapter + FyersDataFeed
│   ├── zerodha.py                 # ZerodhaKiteAdapter
│   ├── angel.py                   # AngelOneAdapter
│   ├── ccxt_adapter.py            # CcxtAdapter (crypto)
│   ├── factory.py                 # build_broker_adapter()
│   └── paper.py                   # PaperBroker (simulated fills)
│
├── data/                          # ENGINE 2: Data Pipeline
│   ├── __init__.py
│   ├── candle_builder.py          # Tick → OHLCV aggregation (any timeframe)
│   ├── candle_store.py            # In-memory candle buffer + resampler
│   ├── history.py                 # Historical data fetcher (broker-agnostic)
│   ├── tick_db.py                 # SQLite tick persistence
│   ├── feed.py                    # WebSocket tick streaming (broker-agnostic)
│   └── timeframes.py              # Timeframe enum: 1m, 3m, 5m, 15m, 30m, 1H, D
│
├── indicators/                    # ENGINE 3: Indicators
│   ├── __init__.py                # All indicators importable from here
│   ├── trend.py                   # Supertrend, EMA, ADX
│   ├── momentum.py                # RSI, CCI, Williams %R, momentum_ok()
│   ├── volatility.py              # ATR, daily_atr, ATR regime classification
│   ├── pivots.py                  # CPR, Traditional, Camarilla, CPR width
│   ├── volume.py                  # VWAP, typical price MA
│   └── patterns.py                # NEW: Candlestick reversal patterns
│
├── signals/                       # ENGINE 4: Signal Detection
│   ├── __init__.py
│   ├── supertrend_pullback.py     # ST pullback + CCI rejection
│   ├── pivot_signals.py           # Pivot acceptance/rejection/breakout
│   ├── compression.py             # Compression → expansion breakout
│   ├── reversal.py                # EMA stretch + pivot zone reversal
│   ├── failed_breakout.py         # Failed breakout detection
│   ├── zone_signals.py            # Demand/supply zone revisit
│   ├── tilt.py                    # Tilt state computation
│   └── opening_range.py           # Opening range breakout/fade
│
├── entry/                         # ENGINE 5: Entry Logic
│   ├── __init__.py
│   ├── scoring.py                 # 100-pt scoring framework (configurable weights)
│   ├── quality_gate.py            # ST bias/slope/OSC gates
│   ├── filters.py                 # Time-of-day, RSI exhaustion, ATR regime
│   └── regime_modifiers.py        # Day-type threshold adjustments
│
├── exit/                          # ENGINE 6: Exit Management
│   ├── __init__.py
│   ├── exit_engine.py             # Prioritized exit precedence engine
│   ├── hft_exits.py               # DTS, momentum exhaustion, vol mean-reversion
│   ├── trailing.py                # ATR-scaled trailing stop
│   ├── scalp_exits.py             # Fixed-point scalp PT/SL
│   ├── partial_booking.py         # Partial TG exit (configurable fraction)
│   ├── time_exit.py               # Max hold / EOD forced exit
│   └── composite_score.py         # Composite exit score gate
│
├── position/                      # ENGINE 7: Position & Order Management
│   ├── __init__.py
│   ├── position_manager.py        # Single-position lifecycle
│   ├── trade_classes.py           # ScalpTrade / TrendTrade dataclasses
│   ├── risk_manager.py            # Max trades, daily loss, drawdown limits
│   └── order_router.py            # Route orders to broker (LIVE/PAPER/REPLAY)
│
├── options/                       # ENGINE 8: Options-Specific
│   ├── __init__.py
│   ├── chain.py                   # Option chain fetcher + strike selection
│   ├── greeks.py                  # BSM Greeks calculator
│   ├── contract_meta.py           # Contract metadata + lot size
│   ├── expiry.py                  # Expiry manager + roll logic
│   └── moneyness.py               # ITM/ATM/OTM selection logic
│
├── context/                       # ENGINE 9: Market Context
│   ├── __init__.py
│   ├── regime.py                  # RegimeContext frozen dataclass + builder
│   ├── day_type.py                # Day classification (6 types)
│   ├── daily_sentiment.py         # Pre-session sentiment scoring
│   ├── vix.py                     # India VIX context + tiers
│   └── pulse.py                   # Tick-rate micro-structure
│
├── analytics/                     # ENGINE 10: Analytics & Dashboard
│   ├── __init__.py
│   ├── log_parser.py              # Structured log → Trade/SessionSummary
│   ├── dashboard.py               # Text/HTML report generation
│   ├── trade_analyzer.py          # Win rate, P&L, profit factor, Sharpe
│   └── replay.py                  # Multi-day replay/backtest harness
│
├── screener/                      # ENGINE 11: Stock Screener (NEW)
│   ├── __init__.py
│   ├── scanner.py                 # Multi-symbol screener framework
│   ├── filters.py                 # Filter by indicator thresholds
│   └── ranker.py                  # Score & rank symbols
│
└── config/                        # Shared Configuration
    ├── __init__.py
    ├── defaults.py                # Default constants (SL, PT, periods, etc.)
    └── timeframes.py              # Timeframe definitions
```

---

## 3. Engine Breakdown

### ENGINE 1: Broker API (`tbot_core.broker`)

**Purpose**: Unified interface to fetch data + place orders across brokers.

```python
# Abstract interface
class BrokerAdapter(ABC):
    def authenticate(self, credentials: dict) -> bool
    def get_historical_candles(self, symbol, timeframe, from_dt, to_dt) -> pd.DataFrame
    def get_option_chain(self, underlying, expiry) -> pd.DataFrame
    def get_symbols(self, exchange, segment) -> list[str]
    def get_ltp(self, symbols: list[str]) -> dict[str, float]
    def place_order(self, order: OrderRequest) -> OrderResponse
    def modify_order(self, order_id, modifications) -> OrderResponse
    def cancel_order(self, order_id) -> bool
    def get_positions(self) -> list[Position]
    def get_orderbook(self) -> list[Order]
    def subscribe_ticks(self, symbols, callback) -> None
    def unsubscribe_ticks(self, symbols) -> None
```

**What's new vs existing**:
- `get_historical_candles()` — broker-agnostic (currently Fyers-only in `orchestration.py`)
- `get_option_chain()` — standardized across brokers (currently Fyers-only in `setup.py`)
- `get_symbols()` — symbol listing for screeners (currently only `contract_metadata.py`)
- `modify_order()`, `cancel_order()` — missing in current codebase
- `get_positions()`, `get_orderbook()` — missing (needed for options selling)

### ENGINE 2: Data Pipeline (`tbot_core.data`)

**Purpose**: Multi-timeframe candle building from ticks or history, any interval.

```python
class CandleStore:
    def add_tick(self, timestamp, price, volume) -> list[Candle]  # returns completed candles
    def get_candles(self, timeframe: Timeframe) -> pd.DataFrame
    def resample(self, source_tf: Timeframe, target_tf: Timeframe) -> pd.DataFrame

class Timeframe(Enum):
    M1 = "1m"
    M3 = "3m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1H"
    D = "D"
```

**What's new**:
- 1m, 5m, 30m, 1H candles (currently only 3m and 15m)
- Daily candle resampling from intraday
- Generic `resample()` from any source to target timeframe

### ENGINE 3: Indicators (`tbot_core.indicators`)

**Purpose**: Every indicator independently callable with any DataFrame.

```python
# All take a standard OHLCV DataFrame and return Series/float
from tbot_core.indicators import (
    supertrend, atr, rsi, cci, ema, adx, williams_r, vwap,
    cpr, traditional_pivots, camarilla_pivots, classify_cpr_width,
    momentum_ok, daily_atr, resolve_atr,
    # NEW: Candlestick patterns
    detect_doji, detect_hammer, detect_engulfing, detect_morning_star,
    detect_evening_star, detect_three_white_soldiers, detect_three_black_crows,
    detect_shooting_star, detect_spinning_top, detect_marubozu,
    detect_harami, detect_piercing_line, detect_dark_cloud_cover,
    detect_tweezer_top, detect_tweezer_bottom,
    scan_all_patterns,  # Returns dict of all detected patterns
)
```

**What's new**:
- **15 candlestick reversal patterns** (currently zero)
- `scan_all_patterns()` — batch detection for screener use
- All indicators accept any timeframe DataFrame (not hardcoded to 3m)

### ENGINE 4: Signal Detection (`tbot_core.signals`)

**Purpose**: Each signal type independently callable.

```python
from tbot_core.signals import (
    check_supertrend_pullback,   # ST pullback + CCI rejection
    detect_pivot_signal,         # Pivot acceptance/rejection/breakout
    detect_compression_breakout, # 3-bar compression → expansion
    detect_reversal,             # EMA stretch + pivot zone reversal
    detect_failed_breakout,      # Camarilla failed breakout
    detect_zone_revisit,         # Demand/supply zone revisit
    compute_tilt_state,          # Bullish/bearish tilt
    detect_opening_range_break,  # OR breakout/fade
)
```

### ENGINE 5: Entry Logic (`tbot_core.entry`)

**Purpose**: Configurable scoring framework.

```python
class EntryScorer:
    def __init__(self, weights: dict = DEFAULT_WEIGHTS, threshold: int = 50):
        ...
    def score(self, candles, indicators, signal, context=None) -> EntryResult:
        """Returns breakdown dict + total score + pass/fail"""
    def add_dimension(self, name, weight, scorer_fn):
        """Register custom scoring dimension"""

class QualityGate:
    def __init__(self, gates: list[GateFn] = DEFAULT_GATES):
        ...
    def check(self, candles, indicators, signal) -> GateResult:
        """Run all gates, return first failure or pass"""
```

**What's new**:
- Pluggable scoring weights (currently hardcoded in `entry_logic.py`)
- Custom dimension registration (strategy-specific scoring)
- Gate composition (mix and match pre-filters)

### ENGINE 6: Exit Management (`tbot_core.exit`)

**Purpose**: Composable exit rules.

```python
class ExitEngine:
    def __init__(self, rules: list[ExitRule] = None):
        ...
    def add_rule(self, rule: ExitRule, priority: int):
        """Register an exit rule with priority"""
    def evaluate(self, position, candle, indicators) -> ExitDecision | None:
        """Evaluate all rules in priority order"""

# Pre-built rules (each independently usable)
class StopLossRule(ExitRule): ...
class TrailingStopRule(ExitRule): ...
class ScalpPTRule(ExitRule): ...
class PartialBookingRule(ExitRule): ...
class TimeExitRule(ExitRule): ...
class DynamicTrailRule(ExitRule): ...       # HFT
class MomentumExhaustionRule(ExitRule): ... # HFT
class CompositeScoreRule(ExitRule): ...
```

**What's new**:
- Composable rule system (currently monolithic `check_exit_condition()`)
- Each rule independently testable and reusable
- Priority-based evaluation (same semantics as current, but pluggable)

### ENGINE 7: Position & Order Management (`tbot_core.position`)

**Purpose**: Position lifecycle + risk controls.

```python
class PositionManager:
    def open(self, signal, broker, mode) -> Position
    def update(self, position, candle) -> ExitDecision | None
    def close(self, position, reason, broker) -> ClosedTrade

class RiskManager:
    def can_trade(self) -> bool           # daily limits
    def check_drawdown(self) -> bool      # max drawdown
    def get_position_size(self, signal, account) -> int  # sizing
```

### ENGINE 8: Options-Specific (`tbot_core.options`)

**Purpose**: Everything needed for options buying AND selling.

```python
from tbot_core.options import (
    get_option_chain,        # Standardized chain across brokers
    select_strike,           # ITM/ATM/OTM selection
    get_greeks,              # BSM Greeks
    get_lot_size,            # Contract metadata
    get_expiry,              # Expiry dates
    check_roll,              # Auto-roll near expiry
    # NEW for options selling:
    get_max_pain,            # Max pain price calculation
    get_pcr,                 # Put-call ratio
    get_oi_analysis,         # Open interest analysis
    iron_condor_legs,        # Multi-leg strategy construction
    straddle_legs,           # Straddle/strangle construction
    calculate_margin,        # Margin requirement estimation
)
```

**What's new for options selling**:
- `get_max_pain()` — max pain calculation from OI data
- `get_pcr()` — put-call ratio
- `get_oi_analysis()` — OI buildup/unwinding
- Multi-leg strategy helpers (`iron_condor_legs`, `straddle_legs`)
- `calculate_margin()` — margin estimation

### ENGINE 9: Market Context (`tbot_core.context`)

**Purpose**: Regime detection and market classification.

```python
from tbot_core.context import (
    RegimeContext, compute_regime_context,
    classify_day_type,
    compute_daily_sentiment,
    compute_intraday_sentiment,
    VIXContext,
    PulseMetrics,
)
```

### ENGINE 10: Analytics (`tbot_core.analytics`)

**Purpose**: Trade analysis and backtesting.

```python
from tbot_core.analytics import (
    parse_session_log,       # Log → SessionSummary
    generate_report,         # Text/HTML dashboard
    run_replay,              # Multi-day backtest
    calculate_sharpe,        # Risk-adjusted returns
    calculate_max_drawdown,  # Peak-to-trough
    trade_journal,           # Trade-by-trade export
)
```

### ENGINE 11: Stock Screener (NEW — `tbot_core.screener`)

**Purpose**: Scan multiple symbols with indicator/pattern filters.

```python
class Screener:
    def __init__(self, broker: BrokerAdapter, symbols: list[str]):
        ...
    def add_filter(self, filter_fn: Callable[[pd.DataFrame], bool]):
        """Add a filter condition"""
    def scan(self, timeframe: Timeframe = Timeframe.D) -> list[ScreenerResult]:
        """Fetch data for all symbols, apply filters, return matches"""

# Pre-built filters
def filter_supertrend_bullish(df) -> bool
def filter_rsi_oversold(df, threshold=30) -> bool
def filter_cci_extreme(df, threshold=200) -> bool
def filter_compression_detected(df) -> bool
def filter_near_pivot(df, levels, buffer_pct=0.5) -> bool
def filter_candlestick_pattern(df, pattern="hammer") -> bool
def filter_adx_trending(df, min_adx=25) -> bool
def filter_volume_breakout(df, mult=2.0) -> bool
```

---

## 4. Detailed Module Mapping (Old → New)

| Current File | Target Location | Migration Notes |
|-------------|----------------|-----------------|
| `config.py` | `tbot_core/config/defaults.py` | Extract constants, make configurable |
| `setup.py` | `tbot_core/broker/fyers.py` | Auth logic only, no global state |
| `indicators.py` | `tbot_core/indicators/trend.py`, `momentum.py`, `volatility.py`, `pivots.py` | Split by category |
| `orchestration.py` | `tbot_core/indicators/` + `tbot_core/data/history.py` | Split indicator calc from data fetch |
| `st_pullback_cci.py` | `tbot_core/signals/supertrend_pullback.py` + `tbot_core/broker/base.py` | Separate signal from broker adapters |
| `entry_logic.py` | `tbot_core/entry/scoring.py` + `filters.py` | Make weights configurable |
| `signals.py` | `tbot_core/signals/pivot_signals.py` + `tilt.py` + `opening_range.py` | Split by signal type |
| `execution.py` | `tbot_core/exit/exit_engine.py` + `tbot_core/position/order_router.py` | Decompose monolith |
| `option_exit_manager.py` | `tbot_core/exit/hft_exits.py` + `composite_score.py` | Split by exit mechanism |
| `position_manager.py` | `tbot_core/position/position_manager.py` | Clean up, add multi-position |
| `trade_classes.py` | `tbot_core/position/trade_classes.py` | Direct move |
| `compression_detector.py` | `tbot_core/signals/compression.py` | Direct move |
| `reversal_detector.py` | `tbot_core/signals/reversal.py` | Direct move |
| `failed_breakout_detector.py` | `tbot_core/signals/failed_breakout.py` | Direct move |
| `zone_detector.py` | `tbot_core/signals/zone_signals.py` | Direct move |
| `regime_context.py` | `tbot_core/context/regime.py` | Direct move |
| `daily_sentiment.py` | `tbot_core/context/daily_sentiment.py` | Direct move |
| `day_type.py` | `tbot_core/context/day_type.py` | Direct move |
| `volatility_context.py` | `tbot_core/context/vix.py` | Direct move |
| `pulse_module.py` | `tbot_core/context/pulse.py` | Direct move |
| `pivot_reaction_engine.py` | `tbot_core/signals/pivot_signals.py` | Merge with pivot signals |
| `contract_metadata.py` | `tbot_core/options/contract_meta.py` | Direct move |
| `expiry_manager.py` | `tbot_core/options/expiry.py` | Direct move |
| `greeks_calculator.py` | `tbot_core/options/greeks.py` | Direct move |
| `candle_builder.py` | `tbot_core/data/candle_builder.py` | Generalize to all timeframes |
| `market_data.py` | `tbot_core/data/candle_store.py` | Rename, add multi-TF |
| `data_feed.py` | `tbot_core/data/feed.py` | Make broker-agnostic |
| `tickdb.py` | `tbot_core/data/tick_db.py` | Direct move |
| `log_parser.py` | `tbot_core/analytics/log_parser.py` | Direct move |
| `dashboard.py` | `tbot_core/analytics/dashboard.py` | Direct move |
| `broker_init.py` | `tbot_core/broker/factory.py` | Direct move |
| `order_utils.py` | `tbot_core/position/order_router.py` | Merge |
| `main.py` | stays in root (strategy-level) | Uses library imports |

---

## 5. New Capabilities to Add

### 5.1 Candlestick Reversal Patterns (`tbot_core/indicators/patterns.py`)

| Pattern | Detection Logic | Use Case |
|---------|----------------|----------|
| Doji | open ≈ close (body < 10% of range) | Indecision, reversal |
| Hammer | lower wick ≥ 2× body, small upper wick | Bullish reversal |
| Inverted Hammer | upper wick ≥ 2× body, small lower wick | Bullish reversal |
| Shooting Star | upper wick ≥ 2× body at top of trend | Bearish reversal |
| Engulfing (Bull) | current body engulfs prior body, bullish | Strong reversal |
| Engulfing (Bear) | current body engulfs prior body, bearish | Strong reversal |
| Morning Star | 3-bar: bearish → doji → bullish | Bullish reversal |
| Evening Star | 3-bar: bullish → doji → bearish | Bearish reversal |
| Three White Soldiers | 3 consecutive bullish bodies, each higher | Strong bullish |
| Three Black Crows | 3 consecutive bearish bodies, each lower | Strong bearish |
| Harami (Bull) | small body inside prior bearish body | Reversal potential |
| Harami (Bear) | small body inside prior bullish body | Reversal potential |
| Piercing Line | 2-bar: bearish then bullish closes above midpoint | Bullish reversal |
| Dark Cloud Cover | 2-bar: bullish then bearish closes below midpoint | Bearish reversal |
| Tweezer Top/Bottom | 2-bar with matching highs/lows | Reversal |

### 5.2 Multi-Timeframe Support

Currently only 3m and 15m. Library will support:
- **1m** — scalping, HFT entry refinement
- **3m** — primary intraday (current)
- **5m** — alternative intraday
- **15m** — structure/bias (current)
- **30m** — swing intraday
- **1H** — positional intraday / swing
- **D** — daily levels, swing strategies

### 5.3 Options Selling Capabilities

| Feature | Description |
|---------|-------------|
| Max Pain Calculator | Compute max pain from OI data |
| PCR (Put-Call Ratio) | Compute from option chain |
| OI Analysis | Buildup (long/short), unwinding patterns |
| Multi-leg Construction | Iron condor, straddle, strangle, butterfly legs |
| Margin Estimation | Approximate margin from SPAN-like model |
| Premium Decay Tracker | Track theta decay across positions |
| Greeks Portfolio | Aggregate Greeks across multi-leg positions |

### 5.4 Stock Screener Framework

| Feature | Description |
|---------|-------------|
| Multi-symbol data fetch | Batch fetch OHLCV for N symbols |
| Indicator filter chain | Composable AND/OR filter pipeline |
| Pattern scanner | Detect candlestick patterns across symbols |
| Rank by score | Composite scoring (indicator + pattern + volume) |
| Alert framework | Trigger callback when filter matches |
| Watchlist management | Add/remove symbols from watchlists |

### 5.5 Stock Trading Support

| Feature | Description |
|---------|-------------|
| Equity order routing | CNC/MIS product types |
| Position sizing (% capital) | Risk-based position sizing |
| Multi-position support | Hold multiple open positions |
| Portfolio-level risk | Aggregate exposure management |
| Swing trade support | Multi-day hold with EOD checks |

---

## 6. Execution Plan (Phased)

### Phase 1: Foundation (Week 1-2)
> Create package structure + migrate core modules with zero behavior change

| # | Task | Files | Priority |
|---|------|-------|----------|
| 1.1 | Create `tbot_core/` package skeleton with all subdirectories | all `__init__.py` | P0 |
| 1.2 | Extract and split `indicators.py` → `trend.py`, `momentum.py`, `volatility.py`, `pivots.py`, `volume.py` | indicators/* | P0 |
| 1.3 | Extract `orchestration.py` indicator functions → merge into `indicators/` | indicators/* | P0 |
| 1.4 | Move `config.py` constants → `config/defaults.py` (keep backward compat) | config/* | P0 |
| 1.5 | Extract `BrokerAdapter` ABC + all adapters → `broker/` | broker/* | P0 |
| 1.6 | Move data pipeline: `candle_builder.py`, `market_data.py`, `tickdb.py` → `data/` | data/* | P0 |
| 1.7 | Write import compatibility layer (old imports still work) | root `__init__.py` stubs | P1 |

### Phase 2: Signal & Entry Engines (Week 2-3)
> Decouple signal detection and entry scoring from execution.py

| # | Task | Files | Priority |
|---|------|-------|----------|
| 2.1 | Extract `st_pullback_cci.py` signal logic → `signals/supertrend_pullback.py` | signals/* | P0 |
| 2.2 | Extract `signals.py` → `signals/pivot_signals.py` + `tilt.py` + `opening_range.py` | signals/* | P0 |
| 2.3 | Move `compression_detector.py` → `signals/compression.py` | signals/* | P0 |
| 2.4 | Move `reversal_detector.py` → `signals/reversal.py` | signals/* | P0 |
| 2.5 | Move `failed_breakout_detector.py` → `signals/failed_breakout.py` | signals/* | P1 |
| 2.6 | Move `zone_detector.py` → `signals/zone_signals.py` | signals/* | P1 |
| 2.7 | Extract `entry_logic.py` → `entry/scoring.py` + `filters.py` with configurable weights | entry/* | P0 |
| 2.8 | Extract quality gate from `execution.py` → `entry/quality_gate.py` | entry/* | P0 |

### Phase 3: Exit & Position Engines (Week 3-4)
> Decompose the monolithic execution.py exit logic

| # | Task | Files | Priority |
|---|------|-------|----------|
| 3.1 | Extract `check_exit_condition()` → `exit/exit_engine.py` with rule composition | exit/* | P0 |
| 3.2 | Extract `option_exit_manager.py` → `exit/hft_exits.py` + `composite_score.py` | exit/* | P0 |
| 3.3 | Extract trailing stop logic → `exit/trailing.py` | exit/* | P0 |
| 3.4 | Extract scalp PT/SL → `exit/scalp_exits.py` | exit/* | P1 |
| 3.5 | Extract partial booking → `exit/partial_booking.py` | exit/* | P1 |
| 3.6 | Move `position_manager.py` → `position/position_manager.py` | position/* | P0 |
| 3.7 | Move `trade_classes.py` → `position/trade_classes.py` | position/* | P1 |
| 3.8 | Create `position/risk_manager.py` (extract from execution.py) | position/* | P0 |
| 3.9 | Create `position/order_router.py` (LIVE/PAPER/REPLAY dispatch) | position/* | P0 |

### Phase 4: Options & Context Engines (Week 4-5)
> Options-specific modules + market context

| # | Task | Files | Priority |
|---|------|-------|----------|
| 4.1 | Move `contract_metadata.py` + `expiry_manager.py` → `options/` | options/* | P0 |
| 4.2 | Move `greeks_calculator.py` → `options/greeks.py` | options/* | P0 |
| 4.3 | Create `options/chain.py` — standardized option chain fetcher | options/* | P1 |
| 4.4 | Create `options/moneyness.py` — ITM/ATM/OTM strike selection | options/* | P1 |
| 4.5 | Move `regime_context.py` → `context/regime.py` | context/* | P0 |
| 4.6 | Move `daily_sentiment.py` + `day_type.py` → `context/` | context/* | P0 |
| 4.7 | Move `volatility_context.py` → `context/vix.py` | context/* | P1 |
| 4.8 | Move `pulse_module.py` → `context/pulse.py` | context/* | P1 |

### Phase 5: New Capabilities (Week 5-7)
> Add missing features: patterns, multi-TF, screener, options selling

| # | Task | Files | Priority |
|---|------|-------|----------|
| 5.1 | Implement 15 candlestick patterns → `indicators/patterns.py` | indicators/* | P0 |
| 5.2 | Implement multi-timeframe candle resampling (1m→D) → `data/timeframes.py` | data/* | P0 |
| 5.3 | Create `screener/scanner.py` — multi-symbol screener framework | screener/* | P1 |
| 5.4 | Create `screener/filters.py` — pre-built indicator/pattern filters | screener/* | P1 |
| 5.5 | Create `screener/ranker.py` — composite scoring + ranking | screener/* | P2 |
| 5.6 | Create `options/chain.py` — max pain, PCR, OI analysis | options/* | P1 |
| 5.7 | Create multi-leg helpers — iron condor, straddle, strangle | options/* | P2 |
| 5.8 | Add equity order support (CNC/MIS) to broker adapters | broker/* | P1 |

### Phase 6: Analytics & Integration (Week 7-8)
> Dashboard, replay, and top-level API

| # | Task | Files | Priority |
|---|------|-------|----------|
| 6.1 | Move `log_parser.py` + `dashboard.py` → `analytics/` | analytics/* | P1 |
| 6.2 | Create `analytics/replay.py` — generic backtest harness | analytics/* | P1 |
| 6.3 | Create `analytics/trade_analyzer.py` — Sharpe, drawdown, etc. | analytics/* | P1 |
| 6.4 | Write `tbot_core/__init__.py` — clean public API exports | __init__.py | P0 |
| 6.5 | Write migration guide for existing `main.py` | docs | P2 |
| 6.6 | Update existing test suites to use new import paths | tests/* | P0 |

---

## 7. API Surface — Key Callables

### 7.1 One-liner Indicator Calls

```python
from tbot_core.indicators import rsi, cci, atr, supertrend, ema, adx, williams_r
from tbot_core.indicators import cpr, traditional_pivots, camarilla_pivots
from tbot_core.indicators import detect_hammer, detect_engulfing, scan_all_patterns

# Each returns a Series or float — works with any OHLCV DataFrame
rsi_series = rsi(df, period=14)
cci_series = cci(df, period=20)
atr_value  = atr(df, period=14)
st_line, st_bias, st_slope = supertrend(df, period=10, multiplier=3.0)
patterns = scan_all_patterns(df)  # {"hammer": [idx1, idx2], "engulfing_bull": [...]}
```

### 7.2 Broker Data Fetch

```python
from tbot_core.broker import build_broker_adapter
from tbot_core.data import Timeframe

broker = build_broker_adapter("fyers", credentials={...})
df_5m  = broker.get_historical_candles("NSE:NIFTY50-INDEX", Timeframe.M5, from_dt, to_dt)
df_15m = broker.get_historical_candles("NSE:NIFTY50-INDEX", Timeframe.M15, from_dt, to_dt)
df_1h  = broker.get_historical_candles("NSE:NIFTY50-INDEX", Timeframe.H1, from_dt, to_dt)
chain  = broker.get_option_chain("NIFTY", expiry_date)
symbols = broker.get_symbols("NSE", "EQ")  # all NSE equity symbols
```

### 7.3 Signal Detection

```python
from tbot_core.signals import (
    check_supertrend_pullback, detect_compression_breakout,
    detect_reversal, detect_pivot_signal
)

signal = check_supertrend_pullback(df_3m, df_15m, config)
comp   = detect_compression_breakout(df_15m)
rev    = detect_reversal(df_3m, camarilla_levels, atr_val)
pivot  = detect_pivot_signal(df_3m, pivot_levels)
```

### 7.4 Entry Scoring

```python
from tbot_core.entry import EntryScorer

scorer = EntryScorer(
    weights={"trend_alignment": 20, "rsi_score": 10, "cci_score": 15, ...},
    threshold=55
)
result = scorer.score(candles_3m, indicators, signal)
if result.passed:
    # Enter trade
```

### 7.5 Exit Engine

```python
from tbot_core.exit import ExitEngine, StopLossRule, TrailingStopRule, TimeExitRule

engine = ExitEngine()
engine.add_rule(StopLossRule(pct=0.45), priority=1)
engine.add_rule(TrailingStopRule(atr_mult=1.5), priority=2)
engine.add_rule(TimeExitRule(max_bars=16), priority=10)

decision = engine.evaluate(position, current_candle, indicators)
if decision and decision.should_exit:
    # Exit trade
```

### 7.6 Options Operations

```python
from tbot_core.options import get_greeks, select_strike, get_option_chain

chain = get_option_chain(broker, "NIFTY", expiry)
strike = select_strike(chain, spot_price, "CE", moneyness="ITM")
greeks = get_greeks(spot_price, strike.strike_price, days_to_expiry, "CE", premium)
print(f"Delta: {greeks.delta}, Theta: {greeks.theta}, IV: {greeks.iv_pct}%")
```

### 7.7 Stock Screener

```python
from tbot_core.screener import Screener
from tbot_core.screener.filters import (
    filter_supertrend_bullish, filter_rsi_oversold, filter_candlestick_pattern
)

screener = Screener(broker, symbols=nifty_50_symbols)
screener.add_filter(filter_supertrend_bullish)
screener.add_filter(filter_rsi_oversold(threshold=35))
screener.add_filter(filter_candlestick_pattern("hammer"))

results = screener.scan(timeframe=Timeframe.D)
for r in results:
    print(f"{r.symbol}: RSI={r.indicators['rsi']:.1f}, ST={r.indicators['st_bias']}")
```

---

## 8. Strategy Builder Examples

### Example 1: Options Buying Strategy (Current Engine, Reimplemented)

```python
from tbot_core.broker import build_broker_adapter
from tbot_core.data import CandleStore, Timeframe
from tbot_core.indicators import supertrend, rsi, cci, atr, ema
from tbot_core.signals import check_supertrend_pullback
from tbot_core.entry import EntryScorer
from tbot_core.exit import ExitEngine, StopLossRule, TrailingStopRule, ScalpPTRule
from tbot_core.options import select_strike
from tbot_core.position import PositionManager, RiskManager
from tbot_core.context import compute_regime_context

broker = build_broker_adapter("fyers", credentials={...})
risk = RiskManager(max_trades=8, max_daily_loss=-15000)
pm = PositionManager(mode="PAPER")
scorer = EntryScorer(threshold=50)
exits = ExitEngine()
exits.add_rule(StopLossRule(pct=0.45), priority=1)
exits.add_rule(TrailingStopRule(atr_mult=1.5), priority=2)
exits.add_rule(ScalpPTRule(points=18), priority=3)

def on_new_candle(df_3m, df_15m, spot):
    if pm.is_open():
        decision = exits.evaluate(pm.position, df_3m.iloc[-1], indicators)
        if decision.should_exit:
            pm.close(decision.reason, broker)
    elif risk.can_trade():
        signal = check_supertrend_pullback(df_3m, df_15m, config)
        if signal:
            result = scorer.score(df_3m, indicators, signal)
            if result.passed:
                strike = select_strike(broker, "NIFTY", expiry, signal["side"])
                pm.open(signal, broker)
```

### Example 2: Options Selling Strategy (Iron Condor)

```python
from tbot_core.broker import build_broker_adapter
from tbot_core.indicators import atr, adx
from tbot_core.options import get_option_chain, get_greeks, iron_condor_legs
from tbot_core.context import classify_day_type, VIXContext

broker = build_broker_adapter("fyers", credentials={...})
vix = VIXContext(broker)
vix.refresh()

chain = get_option_chain(broker, "NIFTY", expiry)
spot = broker.get_ltp(["NSE:NIFTY50-INDEX"])["NSE:NIFTY50-INDEX"]

if vix.get_vix_tier() == "HIGH" and classify_day_type(...) == "RANGE_DAY":
    legs = iron_condor_legs(chain, spot, wing_width=100, delta_target=0.15)
    for leg in legs:
        greeks = get_greeks(spot, leg.strike, days_to_expiry, leg.option_type, leg.premium)
        print(f"{leg.strike} {leg.option_type}: Δ={greeks.delta:.2f} Θ={greeks.theta:.1f}")
    # Place multi-leg order
    broker.place_order(legs)
```

### Example 3: Equity Intraday (Supertrend + Candlestick Patterns)

```python
from tbot_core.broker import build_broker_adapter
from tbot_core.indicators import supertrend, rsi, atr, detect_hammer, detect_engulfing
from tbot_core.data import Timeframe

broker = build_broker_adapter("zerodha", credentials={...})
df = broker.get_historical_candles("NSE:RELIANCE", Timeframe.M15, from_dt, to_dt)

st_line, st_bias, st_slope = supertrend(df)
rsi_val = rsi(df).iloc[-1]
atr_val = atr(df).iloc[-1]

# Check for bullish reversal patterns at support
if st_bias.iloc[-1] == "BULLISH" and rsi_val < 40:
    if detect_hammer(df) or detect_engulfing(df, "BULL"):
        entry_price = df["close"].iloc[-1]
        sl = entry_price - 1.5 * atr_val
        target = entry_price + 2.0 * atr_val
        broker.place_order(OrderRequest("NSE:RELIANCE", "BUY", qty=10, product="MIS"))
```

### Example 4: Stock Screener (Daily Timeframe)

```python
from tbot_core.screener import Screener
from tbot_core.screener.filters import *
from tbot_core.data import Timeframe

broker = build_broker_adapter("fyers", credentials={...})
nifty_200 = broker.get_symbols("NSE", segment="EQ", index="NIFTY200")

# Bullish reversal screener
screener = Screener(broker, symbols=nifty_200)
screener.add_filter(filter_supertrend_bullish)
screener.add_filter(filter_rsi_range(30, 50))           # Oversold but recovering
screener.add_filter(filter_adx_trending(min_adx=20))    # Trend present
screener.add_filter(filter_candlestick_pattern("hammer"))
screener.add_filter(filter_near_pivot("S2"))             # Near support

results = screener.scan(timeframe=Timeframe.D)
print(f"Found {len(results)} matches:")
for r in sorted(results, key=lambda x: x.score, reverse=True):
    print(f"  {r.symbol}: Score={r.score}, RSI={r.rsi:.1f}, Pattern={r.patterns}")
```

---

## 9. Testing Strategy

### 9.1 Existing Tests to Migrate

| Current Test | New Location | Tests |
|-------------|-------------|-------|
| `test_st_pullback_cci.py` | `tests/signals/test_supertrend_pullback.py` | 35 |
| `test_exit_logic.py` | `tests/exit/test_exit_engine.py` | 71 |
| `test_phase4.py` | `tests/context/test_regime_exits.py` | 33 |
| `test_regime_context.py` | `tests/context/test_regime.py` | 36 |
| `test_compression_detector.py` | `tests/signals/test_compression.py` | 30 |
| `test_phase5_regime.py` | `tests/analytics/test_regime_dashboard.py` | 23 |
| `test_phase62_replay.py` | `tests/analytics/test_replay.py` | 26 |
| `test_phase63_regime.py` | `tests/exit/test_regime_exits.py` | 29 |
| `test_phase64_reversal.py` | `tests/signals/test_reversal.py` | TBD |
| Other test_*.py files | corresponding `tests/` subdirectory | ~80+ |

### 9.2 New Tests Required

| Module | Test Focus | Est. Tests |
|--------|-----------|------------|
| `indicators/patterns.py` | Each of 15 patterns (bull + bear cases, edge cases) | ~60 |
| `data/timeframes.py` | Resample accuracy for all 7 TFs | ~20 |
| `screener/scanner.py` | Filter composition, ranking, empty results | ~15 |
| `broker/base.py` | Adapter protocol compliance | ~10 |
| `exit/exit_engine.py` | Rule priority, composition, short-circuit | ~20 |
| `entry/scoring.py` | Custom weights, threshold overrides | ~15 |
| `options/chain.py` | Max pain, PCR, OI analysis | ~15 |

### 9.3 Test Approach

- **Unit tests**: Each indicator/signal/exit rule tested in isolation
- **Integration tests**: Signal → Entry → Exit pipeline end-to-end
- **Regression tests**: Existing 300+ tests must pass with new import paths
- **Replay tests**: Backtest P&L must match before/after refactor

---

## 10. Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Breaking existing strategy | HIGH | Backward-compatible import shims in root files |
| Regression in exit logic | HIGH | Run full replay suite before/after, compare P&L |
| State management across modules | MEDIUM | Frozen dataclasses, no module-level mutable state in library |
| Circular imports | MEDIUM | Strict dependency direction: indicators → signals → entry/exit → position |
| Performance degradation | LOW | Profile critical paths (indicator calc, exit eval) |
| Test migration effort | MEDIUM | Automated import rewriting script |

### Dependency Direction (no circular imports)

```
config ← indicators ← signals ← entry
                                   ↓
                     context ←── exit ←── position ←── broker
                                   ↑                      ↑
                               options                   data
                                   ↑
                               analytics / screener (leaf nodes)
```

---

## Summary

| Metric | Value |
|--------|-------|
| **Existing features extracted** | 82 files → 11 engines |
| **New features added** | 15 candlestick patterns, 5 new timeframes, screener, options selling |
| **Total engines** | 11 (broker, data, indicators, signals, entry, exit, position, options, context, analytics, screener) |
| **Estimated effort** | 6-8 weeks (phased) |
| **Backward compatibility** | Import shims in root files, zero breaking changes during migration |
| **Test coverage target** | 400+ tests (300 migrated + 100 new) |
