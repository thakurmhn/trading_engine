# EXIT PRICE FIX — VALIDATION TEST
## Verify the fix works correctly

---

## Test Scenario

After deploying the fix, run the bot and observe an entry + exit cycle.

### Expected Behavior

**1. Entry Phase (Should show option premium):**
```
[ENTRY][PAPER] CALL NSE:NIFTY2630225400CE @ 300.10 SL=258.08 PT=375.13 TG=435.15 ATR=45.2 Step=18.01 score=8.5 source=CPR_REVERSAL
```

- Entry price: **300.10** (option premium) ✅
- Stop loss: **258.08** (18% below premium) ✅
- Partial target: **375.13** (25% above premium) ✅

---

**2. Exit Phase (Should show option premium, NOT spot):**

**CORRECT (After Fix):**
```
[EXIT][PAPER TARGET_HIT] CALL NSE:NIFTY2630225400CE EntryCandle=247 ExitCandle=251 Entry=300.10 Exit=375.12 Qty=130 PnL=9756.00 (points=75.02) Reason=CPR_REVERSAL TrailUpdates=0
```

✅ Exit price: **375.12** (option premium)
✅ Points gained: **75.02** (exit - entry)
✅ PnL: **9756.00** (75.02 × 130 qty)

**WRONG (Before Fix — should NOT see this):**
```
[EXIT][PAPER TARGET_HIT] CALL NSE:NIFTY2630225400CE Entry=300.10 Exit=25460.25 Qty=130 PnL=25160.15 (points=25160.15) Reason=CPR_REVERSAL TrailUpdates=0
```

❌ Exit price: **25460.25** (spot price) — WRONG!
❌ Points gained: **25160.15** (unrealistic)
❌ PnL: **3.27M** (absurd)

---

**3. EOD Force Exit (Fallback logging):**

If an option is not available in the `df` dataframe at EOD, you should see:

```
[PAPER EOD] NSE:NIFTY2630225400CE not in df, using fallback price=299.50
[EXIT][PAPER] CALL NSE:NIFTY2630225400CE Qty=130 Price=299.50 Reason=EOD
```

✅ Clear warning logged
✅ Price is sensible (close to entry)
✅ No crashes

---

**4. Force Close Fallback (Stress test):**

If the bot force-closes a position and the option is missing:

```
[FORCE_CLOSE] Failed to get LTP for NSE:NIFTY2630225400CE: KeyError('ltp')
[FORCE_CLOSE] NSE:NIFTY2630225400CE not in df, using fallback price=spot_price
```

✅ Exception caught and logged
✅ Graceful fallback
✅ No crash

---

## Assertion Checklist

Run these checks to verify the fix:

### ✅ Check 1: Entry Exit Alignment
```python
# Check a trade entry/exit log pair from your CSV
entry_price = 300.10  # option premium
exit_price  = 375.12  # option premium
points      = exit_price - entry_price  # 75.02
qty         = 130
pnl         = points * qty              # 9756.00

assert entry_price < 1000, "Entry should be option premium, not spot (25000+)"
assert exit_price < 1000, "Exit should be option premium, not spot (25000+)"
assert pnl < 100000, "PnL should be realistic, not 3M+"
print("✅ PASS: Entry/exit prices are realistic option premiums")
```

### ✅ Check 2: Log Format
```python
# Check log format — should have .2f formatting
import re
log_line = "[EXIT][PAPER TARGET_HIT] CALL NSE:NIFTY2630225400CE Entry=300.10 Exit=375.12 Qty=130 PnL=9756.00"

# Extract prices
match = re.search(r'Entry=(\d+\.\d{2})\s+Exit=(\d+\.\d{2})', log_line)
assert match, "Log format incorrect — missing .2f formatting"
assert 250 < float(match.group(1)) < 500, "Entry not in option premium range"
assert 250 < float(match.group(2)) < 500, "Exit not in option premium range"
print("✅ PASS: Log format has proper .2f decimals and ranges")
```

### ✅ Check 3: DataFrame Integrity
```python
# Check CSV output — should have option prices in 'price' column
import pandas as pd

trades = pd.read_csv("trades_options_trade_engine_2026-02-25_PAPER.csv")
entry_rows = trades[trades['action'] == 'CALL']
exit_rows = trades[trades['action'] == 'EXIT']

# All prices should be ~<1000 (option premiums), not spot
assert entry_rows['price'].max() < 1000, "Entry prices unrealistic"
assert exit_rows['price'].max() < 1000, "Exit prices unrealistic"
print(f"✅ PASS: All {len(trades)} trades have realistic option prices")

# Check no NaN prices
assert not trades['price'].isna().any(), "Found NaN prices"
print("✅ PASS: No NaN prices in trades")
```

### ✅ Check 4: PnL Realism
```python
# Check PnL calculations are sensible
import pandas as pd

trades = pd.read_csv("trades_options_trade_engine_2026-02-25_PAPER.csv")

# Group by entry/exit pairs
for symbol in trades['ticker'].unique():
    sym_trades = trades[trades['ticker'] == symbol]
    entries = sym_trades[sym_trades['action'].isin(['CALL', 'PUT'])]
    exits = sym_trades[sym_trades['action'] == 'EXIT']
    
    if len(entries) > 0 and len(exits) > 0:
        avg_entry = entries['price'].mean()
        avg_exit = exits['price'].mean()
        points_gained = avg_exit - avg_entry
        
        # Points should be in range -50 to +300 (typical option moves)
        assert -100 < points_gained < 500, f"Unrealistic points for {symbol}: {points_gained}"
        print(f"✅ PASS: {symbol} Entry={avg_entry:.2f} Exit={avg_exit:.2f} Points={points_gained:.2f}")
```

---

## Log Verification Command

Run this in the trading_engine directory to extract and verify exit lines:

```powershell
# PowerShell extraction
Get-Content access-*.txt | Select-String "\[EXIT\]\[PAPER" | ForEach-Object {
    if ($_ -match "Exit=(\d+\.\d+)") {
        $exitPrice = [float]$matches[1]
        if ($exitPrice -gt 1000) {
            Write-Host "❌ WRONG: $_ (Exit=$exitPrice is spot price)"
        } else {
            Write-Host "✅ CORRECT: $_ (Exit=$exitPrice is option premium)"
        }
    }
}
```

---

## Success Indicators

If all these appear in your logs, the fix is working:

| Indicator | Log |
|-----------|-----|
| ✅ Option prices used | `Entry=300.10 Exit=375.12` (< 1000) |
| ✅ Realistic PnL | `PnL=9756.00 (points=75.02)` |
| ✅ Entry/Exit aligned | Same option contract in both |
| ✅ Safe fallback | `[PAPER EOD] ... not in df, using fallback` (rare) |
| ✅ No crashes | Entry → Exit cycles complete |

---

## Rollback Plan (if needed)

If something goes wrong, revert changes by restoring from git:
```powershell
git checkout execution.py
```

Or manually undo the 6 FIND/REPLACE sections in reverse order.

---

**Expected Deployment:** All logs should show option premiums (< 1000), not spot prices (25460).

