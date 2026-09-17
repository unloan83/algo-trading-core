import time
import threading
from typing import Optional, Callable, Dict
from datetime import datetime
from core.models import Signal, RiskCheckResult, Side

class ApprovalGate:
    def __init__(
        self,
        timeout_seconds: int = 120,
        default_action_on_timeout: str = "system_recommendation"
    ):
        self.timeout_seconds = timeout_seconds
        self.default_action_on_timeout = default_action_on_timeout

    def format_approval_message(self, signal: Signal, risk_res: RiskCheckResult) -> str:
        stop_dist_pct = abs(signal.entry_price - signal.stop_price) / signal.entry_price * 100.0
        r_multiple = abs(signal.target_price - signal.entry_price) / abs(signal.entry_price - signal.stop_price)

        msg = (
            f"⚡ <b>NEW TRADING SIGNAL ({signal.side.value})</b>\n\n"
            f"• <b>Symbol:</b> {signal.symbol}\n"
            f"• <b>Model:</b> {signal.model_name} (Regime: {signal.regime.value})\n"
            f"• <b>Computed Qty:</b> {risk_res.computed_qty} shares\n"
            f"• <b>Entry:</b> ₹{signal.entry_price:.2f}\n"
            f"• <b>Stop Loss:</b> ₹{signal.stop_price:.2f} ({stop_dist_pct:.2f}%)\n"
            f"• <b>Target:</b> ₹{signal.target_price:.2f} ({r_multiple:.1f}R)\n"
            f"• <b>Rupee Risk:</b> ₹{risk_res.rupee_risk:.2f} ({self._fmt_pct(risk_res.rupee_risk/risk_res.equity_now*100)} of equity)\n"
            f"• <b>Daily Circuit Used:</b> {risk_res.daily_circuit_used_pct:.2f}%\n"
            f"• <b>Rationale:</b> {signal.rationale}\n\n"
            f"⏱ <i>120s timer active. Auto-executing precomputed action if no response.</i>"
        )
        return msg

    def _fmt_pct(self, val: float) -> str:
        return f"{val:.2f}%"

    def process_signal(
        self,
        signal: Signal,
        risk_res: RiskCheckResult,
        telegram_send_fn: Optional[Callable[[str, Dict], str]] = None,
        human_response_callback: Optional[Callable[[], Optional[str]]] = None
    ) -> Dict:
        """
        Executes the 120s approval gate workflow.
        Returns dict with: action ('APPROVED' | 'SKIPPED'), auto_executed_on_timeout (bool).
        """
        message_text = self.format_approval_message(signal, risk_res)

        if telegram_send_fn:
            telegram_send_fn(message_text, {"buy": "✅ APPROVE", "skip": "⛔ SKIP", "hold": "⏸ HOLD"})

        start_time = time.time()
        user_choice = None

        if human_response_callback:
            while time.time() - start_time < self.timeout_seconds:
                res = human_response_callback()
                if res in ["APPROVE", "SKIP", "HOLD"]:
                    user_choice = res
                    break
                time.sleep(0.5)

        if user_choice is None:
            # Timeout reached
            if self.default_action_on_timeout == "system_recommendation":
                action = "APPROVED"
            else:
                action = "SKIPPED"
            return {
                "action": action,
                "auto_executed_on_timeout": True,
                "timed_out": True
            }

        if user_choice == "APPROVE":
            return {"action": "APPROVED", "auto_executed_on_timeout": False, "timed_out": False}
        else:
            return {"action": "SKIPPED", "auto_executed_on_timeout": False, "timed_out": False}
