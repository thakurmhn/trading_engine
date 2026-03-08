"""Trade dashboard: log parser, structured report, and equity-curve plotter.

Called at the end of each live/paper/replay session to produce:
  • A structured CSV of all trades parsed from the session log.
  • A summary dict  (total trades, win %, net P&L, CALL vs PUT split).
  • A PNG equity-curve  (cumulative P&L over trade sequence).
  • A JSON export of trades and session summary.
  • A text report with full session diagnostics.
  • A comparison report (baseline vs fixed) for head-to-head analysis.

Usage
-----
    # Programmatic — pass an already-built trades DataFrame:
    from dashboard import SessionDashboard

    dash = SessionDashboard()
    dash.record_entry("2024-01-15T09:30:00", "NSE:NIFTY24JAN22000CE",
                      "CALL", price=120.0, qty=50, position_id="P1")
    dash.record_exit("2024-01-15T10:15:00", "NSE:NIFTY24JAN22000CE",
                     "CALL", price=145.0, qty=50, reason="TARGET_HIT",
                     position_id="P1", bars_held=5)
    artifacts = dash.emit(output_dir="reports/")

    # From log file:
    from dashboard import generate_dashboard
    artifacts = generate_dashboard(log_path="options_trade_engine_2024-01-15.log",
                                   output_dir="reports/")

    # Full log-parser pipeline (new format + legacy EXIT AUDIT):
    from dashboard import generate_full_report, compare_sessions
    generate_full_report(log_path="options_trade_engine_2026-02-24.log",
                         output_dir="reports/")
    compare_sessions(baseline_paths=["baseline.log"],
                     fixed_paths=["fixed.log"],
                     output_dir="reports/")

Log-parsing targets
-------------------
[TRADE OPEN][REPLAY|PAPER|LIVE]  — new-format entry fill
[TRADE EXIT]                     — new-format exit fill (WIN/LOSS + P&L)
[ENTRY BLOCKED][subtype]         — gate suppression
[ENTRY OK]                       — entry passed all gates
[SIGNAL FIRED]                   — signal generated (pre-gate)
[EXIT AUDIT]                     — legacy detailed exit record
[ENTRY DISPATCH]                 — emitted by st_pullback_cci (legacy)
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

# ── optional matplotlib (gracefully absent in headless / CI environments) ──
try:
    import matplotlib
    matplotlib.use("Agg")          # non-interactive backend — safe on servers
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker
    _MPL_AVAILABLE = True
except ImportError:
    _MPL_AVAILABLE = False

logger = logging.getLogger(__name__)

# ── Log-line regex patterns ──────────────────────────────────────────────────

# 2024-01-15 09:30:00,123 - INFO - [ENTRY DISPATCH] broker=... symbol=... side=...
_RE_ENTRY = re.compile(
    r"\[ENTRY DISPATCH\]"
    r".*?broker=(?P<broker>\S+)"
    r".*?symbol=(?P<symbol>\S+)"
    r".*?side=(?P<side>BUY|SELL)"
    r"(?:.*?order_id=(?P<order_id>\S+))?"
    r"(?:.*?timestamp=(?P<timestamp>\S+))?",
    re.IGNORECASE,
)

# [EXIT AUDIT] timestamp=... symbol=... option_type=... exit_type=... reason=... bars_held=... position_id=...
_RE_EXIT = re.compile(
    r"\[EXIT AUDIT\]"
    r".*?timestamp=(?P<timestamp>\S+)"
    r".*?symbol=(?P<symbol>\S+)"
    r".*?option_type=(?P<option_type>CALL|PUT)"
    r".*?exit_type=(?P<exit_type>\S+)"
    r".*?reason=(?P<reason>\S+)"
    r"(?:.*?bars_held=(?P<bars_held>[-\d]+))?"
    r"(?:.*?position_id=(?P<position_id>\S+))?"
    r"(?:.*?premium_move=(?P<premium_move>[-\d.]+))?",
    re.IGNORECASE,
)

# [ENTRY CONFIG] timestamp=... symbol=... adx_min=... rsi_range=... cci_range=...
_RE_ENTRY_CONFIG = re.compile(
    r"\[ENTRY CONFIG\]"
    r".*?adx_min=(?P<adx_min>[\d.]+)"
    r"(?:.*?rsi_range=\[(?P<rsi_min>[\d.]+),(?P<rsi_max>[\d.]+)\])?"
    r"(?:.*?cci_range=\[(?P<cci_min>[-\d.]+),(?P<cci_max>[\d.]+)\])?",
    re.IGNORECASE,
)

# ── Structured trade record ──────────────────────────────────────────────────

_SIDE_MAP = {"BUY": "CALL", "SELL": "PUT"}   # entry side → option_type


class TradeRecord:
    """Holds matched entry + exit data for a single trade leg."""

    __slots__ = (
        "position_id", "symbol", "option_type",
        "entry_ts", "exit_ts",
        "entry_price", "exit_price",
        "qty", "reason", "bars_held", "premium_move",
        "pnl_points", "pnl_rupees",
    )

    def __init__(
        self,
        position_id: str,
        symbol: str,
        option_type: str,
        entry_ts: Optional[str] = None,
        exit_ts: Optional[str] = None,
        entry_price: float = 0.0,
        exit_price: float = 0.0,
        qty: int = 0,
        reason: str = "",
        bars_held: int = -1,
        premium_move: Optional[float] = None,
    ) -> None:
        self.position_id = position_id
        self.symbol = symbol
        self.option_type = option_type
        self.entry_ts = entry_ts
        self.exit_ts = exit_ts
        self.entry_price = entry_price
        self.exit_price = exit_price
        self.qty = qty
        self.reason = reason
        self.bars_held = bars_held
        self.premium_move = premium_move
        # P&L derived from premium_move if prices not available
        self.pnl_points = self._calc_pnl_points()
        self.pnl_rupees = self.pnl_points * self.qty

    def _calc_pnl_points(self) -> float:
        if self.premium_move is not None:
            return float(self.premium_move)
        if self.entry_price and self.exit_price:
            return self.exit_price - self.entry_price
        return 0.0

    def to_dict(self) -> dict:
        return {
            "position_id":  self.position_id,
            "symbol":       self.symbol,
            "option_type":  self.option_type,
            "entry_ts":     self.entry_ts or "",
            "exit_ts":      self.exit_ts or "",
            "entry_price":  self.entry_price,
            "exit_price":   self.exit_price,
            "qty":          self.qty,
            "reason":       self.reason,
            "bars_held":    self.bars_held,
            "pnl_points":   round(self.pnl_points, 2),
            "pnl_rupees":   round(self.pnl_rupees, 2),
        }


# ── Log parser ───────────────────────────────────────────────────────────────

def parse_log_file(log_path: str | Path) -> pd.DataFrame:
    """Parse a session log file and return a DataFrame of EXIT AUDIT records.

    Columns
    -------
    timestamp, symbol, option_type, exit_type, reason,
    bars_held, position_id, premium_move
    """
    log_path = Path(log_path)
    if not log_path.exists():
        logger.warning(f"[DASHBOARD] Log file not found: {log_path}")
        return pd.DataFrame()

    rows: List[dict] = []
    with log_path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = _RE_EXIT.search(line)
            if m:
                d = m.groupdict()
                rows.append(
                    {
                        "timestamp":    d.get("timestamp", ""),
                        "symbol":       d.get("symbol", ""),
                        "option_type":  (d.get("option_type") or "").upper(),
                        "exit_type":    d.get("exit_type", ""),
                        "reason":       d.get("reason", ""),
                        "bars_held":    int(d["bars_held"]) if d.get("bars_held") not in (None, "") else -1,
                        "position_id":  d.get("position_id", "UNKNOWN"),
                        "premium_move": float(d["premium_move"]) if d.get("premium_move") not in (None, "") else None,
                    }
                )

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["pnl_points"] = df["premium_move"].where(df["premium_move"].notna(), 0.0)
    return df


# ── Summary computation ───────────────────────────────────────────────────────

def compute_summary(
    df: pd.DataFrame,
    config_thresholds: Optional[dict] = None,
) -> dict:
    """Compute key performance metrics from a trades DataFrame.

    Works with either a raw parsed-log DataFrame or a SessionDashboard
    DataFrame (both expose ``pnl_points`` and ``option_type`` columns).

    Parameters
    ----------
    config_thresholds : Optional dict with STEntryConfig values
        (e.g. ``{"adx_min": 18.0, "rr_ratio": 2.0, "tg_rr_ratio": 1.0}``).
        When supplied, included verbatim in the returned summary dict.

    Returns
    -------
    dict with keys:
        total_trades, winners, losers, breakeven,
        win_rate_pct, net_pnl_points, net_pnl_rupees,
        max_win_points, max_loss_points,
        call_trades, put_trades,
        call_pnl_points, put_pnl_points,
        config_thresholds
    """
    if df is None or df.empty:
        return {
            "total_trades": 0,
            "winners": 0, "losers": 0, "breakeven": 0,
            "win_rate_pct": 0.0,
            "net_pnl_points": 0.0, "net_pnl_rupees": 0.0,
            "max_win_points": 0.0, "max_loss_points": 0.0,
            "call_trades": 0, "put_trades": 0,
            "call_pnl_points": 0.0, "put_pnl_points": 0.0,
            "config_thresholds": config_thresholds or {},
        }

    pnl = df["pnl_points"].fillna(0.0)
    qty = df["qty"].fillna(1) if "qty" in df.columns else pd.Series([1] * len(df))
    pnl_rupees = (pnl * qty).sum()

    winners   = int((pnl > 0).sum())
    losers    = int((pnl < 0).sum())
    breakeven = int((pnl == 0).sum())
    total     = len(df)

    call_mask  = df.get("option_type", pd.Series([""] * total)).str.upper() == "CALL"
    put_mask   = df.get("option_type", pd.Series([""] * total)).str.upper() == "PUT"

    # Symbol-wise breakdown
    symbol_stats = {}
    if "option_name" in df.columns:
        # Extract underlying from option name (e.g. NIFTY from NSE:NIFTY24JAN...)
        # Simple heuristic: take first alpha part
        df["_underlying"] = df["option_name"].apply(lambda x: re.match(r"([A-Z]+)", str(x).split(':')[-1]).group(1) if re.match(r"([A-Z]+)", str(x).split(':')[-1]) else "UNKNOWN")
        
        for sym, group in df.groupby("_underlying"):
            s_pnl = group["pnl_points"].sum()
            s_wins = (group["pnl_points"] > 0).sum()
            s_total = len(group)
            s_wr = (s_wins / s_total * 100) if s_total else 0
            symbol_stats[sym] = {"pnl": round(s_pnl, 2), "trades": s_total, "win_rate": round(s_wr, 1)}

    return {
        "total_trades":    total,
        "winners":         winners,
        "losers":          losers,
        "breakeven":       breakeven,
        "win_rate_pct":    round(winners / total * 100, 1) if total else 0.0,
        "net_pnl_points":  round(float(pnl.sum()), 2),
        "net_pnl_rupees":  round(float(pnl_rupees), 2),
        "max_win_points":  round(float(pnl.max()), 2) if total else 0.0,
        "max_loss_points": round(float(pnl.min()), 2) if total else 0.0,
        "call_trades":     int(call_mask.sum()),
        "put_trades":      int(put_mask.sum()),
        "call_pnl_points": round(float(pnl[call_mask].sum()), 2),
        "put_pnl_points":  round(float(pnl[put_mask].sum()), 2),
        "config_thresholds": config_thresholds or {},
        "symbol_stats":    symbol_stats,
    }


def print_summary(summary: dict) -> None:
    """Print a formatted summary to stdout."""
    sep = "=" * 55
    print(f"\n{sep}")
    print("  SESSION DASHBOARD - TRADE SUMMARY")
    print(sep)
    print(f"  Total trades   : {summary['total_trades']}")
    print(f"  Winners        : {summary['winners']}")
    print(f"  Losers         : {summary['losers']}")
    print(f"  Breakeven      : {summary['breakeven']}")
    print(f"  Win rate       : {summary['win_rate_pct']:.1f}%")
    print(f"  Net P&L (pts)  : {summary['net_pnl_points']:+.2f}")
    print(f"  Net P&L (Rs)   : Rs {summary['net_pnl_rupees']:+,.2f}")
    print(f"  Max win (pts)  : {summary['max_win_points']:+.2f}")
    print(f"  Max loss (pts) : {summary['max_loss_points']:+.2f}")
    print(sep)
    print(f"  CALL trades    : {summary['call_trades']}  P&L: {summary['call_pnl_points']:+.2f} pts")
    print(f"  PUT  trades    : {summary['put_trades']}  P&L: {summary['put_pnl_points']:+.2f} pts")
    if summary.get("symbol_stats"):
        print(sep)
        for sym, stats in summary["symbol_stats"].items():
            print(f"  {sym:<15}: {stats['trades']} trades, {stats['win_rate']}% win, {stats['pnl']:+.2f} pts")
    thresholds = summary.get("config_thresholds") or {}
    if thresholds:
        print(sep)
        print("  ENTRY THRESHOLDS (STEntryConfig)")
        print(f"  ADX min        : {thresholds.get('adx_min', 'N/A')}")
        print(f"  RR ratio       : {thresholds.get('rr_ratio', 'N/A')}")
        print(f"  TG RR ratio    : {thresholds.get('tg_rr_ratio', 'N/A')}")
    print(sep)


# ── Equity curve ─────────────────────────────────────────────────────────────

def plot_equity_curve(
    df: pd.DataFrame,
    output_path: Optional[str | Path] = None,
    title: str = "Cumulative P&L (points)",
) -> Optional[Path]:
    """Plot cumulative P&L over the trade sequence and save as PNG.

    Parameters
    ----------
    df          : DataFrame with at least a ``pnl_points`` column.
    output_path : Where to save the PNG.  If None, uses a temp file.
    title       : Chart title.

    Returns
    -------
    Path to the saved PNG, or None if matplotlib is unavailable.
    """
    if not _MPL_AVAILABLE:
        logger.warning("[DASHBOARD] matplotlib not installed — equity curve skipped.")
        return None

    if df is None or df.empty or "pnl_points" not in df.columns:
        logger.warning("[DASHBOARD] No pnl_points data — equity curve skipped.")
        return None

    output_path = Path(output_path) if output_path else Path("reports") / "equity_curve.png"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    pnl    = df["pnl_points"].fillna(0.0)
    cumsum = pnl.cumsum().reset_index(drop=True)
    x      = range(1, len(cumsum) + 1)

    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(x, cumsum, linewidth=1.8, color="#2196F3", label="Cumulative P&L")
    ax.fill_between(x, cumsum, 0,
                    where=(cumsum >= 0), alpha=0.15, color="green", label="Profit")
    ax.fill_between(x, cumsum, 0,
                    where=(cumsum < 0),  alpha=0.15, color="red",   label="Loss")
    ax.axhline(0, color="black", linewidth=0.8, linestyle="--")

    final_pnl = float(cumsum.iloc[-1]) if len(cumsum) else 0.0
    color = "green" if final_pnl >= 0 else "red"
    ax.annotate(
        f"Final: {final_pnl:+.2f} pts",
        xy=(len(x), final_pnl),
        xytext=(-60, 12),
        textcoords="offset points",
        fontsize=9,
        color=color,
        fontweight="bold",
    )

    ax.set_title(title, fontsize=13, fontweight="bold")
    ax.set_xlabel("Trade #")
    ax.set_ylabel("P&L (points)")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:+.0f}"))
    ax.legend(fontsize=8)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)

    logger.info(f"[DASHBOARD] Equity curve saved → {output_path}")
    return output_path


# ── CSV report ────────────────────────────────────────────────────────────────

def save_report_csv(df: pd.DataFrame, output_path: str | Path) -> Path:
    """Save the trades DataFrame to a structured CSV."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)
    logger.info(f"[DASHBOARD] Report CSV saved → {output_path}")
    return output_path


