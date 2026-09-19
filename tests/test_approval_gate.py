import unittest
from datetime import datetime
from core.models import Signal, Side, RegimeType, RiskCheckResult
from telegram_bot.approval_gate import ApprovalGate

class TestApprovalGate(unittest.TestCase):
    def test_approval_gate_timeout_auto_execute(self):
        gate = ApprovalGate(timeout_seconds=1, default_action_on_timeout="system_recommendation")
        sig = Signal(
            symbol="TCS", side=Side.BUY, entry_price=3500.0, stop_price=3400.0, target_price=3700.0,
            model_name="trend_pullback", regime=RegimeType.TREND_UP, rationale="test", timestamp=datetime.now()
        )
        risk_res = RiskCheckResult(passed=True, reason_code="PASSED", computed_qty=10, rupee_risk=1000.0, equity_now=100000.0)

        res = gate.process_signal(sig, risk_res)
        self.assertEqual(res['action'], "APPROVED")
        self.assertTrue(res['auto_executed_on_timeout'])
        self.assertTrue(res['timed_out'])

    def test_approval_gate_manual_approve(self):
        gate = ApprovalGate(timeout_seconds=5)
        sig = Signal(
            symbol="TCS", side=Side.BUY, entry_price=3500.0, stop_price=3400.0, target_price=3700.0,
            model_name="trend_pullback", regime=RegimeType.TREND_UP, rationale="test", timestamp=datetime.now()
        )
        risk_res = RiskCheckResult(passed=True, reason_code="PASSED", computed_qty=10, rupee_risk=1000.0, equity_now=100000.0)

        res = gate.process_signal(sig, risk_res, human_response_callback=lambda: "APPROVE")
        self.assertEqual(res['action'], "APPROVED")
        self.assertFalse(res['auto_executed_on_timeout'])
        self.assertFalse(res['timed_out'])

    def test_approval_gate_matching_signal_id(self):
        from telegram_bot.approval_gate import compute_signal_id
        gate = ApprovalGate(timeout_seconds=5)
        sig = Signal(
            symbol="INFY", side=Side.BUY, entry_price=1500.0, stop_price=1450.0, target_price=1600.0,
            model_name="breakout", regime=RegimeType.TREND_UP, rationale="test", timestamp=datetime.now()
        )
        sig_id = compute_signal_id(sig)
        risk_res = RiskCheckResult(passed=True, reason_code="PASSED", computed_qty=10, rupee_risk=500.0, equity_now=100000.0)

        res = gate.process_signal(sig, risk_res, human_response_callback=lambda: ("APPROVE", sig_id))
        self.assertEqual(res['action'], "APPROVED")
        self.assertEqual(res['signal_id'], sig_id)

    def test_approval_gate_mismatched_signal_id(self):
        gate = ApprovalGate(timeout_seconds=1)
        sig = Signal(
            symbol="INFY", side=Side.BUY, entry_price=1500.0, stop_price=1450.0, target_price=1600.0,
            model_name="breakout", regime=RegimeType.TREND_UP, rationale="test", timestamp=datetime.now()
        )
        risk_res = RiskCheckResult(passed=True, reason_code="PASSED", computed_qty=10, rupee_risk=500.0, equity_now=100000.0)

        res = gate.process_signal(sig, risk_res, human_response_callback=lambda: ("APPROVE", "WRONG_ID"))
        self.assertTrue(res['timed_out'])


if __name__ == "__main__":
    unittest.main()
