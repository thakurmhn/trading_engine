# Pivot Reaction Engine - Quick Reference Card
**Purpose**: Fast reference for integration, testing, and monitoring  
**Time**: 2 minutes to read, 30 minutes to integrate

---

## 🎯 What It Does (10 Second Summary)

**Evaluates ALL 27 pivot levels on EVERY candle close**  
**Blocks trades that conflict with pivot reactions**  
**Provides complete audit trail**

---

## 📋 Integration Checklist (30 Minutes)

### Step 1: Initialize (5 min)
**File**: `main.py`  
**Location**: Inside `do_warmup()` function  
**Add**:
```python
from pivot_reaction_engine import initialize_pivot_engine
pivot_engine = initialize_pivot_engine()
```

### Step 2: Evaluate (10 min)
**File**: `main.py`  
**Location**: After `[NEW CANDLE]` log in `main_strategy_code()`  
**Add**:
```python
from pivot_reaction_engine import get_pivot_engine, CandleData

pivot_engine = get_pivot_engine()
candle_data = CandleData(bar_time, open, high, low, close, atr)
pivot_interactions = pivot_engine.evaluate_candle(candle_data, pivot_levels)
data_feed.pivot_interactions = pivot_interactions
```

### Step 3: Validate (10 min)
**File**: `execution.py`  
**Location**: After `detect_signal()`, before entry execution  
**Add**:
```python
from pivot_reaction_engine import get_pivot_engine

pivot_engine = get_pivot_engine()
is_valid, reason = pivot_engine.validate_trade_signal(
    signal_side, signal_reason, pivot_interactions
)
if not is_valid:
    return  # Block trade
```

### Step 4: Dashboard (5 min)
**File**: `dashboard.py`  
**Location**: After TRADE SUMMARY section  
**Add**:
```python
from pivot_reaction_engine import get_pivot_engine
pivot_engine = get_pivot_engine()
print(pivot_engine.get_metrics_summary())
```

---

## ✅ Validation Commands (2 Minutes)

### Syntax Check:
```bash
python -m py_compile pivot_reaction_engine.py main.py execution.py
```

### Replay Test:
```bash
python main.py --mode REPLAY --date 2026-03-11
```

### Verify Logs:
```bash
# Engine initialized?
grep "PIVOT_ENGINE.*Initialized" options_trade_engine_*.log

# Pivots evaluated?
grep "PIVOT_AUDIT" options_trade_engine_*.log | wc -l
# Should be: 27 × number of candles

# Signals validated?
grep "PIVOT_ENGINE.*VALIDATED" options_trade_engine_*.log

# Any blocks?
grep "PIVOT_ENGINE.*BLOCK" options_trade_engine_*.log

# Clusters detected?
grep "PIVOT_CLUSTER_EVENT" options_trade_engine_*.log
```

---

## 📊 Expected Results

### Log Output:
```
[PIVOT_ENGINE] Initialized - Mandatory pivot validation ACTIVE
[PIVOT_AUDIT] timestamp=09:18:00 candle_close=25432.50 pivot_family=CPR pivot_level=TC ...
[PIVOT_CLUSTER_EVENT] cluster_id=0 pivots=['CPR_TC', 'TRADITIONAL_R1'] center=25492.50 ...
[PIVOT_ENGINE][VALIDATED] PUT signal approved - PUT_CONFIRMED_RESISTANCE_REJECTION_R1
```

### Dashboard Metrics:
```
PIVOT REACTION ENGINE METRICS
────────────────────────────────────────
Total Candles Evaluated    : 262
Pivot Levels Checked       : 7,074  (27 × 262)
Pivot Interactions Detected: 184
  - Rejections             : 67
  - Acceptances            : 42
  - Breakouts              : 38
  - Breakdowns             : 37
Pivot Cluster Events       : 12
Trades with Pivot Confirm  : 58
Trades Blocked (No Pivot)  : 4
Pivot Coverage             : 100.0%
```

---

## 🔧 Troubleshooting (1 Minute)

| Issue | Solution |
|-------|----------|
| "Pivot engine not initialized" | Add `initialize_pivot_engine()` in `do_warmup()` |
| "No pivot interactions" | Add `evaluate_candle()` after `[NEW CANDLE]` log |
| All trades blocked | Review validation logic, may be too strict |
| No [PIVOT_AUDIT] logs | Verify `evaluate_candle()` is called |
| Coverage < 100% | Check pivot_levels dict has all families |

---

## 🎯 Success Criteria

✅ Integration successful if:
- [ ] No syntax errors
- [ ] Engine initializes at startup
- [ ] [PIVOT_AUDIT] logs = 27 × candle count
- [ ] [PIVOT_ENGINE][VALIDATED] logs before entries
- [ ] Dashboard shows pivot metrics
- [ ] Pivot coverage = 100%

---

## 📁 Files Reference

| File | Purpose |
|------|---------|
| `pivot_reaction_engine.py` | Core module (600+ lines) |
| `PIVOT_ENGINE_INTEGRATION.md` | Detailed integration guide |
| `PIVOT_ENGINE_SUMMARY.md` | Complete overview |
| This file | Quick reference |

---

## 🚀 Quick Start (Right Now)

```bash
# 1. Backup
git checkout -b pivot-engine-integration

# 2. Open files
code main.py execution.py dashboard.py

# 3. Follow integration checklist above (30 min)

# 4. Test
python -m py_compile pivot_reaction_engine.py main.py execution.py
python main.py --mode REPLAY --date 2026-03-11

# 5. Verify
grep "PIVOT_ENGINE" options_trade_engine_*.log | head -20
cat reports/dashboard_report_2026-03-11.txt | grep -A 15 "PIVOT REACTION"
```

---

## 📞 Need Help?

1. **Read**: PIVOT_ENGINE_INTEGRATION.md (detailed guide)
2. **Check**: Inline docs in `pivot_reaction_engine.py`
3. **Verify**: Run validation commands above
4. **Rollback**: `git checkout HEAD -- main.py execution.py dashboard.py`

---

## 🎓 Key Concepts (30 Seconds)

**Pivot Families**: CPR (3), Traditional (11), Camarilla (8) = 27 levels  
**Interaction Types**: Touch, Rejection, Acceptance, Breakout, Breakdown  
**Clusters**: Multiple pivots within 0.5 × ATR = high-probability zone  
**Validation**: Signal must align with pivot reaction or be blocked  
**Coverage**: 100% = all pivots checked on every candle

---

**Ready?** Start with Step 1 (Initialize) and work through the checklist.