# ── SessionDashboard — live/paper session accumulator ────────────────────────

class SessionDashboard:
    """Accumulate trade entries and exits during a live or paper session.

    Usage
    -----
    ::

        dash = SessionDashboard(qty_default=50)
        dash.record_entry(ts, symbol, "CALL", price=120, position_id="P1")
        dash.record_exit(ts, symbol, "CALL", price=145, reason="TARGET_HIT",
                         position_id="P1", bars_held=5)
        artifacts = dash.emit(output_dir="reports/")
    """

    def __init__(
        self,
        qty_default: int = 1,
        config_thresholds: Optional[dict] = None,
    ) -> None:
        self._entries: Dict[str, dict] = {}   # position_id → entry data
        self._records: List[dict] = []        # completed trade records
        self._qty_default = qty_default
        self._config_thresholds: dict = config_thresholds or {}

    # ── recording ─────────────────────────────────────────────────────────

    def record_entry(
        self,
        timestamp: str,
        symbol: str,
        option_type: str,
        price: float = 0.0,
        qty: int = 0,
        position_id: str = "",
    ) -> None:
        pid = position_id or symbol
        self._entries[pid] = {
            "timestamp": timestamp,
            "symbol": symbol,
            "option_type": option_type.upper(),
            "price": float(price),
            "qty": qty or self._qty_default,
        }

    def record_exit(
        self,
        timestamp: str,
        symbol: str,
        option_type: str,
        price: float = 0.0,
        qty: int = 0,
        reason: str = "",
        position_id: str = "",
        bars_held: int = -1,
        premium_move: Optional[float] = None,
    ) -> None:
        pid = position_id or symbol
        entry = self._entries.pop(pid, {})
        entry_price = float(entry.get("price", 0.0))
        exit_price  = float(price)
        used_qty    = qty or entry.get("qty", self._qty_default)

        pm = premium_move if premium_move is not None else (exit_price - entry_price)
        pnl_points = float(pm)
        pnl_rupees = pnl_points * used_qty

        self._records.append(
            {
                "position_id":  pid,
                "symbol":       symbol,
                "option_type":  option_type.upper(),
                "entry_ts":     entry.get("timestamp", ""),
                "exit_ts":      timestamp,
                "entry_price":  entry_price,
                "exit_price":   exit_price,
                "qty":          used_qty,
                "reason":       reason,
                "bars_held":    bars_held,
                "pnl_points":   round(pnl_points, 2),
                "pnl_rupees":   round(pnl_rupees, 2),
            }
        )

    # ── output ────────────────────────────────────────────────────────────

    def to_dataframe(self) -> pd.DataFrame:
        if not self._records:
            return pd.DataFrame()
        return pd.DataFrame(self._records)

    def summary(self) -> dict:
        return compute_summary(
            self.to_dataframe(),
            config_thresholds=self._config_thresholds,
        )

    def plot_equity_curve(
        self,
        output_path: Optional[str | Path] = None,
        title: str = "Session Equity Curve",
    ) -> Optional[Path]:
        return plot_equity_curve(self.to_dataframe(), output_path=output_path, title=title)

    def emit(
        self,
        output_dir: Optional[str | Path] = None,
        date_tag: Optional[str] = None,
    ) -> dict:
        """Generate all dashboard artifacts and return paths.

        Parameters
        ----------
        output_dir : Directory for output files (created if needed).
        date_tag   : String appended to file names, e.g. ``"2024-01-15"``.
                     Defaults to today's date.

        Returns
        -------
        dict with keys ``csv``, ``chart``, ``summary``.
        """
        out_dir  = Path(output_dir) if output_dir else Path("reports")
        out_dir.mkdir(parents=True, exist_ok=True)
        tag = date_tag or datetime.now().strftime("%Y-%m-%d")

        df      = self.to_dataframe()
        summary = compute_summary(df, config_thresholds=self._config_thresholds)

        csv_path   = save_report_csv(df, out_dir / f"trades_{tag}.csv") if not df.empty else None
        chart_path = plot_equity_curve(df, output_path=out_dir / f"equity_curve_{tag}.png")

        print_summary(summary)
        logging.info(
            "[DASHBOARD] Session report emitted. "
            f"trades={summary['total_trades']} "
            f"win_rate={summary['win_rate_pct']}% "
            f"net_pnl_pts={summary['net_pnl_points']}"
        )

        return {
            "csv":     csv_path,
            "chart":   chart_path,
            "summary": summary,
        }


