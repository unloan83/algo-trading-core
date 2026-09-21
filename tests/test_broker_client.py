import unittest
from unittest.mock import patch

from data.broker_client import UnifiedBrokerClient


class TestUnifiedBrokerClient(unittest.TestCase):
    @patch.object(
        UnifiedBrokerClient,
        "resolve_instrument_key",
        return_value="NSE_INDEX|Nifty 50",
    )
    @patch.object(UnifiedBrokerClient, "_get_json")
    def test_intraday_history_merges_current_session(
        self,
        mock_get_json,
        _mock_resolve,
    ):
        mock_get_json.side_effect = [
            {
                "data": {
                    "candles": [
                        ["2026-09-18T15:25:00+05:30", 100, 102, 99, 101, 10],
                        ["2026-09-21T09:20:00+05:30", 101, 103, 100, 102, 20],
                    ]
                }
            },
            {
                "data": {
                    "candles": [
                        ["2026-09-21T09:20:00+05:30", 102, 104, 101, 103, 30],
                        ["2026-09-21T09:25:00+05:30", 103, 105, 102, 104, 40],
                    ]
                }
            },
        ]

        result = UnifiedBrokerClient().get_intraday_history("NIFTY 50")

        self.assertEqual(len(result), 3)
        self.assertEqual(str(result.iloc[-1]["timestamp"]), "2026-09-21 09:25:00")
        overlap = result[result["timestamp"].astype(str) == "2026-09-21 09:20:00"]
        self.assertEqual(float(overlap.iloc[0]["close"]), 103.0)
        self.assertIn("/historical-candle/intraday/", mock_get_json.call_args_list[1].args[0])


if __name__ == "__main__":
    unittest.main()
