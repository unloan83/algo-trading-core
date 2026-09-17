from typing import Optional, Callable
from core.models import Position

class NotificationService:
    def __init__(self, send_fn: Optional[Callable[[str], None]] = None):
        self.send_fn = send_fn

    def notify_exit(
        self,
        symbol: str,
        side: str,
        qty: int,
        entry_price: float,
        exit_price: float,
        gross_pnl: float,
        net_pnl: float,
        exit_reason: str
    ):
        msg = (
            f"🚨 <b>POSITION EXITED</b>\n\n"
            f"• <b>Symbol:</b> {symbol} ({side})\n"
            f"• <b>Qty:</b> {qty}\n"
            f"• <b>Entry:</b> ₹{entry_price:.2f}\n"
            f"• <b>Exit:</b> ₹{exit_price:.2f}\n"
            f"• <b>Net P&L:</b> ₹{net_pnl:.2f}\n"
            f"• <b>Reason:</b> {exit_reason}"
        )
        if self.send_fn:
            self.send_fn(msg)
        return msg

    def notify_circuit_breaker(self, circuit_type: str, limit_pct: float, current_loss_pct: float):
        msg = (
            f"🛑 <b>RISK GOVERNOR CIRCUIT BREAKER TRIP</b>\n\n"
            f"• <b>Circuit Type:</b> {circuit_type.upper()}\n"
            f"• <b>Threshold:</b> {limit_pct:.2f}%\n"
            f"• <b>Observed Drawdown:</b> {current_loss_pct:.2f}%\n"
            f"• <b>Action:</b> ALL NEW ENTRIES HARD-BLOCKED"
        )
        if self.send_fn:
            self.send_fn(msg)
        return msg
