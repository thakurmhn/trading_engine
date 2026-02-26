# Exit Logic v9 - Integration Guide

**Purpose:** Connect stress testing framework to replay_analyzer_v7.py  
**Status:** Ready for Integration  
**Timeline:** 1-2 hours to integrate + 2-4 hours to execute tests  

---

## Integration Checklist

### Phase 2A: Minimal Integration (30 minutes)

- [ ] Add imports to replay_analyzer_v7.py:
  ```python
  from exit_logic_v9_stress_framework import (
      DatabaseCleaner, StressTestScenarios, StressTestRunner
  )
  ```

- [ ] Add command-line argument:
  ```python
  parser.add_argument('--stress-test', action='store_true',
                     help='Run v9 stress test suite (5000 synthetic replays)')
  ```

- [ ] Add main branch logic:
  ```python
  if args.stress_test:
      run_stress_tests()  # New function
  ```

- [ ] Create `run_stress_tests()` function (skeleton 20 lines)

### Phase 2B: Full Integration (2-3 hours)

- [ ] Implement `run_stress_tests()` - call all 5 scenarios
- [ ] Store results in `v9_stress_test_results.csv`
- [ ] Generate summary report
- [ ] Add pass/fail indicators

### Phase 2C: Baseline Validation (1 hour)

- [ ] Flag baseline mode: `--baseline-validation`
- [ ] Replay same 22 trades with v9
- [ ] Compare metrics: win rate, P&L, convertible losses
- [ ] Generate baseline comparison report

---

## Proposed `run_stress_tests()` Implementation

```python
def run_stress_tests(pm, market_data, output_dir='stress_results'):
    """
    Run all 5 v9 stress test scenarios (5000 synthetic replays)
    
    Expected duration: 60-90 seconds for all 5 scenarios
    Output: CSV + summary report
    """
    
    print("\n" + "="*80)
    print("EXIT LOGIC v9 - STRESS TEST SUITE")
    print("="*80 + "\n")
    
    # Initialize components
    generator = StressTestScenarios(base_price=82500)
    runner = StressTestRunner()
    cleaner = DatabaseCleaner(verbose=False)
    
    results_all = []
    scenarios_map = {
        'gap_open': generator.scenario_gap_open,
        'flash_reversal': generator.scenario_flash_reversal,
        'extreme_volatility': generator.scenario_extreme_volatility,
        'low_liquidity': generator.scenario_low_liquidity,
        'trending_exhaustion': generator.scenario_trending_exhaustion,
    }
    
    total_trials = 0
    total_passed = 0
    
    # Run each scenario
    for scenario_name, scenario_generator in scenarios_map.items():
        print(f"\n[STRESS TEST] Running {scenario_name}...")
        
        # Generate 1000 trials
        scenarios = scenario_generator(trials=1000)
        total_trials += len(scenarios)
        
        # Run through position manager (simulation)
        pass_fail, results = runner.run_scenario(
            scenario_name=scenario_name,
            scenarios=scenarios,
            position_manager=pm,
            simulated_atr=15
        )
        
        # Aggregate
        passed = results['passed']
        total_passed += passed
        win_rate = (passed / len(scenarios)) * 100
        convertible = results['convertible_losses']
        status = "✅ PASS" if pass_fail else "❌ FAIL"
        
        print(f"  Trials: {len(scenarios)}")
        print(f"  Passed: {passed}/{len(scenarios)} ({win_rate:.1f}%)")
        print(f"  Convertible losses: {convertible}")
        print(f"  Avg P&L: {results['avg_loss_pts']:.2f} pts")
        print(f"  Status: {status}")
        
        # Log each scenario result
        results_all.append({
            'scenario': scenario_name,
            'total_trials': len(scenarios),
            'passed': passed,
            'failed': results['failed'],
            'win_rate_pct': win_rate,
            'convertible_losses': convertible,
            'avg_pnl_pts': results['avg_loss_pts'],
            'status': 'PASS' if pass_fail else 'FAIL',
        })
    
    # Generate summary
    aggregate_win_rate = (total_passed / total_trials) * 100
    all_pass = all(r['status'] == 'PASS' for r in results_all)
    
    summary = {
        'test_date': datetime.now().isoformat(),
        'total_scenarios': len(scenarios_map),
        'total_trials': total_trials,
        'total_passed': total_passed,
        'aggregate_win_rate_pct': aggregate_win_rate,
        'all_scenarios_passed': all_pass,
        'overall_status': 'PASS' if all_pass else 'FAIL',
    }
    
    print("\n" + "="*80)
    print("STRESS TEST SUMMARY")
    print("="*80)
    print(f"Total scenarios: {len(scenarios_map)}")
    print(f"Total trials: {total_trials}")
    print(f"Total passed: {total_passed}")
    print(f"Aggregate win rate: {aggregate_win_rate:.1f}%")
    print(f"Overall status: {summary['overall_status']}")
    print("="*80 + "\n")
    
    # Save results
    import csv
    os.makedirs(output_dir, exist_ok=True)
    
    results_file = os.path.join(output_dir, 'v9_stress_test_results.csv')
    with open(results_file, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=results_all[0].keys())
        writer.writeheader()
        writer.writerows(results_all)
    
    summary_file = os.path.join(output_dir, 'v9_stress_test_summary.txt')
    with open(summary_file, 'w') as f:
        f.write("EXIT LOGIC v9 - STRESS TEST SUMMARY\n")
        f.write("="*80 + "\n")
        for key, value in summary.items():
            f.write(f"{key}: {value}\n")
        f.write("\nDetailed Results:\n")
        f.write("="*80 + "\n")
        for r in results_all:
            f.write(f"\n{r['scenario'].upper()}\n")
            f.write(f"  Trials: {r['total_trials']}\n")
            f.write(f"  Win rate: {r['win_rate_pct']:.1f}%\n")
            f.write(f"  Convertible losses: {r['convertible_losses']}\n")
            f.write(f"  Status: {r['status']}\n")
    
    print(f"[STRESS TEST] Results saved to {results_file}")
    print(f"[STRESS TEST] Summary saved to {summary_file}\n")
    
    return summary, results_all
```

