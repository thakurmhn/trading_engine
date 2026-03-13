"""Historical data fetcher — broker-agnostic.

Wraps broker API history calls into a standard interface.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pandas as pd
import pytz

IST = pytz.timezone("Asia/Kolkata")


def fetch_historical_candles(
    broker,
    symbol: str,
    resolution: str = "15",
    days: int = 5,
    include_today: bool = False,
) -> pd.DataFrame:
    """Fetch historical candles via broker adapter.

    Parameters
    ----------
    broker : object
        Broker client with a .history(data=dict) method (e.g., Fyers).
    symbol : str
        Instrument symbol.
    resolution : str
        Candle resolution ("3", "5", "15", "60", "D").
    days : int
        Number of days to look back.
    include_today : bool
        Whether to include today's partial data.

    Returns
    -------
    pd.DataFrame
        Normalised OHLCV DataFrame.
    """
    today = datetime.now(IST).date()
    start_date = today - timedelta(days=days)
    range_to = (today + timedelta(days=1)) if include_today else today

    hist_req = {
        "symbol": symbol,
        "resolution": resolution,
        "date_format": "1",
        "range_from": start_date.strftime("%Y-%m-%d"),
        "range_to": range_to.strftime("%Y-%m-%d"),
        "cont_flag": "1",
    }

    try:
        response = broker.history(data=hist_req)
        candles = response.get("candles", [])
        if not candles:
            logging.warning(f"[FETCH] No candles for {symbol} res={resolution}")
            return pd.DataFrame()

        hist_data = pd.DataFrame(candles, columns=["date", "open", "high", "low", "close", "volume"])
        hist_data["date"] = (
            pd.to_datetime(hist_data["date"], unit="s")
            .dt.tz_localize("UTC")
            .dt.tz_convert(IST)
        )

        if not include_today:
            hist_data = hist_data[hist_data["date"].dt.date < today]
        else:
            mins = int(resolution) if str(resolution).isdigit() else 3
            now_ist = datetime.now(IST)
            slot_min = (now_ist.minute // mins) * mins
            current_slot_start = now_ist.replace(minute=slot_min, second=0, microsecond=0)
            if current_slot_start.tzinfo is None:
                current_slot_start = IST.localize(current_slot_start)
            hist_data = hist_data[hist_data["date"] < current_slot_start]

        hist_data["trade_date"] = hist_data["date"].dt.strftime("%Y-%m-%d")
        hist_data["ist_slot"] = hist_data["date"].dt.strftime("%H:%M:%S")
        hist_data["symbol"] = symbol
        hist_data["time"] = hist_data["trade_date"] + " " + hist_data["ist_slot"]

        logging.info(
            f"[FETCH] {symbol} res={resolution} include_today={include_today} "
            f"rows={len(hist_data)} "
            f"last={hist_data.iloc[-1]['date'] if not hist_data.empty else 'none'}"
        )
        return hist_data.reset_index(drop=True)

    except Exception as e:
        logging.error(f"[FETCH ERROR] {symbol} res={resolution}: {e}", exc_info=True)
        return pd.DataFrame()
