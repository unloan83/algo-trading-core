import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import scripts.algo_health_agent as health_module
from scripts.algo_health_agent import AlgoHealthAgent, HealthCheckResult


class TestAlgoHealthAgent(unittest.TestCase):
    def setUp(self):
        self.mock_db = MagicMock()
        self.mock_broker = MagicMock()
        self.agent = AlgoHealthAgent(db=self.mock_db, broker=self.mock_broker)

    @patch.object(AlgoHealthAgent, "_active_symbols", return_value=["INFY"])
    def test_check_model_health(self, _mock_symbols):
        res = self.agent.check_model_health()
        self.assertEqual(res.domain, "MODEL_HEALTH")
        self.assertEqual(res.status, "OK")
        self.assertIn("breakout", res.details)

    @patch("scripts.algo_health_agent.CircuitTracker")
    def test_check_system_blockers_clear(self, mock_tracker_cls):
        mock_tracker = mock_tracker_cls.return_value
        mock_tracker.compute_mark_to_market_pnl.return_value = (0.0, 0.0, 0.0)
        self.mock_db.paper_account.return_value = (30000.0, 30000.0)
        self.mock_db.get_latest_circuit_state.return_value = {"consecutive_losses": 0, "last_loss_time": None}
        self.mock_db.get_open_positions.return_value = []

        res = self.agent.check_system_blockers()
        self.assertEqual(res.domain, "SYSTEM_BLOCKERS")
        self.assertEqual(res.status, "OK")

    @patch("scripts.algo_health_agent.CircuitTracker")
    def test_check_system_blockers_tripped(self, mock_tracker_cls):
        mock_tracker = mock_tracker_cls.return_value
        mock_tracker.compute_mark_to_market_pnl.return_value = (-5000.0, 0.0, 0.0)
        self.mock_db.paper_account.return_value = (30000.0, 30000.0)
        self.mock_db.get_latest_circuit_state.return_value = {"consecutive_losses": 3, "last_loss_time": None}
        self.mock_db.get_open_positions.return_value = []

        res = self.agent.check_system_blockers()
        self.assertEqual(res.domain, "SYSTEM_BLOCKERS")
        self.assertEqual(res.status, "BLOCKER")
        self.assertIn("Daily circuit breached", res.message)

    @patch(
        "scripts.algo_health_agent.now_ist_naive",
        return_value=datetime(2026, 10, 1, 10, 0, 0),
    )
    @patch("scripts.algo_health_agent.CircuitTracker")
    def test_check_system_blockers_reports_cooldown_as_yellow_state(
        self,
        mock_tracker_cls,
        _mock_now,
    ):
        mock_tracker = mock_tracker_cls.return_value
        mock_tracker.compute_mark_to_market_pnl.return_value = (0.0, 0.0, 0.0)
        self.mock_db.paper_account.return_value = (30000.0, 30000.0)
        self.mock_db.get_latest_circuit_state.return_value = {
            "consecutive_losses": 5,
            "last_loss_time": datetime(2026, 9, 30, 15, 15, 1),
        }
        self.mock_db.get_open_positions.return_value = []

        res = self.agent.check_system_blockers()

        self.assertEqual(res.status, "COOLDOWN")
        self.assertEqual(
            res.message,
            "COOLDOWN until 2026-10-01 15:15:01 IST "
            "(losses=5, net-based)",
        )
        self.assertTrue(res.details["net_based"])

    def test_consecutive_failed_scan_services_reads_database_read_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "scan_runs.db"
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE scan_runs (
                        mode TEXT NOT NULL,
                        started_ts_utc TEXT NOT NULL,
                        status TEXT NOT NULL,
                        error_type TEXT
                    )
                    """
                )
                conn.executemany(
                    "INSERT INTO scan_runs VALUES (?, ?, ?, ?)",
                    [
                        ("intraday", "2026-09-30T07:00:00Z", "FAILED", "TimeoutError"),
                        ("intraday", "2026-09-30T06:55:00Z", "FAILED", "TimeoutError"),
                        ("intraday", "2026-09-30T06:50:00Z", "FAILED", "ConnectionError"),
                        ("intraday", "2026-09-30T06:45:00Z", "SUCCESS", None),
                        ("eod", "2026-09-29T10:30:00Z", "FAILED", "ValueError"),
                    ],
                )

            self.mock_db.db_path = str(db_path)
            streaks = self.agent._consecutive_failed_scan_services()

            self.assertEqual(
                streaks,
                {
                    "intraday": {
                        "count": 3,
                        "latest_error_type": "TimeoutError",
                    }
                },
            )

            with sqlite3.connect(
                f"{db_path.resolve().as_uri()}?mode=ro",
                uri=True,
            ) as conn:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0],
                    5,
                )

    @patch(
        "scripts.algo_health_agent.now_ist_naive",
        return_value=datetime(2026, 9, 30, 7, 0, 0),
    )
    def test_three_failed_scan_services_are_notify_only_warning(
        self,
        _mock_now,
    ):
        self.mock_broker.is_nse_trading_day.return_value = True
        streak = {
            "intraday": {
                "count": 3,
                "latest_error_type": "TimeoutError",
            }
        }

        with patch.object(
            self.agent,
            "_consecutive_failed_scan_services",
            return_value=streak,
        ):
            res = self.agent.check_runtime_activity()

        self.assertEqual(res.status, "WARNING")
        self.assertIn("Notify-only", res.message)
        self.assertIn("count=3", res.message)

    def test_check_token_blockages_missing_env(self):
        with patch.dict(os.environ, {}, clear=True):
            res = self.agent.check_token_blockages()
            self.assertEqual(res.domain, "TOKEN_VALIDITY")
            self.assertEqual(res.status, "BLOCKER")

    def test_check_token_blockages_valid(self):
        with patch.dict(os.environ, {"UPSTOX_ANALYTICS_TOKEN": "mock_valid_token"}):
            self.mock_broker.validate_readonly_access.return_value = (True, "Token valid")
            res = self.agent.check_token_blockages()
            self.assertEqual(res.domain, "TOKEN_VALIDITY")
            self.assertEqual(res.status, "OK")

    def test_check_action_decision_gate(self):
        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "mock_token",
            "TELEGRAM_CHAT_ID": "123456",
            "TELEGRAM_ALLOWED_USER_ID": "123456",
        }):
            agent = AlgoHealthAgent(db=self.mock_db, broker=self.mock_broker)
            res = agent.check_action_decision_gate()
            self.assertEqual(res.domain, "ACTION_DECISION_GATE")
            self.assertIn(res.status, ("OK", "WARNING", "BLOCKER"))
            self.assertIn("timeout_seconds", res.details)

    @patch.object(AlgoHealthAgent, "_active_symbols", return_value=["INFY"])
    def test_run_all_checks_returns_eight_domains(self, _mock_symbols):
        with patch.dict(os.environ, {"UPSTOX_ANALYTICS_TOKEN": "mock_token", "PAPER_STARTING_CAPITAL": "30000"}):
            self.mock_broker.validate_readonly_access.return_value = (True, "Token valid")
            self.mock_db.paper_account.return_value = (30000.0, 30000.0)
            results = self.agent.run_all_checks()

            self.assertEqual(len(results), 8)
            domains = [r.domain for r in results]
            expected_domains = [
                "MODEL_HEALTH",
                "SYSTEM_BLOCKERS",
                "DATA_INTEGRITY",
                "TOKEN_VALIDITY",
                "LOGIC_DB",
                "ACTION_DECISION_GATE",
                "RUNTIME_ACTIVITY",
                "OCI_RESOURCE_HEALTH",
            ]
            self.assertEqual(domains, expected_domains)

    @patch("scripts.algo_health_agent.AlgoHealthAgent")
    @patch(
        "sys.argv",
        ["algo_health_agent.py", "--mode", "check"],
    )
    def test_check_mode_healthy_sends_one_heartbeat(
        self,
        mock_agent_cls,
    ):
        agent = mock_agent_cls.return_value

        results = [
            HealthCheckResult(
                "MODEL_HEALTH",
                "OK",
                "model healthy",
            ),
            HealthCheckResult(
                "DATA_INTEGRITY",
                "OK",
                "data healthy",
            ),
            HealthCheckResult(
                "RUNTIME_ACTIVITY",
                "OK",
                "runtime healthy",
            ),
        ]

        agent.run_all_checks.return_value = results
        agent.send_health_heartbeat.return_value = True

        mode, exit_code = health_module.main()

        self.assertEqual(mode, "check")
        self.assertEqual(exit_code, 0)

        agent.send_health_heartbeat.assert_called_once_with(results)
        agent.send_issue_alert_if_any.assert_not_called()

    @patch("scripts.algo_health_agent.AlgoHealthAgent")
    @patch(
        "sys.argv",
        ["algo_health_agent.py", "--mode", "check"],
    )
    def test_check_mode_warning_sends_alert_not_heartbeat(
        self,
        mock_agent_cls,
    ):
        agent = mock_agent_cls.return_value

        results = [
            HealthCheckResult(
                "OCI_RESOURCE_HEALTH",
                "WARNING",
                "resource warning",
            ),
        ]

        agent.run_all_checks.return_value = results
        agent.send_issue_alert_if_any.return_value = True

        mode, exit_code = health_module.main()

        self.assertEqual(mode, "check")
        self.assertEqual(exit_code, 0)

        agent.send_issue_alert_if_any.assert_called_once_with(results)
        agent.send_health_heartbeat.assert_not_called()

    @patch("scripts.algo_health_agent.AlgoHealthAgent")
    @patch(
        "sys.argv",
        ["algo_health_agent.py", "--mode", "check"],
    )
    def test_check_mode_cooldown_sends_alert_without_failure(
        self,
        mock_agent_cls,
    ):
        agent = mock_agent_cls.return_value
        results = [
            HealthCheckResult(
                "SYSTEM_BLOCKERS",
                "COOLDOWN",
                "COOLDOWN until 2026-10-01 15:15:01 IST "
                "(losses=5, net-based)",
            ),
        ]
        agent.run_all_checks.return_value = results
        agent.send_issue_alert_if_any.return_value = True

        mode, exit_code = health_module.main()

        self.assertEqual(mode, "check")
        self.assertEqual(exit_code, 0)
        agent.send_issue_alert_if_any.assert_called_once_with(results)
        agent.send_health_heartbeat.assert_not_called()

    @patch("scripts.algo_health_agent.AlgoHealthAgent")
    @patch(
        "sys.argv",
        ["algo_health_agent.py", "--mode", "check"],
    )
    def test_check_mode_blocker_sends_alert_not_heartbeat(
        self,
        mock_agent_cls,
    ):
        agent = mock_agent_cls.return_value

        results = [
            HealthCheckResult(
                "DATA_INTEGRITY",
                "BLOCKER",
                "market data stale",
            ),
        ]

        agent.run_all_checks.return_value = results
        agent.send_issue_alert_if_any.return_value = True

        mode, exit_code = health_module.main()

        self.assertEqual(mode, "check")
        self.assertEqual(exit_code, 1)

        agent.send_issue_alert_if_any.assert_called_once_with(results)
        agent.send_health_heartbeat.assert_not_called()

    @patch("scripts.algo_health_agent.AlgoHealthAgent")
    @patch(
        "sys.argv",
        ["algo_health_agent.py", "--mode", "check"],
    )
    def test_check_mode_heartbeat_delivery_failure_returns_failure(
        self,
        mock_agent_cls,
    ):
        agent = mock_agent_cls.return_value

        results = [
            HealthCheckResult(
                "MODEL_HEALTH",
                "OK",
                "model healthy",
            ),
            HealthCheckResult(
                "DATA_INTEGRITY",
                "OK",
                "data healthy",
            ),
            HealthCheckResult(
                "RUNTIME_ACTIVITY",
                "OK",
                "runtime healthy",
            ),
        ]

        agent.run_all_checks.return_value = results
        agent.send_health_heartbeat.return_value = False

        mode, exit_code = health_module.main()

        self.assertEqual(mode, "check")
        self.assertEqual(exit_code, 1)

        agent.send_health_heartbeat.assert_called_once_with(results)

    def test_health_heartbeat_rejects_non_ok_results(self):
        with self.assertRaisesRegex(
            RuntimeError,
            "HEALTH_HEARTBEAT_REQUIRES_ALL_CHECKS_OK",
        ):
            self.agent.send_health_heartbeat(
                [
                    HealthCheckResult(
                        "DATA_INTEGRITY",
                        "WARNING",
                        "warning",
                    )
                ]
            )

    def test_cooldown_alert_is_yellow_and_contains_expiry(self):
        self.agent.telegram.is_configured = MagicMock(return_value=True)
        self.agent.telegram.send_message = MagicMock(
            return_value=(True, None, None)
        )
        result = HealthCheckResult(
            "SYSTEM_BLOCKERS",
            "COOLDOWN",
            "COOLDOWN until 2026-10-01 15:15:01 IST "
            "(losses=5, net-based)",
        )

        self.assertTrue(self.agent.send_issue_alert_if_any([result]))

        message = self.agent.telegram.send_message.call_args.args[0]
        self.assertIn("🟡 <b>ALGO SYSTEM — HEALTH ALERT</b>", message)
        self.assertIn(
            "🟡 <b>SYSTEM_BLOCKERS</b>: COOLDOWN until "
            "2026-10-01 15:15:01 IST (losses=5, net-based)",
            message,
        )


if __name__ == "__main__":
    unittest.main()
