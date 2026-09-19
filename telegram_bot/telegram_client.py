import logging
import os
from typing import Optional, Tuple

import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
_project_root = Path(__file__).resolve().parents[1]
for _env_path in [
    Path("/home/user/projects/retained_credentials_and_data/Telegram_Credentials.env"),
    Path("/home/user/projects/Telegram_Credentials.env"),
    _project_root / ".env",
]:
    if _env_path.exists():
        load_dotenv(_env_path, override=True)

logger = logging.getLogger("telegram_client")


class TelegramClient:
    """Single-user Telegram client using long polling and a mandatory user allowlist."""

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        allowed_user_id: Optional[str] = None,
    ):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
        self.chat_id = str(chat_id or os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID") or "")
        self.allowed_user_id = str(allowed_user_id or os.getenv("TELEGRAM_ALLOWED_USER_ID") or "")
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}" if self.bot_token else None
        self._offset: Optional[int] = None

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
            return False, None, f"Telegram HTTP exception: {exc}"

    def send_approval(self, text: str, signal_id: Optional[str] = None):
        suffix = f":{signal_id}" if signal_id else ""
        keyboard = [[
            {"text": "✅ APPROVE", "callback_data": f"APPROVE{suffix}"},
            {"text": "⛔ SKIP", "callback_data": f"SKIP{suffix}"},
            {"text": "⏸ HOLD", "callback_data": f"HOLD{suffix}"},
        ]]
        return self.send_message(text, keyboard)

    def poll_callback_query(self, timeout: int = 2) -> Tuple[Optional[str], Optional[str]]:
        if not self.is_configured():
            return None, None
        params = {"timeout": timeout}
        if self._offset is not None:
            params["offset"] = self._offset
        try:
            resp = requests.get(
                f"{self.base_url}/getUpdates",
                params=params,
                timeout=timeout + 5,
            )
            data = resp.json()
            if not data.get("ok"):
                return None, None
            for update in data.get("result", []):
                self._offset = update["update_id"] + 1
                cb = update.get("callback_query")
                if not cb:
                    continue
                from_id = str((cb.get("from") or {}).get("id", ""))
                if from_id != self.allowed_user_id:
                    logger.warning("Ignored Telegram callback from unauthorized user id")
                    continue
                message_chat_id = str(((cb.get("message") or {}).get("chat") or {}).get("id", ""))
                if message_chat_id and message_chat_id != self.chat_id:
                    logger.warning("Ignored Telegram callback from unauthorized chat")
                    continue
                raw_data = str(cb.get("data") or "").strip()
                if ":" in raw_data:
                    action, cb_id = raw_data.split(":", 1)
                    return action.upper(), cb_id.strip()
                return raw_data.upper(), None
        except Exception:
            return None, None
        return None, None
