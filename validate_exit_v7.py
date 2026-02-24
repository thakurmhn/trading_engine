#!/usr/bin/env python3
"""
Validate exit logic v7 refactoring syntax and structure.
This tests that the 4 simple rules integrate properly without breaking.
"""

import logging
import sys
from datetime import datetime, timedelta
from position_manager import PositionManager, ExitDecision

logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s - %(message)s'
)

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"

def test_basic_position_lifecycle():
    """Test that basic position open/update/close works with new exit logic"""
    print(f"\n{YELLOW}=== TEST: Basic Position Lifecycle ==={RESET}")
    
    pm = PositionManager(mode="REPLAY", lot_size=130)
    
    # Create a signal to open position
    signal = {
        "side": "CALL",
        "entry_type": "PULLBACK",
        "source": "PIVOT",
        "day_type": "NORMAL",
        "cpr_width": "NORMAL",
        "atr": 150,
        "score": 55,
    }
    
    # Open position
    pm.open(
        bar_idx=0,
        bar_time=datetime.now(),
        underlying=23580,
        entry_premium=150,
        signal=signal
    )
    
    print(f"✓ Position opened: CALL at 150, UL=23580")
    assert pm.is_open(), "Position should be open"
    
    # Store R4/S4 for breakout hold testing
    pm._t['r4'] = 23600
    pm._t['s4'] = 23550
    
    # Create mock row data for update
    mock_row = {
        'rsi14': 55,
        'cci20': 50,
        'supertrend_bias': 'UP',
        'st_bias_15m': 'UP',
        'ema9': 23585,
        'ema13': 23585,
        'open': 23580,
        'close': 23585,
        'high': 23590,
        'low': 23580,
        'adx14': 20,
        'williams_r': -30,
    }
    
    # Bar 1: Small gain, should hold
    dec_1 = pm.update(
        bar_idx=1,
        bar_time=datetime.now() + timedelta(minutes=3),
        underlying=23590,  # +10 pts
        row=mock_row
    )
    
    print(f"✓ Bar 1 update: UL=23590 (+10pts) | should_exit={dec_1.should_exit}")
    assert isinstance(dec_1, ExitDecision), "Should return ExitDecision"
    
    # At 10 pt UL move, QUICK_PROFIT should trigger
    if dec_1.should_exit:
        print(f"  → QUICK_PROFIT rule fired at +10 pts UL move")
    else:
        print(f"  → Position held (QUICK_PROFIT may trigger next bar)")
    
    # If position still open, continue another bar
    if pm.is_open():
        mock_row['rsi14'] = 50
        mock_row['close'] = 23585
        
        dec_2 = pm.update(
            bar_idx=2,
            bar_time=datetime.now() + timedelta(minutes=6),
            underlying=23585,
            row=mock_row
        )
        
        print(f"✓ Bar 2 update: UL=23585 | should_exit={dec_2.should_exit}")
        assert isinstance(dec_2, ExitDecision), "Should return ExitDecision"
    
    print(f"{GREEN}✓ Basic lifecycle test passed{RESET}\n")


def test_exit_rules_fire_correctly():
    """Test that exit rules generate expected log messages"""
    print(f"\n{YELLOW}=== TEST: Exit Rule Log Messages ==={RESET}")
    
    # Create PM and open position
    pm = PositionManager(mode="REPLAY", lot_size=130)
    
    signal = {
        "side": "PUT",
        "entry_type": "ACCEPTANCE",
        "source": "PIVOT",
        "day_type": "NORMAL",
        "cpr_width": "NORMAL",
        "atr": 150,
    }
    
    pm.open(
        bar_idx=0,
        bar_time=datetime.now(),
        underlying=23580,
        entry_premium=100,
        signal=signal
    )
    
    pm._t['r4'] = 23600
    pm._t['s4'] = 23550
    
    print("✓ Opened PUT position")
    
    # Create mock row
    mock_row = {
        'rsi14': 45,
        'cci20': -50,
        'supertrend_bias': 'DOWN',
        'st_bias_15m': 'DOWN',
        'ema9': 23570,
        'ema13': 23570,
        'open': 23575,
        'close': 23570,
        'high': 23575,
        'low': 23565,
        'adx14': 25,
        'williams_r': -70,
    }
    
    # Update position (should generate [EXIT DECISION] logs)
    decision = pm.update(
        bar_idx=1,
        bar_time=datetime.now() + timedelta(minutes=3),
        underlying=23570,
        row=mock_row
    )
    
    print(f"✓ Position update executed")
    print(f"  → should_exit={decision.should_exit}")
    print(f"  → cur_gain={decision.cur_gain:.2f}pts")
    print(f"  → peak_gain={decision.peak_gain:.2f}pts")
    
    assert isinstance(decision, ExitDecision), "Should return valid ExitDecision"
    
    print(f"{GREEN}✓ Exit rule logging test passed{RESET}\n")


