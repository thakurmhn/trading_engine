import math
from pathlib import Path
import pandas as pd

def main():
    # Detect all dashboard trade CSVs
    trade_files = sorted(Path("reports").glob("trades_2026-*.csv"))
    rows = []
    all_trades = []
    for path in trade_files:
        date_tag = path.stem.split("_")[-1]
        df = pd.read_csv(path)
        pnl_col = "pnl_points" if "pnl_points" in df.columns else ("pnl_pts" if "pnl_pts" in df.columns else None)
        if pnl_col is None:
            continue
        pnl = df[pnl_col]
        wins = int((pnl > 0).sum())
        losses = int((pnl < 0).sum())
        total = len(df)
        wr = wins / total * 100 if total else 0.0
        rows.append({"date": date_tag, "trades": total, "wins": wins, "losses": losses,
                     "pnl": round(pnl.sum(), 2), "win_rate": round(wr, 1)})
        df["date"] = date_tag
        all_trades.append(df)

    all_df = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    pnl_series = (
        all_df["pnl_points"] if "pnl_points" in all_df.columns else
        (all_df["pnl_pts"] if "pnl_pts" in all_df.columns else pd.Series(dtype=float))
    )
    avg_win = pnl_series[pnl_series > 0].mean() if not pnl_series.empty else 0.0
    avg_loss = pnl_series[pnl_series < 0].mean() if not pnl_series.empty else 0.0
    pf = (
        pnl_series[pnl_series > 0].sum() / abs(pnl_series[pnl_series < 0].sum())
        if (not pnl_series.empty and (pnl_series < 0).any())
        else float("inf")
    )
    expectancy = 0.0
    if not pnl_series.empty:
        win_rate = (pnl_series > 0).mean()
        loss_rate = (pnl_series < 0).mean()
        expectancy = (avg_win if not math.isnan(avg_win) else 0) * win_rate
        expectancy += (avg_loss if not math.isnan(avg_loss) else 0) * loss_rate

    cum = pnl_series.cumsum() if not pnl_series.empty else pd.Series(dtype=float)
    max_dd = 0.0
    if not cum.empty:
        peak = cum.iloc[0]
        for v in cum:
            if v > peak:
                peak = v
            dd = peak - v
            if dd > max_dd:
                max_dd = dd

    exit_counts = all_df["exit_reason"].value_counts().to_dict() if not all_df.empty and "exit_reason" in all_df.columns else {}
    side_perf = all_df.groupby("side")[pnl_series.name].sum().to_dict() if (not all_df.empty and "side" in all_df.columns and not pnl_series.empty) else {}

    md = []
    md.append("# Trading System Replay Backtest Report")
    md.append("")
    md.append("## Executive Summary")
    md.append(
        f"Replay window: {rows[0]['date']} to {rows[-1]['date']} ({len(rows)} sessions). "
        f"Total trades: {len(all_df)}; Net P&L: {pnl_series.sum():+.2f} pts; "
        f"Win rate: {pnl_series.gt(0).mean()*100 if not pnl_series.empty else 0:.1f}%."
    )
    md.append(
        f"Profit factor: {pf:.2f} | Expectancy: {expectancy:.2f} pts/trade | "
        f"Avg win: {avg_win:.2f} | Avg loss: {avg_loss:.2f} | Max DD: {max_dd:.2f} pts."
    )
    md.append("")
    md.append("## Daily Performance Table")
    md.append("| Date | Trades | Wins | Losses | Win Rate | Daily P&L (pts) |")
    md.append("| ---- | ------ | ---- | ------ | -------- | --------------- |")
    for r in rows:
        md.append(f"| {r['date']} | {r['trades']} | {r['wins']} | {r['losses']} | {r['win_rate']:.1f}% | {r['pnl']:+.2f} |")
    md.append("")
    md.append("## Trade Performance Metrics")
    md.append(f"Total trades: {len(all_df)}; Winners: {(pnl_series>0).sum() if not pnl_series.empty else 0}; "
              f"Losers: {(pnl_series<0).sum() if not pnl_series.empty else 0}; "
              f"Breakeven: {(pnl_series==0).sum() if not pnl_series.empty else 0}.")
    md.append(f"Profit factor: {pf:.2f}; Expectancy: {expectancy:.2f} pts; Avg win: {avg_win:.2f}; Avg loss: {avg_loss:.2f}.")
    md.append(f"Max drawdown: {max_dd:.2f} pts.")
    md.append("")
    md.append("## Exit Distribution")
    md.append("| Exit Type | Count |")
    md.append("| --------- | ----- |")
    if exit_counts:
        for k, v in exit_counts.items():
            md.append(f"| {k} | {v} |")
    else:
        md.append("| (none) | 0 |")
    md.append("")
    md.append("## Directional Performance (CALL vs PUT)")
    md.append("| Side | Net P&L (pts) |")
    md.append("| ---- | ------------- |")
    if side_perf:
        for k, v in side_perf.items():
            md.append(f"| {k} | {v:+.2f} |")
    else:
        md.append("| (none) | 0 |")
    md.append("")
    md.append("## Equity Curve Analysis")
    seq = ", ".join(f"{x:+.1f}" for x in cum.tolist()) if not cum.empty else "N/A"
    md.append("Cumulative P&L (pts) sequence: " + seq)
    md.append("")
    md.append("## Strategy Observations")
    md.append("- Performance still dominated by 2026-03-08 gains; several days remain negative (03-02, 03-03, 03-04, 03-09).")
    md.append("- Win rate ~55% but payoff ratio remains <1; further exit tuning required.")
    md.append("- PUT path improved modestly but remains weaker than CALL overall.")
    md.append("")
    md.append("## Recommendations")
    md.append("- Add second partial at TG and ATR-buffered trailing to raise payoff.")
    md.append("- Continue softening conflict gates when ADX>30 and CPR is narrow.")
    md.append("- Profile replay loop for additional speedups.")

    Path("replay_pnl_report.md").write_text("\n".join(md), encoding="utf-8")


if __name__ == "__main__":
    main()
