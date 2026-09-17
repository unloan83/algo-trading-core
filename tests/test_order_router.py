import unittest
from datetime import datetime
from core.models import Signal, Side, RegimeType, OrderStatus
from core.order_router import OrderRouter

class TestOrderRouter(unittest.TestCase):
    def test_paper_order_router_fill_and_slippage(self):
        router = OrderRouter(live_mode=False, slippage_pct=0.1)
        sig = Signal(
            symbol="INFY", side=Side.BUY, entry_price=1500.0, stop_price=1450.0, target_price=1600.0,
            model_name="breakout", regime=RegimeType.TREND_UP, rationale="test", timestamp=datetime.now()
        )
        
        order = router.route_order(sig, qty=20, auto_executed_on_timeout=True)
        
        self.assertEqual(order.status, OrderStatus.FILLED)
        self.assertTrue(order.is_paper)
        self.assertTrue(order.auto_executed_on_timeout)
        self.assertAlmostEqual(order.filled_price, 1501.5, places=3)

if __name__ == "__main__":
    unittest.main()
