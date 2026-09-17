import unittest
from datetime import datetime, timedelta
from pydantic import ValidationError
from core.models import Signal, Side, RegimeType
from core.risk_governor import RiskGovernor

def make_signal(symbol="RELIANCE", entry=100.0, stop=95.0, target=110.0, age_sec=10):
    return Signal(
        symbol=symbol,
        side=Side.BUY,
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        model_name="trend_pullback",
        regime=RegimeType.TREND_UP,
        rationale="test",
        timestamp=datetime.now() - timedelta(seconds=age_sec)
    )

class TestRiskGovernor(unittest.TestCase):
    def test_risk_governor_zero_equity_top_level_guard(self):
        gov = RiskGovernor()
        sig = make_signal()
        res = gov.validate_signal_against_invariants(
            signal=sig, equity_now=0.0, available_cash=100000.0, open_positions=[],
            daily_pnl=0.0, weekly_pnl=0.0, monthly_pnl=0.0,
            consecutive_losses=0, last_loss_time=None,
            halted_symbols=[], corporate_action_symbols=[], broker_account_ok=True
        )
        self.assertFalse(res.passed)
        self.assertEqual(res.reason_code, "EQUITY_ZERO_OR_NEGATIVE")
        self.assertEqual(res.equity_now, 0.0)

    def test_risk_governor_stop_above_buy_entry_blocked_by_pydantic(self):
        with self.assertRaises(ValidationError):
            make_signal(entry=100.0, stop=105.0, target=120.0)

    def test_risk_governor_daily_circuit_breach(self):
        gov = RiskGovernor(daily_circuit_pct=1.5)
        sig = make_signal()
        res = gov.validate_signal_against_invariants(
            signal=sig, equity_now=100000.0, available_cash=500000.0, open_positions=[],
            daily_pnl=-1600.0,
            weekly_pnl=0.0, monthly_pnl=0.0,
            consecutive_losses=0, last_loss_time=None,
            halted_symbols=[], corporate_action_symbols=[], broker_account_ok=True
        )
        self.assertFalse(res.passed)
        self.assertEqual(res.reason_code, "INV5_DAILY_LOSS_CIRCUIT_BREACHED")

    def test_risk_governor_cooldown_active(self):
        gov = RiskGovernor(cooldown_losses_trigger=3, cooldown_hours=24)
        sig = make_signal()
        last_loss = datetime.now() - timedelta(hours=2)
        res = gov.validate_signal_against_invariants(
            signal=sig, equity_now=100000.0, available_cash=500000.0, open_positions=[],
            daily_pnl=0.0, weekly_pnl=0.0, monthly_pnl=0.0,
            consecutive_losses=3, last_loss_time=last_loss,
            halted_symbols=[], corporate_action_symbols=[], broker_account_ok=True
        )
        self.assertFalse(res.passed)
        self.assertEqual(res.reason_code, "INV6_CONSECUTIVE_LOSS_COOLDOWN_ACTIVE")

    def test_risk_governor_trading_halt(self):
        gov = RiskGovernor()
        sig = make_signal(symbol="HALTED_STOCK")
        res = gov.validate_signal_against_invariants(
            signal=sig, equity_now=100000.0, available_cash=500000.0, open_positions=[],
            daily_pnl=0.0, weekly_pnl=0.0, monthly_pnl=0.0,
            consecutive_losses=0, last_loss_time=None,
            halted_symbols=["HALTED_STOCK"], corporate_action_symbols=[], broker_account_ok=True
        )
        self.assertFalse(res.passed)
        self.assertEqual(res.reason_code, "INV7_SYMBOL_TRADING_HALTED")

if __name__ == "__main__":
    unittest.main()
