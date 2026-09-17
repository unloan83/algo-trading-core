#!/usr/bin/env python3
import os
import sys
import pandas as pd
from datetime import datetime

# Add project root to PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.regime_filter import evaluate_regime
from data.db_models import DatabaseManager

def main():
    print(f"[{datetime.now().isoformat()}] Running daily regime filter check...")
    db = DatabaseManager()

    # Generate or fetch Nifty OHLC sample data for regime calculation
    dates = pd.date_range(end=datetime.now(), periods=100, freq='D')
    nifty_data = []
    price = 24000.0
    for d in dates:
        price += (hash(str(d)) % 100 - 45) # synthetic realistic daily movement
        high = price + 100
        low = price - 100
        close = price + 20
        nifty_data.append({
            'timestamp': d,
            'open': price,
            'high': high,
            'low': low,
            'close': close,
            'volume': 1000000
        })
    df_nifty = pd.DataFrame(nifty_data)

    regime, rationale = evaluate_regime(df_nifty)
    print(f"Regime evaluated: {regime.value} - Rationale: {rationale}")
    db.record_regime(regime.value, rationale)
    print("Regime logged to database successfully.")

if __name__ == "__main__":
    main()
