#!/usr/bin/env python3
"""
Verification script for scoring engine fix (v5)
Validates that all required indicators are being computed and passed to scoring engine.
Run this AFTER the signals.py fix to confirm everything works.

Usage:
    python verify_scoring_fix.py --mode=check    # Just check if fix is in place
    python verify_scoring_fix.py --mode=test     # Run test with sample data
"""

import sys
import pandas as pd
from datetime import datetime, timedelta

# Import modified modules
from signals import detect_signal
from entry_logic import check_entry_condition
from indicators import classify_cpr_width, momentum_ok
from candle_builder import build_3m_candles

def check_imports():
    """Verify all required functions can be imported."""
    print("\n" + "="*60)
    print("STEP 1: Checking Imports")
    print("="*60)
    try:
        from signals import detect_signal
        print("✓ detect_signal imported")
        
        from entry_logic import check_entry_condition
        print("✓ check_entry_condition imported")
        
        from indicators import classify_cpr_width, momentum_ok
        print("✓ classify_cpr_width imported")
        print("✓ momentum_ok imported")
        
        return True
    except ImportError as e:
        print(f"✗ Import failed: {e}")
        return False


def check_signals_code():
    """Verify that signals.py contains the fix."""
    print("\n" + "="*60)
    print("STEP 2: Checking signals.py for v5 fix")
    print("="*60)
    
    try:
        with open("signals.py", "r") as f:
            content = f.read()
        
        checks = {
            "classify_cpr_width import": "from indicators import" in content and "classify_cpr_width" in content,
            "momentum_ok_call computation": "momentum_ok_call" in content,
            "momentum_ok_put computation": "momentum_ok_put" in content,
            "cpr_width computation": "cpr_width = classify_cpr_width" in content,
            "entry_type computation": "entry_type =" in content and "BREAKOUT" in content,
            "rsi_prev computation": "rsi_prev" in content and "rsi14" in content,
            "indicators dict has momentum_ok_call": '"momentum_ok_call": mom_ok_call' in content,
            "indicators dict has momentum_ok_put": '"momentum_ok_put": mom_ok_put' in content,
            "indicators dict has cpr_width": '"cpr_width": cpr_width' in content,
            "indicators dict has entry_type": '"entry_type": entry_type' in content,
            "indicators dict has rsi_prev": '"rsi_prev": rsi_prev' in content,
        }
        
        all_passed = True
        for check_name, result in checks.items():
            status = "✓" if result else "✗"
            print(f"{status} {check_name}")
            if not result:
                all_passed = False
        
        return all_passed
    
    except FileNotFoundError:
        print("✗ signals.py not found")
        return False


def check_entry_logic_logging():
    """Verify that entry_logic.py has enhanced logging."""
    print("\n" + "="*60)
    print("STEP 3: Checking entry_logic.py for logging enhancements")
    print("="*60)
    
    try:
        with open("entry_logic.py", "r") as f:
            content = f.read()
        
        checks = {
            "SCORE BREAKDOWN v5 logging": "[SCORE BREAKDOWN v5]" in content,
            "Indicator availability logging": "MOM=" in content and "CPR=" in content,
            "Scorer breakdown logging": "ST=" in content and "RSI=" in content,
            "Entry scoring v5 start log": "[ENTRY SCORING v5 START]" in content or "[ENTRY SCORING]" in content,
        }
        
        all_passed = True
        for check_name, result in checks.items():
            status = "✓" if result else "✗"
            print(f"{status} {check_name}")
            if not result:
                all_passed = False
        
        return all_passed
    
    except FileNotFoundError:
        print("✗ entry_logic.py not found")
        return False


def check_syntax():
    """Verify no syntax errors in modified files."""
    print("\n" + "="*60)
    print("STEP 4: Checking Python syntax")
    print("="*60)
    
    import py_compile
    files_to_check = ["signals.py", "entry_logic.py", "indicators.py"]
    all_passed = True
    
    for filename in files_to_check:
        try:
            py_compile.compile(filename, doraise=True)
            print(f"✓ {filename} - no syntax errors")
        except py_compile.PyCompileError as e:
            print(f"✗ {filename} - syntax error: {e}")
            all_passed = False
    
    return all_passed


def test_indicators_computation():
    """Test that indicators can be computed correctly."""
    print("\n" + "="*60)
    print("STEP 5: Testing indicator computation")
    print("="*60)
    
    try:
        # Create mock candle data
        dates = pd.date_range(end=datetime.now(), periods=20, freq='3min')
        candles = pd.DataFrame({
            'datetime': dates,
            'open': [25400 + i*10 for i in range(20)],
            'high': [25420 + i*10 for i in range(20)],
            'low': [25380 + i*10 for i in range(20)],
            'close': [25410 + i*10 for i in range(20)],
            'volume': [1000000 + i*10000 for i in range(20)],
        })
        
        # Test momentum_ok function
        result_call, explanation_call = momentum_ok(candles, "CALL")
        print(f"✓ momentum_ok('CALL') = {result_call}")
        
        result_put, explanation_put = momentum_ok(candles, "PUT")
        print(f"✓ momentum_ok('PUT') = {result_put}")
        
        # Test classify_cpr_width function
        cpr_levels = {
            "tc": 25500,
            "bc": 25400,
            "pivot": 25450
        }
        close_price = 25425
        width = classify_cpr_width(cpr_levels, close_price)
        print(f"✓ classify_cpr_width() = '{width}'")
        
        assert width in ["NARROW", "NORMAL", "WIDE"], f"Invalid CPR width: {width}"
        
        return True
    
    except Exception as e:
        print(f"✗ Indicator computation test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def display_summary(results):
    """Display final results summary."""
    print("\n" + "="*60)
    print("SUMMARY - SCORING FIX VERIFICATION")
    print("="*60)
    
    all_passed = all(results.values())
    
    for test_name, result in results.items():
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"{status}: {test_name}")
    
    print("\n" + "="*60)
    if all_passed:
        print("✓✓✓ ALL CHECKS PASSED ✓✓✓")
        print("The scoring engine fix is properly implemented!")
        print("You can now:")
        print("  1. Deploy to PAPER mode")
        print("  2. Run REPLAY to test with historical data")
        print("  3. Monitor logs for [INDICATORS BUILT] and [SCORE BREAKDOWN v5]")
        print("="*60)
        return 0
    else:
        print("✗✗✗ SOME CHECKS FAILED ✗✗✗")
        print("Please review the failures above before deploying.")
        print("="*60)
        return 1


def main():
    """Run all verification checks."""
    print("\n")
    print("#" * 60)
    print("# SCORING ENGINE v5 FIX VERIFICATION SCRIPT")
    print("#" * 60)
    
    results = {
        "Import availability": check_imports(),
        "signals.py has v5 fix": check_signals_code(),
        "entry_logic.py has enhanced logging": check_entry_logic_logging(),
        "Python syntax validation": check_syntax(),
        "Indicator computation": test_indicators_computation(),
    }
    
    exit_code = display_summary(results)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
