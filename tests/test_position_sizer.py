import unittest
from datetime import datetime
from core.position_sizer import calculate_position_size
from core.models import Position, Side

class TestPositionSizer(unittest.TestCase):
    def test_position_size_normal_buy(self):
        res = calculate_position_size(
            equity_now=100000.0,
            available_cash=500000.0,
            entry_price=100.0,
            stop_price=95.0,
            open_positions=[],
            risk_per_trade_pct=0.5
        )
        self.assertTrue(res.passed)
        self.assertEqual(res.computed_qty, 100)
        self.assertEqual(res.rupee_risk, 500.0)

    def test_position_size_zero_stop_dist(self):
        res = calculate_position_size(
            equity_now=100000.0,
            available_cash=500000.0,
            entry_price=100.0,
            stop_price=100.0,
            open_positions=[]
        )
        self.assertFalse(res.passed)
        self.assertEqual(res.reason_code, "ZERO_STOP_DISTANCE")
        self.assertEqual(res.computed_qty, 0)

    def test_position_size_small_budget_no_rounding_up(self):
        res = calculate_position_size(
            equity_now=1000.0,
            available_cash=500000.0,
            entry_price=500.0,
            stop_price=480.0,
            open_positions=[],
            risk_per_trade_pct=0.5
        )
        self.assertFalse(res.passed)
        self.assertEqual(res.reason_code, "RISK_BUDGET_TOO_SMALL")
        self.assertEqual(res.computed_qty, 0)

    def test_position_size_insufficient_cash_hard_reject(self):
        res = calculate_position_size(
            equity_now=100000.0,
            available_cash=1000.0, # Cash only ₹1,000, buy 100 shares @ ₹100 needs ₹10,000
            entry_price=100.0,
            stop_price=95.0,
            open_positions=[],
            risk_per_trade_pct=0.5
        )
        self.assertFalse(res.passed)
        self.assertEqual(res.reason_code, "INSUFFICIENT_CASH_FOR_FULL_SIZE")
        self.assertEqual(res.computed_qty, 0)

    def test_position_size_open_risk_exceeded(self):
        pos1 = Position(
            position_id="p1", symbol="SBIN", side=Side.BUY, qty=150, entry_price=100.0,
            current_price=100.0, stop_price=90.0, target_price=110.0, opened_at=datetime.now()
        )
        res = calculate_position_size(
            equity_now=100000.0,
            available_cash=500000.0,
            entry_price=200.0,
            stop_price=190.0,
            open_positions=[pos1],
            max_open_risk_pct=1.5
        )
        self.assertFalse(res.passed)
        self.assertEqual(res.reason_code, "MAX_CONCURRENT_OPEN_RISK_EXCEEDED")

    def test_position_size_negative_equity_true_value(self):
        res = calculate_position_size(
            equity_now=-5000.0,
            available_cash=1000.0,
            entry_price=100.0,
            stop_price=95.0,
            open_positions=[]
        )
        self.assertFalse(res.passed)
        self.assertEqual(res.reason_code, "EQUITY_ZERO_OR_NEGATIVE")
        self.assertEqual(res.equity_now, -5000.0) # Reports true equity_now, not 1.0!

if __name__ == "__main__":
    unittest.main()