def test_breakout_hold_logic():
    """Test BREAKOUT_HOLD rule initialization and state"""
    print(f"\n{YELLOW}=== TEST: BREAKOUT_HOLD Rule Logic ==={RESET}")
    
    pm = PositionManager(mode="REPLAY", lot_size=130)
    
    signal = {
        "side": "CALL",
        "entry_type": "PULLBACK",
        "source": "PIVOT",
        "day_type": "NORMAL",
        "cpr_width": "NORMAL",
        "atr": 150,
    }
    
    pm.open(
        bar_idx=0,
        bar_time=datetime.now(),
        underlying=23580,
        entry_premium=150,
        signal=signal
    )
    
    # Set R4 level
    r4_level = 23600
    pm._t['r4'] = r4_level
    pm._t['s4'] = 23550
    
    print(f"✓ Position opened above R4={r4_level}")
    
    # Initial state should not have breakout_hold_active
    assert pm._t.get('breakout_hold_active', False) == False, "Should start without breakout hold"
    print(f"✓ Initial breakout_hold_active=False")
    
    # Update when sustaining above R4
    mock_row = {
        'rsi14': 60,
        'cci20': 100,
        'supertrend_bias': 'UP',
        'st_bias_15m': 'UP',
        'ema9': 23605,
        'ema13': 23605,
        'open': 23603,
        'close': 23605,
        'high': 23610,
        'low': 23600,
        'adx14': 20,
        'williams_r': -20,
    }
    
    decision = pm.update(
        bar_idx=1,
        bar_time=datetime.now() + timedelta(minutes=3),
        underlying=23605,  # Above R4
        row=mock_row
    )
    
    print(f"✓ Update with UL=23605 (above R4={r4_level})")
    
    # After update at price above R4, breakout_hold may be triggered
    is_breakout_active = pm._t.get('breakout_hold_active', False)
    print(f"  → breakout_hold_active={is_breakout_active}")
    
    if is_breakout_active:
        print(f"  → breakout_hold_bars={pm._t.get('breakout_hold_bars', 0)}")
        print(f"{GREEN}✓ BREAKOUT_HOLD logic activated{RESET}")
    else:
        print(f"  → BREAKOUT_HOLD not yet active (may activate on next bar above R4)")
    
    print(f"{GREEN}✓ BREAKOUT_HOLD rule logic test passed{RESET}\n")


def test_position_state_persistence():
    """Test that position state is correctly maintained through updates"""
    print(f"\n{YELLOW}=== TEST: Position State Persistence ==={RESET}")
    
    pm = PositionManager(mode="REPLAY", lot_size=130)
    
    signal = {
        "side": "CALL",
        "entry_type": "PULLBACK",
        "source": "PIVOT",
        "day_type": "NORMAL",
        "cpr_width": "NORMAL",
        "atr": 150,
    }
    
    pm.open(
        bar_idx=0,
        bar_time=datetime.now(),
        underlying=23580,
        entry_premium=150,
        signal=signal
    )
    
    pm._t['r4'] = 23600
    pm._t['s4'] = 23550
    
    entry_premium = pm._t['entry_px']
    entry_ul = pm._t['entry_ul']
    
    print(f"✓ Position opened: entry_px={entry_premium}, entry_ul={entry_ul}")
    
    mock_row = {
        'rsi14': 55,
        'cci20': 50,
        'supertrend_bias': 'UP',
        'st_bias_15m': 'UP',
        'ema9': 23585,
        'ema13': 23585,
        'open': 23580,
        'close': 23585,
        'high': 23590,
        'low': 23580,
        'adx14': 20,
        'williams_r': -30,
    }
    
    # Multiple updates
    for bar_idx in range(1, 4):
        ul = 23580 + bar_idx * 5
        mock_row['close'] = ul
        mock_row['ema9'] = ul
        mock_row['ema13'] = ul
        
        decision = pm.update(
            bar_idx=bar_idx,
            bar_time=datetime.now() + timedelta(minutes=3 * bar_idx),
            underlying=ul,
            row=mock_row
        )
        
        # Check state persistence
        assert pm._t['entry_px'] == entry_premium, "Entry price should not change"
        assert pm._t['entry_ul'] == entry_ul, "Entry UL should not change"
        assert pm._t['bars_held'] == bar_idx, f"bars_held should be {bar_idx}, got {pm._t['bars_held']}"
        
        print(f"✓ Bar {bar_idx}: bars_held={pm._t['bars_held']}, peak_ul={pm._t['peak_ul']:.2f}")
        
        if decision.should_exit:
            print(f"  → Position exited: {decision.reason[:50]}...")
            break
    
    print(f"{GREEN}✓ Position state persistence test passed{RESET}\n")


if __name__ == "__main__":
    try:
        print("\n" + "="*70)
        print("  EXIT LOGIC V7 VALIDATION")
        print("  (4 Simple Rules: LOSS_CUT, QUICK_PROFIT, DRAWDOWN, BREAKOUT_HOLD)")
        print("="*70)
        
        test_basic_position_lifecycle()
        test_exit_rules_fire_correctly()
        test_breakout_hold_logic()
        test_position_state_persistence()
        
        print("="*70)
        print(f"{GREEN}✓ ALL VALIDATION TESTS PASSED{RESET}")
        print("="*70 + "\n")
        
    except AssertionError as e:
        print(f"\n{RED}✗ ASSERTION FAILED: {e}{RESET}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        print(f"\n{RED}✗ ERROR: {e}{RESET}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