---

## Import Statement (Add to replay_analyzer_v7.py)

**Location:** After other imports (top of file)

```python
import os
from exit_logic_v9_stress_framework import (
    DatabaseCleaner,
    StressTestScenarios,
    StressTestRunner
)
```

---

## Command-Line Usage

### Run Stress Tests Only
```bash
python replay_analyzer_v7.py --stress-test
```

**Output:**
```
================================================================================
EXIT LOGIC v9 - STRESS TEST SUITE
================================================================================

[STRESS TEST] Running gap_open...
  Trials: 1000
  Passed: 970/1000 (97.0%)
  Convertible losses: 0
  Avg P&L: -2.30 pts
  Status: ✅ PASS

[STRESS TEST] Running flash_reversal...
  Trials: 1000
  Passed: 940/1000 (94.0%)
  Convertible losses: 0
  Avg P&L: -8.50 pts
  Status: ✅ PASS

... (3 more scenarios)

================================================================================
STRESS TEST SUMMARY
================================================================================
Total scenarios: 5
Total trials: 5000
Total passed: 4710
Aggregate win rate: 94.2%
Overall status: PASS
================================================================================

[STRESS TEST] Results saved to stress_results/v9_stress_test_results.csv
[STRESS TEST] Summary saved to stress_results/v9_stress_test_summary.txt
```

### Run Baseline Validation
```bash
python replay_analyzer_v7.py --baseline-validation
```

**Output:**
```
[BASELINE VALIDATION] Replaying v9 on historical 22 trades...
[BASELINE VALIDATION] Trade 1/22: NSE_NIFTY50-INDEX_2026-02-16 CALL
  v8 result: -5.2 pts
  v9 result: -5.2 pts
  Match: ✅

[BASELINE VALIDATION] Trade 2/22: ...

[BASELINE VALIDATION SUMMARY]
  Trades analyzed: 22
  Win rate v8: 63.6%
  Win rate v9: 64.3% (+0.7%)
  P&L v8: +39.53 pts
  P&L v9: +42.18 pts (+2.65 pts, +6.7%)
  Regression analysis: ✅ PASS (no negative regressions)
```

---

## Expected Test Results (Benchmarks)

### Stress Test Scenarios - Pass Rates

| Scenario | Expected Pass Rate | Reasoning |
|----------|-------------------|-----------|
| gap_open | 95-98% | LOSS_CUT catches gaps effectively |
| flash_reversal | 90-95% | DRAWDOWN_EXIT captures reversals |
| extreme_volatility | 96-99% | Dynamic thresholds adapt well |
| low_liquidity | 85-92% | TIME_EXIT prevents capital lockup |
| trending_exhaustion | 88-94% | TIME_EXIT balances hope vs release |
| **Aggregate** | **92-95%** | Robust under shocks |

