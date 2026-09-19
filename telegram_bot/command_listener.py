#!/usr/bin/env python3

import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from core.circuit_tracker import CircuitTracker
from core.order_router import OrderRouter
from core.paper_engine import paper_gate_status
from data.db_models import DatabaseManager
from scripts.algo_health_agent import AlgoHealthAgent
from scripts.runtime_common import now_ist_naive, read_runtime_marker
from telegram_bot.telegram_client import TelegramClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("telegram_command_listener")


class StatusReporter:
    def __init__(
        self,
        db: DatabaseManager,
        health_agent: Optional[AlgoHealthAgent] = None,
        now_fn: Callable[[], datetime] = now_ist_naive,
        marker_reader: Callable = read_runtime_marker,
    ):
        self.db = db
        self.health_agent = health_agent or AlgoHealthAgent(db=db)
        self.now_fn = now_fn
        self.marker_reader = marker_reader

    @staticmethod
    def _format_timestamp(value: Optional[datetime]) -> str:
        return value.isoformat(sep=" ", timespec="seconds") if value else "NOT_RECORDED"

    def render(self) -> str:
        now = self.now_fn()
        date_iso = now.date().isoformat()
        mode = os.getenv("TRADING_MODE", "PAPER").upper()
        gate = paper_gate_status(self.db, as_of=now)
        summary = self.health_agent.today_summary(date_iso)
        regime = self.db.get_latest_regime(date_iso)
        positions = self.db.get_open_positions()
        circuit = self.health_agent.check_system_blockers()
        preflight_time = self.db.get_last_preflight_time()
        health_marker = self.marker_reader("algo_health_agent")
        health_time = health_marker.get("timestamp") if health_marker else None

        circuit_details = circuit.details
        daily_usage = circuit_details.get("daily_circuit_used_pct")
        weekly_usage = circuit_details.get("weekly_circuit_used_pct")
        monthly_usage = circuit_details.get("monthly_circuit_used_pct")

        def usage_text(value: Optional[float], limit: float) -> str:
            if value is None:
                return "UNAVAILABLE"
            return f"{value:.2f}% / {limit:.2f}%"

        lines = [
            "📊 <b>ALGO SYSTEM — STATUS</b>",
            "",
            f"• <b>Timestamp:</b> {now.strftime('%Y-%m-%d %H:%M:%S IST')}",
            f"• <b>Trading mode:</b> {mode}",
            f"• <b>Live order path:</b> {OrderRouter.live_order_path_status()}",
            "",
            "<b>Paper Gate</b>",
            f"• Days elapsed: {gate['days_elapsed']} / {gate['days_required']}",
            f"• Trades completed: {gate['trades_completed']} / {gate['trades_required']}",
            f"• gate_cleared: {str(gate['gate_cleared']).lower()}",
            "",
            "<b>Today's Activity</b>",
            f"• Regime: {regime['regime'] if regime else 'NOT_RECORDED'}",
            f"• Signals evaluated: {summary['signals_evaluated_today']}",
            f"• Signals blocked: {summary['blocked_signals_today']}",
            f"• Orders placed: {summary['orders_today']}",
            "",
            "<b>Open Positions</b>",
        ]

        if positions:
            for position in positions:
                mtm = CircuitTracker.position_mark_to_market(position)
                lines.append(
                    f"• {position.symbol}: qty={position.qty}, "
                    f"entry=₹{position.entry_price:.2f}, MTM=₹{mtm:,.2f}"
                )
        else:
            lines.append("• None")

        lines.extend(
            [
                "",
                "<b>Circuit Usage</b>",
                "• Daily: "
                + usage_text(daily_usage, self.health_agent.governor.daily_circuit_pct),
                "• Weekly: "
                + usage_text(weekly_usage, self.health_agent.governor.weekly_circuit_pct),
                "• Monthly: "
                + usage_text(monthly_usage, self.health_agent.governor.monthly_circuit_pct),
                "",
                "<b>Last Runs</b>",
                f"• Successful preflight: {self._format_timestamp(preflight_time)}",
                f"• Health check: {self._format_timestamp(health_time)}",
            ]
        )
        return "\n".join(lines)


class TelegramCommandListener:
    """The only process allowed to consume Telegram getUpdates."""

    def __init__(
        self,
        db: DatabaseManager,
        telegram: TelegramClient,
        status_reporter: StatusReporter,
    ):
        self.db = db
        self.telegram = telegram
        self.status_reporter = status_reporter

    def _authorized(self, payload: dict) -> bool:
        user_id = (payload.get("from") or {}).get("id")
        chat_id = ((payload.get("chat") or {}).get("id"))
        return self.telegram.is_authorized_sender(user_id, chat_id)

    def handle_update(self, update: dict) -> None:
        callback = update.get("callback_query")
        if callback:
            message = callback.get("message") or {}
            owner_payload = {
                "from": callback.get("from") or {},
                "chat": message.get("chat") or {},
            }
            if not self._authorized(owner_payload):
                return
            action, signal_id = self.telegram.parse_callback_data(
                callback.get("data")
            )
            if action in {"APPROVE", "SKIP", "HOLD"}:
                self.db.enqueue_telegram_callback(
                    update_id=int(update["update_id"]),
                    action=action,
                    signal_id=signal_id,
                )
            return

        message = update.get("message")
        if not message or not self._authorized(message):
            return

        command = str(message.get("text") or "").strip().split(maxsplit=1)[0]
        command = command.split("@", 1)[0].lower()
        if command != "/status":
            return

        response = self.status_reporter.render()
        ok, _, error = self.telegram.send_message(response)
        if not ok:
            raise RuntimeError(f"TELEGRAM_STATUS_REPLY_FAILED:{error}")

    def poll_once(self, timeout: int = 30) -> int:
        offset = self.db.get_telegram_update_offset()
        updates = self.telegram.get_updates(offset=offset, timeout=timeout)
        for update in sorted(updates, key=lambda item: int(item["update_id"])):
            self.handle_update(update)
            self.db.set_telegram_update_offset(int(update["update_id"]) + 1)
        return len(updates)

    def run_forever(self, timeout: int = 30, retry_seconds: int = 5) -> None:
        if not self.telegram.is_configured():
            raise RuntimeError("TELEGRAM_NOT_CONFIGURED_OR_ALLOWLIST_MISSING")
        log.info("Telegram command listener started as sole getUpdates consumer")
        while True:
            try:
                self.poll_once(timeout=timeout)
            except Exception as exc:
                log.error(
                    "Telegram listener poll failed: %s: %s",
                    type(exc).__name__,
                    self.telegram.safe_error(exc),
                )
                time.sleep(retry_seconds)


def main() -> None:
    db = DatabaseManager()
    telegram = TelegramClient(callback_store=db)
    reporter = StatusReporter(db=db)
    TelegramCommandListener(db, telegram, reporter).run_forever()


if __name__ == "__main__":
    main()
