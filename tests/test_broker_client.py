import json
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from data.broker_client import UnifiedBrokerClient


class TestUnifiedBrokerClient(unittest.TestCase):
    def test_halted_requires_placeholder_only_symbol(self):
        symbols = [
            "ADANIENT",
            "ONLYPLACEHOLDER",
            *(f"ACTIVE{i}" for i in range(8)),
        ]
        suspended = [
            {
                "trading_symbol": "ADANIENT",
                "instrument_type": "BE",
                "lot_size": 1,
                "freeze_quantity": 100000,
            },
            {
                "trading_symbol": "ADANIENT",
                "instrument_type": "BL",
                "lot_size": 999999999,
                "freeze_quantity": 999999999,
            },
            {
                "trading_symbol": "ONLYPLACEHOLDER",
                "instrument_type": "TL",
                "lot_size": 999999999,
                "freeze_quantity": 999999999,
            },
        ]
        response = MagicMock()
        response.content = b"mock suspended-instrument response"
        client = UnifiedBrokerClient()

        with tempfile.TemporaryDirectory() as cache_dir:
            client.cache_dir = cache_dir
            with patch.object(client.session, "get", return_value=response), patch.object(
                client,
                "_decode_gzip_json",
                return_value=suspended,
            ), patch.object(client, "resolve_instrument", return_value={}):
                halted, corp_actions = client.get_trading_halts_and_corp_actions(symbols)

        self.assertNotIn("ADANIENT", halted)
        self.assertEqual(halted, ["ONLYPLACEHOLDER"])
        self.assertEqual(corp_actions, [])

    @patch("data.broker_client.time.sleep")
    def test_implausible_suspended_instrument_ratio_retries_once(self, mock_sleep):
        symbols = [f"SYMBOL{i}" for i in range(20)]
        first_payload = [
            {"trading_symbol": symbol, "instrument_type": "BL"}
            for symbol in symbols
        ]
        second_payload = [
            {"trading_symbol": symbols[0], "instrument_type": "BL"}
        ]
        response = MagicMock()
        response.content = b"mock suspended-instrument response"
        client = UnifiedBrokerClient()

        with tempfile.TemporaryDirectory() as cache_dir:
            client.cache_dir = cache_dir
            with open(f"{cache_dir}/upstox_suspended.json", "w", encoding="utf-8") as cache_file:
                json.dump(first_payload, cache_file)
            with patch.object(client.session, "get", return_value=response) as mock_get, patch.object(
                client,
                "_decode_gzip_json",
                return_value=second_payload,
            ), patch.object(client, "resolve_instrument", return_value={}):
                halted, corp_actions = client.get_trading_halts_and_corp_actions(symbols)

        self.assertEqual(halted, [symbols[0]])
        self.assertEqual(corp_actions, [])
        self.assertEqual(mock_get.call_count, 1)
        mock_sleep.assert_called_once_with(30)

    @patch("data.broker_client.time.sleep")
    def test_implausible_suspended_instrument_ratio_raises(self, mock_sleep):
        symbols = [f"SYMBOL{i}" for i in range(20)]
        suspended = [
            {"trading_symbol": symbol, "instrument_type": "BL"}
            for symbol in symbols[:4]
        ]
        response = MagicMock()
        response.content = b"mock suspended-instrument response"
        client = UnifiedBrokerClient()

        with tempfile.TemporaryDirectory() as cache_dir:
            client.cache_dir = cache_dir
            with patch.object(client.session, "get", return_value=response) as mock_get, patch.object(
                client,
                "_decode_gzip_json",
                return_value=suspended,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "^SUSPENDED_INSTRUMENT_DATA_IMPLAUSIBLE$",
                ):
                    client.get_trading_halts_and_corp_actions(symbols)
            self.assertEqual(mock_get.call_count, 2)
            mock_sleep.assert_called_once_with(30)

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
