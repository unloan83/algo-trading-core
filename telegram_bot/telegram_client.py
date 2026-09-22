import logging
import os
import time
from pathlib import Path
from typing import Any, List, Optional, Tuple

import requests
from dotenv import load_dotenv

_project_root = Path(__file__).resolve().parents[1]
load_dotenv(_project_root / ".env", override=False)

logger = logging.getLogger("telegram_client")


class TelegramClient:
    """Single-user Telegram client using long polling and a mandatory user allowlist."""

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        allowed_user_id: Optional[str] = None,
        callback_store: Optional[Any] = None,
    ):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
        self.chat_id = str(chat_id or os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID") or "")
        self.allowed_user_id = str(allowed_user_id or os.getenv("TELEGRAM_ALLOWED_USER_ID") or "")
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}" if self.bot_token else None
        self.callback_store = callback_store

    def safe_error(self, exc: Exception) -> str:
        message = str(exc)
        if self.bot_token:
            message = message.replace(self.bot_token, "[REDACTED]")
        return message

    def is_configured(self) -> bool:
        return bool(
            self.bot_token
            and self.chat_id
            and self.allowed_user_id
            and self.bot_token != "YOUR_TELEGRAM_BOT_TOKEN"
        )

    def send_message(self, text: str, inline_keyboard: Optional[list] = None) -> Tuple[bool, Optional[int], str]:
        if not self.is_configured():
            return False, None, "TELEGRAM_NOT_CONFIGURED_OR_ALLOWLIST_MISSING"
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
        if inline_keyboard:
            payload["reply_markup"] = {"inline_keyboard": inline_keyboard}
        try:
            resp = requests.post(f"{self.base_url}/sendMessage", json=payload, timeout=10)
            data = resp.json()
            if data.get("ok"):
                return True, data["result"]["message_id"], "Message sent successfully"
            return False, None, f"Telegram API error: {data.get('description')}"
        except Exception as exc:
            safe_error = self.safe_error(exc)
            logger.error(
                "Telegram send failed: %s: %s",
                type(exc).__name__,
                safe_error,
            )
            return False, None, f"Telegram HTTP exception: {safe_error}"

    def send_approval(self, text: str, signal_id: Optional[str] = None):
        suffix = f":{signal_id}" if signal_id else ""
        keyboard = [[
            {"text": "✅ APPROVE", "callback_data": f"APPROVE{suffix}"},
            {"text": "⛔ SKIP", "callback_data": f"SKIP{suffix}"},
            {"text": "⏸ HOLD", "callback_data": f"HOLD{suffix}"},
        ]]
        return self.send_message(text, keyboard)

    def get_updates(
        self,
        offset: Optional[int] = None,
        timeout: int = 30,
    ) -> List[dict]:
        if not self.is_configured():
            raise RuntimeError("TELEGRAM_NOT_CONFIGURED_OR_ALLOWLIST_MISSING")
        params = {
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            params["offset"] = int(offset)
        response = requests.get(
            f"{self.base_url}/getUpdates",
            params=params,
            timeout=timeout + 5,
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(
                f"TELEGRAM_GET_UPDATES_FAILED:{data.get('description', 'unknown')}"
            )
        return list(data.get("result", []))

    def is_authorized_sender(
        self,
        user_id: object,
        chat_id: object,
    ) -> bool:
        return (
            str(user_id or "") == self.allowed_user_id
            and str(chat_id or "") == self.chat_id
        )

    @staticmethod
    def parse_callback_data(raw_data: object) -> Tuple[str, Optional[str]]:
        value = str(raw_data or "").strip()
        if ":" in value:
            action, signal_id = value.split(":", 1)
            return action.upper(), signal_id.strip() or None
        return value.upper(), None

    def poll_callback_query(
        self,
        timeout: int = 2,
        signal_id: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Read callbacks routed by the sole Telegram getUpdates owner."""
        if not self.is_configured() or self.callback_store is None:
            return None, None

        deadline = time.monotonic() + max(0, timeout)
        while True:
            try:
                callback = self.callback_store.pop_telegram_callback(signal_id)
            except Exception as exc:
                logger.error(
                    "Telegram callback inbox failed: %s: %s",
                    type(exc).__name__,
                    self.safe_error(exc),
                )
                return None, None
            if callback is not None:
                return callback
            if time.monotonic() >= deadline:
                return None, None
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
