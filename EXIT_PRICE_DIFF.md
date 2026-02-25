# EXIT PRICE FIX — COPY-PASTE DIFF
## For: execution.py

---

## FIX #1: process_order() — Fetch option price (Lines 827–871)

### LOCATION: execution.py, inside process_order() function
### FIND THIS:
```python
    # --- Explicit SL/Target checks ---
    if side == "CALL":
        if current_candle["low"] <= state["stop"] + buffer:
            exit_reason = "SL_HIT"
        elif spot_price >= state["pt"] - buffer:
            exit_reason = "TARGET_HIT"
    elif side == "PUT":
        if current_candle["high"] >= state["stop"] - buffer:
            exit_reason = "SL_HIT"
        elif spot_price <= state["pt"] + buffer:
            exit_reason = "TARGET_HIT"
```

### REPLACE WITH:
```python
    # --- Get option's current price from df (not spot price) ---
    option_current_price = None
    if symbol in df.index:
        try:
            option_current_price = float(df.loc[symbol, "ltp"])
        except Exception:
            pass
    
    # Use option price if available; fallback to spot for target check
    current_option_price = option_current_price if option_current_price else spot_price

    # --- Explicit SL/Target checks (using option price, not spot) ---
    if side == "CALL":
        if current_candle["low"] <= state["stop"] + buffer:
            exit_reason = "SL_HIT"
        elif current_option_price >= state["pt"] - buffer:
            exit_reason = "TARGET_HIT"
    elif side == "PUT":
        if current_candle["high"] >= state["stop"] - buffer:
            exit_reason = "SL_HIT"
        elif current_option_price <= state["pt"] + buffer:
            exit_reason = "TARGET_HIT"
```

---

## FIX #2: process_order() — Use option price for exit PnL (Lines 858–861)

### FIND THIS:
```python
    if success:
        exit_price = current_candle["close"]
        pnl_points = exit_price - entry if side == "CALL" else entry - exit_price
        pnl_value  = pnl_points * qty
```

### REPLACE WITH:
```python
    if success:
        # FIX: Use the option's actual traded price (from df), not spot candle close
        exit_price = option_current_price if option_current_price else current_candle["close"]
        pnl_points = exit_price - entry if side == "CALL" else entry - exit_price
        pnl_value  = pnl_points * qty
```

---

## FIX #3: cleanup_trade_exit() — Validate exit_price (Lines 908–943)

### FIND THIS:
```python
def cleanup_trade_exit(info, leg, side, name, qty, exit_price, mode, reason):
    """
    Unified cleanup for any exit (STOPLOSS, TARGET, PARTIAL, EOD, FORCE).
    Ensures trade_flag reset to 0 so new entries are allowed.
    """
    ct = dt.now(time_zone)
    info[leg]["trade_flag"] = 0        # ✅ always reset
    info[leg]["quantity"] = 0
    info[leg]["filled_df"].loc[ct] = {
        'ticker': name,
        'price': exit_price,
        'action': 'EXIT',
        'stop_price': None,
        'take_profit': None,
        'spot_price': spot_price,
        'quantity': qty
    }
    logging.info(
        f"{RED}[EXIT][{mode}] {side} {name} Qty={qty} Price={exit_price} Reason={reason}{RESET}"
    )
```

### REPLACE WITH:
```python
def cleanup_trade_exit(info, leg, side, name, qty, exit_price, mode, reason):
    """
    Unified cleanup for any exit (STOPLOSS, TARGET, PARTIAL, EOD, FORCE).
    Ensures trade_flag reset to 0 so new entries are allowed.
    
    FIX: exit_price should always be the option's traded price, not spot_price.
    If exit_price is None or invalid, try to fetch from df as last resort.
    """
    ct = dt.now(time_zone)
    
    # Ensure exit_price is the option's traded price, not spot
    if exit_price is None or (isinstance(exit_price, float) and pd.isna(exit_price)):
        # Fallback: try to get from df dataframe
        if name in df.index:
            try:
                exit_price = float(df.loc[name, "ltp"])
            except Exception:
                exit_price = spot_price if spot_price else 0
        else:
            exit_price = spot_price if spot_price else 0
    
    info[leg]["trade_flag"] = 0        # ✅ always reset
    info[leg]["quantity"] = 0
    info[leg]["filled_df"].loc[ct] = {
        'ticker': name,
        'price': exit_price,
        'action': 'EXIT',
        'stop_price': None,
        'take_profit': None,
        'spot_price': spot_price,
        'quantity': qty
    }
    logging.info(
        f"{RED}[EXIT][{mode}] {side} {name} Qty={qty} Price={exit_price:.2f} Reason={reason}{RESET}"
    )
```

---

## FIX #4: force_close_old_trades() — Enhanced price retrieval (Lines 945–977)

### FIND THIS:
```python
def force_close_old_trades(info, mode):
    ct = dt.now(time_zone)
    for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
        if info[leg]["trade_flag"] == 1:  # still active
            name = info[leg]["option_name"]
            qty  = info[leg]["quantity"]

            if mode.upper() == "PAPER":
                success, order_id = send_paper_exit_order(name, qty, "FORCE_CLEANUP")
            else:
                success, order_id = send_live_exit_order(name, qty, "FORCE_CLEANUP")

            if success:
                exit_price = df.loc[name, "ltp"] if name in df.index else spot_price
                cleanup_trade_exit(info, leg, side, name, qty, exit_price, mode, "FORCE_CLEANUP")
```

