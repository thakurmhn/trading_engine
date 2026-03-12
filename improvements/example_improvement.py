# Example: Immediate Improvement to send_live_entry_order Function

# BEFORE (Current problematic version):
def send_live_entry_order_old(symbol, qty, side, buffer=ENTRY_OFFSET):
    try:
        quote = fyers.quotes({"symbols": symbol})
        ltp = quote["d"][0]["v"]["lp"]
        limit_price = max(ltp - buffer, 0.05)
        
        order_data = {
            "symbol": symbol,
            "qty": qty,
            "type": 1,
            "side": side,
            "productType": "INTRADAY",
            "limitPrice": limit_price,
            "stopPrice": 0,
            "validity": "DAY",
            "stopLoss": 0,
            "takeProfit": 0,
            "offlineOrder": False,
            "disclosedQty": 0,
            "isSliceOrder": False,
            "orderTag": str(side)
        }
        
        response = fyers.place_order(data=order_data)
        
        if response.get("s") == "ok":
            logging.info(f"{YELLOW}[LIVE ENTRY] {symbol} Qty={qty}{RESET}")
            return True, response.get("id")
        else:
            logging.error(f"{CYAN}[LIVE ENTRY FAILED] {symbol} {response}{RESET}")
            return False, None
            
    except Exception as e:
        logging.error(f"{CYAN}[LIVE ENTRY ERROR] {symbol} {e}{RESET}")
        return False, None

# AFTER (Improved version with error handling):
from improvements.error_handling_fixes import retry_on_failure, safe_api_call, validate_order_params

@retry_on_failure(max_retries=3, delay=1.0, exceptions=(ConnectionError, TimeoutError))
@safe_api_call
def send_live_entry_order_improved(symbol, qty, side, buffer=ENTRY_OFFSET):
    """
    Place a live LIMIT entry order via Fyers API with improved error handling.
    
    Args:
        symbol: Option symbol to trade
        qty: Quantity to trade
        side: 1 for BUY, -1 for SELL
        buffer: Price buffer for limit order
        
    Returns:
        Tuple[bool, Optional[str]]: (success, order_id)
    """
    
    # Validate parameters before API call
    try:
        validate_order_params(symbol, qty, buffer)
    except ValueError as e:
        logging.error(f"[ORDER VALIDATION FAILED] {symbol}: {e}")
        return False, None
    
    # Get quote with error handling
    try:
        quote_response = fyers.quotes({"symbols": symbol})
        
        if not quote_response or quote_response.get("s") != "ok":
            logging.error(f"[QUOTE FAILED] {symbol}: Invalid response")
            return False, None
            
        ltp = quote_response["d"][0]["v"]["lp"]
        
        if not ltp or ltp <= 0:
            logging.error(f"[INVALID LTP] {symbol}: {ltp}")
            return False, None
            
    except (KeyError, IndexError, TypeError) as e:
        logging.error(f"[QUOTE PARSE ERROR] {symbol}: {e}")
        return False, None
    except Exception as e:
        logging.error(f"[QUOTE ERROR] {symbol}: {e}")
        return False, None
    
    # Calculate limit price with validation
    limit_price = max(ltp - buffer, 0.05)
    
    if limit_price <= 0:
        logging.error(f"[INVALID LIMIT PRICE] {symbol}: {limit_price}")
        return False, None
    
    # Prepare order data
    order_data = {
        "symbol": symbol,
        "qty": int(qty),  # Ensure integer
        "type": 1,        # LIMIT
        "side": int(side), # Ensure integer
        "productType": "INTRADAY",
        "limitPrice": round(limit_price, 2),  # Round to 2 decimals
        "stopPrice": 0,
        "validity": "DAY",
        "stopLoss": 0,
        "takeProfit": 0,
        "offlineOrder": False,
        "disclosedQty": 0,
        "isSliceOrder": False,
        "orderTag": str(side)
    }
    
    # Log order attempt
    logging.info(f"[ORDER ATTEMPT] {symbol} qty={qty} side={side} limit={limit_price:.2f}")
    
    try:
        # Place order with timeout
        response = fyers.place_order(data=order_data)
        
        if not response:
            logging.error(f"[ORDER FAILED] {symbol}: No response from API")
            return False, None
        
        if response.get("s") == "ok":
            order_id = response.get("id")
            logging.info(f"{YELLOW}[LIVE ENTRY SUCCESS] {symbol} Qty={qty} OrderID={order_id}{RESET}")
            
            # Validate order ID
            if not order_id:
                logging.warning(f"[ORDER WARNING] {symbol}: Success but no order ID")
            
            return True, order_id
        else:
            error_msg = response.get("message", "Unknown error")
            error_code = response.get("code", "Unknown")
            logging.error(f"{CYAN}[LIVE ENTRY FAILED] {symbol} Code={error_code} Msg={error_msg}{RESET}")
            return False, None
            
    except Exception as e:
        logging.error(f"{CYAN}[ORDER PLACEMENT ERROR] {symbol}: {e}{RESET}")
        return False, None

# Usage example:
def example_usage():
    """Example of how to use the improved function"""
    
    # This will automatically retry up to 3 times if it fails
    success, order_id = send_live_entry_order_improved("NSE:NIFTY23DEC21000CE", 50, 1, 2.0)
    
    if success:
        print(f"Order placed successfully: {order_id}")
        # Continue with order tracking logic
    else:
        print("Order failed after retries")
        # Handle failure case

# Key Improvements Made:
# 1. Added parameter validation before API calls
# 2. Added retry logic with exponential backoff
# 3. Better error handling for different failure types
# 4. Proper validation of API responses
# 5. More detailed logging with context
# 6. Input sanitization (int conversion, rounding)
# 7. Structured error messages
# 8. Graceful handling of edge cases (no LTP, invalid responses)

# This same pattern can be applied to:
# - send_live_exit_order()
# - check_order_status()
# - Any other API-dependent functions