# Strategy Research Cycle Report

## Baseline Metrics (pre-pivot expansion)
- Profit factor: 1.08
- Expectancy: 0.54 pts/trade
- Avg win: 12.64 pts | Avg loss: -14.72 pts (payoff 0.86)
- Net P&L: +160.27 pts (window 2026-02-25..2026-03-10)

## Changes Implemented
- Extended traditional pivots to S/R1–S/R5.
- Extended Camarilla pivots to S/R1–S/R6.
- Added acceptance/rejection detection across traditional and Camarilla ladders with logs `[PIVOT ACCEPTED]` / `[PIVOT REJECTED]`.
- Reversal capture hook: on pivot rejection, emit `[REVERSAL ENTRY]` state for reversal path.
- Log parser now counts trend overrides, pivot events, and reversal entries.

## Validation Status
- Pivot-expanded code is in place; full replay with new logic is pending.
- Required action: rerun replays for 2026-02-25..latest with `--out reports` to regenerate trades_*.csv/json, then rebuild replay_pnl_report.md and this report with “New” metrics.

## Next Steps
1. Run batch replay:
   - `c:/Users/mohan/trading_engine/execution.py --date YYYY-MM-DD --sym NSE:NIFTY50-INDEX --db C:\SQLite\ticks\ticks_YYYY-MM-DD.db --out reports`
2. Rebuild analytics:
   - `python _build_replay_report.py`
   - `python _build_strategy_diagnostics.py` (if desired)
3. Compare baseline vs new metrics and evaluate reversal capture (counts, win rate, P&L).

## Pending Metrics
- Profit factor (new): pending
- Expectancy (new): pending
- Payoff ratio (new): pending
- Reversal trade stats: pending
