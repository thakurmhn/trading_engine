# Trading Engine - Development Guidelines

## Code Quality Standards

### Naming Conventions
- **Functions**: snake_case (e.g., `build_dynamic_levels`, `check_exit_condition`)
- **Classes**: PascalCase (e.g., `MarketData`, `CompressionState`, `RegimeContext`)
- **Constants**: UPPER_SNAKE_CASE (e.g., `MAX_DAILY_LOSS`, `SCALP_PT_POINTS`)
- **Private functions**: Leading underscore (e.g., `_trend_entry_quality_gate`, `_get_option_market_snapshot`)
- **Global state**: Trailing underscore for module-level caches (e.g., `_paper_zones`, `_logged_trades`)

### Code Organization
- **Imports**: Group by standard library, third-party, local (PEP 8 style)
- **Module structure**: Constants → Classes → Functions → Main logic
- **Function length**: Keep functions under 200 lines; extract complex logic to helpers
- **Docstrings**: Use triple-quoted strings for module/class/function documentation
- **Type hints**: Use for function signatures where clarity is needed (not required everywhere)

### Documentation Standards
- **Inline comments**: Explain WHY, not WHAT (code shows what)
- **Log messages**: Use structured format: `[TAG] message` (e.g., `[ENTRY BLOCKED][ST_CONFLICT]`)
- **Audit trails**: Log all entry/exit decisions with timestamp, symbol, reason
- **Error messages**: Include context (symbol, price, threshold) for debugging

### Error Handling
- **Try-except blocks**: Catch specific exceptions, log with context
- **Fallback logic**: Provide sensible defaults (e.g., fallback to spot price if option LTP unavailable)
- **Validation**: Check data types and ranges before use (e.g., `if np.isfinite(value)`)
- **Recovery**: Implement graceful degradation (e.g., skip indicator if insufficient data)

## Structural Conventions

### Configuration Management
- **Centralized config**: All tunable parameters in `config.py` or dedicated config dicts
- **Environment overrides**: Use `os.getenv()` for runtime customization
- **Constants extraction**: Move hardcoded values to module-level dicts (see `REGIME_MATRIX`, `TRADING_CONSTANTS`)
- **Example**:
```python
# config.py
TREND_ENTRY_ADX_MIN = float(os.getenv("TREND_ENTRY_ADX_MIN", "18.0"))

# execution.py
REGIME_MATRIX = {
    "TRENDING_DAY": {"RSI_FLOOR": 0, "COUNTER_PENALTY": -15},
    "RANGE_DAY": {"RSI_FLOOR": None, "COUNTER_PENALTY": 0},
}
```

### State Management
- **Pickle-based persistence**: Use `pickle.dump()` for daily state snapshots
- **Ledger format**: Store list of timestamped snapshots, not single dict
- **Restart recovery**: Load latest snapshot and validate against current gates
- **Example**:
```python
def store(data, account_type_):
    ledger = load_existing_or_empty()
    snapshot = {"timestamp": dt.now(time_zone), "state": data}
    ledger.append(snapshot)
    pickle.dump(ledger, open(filename, "wb"))

def load(account_type_):
    ledger = pickle.load(open(filename, "rb"))
    return ledger[-1]["state"] if ledger else {}
```

### Logging Architecture
- **Color coding**: Use ANSI colors for visual scanning (GREEN=entry, YELLOW=exit, RED=error)
- **Log levels**: INFO for signals/orders, DEBUG for calculations, WARNING for fallbacks
- **Structured format**: `[COMPONENT][ACTION] details` (e.g., `[ENTRY][PAPER] CALL signal`)
- **Audit trail**: Every entry/exit must log: timestamp, symbol, side, reason, position_id
- **Example**:
```python
logging.info(
    f"{GREEN}[ENTRY][PAPER] {side} {opt_name} @ {entry_price:.2f} "
    f"SL={stop:.2f} PT={pt:.2f} position_id={position_id}{RESET}"
)
```

