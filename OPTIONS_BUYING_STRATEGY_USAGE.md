Usage — options_buying_strategy.py
1. Quick Start (Factory)

from options_buying_strategy import create_strategy, StrategyConfig

# Default paper mode
strategy = create_strategy(mode="PAPER")

# Custom config
strategy = create_strategy(
    mode="PAPER",
    quantity=4,
    max_trades_per_day=10,
    scalp_pt_points=20.0,
)
2. Replay Mode (Backtesting)

from options_buying_strategy import create_strategy

strategy = create_strategy(mode="REPLAY")

# Single-day replay from tick database
result = strategy.run(
    mode="REPLAY",
    db_path=r"C:\SQLite\ticks\ticks_2026-03-10.db",
    date_str="2026-03-10",
    output_dir="./replay_output",
)

# Print results
summary = strategy.get_trade_summary()
print(f"Trades: {summary['total_trades']}, PnL: {summary['total_pnl']:.2f}, Win%: {summary['win_rate']:.1f}%")
CLI shortcut:


python options_buying_strategy.py --date 2026-03-10 --db "C:\SQLite\ticks\ticks_2026-03-10.db" --output-dir ./results
3. Paper Mode (Simulated Trading)
Called on every new 3m candle from your data feed loop:


from options_buying_strategy import create_strategy
import pandas as pd

strategy = create_strategy(mode="PAPER")

# Your data feed loop
def on_new_candle(candles_3m: pd.DataFrame, candles_15m: pd.DataFrame):
    result = strategy.run_paper(
        candles_3m=candles_3m,
        candles_15m=candles_15m,
        spot_price=candles_3m.iloc[-1]["close"],
    )
    if result:
        if result.get("entry"):
            print(f"Entered {result['side']} {result['type']}")
        elif result.get("exit"):
            print(f"Exited {result['side']} reason={result['reason']}")
4. Live Mode (Real Broker Orders)

from options_buying_strategy import create_strategy

strategy = create_strategy(mode="LIVE", quantity=2)

# Called from your main loop (requires setup.py loaded)
def on_new_candle(candles_3m, candles_15m):
    result = strategy.run_live(
        candles_3m=candles_3m,
        candles_15m=candles_15m,
    )
    if result and result.get("entry"):
        print(f"LIVE ORDER placed: {result['side']}")
5. Unified run() Entry Point

strategy = create_strategy()

# Routes automatically by mode
strategy.run(mode="PAPER",   candles_3m=df_3m, candles_15m=df_15m)
strategy.run(mode="LIVE",    candles_3m=df_3m, candles_15m=df_15m)
strategy.run(mode="REPLAY",  db_path="...", date_str="2026-03-10")
6. Session Management

# End-of-day
strategy.save_trades_csv(suffix="PAPER")   # → trades_options_trade_engine_2026-03-14_PAPER.csv
summary = strategy.get_trade_summary()
print(summary)
# {'total_trades': 5, 'total_pnl': 142.50, 'winners': 3, 'losers': 2, 'win_rate': 60.0}

# New day reset
strategy.reset_session()
7. Custom Config Override

from options_buying_strategy import StrategyConfig, OptionsBuyingStrategy

config = StrategyConfig(
    quantity=4,
    max_trades_per_day=6,
    max_trades_trend=4,
    max_trades_scalp=8,
    scalp_pt_points=22.0,
    scalp_sl_points=12.0,
    paper_slippage_pts=2.0,
    rr_ratio=2.5,
    trend_entry_adx_min=20.0,
    call_moneyness="ITM",
    put_moneyness="ITM",
)

strategy = OptionsBuyingStrategy(config=config)
Key Methods Reference
Method	Purpose
run(mode, ...)	Unified entry point — routes to paper/live/replay
run_paper(candles_3m, candles_15m)	Paper trading (call per candle)
run_live(candles_3m, candles_15m)	Live trading with Fyers orders
run_replay(db_path, date_str)	Offline candle-by-candle backtest
get_trade_summary()	Returns dict with trades, PnL, win rate
save_trades_csv(suffix)	Export fills to CSV
reset_session()	Clear all state for new trading day
quality_gate(...)	Trend entry quality gate (delegates to execution.py)
check_exit_condition(...)	All exit conditions with precedence
build_dynamic_levels(...)	Regime-adaptive SL/PT/TG levels


Scripts Created
run_paper.py — Paper Trading

python run_paper.py
python run_paper.py --quantity 4 --max-trades 6 --adx-min 20.0
Live WebSocket ticks, simulated order fills
Full async loop (1s interval), candle detection, startup guard
Persists restart state, saves CSV on exit/interrupt
Session summary on shutdown
run_replay.py — Offline Replay

# Single day
python run_replay.py --date 2026-03-10

# Signal-only analysis
python run_replay.py --date 2026-03-10 --signal-only

# Multi-day with aggregate stats
python run_replay.py --from 2026-03-03 --to 2026-03-10

# Custom DB path
python run_replay.py --date 2026-03-10 --db "C:\SQLite\ticks\ticks_2026-03-10.db"
No live connection needed — fully offline
Multi-day support with weekend skip + per-day summary
Aggregate stats: total PnL, win rate, avg PnL/trade, avg PnL/day
run_live.py — Live Trading

# Dry run (shows config, does NOT trade)
python run_live.py