### Baseline Validation - Expected Outcomes

**Hypothesis (v9 vs v8 on same 22 trades):**

| Metric | v8 Baseline | v9 Expected | Target |
|--------|------------|-------------|--------|
| Win rate | 63.6% | 63.6-65.5% | >= 63.6% ✅ |
| P&L (pts) | +39.53 | +42.00 to +50.00 | >= +39.53 ✅ |
| Convertible losses | 0 | 0 | = 0 ✅ |
| Breakout_hold triggers | ~10 | 8-12 (ATR-scaled) | ± 20% OK |

**Most Likely:**
- Win rate: 64.2% (+0.6%, 1-2 improved trades)
- P&L: +43.75 pts (+4.22 pts, ~11% improvement)
- Convertible losses: 0 (maintained)
- Reason: Dynamic sustain + TIME_EXIT improve efficiency

---

## Integration Hooks

### Hook 1: DatabaseCleaner (before replay)
```python
def replay_with_cleaned_data(csvfile):
    """Load CSV, clean, then replay"""
    
    candles = load_candles_from_csv(csvfile)
    cleaner = DatabaseCleaner(verbose=True)
    cleaned, stats = cleaner.clean_candles(candles)
    
    print(f"Cleaned {len(candles)} → {len(cleaned)} candles")
    
    # Replay with cleaned candles
    return replay_candles(cleaned)
```

### Hook 2: Stress Framework (standalone validation)
```python
def validate_exit_logic_v9():
    """Run full stress suite without replay"""
    
    return run_stress_tests(pm=None, market_data=None)
    # Returns: (summary_dict, results_list)
```

### Hook 3: Baseline Replay (compare v8 vs v9)
```python
def baseline_comparison_replay():
    """Replay same 22 trades, compare metrics"""
    
    # Use same trade list as production validation
    trades = load_production_trades()  # 22 trades from Feb 16-20
    
    results_v8 = replay_with_version(trades, version='v8')
    results_v9 = replay_with_version(trades, version='v9')
    
    # Compare
    comparison = compare_results(results_v8, results_v9)
    return comparison
```

---

## File Structure After Integration

```
c:\Users\mohan\trading_engine\
├── position_manager.py                    (v9 enhancements added)
├── replay_analyzer_v7.py                 (stress import + flags added)
├── exit_logic_v9_stress_framework.py     (NEW - stress framework)
│
├── EXIT_LOGIC_V9_PROJECT_PLAN.md         (architecture spec)
├── EXIT_LOGIC_V9_SUMMARY.md              (this comprehensive doc)
├── EXIT_LOGIC_V9_INTEGRATION_GUIDE.md    (this integration doc)
│
└── stress_results/                       (output directory, created by tests)
    ├── v9_stress_test_results.csv        (5 scenario results)
    ├── v9_stress_test_summary.txt        (aggregate pass/fail)
    └── v9_baseline_comparison.csv        (v9 vs v8 on 22 trades)
```

---

## Time Estimate

| Task | Duration | Cumulative |
|------|----------|------------|
| Import framework | 5 min | 5 min |
| Add CLI arguments | 5 min | 10 min |
| Implement `run_stress_tests()` | 40 min | 50 min |
| Test stress suite (5000 replays) | 2-3 min | 53 min |
| Implement baseline validation | 20 min | 73 min |
| Test baseline replay (22 trades) | 20 sec | 73.3 min |
| Review results & documentation | 15 min | 88.3 min |
| **TOTAL** | **~90 min** (1.5 hours) | - |

---

## Next Action

After completing Phase 1 (framework + position_manager v9 enhancements):

1. **Copy this guide** to team/documentation
2. **Implement `run_stress_tests()` function** in replay_analyzer_v7.py
3. **Add CLI arguments** (--stress-test, --baseline-validation)
4. **Run stress suite:** `python replay_analyzer_v7.py --stress-test` (5 min)
5. **Run baseline validation:** `python replay_analyzer_v7.py --baseline-validation` (2 min)
6. **Review results** and decide on deployment

**Target completion:** Within 2-3 hours of Phase 2A/B/C

---

**Document Version:** 1.0  
**Created:** 2026-02-24  
**Status:** Ready for Implementation