### Broker Integration Pattern
- **Adapter pattern**: Abstract broker-specific logic behind `BrokerAdapter` interface
- **Lazy imports**: Import broker SDKs only when needed (avoid hard dependency)
- **Factory function**: Use `build_broker_adapter()` to construct broker instance
- **Error handling**: Catch API errors and provide fallback behavior
- **Example**:
```python
def build_broker_adapter(broker_override=None):
    selected = (broker_override or BROKER).lower()
    if selected == "fyers":
        return _build_fyers_adapter()
    elif selected == "zerodha":
        return _build_zerodha_adapter()
    else:
        raise ValueError(f"Unknown broker: {selected}")
```

## Semantic Patterns

### Entry Signal Detection
- **Multi-layer gating**: Supertrend → ADX → Oscillators → Pivots
- **Override hierarchy**: Reversal signals (score ≥80) override Supertrend conflicts
- **Conflict resolution**: When 3m/15m Supertrend disagree, check reversal/CPR/ADX for override
- **Example**:
```python
# Check Supertrend alignment first
aligned, allowed_side, details = _supertrend_alignment_gate(...)
if not aligned:
    # Check for reversal override
    if reversal_signal and reversal_signal["score"] >= 80:
        allowed_side = reversal_signal["side"]  # override
    else:
        return False, None, "Supertrend conflict"
```

### Exit Precedence Logic
- **Strict ordering**: HFT → SL → PT/TG → Min bars → Contextual → Oscillator → ST flip → Reversal → Momentum → Time
- **Survivability guardrails**: Scalp trades must hold ≥2 bars before SL (unless extreme move)
- **Partial exits**: PT1 at 40%, PT2 at 30%, ratchet SL to entry after PT1
- **Example**:
```python
def check_exit_condition(df_slice, state, option_price, timestamp):
    # 1. HFT override (highest priority)
    if hf_mgr.check_exit(...):
        return True, "HFT_EXIT"
    
    # 2. Stop loss (with survivability check)
    if bars_held < min_hold and current_ltp <= stop:
        if not is_extreme_move:
            return False, None  # defer
    
    # 3. Profit targets (partial exits)
    if pt_hit and bars_held >= min_hold:
        return True, "PT1_PARTIAL_EXIT"
    
    # ... continue through precedence
```

### Regime-Adaptive Parameters
- **ATR tiers**: Classify volatility into VERY_LOW/LOW/MODERATE/HIGH regimes
- **Day type modifiers**: TRENDING_DAY holds longer, RANGE_DAY exits faster
- **ADX tiers**: Strong (>40) allows wider SL/PT, weak (<20) requires tighter stops
- **Example**:
```python
if atr <= 60:
    regime = "VERY_LOW"
    pt_mult, tg_mult = 1.7, 2.3
elif atr <= 100:
    regime = "LOW"
    pt_mult, tg_mult = 2.0, 2.8
else:
    regime = "MODERATE"
    pt_mult, tg_mult = 2.2, 3.2

# Apply day type override
if day_type == "TRENDING_DAY":
    trail_step = max(trail_step, entry_atr * 1.8)
```

### Deduplication Pattern
- **Track (symbol, timestamp, price) tuples**: Prevent duplicate entries on same candle
- **Daily reset**: Clear tracker at session start
- **Example**:
```python
_logged_trades = set()
_dedup_date = ""

def paper_order(...):
    today = ct.strftime("%Y-%m-%d")
    if _dedup_date != today:
        _logged_trades.clear()
        _dedup_date = today
    
    trade_key = (opt_name, last_candle_time, round(entry_price, 2))
    if trade_key in _logged_trades:
        logging.warning(f"[DUPLICATE BLOCKED] {opt_name}")
        return
    _logged_trades.add(trade_key)
```

### Oscillator Bounds Expansion
- **Base thresholds**: RSI [30-70], CCI [-150, 150]
- **ADX expansion**: Strong trends (ADX>40) → RSI [20-80], CCI [-250, 250]
- **ATR expansion**: High volatility (ATR > 1.5x MA) → +5 RSI, +30 CCI
- **Gap-aware**: GAP_UP favors CALL → +2 RSI_MAX, +10 CCI_MAX
- **Example**:
```python
if adx_val > 40:
    rsi_min, rsi_max = 20.0, 80.0
    cci_min, cci_max = -250.0, 250.0
elif adx_val > 30:
    rsi_min, rsi_max = 25.0, 75.0
    cci_min, cci_max = -200.0, 200.0

if atr > 1.5 * atr_ma:
    rsi_max += 5
    cci_max += 30
```

