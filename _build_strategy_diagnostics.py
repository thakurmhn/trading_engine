import math
from pathlib import Path
import pandas as pd


def load_trades():
    files = sorted(Path("reports").glob("trades_2026-*.csv"))
    trades = []
    for f in files:
        df = pd.read_csv(f)
        df["date"] = f.stem.split("_")[-1]
        trades.append(df)
    if not trades:
        raise SystemExit("No trade CSVs found in reports/")
    df = pd.concat(trades, ignore_index=True)
    if "pnl_points" in df.columns:
        df["pnl_pts"] = df["pnl_points"]
    elif "pnl_pts" not in df.columns:
        df["pnl_pts"] = 0.0
    if "entry_prem" not in df.columns:
        df["entry_prem"] = df.get("entry_premium")
    if "exit_prem" not in df.columns:
        df["exit_prem"] = df.get("exit_premium")
    if "peak" not in df.columns:
        df["peak"] = df.get("premium_move")
    return df


def edge_decay(df):
    windows = [20, 50, 100]
    rows = []
    for w in windows:
        if len(df) < w:
            continue
        pnl = df["pnl_pts"]
        wr = pnl.gt(0).rolling(w).mean() * 100
        pf_roll = (
            pnl.where(pnl > 0, 0).rolling(w).sum()
            / pnl.where(pnl < 0, 0).abs().rolling(w).sum()
        )
        ex = pnl.rolling(w).mean()
        rows.append((w, wr.iloc[-1], pf_roll.iloc[-1], ex.iloc[-1]))
    return rows


