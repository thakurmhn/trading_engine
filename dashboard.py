"""Trade dashboard: log parser, structured report, and equity-curve plotter.

Called at the end of each live/paper/replay session to produce:
  • A structured CSV of all trades parsed from the session log.
  • A summary dict  (total trades, win %, net P&L, CALL vs PUT split).
  • A PNG equity-curve  (cumulative P&L over trade sequence).

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

Log-parsing targets
-------------------
[ENTRY DISPATCH] lines  — emitted by st_pullback_cci.place_st_pullback_entry()
[EXIT AUDIT]    lines  — emitted by execution.check_exit_condition() and
                          execution.cleanup_trade_exit()
"""

from __future__ import annotations

import csv
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