# ── generate_dashboard — log-file entry point ─────────────────────────────────

def _parse_config_thresholds(log_path: "str | Path") -> dict:
    """Extract STEntryConfig thresholds from the first [ENTRY CONFIG] log line."""
    thresholds: dict = {}
    try:
        with Path(log_path).open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = _RE_ENTRY_CONFIG.search(line)
                if m:
                    d = m.groupdict()
                    if d.get("adx_min") not in (None, ""):
                        thresholds["adx_min"] = float(d["adx_min"])
                    if d.get("rsi_min") not in (None, ""):
                        thresholds["rsi_min"] = float(d["rsi_min"])
                    if d.get("rsi_max") not in (None, ""):
                        thresholds["rsi_max"] = float(d["rsi_max"])
                    if d.get("cci_min") not in (None, ""):
                        thresholds["cci_min"] = float(d["cci_min"])
                    if d.get("cci_max") not in (None, ""):
                        thresholds["cci_max"] = float(d["cci_max"])
                    break  # first match is enough
    except Exception:
        pass
    return thresholds


def generate_dashboard(
    log_path: Optional[str | Path] = None,
    trades_df: Optional[pd.DataFrame] = None,
    output_dir: str | Path = "reports",
) -> dict:
    """Parse a log file (or accept an existing trades DataFrame) and emit artifacts.

    Parameters
    ----------
    log_path   : Path to the session ``.log`` file.  When provided the
                 ``[ENTRY CONFIG]`` lines are also parsed to extract the
                 active STEntryConfig thresholds included in the summary.
    trades_df  : Pre-built DataFrame (used when log_path is not provided).
    output_dir : Where to write the CSV report and chart.

    Returns
    -------
    dict with keys ``csv``, ``chart``, ``summary``.
    """
    config_thresholds: dict = {}
    if trades_df is None and log_path is not None:
        trades_df = parse_log_file(log_path)
        config_thresholds = _parse_config_thresholds(log_path)

    if trades_df is None:
        trades_df = pd.DataFrame()

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    tag = datetime.now().strftime("%Y-%m-%d_%H%M")

    summary    = compute_summary(trades_df, config_thresholds=config_thresholds)
    csv_path   = save_report_csv(trades_df, out_dir / f"trades_{tag}.csv") if not trades_df.empty else None
    chart_path = plot_equity_curve(trades_df, output_path=out_dir / f"equity_curve_{tag}.png")

    print_summary(summary)
    logging.info(
        "[DASHBOARD] generate_dashboard completed. "
        f"source={'log_file' if log_path else 'dataframe'} "
        f"trades={summary['total_trades']} "
        f"net_pnl_pts={summary['net_pnl_points']} "
        f"config_thresholds={config_thresholds or 'N/A'}"
    )

    return {
        "csv":     csv_path,
        "chart":   chart_path,
        "summary": summary,
    }


# ── JSON export ────────────────────────────────────────────────────────────────

