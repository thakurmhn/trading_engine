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
    }


def print_summary(summary: dict) -> None:
    """Print a formatted summary to stdout."""
    sep = "=" * 55
    print(f"\n{sep}")
    print("  SESSION DASHBOARD — TRADE SUMMARY")
    print(sep)
    print(f"  Total trades   : {summary['total_trades']}")
    print(f"  Winners        : {summary['winners']}")
    print(f"  Losers         : {summary['losers']}")
    print(f"  Breakeven      : {summary['breakeven']}")
    print(f"  Win rate       : {summary['win_rate_pct']:.1f}%")
    print(f"  Net P&L (pts)  : {summary['net_pnl_points']:+.2f}")
    print(f"  Net P&L (Rs)   : ₹{summary['net_pnl_rupees']:+,.2f}")
    print(f"  Max win (pts)  : {summary['max_win_points']:+.2f}")
    print(f"  Max loss (pts) : {summary['max_loss_points']:+.2f}")
    print(sep)
    print(f"  CALL trades    : {summary['call_trades']}  P&L: {summary['call_pnl_points']:+.2f} pts")
    print(f"  PUT  trades    : {summary['put_trades']}  P&L: {summary['put_pnl_points']:+.2f} pts")
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
        f"[DASHBOARD] Full report: trades={session.total_trades} "
        f"win={session.win_rate_pct}% net={session.net_pnl_pts:+.2f}pts "
        f"blocked={session.total_blocked}"
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

    lines += ["", sep, ""]

    text = "\n".join(lines)
    output_path.write_text(text, encoding="utf-8")
    logger.info(f"[DASHBOARD] Text report saved → {output_path}")
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
        return {
            "sessions":        len(sessions),
            "total_trades":    total,
            "winners":         wins,
            "losers":          losses,
            "win_rate_pct":    round(wins / total * 100, 1) if total else 0.0,
            "net_pnl_pts":     round(pnl, 2),
            "total_blocked":   blocked_total,
            "blocked_counts":  all_blocked,
            "tag_counts":      all_tags,
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
