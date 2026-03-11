import glob
import os
from datetime import datetime, timedelta

import pandas as pd

ROOT = os.getcwd()
today = datetime.strptime("2026-03-10", "%Y-%m-%d")  # fixed to run deterministically
start = today - timedelta(days=13)


def load_trades():
    rows = []
    for path in glob.glob("trades_2026-*.csv"):
        # parse date inside filename trades_YYYY-MM-DD.csv
        try:
            date_str = path.split("_")[1].split(".")[0]
            dt = datetime.strptime(date_str, "%Y-%m-%d")
        except Exception:
            continue
        if start.date() <= dt.date() <= today.date():
            df = pd.read_csv(path)
            df["trade_date"] = dt.date()
            rows.append(df)
    if not rows:
        raise SystemExit("No trades in last 14 days.")
    return pd.concat(rows, ignore_index=True)


def summarize(df: pd.DataFrame):
    # normalize column names if variants exist
    renames = {
        "pnl_points": "pnl_pts",
        "pnl_pts": "pnl_pts",
        "pnl_rupees": "pnl_rs",
        "bars_held": "bars_held",
        "exit_reason": "exit_reason",
        "side": "side",
        "src": "src",
    }
    for k, v in renames.items():
        if k in df.columns and v not in df.columns:
            df[v] = df[k]
    df = df[[c for c in ["pnl_pts", "pnl_rs", "bars_held", "exit_reason", "side", "src", "trade_date"] if c in df.columns]]

    total = len(df)
    wins = (df["pnl_pts"] > 0).sum()
    losses = (df["pnl_pts"] < 0).sum()
    breakeven = (df["pnl_pts"] == 0).sum()
    win_rate = wins / total * 100 if total else 0
    avg_win = df.loc[df["pnl_pts"] > 0, "pnl_pts"].mean()
    avg_loss = df.loc[df["pnl_pts"] < 0, "pnl_pts"].mean()
    gross_win = df.loc[df["pnl_pts"] > 0, "pnl_pts"].sum()
    gross_loss = -df.loc[df["pnl_pts"] < 0, "pnl_pts"].sum()
    profit_factor = gross_win / gross_loss if gross_loss else float("inf")
    expectancy = (win_rate / 100) * (avg_win or 0) + (1 - win_rate / 100) * (avg_loss or 0)
    avg_hold = df["bars_held"].mean()

    by_side = df.groupby("side")["pnl_pts"].agg(["count", "mean", "sum"]).reset_index()
    by_exit = df.groupby("exit_reason")["pnl_pts"].agg(["count", "mean", "sum"]).reset_index()
    by_day = df.groupby("trade_date")["pnl_pts"].agg(["count", "mean", "sum"]).reset_index()

    print("Trades window:", start.date(), "to", today.date())
    print(f"Total trades: {total}")
    print(f"Win rate: {win_rate:.1f}%  (W:{wins} L:{losses} BE:{breakeven})")
    print(f"Avg win: {avg_win:.2f} pts   Avg loss: {avg_loss:.2f} pts")
    print(f"Profit factor: {profit_factor:.2f}   Expectancy: {expectancy:.2f} pts/trade")
    print(f"Avg hold: {avg_hold:.2f} bars")
    print("\nBy side:")
    print(by_side.to_string(index=False))
    print("\nBy exit reason:")
    print(by_exit.sort_values('sum').to_string(index=False))
    print("\nBy day:")
    print(by_day.sort_values('trade_date').to_string(index=False))


if __name__ == "__main__":
    df_all = load_trades()
    summarize(df_all)
