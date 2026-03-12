# Error Handling Improvements for execution.py

import logging
import time
from functools import wraps

def retry_on_failure(max_retries=3, delay=1.0, exceptions=(Exception,)):
    """Decorator to retry failed operations with exponential backoff"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        wait_time = delay * (2 ** attempt)
                        logging.warning(f"[RETRY] {func.__name__} failed (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait_time}s")
                        time.sleep(wait_time)
                    else:
                        logging.error(f"[RETRY FAILED] {func.__name__} failed after {max_retries} attempts: {e}")
            raise last_exception
        return wrapper
    return decorator

def safe_api_call(func):
    """Decorator for safe API calls with proper error handling"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except ConnectionError as e:
            logging.error(f"[API CONNECTION ERROR] {func.__name__}: {e}")
            return None, f"Connection failed: {e}"
        except TimeoutError as e:
            logging.error(f"[API TIMEOUT] {func.__name__}: {e}")
            return None, f"Request timeout: {e}"
        except Exception as e:
            logging.error(f"[API ERROR] {func.__name__}: {e}")
            return None, f"API error: {e}"
    return wrapper

def safe_market_data_fetch(symbol, fallback_value=None):
    """Safely fetch market data with fallback"""
    try:
        if symbol in df.index:
            ltp = df.loc[symbol, "ltp"]
            if ltp is not None and not pd.isna(ltp):
                return float(ltp)
    except Exception as e:
        logging.warning(f"[MARKET DATA ERROR] Failed to fetch {symbol}: {e}")
    
    return fallback_value

def validate_order_params(symbol, qty, price):
    """Validate order parameters before submission"""
    errors = []
    
    if not symbol or not isinstance(symbol, str):
        errors.append("Invalid symbol")
    
    if not isinstance(qty, int) or qty <= 0:
        errors.append("Invalid quantity")
    
    if not isinstance(price, (int, float)) or price <= 0:
        errors.append("Invalid price")
    
    if errors:
        raise ValueError(f"Order validation failed: {', '.join(errors)}")
    
    return True