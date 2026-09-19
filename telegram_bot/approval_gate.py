import hashlib
import time
from typing import Optional, Callable, Dict, Any
from core.models import Signal, RiskCheckResult


def compute_signal_id(signal: Signal) -> str:
    ts_str = signal.timestamp.isoformat() if hasattr(signal.timestamp, "isoformat") else str(signal.timestamp)
    raw = f"{signal.symbol}_{ts_str}_{signal.model_name}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


class ApprovalGate:
    def __init__(self, timeout_seconds: int = 120, default_action_on_timeout: str = "system_recommendation"):
        self.timeout_seconds = timeout_seconds
        self.default_action_on_timeout = default_action_on_timeout

    def format_approval_message(self, signal: Signal, risk_res: RiskCheckResult) -> str:
        stop_dist_pct = abs(signal.entry_price - signal.stop_price) / signal.entry_price * 100.0
        r_multiple = abs(signal.target_price - signal.entry_price) / abs(signal.entry_price - signal.stop_price)
        return (
            f"⚡ <b>NEW PAPER SIGNAL ({signal.side.value})</b>\n\n"
            f"• <b>Symbol:</b> {signal.symbol}\n"
            f"• <b>Model:</b> {signal.model_name} (Regime: {signal.regime.value})\n"
            f"• <b>Computed Qty:</b> {risk_res.computed_qty} shares\n"
            f"• <b>Entry:</b> ₹{signal.entry_price:.2f}\n"
            f"• <b>Stop Loss:</b> ₹{signal.stop_price:.2f} ({stop_dist_pct:.2f}%)\n"
            f"• <b>Target:</b> ₹{signal.target_price:.2f} ({r_multiple:.1f}R)\n"
            f"• <b>Rupee Risk:</b> ₹{risk_res.rupee_risk:.2f}\n"
            f"• <b>Rationale:</b> {signal.rationale}\n\n"
            f"⏱ <i>{self.timeout_seconds}s approval window.</i>"
        )

    def process_signal(
        self,
        signal: Signal,
        risk_res: RiskCheckResult,
        telegram_send_fn: Optional[Callable] = None,
        human_response_callback: Optional[Callable[[], Any]] = None,
    ) -> Dict:
        target_id = compute_signal_id(signal)

        if telegram_send_fn:
            try:
                telegram_send_fn(self.format_approval_message(signal, risk_res), target_id)
            except TypeError:
                telegram_send_fn(self.format_approval_message(signal, risk_res))

        start = time.monotonic()
        user_choice = None
        while time.monotonic() - start < self.timeout_seconds:
            if human_response_callback:
                res = human_response_callback()
                if res is not None:
                    if isinstance(res, tuple):
                        response, cb_id = res
                    elif isinstance(res, list):
                        response, cb_id = (res[0], res[1]) if len(res) >= 2 else (res[0], None)
                    else:
                        response, cb_id = res, None

                    if response in {"APPROVE", "SKIP", "HOLD"}:
                        if cb_id is None or str(cb_id).strip().lower() == target_id.lower():
                            user_choice = response
                            break
            time.sleep(0.5)

        if user_choice is None:
            action = "APPROVED" if self.default_action_on_timeout == "system_recommendation" else "SKIPPED"
            return {"action": action, "auto_executed_on_timeout": True, "timed_out": True, "signal_id": target_id}

        return {
            "action": "APPROVED" if user_choice == "APPROVE" else "SKIPPED",
            "auto_executed_on_timeout": False,
            "timed_out": False,
            "signal_id": target_id,
        }
