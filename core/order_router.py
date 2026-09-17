import os
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from core.models import Signal, Order, OrderStatus, Side


class PaperBrokerSimulator:
    def __init__(self, slippage_pct: float = 0.05):
        self.slippage_pct = slippage_pct

    def execute_order(
        self,
        signal: Signal,
        qty: int,
        auto_executed_on_timeout: bool = False,
        is_intraday: bool = False,
    ) -> Order:
        if signal.side == Side.BUY:
            fill_price = signal.entry_price * (1.0 + self.slippage_pct / 100.0)
        else:
            fill_price = signal.entry_price * (1.0 - self.slippage_pct / 100.0)

        return Order(
            order_id=f"paper_{uuid.uuid4().hex[:10]}",
            symbol=signal.symbol,
            side=signal.side,
            qty=qty,
            entry_price=signal.entry_price,
            stop_price=signal.stop_price,
            target_price=signal.target_price,
            status=OrderStatus.FILLED,
            created_at=datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None),
            broker_order_id=f"sim_{uuid.uuid4().hex[:8]}",
            filled_price=fill_price,
            auto_executed_on_timeout=auto_executed_on_timeout,
            is_paper=True,
            is_intraday=is_intraday,
        )

    def simulate_exit_price(self, side: Side, observed_price: float) -> float:
        if side == Side.BUY:
            return observed_price * (1.0 - self.slippage_pct / 100.0)
        return observed_price * (1.0 + self.slippage_pct / 100.0)


class OrderRouter:
    """
    PAPER-only router for the mandatory 30-day paper gate.

    There is deliberately no Upstox order-placement implementation in this class.
    """
    def __init__(self, live_mode: bool = False, slippage_pct: float = 0.05):
        if live_mode:
            raise RuntimeError("LIVE_TRADING_DISABLED_DURING_30_DAY_PAPER_GATE")
        if os.getenv("TRADING_MODE", "PAPER").upper() != "PAPER":
            raise RuntimeError("TRADING_MODE_MUST_BE_PAPER_DURING_30_DAY_GATE")
        self.live_mode = False
        self.simulator = PaperBrokerSimulator(slippage_pct=slippage_pct)

    def route_order(
        self,
        signal: Signal,
        qty: int,
        auto_executed_on_timeout: bool = False,
        is_intraday: bool = False,
    ) -> Order:
        return self.simulator.execute_order(
            signal,
            qty,
            auto_executed_on_timeout=auto_executed_on_timeout,
            is_intraday=is_intraday,
        )