def save_report_json(
    trades: list | pd.DataFrame,
    summary: dict,
    output_path: str | Path,
) -> Path:
    """Save trades list and session summary to a JSON file.

    Parameters
    ----------
    trades      : List of trade dicts or a DataFrame with trades.
    summary     : Session summary dict (from compute_summary or SessionSummary.to_dict).
    output_path : Destination .json file path.

    Returns
    -------
    Path to the saved JSON file.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if isinstance(trades, pd.DataFrame):
        trade_list = trades.where(trades.notna(), other=None).to_dict(orient="records")
    else:
        trade_list = list(trades)

    payload = {
        "generated_at": datetime.now().isoformat(),
        "summary":      summary,
        "trades":       trade_list,
    }

    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)

    logger.info(f"[DASHBOARD] JSON report saved → {output_path}")
    return output_path


# ── Full log-parser report ─────────────────────────────────────────────────────

def generate_full_report(
    log_path: str | Path,
    output_dir: str | Path = "reports",
) -> dict:
    """Parse a log file using LogParser (new format + legacy) and emit artifacts.

    Produces:
      trades_{date}.csv
      trades_{date}.json
      equity_curve_{date}.png
      dashboard_report_{date}.txt

    Returns
    -------
    dict with keys: csv, json, chart, text, summary
    """
    from log_parser import LogParser

    log_path = Path(log_path)
    parser   = LogParser(log_path)
    session  = parser.parse()

    out_dir  = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tag      = session.date_tag or datetime.now().strftime("%Y-%m-%d")

    trades_df = pd.DataFrame(session.trades) if session.trades else pd.DataFrame()
    summary   = session.to_dict()

    csv_path   = save_report_csv(trades_df, out_dir / f"trades_{tag}.csv") if not trades_df.empty else None
    json_path  = save_report_json(trades_df, summary, out_dir / f"trades_{tag}.json")
    chart_path = plot_equity_curve(trades_df, output_path=out_dir / f"equity_curve_{tag}.png")
    text_path  = _write_text_report(session, out_dir / f"dashboard_report_{tag}.txt")

    logger.info(
        f"[DASHBOARD_REPORT] date={session.date_tag} "
        f"sessions=1 trades={session.total_trades} "
        f"net_pnl={session.net_pnl_pts:+.1f}pts "
        f"win_rate={session.win_rate_pct:.1f}% "
        f"survivability={session.survivability_ratio:.1f}%"
    )

    return {
        "csv":     csv_path,
        "json":    json_path,
        "chart":   chart_path,
        "text":    text_path,
        "summary": summary,
    }


def _write_text_report(session, output_path: Path) -> Path:
    """Write a human-readable text report for a SessionSummary."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sep  = "=" * 60
    sep2 = "-" * 60

    lines = [
        sep,
        f"  TRADING ENGINE SESSION REPORT — {session.date_tag}",
        f"  Log  : {session.log_path}",
        f"  Type : {session.session_type}",
        sep,
        "",
        "  TRADE SUMMARY",
        sep2,
        f"  Total trades   : {session.total_trades}",
        f"  Winners        : {session.winners}",
        f"  Losers         : {session.losers}",
        f"  Breakeven      : {session.breakeven}",
        f"  Win rate       : {session.win_rate_pct:.1f}%",
        f"  Net P&L (pts)  : {session.net_pnl_pts:+.2f}",
        f"  Net P&L (Rs)   : {session.net_pnl_rs:+,.0f}",
        f"  CALL trades    : {session.call_trades}",
        f"  PUT  trades    : {session.put_trades}",
        "",
        "  SIGNAL PIPELINE",
        sep2,
        f"  Signals fired  : {session.signals_fired}",
        f"  Entry OK       : {session.entry_ok_count}",
        f"  Total blocked  : {session.total_blocked}",
    ]

    if session.blocked_counts:
        lines.append("")
        lines.append("  BLOCKED BREAKDOWN")
        lines.append(sep2)
        for reason, cnt in sorted(session.blocked_counts.items(),
                                  key=lambda x: -x[1]):
            lines.append(f"  {reason:<30}: {cnt:>5}")

    if session.exit_reason_counts:
        lines.append("")
        lines.append("  EXIT REASONS")
        lines.append(sep2)
        for reason, cnt in sorted(session.exit_reason_counts.items(),
                                  key=lambda x: -x[1]):
            lines.append(f"  {reason:<30}: {cnt:>5}")

    if session.tag_counts:
        lines.append("")
        lines.append("  P1-P5 TAGS FIRED")
        lines.append(sep2)
        for tag, cnt in sorted(session.tag_counts.items(), key=lambda x: -x[1]):
            lines.append(f"  [{tag}]{'':>5}: {cnt}")

    # Day classification section
    lines.append("")
    lines.append("  DAY CLASSIFICATION")
    lines.append(sep2)
    lines.append(f"  Day type tag       : {getattr(session, 'day_type_tag', 'N/A')}")
    lines.append(f"  CPR width          : {getattr(session, 'cpr_width_tag', 'N/A')}")
    lines.append(f"  Open bias tag      : {session.open_bias_tag}")
    lines.append(f"  Open bias logs     : {session.tag_counts.get('OPEN_BIAS', 0)}")
    lines.append(f"  Day-bias aligned   : {session.tag_counts.get('DAY_BIAS_ALIGN', 0)}")
    lines.append(f"  Day-bias misalign  : {session.tag_counts.get('DAY_BIAS_MISALIGN', 0)}")
    lines.append(f"  Gap tag            : {session.gap_tag}")
    lines.append(f"  Balance tag        : {session.balance_tag}")

    # Phase 5: Regime performance breakdown
    _regime_perf = getattr(session, "regime_performance", {})
    _has_regime_data = any(
        bool(dim_data) and not (len(dim_data) == 1 and "UNKNOWN" in dim_data)
        for dim_data in _regime_perf.values()
    )
    if _has_regime_data:
        _dim_labels = {
            "day_type":   "DAY TYPE PERFORMANCE",
            "adx_tier":   "ADX TIER PERFORMANCE",
            "atr_regime": "ATR REGIME PERFORMANCE",
            "cpr_width":  "CPR WIDTH PERFORMANCE",
        }
        for dim, title in _dim_labels.items():
            dim_data = _regime_perf.get(dim, {})
            # Skip if only UNKNOWN
            if not dim_data or (len(dim_data) == 1 and "UNKNOWN" in dim_data):
                continue
            lines.append("")
            lines.append(f"  {title}")
            lines.append(sep2)
            lines.append(f"  {'Label':<20} {'Trades':>6} {'Wins':>5} {'WR%':>6} {'Net P&L':>10}")
            lines.append(f"  {'─' * 20} {'─' * 6} {'─' * 5} {'─' * 6} {'─' * 10}")
            for label in sorted(dim_data.keys()):
                perf = dim_data[label]
                lines.append(
                    f"  {label:<20} {perf['trades']:>6} {perf['winners']:>5}"
                    f" {perf['win_rate']:>5.1f}% {perf['net_pnl']:>+10.2f}"
                )
        _rc_count = getattr(session, "regime_context_count", 0)
        _ra_count = getattr(session, "regime_adaptive_count", 0)
        lines.append("")
        lines.append(f"  Regime context logs  : {_rc_count}")
        lines.append(f"  Regime adaptive logs : {_ra_count}")

    # Phase 6: Bias Alignment Performance
    _ba_perf = getattr(session, "bias_alignment_performance", {})
    _has_ba = any(
        status in _ba_perf and _ba_perf[status].get("trades", 0) > 0
        for status in ("ALIGNED", "MISALIGNED")
    )
    if _has_ba:
        lines.append("")
        lines.append("  BIAS ALIGNMENT PERFORMANCE (Phase 6)")
        lines.append(sep2)
        lines.append(f"  {'Status':<15} {'Trades':>6} {'Wins':>5} {'WR%':>6} {'Net P&L':>10}")
        lines.append(f"  {'─' * 15} {'─' * 6} {'─' * 5} {'─' * 6} {'─' * 10}")
        for status in ("ALIGNED", "MISALIGNED", "NEUTRAL"):
            perf = _ba_perf.get(status)
            if perf and perf["trades"] > 0:
                lines.append(
                    f"  {status:<15} {perf['trades']:>6} {perf['winners']:>5}"
                    f" {perf['win_rate']:>5.1f}% {perf['net_pnl']:>+10.2f}"
                )
        _ba_count = getattr(session, "bias_alignment_count", 0)
        _bc_count = getattr(session, "bar_close_alignment_count", 0)
        lines.append("")
        lines.append(f"  Bias alignment logs     : {_ba_count}")
        lines.append(f"  Bar-close alignment logs: {_bc_count}")

    # Phase 6: Microstructure Attribution
    _mc = getattr(session, "microstructure_counts", {})
    _pe = _mc.get("pulse_exhaustion", 0)
    _za = _mc.get("zone_absorption", 0)
    _sn = _mc.get("spread_noise", 0)
    _slope_time = getattr(session, "slope_override_time_count", 0)
    _conflict_bl = getattr(session, "conflict_blocked_count", 0)
    if _pe > 0 or _za > 0 or _sn > 0 or _slope_time > 0 or _conflict_bl > 0:
        lines.append("")
        lines.append("  MICROSTRUCTURE ATTRIBUTION (Phase 6)")
        lines.append(sep2)
        lines.append(f"  Pulse exhaustion events  : {_pe}")
        lines.append(f"  Zone absorption events   : {_za}")
        lines.append(f"  Spread noise entries     : {_sn}")
        lines.append(f"  Slope time overrides     : {_slope_time}")
        lines.append(f"  Conflict blocked         : {_conflict_bl}")

    # Phase 6.1: Tilt Performance
    _tilt_perf = getattr(session, "tilt_performance", {})
    _has_tilt = any(
        tilt in _tilt_perf and _tilt_perf[tilt].get("trades", 0) > 0
        for tilt in ("BULLISH_TILT", "BEARISH_TILT")
    )
    if _has_tilt:
        lines.append("")
        lines.append("  TILT PERFORMANCE (Phase 6.1)")
        lines.append(sep2)
        lines.append(f"  {'Tilt State':<15} {'Trades':>6} {'Wins':>5} {'WR%':>6} {'Net P&L':>10}")
        lines.append(f"  {'─' * 15} {'─' * 6} {'─' * 5} {'─' * 6} {'─' * 10}")
        for tilt in ("BULLISH_TILT", "BEARISH_TILT", "NEUTRAL"):
            perf = _tilt_perf.get(tilt)
            if perf and perf["trades"] > 0:
                lines.append(
                    f"  {tilt:<15} {perf['trades']:>6} {perf['winners']:>5}"
                    f" {perf['win_rate']:>5.1f}% {perf['net_pnl']:>+10.2f}"
                )
        _gov_easy = getattr(session, "governance_easy_count", 0)
        _gov_strict = getattr(session, "governance_strict_count", 0)
        _tilt_total = getattr(session, "tilt_state_count", 0)
        lines.append("")
        lines.append(f"  Tilt state logs          : {_tilt_total}")
        lines.append(f"  Governance EASY entries   : {_gov_easy}")
        lines.append(f"  Governance STRICT entries : {_gov_strict}")
        _tilt_bias_ovr = getattr(session, "tilt_bias_override_count", 0)
        if _tilt_bias_ovr > 0:
            lines.append(f"  Bias misalign bypassed   : {_tilt_bias_ovr}  (tilt-aligned override)")

    # Reversal detection section
    rev_count = getattr(session, "reversal_trades_count", 0)
    rev_signal_count = getattr(session, "reversal_signal_count", 0)
    slope_count = getattr(session, "st_slope_override_count", 0)
    slope_trend_override = session.tag_counts.get("SLOPE_OVERRIDE_TREND", 0)
    rev_pnl = getattr(session, "reversal_pnl_attribution", 0.0)
    if rev_count > 0 or rev_signal_count > 0 or slope_count > 0 or slope_trend_override > 0:
        lines.append("")
        lines.append("  REVERSAL DETECTOR")
        lines.append(sep2)
        lines.append(f"  Reversal signals   : {rev_signal_count}  ([REVERSAL_SIGNAL] detector firings)")
        lines.append(f"  Reversal trades    : {rev_count}  (trades opened via REVERSAL_OVERRIDE path)")
        lines.append(f"  Reversal P&L (pts) : {rev_pnl:+.2f}")
        lines.append(f"  ST_SLOPE overrides : {slope_count}")
        lines.append(f"  Slope Override Trend Signals : {slope_trend_override}")

    # Oscillator gating section
    osc_blocks    = getattr(session, "oscillator_blocks", 0)
    osc_overrides = getattr(session, "oscillator_overrides", 0)
    zone_a_blocks = session.tag_counts.get("OSC_EXTREME", 0)
    zone_b_trigs = session.tag_counts.get("OSC_REVERSAL", 0)
    zone_c_relaxed = session.tag_counts.get("OSC_CONTINUATION", 0)
    osc_context_logs = session.tag_counts.get("OSC_CONTEXT", 0)
    _zone_entries = getattr(session, "zone_entry_counts", {})
    _za_entries = _zone_entries.get("ZoneA", 0)
    _zb_entries = _zone_entries.get("ZoneB", 0)
    _zc_entries = _zone_entries.get("ZoneC", 0)
    _zone_total = _za_entries + _zb_entries + _zc_entries
    if osc_blocks > 0 or osc_overrides > 0 or zone_a_blocks > 0 or zone_b_trigs > 0 or zone_c_relaxed > 0:
        osc_total = osc_blocks + osc_overrides
        override_rate = (osc_overrides / osc_total * 100) if osc_total else 0.0
        lines.append("")
        lines.append("  OSCILLATOR GATING")
        lines.append(sep2)
        lines.append(f"  OSC blocks (extreme)   : {osc_blocks}")
        lines.append(f"  OSC overrides (ADX)    : {osc_overrides}")
        lines.append(f"  Override rate          : {override_rate:.1f}%")
        if zone_a_blocks > 0 or zone_b_trigs > 0 or zone_c_relaxed > 0:
            lines.append(f"  OSC context logs       : {osc_context_logs}")
            lines.append(f"  Zone A blockers        : {zone_a_blocks}")
            lines.append(f"  Zone B reversal trig   : {zone_b_trigs}")
            lines.append(f"  Zone C continuation    : {zone_c_relaxed}")
        if _zone_total > 0:
            lines.append(f"  Zone A allowed entries : {_za_entries}  (tight OSC bounds)")
            lines.append(f"  Zone B allowed entries : {_zb_entries}  (standard bounds)")
            lines.append(f"  Zone C allowed entries : {_zc_entries}  (relaxed / trend bounds)")

    # Entry gate context diagnostics section
    entry_gate_count = session.tag_counts.get("ENTRY_GATE_CONTEXT", 0)
    _bias_align    = session.tag_counts.get("DAY_BIAS_ALIGN",    0)
    _bias_misalign = session.tag_counts.get("DAY_BIAS_MISALIGN", 0)
    _bias_total    = _bias_align + _bias_misalign
    if entry_gate_count > 0 or _zone_total > 0 or _bias_align > 0 or _bias_misalign > 0:
        lines.append("")
        lines.append("  ENTRY GATE CONTEXT")
        lines.append(sep2)
        lines.append(f"  Gate context logs      : {entry_gate_count}  ([ENTRY_GATE_CONTEXT] per-bar events)")
        if _zone_total > 0:
            lines.append(f"  Zone distribution      : A={_za_entries}  B={_zb_entries}  C={_zc_entries}  (allowed entries by zone)")
        if _bias_total > 0:
            _align_pct = round(_bias_align / _bias_total * 100, 1)
            lines.append(f"  Day-bias aligned       : {_bias_align}/{_bias_total}  ({_align_pct:.1f}% aligned)")
            lines.append(f"  Day-bias misaligned    : {_bias_misalign}")

    # Trend loss section + OSC relief + Contract roll / expiry
    trend_losses  = getattr(session, "trend_loss_count", 0)
    osc_relief    = getattr(session, "oscillator_relief_count", 0)
    expiry_rolls  = getattr(session, "expiry_roll_count", 0)
    lot_mismatches = getattr(session, "lot_size_mismatch_count", 0)
    intrinsic_skips = getattr(session, "intrinsic_filter_count", 0)

    if trend_losses > 0 or osc_relief > 0 or expiry_rolls > 0 or lot_mismatches > 0 or intrinsic_skips > 0:
        sl_loss_pct   = (trend_losses / session.total_trades * 100) if session.total_trades else 0.0
        lines.append("")
        lines.append("  TREND SURVIVABILITY")
        lines.append(sep2)
        if trend_losses > 0:
            lines.append(f"  Trend SL exits         : {trend_losses}")
            lines.append(f"  As % of all trades     : {sl_loss_pct:.1f}%")
        scalp_sl_hits = session.tag_counts.get("SCALP_SL_HIT", 0)
        trend_sl_hits = session.tag_counts.get("TREND_SL_HIT", 0)
        if scalp_sl_hits > 0 or trend_sl_hits > 0:
            lines.append(f"  Scalp SL hits          : {scalp_sl_hits}")
            lines.append(f"  Trend SL hits          : {trend_sl_hits}")
        if osc_relief > 0:
            lines.append(f"  OSC relief overrides   : {osc_relief}  (S4/R4 breakout, extreme bypassed)")
        if expiry_rolls > 0 or lot_mismatches > 0 or intrinsic_skips > 0:
            lines.append("")
            lines.append("  CONTRACT ROLL / EXPIRY")
            lines.append(sep2)
            lines.append(f"  Expiry rolls           : {expiry_rolls}  (automatic contract roll-over)")
            lines.append(f"  Lot size mismatches    : {lot_mismatches}  (API vs manual mismatch)")
            lines.append(f"  Intrinsic filter skips : {intrinsic_skips}  (zero-intrinsic contracts excluded)")

    # Failed breakout, EMA stretch, and zone revisit attribution
    fb_trades = [t for t in session.trades if t.get("failed_breakout")]
    ema_trades = [t for t in session.trades if t.get("ema_stretch")]
    zone_trades = [t for t in session.trades if t.get("zone_revisit")]
    if fb_trades or ema_trades or zone_trades:
        lines.append("")
        lines.append("  CONTEXT ATTRIBUTION")
        lines.append(sep2)
        if fb_trades:
            fb_w = sum(1 for t in fb_trades if t.get("pnl_pts", 0) > 0)
            fb_l = sum(1 for t in fb_trades if t.get("pnl_pts", 0) < 0)
            lines.append(f"  Failed breakout trades : {len(fb_trades)}  W/L={fb_w}/{fb_l}")
        if ema_trades:
            em_w = sum(1 for t in ema_trades if t.get("pnl_pts", 0) > 0)
            em_l = sum(1 for t in ema_trades if t.get("pnl_pts", 0) < 0)
            lines.append(f"  EMA stretch trades    : {len(ema_trades)}  W/L={em_w}/{em_l}")
        if zone_trades:
            z_w = sum(1 for t in zone_trades if t.get("pnl_pts", 0) > 0)
            z_l = sum(1 for t in zone_trades if t.get("pnl_pts", 0) < 0)
            br = [t for t in zone_trades if str(t.get("zone_revisit_action", "")).upper() == "BREAKOUT"]
            rv = [t for t in zone_trades if str(t.get("zone_revisit_action", "")).upper() == "REVERSAL"]
            br_w = sum(1 for t in br if t.get("pnl_pts", 0) > 0)
            br_l = sum(1 for t in br if t.get("pnl_pts", 0) < 0)
            rv_w = sum(1 for t in rv if t.get("pnl_pts", 0) > 0)
            rv_l = sum(1 for t in rv if t.get("pnl_pts", 0) < 0)
            atr_stop = sum(1 for t in zone_trades if "SL" in str(t.get("exit_reason", "")).upper())
            avg_bars = (sum(float(t.get("bars_held", 0)) for t in zone_trades) / len(zone_trades)) if zone_trades else 0.0
            lines.append(f"  Zone revisit trades    : {len(zone_trades)}  W/L={z_w}/{z_l}")
            lines.append(f"  Breakout W/L           : {br_w}/{br_l}")
            lines.append(f"  Reversal W/L           : {rv_w}/{rv_l}")
            lines.append(f"  Avg bars held          : {avg_bars:.1f}")
            lines.append(f"  ATR-stop exits         : {atr_stop}")
            if any("zone_age_bars" in t for t in zone_trades):
                ages = [float(t.get("zone_age_bars", 0)) for t in zone_trades]
                age_avg = (sum(ages) / len(ages)) if ages else 0.0
                lines.append(f"  Zone age avg (bars)    : {age_avg:.1f}")

    # Phase 6.2: Trend continuation section
    tc_activations = getattr(session, "trend_continuation_activations", 0)
    tc_entries     = getattr(session, "trend_continuation_entries", 0)
    tc_deactivations = getattr(session, "trend_continuation_deactivations", 0)
    tc_side        = getattr(session, "trend_continuation_side", "")
    if tc_activations > 0 or tc_entries > 0:
        tc_trades = [t for t in session.trades if t.get("reason") == "TREND_CONTINUATION" or t.get("source") == "TREND_CONTINUATION"]
        tc_wins   = sum(1 for t in tc_trades if t.get("pnl_pts", 0) > 0)
        tc_losses = sum(1 for t in tc_trades if t.get("pnl_pts", 0) <= 0)
        tc_pnl    = sum(t.get("pnl_pts", 0) for t in tc_trades)
        lines.append("")
        lines.append("  TREND CONTINUATION (Phase 6.2)")
        lines.append(sep2)
        lines.append(f"  Activations            : {tc_activations}  (directional tilt detected)")
        lines.append(f"  Continuation entries   : {tc_entries}  (re-entries in trend direction)")
        lines.append(f"  Deactivations          : {tc_deactivations}  (price returned to S4-R4 range)")
        lines.append(f"  Active side            : {tc_side or 'N/A'}")
        if tc_trades:
            lines.append(f"  W/L                    : {tc_wins}/{tc_losses}")
            lines.append(f"  Total P&L (pts)        : {tc_pnl:+.1f}")

    # Exit governance attribution
    tg_exits = sum(1 for t in session.trades if str(t.get("exit_reason", "")).upper() in {"TARGET_HIT", "TG_PARTIAL_EXIT"})
    rev_exits = sum(1 for t in session.trades if str(t.get("exit_reason", "")).upper() in {"REVERSAL_EXIT", "MOMENTUM_EXHAUSTION"})
    atr_exits = sum(
        1 for t in session.trades
        if str(t.get("exit_reason", "")).upper() in {"SL_HIT", "TIME_EXIT", "ST_FLIP", "OSC_EXHAUSTION", "MOMENTUM_EXIT"}
    )
    pt_tg_unreach = session.tag_counts.get("PT_TG_UNREACHABLE_EXIT", 0)
    tg_suppressed = session.tag_counts.get("TG_HIT_SUPPRESSED", 0)
    if tg_exits > 0 or rev_exits > 0 or atr_exits > 0 or pt_tg_unreach > 0 or tg_suppressed > 0:
        lines.append("")
        lines.append("  EXIT GOVERNANCE")
        lines.append(sep2)
        lines.append(f"  TG exits               : {tg_exits}")
        lines.append(f"  Reversal exits         : {rev_exits}")
        lines.append(f"  ATR exits              : {atr_exits}")
        lines.append(f"  PT/TG unreachable exits: {pt_tg_unreach}")
        lines.append(f"  TG hit but suppressed  : {tg_suppressed}")

    # ORB monitoring attribution
    orb_active = session.tag_counts.get("ORB_ACTIVE", 0)
    orb_expired = session.tag_counts.get("ORB_EXPIRED", 0)
    if orb_active > 0 or orb_expired > 0:
        lines.append("")
        lines.append("  ORB MONITORING")
        lines.append(sep2)
        lines.append(f"  ORB_ACTIVE logs        : {orb_active}")
        lines.append(f"  ORB_EXPIRED logs       : {orb_expired}")

    # Volatility context section
    vix_count     = getattr(session, "vix_tier_count",         0)
    greeks_count  = getattr(session, "greeks_usage_count",     0)
    theta_count   = getattr(session, "theta_penalty_count",    0)
    vega_count    = getattr(session, "vega_penalty_count",     0)
    vc_align      = getattr(session, "vol_context_align_count",  0)
    gr_align      = getattr(session, "greeks_align_count",       0)
    sc_matrix     = getattr(session, "score_matrix_usage_count", 0)
    if vix_count > 0 or greeks_count > 0 or theta_count > 0 or vega_count > 0 \
            or vc_align > 0 or gr_align > 0 or sc_matrix > 0:
        lines.append("")
        lines.append("  VOLATILITY CONTEXT")
        lines.append(sep2)
        lines.append(f"  VIX tier refreshes     : {vix_count}  (India VIX regime updates)")
        lines.append(f"  Greeks computed        : {greeks_count}  (BSM IV + Delta/Theta/Vega)")
        if theta_count > 0:
            theta_pct = (theta_count / session.total_trades * 100) if session.total_trades else 0.0
            lines.append(
                f"  Theta penalty entries  : {theta_count}  ({theta_pct:.1f}% of trades — high decay)"
            )
        else:
            lines.append(f"  Theta penalty entries  : {theta_count}")
        if vega_count > 0:
            vega_pct = (vega_count / session.total_trades * 100) if session.total_trades else 0.0
            lines.append(
                f"  Vega size reductions   : {vega_count}  ({vega_pct:.1f}% of trades — lots reduced)"
            )
        else:
            lines.append(f"  Vega size reductions   : {vega_count}")
        if vc_align > 0:
            lines.append(f"  Vol-indicator aligns   : {vc_align}  ([VOL_CONTEXT][ALIGN] per-side events)")
        if gr_align > 0:
            lines.append(f"  Greeks-indicator aligns: {gr_align}  ([GREEKS_ALIGN] per-side events)")
        if sc_matrix > 0:
            lines.append(f"  Score matrix audits    : {sc_matrix}  ([SCORE_MATRIX] per-side breakdowns)")

    # P5-F: Opening Scenario section (all P5 tags + alignment breakdown)
    obs = session.open_bias_stats
    if obs["open_bias_tag"] != "NONE" or session.total_trades:
        lines.append("")
        lines.append("  OPEN BIAS ALIGNMENT (P5)")
        lines.append(sep2)
        lines.append(f"  Open position bias : {obs['open_bias_tag']}")
        lines.append(f"  Open vs prev close : {obs.get('vs_close_tag', 'N/A')}")
        lines.append(f"  Gap scenario       : {obs.get('gap_tag', 'N/A')}")
        lines.append(f"  Balance zone open  : {obs.get('balance_tag', 'N/A')}")
        lines.append("")
        lines.append(
            f"  Aligned trades     : {obs['aligned_count']:>4}  ({obs['pct_aligned']:.1f}%)   "
            f"P&L: {obs['aligned_pnl']:+.2f} pts"
        )
        lines.append(
            f"  Misaligned trades  : {obs['misaligned_count']:>4}              "
            f"P&L: {obs['misaligned_pnl']:+.2f} pts"
        )
        lines.append(f"  Neutral trades     : {obs['neutral_count']:>4}")
        _aligned_w = sum(1 for t in session.trades if t.get("open_bias_aligned") == "ALIGNED" and t.get("pnl_pts", 0) > 0)
        _aligned_l = sum(1 for t in session.trades if t.get("open_bias_aligned") == "ALIGNED" and t.get("pnl_pts", 0) < 0)
        _mis_w = sum(1 for t in session.trades if t.get("open_bias_aligned") == "MISALIGNED" and t.get("pnl_pts", 0) > 0)
        _mis_l = sum(1 for t in session.trades if t.get("open_bias_aligned") == "MISALIGNED" and t.get("pnl_pts", 0) < 0)
        lines.append(f"  Aligned W/L        : {_aligned_w}/{_aligned_l}")
        lines.append(f"  Misaligned W/L     : {_mis_w}/{_mis_l}")
        if obs.get("is_gap_day"):
            lines.append(
                f"  Gap day P&L        : {obs['gap_day_pnl']:+.2f} pts  "
                f"({obs['gap_tag']})"
            )
        if obs.get("is_balance_day"):
            lines.append(
                f"  Balance day P&L    : {obs['balance_day_pnl']:+.2f} pts  "
                f"({obs['balance_tag']})"
            )

    # Survivability & Liquidity
    tg_suppressed = session.tag_counts.get("TG_HIT_EXIT_SUPPRESSED", 0)
    surv_override = session.tag_counts.get("SURVIVABILITY_OVERRIDE", 0)
    entry_skipped = session.tag_counts.get("ENTRY_ALLOWED_BUT_NOT_EXECUTED", 0)
    
    if tg_suppressed > 0 or surv_override > 0 or entry_skipped > 0:
        lines.append("")
        lines.append("  SURVIVABILITY & LIQUIDITY")
        lines.append(sep2)
        lines.append(f"  TG exit suppressed     : {tg_suppressed}  (held for min bars)")
        lines.append(f"  Survivability override : {surv_override}  (premature exit prevented)")
        lines.append(f"  Entry skipped (liq)    : {entry_skipped}  (signal OK but no option/cap)")

    # ── GREEKS PENALTIES (standalone) ─────────────────────────────────────────
    if theta_count > 0 or vega_count > 0:
        lines.append("")
        lines.append("  GREEKS PENALTIES")
        lines.append(sep2)
        _theta_score_est = theta_count * 8  # each penalty = −8 score pts
        if theta_count > 0:
            _theta_pct = (theta_count / session.total_trades * 100) if session.total_trades else 0.0
            lines.append(
                f"  Theta penalty entries  : {theta_count}"
                f"  ({_theta_pct:.1f}% of trades, ~{_theta_score_est} score pts lost)"
            )
        else:
            lines.append(f"  Theta penalty entries  : 0")
        if vega_count > 0:
            _vega_pct = (vega_count / session.total_trades * 100) if session.total_trades else 0.0
            lines.append(
                f"  Vega size reductions   : {vega_count}"
                f"  ({_vega_pct:.1f}% of trades — high IV exposure)"
            )
        else:
            lines.append(f"  Vega size reductions   : 0")

    # ── LOT SIZE ENFORCEMENT ───────────────────────────────────────────────────
    _lot_cap   = getattr(session, "lot_cap_count", 0)
    _lot_mis   = getattr(session, "lot_size_mismatch_count", 0)
    _intr_skip = getattr(session, "intrinsic_filter_count", 0)
    if _lot_cap > 0 or _lot_mis > 0 or _intr_skip > 0:
        lines.append("")
        lines.append("  LOT SIZE ENFORCEMENT")
        lines.append(sep2)
        lines.append(f"  MAX_TRADES_CAP blocks  : {_lot_cap}  (daily cap reached)")
        lines.append(f"  Lot size mismatches    : {_lot_mis}  (API lot ≠ config lot)")
        lines.append(f"  Intrinsic filter skips : {_intr_skip}  (zero-intrinsic contracts skipped)")
        lines.append(f"  Trend trades used      : {session.tag_counts.get('TREND_ENTRY', 0)} / 8")
        lines.append(f"  Scalp trades used      : {session.tag_counts.get('SCALP_ENTRY', 0)} / 12")

    # ── TRADE DETAILS table ────────────────────────────────────────────────────
    if session.total_trades > 0:
        lines.append("")
        lines.append("  TRADE DETAILS")
        lines.append(sep2)
        _hdr = (
            f"  {'#':>2}  {'Time':<8}  {'Side':<4}  {'Contract':<26}"
            f"  {'Entry':>7}  {'Exit':>7}  {'P&L(pts)':>9}  {'P&L(Rs)':>8}"
            f"  {'Bars':>4}  {'Lots':>4}  Reason"
        )
        lines.append(_hdr)
        lines.append("  " + "-" * (len(_hdr) - 2))
        for _i, _t in enumerate(session.trades, 1):
            _ts     = str(_t.get("bar_ts") or "")[-8:] or "?"
            _side   = (_t.get("side") or "?")[:4]
            _cname  = (_t.get("option_name") or "N/A")[:26]
            _eprem  = _t.get("entry_prem")
            _xprem  = _t.get("exit_prem")
            _pnl_p  = _t.get("pnl_pts", 0.0)
            _pnl_r  = _t.get("pnl_rs", 0.0)
            _bars   = _t.get("bars_held", "?")
            _lots   = _t.get("lot") or "?"
            _reason = (_t.get("exit_reason") or _t.get("outcome") or "?")[:12]
            lines.append(
                f"  {_i:>2}  {_ts:<8}  {_side:<4}  {_cname:<26}"
                f"  {_eprem if _eprem is not None else '?':>7}  "
                f"{_xprem if _xprem is not None else '?':>7}  "
                f"{_pnl_p:>+9.2f}  {_pnl_r:>+8.0f}"
                f"  {str(_bars):>4}  {str(_lots):>4}  {_reason}"
            )
        lines.append("  " + "-" * (len(_hdr) - 2))

    # ── P&L SUMMARY ───────────────────────────────────────────────────────────
    if session.total_trades > 0:
        lines.append("")
        lines.append("  P&L SUMMARY")
        lines.append(sep2)
        lines.append(
            f"  Net P&L       : {session.net_pnl_pts:>+8.2f} pts"
            f"  ({session.net_pnl_rs:>+.0f} Rs)"
        )
        lines.append(
            f"  Average P&L   : {session.avg_pnl_pts:>+8.2f} pts per trade"
        )
        # Best / worst trade
        _pnl_vals = [(t.get("pnl_pts", 0.0), t) for t in session.trades]
        if _pnl_vals:
            _best_pts, _best_t = max(_pnl_vals, key=lambda x: x[0])
            _worst_pts, _worst_t = min(_pnl_vals, key=lambda x: x[0])
            _best_side  = _best_t.get("side", "?")
            _worst_side = _worst_t.get("side", "?")
            _best_bar   = _best_t.get("bar") or str(_best_t.get("bar_ts") or "?")[-8:] or "?"
            _worst_bar  = _worst_t.get("bar") or str(_worst_t.get("bar_ts") or "?")[-8:] or "?"
            lines.append(
                f"  Best trade    : {_best_pts:>+8.2f} pts"
                f"  ({_best_side} bar={_best_bar})"
            )
            lines.append(
                f"  Worst trade   : {_worst_pts:>+8.2f} pts"
                f"  ({_worst_side} bar={_worst_bar})"
            )
        lines.append(
            f"  Survivability : {session.survivability_count}/{session.total_trades} trades"
            f"  ({session.survivability_ratio:.1f}%) held >=3 bars"
        )

    lines += ["", sep, ""]

    text = "\n".join(lines)
    output_path.write_text(text, encoding="utf-8")
    logger.info(f"[DASHBOARD] Text report saved -> {output_path}")
    return output_path