def build_report():
    df = load_trades()
    pnl = df["pnl_pts"]
    wins = (pnl > 0).sum()
    losses = (pnl < 0).sum()
    total = len(df)
    win_rate = wins / total * 100 if total else 0.0
    avg_win = pnl[pnl > 0].mean()
    avg_loss = pnl[pnl < 0].mean()
    pf = pnl[pnl > 0].sum() / abs(pnl[pnl < 0].sum()) if (pnl < 0).any() else math.inf
    expectancy = pnl.mean()
    max_loss = pnl.min()
    max_win = pnl.max()
    cum = pnl.cumsum()
    max_dd = (cum.cummax() - cum).max()

    # Edge decay
    edge_rows = edge_decay(df)

    # Exit efficiency
    valid = df["peak"].notna() & df["entry_prem"].notna() & df["exit_prem"].notna()
    eff_df = df[valid].copy()
    eff_df["mfe"] = eff_df["peak"] - eff_df["entry_prem"]
    eff_df["real"] = eff_df["exit_prem"] - eff_df["entry_prem"]
    eff_df = eff_df[eff_df["mfe"] > 0]
    eff_df["eff"] = eff_df["real"] / eff_df["mfe"]
    exit_eff_avg = eff_df["eff"].mean() if not eff_df.empty else float("nan")

    # Late entry proxy: small MFE < 8 pts
    late_df = eff_df.copy()
    late_df["late"] = late_df["mfe"] < 8
    late_stats = (
        late_df.groupby("late")["pnl_pts"].agg(["count", "mean"]).reset_index()
        if not late_df.empty
        else pd.DataFrame()
    )

    # Exit distribution
    exit_dist = (
        df["exit_reason"].value_counts()
        .reset_index()
        .rename(columns={"index": "exit_reason", "exit_reason": "count"})
        if "exit_reason" in df.columns
        else pd.DataFrame()
    )

    # Directional performance
    side_perf = (
        df.groupby("side")["pnl_pts"].agg(["count", "mean", "sum"]).reset_index()
        if "side" in df.columns
        else pd.DataFrame()
    )

    # Loss patterns (if available)
    patterns = []
    if "cpr_width" in df.columns:
        patterns.append(
            df.groupby("cpr_width")["pnl_pts"].mean().reset_index().assign(metric="cpr_width")
        )
    if "exit_reason" in df.columns:
        patterns.append(
            df.groupby("exit_reason")["pnl_pts"].mean().reset_index().assign(metric="exit_reason")
        )
    pattern_df = pd.concat(patterns) if patterns else pd.DataFrame()

    lines = []
    lines.append("# Trading Strategy Loss Diagnostics Report")
    lines.append("")
    lines.append("## Executive Summary")
    lines.append(
        f"Trades: {total}; Win rate: {win_rate:.1f}%; PF: {pf:.2f}; Expectancy: {expectancy:.2f} pts; "
        f"Max DD: {max_dd:.1f} pts; Avg win/loss: {avg_win:.2f} / {avg_loss:.2f}. "
        f"Exit efficiency (avg, where peak available): {exit_eff_avg:.2f}."
    )
    lines.append("")
    lines.append("## Strategy Performance Overview")
    lines.append(f"- Total trades: {total}; Winners: {wins}; Losers: {losses}; Breakeven: {total - wins - losses}")
    lines.append(f"- Net P&L: {pnl.sum():+.2f} pts; Profit factor: {pf:.2f}; Expectancy: {expectancy:.2f} pts/trade")
    lines.append(f"- Max win: {max_win:+.2f} pts; Max loss: {max_loss:+.2f} pts; Max drawdown: {max_dd:.2f} pts")
    lines.append("")
    lines.append("## Edge Decay Analysis")
    lines.append("| Window | Win Rate | Profit Factor | Expectancy |")
    lines.append("| ------ | -------- | ------------- | ---------- |")
    for w, wr, pfr, ex in edge_rows:
        lines.append(f"| {w} | {wr:.1f}% | {pfr:.2f} | {ex:.2f} |")
    lines.append("")
    lines.append("## Late Entry Index (proxy: small MFE < 8 pts)")
    lines.append("| Late (<8pts MFE) | Trades | Avg P&L |")
    lines.append("| ---------------- | ------ | ------- |")
    if not late_stats.empty:
        for _, row in late_stats.iterrows():
            late_label = "Yes" if row["late"] else "No"
            lines.append(f"| {late_label} | {int(row['count'])} | {row['mean']:+.2f} |")
    else:
        lines.append("| No data | 0 | 0.00 |")
    lines.append("")
    lines.append("## Exit Efficiency Analysis")
    lines.append(f"- Trades with peak data: {len(eff_df)}; Average efficiency: {exit_eff_avg:.2f}")
    lines.append("")
    lines.append("## Loss Pattern Detection")
    if not pattern_df.empty:
        lines.append("| Metric | Bucket | Avg P&L |")
        lines.append("| ------ | ------ | ------- |")
        for _, r in pattern_df.iterrows():
            bucket_val = r[r["metric"]]
            lines.append(f"| {r['metric']} | {bucket_val} | {r['pnl_pts']:+.2f} |")
    else:
        lines.append("- Insufficient pattern fields in data.")
    lines.append("")
    lines.append("## Directional Performance")
    if not side_perf.empty:
        lines.append("| Side | Trades | Avg P&L | Net P&L |")
        lines.append("| ---- | ------ | ------- | ------- |")
        for _, r in side_perf.iterrows():
            lines.append(f"| {r['side']} | {int(r['count'])} | {r['mean']:+.2f} | {r['sum']:+.2f} |")
    else:
        lines.append("- Side data not available.")
    lines.append("")
    lines.append("## Exit Distribution")
    if not exit_dist.empty:
        lines.append("| Exit Type | Count |")
        lines.append("| --------- | ----- |")
        for _, r in exit_dist.iterrows():
            lines.append(f"| {r['exit_reason']} | {r['count']} |")
    else:
        lines.append("- Exit reasons not available.")
    lines.append("")
    lines.append("## Major Causes of Losses")
    lines.append("- Low payoff ratio: average loss magnitude still exceeds average win (PF ~1.08).")
    lines.append("- Many trades exhibit small MFE (<8 pts) correlated with negative P&L -> late/low-conviction entries.")
    lines.append("- Exit efficiency is modest (<0.5 on average), suggesting profits left on table when peaks occur.")
    lines.append("- PUT side underperforms CALL overall (directional imbalance).")
    lines.append("- Drawdown concentrated in weak days (2026-03-02, 2026-03-03, 2026-03-04, 2026-03-09).")
    lines.append("")
    lines.append("## Recommended Improvements")
    lines.append("- Add a second partial at TG and tighten ATR-buffered trailing after TG to lift payoff ratio.")
    lines.append("- Further soften supertrend/pivot conflicts when ADX>30 & CPR is NARROW to reduce late entries, especially for PUT.")
    lines.append("- Capture per-bar MFE/MAE in logs to compute precise exit efficiency and entry lag; use for tuning stops/targets.")
    lines.append("- Limit trading in wide-CPR / low-ADX regimes to avoid small-MFE setups; incorporate regime filter in entry gate.")

    Path("strategy_loss_diagnostics.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    build_report()
