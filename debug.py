"""
debug_945.py — TARGETED DIAGNOSTIC FOR MISSING 9:45 SIGNAL  (v2)
=================================================================
Run from your trading_engine folder:
    python debug_945.py --db "C:\\SQLite\\ticks\\ticks_2026-02-20.db"

v2 fixes vs v1:
- Prepends previous-day DBs (same logic as execution.py) so indicators
  have enough history for ADX14/RSI14/Supertrend to warm up
- Shows exact THRESHOLDS from your local entry_logic.py
- Prints full score breakdown even when signal fires
"""

import sys
import os
import logging
import argparse
import sqlite3
import pathlib
import pandas as pd
import numpy as np

# ── Logging — DEBUG so score breakdown appears ────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(stream=sys.stdout)]
)

GREEN  = "\033[92m"; YELLOW = "\033[93m"; RED = "\033[91m"; RESET = "\033[0m"
def ok(msg):   print(f"  {GREEN}✔ PASS{RESET}  {msg}")
def fail(msg): print(f"  {RED}✘ FAIL{RESET}  {msg}")
def info(msg): print(f"  {YELLOW}→{RESET}      {msg}")

# ── Args ──────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--db",     required=True, help="Path to ticks_2026-02-20.db")
parser.add_argument("--symbol", default="NSE:NIFTY50-INDEX")
parser.add_argument("--warmup", default=5, type=int,
                    help="Prev trading days to prepend (default 5)")
args = parser.parse_args()
DB  = args.db
SYM = args.symbol

if not os.path.exists(DB):
    sys.exit(f"DB not found: {DB}")

IST = "Asia/Kolkata"