# ── Comparison report (baseline vs fixed) ─────────────────────────────────────

def compare_sessions(
    baseline_paths: List[str | Path],
    fixed_paths: List[str | Path],
    output_dir: str | Path = "reports",
) -> dict:
    """Compare baseline vs fixed log files and write a comparison report.

    Parameters
    ----------
    baseline_paths : List of log file paths representing the pre-fix baseline.
    fixed_paths    : List of log file paths with P1–P4 fixes active.
    output_dir     : Directory for the comparison report.

    Returns
    -------
    dict with keys: text, baseline_summary, fixed_summary
    """
    from log_parser import parse_multiple, SessionSummary

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    baseline_sessions = parse_multiple(baseline_paths)
    fixed_sessions    = parse_multiple(fixed_paths)

    def _aggregate(sessions: List[SessionSummary]) -> dict:
        trades = []
        for s in sessions:
            trades.extend(s.trades)
        total  = len(trades)
        wins   = sum(1 for t in trades if t.get("pnl_pts", 0) > 0)
        losses = sum(1 for t in trades if t.get("pnl_pts", 0) < 0)
        pnl    = sum(t.get("pnl_pts", 0) for t in trades)
        blocked_total = sum(s.total_blocked for s in sessions)
        all_blocked: Dict[str, int] = {}
        for s in sessions:
            for k, v in s.blocked_counts.items():
                all_blocked[k] = all_blocked.get(k, 0) + v
        all_tags: Dict[str, int] = {}
        for s in sessions:
            for k, v in s.tag_counts.items():
                all_tags[k] = all_tags.get(k, 0) + v
        reversal_trades   = sum(getattr(s, "reversal_trades_count", 0) for s in sessions)
        reversal_signals  = sum(getattr(s, "reversal_signal_count", 0) for s in sessions)
        slope_overrides   = sum(getattr(s, "st_slope_override_count", 0) for s in sessions)
        reversal_pnl      = round(sum(getattr(s, "reversal_pnl_attribution", 0.0) for s in sessions), 2)
        osc_blocks        = sum(getattr(s, "oscillator_blocks", 0) for s in sessions)
        osc_overrides     = sum(getattr(s, "oscillator_overrides", 0) for s in sessions)
        osc_relief        = sum(getattr(s, "oscillator_relief_count", 0) for s in sessions)
        trend_losses      = sum(getattr(s, "trend_loss_count", 0) for s in sessions)
        expiry_rolls      = sum(getattr(s, "expiry_roll_count", 0) for s in sessions)
        lot_mismatches    = sum(getattr(s, "lot_size_mismatch_count", 0) for s in sessions)
        intrinsic_skips   = sum(getattr(s, "intrinsic_filter_count", 0) for s in sessions)
        vix_tier_count    = sum(getattr(s, "vix_tier_count",         0) for s in sessions)
        greeks_usage      = sum(getattr(s, "greeks_usage_count",     0) for s in sessions)
        theta_penalties   = sum(getattr(s, "theta_penalty_count",    0) for s in sessions)
        vega_penalties    = sum(getattr(s, "vega_penalty_count",     0) for s in sessions)
        vc_aligns         = sum(getattr(s, "vol_context_align_count",  0) for s in sessions)
        gr_aligns         = sum(getattr(s, "greeks_align_count",       0) for s in sessions)
        sc_matrices       = sum(getattr(s, "score_matrix_usage_count", 0) for s in sessions)
        surv_count        = sum(getattr(s, "survivability_count",      0) for s in sessions)
        lot_cap           = sum(getattr(s, "lot_cap_count",            0) for s in sessions)
        zone_counts_all: Dict[str, int] = {}
        for s in sessions:
            for z, v in getattr(s, "zone_entry_counts", {}).items():
                zone_counts_all[z] = zone_counts_all.get(z, 0) + v
        bias_aligned_wins   = sum(
            1 for s in sessions for t in s.trades
            if t.get("open_bias_aligned") == "ALIGNED" and t.get("pnl_pts", 0) > 0
        )
        bias_aligned_losses = sum(
            1 for s in sessions for t in s.trades
            if t.get("open_bias_aligned") == "ALIGNED" and t.get("pnl_pts", 0) < 0
        )
        bias_misalign_wins  = sum(
            1 for s in sessions for t in s.trades
            if t.get("open_bias_aligned") == "MISALIGNED" and t.get("pnl_pts", 0) > 0
        )
        bias_misalign_losses = sum(
            1 for s in sessions for t in s.trades
            if t.get("open_bias_aligned") == "MISALIGNED" and t.get("pnl_pts", 0) < 0
        )
        return {
            "sessions":                 len(sessions),
            "total_trades":             total,
            "winners":                  wins,
            "losers":                   losses,
            "win_rate_pct":             round(wins / total * 100, 1) if total else 0.0,
            "net_pnl_pts":              round(pnl, 2),
            "total_blocked":            blocked_total,
            "blocked_counts":           all_blocked,
            "tag_counts":               all_tags,
            "reversal_trades_count":    reversal_trades,
            "reversal_signal_count":    reversal_signals,
            "reversal_pnl_attribution": reversal_pnl,
            "st_slope_override_count":  slope_overrides,
            "oscillator_blocks":        osc_blocks,
            "oscillator_overrides":     osc_overrides,
            "oscillator_relief_count":   osc_relief,
            "trend_loss_count":          trend_losses,
            "expiry_roll_count":         expiry_rolls,
            "lot_size_mismatch_count":   lot_mismatches,
            "intrinsic_filter_count":    intrinsic_skips,
            "vix_tier_count":            vix_tier_count,
            "greeks_usage_count":        greeks_usage,
            "theta_penalty_count":       theta_penalties,
            "vega_penalty_count":        vega_penalties,
            "vol_context_align_count":   vc_aligns,
            "greeks_align_count":        gr_aligns,
            "score_matrix_usage_count":  sc_matrices,
            "survivability_count":       surv_count,
            "survivability_ratio":       round(surv_count / total * 100, 1) if total else 0.0,
            "avg_pnl_pts":               round(pnl / total, 2) if total else 0.0,
            "lot_cap_count":             lot_cap,
            "zone_entry_counts":         zone_counts_all,
            "bias_aligned_wins":         bias_aligned_wins,
            "bias_aligned_losses":       bias_aligned_losses,
            "bias_misalign_wins":        bias_misalign_wins,
            "bias_misalign_losses":      bias_misalign_losses,
            "regime_context_count":      sum(getattr(s, "regime_context_count", 0) for s in sessions),
            "regime_adaptive_count":     sum(getattr(s, "regime_adaptive_count", 0) for s in sessions),
            # Phase 6
            "bias_alignment_count":      sum(getattr(s, "bias_alignment_count", 0) for s in sessions),
            "bar_close_alignment_count": sum(getattr(s, "bar_close_alignment_count", 0) for s in sessions),
            "slope_override_time_count": sum(getattr(s, "slope_override_time_count", 0) for s in sessions),
            "conflict_blocked_count":    sum(getattr(s, "conflict_blocked_count", 0) for s in sessions),
            "pulse_exhaustion_count":    sum(getattr(s, "pulse_exhaustion_count", 0) for s in sessions),
            "zone_absorption_count":     sum(getattr(s, "zone_absorption_count", 0) for s in sessions),
            "spread_noise_count":        sum(getattr(s, "spread_noise_count", 0) for s in sessions),
            # Phase 6.1
            "tilt_state_count":          sum(getattr(s, "tilt_state_count", 0) for s in sessions),
            "governance_easy_count":     sum(getattr(s, "governance_easy_count", 0) for s in sessions),
            "governance_strict_count":   sum(getattr(s, "governance_strict_count", 0) for s in sessions),
            "tilt_bias_override_count":  sum(getattr(s, "tilt_bias_override_count", 0) for s in sessions),
            # Phase 6.2
            "trend_continuation_activations": sum(getattr(s, "trend_continuation_activations", 0) for s in sessions),
            "trend_continuation_entries":     sum(getattr(s, "trend_continuation_entries", 0) for s in sessions),
        }

    base = _aggregate(baseline_sessions)
    fix  = _aggregate(fixed_sessions)

    text_path = out_dir / "comparison_report.txt"
    _write_comparison_text(base, fix, baseline_paths, fixed_paths, text_path)
    logger.info(f"[DASHBOARD] Comparison report saved → {text_path}")

    return {
        "text":             text_path,
        "baseline_summary": base,
        "fixed_summary":    fix,
    }


