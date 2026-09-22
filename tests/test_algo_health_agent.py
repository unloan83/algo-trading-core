import os
import unittest
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


if __name__ == "__main__":
    unittest.main()