# Start live trading (safety flag required)
python run_live.py --confirm
python run_live.py --confirm --quantity 130 --max-trades 4
Requires --confirm flag to prevent accidental runs
Real Fyers API orders, emergency exit on Ctrl+C
Risk halt on daily loss/drawdown limit
Restart state recovery for open positions


Essential Files & Directories
Runner Scripts (pick one per mode)
File	Mode	Needs Broker Auth?
run_replay.py	Offline replay	No
run_paper.py	Paper trading	Yes (Fyers OAuth)
run_live.py	Live trading	Yes (Fyers OAuth)
Strategy Layer
File	Purpose
options_buying_strategy.py	Strategy class — OptionsBuyingStrategy + StrategyConfig
tbot_core Library (34 files)

tbot_core/
├── __init__.py
├── indicators/          ← STANDALONE (no root deps)
│   ├── __init__.py
│   ├── trend.py         # supertrend, EMA, ADX
│   ├── momentum.py      # RSI, CCI, Williams %R
│   ├── volatility.py    # ATR
│   ├── pivots.py        # CPR, Traditional, Camarilla
│   ├── volume.py        # VWAP proxy
│   ├── patterns.py      # 15 candlestick patterns
│   └── builder.py       # build_indicator_dataframe()
├── broker/              ← STANDALONE
│   ├── __init__.py
│   ├── base.py          # BrokerAdapter ABC, OrderRequest/Response
│   ├── factory.py       # build_broker_adapter()
│   ├── fyers.py
│   ├── zerodha.py
│   ├── angel.py
│   ├── ccxt_adapter.py
│   └── paper.py         # PaperBroker
├── config/              ← STANDALONE
│   ├── __init__.py
│   ├── defaults.py      # All constants
│   └── timeframes.py    # Timeframe enum
├── data/                ← STANDALONE
│   ├── __init__.py
│   ├── candle_builder.py
│   ├── candle_store.py
│   ├── history.py
│   ├── feed.py
│   └── tick_db.py
├── entry/__init__.py    ← BRIDGE → entry_logic.py
├── exit/__init__.py     ← BRIDGE → option_exit_manager.py
├── position/__init__.py ← BRIDGE → position_manager.py, trade_classes.py
├── signals/__init__.py  ← BRIDGE → signals.py, reversal_detector.py, etc.
├── context/__init__.py  ← BRIDGE → regime_context.py, day_type.py, daily_sentiment.py
├── options/__init__.py  ← BRIDGE → contract_metadata.py, expiry_manager.py
├── analytics/__init__.py  ← PLACEHOLDER (Phase 6)
└── screener/__init__.py   ← PLACEHOLDER (Phase 5)
Root Modules (required by bridges + delegation)
Core Engine (always needed):

File	Imported By	Purpose
config.py	execution, signals, indicators, orchestration	All constants + symbols
execution.py	options_buying_strategy (delegation)	Quality gate, exit logic, levels, replay
indicators.py	execution, signals, orchestration	Root indicator functions
signals.py	tbot_core/signals, execution	detect_signal(), trend continuation
entry_logic.py	tbot_core/entry, signals	check_entry_condition(), scoring
option_exit_manager.py	tbot_core/exit, execution	HFT exit engine
position_manager.py	tbot_core/position, execution	Position lifecycle
trade_classes.py	tbot_core/position	ScalpTrade / TrendTrade
orchestration.py	execution, market_data	build_indicator_dataframe(), history fetch
Signal Detection:

File	Imported By
reversal_detector.py	tbot_core/signals, execution
compression_detector.py	tbot_core/signals, execution
failed_breakout_detector.py	tbot_core/signals, execution
zone_detector.py	tbot_core/signals, execution
pulse_module.py	tbot_core/signals, execution, data_feed
pivot_reaction_engine.py	signals.py
Market Context:

File	Imported By
regime_context.py	tbot_core/context, execution
day_type.py	tbot_core/context, execution, entry_logic
daily_sentiment.py	tbot_core/context, execution
volatility_context.py	execution (optional)
Options:

File	Imported By
contract_metadata.py	tbot_core/options (graceful fallback)
expiry_manager.py	tbot_core/options (graceful fallback)
Live/Paper Mode Only (not needed for replay):

File	Imported By	Purpose
setup.py	run_paper, run_live, data_feed	Fyers OAuth + session init
market_data.py	run_paper, run_live	CandleAggregator + indicator enrichment
data_feed.py	run_paper, run_live	WebSocket tick streaming
order_utils.py	data_feed	update_order_status(), map_status_code()
tickdb.py	data_feed, run_replay_v7	SQLite TickDatabase singleton
candle_builder.py	tbot_core/data (standalone copy exists)	Tick aggregation
st_pullback_cci.py	execution (broker adapters)	BrokerAdapter implementations
External Data
Path	Purpose	Required For
C:\SQLite\ticks\ticks_YYYY-MM-DD.db	SQLite tick databases	Replay mode
.env or access-YYYY-MM-DD.txt	Fyers OAuth token cache	Paper + Live modes
Python Packages (pip)

pandas numpy pendulum fyers_apiv3 pytz python-dotenv pandas_ta ccxt
Summary by Mode
Mode	Files Needed
Replay	run_replay.py + options_buying_strategy.py + tbot_core/ (34) + 20 root modules + SQLite DB
Paper	run_paper.py + options_buying_strategy.py + tbot_core/ (34) + 24 root modules + setup.py/data_feed.py
Live	run_live.py + options_buying_strategy.py + tbot_core/ (34) + 24 root modules + setup.py/data_feed.py