def _write_comparison_text(
    base: dict,
    fix: dict,
    baseline_paths: List,
    fixed_paths: List,
    output_path: Path,
) -> None:
    sep  = "=" * 70
    sep2 = "-" * 70

    def _delta(key: str, fmt: str = ".1f", higher_is_better: bool = True) -> str:
        bval = base.get(key, 0)
        fval = fix.get(key, 0)
        diff = fval - bval
        sign = "+" if diff >= 0 else ""
        arrow = "▲" if (diff > 0) == higher_is_better else ("▼" if diff != 0 else "=")
        return f"{fval:{fmt}} ({sign}{diff:{fmt}} {arrow})"

    lines = [
        sep,
        "  HEAD-TO-HEAD COMPARISON: BASELINE vs FIXED",
        sep,
        f"  Baseline logs  : {[str(p) for p in baseline_paths]}",
        f"  Fixed logs     : {[str(p) for p in fixed_paths]}",
        sep2,
        f"  {'Metric':<30} {'Baseline':>12} {'Fixed':>20}",
        sep2,
        f"  {'Sessions':<30} {base['sessions']:>12} {_fix_val(fix,'sessions'):>20}",
        f"  {'Total trades':<30} {base['total_trades']:>12} {_fix_val(fix,'total_trades'):>20}",
        f"  {'Winners':<30} {base['winners']:>12} {_fix_val(fix,'winners'):>20}",
        f"  {'Losers':<30} {base['losers']:>12} {_fix_val(fix,'losers'):>20}",
        f"  {'Win rate %':<30} {base['win_rate_pct']:>11.1f}% {_delta('win_rate_pct','.1f',True):>20}",
        f"  {'Net P&L (pts)':<30} {base['net_pnl_pts']:>+12.2f} {_delta('net_pnl_pts','+.2f',True):>20}",
        f"  {'Total blocked':<30} {base['total_blocked']:>12} {_delta('total_blocked','d',False):>20}",
        sep2,
    ]

    # Blocked breakdown comparison
    all_reasons = set(base["blocked_counts"]) | set(fix["blocked_counts"])
    if all_reasons:
        lines += ["", "  BLOCKED ENTRY BREAKDOWN", sep2]
        for r in sorted(all_reasons):
            bv = base["blocked_counts"].get(r, 0)
            fv = fix["blocked_counts"].get(r, 0)
            diff = fv - bv
            lines.append(f"  {r:<30} {bv:>12} {fv:>12}  ({'+' if diff>=0 else ''}{diff})")

    # Reversal / oscillator section
    rev_b = base.get("reversal_trades_count", 0)
    rev_f = fix.get("reversal_trades_count", 0)
    rev_sig_b = base.get("reversal_signal_count", 0)
    rev_sig_f = fix.get("reversal_signal_count", 0)
    slope_b = base.get("st_slope_override_count", 0)
    slope_f = fix.get("st_slope_override_count", 0)
    ob_b = base.get("oscillator_blocks", 0)
    ob_f = fix.get("oscillator_blocks", 0)
    oo_b = base.get("oscillator_overrides", 0)
    oo_f = fix.get("oscillator_overrides", 0)
    or_b = base.get("oscillator_relief_count", 0)
    or_f = fix.get("oscillator_relief_count", 0)
    tl_b = base.get("trend_loss_count", 0)
    tl_f = fix.get("trend_loss_count", 0)
    er_b  = base.get("expiry_roll_count", 0)
    er_f  = fix.get("expiry_roll_count", 0)
    lm_b  = base.get("lot_size_mismatch_count", 0)
    lm_f  = fix.get("lot_size_mismatch_count", 0)
    is_b  = base.get("intrinsic_filter_count", 0)
    is_f  = fix.get("intrinsic_filter_count", 0)
    sv_b  = base.get("survivability_ratio", 0.0)
    sv_f  = fix.get("survivability_ratio", 0.0)
    lc_b  = base.get("lot_cap_count", 0)
    lc_f  = fix.get("lot_cap_count", 0)
    if any([rev_b, rev_f, rev_sig_b, rev_sig_f, slope_b, slope_f,
            ob_b, ob_f, oo_b, oo_f, or_b, or_f, tl_b, tl_f, sv_b, sv_f]):
        lines += ["", "  REVERSAL & OSCILLATOR GATING", sep2]
        for label, bv, fv, hib in [
            ("Reversal signals",             rev_sig_b, rev_sig_f, True),
            ("Reversal trades",              rev_b,   rev_f,   True),
            ("ST_SLOPE overrides",           slope_b, slope_f, True),
            ("OSC blocks (extreme)",         ob_b,    ob_f,    False),
            ("OSC overrides (ADX)",          oo_b,    oo_f,    True),
            ("OSC relief (S4/R4 break)",     or_b,    or_f,    True),
            ("Trend SL exits",               tl_b,    tl_f,    False),
            ("Survivability ratio % (▲=better)", sv_b, sv_f,  True),
        ]:
            diff = fv - bv
            arrow = "▲" if (diff > 0) == hib else ("▼" if diff != 0 else "=")
            lines.append(
                f"  {label:<30} {bv:>12} {fv:>12}  ({'+' if diff>=0 else ''}{diff} {arrow})"
            )

    # Open bias alignment comparison
    ba_w_b = base.get("bias_aligned_wins",   0)
    ba_w_f = fix.get("bias_aligned_wins",    0)
    ba_l_b = base.get("bias_aligned_losses", 0)
    ba_l_f = fix.get("bias_aligned_losses",  0)
    bm_w_b = base.get("bias_misalign_wins",  0)
    bm_w_f = fix.get("bias_misalign_wins",   0)
    bm_l_b = base.get("bias_misalign_losses",0)
    bm_l_f = fix.get("bias_misalign_losses", 0)
    if any([ba_w_b, ba_w_f, ba_l_b, ba_l_f, bm_w_b, bm_w_f, bm_l_b, bm_l_f]):
        lines += ["", "  OPEN BIAS ALIGNMENT", sep2]
        for label, bv, fv, hib in [
            ("Aligned   W (▲=more wins)",     ba_w_b, ba_w_f, True),
            ("Aligned   L (▼=fewer losses)",  ba_l_b, ba_l_f, False),
            ("Misaligned W",                  bm_w_b, bm_w_f, True),
            ("Misaligned L (▼=fewer losses)", bm_l_b, bm_l_f, False),
        ]:
            diff = fv - bv
            arrow = "▲" if (diff > 0) == hib else ("▼" if diff != 0 else "=")
            lines.append(
                f"  {label:<35} {bv:>9} {fv:>9}  ({'+' if diff>=0 else ''}{diff} {arrow})"
            )

    # Contract roll / expiry comparison
    if any([er_b, er_f, lm_b, lm_f, is_b, is_f, lc_b, lc_f]):
        lines += ["", "  CONTRACT ROLL / EXPIRY", sep2]
        for label, bv, fv, hib in [
            ("Expiry rolls (▲ = better continuity)", er_b, er_f, True),
            ("Lot size mismatches (▼ = better)",     lm_b, lm_f, False),
            ("Intrinsic filter skips",               is_b, is_f, False),
            ("MAX_TRADES_CAP blocks (▼ = better)",   lc_b, lc_f, False),
        ]:
            diff  = fv - bv
            arrow = "▲" if (diff > 0) == hib else ("▼" if diff != 0 else "=")
            lines.append(
                f"  {label:<40} {bv:>8} {fv:>8}  ({'+' if diff>=0 else ''}{diff} {arrow})"
            )

    # Volatility context comparison
    vc_b  = base.get("vix_tier_count",         0)
    vc_f  = fix.get("vix_tier_count",          0)
    gr_b  = base.get("greeks_usage_count",     0)
    gr_f  = fix.get("greeks_usage_count",      0)
    tp_b  = base.get("theta_penalty_count",    0)
    tp_f  = fix.get("theta_penalty_count",     0)
    vp_b  = base.get("vega_penalty_count",     0)
    vp_f  = fix.get("vega_penalty_count",      0)
    vca_b = base.get("vol_context_align_count",  0)
    vca_f = fix.get("vol_context_align_count",   0)
    gra_b = base.get("greeks_align_count",       0)
    gra_f = fix.get("greeks_align_count",        0)
    scm_b = base.get("score_matrix_usage_count", 0)
    scm_f = fix.get("score_matrix_usage_count",  0)
    if any([vc_b, vc_f, gr_b, gr_f, tp_b, tp_f, vp_b, vp_f,
            vca_b, vca_f, gra_b, gra_f, scm_b, scm_f]):
        lines += ["", "  VOLATILITY CONTEXT", sep2]
        for label, bv, fv, hib in [
            ("VIX tier refreshes (▲ = more aware)",        vc_b,  vc_f,  True),
            ("Greeks computed (▲ = more coverage)",        gr_b,  gr_f,  True),
            ("Theta penalties (▼ = fewer = better)",       tp_b,  tp_f,  False),
            ("Vega size cuts (▼ = fewer = better)",        vp_b,  vp_f,  False),
            ("Vol-indicator aligns (▲ = more audited)",    vca_b, vca_f, True),
            ("Greeks aligns (▲ = more audited)",           gra_b, gra_f, True),
            ("Score matrix events (▲ = more adjustments)", scm_b, scm_f, True),
        ]:
            diff  = fv - bv
            arrow = "▲" if (diff > 0) == hib else ("▼" if diff != 0 else "=")
            lines.append(
                f"  {label:<40} {bv:>8} {fv:>8}  ({'+' if diff>=0 else ''}{diff} {arrow})"
            )

    # P1-P4 tag comparison
    all_tags = set(base["tag_counts"]) | set(fix["tag_counts"])
    if all_tags:
        lines += ["", "  P1-P4 TAGS FIRED", sep2]
        for tag in sorted(all_tags):
            bv = base["tag_counts"].get(tag, 0)
            fv = fix["tag_counts"].get(tag, 0)
            lines.append(f"  [{tag}]{'':>3}{bv:>12}  →  {fv}")

    lines += ["", sep, ""]
    output_path.write_text("\n".join(lines), encoding="utf-8")


