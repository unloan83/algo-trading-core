import uuid
from datetime import datetime
from typing import Optional, Dict
from core.models import Signal, Order, OrderStatus, Position, Side
from backtest.cost_model import calculate_indian_transaction_costs

class PaperBrokerSimulator:
    def __init__(self, slippage_pct: float = 0.05):
        self.slippage_pct = slippage_pct

    def execute_order(
        self,
        signal: Signal,
        qty: int,
        auto_executed_on_timeout: bool = False
    ) -> Order:
        # Simulate fill with slippage
        if signal.side == Side.BUY:
            fill_price = signal.entry_price * (1.0 + self.slippage_pct / 100.0)
        else:
            fill_price = signal.entry_price * (1.0 - self.slippage_pct / 100.0)

        order_id = f"paper_{uuid.uuid4().hex[:10]}"
        broker_order_id = f"sim_{uuid.uuid4().hex[:8]}"

        return Order(
            order_id=order_id,
            symbol=signal.symbol,
            side=signal.side,
            qty=qty,
            entry_price=signal.entry_price,
            stop_price=signal.stop_price,
            target_price=signal.target_price,
            status=OrderStatus.FILLED,
            created_at=datetime.now(),
            broker_order_id=broker_order_id,
            filled_price=fill_price,
            auto_executed_on_timeout=auto_executed_on_timeout,
            is_paper=True
        )

class OrderRouter:
    def __init__(self, live_mode: bool = False, slippage_pct: float = 0.05):
        self.live_mode = live_mode
        self.simulator = PaperBrokerSimulator(slippage_pct=slippage_pct)

    def route_order(
        self,
        signal: Signal,
        qty: int,
        auto_executed_on_timeout: bool = False
    ) -> Order:
        if not self.live_mode:
            return self.simulator.execute_order(signal, qty, auto_executed_on_timeout)
        else:
            raise NotImplementedError("Live mode router requires live broker API credentials and 90-day paper trading gate completion.")
