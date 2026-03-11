# Trend Day Amplifier Strategy Report

## Baseline Performance
- Window: 2026-02-25 to 2026-03-10 (pre-trend-amplifier changes)
- Trades: 298 | Net P&L: +160.27 pts | Win rate: 54.7%
- Profit factor: 1.08 | Expectancy: +0.54 pts/trade
- Avg win: +12.64 pts | Avg loss: -14.72 pts | Payoff ratio: 0.86
- Max drawdown: 572 pts

## Trend Detection Logic
- Trend day triggers when ADX ≥ 30, CPR width = NARROW, and early R2/S2 breakout.
- Logs `[TREND DAY DETECTED] ADX=.. CPR=narrow breakout=R2/S2`.
- Trend re-entries allowed on pullbacks with ADX ≥ 30; capped at 5 per side and logged `[TREND REENTRY]`.
- Signal conflicts in trend mode log `[SIGNAL OVERRIDE – TREND MODE]`.

## Implemented Enhancements
- Wider trailing in trend mode: trail step widened to max(existing, ATR × 1.8).
- Dual partial ladder retained: TP1 40%, TP2 30%, runner trails; regime-aware trailing (weak vs strong vs trend day).
- Trend flags carried into position state; log parser tracks trend detections, overrides, and re-entries.

## Replay Results
Pending — Python execution is currently blocked in this sandbox, so post-change replays could not be run yet.

## Trend Day Performance Comparison
- Target day: 2026-03-08 (baseline +569 pts). Post-change replay pending to validate uplift.

## Payoff Ratio Improvement
- Expected improvement from larger runner on trend days and second ladder step; verification pending replay.

## Final Strategy Metrics
- To be populated after rerun of 2026-02-25 .. 2026-03-10 with updated code.

### Next Steps to Validate
1. Enable Python execution and run: `c:/Users/mohan/trading_engine/execution.py --date YYYY-MM-DD --sym NSE:NIFTY50-INDEX --db C:\SQLite\ticks\ticks_YYYY-MM-DD.db` for 2026-02-25 .. latest.
2. Regenerate reports (`reports/trades_*.csv/json`) and rebuild dashboard to confirm trend-day amplification.
3. Update this report with “After” metrics and the 2026-03-08 trend-day comparison.