def _fix_val(d: dict, key: str) -> str:
    return str(d.get(key, 0))


# ── CLI entry point ────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m dashboard",
        description="Trading engine dashboard — parse logs and emit reports.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # report sub-command
    rep = sub.add_parser("report", help="Generate a full report from a log file.")
    rep.add_argument("log", help="Path to the session log file.")
    rep.add_argument("-o", "--output-dir", default="reports",
                     help="Output directory (default: reports/)")

    # compare sub-command
    cmp = sub.add_parser("compare", help="Compare baseline vs fixed log files.")
    cmp.add_argument("--baseline", nargs="+", required=True,
                     help="Baseline log file(s) (pre-fix).")
    cmp.add_argument("--fixed", nargs="+", required=True,
                     help="Fixed log file(s) (P1–P4 active).")
    cmp.add_argument("-o", "--output-dir", default="reports",
                     help="Output directory (default: reports/)")

    return p


def _main(argv=None) -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    args = _build_parser().parse_args(argv)

    if args.command == "report":
        result = generate_full_report(args.log, output_dir=args.output_dir)
        print(f"\nArtifacts written to: {args.output_dir}")
        for key, val in result.items():
            if key != "summary" and val:
                print(f"  {key:<6}: {val}")

    elif args.command == "compare":
        result = compare_sessions(
            baseline_paths=args.baseline,
            fixed_paths=args.fixed,
            output_dir=args.output_dir,
        )
        print(f"\nComparison report: {result['text']}")
        b = result["baseline_summary"]
        f = result["fixed_summary"]
        print(f"  Baseline: {b['total_trades']} trades, "
              f"{b['win_rate_pct']:.1f}% win, {b['net_pnl_pts']:+.2f} pts")
        print(f"  Fixed:    {f['total_trades']} trades, "
              f"{f['win_rate_pct']:.1f}% win, {f['net_pnl_pts']:+.2f} pts")


if __name__ == "__main__":
    _main()
