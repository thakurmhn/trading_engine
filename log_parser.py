"""log_parser.py — Trading engine log parsing engine.

Parses structured log tags emitted by the trading engine:

  [TRADE OPEN][REPLAY|PAPER|LIVE]  — entry filled
  [TRADE EXIT]                     — exit filled (WIN/LOSS + P&L)
  [ENTRY BLOCKED][subtype]         — entry suppressed by a gate
  [ENTRY OK]                       — entry passed all gates
  [SIGNAL FIRED]                   — signal generated (pre-gate)
  [EXIT AUDIT]                     — legacy detailed exit record

Produces per-trade dicts and a SessionSummary for each log file.

Usage
-----
    from log_parser import LogParser, SessionSummary

    parser = LogParser("options_trade_engine_2026-02-24.log")
    summary: SessionSummary = parser.parse()

    for trade in summary.trades:
        print(trade["side"], trade["pnl_pts"], trade["bars_held"])

    print(summary.blocked_counts)   # {"ST_CONFLICT": 371, ...}
    print(summary.win_rate_pct)
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

# ── ANSI strip ────────────────────────────────────────────────────────────────
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _strip(line: str) -> str:
    return _ANSI.sub("", line)


# ── Regex patterns ─────────────────────────────────────────────────────────────

# Log-line timestamp prefix: "2026-02-24 11:02:48,553 - INFO - "
_RE_LOG_TS = re.compile(
    r"^(?P<log_ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
)

# [TRADE OPEN][REPLAY|PAPER|LIVE] CALL bar=791 2026-02-20 09:45:00
#   underlying=25497.75 premium=153.00 score=83 src=PIVOT pivot=BREAKOUT_R3
#   cpr=NARROW day=UNKNOWN max_hold=23bars trail_min=35pts trail_step=12% lot=130
_RE_TRADE_OPEN = re.compile(
    r"\[TRADE OPEN\]\[(?P<session_type>\w+)\] (?P<side>CALL|PUT) "
    r"bar=(?P<bar>\d+) "
    r"(?P<bar_ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) "
    r"underlying=(?P<underlying>[\d.]+) premium=(?P<premium>[\d.]+) "
    r"score=(?P<score>\d+) src=(?P<src>\S+)"
    r"(?:.*?pivot=(?P<pivot>\S+))?"
    r"(?:.*?cpr=(?P<cpr>\S+))?"
    r"(?:.*?day=(?P<day>[A-Z_]+))?"
    r"(?:.*?lot=(?P<lot>\d+))?",
    re.IGNORECASE,
)

# New format:  [TRADE EXIT] WIN/LOSS  CALL bar=798 2026-02-20 10:06:00
#               prem 153.00→143.61 P&L=-9.39pts (-1221₹) peak=165.56 held=7bars
# Legacy format: [TRADE EXIT][REASON] WIN/LOSS CALL bar=811 2026-02-20 10:45:00
#               premium 153.00→201.23 P&L=+48.23pts (+6269₹) peak=204.20 held=20bars
# Also handles ASCII arrow ->  and suffix Rs instead of ₹
_RE_TRADE_EXIT = re.compile(
    r"\[TRADE EXIT\](?:\[(?P<exit_reason>\w+)\])? "
    r"(?P<outcome>WIN|LOSS)\s+(?P<side>CALL|PUT) "
    r"bar=(?P<bar>\d+) "
    r"(?P<bar_ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) "
    r"prem(?:ium)? (?P<entry_prem>[\d.]+)(?:\u2192|->)(?P<exit_prem>[\d.]+) "
    r"P&L=(?P<pnl_pts>[+-][\d.]+)pts "
    r"\((?P<pnl_rs>[+-]\d+)[^)]*\) "
    r"peak=(?P<peak>[\d.]+) "
    r"held=(?P<bars_held>\d+)bars",
    re.IGNORECASE,
)

# [ENTRY BLOCKED][ST_CONFLICT] ...
# [ENTRY BLOCKED][COOLDOWN] 0s < 120s
_RE_ENTRY_BLOCKED = re.compile(r"\[ENTRY BLOCKED\]\[(?P<subtype>\w+)\]")

# [ENTRY OK] CALL score=83/50 NORMAL HIGH | ST=20/20 RSI=61.7...
_RE_ENTRY_OK = re.compile(
    r"\[ENTRY OK\] (?P<side>CALL|PUT) score=(?P<score>\d+)/(?P<threshold>\d+)"
    r" (?P<regime>\w+) (?P<strength>\w+)"
    r"(?:.*?pivot=(?P<pivot>\S+))?",
    re.IGNORECASE,
)

# [SIGNAL FIRED] CALL score=83 strength=HIGH | ...
_RE_SIGNAL_FIRED = re.compile(
    r"\[SIGNAL FIRED\] (?P<side>CALL|PUT) score=(?P<score>\d+) "
    r"strength=(?P<strength>\w+)",
    re.IGNORECASE,
)

# Legacy [EXIT AUDIT] — emitted by execution.py
_RE_EXIT_AUDIT = re.compile(
    r"\[EXIT AUDIT\]"
    r".*?timestamp=(?P<timestamp>\S+)"
    r".*?option_type=(?P<option_type>CALL|PUT)"
    r".*?exit_type=(?P<exit_type>\S+)"
    r".*?reason=(?P<reason>\S+)"
    r"(?:.*?bars_held=(?P<bars_held>[-\d]+))?"
    r"(?:.*?position_id=(?P<position_id>\S+))?"
    r"(?:.*?premium_move=(?P<premium_move>[-\d.]+))?",
    re.IGNORECASE,
)

# P1–P5 tag presence detection (for tag-fired counts)
_P_TAGS = [
    "FALSE_BREAKOUT_COOLDOWN",
    "SCALP_OVERRIDE",
    "TG_PARTIAL_EXIT",
    "DYNAMIC_EXIT_THRESHOLD",
    "VOL_REVERSION_REFINED",
    "SLIPPAGE_MODELED",
    "MAX_TRADES_CAP",
    "BALANCE_ZONE",
    "CAMARILLA_BIAS",
    "CPR_PRECLASS",
    "COMPRESSION_FORECAST",
    "COMPRESSION_START",
    "EXPANSION_CONFIRMED",
    "COMPRESSION_DISSOLVED",
    "OPEN_POSITION",       # P5-A
    "OPEN_BIAS_SCORE",     # P5 scoring log
    "OPEN_ABOVE_CLOSE",    # P5-B
    "OPEN_BELOW_CLOSE",    # P5-B
    "OPEN_CLOSE_EQUAL",    # P5-B
    "GAP_UP",              # P5-C
    "GAP_DOWN",            # P5-C
    "NO_GAP",              # P5-C
    "BALANCE_OPEN",        # P5-D
    "OUTSIDE_BALANCE",     # P5-D
]
_RE_TAG_ANY = re.compile(r"\[(" + "|".join(_P_TAGS) + r")\]")

# [OPEN_POSITION] tag=OPEN_HIGH open=25000.00 high=25000.00 low=24850.00 ...
_RE_OPEN_POSITION = re.compile(
    r"\[OPEN_POSITION\] tag=(?P<open_bias_tag>OPEN_HIGH|OPEN_LOW|NONE)"
    r".*?open=(?P<open_px>[\d.]+)"
    r".*?high=(?P<high_px>[\d.]+)"
    r".*?low=(?P<low_px>[\d.]+)",
    re.IGNORECASE,
)

# [OPEN_ABOVE_CLOSE] open=25100.00 prev_close=25000.00 ...
# [OPEN_BELOW_CLOSE] open=24900.00 ...
# [OPEN_CLOSE_EQUAL] open=25000.00 ...
_RE_OPEN_VS_CLOSE = re.compile(
    r"\[(?P<vs_close_tag>OPEN_ABOVE_CLOSE|OPEN_BELOW_CLOSE|OPEN_CLOSE_EQUAL)\]"
    r".*?open=(?P<open_px>[\d.]+)",
    re.IGNORECASE,
)

# [GAP_UP] open=25200.00 prev_high=25100.00 prev_low=24900.00 ...
# [GAP_DOWN] open=24800.00 ...
# [NO_GAP]  open=25000.00 ...
_RE_GAP = re.compile(
    r"\[(?P<gap_tag>GAP_UP|GAP_DOWN|NO_GAP)\]"
    r".*?open=(?P<open_px>[\d.]+)",
    re.IGNORECASE,
)

# [BALANCE_OPEN]    open=25000.00 bc=24990.00 tc=25010.00
# [OUTSIDE_BALANCE] open=25100.00 bc=24990.00 tc=25010.00
_RE_BALANCE_OPEN = re.compile(
    r"\[(?P<balance_tag>BALANCE_OPEN|OUTSIDE_BALANCE)\]"
    r".*?open=(?P<open_px>[\d.]+)",
    re.IGNORECASE,
)


# ── SessionSummary dataclass ──────────────────────────────────────────────────

@dataclass
class SessionSummary:
    """Aggregated statistics for a single log-file session."""

    log_path:       str
    session_type:   str                  # REPLAY, PAPER, LIVE, or MIXED
    date_tag:       str                  # e.g. "2026-02-24"

    trades:         List[dict] = field(default_factory=list)
    blocked_counts: Dict[str, int] = field(default_factory=dict)
    tag_counts:     Dict[str, int] = field(default_factory=dict)
    signals_fired:  int = 0
    entry_ok_count: int = 0
    open_bias_tag:  str = "NONE"          # P5-A: OPEN_HIGH | OPEN_LOW | NONE
    vs_close_tag:   str = "OPEN_CLOSE_EQUAL"   # P5-B: OPEN_ABOVE_CLOSE | OPEN_BELOW_CLOSE | OPEN_CLOSE_EQUAL
    gap_tag:        str = "NO_GAP"        # P5-C: GAP_UP | GAP_DOWN | NO_GAP
    balance_tag:    str = "OUTSIDE_BALANCE"    # P5-D: BALANCE_OPEN | OUTSIDE_BALANCE

    # Computed on demand
    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def winners(self) -> int:
        return sum(1 for t in self.trades if t.get("pnl_pts", 0.0) > 0)

    @property
    def losers(self) -> int:
        return sum(1 for t in self.trades if t.get("pnl_pts", 0.0) < 0)

    @property
    def breakeven(self) -> int:
        return sum(1 for t in self.trades if t.get("pnl_pts", 0.0) == 0)

    @property
    def win_rate_pct(self) -> float:
        return round(self.winners / self.total_trades * 100, 1) if self.total_trades else 0.0

    @property
    def net_pnl_pts(self) -> float:
        return round(sum(t.get("pnl_pts", 0.0) for t in self.trades), 2)

    @property
    def net_pnl_rs(self) -> float:
        return round(sum(t.get("pnl_rs", 0.0) for t in self.trades), 2)

    @property
    def call_trades(self) -> int:
        return sum(1 for t in self.trades if t.get("side") == "CALL")

    @property
    def put_trades(self) -> int:
        return sum(1 for t in self.trades if t.get("side") == "PUT")

    @property
    def total_blocked(self) -> int:
        return sum(self.blocked_counts.values())

    @property
    def exit_reason_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = defaultdict(int)
        for t in self.trades:
            reason = t.get("exit_reason") or t.get("outcome") or "UNKNOWN"
            counts[reason] += 1
        return dict(counts)

    @property
    def open_bias_stats(self) -> dict:
        """P5-F: Alignment of executed trades vs. session open bias (all P5 tags)."""
        aligned    = [t for t in self.trades if t.get("open_bias_aligned") == "ALIGNED"]
        misaligned = [t for t in self.trades if t.get("open_bias_aligned") == "MISALIGNED"]
        neutral    = [t for t in self.trades if t.get("open_bias_aligned") == "NEUTRAL"]
        aligned_pnl    = round(sum(t.get("pnl_pts", 0) for t in aligned), 2)
        misaligned_pnl = round(sum(t.get("pnl_pts", 0) for t in misaligned), 2)
        total = self.total_trades
        is_gap_day     = self.gap_tag in ("GAP_UP", "GAP_DOWN")
        is_balance_day = self.balance_tag == "BALANCE_OPEN"
        all_pnl        = round(sum(t.get("pnl_pts", 0) for t in self.trades), 2)
        return {
            "open_bias_tag":    self.open_bias_tag,
            "vs_close_tag":     self.vs_close_tag,
            "gap_tag":          self.gap_tag,
            "balance_tag":      self.balance_tag,
            "aligned_count":    len(aligned),
            "misaligned_count": len(misaligned),
            "neutral_count":    len(neutral),
            "aligned_pnl":      aligned_pnl,
            "misaligned_pnl":   misaligned_pnl,
            "pct_aligned":      round(len(aligned) / total * 100, 1) if total else 0.0,
            "is_gap_day":       is_gap_day,
            "is_balance_day":   is_balance_day,
            "gap_day_pnl":      all_pnl if is_gap_day else 0.0,
            "balance_day_pnl":  all_pnl if is_balance_day else 0.0,
        }

    def to_dict(self) -> dict:
        return {
            "log_path":        self.log_path,
            "session_type":    self.session_type,
            "date_tag":        self.date_tag,
            "total_trades":    self.total_trades,
            "winners":         self.winners,
            "losers":          self.losers,
            "breakeven":       self.breakeven,
            "win_rate_pct":    self.win_rate_pct,
            "net_pnl_pts":     self.net_pnl_pts,
            "net_pnl_rs":      self.net_pnl_rs,
            "call_trades":     self.call_trades,
            "put_trades":      self.put_trades,
            "signals_fired":   self.signals_fired,
            "entry_ok_count":  self.entry_ok_count,
            "total_blocked":   self.total_blocked,
            "blocked_counts":  self.blocked_counts,
            "tag_counts":      self.tag_counts,
            "exit_reasons":    self.exit_reason_counts,
            "open_bias_tag":   self.open_bias_tag,
            "vs_close_tag":    self.vs_close_tag,
            "gap_tag":         self.gap_tag,
            "balance_tag":     self.balance_tag,
            "open_bias_stats": self.open_bias_stats,
        }


# ── LogParser ─────────────────────────────────────────────────────────────────

class LogParser:
    """Parse a single trading engine log file into a SessionSummary.

    Supports two trade-record formats:
      1. New format: [TRADE OPEN] + [TRADE EXIT] pairs (replay / paper / live)
      2. Legacy format: [EXIT AUDIT] lines only

    When both are present, new-format pairs take precedence and EXIT AUDIT
    records are merged in for any remaining unmatched exits.
    """

    def __init__(self, log_path: str | Path) -> None:
        self.log_path = Path(log_path)

    # ── public ────────────────────────────────────────────────────────────────

    def parse(self) -> SessionSummary:
        """Parse the log file and return a SessionSummary."""
        if not self.log_path.exists():
            return SessionSummary(
                log_path=str(self.log_path),
                session_type="UNKNOWN",
                date_tag="",
            )

        date_tag = self._extract_date_tag()
        (trades, session_types, blocked, tags, signals, ok_count,
         open_bias_tag, vs_close_tag, gap_tag, balance_tag) = self._scan_file()

        if session_types:
            session_type = session_types.pop() if len(session_types) == 1 else "MIXED"
        else:
            session_type = "UNKNOWN"

        return SessionSummary(
            log_path=str(self.log_path),
            session_type=session_type,
            date_tag=date_tag,
            trades=trades,
            blocked_counts=dict(blocked),
            tag_counts=dict(tags),
            signals_fired=signals,
            entry_ok_count=ok_count,
            open_bias_tag=open_bias_tag,
            vs_close_tag=vs_close_tag,
            gap_tag=gap_tag,
            balance_tag=balance_tag,
        )

    # ── private ───────────────────────────────────────────────────────────────

    def _extract_date_tag(self) -> str:
        """Return YYYY-MM-DD from the log filename, or empty string."""
        m = re.search(r"(\d{4}-\d{2}-\d{2})", self.log_path.name)
        return m.group(1) if m else ""

    def _scan_file(self):
        """Single-pass scan of the log file.

        Returns
        -------
        (trades, session_types, blocked, tags, signals_fired, entry_ok_count,
         open_bias_tag)
        """
        trades: List[dict] = []
        open_queue: List[dict] = []   # pending TRADE OPEN records (FIFO)
        session_types: set = set()
        blocked: Dict[str, int] = defaultdict(int)
        tags: Dict[str, int] = defaultdict(int)
        signals_fired: int = 0
        entry_ok_count: int = 0
        open_bias_tag: str = "NONE"          # P5-A: OPEN_HIGH | OPEN_LOW | NONE
        vs_close_tag:  str = "OPEN_CLOSE_EQUAL"  # P5-B: OPEN_ABOVE_CLOSE | OPEN_BELOW_CLOSE | OPEN_CLOSE_EQUAL
        gap_tag:       str = "NO_GAP"        # P5-C: GAP_UP | GAP_DOWN | NO_GAP
        balance_tag:   str = "OUTSIDE_BALANCE"   # P5-D: BALANCE_OPEN | OUTSIDE_BALANCE

        # Keep EXIT AUDIT records as fallback when no TRADE OPEN+EXIT pairs found
        audit_records: List[dict] = []

        with self.log_path.open(encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                line = _strip(raw_line)

                # ── [TRADE OPEN] ──────────────────────────────────────────
                m = _RE_TRADE_OPEN.search(line)
                if m:
                    d = m.groupdict()
                    log_ts = self._log_ts(line)
                    open_queue.append({
                        "session_type": d["session_type"].upper(),
                        "side":         d["side"].upper(),
                        "bar":          int(d["bar"]),
                        "bar_ts":       d["bar_ts"],
                        "log_ts":       log_ts,
                        "underlying":   float(d["underlying"]),
                        "entry_prem":   float(d["premium"]),
                        "score":        int(d["score"]),
                        "src":          d.get("src") or "",
                        "pivot":        d.get("pivot") or "",
                        "cpr":          d.get("cpr") or "",
                        "day_type":     d.get("day") or "",
                        "lot":          int(d["lot"]) if d.get("lot") else 0,
                    })
                    session_types.add(d["session_type"].upper())
                    continue

                # ── [TRADE EXIT] ──────────────────────────────────────────
                m = _RE_TRADE_EXIT.search(line)
                if m:
                    d = m.groupdict()
                    log_ts = self._log_ts(line)
                    # exit_reason: prefer the bracket tag (ST_FLIP_2 etc.),
                    # fall back to WIN/LOSS outcome string
                    reason = (d.get("exit_reason") or "").upper() or d["outcome"].upper()
                    exit_rec = {
                        "outcome":     d["outcome"].upper(),
                        "side":        d["side"].upper(),
                        "exit_bar":    int(d["bar"]),
                        "exit_bar_ts": d["bar_ts"],
                        "log_ts":      log_ts,
                        "exit_prem":   float(d["exit_prem"]),
                        "entry_prem":  float(d["entry_prem"]),
                        "pnl_pts":     float(d["pnl_pts"]),
                        "pnl_rs":      float(d["pnl_rs"]),
                        "peak":        float(d["peak"]),
                        "bars_held":   int(d["bars_held"]),
                        "exit_reason": reason,
                    }
                    # Merge with matching open record (FIFO on same side)
                    matched_open = self._pop_open(open_queue, d["side"].upper())
                    if matched_open:
                        trade = {**matched_open, **exit_rec}
                    else:
                        trade = exit_rec
                    trades.append(trade)
                    continue

                # ── [ENTRY BLOCKED] ───────────────────────────────────────
                m = _RE_ENTRY_BLOCKED.search(line)
                if m:
                    blocked[m.group("subtype").upper()] += 1
                    continue

                # ── [ENTRY OK] ────────────────────────────────────────────
                m = _RE_ENTRY_OK.search(line)
                if m:
                    entry_ok_count += 1
                    continue

                # ── [SIGNAL FIRED] ────────────────────────────────────────
                m = _RE_SIGNAL_FIRED.search(line)
                if m:
                    signals_fired += 1
                    continue

                # ── [OPEN_POSITION] (P5-A) — extract bias tag before generic check ──
                m = _RE_OPEN_POSITION.search(line)
                if m:
                    open_bias_tag = m.group("open_bias_tag").upper()
                    tags["OPEN_POSITION"] += 1
                    continue

                # ── [OPEN_ABOVE/BELOW_CLOSE / OPEN_CLOSE_EQUAL] (P5-B) ────
                m = _RE_OPEN_VS_CLOSE.search(line)
                if m:
                    vs_close_tag = m.group("vs_close_tag").upper()
                    tags[vs_close_tag] += 1
                    continue

                # ── [GAP_UP / GAP_DOWN / NO_GAP] (P5-C) ──────────────────
                m = _RE_GAP.search(line)
                if m:
                    gap_tag = m.group("gap_tag").upper()
                    tags[gap_tag] += 1
                    continue

                # ── [BALANCE_OPEN / OUTSIDE_BALANCE] (P5-D) ──────────────
                m = _RE_BALANCE_OPEN.search(line)
                if m:
                    balance_tag = m.group("balance_tag").upper()
                    tags[balance_tag] += 1
                    continue

                # ── P1–P5 tags ────────────────────────────────────────────
                m = _RE_TAG_ANY.search(line)
                if m:
                    tags[m.group(1)] += 1
                    continue

                # ── [EXIT AUDIT] (legacy) ─────────────────────────────────
                m = _RE_EXIT_AUDIT.search(line)
                if m:
                    d = m.groupdict()
                    audit_records.append({
                        "side":        (d.get("option_type") or "").upper(),
                        "exit_reason": d.get("reason") or "",
                        "bars_held":   int(d["bars_held"]) if d.get("bars_held") not in (None, "") else -1,
                        "pnl_pts":     float(d["premium_move"]) if d.get("premium_move") not in (None, "") else 0.0,
                        "pnl_rs":      0.0,
                    })

        # If no TRADE OPEN/EXIT pairs found, fall back to EXIT AUDIT records
        if not trades and audit_records:
            trades = audit_records

        # P5-F: annotate every trade with open_bias_aligned (extended P5-E logic)
        for trade in trades:
            side = trade.get("side", "")
            call_aligned = side == "CALL" and (
                open_bias_tag == "OPEN_LOW" or gap_tag == "GAP_UP"
            )
            put_aligned = side == "PUT" and (
                open_bias_tag == "OPEN_HIGH" or gap_tag == "GAP_DOWN"
            )
            if open_bias_tag == "NONE" and gap_tag == "NO_GAP":
                trade["open_bias_aligned"] = "NEUTRAL"
            elif call_aligned or put_aligned:
                trade["open_bias_aligned"] = "ALIGNED"
            else:
                trade["open_bias_aligned"] = "MISALIGNED"

        return (trades, session_types, blocked, tags, signals_fired, entry_ok_count,
                open_bias_tag, vs_close_tag, gap_tag, balance_tag)

    @staticmethod
    def _log_ts(line: str) -> str:
        m = _RE_LOG_TS.match(line)
        return m.group("log_ts") if m else ""

    @staticmethod
    def _pop_open(queue: List[dict], side: str) -> Optional[dict]:
        """Pop the first open record matching side (FIFO)."""
        for i, rec in enumerate(queue):
            if rec["side"] == side:
                return queue.pop(i)
        # If no same-side match, pop the oldest open regardless
        return queue.pop(0) if queue else None


# ── Convenience function ──────────────────────────────────────────────────────

def parse_session(log_path: str | Path) -> SessionSummary:
    """Parse a log file and return a SessionSummary.

    Equivalent to ``LogParser(log_path).parse()``.
    """
    return LogParser(log_path).parse()


def parse_multiple(log_paths: List[str | Path]) -> List[SessionSummary]:
    """Parse multiple log files and return a list of SessionSummary objects."""
    return [parse_session(p) for p in log_paths]
