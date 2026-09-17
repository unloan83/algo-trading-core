import os
import tempfile
import unittest
from datetime import datetime

from core.models import Signal, Side, RegimeType
from core.order_router import OrderRouter
from data.db_models import DatabaseManager


class TestPaperSafety(unittest.TestCase):
    def setUp(self):
        os.environ["TRADING_MODE"] = "PAPER"

    def test_live_router_is_physically_disabled(self):
        with self.assertRaises(RuntimeError):
            OrderRouter(live_mode=True)

    def test_paper_fill_persists_open_position(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            db = DatabaseManager(f.name)
            sig = Signal(
                symbol="TCS",
                side=Side.BUY,
                entry_price=100.0,
                stop_price=95.0,
                target_price=110.0,
                model_name="test",
                regime=RegimeType.TREND_UP,
                rationale="test",
                timestamp=datetime.now(),
            )
            order = OrderRouter().route_order(sig, 10, is_intraday=True)
            pos = db.record_paper_order_and_open_position(order)
            self.assertEqual(pos.symbol, "TCS")
            self.assertTrue(pos.is_paper)
            self.assertTrue(pos.is_intraday)
            self.assertEqual(len(db.get_open_positions()), 1)


if __name__ == "__main__":
    unittest.main()