### Partial Exit Ladder
- **PT1 (40% qty)**: Exit at 1.7x ATR, lock SL at entry
- **PT2 (30% qty)**: Exit at 2.3x ATR, ratchet SL to PT1 level
- **Remaining (30% qty)**: Trailing stop or time exit
- **Example**:
```python
if pt_hit and not partial_booked:
    partial_qty = int(qty * 0.40)
    state["partial_booked"] = True
    state["stop"] = entry_price  # lock at entry
    return True, "PT1_PARTIAL_EXIT"

if tg_hit and partial_booked:
    partial_qty = int(qty * 0.30)
    state["stop"] = max(state["stop"], pt)  # ratchet
    return True, "PT2_PARTIAL_EXIT"
```

## Common Code Idioms

### Safe Value Extraction
```python
# Extract with NaN/None safety
value = float(series.iloc[-1]) if pd.notna(series.iloc[-1]) else float("nan")

# Conditional formatting
formatted = f"{value:.2f}" if np.isfinite(value) else "N/A"

# Fallback chain
result = (
    df.loc[symbol, "ltp"] if symbol in df.index and pd.notna(df.loc[symbol, "ltp"])
    else fallback_price
)
```

### Logging with Context
```python
# Always include: timestamp, symbol, side, reason, position_id
logging.info(
    f"[EXIT][{mode}] {side} {symbol} "
    f"Entry={entry:.2f} Exit={exit_price:.2f} PnL={pnl_value:.2f} "
    f"BarsHeld={bars_held} Reason={reason} PositionId={position_id}"
)
```

### Conditional Logging (Avoid Spam)
```python
# Log once per N iterations
check_count = state.get("exit_check_count", 0)
if check_count % 5 == 0:
    logging.info(f"[EXIT CHECK] {side} bars_held={bars_held}")
state["exit_check_count"] = check_count + 1
```

### Safe Dictionary Updates
```python
# Merge with defaults
state.setdefault("lifecycle_state", "EXIT")
state.setdefault("scalp_mode", False)
state.setdefault("trail_updates", 0)

# Conditional update
if new_value is not None:
    state["key"] = new_value
```

### Dataframe Operations
```python
# Filter with safety
if "column" in df.columns and not df.empty:
    filtered = df[df["column"] > threshold]
else:
    filtered = pd.DataFrame()

# Aggregate safely
result = df["value"].sum() if not df.empty else 0.0
```

## Testing Patterns

### Fixture Setup
```python
@pytest.fixture(autouse=True)
def reset_logging_disable():
    """Restore logging before every test."""
    logging.disable(logging.NOTSET)
    yield
```

### Assertion Patterns
```python
# Assert with context
assert quality_ok, f"Entry gate failed: {gate_reason}"

# Assert logs
with self.assertLogs(level="INFO") as cm:
    paper_order(...)
    self.assertIn("[ENTRY]", cm.output[0])
```

## Performance Optimization

### Caching Patterns
```python
# Cache expensive calculations
_cache = {}
_cache_date = ""

def get_cached_value():
    global _cache, _cache_date
    today = dt.now().strftime("%Y-%m-%d")
    if _cache_date != today:
        _cache = expensive_calculation()
        _cache_date = today
    return _cache
```

### Vectorized Operations
```python
# Use pandas/numpy for bulk operations
pnl_series = df["exit_price"] - df["entry_price"]
win_rate = (pnl_series > 0).mean()
avg_win = pnl_series[pnl_series > 0].mean()
```

## Deployment Checklist

- [ ] All hardcoded values extracted to config.py
- [ ] Logging includes [TAG] prefix for all messages
- [ ] Error handling with fallback logic
- [ ] State persistence tested with restart scenario
- [ ] Broker adapter pattern used for API calls
- [ ] Deduplication logic prevents duplicate entries
- [ ] Audit trail logs all entry/exit decisions
- [ ] Tests pass with pytest
- [ ] Code follows naming conventions
- [ ] Documentation updated for new features
