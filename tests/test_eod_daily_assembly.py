import unittest
from datetime import datetime
from unittest.mock import patch

import pandas as pd
import requests

from data.broker_client import UnifiedBrokerClient


def daily_history(include_session=False):
    dates = pd.date_range("2026-09-28", periods=2, freq="D")
    frame = pd.DataFrame(
        {
            "timestamp": dates,
            "open": [100.0, 101.0],
            "high": [102.0, 103.0],
            "low": [99.0, 100.0],
            "close": [101.0, 102.0],
            "volume": [1000, 1100],
        }
    )
    if include_session:
        frame.loc[len(frame)] = [pd.Timestamp("2026-09-30"), 1, 2, 1, 2, 1]
    return frame


def complete_intraday(volume=0):
    timestamps = pd.date_range(
        "2026-09-30 09:15:00",
        "2026-09-30 15:25:00",
        freq="5min",
    )
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [100.0] * len(timestamps),
            "high": [103.0] * len(timestamps),
            "low": [99.0] * len(timestamps),
            "close": [102.0] * len(timestamps),
            "volume": [volume] * len(timestamps),
        }
    )


class TestEODDailyAssembly(unittest.TestCase):
    def setUp(self):
        self.client = UnifiedBrokerClient()
        self.now = datetime(2026, 9, 30, 15, 40)

    def run_result(self, history, intraday, **kwargs):
        with patch.object(
            self.client, "get_historical_data", return_value=history
        ), patch.object(
            self.client, "get_current_intraday_session", return_value=intraday
        ), patch.object(
            self.client,
            "resolve_instrument",
            return_value={"instrument_type": "INDEX"},
        ):
            return self.client.get_eod_historical_data(
                "NIFTY 50",
                days=210,
                now_ist=kwargs.get("now", self.now),
                is_trading_session=kwargs.get("is_trading_session", True),
            )

    def test_normal_merge_assembles_complete_session(self):
        result = self.run_result(daily_history(), complete_intraday())

        self.assertFalse(result.input_asof_stale)
        self.assertTrue(result.assembled_current_session)
        self.assertEqual(result.candles.iloc[-1]["timestamp"].date(), self.now.date())
        self.assertEqual(float(result.candles.iloc[-1]["open"]), 100.0)
        self.assertEqual(float(result.candles.iloc[-1]["close"]), 102.0)

    def test_pre_close_retains_legacy_and_marks_stale(self):
        history = daily_history()
        result = self.run_result(
            history,
            complete_intraday(),
            now=datetime(2026, 9, 30, 15, 34),
        )

        self.assertTrue(result.input_asof_stale)
        self.assertEqual(result.reason, "BEFORE_EOD_ASSEMBLY_WINDOW")
        pd.testing.assert_frame_equal(result.candles, history)

    def test_weekend_or_holiday_retains_legacy_and_marks_stale(self):
        history = daily_history()
        result = self.run_result(
            history,
            complete_intraday(),
            is_trading_session=False,
        )

        self.assertTrue(result.input_asof_stale)
        self.assertEqual(result.reason, "NOT_NSE_TRADING_SESSION")
        pd.testing.assert_frame_equal(result.candles, history)

    def test_duplicate_current_session_is_idempotently_replaced(self):
        result = self.run_result(daily_history(include_session=True), complete_intraday())
        session_rows = result.candles[
            pd.to_datetime(result.candles["timestamp"]).dt.date == self.now.date()
        ]

        self.assertEqual(len(session_rows), 1)
        self.assertEqual(float(session_rows.iloc[0]["close"]), 102.0)

    def test_intraday_endpoint_failure_retains_legacy_and_marks_stale(self):
        history = daily_history()
        with patch.object(
            self.client, "get_historical_data", return_value=history
        ), patch.object(
            self.client,
            "get_current_intraday_session",
            side_effect=requests.Timeout("bounded timeout"),
        ):
            result = self.client.get_eod_historical_data(
                "NIFTY 50",
                days=210,
                now_ist=self.now,
                is_trading_session=True,
            )

        self.assertTrue(result.input_asof_stale)
        self.assertEqual(
            result.reason,
            "INTRADAY_ENDPOINT_OR_ASSEMBLY_ERROR:Timeout",
        )
        pd.testing.assert_frame_equal(result.candles, history)


if __name__ == "__main__":
    unittest.main()