# ─────────────────────────────────────────────────────────────────────────────
def _load_candles(db_path, table, sym):
    try:
        with sqlite3.connect(db_path) as conn:
            q = f"""
                SELECT
                    trade_date || 'T' || ist_slot AS _dt,
                    trade_date || ' '  || ist_slot AS time,
                    trade_date, ist_slot,
                    open, high, low, close,
                    COALESCE(volume, 0) AS volume
                FROM {table}
                WHERE symbol = ?
                ORDER BY trade_date, ist_slot
            """
            df = pd.read_sql_query(q, conn, params=(sym,))
        if df.empty:
            return pd.DataFrame()
        df["date"] = pd.to_datetime(df["_dt"]).dt.tz_localize(IST)
        df.drop(columns=["_dt"], inplace=True, errors="ignore")
        for c in ("open","high","low","close","volume"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df.reset_index(drop=True)
    except Exception as e:
        return pd.DataFrame()


def _prepend_warmup(db_path, sym, days=5):
    db_dir   = str(pathlib.Path(db_path).parent)
    stem     = pathlib.Path(db_path).stem        # "ticks_2026-02-20"
    date_str = stem.replace("ticks_", "")
    ref_date = pd.Timestamp(date_str)

    frames_3m, frames_15m = [], []
    days_back, days_found = 0, 0

    while days_back < 14 and days_found < days:
        days_back += 1
        cand     = ref_date - pd.Timedelta(days=days_back)
        if cand.weekday() >= 5:
            continue
        cand_str  = cand.strftime("%Y-%m-%d")
        cand_path = os.path.join(db_dir, f"ticks_{cand_str}.db")
        if not os.path.exists(cand_path):
            continue
        f3  = _load_candles(cand_path, "candles_3m_ist",  sym)
        f15 = _load_candles(cand_path, "candles_15m_ist", sym)
        if not f3.empty:  frames_3m.append(f3)
        if not f15.empty: frames_15m.append(f15)
        days_found += 1
        info(f"Warmup day {days_found}: {cand_str}  3m={len(f3)} bars  15m={len(f15)} bars")

    if days_found == 0:
        fail(f"No previous-day ticks_YYYY-MM-DD.db files found in: {db_dir}")
        fail("ADX14 / RSI14 / Supertrend WILL be NaN at 9:45 — only 12 bars available!")
        info("Fix: copy ticks_2026-02-19.db, ticks_2026-02-18.db etc. to same folder")

    w3  = pd.concat(frames_3m,  ignore_index=True) if frames_3m  else pd.DataFrame()
    w15 = pd.concat(frames_15m, ignore_index=True) if frames_15m else pd.DataFrame()
    return w3, w15


print(f"\n{'='*70}")
print(f"DIAGNOSTIC v2: 9:45 Signal for 2026-02-20  (with warmup)")
print(f"DB: {DB}")
print(f"{'='*70}\n")

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1: Load today + warmup
# ─────────────────────────────────────────────────────────────────────────────
print("STEP 1: Loading candles")
df3_today  = _load_candles(DB, "candles_3m_ist",  SYM)
df15_today = _load_candles(DB, "candles_15m_ist", SYM)

if df3_today.empty:
    sys.exit(f"{RED}No 3m data for {SYM} in {DB}{RESET}")

info(f"Today  3m  rows={len(df3_today)}  "
     f"{df3_today.iloc[0]['time']} → {df3_today.iloc[-1]['time']}")
info(f"Today  15m rows={len(df15_today)}")

print(f"\n  Scanning for previous-day DBs in: {os.path.dirname(DB)}")
w3, w15 = _prepend_warmup(DB, SYM, days=args.warmup)

# Merge warmup + today
def _merge(warm, today):
    if warm.empty: return today.copy()
    out = pd.concat([warm, today], ignore_index=True)
    out.drop_duplicates(subset=["time"], keep="last", inplace=True)
    out.sort_values("time", inplace=True)
    out.reset_index(drop=True, inplace=True)
    return out

df3_all  = _merge(w3,  df3_today)
df15_all = _merge(w15, df15_today)
ok(f"After warmup: 3m total={len(df3_all)} bars  15m total={len(df15_all)} bars")

# ─────────────────────────────────────────────────────────────────────────────
# STEP 2: Find 09:45 bar
# ─────────────────────────────────────────────────────────────────────────────
print("\nSTEP 2: Locating 09:45 bar")
mask = (df3_all["time"].astype(str).str.startswith("2026-02-20") &
        df3_all["time"].astype(str).str.contains("09:45"))
if not mask.any():
    fail("No bar labelled 09:45 for 2026-02-20")
    print("  Available 2026-02-20 bars 09:30-10:00:")
    m2 = (df3_all["time"].astype(str).str.startswith("2026-02-20") &
          df3_all["time"].astype(str).str.contains(r"09:[34]|10:0"))
    for _, r in df3_all[m2].iterrows():
        print(f"    {r['time']}  C={r['close']:.2f}")
    sys.exit(1)

bar_idx_945 = df3_all.index[mask][0]
bar_945     = df3_all.loc[bar_idx_945]
ok(f"09:45 bar at index {bar_idx_945}  C={bar_945['close']:.2f}")
info(f"Bars available BEFORE this bar (warmup history): {bar_idx_945}")

if bar_idx_945 < 28:
    fail(f"Only {bar_idx_945} warmup bars — ADX14 needs ≥28 for full convergence")
    info("Add more previous-day DB files or increase --warmup N")
else:
    ok(f"Warmup sufficient ({bar_idx_945} bars ≥ 28)")

# ─────────────────────────────────────────────────────────────────────────────
# STEP 3: Import local modules
# ─────────────────────────────────────────────────────────────────────────────
print("\nSTEP 3: Importing local modules")
for name, imp in [
    ("orchestration", "from orchestration import (build_indicator_dataframe, calculate_cpr, calculate_camarilla_pivots, calculate_traditional_pivots)"),
    ("signals",       "from signals import detect_signal, calculate_vwap, get_opening_range, _best_pivot_for_side, _norm_bias"),
    ("entry_logic",   "from entry_logic import check_entry_condition, THRESHOLDS, ATR_LOW_MAX, ATR_HIGH_MIN, WEIGHTS"),
    ("indicators",    "from indicators import resolve_atr"),
    ("day_type",      "from day_type import DayTypeResult"),
]:
    try:
        exec(imp, globals())
        ok(f"{name} imported")
    except Exception as e:
        fail(f"{name}: {e}"); sys.exit(1)

print(f"\n  {YELLOW}*** LOCAL THRESHOLDS: {THRESHOLDS} ***{RESET}")
if THRESHOLDS.get("NORMAL", 50) > 60:
    fail(f"NORMAL={THRESHOLDS['NORMAL']} is OLD (v3 = 50). Deploy new entry_logic.py!")
elif THRESHOLDS.get("NORMAL", 50) <= 50:
    ok(f"NORMAL={THRESHOLDS['NORMAL']} is v3-compliant")

# ─────────────────────────────────────────────────────────────────────────────
# STEP 4: Build indicators
# ─────────────────────────────────────────────────────────────────────────────
print("\nSTEP 4: Building indicators")
slice_3m = df3_all.iloc[:bar_idx_945 + 1].copy()

# Align 15m
if not df15_all.empty:
    tc15 = "date" if "date" in df15_all.columns else "time"
    if "date" in df3_all.columns:
        cur_dt = pd.to_datetime(bar_945["date"])
    else:
        cur_dt = pd.Timestamp("2026-02-20 09:45:00").tz_localize(IST)
    slice_15m = df15_all[pd.to_datetime(df15_all[tc15]) <= cur_dt].copy()
else:
    slice_15m = pd.DataFrame()

info(f"slice_3m={len(slice_3m)} bars  slice_15m={len(slice_15m)} bars")
slice_3m  = build_indicator_dataframe(SYM, slice_3m,  interval="3m")
if not slice_15m.empty:
    slice_15m = build_indicator_dataframe(SYM, slice_15m, interval="15m")

last_3m  = slice_3m.iloc[-1]
last_15m = slice_15m.iloc[-1] if not slice_15m.empty else pd.Series(dtype=object)

# ─────────────────────────────────────────────────────────────────────────────
# STEP 5: Indicator table
# ─────────────────────────────────────────────────────────────────────────────
print("\nSTEP 5: Indicator values at 09:45")
print(f"  {'Indicator':<25} {'3m':>16}   {'15m':>16}")
print(f"  {'-'*25} {'-'*16}   {'-'*16}")
for ind in ["ema9","ema13","adx14","cci20","rsi14",
            "supertrend_bias","supertrend_line","vwap"]:
    v3  = last_3m.get(ind, "MISSING")
    v15 = last_15m.get(ind, "MISSING") if not slice_15m.empty else "N/A"
    fmt = lambda v: f"{v:.4f}" if isinstance(v,(int,float)) and not pd.isna(v) else str(v)
    bad = str(v3) in ("nan","None","MISSING") or str(v15) in ("nan","None","MISSING")
    flag = f"  {RED}← BAD{RESET}" if bad else ""
    print(f"  {ind:<25} {fmt(v3):>16}   {fmt(v15):>16}{flag}")

# ─────────────────────────────────────────────────────────────────────────────
# STEP 6: Individual gate checks
# ─────────────────────────────────────────────────────────────────────────────
print("\nSTEP 6: Gate checks")
ts = pd.Timestamp(str(bar_945.get("time","")))
t  = ts.hour * 60 + ts.minute
info(f"Bar time = {ts.hour:02d}:{ts.minute:02d}  t={t}")
if   t < 9*60+30: fail(f"PRE_OPEN (t={t})")
elif t < 9*60+45: fail(f"OPENING_NOISE block: bar labelled {ts.hour:02d}:{ts.minute:02d} but entry_logic blocks t<585")
else:             ok(f"Time gate: t={t}")

atr_val, atr_src = resolve_atr(slice_3m)
if atr_val is None or (isinstance(atr_val,float) and pd.isna(atr_val)):
    fail(f"ATR is NaN — {len(slice_3m)} bars not enough; need prev-day DBs")
elif atr_val < ATR_LOW_MAX:
    fail(f"ATR LOW regime: {atr_val:.2f} < {ATR_LOW_MAX}")
else:
    ok(f"ATR={atr_val:.2f}")

from signals import range_is_ok
ok("Range gate OK") if range_is_ok(slice_3m) else fail("NARROW_RANGE block")

bias_raw = last_15m.get("supertrend_bias","NEUTRAL") if not slice_15m.empty else "NEUTRAL"
info(f"15m supertrend_bias = {bias_raw}")
if str(bias_raw) in ("nan","None","NEUTRAL",""): fail("15m bias is NaN/NEUTRAL — warmup issue")
else:                                            ok(f"15m bias = {bias_raw}")

# ─────────────────────────────────────────────────────────────────────────────
# STEP 7: Full detect_signal
# ─────────────────────────────────────────────────────────────────────────────
print("\nSTEP 7: Full detect_signal() call")
print("-"*70)
tpma     = float(slice_3m["vwap"].iloc[-1]) if "vwap" in slice_3m.columns and not pd.isna(slice_3m["vwap"].iloc[-1]) else None
orb_h, orb_l = get_opening_range(slice_3m)
pivot_src    = slice_3m.iloc[-2] if len(slice_3m) >= 2 else slice_3m.iloc[-1]
cpr  = calculate_cpr(float(pivot_src["high"]), float(pivot_src["low"]), float(pivot_src["close"]))
trad = calculate_traditional_pivots(float(pivot_src["high"]), float(pivot_src["low"]), float(pivot_src["close"]))
cam  = calculate_camarilla_pivots(float(pivot_src["high"]), float(pivot_src["low"]), float(pivot_src["close"]))

class FakeTime:
    def __init__(self, h, m): self.hour, self.minute = h, m
fake_time        = FakeTime(ts.hour, ts.minute)
day_type_result  = DayTypeResult()

signal = detect_signal(
    candles_3m=slice_3m, candles_15m=slice_15m,
    cpr_levels=cpr, camarilla_levels=cam, traditional_levels=trad,
    atr=atr_val, include_partial=False, current_time=fake_time,
    vwap=tpma, orb_high=orb_h, orb_low=orb_l,
    day_type_result=day_type_result,
)
print("-"*70)

# ─────────────────────────────────────────────────────────────────────────────
# STEP 8: Score breakdown (always shown)
# ─────────────────────────────────────────────────────────────────────────────
print("\nSTEP 8: Score breakdown")
if signal:
    print(f"\n{GREEN}  ✔ SIGNAL FIRED: {signal['side']}  score={signal.get('score')}  "
          f"threshold={signal.get('threshold','?')}  "
          f"source={signal.get('source')}  strength={signal.get('strength')}{RESET}")
    print(f"  reason: {signal.get('reason')}")

# Direct call to always show breakdown
bias_15m_arg = _norm_bias(str(bias_raw))
ind_direct = {
    "atr":                 atr_val,
    "supertrend_line_3m":  float(last_3m["supertrend_line"])  if pd.notna(last_3m.get("supertrend_line"))  else None,
    "supertrend_line_15m": float(last_15m["supertrend_line"]) if not slice_15m.empty and pd.notna(last_15m.get("supertrend_line")) else None,
    "ema_fast":  float(last_3m["ema9"])  if pd.notna(last_3m.get("ema9"))  else None,
    "ema_slow":  float(last_3m["ema13"]) if pd.notna(last_3m.get("ema13")) else None,
    "adx":       float(last_3m["adx14"]) if pd.notna(last_3m.get("adx14")) else None,
    "cci":       float(last_3m["cci20"]) if pd.notna(last_3m.get("cci20")) else None,
    "candle_15m": last_15m if not slice_15m.empty else None,
    "st_bias_3m":  _norm_bias(str(last_3m.get("supertrend_bias","NEUTRAL"))),
    "st_bias_15m": bias_15m_arg,
    "vwap":        tpma,
}
pv_call = _best_pivot_for_side(
    last_3m, slice_3m.iloc[-2] if len(slice_3m)>=2 else last_3m,
    float(last_3m["high"])-float(last_3m["low"]),
    atr_val if (atr_val and not pd.isna(atr_val)) else 50,
    cpr, cam, trad, "CALL", bias_15m_arg,
    vwap=tpma, orb_high=orb_h, orb_low=orb_l, current_time=fake_time,
)
info(f"Pivot signal for CALL: {pv_call}")

lz = check_entry_condition(
    candle=last_3m, indicators=ind_direct,
    bias_15m=bias_15m_arg, pivot_signal=pv_call,
    current_time=fake_time, day_type_result=day_type_result,
)
total = sum(lz["breakdown"].values()) if lz["breakdown"] else 0
thresh = lz["threshold"]
max_pts = sum(WEIGHTS.values())

print(f"\n  {'Dimension':<22} {'Got':>5}  {'Max':>5}  Visual")
print(f"  {'-'*22} {'-'*5}  {'-'*5}  {'─'*20}")
for k, v in (lz["breakdown"] or {}).items():
    w   = WEIGHTS.get(k, 0)
    bar = "█" * v + "░" * max(0, w - v)
    pct_s = f"{v}/{w}"
    print(f"  {k:<22} {pct_s:>5}  {w:>5}  {bar}")
print(f"  {'─'*22} {'─'*5}  {'─'*5}")
print(f"  {'TOTAL':<22} {total:>5}  {max_pts:>5}  vs threshold={thresh}")

print()
if total >= thresh:
    ok(f"Score {total} >= threshold {thresh}  → entry_logic APPROVES")
    if not signal:
        fail("BUT detect_signal returned None — blocked UPSTREAM of scoring")
        info("Check: range_is_ok, ATR gate in detect_signal, HTF/LTF conflict")
else:
    fail(f"Score {total} < threshold {thresh}  →  {thresh-total} points SHORT")
    zeros = [(k, WEIGHTS.get(k,0)) for k,v in (lz["breakdown"] or {}).items() if v==0]
    if zeros:
        info("Dimensions scoring 0:")
        for k, mx in zeros:
            print(f"    {RED}{k:<22}{RESET}  max={mx} pts")

print(f"\n{'='*70}")
print("DIAGNOSTIC v2 COMPLETE")
print(f"{'='*70}\n")