#!/usr/bin/env python3
from datetime import date, datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd

from data.broker_client import UPSTOX_BASE, UnifiedBrokerClient


EARLIEST_UPSTOX_DAILY_DATE = date(2000, 1, 1)
SYMBOL = "NIFTY 50"


def _ten_year_windows(start_date, end_date):
    window_end = end_date
    while window_end >= start_date:
        try:
            window_start = window_end.replace(year=window_end.year - 10)
        except ValueError:
            window_start = window_end.replace(
                year=window_end.year - 10,
                day=28,
            )
        window_start = max(start_date, window_start)
        yield window_start, window_end
        window_end = window_start - timedelta(days=1)


def main():
    broker = UnifiedBrokerClient(paper_mode=True)
    instrument_key = quote(broker.resolve_instrument_key(SYMBOL), safe="")
    end_date = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    frames = []
    for start_date, window_end in _ten_year_windows(
        EARLIEST_UPSTOX_DAILY_DATE,
        end_date,
    ):
        url = (
            f"{UPSTOX_BASE}/v3/historical-candle/{instrument_key}/days/1/"
            f"{window_end.isoformat()}/{start_date.isoformat()}"
        )
        frames.append(broker._candles_to_df(broker._get_json(url)))

    candles = (
        pd.concat(frames, ignore_index=True)
        .sort_values("timestamp")
        .drop_duplicates("timestamp")
        .reset_index(drop=True)
    )
    if candles.empty:
        raise RuntimeError("UPSTOX_HISTORICAL_DATA_UNAVAILABLE")

    print(f"symbol={SYMBOL}")
    print(f"earliest_date={candles.iloc[0]['timestamp'].date().isoformat()}")
    print(f"latest_date={candles.iloc[-1]['timestamp'].date().isoformat()}")
    print(f"candle_count={len(candles)}")


if __name__ == "__main__":
    main()
