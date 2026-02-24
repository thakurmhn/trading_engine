#!/usr/bin/env python3
"""
Quick test to verify PUT score breakdown logging works.
"""

import logging
import sys
from position_manager import PositionManager

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Create a test signal with breakdown
test_signal = {
    "side": "PUT",
    "score": 52,
    "threshold": 50,
    "atr": 150,
    "atr14": 150,
    "cpr_width": "NARROW",
    "entry_type": "PULLBACK",
    "st_bias": "BEARISH",
    "pivot_reason": "ACCEPTANCE_R4",
    "breakdown": {
        "trend_alignment": 20,
        "rsi_score": 10,
        "cci_score": 15,
        "vwap_position": 5,
        "pivot_structure": 10,
        "momentum_ok": 15,
        "cpr_width": 5,
        "entry_type_bonus": 5,
    },
    "source": "PIVOT", 
    "pivot": "ACCEPTANCE_R4",
}

# Create PM instance
pm = PositionManager(mode="REPLAY", lot_size=130)

# Test the breakdown logging
print("\n=== Testing PUT Score Breakdown Logging ===\n")
pm._log_entry_score_breakdown(test_signal, "PUT")

print("\n=== Testing CALL Score Breakdown Logging ===\n")
test_signal["side"] = "CALL"
test_signal["breakdown"]["trend_alignment"] = 20
test_signal["breakdown"]["momentum_ok"] = 15
pm._log_entry_score_breakdown(test_signal, "CALL")

print("\n=== Test Complete ===")
print("✓ PUT score breakdown logging working")
print("✓ CALL score breakdown logging working")
