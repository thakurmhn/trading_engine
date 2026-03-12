# Replay Validation Agent - Quick Reference Card

## WHAT IT DOES

Validates that Pivot Reaction Engine and Liquidity Event Detection work correctly on historical data.

**Modules Validated**:
- ✓ Pivot Reaction Engine (mandatory pivot evaluation)
- ✓ Liquidity Event Detection (sweep/trap detection)

**Data Source**: Historical tick databases (most recent 14 days)

**Output**: Comprehensive validation report with pass/fail status

---

## QUICK START

### Run Validation
```bash
cd c:\Users\mohan\trading_engine
python replay_validation_agent.py
```

### Check Results
```bash
# View detailed log
tail -100 replay_validation_agent.log

# View structured report
cat replay_validation_report.json
```

---

## SUCCESS CRITERIA

✓ Pivot coverage >= 99%  
✓ Liquidity sweeps detected > 0  
✓ No trade signal bypasses pivot validation  
✓ Signal confirmation rate > 90%  
✓ Failures < 5  

---

## VALIDATION METRICS

### Pivot Metrics
- Total candles processed
- Pivot levels checked
- Pivot interactions detected
- Rejections / Acceptances / Breakouts / Breakdowns
- Pivot clusters detected

### Liquidity Metrics
- Liquidity events detected
- Sweeps at pivots
- False breakouts detected
- Trap events detected

### Signal Metrics
- Signals generated
- Signals with pivot confirmation
- Signals with liquidity confirmation
- Signals blocked due to trap

---

## EXPECTED OUTPUT

```
Status: PASSED
Pivot Coverage: 99.87%
Signal Confirmation: 94.2%
Failures: 0

PIVOT METRICS
  Levels Checked: 85410
  Interactions: 2834
  Rejections: 1247
  Acceptances: 892
  Breakouts: 456
  Breakdowns: 239
  Clusters: 156

LIQUIDITY METRICS
  Events: 487
  Sweeps at Pivots: 412
  False Breakouts: 45
  Traps: 30

SIGNAL METRICS
  Generated: 127
  With Pivot Confirmation: 120
  With Liquidity Confirmation: 115
  Blocked by Trap: 8
```

---

## FAILURE TROUBLESHOOTING

| Failure | Cause | Fix |
|---------|-------|-----|
| Coverage < 99% | Pivot eval not running | Check evaluate_candle() calls |
| No liquidity events | Thresholds too strict | Review sweep/trap thresholds |
| Signal bypass | No validation call | Add validate_trade_signal() |
| High failures | Multiple issues | Fix one at a time, re-run |

---

## FILES GENERATED

- `replay_validation_agent.log` - Detailed execution log
- `replay_validation_report.json` - Structured results

---

## NEXT STEPS

1. Run validation
2. Review report
3. If PASSED → Deploy to live
4. If FAILED → Fix issues → Re-run

---

## DOCUMENTATION

- Full details: `REPLAY_VALIDATION_FRAMEWORK.md`
- Pivot engine: `pivot_reaction_engine.py`
- Execution layer: `execution.py`
- Main entry: `main.py`
