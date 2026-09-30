import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

from core.models import RegimeType, Side, Signal
from core.risk_governor import RiskGovernor
from data.broker_client import UnifiedBrokerClient


class TestInv7PositiveControls(unittest.TestCase):
    @staticmethod
    def _signal(symbol: str, now: datetime) -> Signal:
        return Signal(
            symbol=symbol,
            side=Side.BUY,
            entry_price=100.0,
            stop_price=95.0,
            target_price=110.0,
            model_name="trend_pullback",
            regime=RegimeType.TREND_UP,
            rationale="INV7 regression control",
            timestamp=now,
        )

    @staticmethod
    def _risk_result(symbol: str, halted_symbols: list[str]):
        now = datetime(2026, 9, 21, 10, 0, 0)
        return RiskGovernor().validate_signal_against_invariants(
            signal=TestInv7PositiveControls._signal(symbol, now),
            equity_now=100_000.0,
            available_cash=100_000.0,
            open_positions=[],
            daily_pnl=0.0,
            weekly_pnl=0.0,
            monthly_pnl=0.0,
            consecutive_losses=0,
            last_loss_time=None,
            halted_symbols=halted_symbols,
            corporate_action_symbols=[],
            broker_account_ok=True,
            now=now,
        )

    def test_genuine_nse_cash_equity_halt_blocks_inv7(self):
        halted = UnifiedBrokerClient._halted_symbols_from_suspended_feed(
            [
                {
                    "trading_symbol": "REALHALT",
                    "segment": "NSE_EQ",
                    "instrument_type": "EQ",
                }
            ],
            {"REALHALT"},
        )

        result = self._risk_result("REALHALT", halted)

        self.assertEqual(halted, ["REALHALT"])
        self.assertFalse(result.passed)
        self.assertEqual(result.reason_code, "INV7_SYMBOL_TRADING_HALTED")

    def test_placeholder_only_rows_do_not_block_inv7(self):
        halted = UnifiedBrokerClient._halted_symbols_from_suspended_feed(
            [
                {
                    "trading_symbol": "PLACEHOLDER",
                    "segment": "NSE_EQ",
                    "instrument_type": instrument_type,
                }
                for instrument_type in ("BL", "TL", "DL")
            ],
            {"PLACEHOLDER"},
        )

        result = self._risk_result("PLACEHOLDER", halted)

        self.assertEqual(halted, [])
        self.assertNotEqual(result.reason_code, "INV7_SYMBOL_TRADING_HALTED")
        self.assertTrue(result.passed)

    def test_missing_suspended_feed_preserves_current_empty_halt_behavior(self):
        client = UnifiedBrokerClient()

        with tempfile.TemporaryDirectory() as cache_dir:
            client.cache_dir = cache_dir
            with patch.object(
                client.session,
                "get",
                side_effect=RuntimeError("suspended feed unavailable"),
            ), patch.object(client, "resolve_instrument", return_value={}):
                halted, corporate_actions = client.get_trading_halts_and_corp_actions(
                    ["RELIANCE"]
                )

        self.assertEqual(halted, [])
        self.assertEqual(corporate_actions, [])


if __name__ == "__main__":
    unittest.main()
