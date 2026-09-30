#!/usr/bin/env python3
"""
Parallel Research Dataset Builder for research/edge branch.
Caches up to 12 months of 5-minute and daily bars for the current universe.
Strictly enforces single attempt per symbol and caches individual symbols incrementally.
"""
import os
import sys
import time
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from urllib.parse import quote
import pandas as pd
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env", override=False)

from data.broker_client import UnifiedBrokerClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("build_research_dataset")

CACHE_DIR = PROJECT_ROOT / ".cache" / "research_edge_bars"
SYMBOLS_CACHE_DIR = CACHE_DIR / "symbols"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
SYMBOLS_CACHE_DIR.mkdir(parents=True, exist_ok=True)

def load_universe_symbols() -> List[str]:
    cache_path = PROJECT_ROOT / ".cache" / "active_universe.json"
    if not cache_path.exists():
        raise RuntimeError("active_universe.json not found")
    with open(cache_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    symbols = [row["symbol"] for row in data["selected"]]
    return symbols

def fetch_daily_bars_single_attempt(client: UnifiedBrokerClient, symbol: str, days: int = 365) -> pd.DataFrame:
    df = client.get_historical_data(symbol, days=days)
    return df

def fetch_5m_bars_single_attempt(client: UnifiedBrokerClient, symbol: str, total_days: int = 365) -> pd.DataFrame:
    key = quote(client.resolve_instrument_key(symbol), safe="")
    end_date = datetime.now().date()
    start_limit = end_date - timedelta(days=total_days)

    curr_end = end_date
    all_dfs = []
    while curr_end > start_limit:
        curr_start = max(curr_end - timedelta(days=25), start_limit)
        url = f"https://api.upstox.com/v3/historical-candle/{key}/minutes/5/{curr_end.isoformat()}/{curr_start.isoformat()}"
        data = client._get_json(url)
        df = client._candles_to_df(data)
        if not df.empty:
            all_dfs.append(df)
        curr_end = curr_start - timedelta(days=1)
        time.sleep(0.04)

    if not all_dfs:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    full_df = (
        pd.concat(all_dfs, ignore_index=True)
        .drop_duplicates("timestamp")
        .sort_values("timestamp")
        .reset_index(drop=True)
    )
    return full_df

def process_symbol(symbol: str) -> Tuple[str, Optional[pd.DataFrame], Optional[pd.DataFrame], Optional[str]]:
    sym_clean = symbol.replace(" ", "_")
    daily_cache = SYMBOLS_CACHE_DIR / f"{sym_clean}_daily.pkl"
    intraday_cache = SYMBOLS_CACHE_DIR / f"{sym_clean}_5m.pkl"

    if daily_cache.exists() and intraday_cache.exists():
        try:
            df_d = pd.read_pickle(daily_cache)
            df_5m = pd.read_pickle(intraday_cache)
            if not df_d.empty and not df_5m.empty:
                return symbol, df_d, df_5m, None
        except Exception:
            pass

    client = UnifiedBrokerClient(paper_mode=True)
    try:
        df_daily = fetch_daily_bars_single_attempt(client, symbol, days=365)
        df_5m = fetch_5m_bars_single_attempt(client, symbol, total_days=365)

        if df_daily.empty or df_5m.empty:
            return symbol, None, None, "EMPTY_DATA"

        pd.to_pickle(df_daily, daily_cache)
        pd.to_pickle(df_5m, intraday_cache)

        return symbol, df_daily, df_5m, None
    except Exception as exc:
        return symbol, None, None, f"{type(exc).__name__}:{exc}"

def main():
    start_time = time.time()
    log.info("Starting Parallel Research Dataset Build (branch: research/edge)")
    sys.stdout.flush()

    client = UnifiedBrokerClient(paper_mode=True)
    ok, msg = client.validate_readonly_access()
    if not ok:
        raise RuntimeError(f"Broker client validation failed: {msg}")

    client._load_instruments()
    log.info("Instruments master pre-warmed.")
    sys.stdout.flush()

    universe_symbols = load_universe_symbols()
    all_targets = ["NIFTY 50"] + universe_symbols
    log.info(f"Loaded {len(all_targets)} total target symbols.")
    sys.stdout.flush()

    daily_dict: Dict[str, pd.DataFrame] = {}
    intraday_dict: Dict[str, pd.DataFrame] = {}
    failed_symbols = []

    max_workers = 4
    log.info(f"Launching {max_workers} worker threads...")
    sys.stdout.flush()

    completed_count = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(process_symbol, sym): sym for sym in all_targets}
        for future in as_completed(future_map):
            sym = future_map[future]
            completed_count += 1
            symbol, df_d, df_5m, error = future.result()
            if error:
                log.warning(f"[{completed_count}/{len(all_targets)}] {symbol} FAILED: {error}")
                failed_symbols.append((symbol, error))
            else:
                daily_dict[symbol] = df_d
                intraday_dict[symbol] = df_5m
                log.info(f"[{completed_count}/{len(all_targets)}] {symbol} DONE: Daily {len(df_d)} rows, 5m {len(df_5m)} rows")
            sys.stdout.flush()

    log.info("Saving combined cached datasets to disk...")
    sys.stdout.flush()
    pd.to_pickle(daily_dict, CACHE_DIR / "daily_bars.pkl")
    pd.to_pickle(intraday_dict, CACHE_DIR / "intraday_5m_bars.pkl")

    meta = {
        "built_at": datetime.now().isoformat(),
        "total_targets": len(all_targets),
        "successful_daily": len(daily_dict),
        "successful_5m": len(intraday_dict),
        "failed_symbols": failed_symbols,
        "nifty_daily_range": [str(daily_dict["NIFTY 50"]["timestamp"].min()), str(daily_dict["NIFTY 50"]["timestamp"].max())] if "NIFTY 50" in daily_dict else [],
        "nifty_5m_range": [str(intraday_dict["NIFTY 50"]["timestamp"].min()), str(intraday_dict["NIFTY 50"]["timestamp"].max())] if "NIFTY 50" in intraday_dict else [],
        "survivorship_bias_label": "SURVIVORSHIP_BIAS_PRESENT: Universe consists of top 100 ADTV liquid Nifty 200 constituents selected as of September 2026 active universe snapshot rather than historical point-in-time universe constituents for each past session."
    }

    meta_file = CACHE_DIR / "dataset_metadata.json"
    meta_file.write_text(json.dumps(meta, indent=2))

    elapsed = time.time() - start_time
    log.info(f"Dataset build completed in {elapsed:.2f} seconds.")
    sys.stdout.flush()

if __name__ == "__main__":
    main()
