import unittest
import os
import sqlite3
from datetime import datetime, date
from core.circuit_tracker import CircuitTracker
from data.db_models import DatabaseManager
from core.models import Position, Side

class TestCircuitTracker(unittest.TestCase):
    def setUp(self):
        self.db_path = "/tmp/test_circuit_tracker.db"
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        self.db = DatabaseManager(self.db_path)
        self.tracker = CircuitTracker(self.db)

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_mark_to_market_pnl_includes_open_positions(self):
        # Open position: BUY 100 shares at 100.0, current price 95.0 -> MTM loss = -500.0
        pos = Position(
            position_id="p1", symbol="TCS", side=Side.BUY, qty=100,
            entry_price=100.0, current_price=95.0, stop_price=90.0, target_price=110.0,
            opened_at=datetime.now()
        )
        
        daily_pnl, weekly_pnl, monthly_pnl = self.tracker.compute_mark_to_market_pnl([pos])
        
        # MTM P&L includes open position unrealized P&L (-500.0)
        self.assertEqual(daily_pnl, -500.0)
        self.assertEqual(weekly_pnl, -500.0)
        self.assertEqual(monthly_pnl, -500.0)

    def test_calendar_rollover_resets(self):
        # Day 1: Friday 2026-09-18
        dt_friday = datetime(2026, 9, 18, 10, 0, 0)
        res1 = self.tracker.check_and_update_rollover_state(dt_friday)
        
        # Day 2: Monday 2026-09-21 (New ISO Week)
        dt_monday = datetime(2026, 9, 21, 10, 0, 0)
        res2 = self.tracker.check_and_update_rollover_state(dt_monday)
        
        self.assertTrue(res2['daily_reset'])
        self.assertTrue(res2['weekly_reset']) # Monday ISO week rollover verified!

if __name__ == "__main__":
    unittest.main()
