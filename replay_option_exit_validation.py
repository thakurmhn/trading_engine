"""Replay validator for OptionExitManager using SQLite tick databases.

This script replays option ticks sequentially into OptionExitManager and
produces:
1) Trade ledger (entry/exit/pnl/held/exitreason)
2) Summary statistics
3) CSV export for audit
4) Risk-buffer comparison (same replay with risk_buffer=0.0)

Usage:
    python replay_option_exit_validation.py
    python replay_option_exit_validation.py --db-glob "C:\\SQLite\\ticks\\*.db"
"""

from __future__ import annotations

import argparse
import glob
import itertools
import os
import sqlite3
from dataclasses import dataclass
from typing import Any

import pandas as pd

from option_exit_manager import OptionExitConfig, OptionExitManager


@dataclass
class ReplayConfig:
    """Configuration for replay validation."""

    db_glob: str = r"C:\SQLite\ticks\*.db"
    out_csv: str = ""
    min_symbol_ticks: int = 80
    max_symbols_per_db: int = 20
    entry_stride_ticks: int = 30
    max_hold_ticks: int = 60
    min_premium: float = 200.0
    max_premium: float = 400.0
    risk_buffer: float = 1.0
    profile_name: str = "baseline"
    hf_config: OptionExitConfig | None = None


HF_REASONS = [
    "DYNAMIC_TRAILING_STOP",
    "MOMENTUM_EXHAUSTION",
    "VOLATILITY_MEAN_REVERSION",
]

EXIT_TYPES = ["HFT", "SL", "SCALP", "PT", "TG", "MIN_BAR", "ATR", "CPR", "CAMARILLA"]


SWEEP_RISK_BUFFER = [0.0, 0.5, 1.0, 2.0]
SWEEP_ROC_WINDOW = [5, 8, 10]
SWEEP_TIGHTEN_FRAC = [0.30, 0.50, 0.70]
SWEEP_TRAIL_PAIRS = [(0.12, 0.05), (0.10, 0.03)]
SWEEP_STD_THRESHOLD = [1.5, 2.0, 2.5]


