import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from core.models import Position, RegimeType, Side, Signal
from data.db_models import DatabaseManager
from scripts.algo_health_agent import HealthCheckResult
from telegram_bot.command_listener import (
    StatusReporter,
    TelegramCommandListener,
)
from telegram_bot.telegram_client import TelegramClient


class TestTelegramCommandListener(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.telegram = TelegramClient(
            bot_token="mock_token",
            chat_id="42",
            allowed_user_id="42",
            callback_store=self.db,
        )
        self.telegram.send_message = MagicMock(
            return_value=(True, 1, "Message sent successfully")
        )

    def test_unauthorized_status_message_sends_no_reply(self):
        reporter = MagicMock()
        listener = TelegramCommandListener(self.db, self.telegram, reporter)
        update = {
            "update_id": 100,
            "message": {
                "from": {"id": 42},
                "chat": {"id": 999},
                "text": "/status",
            },
        }

        listener.handle_update(update)

        reporter.render.assert_not_called()
        self.telegram.send_message.assert_not_called()

    def test_authorized_status_reply_contains_expected_fields(self):
        now = datetime(2026, 9, 21, 10, 0, 0)
        self.db.get_first_preflight_time.return_value = datetime(2026, 9, 1, 8, 30)
        self.db.get_completed_trades_count.return_value = 12
        self.db.get_last_preflight_time.return_value = datetime(2026, 9, 21, 8, 30)
        self.db.get_latest_regime.return_value = {
            "regime": "TREND_UP",
            "rationale": "test",
            "timestamp": now.isoformat(),
        }
        self.db.get_open_positions.return_value = [
            Position(
                position_id="pos_1",
                symbol="INFY",
                side=Side.BUY,
                qty=10,
                entry_price=100.0,
                current_price=105.0,
                stop_price=95.0,
                target_price=110.0,
                opened_at=now,
            )
        ]

        health_agent = MagicMock()
        health_agent.today_summary.return_value = {
            "signals_evaluated_today": 7,
            "blocked_signals_today": 2,
            "orders_today": 1,
        }
        health_agent.check_system_blockers.return_value = HealthCheckResult(
            "SYSTEM_BLOCKERS",
            "OK",
            "Risk governor circuits clear",
            {
                "daily_circuit_used_pct": 0.25,
                "weekly_circuit_used_pct": 0.50,
                "monthly_circuit_used_pct": 0.75,
            },
        )
        health_agent.governor.daily_circuit_pct = 1.5
        health_agent.governor.weekly_circuit_pct = 3.0
        health_agent.governor.monthly_circuit_pct = 4.5

        reporter = StatusReporter(
            db=self.db,
            health_agent=health_agent,
            now_fn=lambda: now,
            marker_reader=lambda _service: {
                "timestamp": datetime(2026, 9, 21, 9, 45)
            },
        )
        listener = TelegramCommandListener(self.db, self.telegram, reporter)
        update = {
            "update_id": 101,
            "message": {
                "from": {"id": 42},
                "chat": {"id": 42},
                "text": "/status",
            },
        }

        with patch.dict(os.environ, {"TRADING_MODE": "PAPER"}):
            listener.handle_update(update)

        self.telegram.send_message.assert_called_once()
        reply = self.telegram.send_message.call_args.args[0]
        self.assertIn("Trading mode:</b> PAPER", reply)
        self.assertIn("Live order path:</b> DISABLED", reply)
        self.assertIn("gate_cleared: false", reply)
        self.assertIn("INFY: qty=10, entry=₹100.00, MTM=₹50.00", reply)
        for expected in (
            "Trading mode:",
            "Live order path:",
            "Days elapsed:",
            "Trades completed:",
            "gate_cleared:",
            "Regime:",
            "Signals evaluated:",
            "Signals blocked:",
            "Orders placed:",
            "INFY: qty=10",
            "Daily:",
            "Weekly:",
            "Monthly:",
            "Successful preflight:",
            "Health check:",
        ):
            self.assertIn(expected, reply)

    def test_callback_is_routed_without_second_getupdates_consumer(self):
        reporter = MagicMock()
        listener = TelegramCommandListener(self.db, self.telegram, reporter)
        update = {
            "update_id": 102,
            "callback_query": {
                "from": {"id": 42},
                "message": {"chat": {"id": 42}},
                "data": "APPROVE:signal_123",
            },
        }

        listener.handle_update(update)

        self.db.enqueue_telegram_callback.assert_called_once_with(
            update_id=102,
            action="APPROVE",
            signal_id="signal_123",
        )

        self.db.pop_telegram_callback.return_value = ("APPROVE", "signal_123")
        with patch("telegram_bot.telegram_client.requests.get") as mock_get:
            callback = self.telegram.poll_callback_query(
                timeout=0,
                signal_id="signal_123",
            )
        self.assertEqual(callback, ("APPROVE", "signal_123"))
        self.db.pop_telegram_callback.assert_called_once_with("signal_123")
        mock_get.assert_not_called()


class TestTelegramCallbackInbox(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self.db_path = handle.name
        self.db = DatabaseManager(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def test_callback_pop_is_scoped_to_signal_id(self):
        self.db.enqueue_telegram_callback(1, "APPROVE", "signal_a")
        self.db.enqueue_telegram_callback(2, "SKIP", "signal_b")

        self.assertEqual(
            self.db.pop_telegram_callback("signal_b"),
            ("SKIP", "signal_b"),
        )
        self.assertEqual(
            self.db.pop_telegram_callback("signal_a"),
            ("APPROVE", "signal_a"),
        )

    def test_today_summary_counts_recorded_signal_evaluations(self):
        now = datetime(2026, 9, 21, 10, 0, 0)
        signal = Signal(
            symbol="INFY",
            side=Side.BUY,
            entry_price=100.0,
            stop_price=95.0,
            target_price=110.0,
            model_name="breakout",
            regime=RegimeType.TREND_UP,
            rationale="test",
            timestamp=now,
        )

        self.db.record_signal_evaluations([signal], evaluated_at=now)

        summary = self.db.get_today_summary("2026-09-21")
        self.assertEqual(summary["signals_evaluated_today"], 1)


if __name__ == "__main__":
    unittest.main()
