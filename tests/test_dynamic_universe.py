import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import data.universe_selector as selector


class FakeBroker:
    def resolve_instrument_key(self, symbol):
        return f"NSE_EQ|{symbol}"

    def get_historical_data(self, symbol, days=25):
        idx = int(symbol.replace("SYM", ""))
        return pd.DataFrame(
            {
                "close": [100.0] * days,
                "volume": [100_000 + idx * 10_000] * days,
            }
        )


class TestDynamicUniverse(unittest.TestCase):
    def test_builds_and_loads_ranked_universe(self):
        constituents = [f"SYM{i}" for i in range(200)]
        cfg = {
            "mode": "dynamic_nifty200_top100",
            "top_n": 3,
            "adtv_lookback_days": 20,
            "min_adtv_cr": 0,
            "constituent_cache_max_age_days": 30,
            "source_url": "https://example.invalid/nifty200.csv",
        }

        with tempfile.TemporaryDirectory() as tmp:
            active_cache = Path(tmp) / "active_universe.json"
            with patch.object(
                selector,
                "ACTIVE_UNIVERSE_CACHE",
                active_cache,
            ), patch.object(
                selector,
                "fetch_nifty200_constituents",
                return_value=(constituents, "test"),
            ):
                symbols, meta = selector.build_and_cache_dynamic_universe(
                    FakeBroker(),
                    cfg,
                )
                self.assertEqual(symbols, ["SYM199", "SYM198", "SYM197"])
                self.assertEqual(meta["top_n"], 3)
                self.assertEqual(
                    selector.load_active_universe(cfg),
                    symbols,
                )

    def test_missing_active_cache_fails_closed(self):
        cfg = {"top_n": 100}
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(
                selector,
                "ACTIVE_UNIVERSE_CACHE",
                Path(tmp) / "missing.json",
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "ACTIVE_UNIVERSE_CACHE_MISSING",
                ):
                    selector.load_active_universe(cfg)


if __name__ == "__main__":
    unittest.main()
