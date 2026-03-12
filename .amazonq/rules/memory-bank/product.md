# Trading Engine - Product Overview

## Project Purpose
An automated options trading engine for NSE NIFTY50 index options that executes algorithmic trading strategies in both paper (simulation) and live modes. The system detects market signals, manages risk, and executes trades with sophisticated entry/exit logic.

## Core Value Proposition
- **Dual-Mode Operation**: Paper trading for backtesting and live trading with real broker integration (Fyers API)
- **Intelligent Entry Gating**: Multi-layer quality gates (Supertrend alignment, ADX strength, oscillator bounds, pivot analysis)
- **Adaptive Risk Management**: Dynamic SL/PT/TG levels based on ATR volatility regimes and market conditions
- **Regime-Aware Trading**: Adjusts strategy parameters based on day type (trending, range, gap, balance)
- **Scalp + Trend Modes**: Separate trade classes with distinct exit logic and position sizing
- **Real-Time Signal Detection**: Detects reversals, failed breakouts, compression breakouts, zone revisits
- **State Persistence**: Automatic restart recovery with position hydration and validation

## Key Features

### Entry Signals
- **Trend Entries**: Supertrend-aligned moves with ADX confirmation (min 18.0)
- **Scalp Entries**: Dip/rally reversals at support/resistance with pulse-rate confirmation
- **Reversal Entries**: S5/R5 extreme pivot rejections with high-confidence scoring (≥80)
- **Failed Breakout Reversals**: Entries on breakout failures with oscillator confirmation
- **Compression Breakouts**: Entries on CPR compression breakout with ATR expansion

### Exit Logic (Strict Precedence)
1. HFT Manager (highest priority override)
2. Stop Loss (with scalp/trend survivability guardrails)
3. Profit Target (PT1 partial at 40%, PT2 at 30%)
4. Target Gate (TG full exit with SL ratchet)
5. Minimum Bar Maturity (2-3 bars based on ATR)
6. Contextual Exits (ATR/CPR/Camarilla structure breaks)
7. Oscillator Exhaustion (RSI/CCI/Williams%R extremes)
8. Supertrend Flip (2 consecutive bars opposite direction)
9. Reversal Exit (3+ consecutive bars opposite candle direction)
10. Momentum Exhaustion (EMA plateau + momentum drop)
11. Time Exit (16 candles without trailing update)

### Risk Management
- **Daily Loss Limit**: -15,000 points (scaled to position size)
- **Max Drawdown**: -10,000 points
- **Trade Caps**: 12 total trades/day, 8 trend, 12 scalp
- **Scalp Cooldown**: 20 minutes after scalp exit
- **Startup Suppression**: 5 minutes post-restart (prevents false signals on stale data)
- **Oscillator Hold**: Extreme oscillator readings trigger hold timer before re-entry

### Market Context
- **Day Type Classification**: TRENDING_DAY, RANGE_DAY, GAP_DAY, BALANCE_DAY, HIGH_VOL
- **Bias Alignment**: Gap-aware entry filtering (GAP_UP favors CALL, GAP_DOWN favors PUT)
- **Tilt Governance**: Price structure (above R3+TC or below S3+BC) overrides day bias
- **Compression Detection**: Identifies CPR width (NARROW/NORMAL/WIDE) for breakout setups
- **Zone Revisit**: Detects price returning to previous support/resistance zones

## Target Users
- **Algorithmic Traders**: Seeking automated NIFTY50 options strategies
- **Quantitative Researchers**: Backtesting and strategy optimization
- **Risk Managers**: Monitoring multi-leg options positions with adaptive exits
- **Prop Trading Firms**: Scalable, rule-based execution with audit trails

## Use Cases
1. **Intraday Scalping**: 3-minute candle dip/rally reversals with 20-point targets
2. **Trend Following**: 15-minute Supertrend alignment with ATR-based SL/PT
3. **Reversal Trading**: Extreme pivot rejections (S5/R5) with high-confidence scoring
4. **Breakout Trading**: CPR compression breakouts with volatility expansion
5. **Risk Hedging**: Dual-leg (CALL+PUT) positions with independent exit logic
6. **Backtesting**: Replay mode with historical tick data for strategy validation

## Technical Stack
- **Language**: Python 3.x
- **Broker API**: Fyers API v3 (live trading)
- **Data**: Real-time WebSocket ticks + historical candles
- **Indicators**: Supertrend, ADX, RSI, CCI, Williams%R, ATR, EMA, VWAP
- **Pivots**: CPR, Traditional, Camarilla (S4/R4 daily levels)
- **Persistence**: Pickle-based state ledger with restart recovery
- **Logging**: Structured audit logs with ANSI color coding

## Performance Metrics
- **Win Rate**: Target ≥50% (positive expectancy at 1:1 R:R)
- **Profit Factor**: Target ≥1.5 (gross profit / gross loss)
- **Sharpe Ratio**: Optimized for consistent daily returns
- **Max Consecutive Losses**: Capped at 3-5 trades before cooldown
- **Drawdown Recovery**: Adaptive position sizing reduces drawdown impact
