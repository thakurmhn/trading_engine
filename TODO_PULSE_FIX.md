# TODO: Pulse Module tick_rate Fix

## Issue
The Pulse module's `tick_rate` is being retrieved using `getattr()` but `pulse.get_pulse()` returns a dictionary, not an object with attributes. This causes `tick_rate` to always be 0 (the default).

## Files to Edit
- [ ] execution.py - Fix in `paper_order` function (around line ~3724)
- [ ] execution.py - Fix in `live_order` function (around line ~4600+)

## Changes Required

### 1. In paper_order function:
Change:
```python
pulse_metrics = pulse.get_pulse()
tick_rate = getattr(pulse_metrics, "tick_rate", 0)
```
To:
```python
pulse_metrics = pulse.get_pulse()
tick_rate = pulse_metrics.get("tick_rate", 0) if isinstance(pulse_metrics, dict) else getattr(pulse_metrics, "tick_rate", 0)
```

### 2. In live_order function:
Same change as above.

## Status
- [ ] Apply fix to paper_order
- [ ] Apply fix to live_order
- [ ] Verify syntax

