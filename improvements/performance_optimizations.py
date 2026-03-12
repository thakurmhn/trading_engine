# Performance Optimization Improvements

import pandas as pd
import numpy as np
from functools import lru_cache
from typing import Dict, Any, Optional, Tuple
import time

class PerformanceOptimizer:
    """Performance optimization utilities for trading engine"""
    
    def __init__(self):
        self.calculation_cache = {}
        self.last_cache_clear = time.time()
        self.cache_ttl = 300  # 5 minutes
    
    def clear_expired_cache(self):
        """Clear expired cache entries"""
        current_time = time.time()
        if current_time - self.last_cache_clear > self.cache_ttl:
            self.calculation_cache.clear()
            self.last_cache_clear = current_time
    
    @lru_cache(maxsize=128)
    def cached_atr_calculation(self, high_tuple: tuple, low_tuple: tuple, close_tuple: tuple, period: int = 14):
        """Cached ATR calculation to avoid redundant computations"""
        highs = np.array(high_tuple)
        lows = np.array(low_tuple)
        closes = np.array(close_tuple)
        
        tr1 = highs - lows
        tr2 = np.abs(highs - np.roll(closes, 1))
        tr3 = np.abs(lows - np.roll(closes, 1))
        
        tr = np.maximum(tr1, np.maximum(tr2, tr3))
        atr = np.mean(tr[-period:]) if len(tr) >= period else np.mean(tr)
        
        return float(atr)
    
    def optimize_dataframe_operations(self, df: pd.DataFrame) -> pd.DataFrame:
        """Optimize pandas DataFrame operations"""
        # Use vectorized operations instead of loops
        if 'close' in df.columns and 'open' in df.columns:
            df['price_change'] = df['close'] - df['open']
            df['price_change_pct'] = df['price_change'] / df['open'] * 100
        
        # Use efficient data types
        for col in df.select_dtypes(include=[np.float64]).columns:
            df[col] = pd.to_numeric(df[col], downcast='float')
        
        for col in df.select_dtypes(include=[np.int64]).columns:
            df[col] = pd.to_numeric(df[col], downcast='integer')
        
        return df
    
    def batch_indicator_calculation(self, df: pd.DataFrame, indicators: list) -> pd.DataFrame:
        """Calculate multiple indicators in batch to reduce overhead"""
        result_df = df.copy()
        
        if 'rsi' in indicators and 'close' in df.columns:
            result_df['rsi14'] = self.calculate_rsi_vectorized(df['close'], 14)
        
        if 'ema' in indicators and 'close' in df.columns:
            result_df['ema9'] = df['close'].ewm(span=9, adjust=False).mean()
            result_df['ema13'] = df['close'].ewm(span=13, adjust=False).mean()
        
        if 'sma' in indicators and 'close' in df.columns:
            result_df['sma20'] = df['close'].rolling(window=20).mean()
        
        return result_df
    
    def calculate_rsi_vectorized(self, prices: pd.Series, period: int = 14) -> pd.Series:
        """Vectorized RSI calculation for better performance"""
        delta = prices.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        return rsi
    
    def memory_efficient_slice(self, df: pd.DataFrame, lookback: int = 100) -> pd.DataFrame:
        """Keep only necessary data to reduce memory usage"""
        if len(df) > lookback:
            return df.tail(lookback).copy()
        return df
    
    def optimize_option_selection(self, option_chain: pd.DataFrame, spot_price: float, 
                                 side: str, strike_diff: float) -> Optional[Tuple[str, float]]:
        """Optimized option selection with pre-filtering"""
        # Pre-filter by option type
        filtered_chain = option_chain[option_chain['option_type'] == side].copy()
        
        if filtered_chain.empty:
            return None, None
        
        # Calculate target strike
        atm_strike = round(spot_price / strike_diff) * strike_diff
        target_strike = atm_strike - strike_diff if side == 'CE' else atm_strike + strike_diff
        
        # Vectorized distance calculation
        filtered_chain['strike_distance'] = np.abs(filtered_chain['strike_price'] - target_strike)
        
        # Get closest strike
        best_option = filtered_chain.loc[filtered_chain['strike_distance'].idxmin()]
        
        return best_option['symbol'], best_option['strike_price']

class DataCache:
    """Simple data cache for frequently accessed data"""
    
    def __init__(self, ttl: int = 60):
        self.cache = {}
        self.ttl = ttl
    
    def get(self, key: str) -> Any:
        """Get cached value if not expired"""
        if key in self.cache:
            value, timestamp = self.cache[key]
            if time.time() - timestamp < self.ttl:
                return value
            else:
                del self.cache[key]
        return None
    
    def set(self, key: str, value: Any):
        """Set cached value with timestamp"""
        self.cache[key] = (value, time.time())
    
    def clear(self):
        """Clear all cached values"""
        self.cache.clear()

# Global performance optimizer instance
perf_optimizer = PerformanceOptimizer()
data_cache = DataCache()

def performance_monitor(func):
    """Decorator to monitor function performance"""
    def wrapper(*args, **kwargs):
        start_time = time.time()
        result = func(*args, **kwargs)
        execution_time = time.time() - start_time
        
        if execution_time > 1.0:  # Log slow operations
            print(f"[PERFORMANCE] {func.__name__} took {execution_time:.2f}s")
        
        return result
    return wrapper