def _has_ticks_table(cur: sqlite3.Cursor) -> bool:
    tables = {
        row[0] for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    return "ticks" in tables


def _map_exit_type(exit_reason: str) -> str:
    """Map concrete reason to normalized audit exit type."""
    reason = (exit_reason or "").upper()
    if reason in {"DYNAMIC_TRAILING_STOP", "MOMENTUM_EXHAUSTION", "VOLATILITY_MEAN_REVERSION", "HF_EXIT"}:
        return "HFT"
    if reason == "SL_HIT":
        return "SL"
    if reason in {"SCALP_PT_HIT", "SCALP_SL_HIT"}:
        return "SCALP"
    if reason == "PT_HIT":
        return "PT"
    if reason in {"TARGET_HIT", "TG_HIT"}:
        return "TG"
    if reason in {"DEFERRED", "MIN_BAR"}:
        return "MIN_BAR"
    if reason in {"OSC_EXHAUSTION", "MOMENTUM_EXIT", "TIME_EXIT"}:
        return "ATR"
    if "CPR" in reason:
        return "CPR"
    if "CAMARILLA" in reason:
        return "CAMARILLA"
    return "ATR"


def _fetch_option_symbols(cur: sqlite3.Cursor, cfg: ReplayConfig) -> list[str]:
    query = """
        SELECT symbol, COUNT(*) AS n, AVG(last_price) AS avg_px
        FROM ticks
        WHERE (symbol LIKE '%CE' OR symbol LIKE '%PE')
          AND last_price IS NOT NULL
          AND last_price > 0
        GROUP BY symbol
        HAVING n >= ? AND avg_px BETWEEN ? AND ?
        ORDER BY n DESC
    """
    rows = cur.execute(
        query,
        (cfg.min_symbol_ticks, cfg.min_premium, cfg.max_premium),
    ).fetchall()
    return [row[0] for row in rows[: cfg.max_symbols_per_db]]


def _replay_symbol(
    cur: sqlite3.Cursor,
    db_name: str,
    symbol: str,
    cfg: ReplayConfig,
    risk_buffer: float,
) -> list[dict]:
    query = """
        SELECT timestamp, last_price, COALESCE(volume, 0)
        FROM ticks
        WHERE symbol = ?
          AND last_price IS NOT NULL
          AND last_price > 0
        ORDER BY timestamp
    """
    rows = cur.execute(query, (symbol,)).fetchall()
    if len(rows) < 40:
        return []

    hf_cfg = cfg.hf_config or OptionExitConfig(
        dynamic_trail_lo=0.10,
        dynamic_trail_hi=0.03,
        trail_tighten_profit_frac=0.50,
        roc_window_ticks=8,
        roc_drop_fraction=0.60,
        ma_window=20,
        std_threshold=2.0,
        min_1m_bars_for_structure=3,
    )

    trades: list[dict] = []
    manager: OptionExitManager | None = None
    entry_idx: int | None = None
    entry_px: float | None = None
    entry_ts: pd.Timestamp | None = None

    for i, (ts_raw, px_raw, vol_raw) in enumerate(rows):
        px = float(px_raw)
        vol = float(vol_raw or 0.0)
        ts = pd.Timestamp(ts_raw)

        if manager is None:
            if i % cfg.entry_stride_ticks == 0 and cfg.min_premium <= px <= cfg.max_premium:
                manager = OptionExitManager(
                    entry_price=px,
                    side="CALL",
                    risk_buffer=risk_buffer,
                    config=hf_cfg,
                )
                entry_idx = i
                entry_px = px
                entry_ts = ts
            continue

        # Explicit sequential stream into update_tick() for replay fidelity.
        manager.update_tick(px, vol, ts)
        should_exit = manager.check_exit(
            px,
            ts,
            current_volume=vol,
            ingest_tick=False,
        )
        ticks_held = i - int(entry_idx)

        if should_exit or ticks_held >= cfg.max_hold_ticks:
            exit_reason = manager.last_reason if should_exit else "TIMEOUT"
            pnl = px - float(entry_px)
            bars_held = max(1, int((ts - entry_ts).total_seconds() // 60))

            trades.append(
                {
                    "profile": cfg.profile_name,
                    "db": db_name,
                    "symbol": symbol,
                    "entry_time": entry_ts,
                    "exit_time": ts,
                    "entry_price": round(float(entry_px), 2),
                    "exit_price": round(px, 2),
                    "ticks_held": ticks_held,
                    "bars_held": bars_held,
                    "pnl": round(pnl, 2),
                    "exit_reason": exit_reason,
                    "exit_type": _map_exit_type(exit_reason),
                    "risk_buffer": risk_buffer,
                }
            )

            manager = None
            entry_idx = None
            entry_px = None
            entry_ts = None

    return trades


def run_replay(cfg: ReplayConfig, risk_buffer: float) -> list[dict]:
    """Replay all eligible DBs and return trade records."""
    trades: list[dict] = []
    paths = sorted(glob.glob(cfg.db_glob))

    for db_path in paths:
        db_name = os.path.basename(db_path)
        con = sqlite3.connect(db_path)
        cur = con.cursor()

        try:
            if not _has_ticks_table(cur):
                continue

            symbols = _fetch_option_symbols(cur, cfg)
            for symbol in symbols:
                trades.extend(_replay_symbol(cur, db_name, symbol, cfg, risk_buffer))
        finally:
            con.close()

    return trades


def _print_summary(df: pd.DataFrame, profile_name: str) -> None:
    win_mask = df["pnl"] > 0
    print(f"=== REPLAY SUMMARY ({profile_name.upper()}) ===")
    print(f"total_trades: {len(df)}")
    print(f"win_rate: {win_mask.mean() * 100:.2f}%")
    print(f"avg_pnl: {df['pnl'].mean():.2f}")
    print(f"avg_bars_held: {df['bars_held'].mean():.2f}")
    premium_min = float(df[["entry_price", "exit_price"]].min().min())
    premium_max = float(df[["entry_price", "exit_price"]].max().max())
    print(f"premium_range: {premium_min:.2f} - {premium_max:.2f}")

    print("\n=== EXIT REASONS ===")
    print(df["exit_reason"].value_counts().to_string())
    if "exit_type" in df.columns:
        print("\n=== EXIT TYPES ===")
        print(df["exit_type"].value_counts().to_string())
        observed_types = [x for x in EXIT_TYPES if x in set(df["exit_type"])]
        print("exit_type_coverage:", ", ".join(observed_types) if observed_types else "None")
    print("\n=== WIN/LOSS BREAKDOWN ===")
    print(df.assign(outcome=df["pnl"].apply(lambda x: "WIN" if x > 0 else ("LOSS" if x < 0 else "FLAT")))
          .groupby(["exit_reason", "outcome"]).size().to_string())

    wins = df[df["pnl"] > 0]
    losses = df[df["pnl"] < 0]

    print("\n=== SAMPLE WIN ===")
    if wins.empty:
        print("None")
    else:
        print(wins.sort_values("pnl", ascending=False).head(1).to_string(index=False))

    print("\n=== SAMPLE LOSS ===")
    if losses.empty:
        print("None")
    else:
        print(losses.sort_values("pnl", ascending=True).head(1).to_string(index=False))


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="OptionExitManager replay validator")
    parser.add_argument("--db-glob", default=ReplayConfig.db_glob)
    parser.add_argument("--out-csv", default=ReplayConfig.out_csv)
    parser.add_argument("--risk-buffer", type=float, default=ReplayConfig.risk_buffer)
    parser.add_argument("--entry-stride", type=int, default=ReplayConfig.entry_stride_ticks)
    parser.add_argument("--max-hold", type=int, default=ReplayConfig.max_hold_ticks)
    parser.add_argument("--max-symbols", type=int, default=ReplayConfig.max_symbols_per_db)
    parser.add_argument("--min-symbol-ticks", type=int, default=ReplayConfig.min_symbol_ticks)
    parser.add_argument("--min-premium", type=float, default=ReplayConfig.min_premium)
    parser.add_argument("--max-premium", type=float, default=ReplayConfig.max_premium)
    parser.add_argument(
        "--volstress",
        action="store_true",
        help="Run volatility stress profile in addition to baseline and stress.",
    )
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="Run parameter sensitivity sweep and export per-set ledgers.",
    )
    parser.add_argument(
        "--sweep-max-runs",
        type=int,
        default=0,
        help="Optional cap on number of parameter combinations (0 = all).",
    )
    parser.add_argument(
        "--func-check",
        action="store_true",
        help="Run trading-pipeline functionality compatibility checks and export report.",
    )
    return parser.parse_args()


def _format_reason_distribution(df: pd.DataFrame) -> str:
    """Return compact reason distribution string."""
    if df.empty:
        return "N/A"
    vc = df["exit_reason"].value_counts()
    return "; ".join([f"{reason}:{count}" for reason, count in vc.items()])


def _build_profile_row(df: pd.DataFrame, profile_name: str) -> dict[str, Any]:
    """Build profile-level metrics row for comparative output."""
    if df.empty:
        return {
            "profile": profile_name,
            "trades": 0,
            "win_rate": 0.0,
            "avg_pnl": 0.0,
            "avg_bars_held": 0.0,
            "exit_reasons": "N/A",
        }
    return {
        "profile": profile_name,
        "trades": int(len(df)),
        "win_rate": float((df["pnl"] > 0).mean() * 100.0),
        "avg_pnl": float(df["pnl"].mean()),
        "avg_bars_held": float(df["bars_held"].mean()),
        "exit_reasons": _format_reason_distribution(df),
    }


def _print_comparative_table(rows: list[dict[str, Any]]) -> str:
    """Print and return side-by-side comparison table as text."""
    comp_df = pd.DataFrame(rows)
    if comp_df.empty:
        return "No comparative data."

    display = comp_df.copy()
    display["win_rate"] = display["win_rate"].map(lambda x: f"{x:.2f}%")
    display["avg_pnl"] = display["avg_pnl"].map(lambda x: f"{x:.2f}")
    display["avg_bars_held"] = display["avg_bars_held"].map(lambda x: f"{x:.2f}")
    display.rename(
        columns={
            "profile": "Profile",
            "trades": "Trades",
            "win_rate": "Win Rate",
            "avg_pnl": "Avg PnL",
            "avg_bars_held": "Avg Bars Held",
            "exit_reasons": "Exit Reasons",
        },
        inplace=True,
    )
    table_txt = display.to_string(index=False)
    print("\n=== PROFILE COMPARISON ===")
    print(table_txt)
    return table_txt


def _run_profile(cfg: ReplayConfig) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Run one replay profile and print summary diagnostics."""
    trades = run_replay(cfg, risk_buffer=cfg.risk_buffer)
    if not trades:
        print(f"No option trades generated for profile={cfg.profile_name}.")
        return pd.DataFrame(), {
            "profile": cfg.profile_name,
            "coverage": set(),
            "early_exits_rb1": 0,
            "early_exits_rb0": 0,
            "row": _build_profile_row(pd.DataFrame(), cfg.profile_name),
            "reason_counts": {},
            "sample_trailing": None,
            "sample_mean_reversion": None,
        }

    df = pd.DataFrame(trades)
    df.to_csv(cfg.out_csv, index=False)
    _print_summary(df, cfg.profile_name)

    observed_hf = set(df["exit_reason"].unique()) & set(HF_REASONS)
    print(f"\n=== HF ALGO COVERAGE ({cfg.profile_name.upper()}) ===")
    for reason in HF_REASONS:
        print(f"{reason}: {'OBSERVED' if reason in observed_hf else 'NOT_OBSERVED'}")

    trades_rb1 = run_replay(cfg, risk_buffer=1.0)
    trades_rb0 = run_replay(cfg, risk_buffer=0.0)
    early_1 = 0
    early_0 = 0
    if trades_rb1 and trades_rb0:
        df1 = pd.DataFrame(trades_rb1)
        df0 = pd.DataFrame(trades_rb0)
        early_1 = int((df1["ticks_held"] <= 3).sum())
        early_0 = int((df0["ticks_held"] <= 3).sum())
        print(f"\n=== RISK BUFFER CHECK ({cfg.profile_name.upper()}) ===")
        print("early exits <=3 ticks (risk_buffer=1.0): " f"{early_1}")
        print("early exits <=3 ticks (risk_buffer=0.0): " f"{early_0}")

    print(f"\nCSV exported: {cfg.out_csv}")
    profile_report = {
        "profile": cfg.profile_name,
        "coverage": observed_hf,
        "early_exits_rb1": early_1,
        "early_exits_rb0": early_0,
        "row": _build_profile_row(df, cfg.profile_name),
        "reason_counts": df["exit_reason"].value_counts().to_dict(),
        "sample_trailing": (
            df[df["exit_reason"] == "DYNAMIC_TRAILING_STOP"].head(1).to_dict("records")
        ),
        "sample_mean_reversion": (
            df[df["exit_reason"] == "VOLATILITY_MEAN_REVERSION"].head(1).to_dict("records")
        ),
    }
    return df, profile_report


def _fmt_num(value: float) -> str:
    """Format float for filenames."""
    return f"{value:.2f}".rstrip("0").rstrip(".").replace(".", "p")


def _build_sweep_tag(params: dict[str, Any]) -> str:
    """Build deterministic filename-safe tag for one parameter set."""
    return (
        f"rb{_fmt_num(params['risk_buffer'])}_"
        f"roc{params['roc_window_ticks']}_"
        f"tt{_fmt_num(params['trail_tighten_profit_frac'])}_"
        f"dlo{_fmt_num(params['dynamic_trail_lo'])}_"
        f"dhi{_fmt_num(params['dynamic_trail_hi'])}_"
        f"std{_fmt_num(params['std_threshold'])}"
    )


def _sweep_param_grid() -> list[dict[str, Any]]:
    """Return full parameter sweep grid."""
    grid: list[dict[str, Any]] = []
    for rb, roc_w, tighten, (d_lo, d_hi), std_th in itertools.product(
        SWEEP_RISK_BUFFER,
        SWEEP_ROC_WINDOW,
        SWEEP_TIGHTEN_FRAC,
        SWEEP_TRAIL_PAIRS,
        SWEEP_STD_THRESHOLD,
    ):
        grid.append(
            {
                "risk_buffer": rb,
                "roc_window_ticks": roc_w,
                "trail_tighten_profit_frac": tighten,
                "dynamic_trail_lo": d_lo,
                "dynamic_trail_hi": d_hi,
                "std_threshold": std_th,
            }
        )
    return grid


def _build_sweep_cfg(
    args: argparse.Namespace,
    out_dir: str,
    params: dict[str, Any],
) -> ReplayConfig:
    """Build replay config for one sweep parameter set."""
    tag = _build_sweep_tag(params)
    return ReplayConfig(
        db_glob=args.db_glob,
        out_csv=os.path.join(out_dir, f"replay_sweep_{tag}.csv"),
        min_symbol_ticks=max(40, args.min_symbol_ticks // 2),
        max_symbols_per_db=max(40, args.max_symbols),
        entry_stride_ticks=max(4, args.entry_stride // 4),
        max_hold_ticks=max(600, args.max_hold * 10),
        risk_buffer=float(params["risk_buffer"]),
        min_premium=100.0,
        max_premium=600.0,
        profile_name=f"sweep::{tag}",
        hf_config=OptionExitConfig(
            dynamic_trail_lo=float(params["dynamic_trail_lo"]),
            dynamic_trail_hi=float(params["dynamic_trail_hi"]),
            trail_tighten_profit_frac=float(params["trail_tighten_profit_frac"]),
            roc_window_ticks=int(params["roc_window_ticks"]),
            roc_drop_fraction=0.60,
            ma_window=20,
            std_threshold=float(params["std_threshold"]),
            min_1m_bars_for_structure=3,
        ),
    )


def _build_sweep_row(df: pd.DataFrame, params: dict[str, Any], tag: str) -> dict[str, Any]:
    """Build summary row for one sweep run."""
    if df.empty:
        return {
            "param_set": tag,
            **params,
            "trades": 0,
            "win_rate": 0.0,
            "avg_pnl": 0.0,
            "avg_bars_held": 0.0,
            "exit_reasons": "N/A",
            "hf_coverage": "NONE",
            "early_exit_rate": 0.0,
        }
    coverage = sorted(list(set(df["exit_reason"]) & set(HF_REASONS)))
    return {
        "param_set": tag,
        **params,
        "trades": int(len(df)),
        "win_rate": float((df["pnl"] > 0).mean() * 100.0),
        "avg_pnl": float(df["pnl"].mean()),
        "avg_bars_held": float(df["bars_held"].mean()),
        "exit_reasons": _format_reason_distribution(df),
        "hf_coverage": ",".join(coverage) if coverage else "NONE",
        "early_exit_rate": float((df["ticks_held"] <= 3).mean() * 100.0),
    }


def _run_parameter_sweep(args: argparse.Namespace, out_dir: str) -> None:
    """Run parameter sensitivity sweep and export comparative summary."""
    grid = _sweep_param_grid()
    if args.sweep_max_runs and args.sweep_max_runs > 0:
        grid = grid[: args.sweep_max_runs]

    print(f"\n=== PARAMETER SWEEP START ===")
    print(f"total_param_sets: {len(grid)}")

    rows: list[dict[str, Any]] = []
    for idx, params in enumerate(grid, start=1):
        tag = _build_sweep_tag(params)
        print(f"[SWEEP] {idx}/{len(grid)} {tag}")
        cfg = _build_sweep_cfg(args, out_dir, params)
        trades = run_replay(cfg, risk_buffer=cfg.risk_buffer)
        df = pd.DataFrame(trades)
        df.to_csv(cfg.out_csv, index=False)
        rows.append(_build_sweep_row(df, params, tag))

    summary_df = pd.DataFrame(rows)
    summary_df.sort_values(["avg_pnl", "win_rate"], ascending=[False, False], inplace=True)

    top_n = summary_df.head(5)
    unstable = summary_df[
        (summary_df["early_exit_rate"] > 10.0) | (summary_df["win_rate"] < 50.0)
    ]

    rb_group = summary_df.groupby("risk_buffer")[["win_rate", "avg_pnl", "early_exit_rate"]].mean()
    roc_group = summary_df.groupby("roc_window_ticks")[["win_rate", "avg_pnl"]].mean()
    best_rb = rb_group["avg_pnl"].idxmax() if not rb_group.empty else None
    best_roc = roc_group["avg_pnl"].idxmax() if not roc_group.empty else None
    unstable_rb = rb_group[rb_group["early_exit_rate"] > 10.0].index.tolist()

    sweep_summary_path = os.path.join(out_dir, "replay_option_exit_sweep_summary.txt")
    lines = [
        "OPTION EXIT PARAMETER SWEEP SUMMARY",
        "",
        "Table: Param Set | Trades | Win Rate | Avg PnL | Avg Bars Held | Exit Reasons",
        summary_df[
            [
                "param_set",
                "trades",
                "win_rate",
                "avg_pnl",
                "avg_bars_held",
                "exit_reasons",
            ]
        ].to_string(index=False),
        "",
        "Top 5 by Avg PnL then Win Rate:",
        top_n[
            [
                "param_set",
                "win_rate",
                "avg_pnl",
                "avg_bars_held",
                "hf_coverage",
            ]
        ].to_string(index=False),
        "",
        "Unstable configurations (early_exit_rate > 10% or win_rate < 50%):",
        unstable[
            [
                "param_set",
                "win_rate",
                "avg_pnl",
                "early_exit_rate",
                "hf_coverage",
            ]
        ].to_string(index=False) if not unstable.empty else "None",
        "",
        "Average metrics by risk_buffer:",
        rb_group.to_string(),
        "",
        "Average metrics by roc_window_ticks:",
        roc_group.to_string(),
        "",
        "Sensitivity highlights:",
        (
            f"Recommended risk_buffer (highest avg_pnl): {best_rb}"
            if best_rb is not None else "Recommended risk_buffer: N/A"
        ),
        (
            f"Recommended roc_window_ticks (highest avg_pnl): {best_roc}"
            if best_roc is not None else "Recommended roc_window_ticks: N/A"
        ),
        (
            "Unstable risk_buffer values by early exits: "
            + (", ".join([str(x) for x in unstable_rb]) if unstable_rb else "None")
        ),
    ]
    with open(sweep_summary_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print("\n=== PARAMETER SWEEP SUMMARY (TOP 5) ===")
    print(
        top_n[
            [
                "param_set",
                "trades",
                "win_rate",
                "avg_pnl",
                "avg_bars_held",
                "hf_coverage",
            ]
        ].to_string(index=False)
    )
    print(f"\nSweep summary exported: {sweep_summary_path}")


def _run_functionality_check(args: argparse.Namespace, out_dir: str) -> None:
    """Run compatibility checks for entry/exit orchestration and audit fields."""
    func_cfg = ReplayConfig(
        db_glob=args.db_glob,
        out_csv=os.path.join(out_dir, "replay_functionality_check.csv"),
        min_symbol_ticks=max(60, args.min_symbol_ticks // 2),
        max_symbols_per_db=max(30, args.max_symbols),
        entry_stride_ticks=max(5, args.entry_stride // 3),
        max_hold_ticks=max(180, args.max_hold * 3),
        risk_buffer=1.0,
        min_premium=200.0,
        max_premium=400.0,
        profile_name="functionality_check",
        hf_config=OptionExitConfig(
            dynamic_trail_lo=0.10,
            dynamic_trail_hi=0.03,
            trail_tighten_profit_frac=0.50,
            roc_window_ticks=8,
            roc_drop_fraction=0.60,
            ma_window=20,
            std_threshold=2.0,
            min_1m_bars_for_structure=3,
        ),
    )
    trades = run_replay(func_cfg, risk_buffer=func_cfg.risk_buffer)
    df = pd.DataFrame(trades)
    df.to_csv(func_cfg.out_csv, index=False)

    required_cols = {
        "entry_price",
        "exit_price",
        "bars_held",
        "pnl",
        "exit_reason",
        "entry_time",
        "exit_time",
        "symbol",
    }
    has_data = not df.empty
    cols_ok = required_cols.issubset(set(df.columns))
    signals_firing_ok = has_data
    entry_price_ok = has_data and df["entry_price"].between(200, 400).all()
    exit_price_ok = has_data and (df["exit_price"] < 1000).all() and (df["exit_price"] > 0).all()
    no_spot_prices = has_data and ((df["entry_price"] > 25000).sum() == 0) and ((df["exit_price"] > 25000).sum() == 0)
    duplicate_count = int(df.duplicated(subset=["profile", "symbol", "entry_time", "exit_time"]).sum()) if has_data else 0
    no_duplicates = duplicate_count == 0
    bars_ok = has_data and (df["bars_held"] >= 1).all()
    reason_ok = has_data and df["exit_reason"].notna().all()
    pnl_consistency = has_data and ((df["pnl"] - (df["exit_price"] - df["entry_price"])).abs() <= 0.05).all()
    hf_integration_ok = has_data and any(df["exit_reason"].isin(HF_REASONS))

    checks = [
        ("Signal firing detected (entry events present)", signals_firing_ok),
        ("Entry prices are option premiums in 200-400 range", entry_price_ok),
        ("Exit prices are option premiums (<1000, >0)", exit_price_ok),
        ("No spot-like prices (25000+) in entry/exit fields", no_spot_prices),
        ("Required audit fields present in ledger", cols_ok),
        ("No duplicate entry/exit rows", no_duplicates),
        ("bars_held is valid (>=1)", bars_ok),
        ("exit_reason populated for every trade", reason_ok),
        ("PnL matches (exit-entry) within tolerance", pnl_consistency),
        ("HF exits co-exist in orchestration output", hf_integration_ok),
    ]

    sample_entry = df.head(1).to_dict("records")[0] if has_data else None
    sample_exit = df.sort_values("pnl", ascending=False).head(1).to_dict("records")[0] if has_data else None
    reasons = df["exit_reason"].value_counts().to_string() if has_data else "No trades"

    summary_path = os.path.join(out_dir, "replay_functionality_summary.txt")
    lines = [
        "REPLAY FUNCTIONALITY CHECK SUMMARY",
        "",
        f"total_trades: {len(df)}",
        f"win_rate: {((df['pnl'] > 0).mean() * 100.0):.2f}%" if has_data else "win_rate: N/A",
        f"avg_pnl: {df['pnl'].mean():.2f}" if has_data else "avg_pnl: N/A",
        f"avg_bars_held: {df['bars_held'].mean():.2f}" if has_data else "avg_bars_held: N/A",
        "",
        "Checklist:",
    ]
    for label, ok in checks:
        lines.append(f"- {'PASS' if ok else 'FAIL'}: {label}")
    lines.extend(
        [
            "",
            "Exit reason distribution:",
            reasons,
            "",
            "Sample entry row:",
            str(sample_entry) if sample_entry else "None",
            "",
            "Sample exit row:",
            str(sample_exit) if sample_exit else "None",
            "",
            f"duplicate_rows_detected: {duplicate_count}",
            f"csv_path: {func_cfg.out_csv}",
        ]
    )
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print("\n=== FUNCTIONALITY CHECK ===")
    for label, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'} | {label}")
    print("\nExit reason distribution:")
    print(reasons)
    if sample_entry:
        print("\nSample entry trade:")
        print(sample_entry)
    if sample_exit:
        print("\nSample exit trade:")
        print(sample_exit)
    print(f"\nFunctionality CSV exported: {func_cfg.out_csv}")
    print(f"Functionality summary exported: {summary_path}")


def _write_summary_file(
    path: str,
    reports: list[dict[str, Any]],
    table_txt: str,
) -> None:
    """Write combined replay summary text artifact."""
    by_profile = {r["profile"]: r for r in reports}

    coverage_lines: list[str] = []
    for reason in HF_REASONS:
        observed_profiles = [
            r["profile"] for r in reports if reason in r.get("coverage", set())
        ]
        cov_txt = ", ".join(observed_profiles) if observed_profiles else "None"
        coverage_lines.append(f"{reason}: {cov_txt}")

    diff_lines: list[str] = []
    if "volstress" in by_profile:
        vol_cov = by_profile["volstress"].get("coverage", set())
        others_cov = set().union(
            *[r.get("coverage", set()) for r in reports if r["profile"] != "volstress"]
        )
        only_vol = sorted(list(vol_cov - others_cov))
        diff_lines.append(
            "Observed only in volstress: " + (", ".join(only_vol) if only_vol else "None")
        )
    if "stress" in by_profile and "baseline" in by_profile:
        st_cov = by_profile["stress"].get("coverage", set())
        base_cov = by_profile["baseline"].get("coverage", set())
        only_stress = sorted(list(st_cov - base_cov))
        diff_lines.append(
            "Observed only in stress vs baseline: "
            + (", ".join(only_stress) if only_stress else "None")
        )

    reason_count_lines: list[str] = []
    for reason in ["DYNAMIC_TRAILING_STOP", "VOLATILITY_MEAN_REVERSION"]:
        counts = []
        for r in reports:
            n = int(r.get("reason_counts", {}).get(reason, 0))
            counts.append(f"{r['profile']}={n}")
        reason_count_lines.append(f"{reason}: " + ", ".join(counts))

    rb_lines: list[str] = []
    for r in reports:
        rb_lines.append(
            f"{r['profile']} rb=1.0:{r['early_exits_rb1']} rb=0.0:{r['early_exits_rb0']}"
        )

    stress_sample = by_profile.get("stress", {}).get("sample_trailing")
    volstress_sample = by_profile.get("volstress", {}).get("sample_mean_reversion")

    lines = [
        "REPLAY OPTION EXIT VALIDATION SUMMARY",
        "",
        "Side-by-side comparison:",
        table_txt,
        "",
        "HF coverage by profile:",
        *coverage_lines,
        "",
        "Coverage differences:",
        *diff_lines,
        "",
        "Reason-count comparison (extreme-exit focus):",
        *reason_count_lines,
        "",
        "Risk buffer early-exit check (ticks_held <= 3):",
        *rb_lines,
        "",
        "Stress sample (DYNAMIC_TRAILING_STOP):",
        str(stress_sample[0]) if stress_sample else "None",
        "",
        "Volstress sample (VOLATILITY_MEAN_REVERSION):",
        str(volstress_sample[0]) if volstress_sample else "None",
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main() -> None:
    """Execute baseline and stress replay with comparative reporting."""
    args = parse_args()
    out_dir = os.path.dirname(args.out_csv) if args.out_csv else os.getcwd()
    os.makedirs(out_dir, exist_ok=True)

    baseline_cfg = ReplayConfig(
        db_glob=args.db_glob,
        out_csv=os.path.join(out_dir, "replay_option_exit_validation_baseline.csv"),
        min_symbol_ticks=args.min_symbol_ticks,
        max_symbols_per_db=args.max_symbols,
        entry_stride_ticks=args.entry_stride,
        max_hold_ticks=args.max_hold,
        risk_buffer=args.risk_buffer,
        min_premium=args.min_premium,
        max_premium=args.max_premium,
        profile_name="baseline",
        hf_config=OptionExitConfig(
            dynamic_trail_lo=0.10,
            dynamic_trail_hi=0.03,
            trail_tighten_profit_frac=0.50,
            roc_window_ticks=8,
            roc_drop_fraction=0.60,
            ma_window=20,
            std_threshold=2.0,
            min_1m_bars_for_structure=3,
        ),
    )
    # Stress profile widens symbol/price coverage and holds positions longer
    # to increase observation probability for all HF exits in replay.
    stress_cfg = ReplayConfig(
        db_glob=args.db_glob,
        out_csv=os.path.join(out_dir, "replay_option_exit_validation_stress.csv"),
        min_symbol_ticks=max(50, args.min_symbol_ticks // 2),
        max_symbols_per_db=max(30, args.max_symbols),
        entry_stride_ticks=max(5, args.entry_stride // 3),
        max_hold_ticks=max(300, args.max_hold * 5),
        risk_buffer=0.5,
        min_premium=150.0,
        max_premium=450.0,
        profile_name="stress",
        hf_config=OptionExitConfig(
            dynamic_trail_lo=0.015,
            dynamic_trail_hi=0.005,
            trail_tighten_profit_frac=0.15,
            roc_window_ticks=12,
            roc_drop_fraction=0.95,
            ma_window=20,
            std_threshold=1.2,
            min_1m_bars_for_structure=2,
        ),
    )

    profile_reports: list[dict[str, Any]] = []

    _, baseline_report = _run_profile(baseline_cfg)
    profile_reports.append(baseline_report)
    _, stress_report = _run_profile(stress_cfg)
    profile_reports.append(stress_report)

    if args.volstress:
        volstress_cfg = ReplayConfig(
            db_glob=args.db_glob,
            out_csv=os.path.join(out_dir, "replay_option_exit_validation_volstress.csv"),
            min_symbol_ticks=max(40, args.min_symbol_ticks // 2),
            max_symbols_per_db=max(40, args.max_symbols),
            entry_stride_ticks=max(4, args.entry_stride // 4),
            max_hold_ticks=max(600, args.max_hold * 10),
            risk_buffer=0.5,
            min_premium=100.0,
            max_premium=600.0,
            profile_name="volstress",
            hf_config=OptionExitConfig(
                dynamic_trail_lo=0.012,
                dynamic_trail_hi=0.004,
                trail_tighten_profit_frac=0.12,
                roc_window_ticks=14,
                roc_drop_fraction=0.97,
                ma_window=24,
                std_threshold=1.1,
                min_1m_bars_for_structure=2,
            ),
        )
        _, volstress_report = _run_profile(volstress_cfg)
        profile_reports.append(volstress_report)

    table_txt = _print_comparative_table([r["row"] for r in profile_reports])

    summary_path = os.path.join(out_dir, "replay_option_exit_summary.txt")
    _write_summary_file(summary_path, profile_reports, table_txt)
    print(f"\nSummary exported: {summary_path}")

    if args.sweep:
        _run_parameter_sweep(args, out_dir)
    if args.func_check:
        _run_functionality_check(args, out_dir)


if __name__ == "__main__":
    main()