### REPLACE WITH:
```python
def force_close_old_trades(info, mode):
    """Force close any open positions. Retrieves option's actual price from df."""
    ct = dt.now(time_zone)
    for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
        if info[leg]["trade_flag"] == 1:  # still active
            name = info[leg]["option_name"]
            qty  = info[leg]["quantity"]

            if mode.upper() == "PAPER":
                success, order_id = send_paper_exit_order(name, qty, "FORCE_CLEANUP")
            else:
                success, order_id = send_live_exit_order(name, qty, "FORCE_CLEANUP")

            if success:
                # FIX: Ensure we get the option's traded price, with safe fallback
                exit_price = None
                if name in df.index:
                    try:
                        ltp = df.loc[name, "ltp"]
                        if ltp and not (isinstance(ltp, float) and pd.isna(ltp)):
                            exit_price = float(ltp)
                    except Exception as e:
                        logging.warning(f"[FORCE_CLOSE] Failed to get LTP for {name}: {e}")
                
                if exit_price is None:
                    exit_price = spot_price if spot_price else 0
                    logging.warning(f"[FORCE_CLOSE] {name} not in df, using fallback price={exit_price}")
                
                cleanup_trade_exit(info, leg, side, name, qty, exit_price, mode, "FORCE_CLEANUP")
```

---

## FIX #5: paper_order() EOD Exit — Enhanced price retrieval (Lines 1014–1040)

### FIND THIS (inside paper_order function):
```python
    # 3. End-of-day force exit
    if ct > end_time:
        logging.info("[PAPER] EOD — closing open positions")
        for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
            if paper_info[leg]["trade_flag"] == 1:
                name = paper_info[leg]["option_name"]
                qty  = paper_info[leg]["quantity"]
                ep   = df.loc[name, "ltp"] if name in df.index else spot_price
                send_paper_exit_order(name, qty, "EOD")
                cleanup_trade_exit(paper_info, leg, side, name, qty, ep, "PAPER", "EOD")
        store(paper_info, account_type)
        return
```

### REPLACE WITH:
```python
    # 3. End-of-day force exit
    if ct > end_time:
        logging.info("[PAPER] EOD — closing open positions")
        for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
            if paper_info[leg]["trade_flag"] == 1:
                name = paper_info[leg]["option_name"]
                qty  = paper_info[leg]["quantity"]
                
                # FIX: Retrieve option's actual traded price with safe fallback
                ep = None
                if name in df.index:
                    try:
                        ltp = df.loc[name, "ltp"]
                        if ltp and not (isinstance(ltp, float) and pd.isna(ltp)):
                            ep = float(ltp)
                    except Exception:
                        pass
                
                if ep is None:
                    ep = spot_price if spot_price else 0
                    logging.warning(f"[PAPER EOD] {name} not in df, using fallback price={ep}")
                
                send_paper_exit_order(name, qty, "EOD")
                cleanup_trade_exit(paper_info, leg, side, name, qty, ep, "PAPER", "EOD")
        store(paper_info, account_type)
        return
```

---

## FIX #6: live_order() EOD Exit — Enhanced price retrieval (Lines 1258–1285)

### FIND THIS (inside live_order function):
```python
    # 3. End-of-day force exit
    if ct > end_time:
        logging.info("[LIVE] EOD — closing positions")
        for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
            if live_info[leg]["trade_flag"] == 1:
                name = live_info[leg]["option_name"]
                qty  = live_info[leg]["quantity"]
                success, order_id = send_live_exit_order(name, qty, "EOD")
                if success:
                    ep = df.loc[name, "ltp"] if name in df.index else spot_price
                    cleanup_trade_exit(live_info, leg, side, name, qty, ep, "LIVE", "EOD")
                    update_order_status(order_id, "PENDING", qty, ep, name)
        return
```

### REPLACE WITH:
```python
    # 3. End-of-day force exit
    if ct > end_time:
        logging.info("[LIVE] EOD — closing positions")
        for leg, side in [("call_buy", "CALL"), ("put_buy", "PUT")]:
            if live_info[leg]["trade_flag"] == 1:
                name = live_info[leg]["option_name"]
                qty  = live_info[leg]["quantity"]
                success, order_id = send_live_exit_order(name, qty, "EOD")
                if success:
                    # FIX: Retrieve option's actual traded price with safe fallback
                    ep = None
                    if name in df.index:
                        try:
                            ltp = df.loc[name, "ltp"]
                            if ltp and not (isinstance(ltp, float) and pd.isna(ltp)):
                                ep = float(ltp)
                        except Exception:
                            pass
                    
                    if ep is None:
                        ep = spot_price if spot_price else 0
                        logging.warning(f"[LIVE EOD] {name} not in df, using fallback price={ep}")
                    
                    cleanup_trade_exit(live_info, leg, side, name, qty, ep, "LIVE", "EOD")
                    update_order_status(order_id, "PENDING", qty, ep, name)
        return
```

---

## Verification

After applying all 6 fixes, verify logs show:

✅ **Entry (Correct before, remains correct):**
```
[ENTRY][PAPER] CALL NSE:NIFTY2630225400CE @ 300.10 SL=258.08 ...
```

✅ **Exit (Now fixed — should show option price, NOT spot):**
```
[EXIT][PAPER TARGET_HIT] CALL NSE:NIFTY2630225400CE Entry=300.10 Exit=375.12 Qty=130 PnL=9756.00 (points=75.02)
```

❌ **Before (incorrect — should NOT see this anymore):**
```
[EXIT][PAPER TARGET_HIT] CALL NSE:NIFTY2630225400CE Entry=300.10 Exit=25460.25 Qty=130 PnL=25160.15 (points=25160.15)
```

---

**All fixes applied to production file:** ✅ c:\Users\mohan\trading_engine\execution.py
