import os
import requests
import logging
from typing import Optional, Dict, Any, Tuple
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("telegram_client")

class TelegramClient:
    """
    Lightweight Telegram Bot API client using standard requests.
    Supports TELEGRAM_BOT_TOKEN and TELEGRAM_TOKEN environment variable keys.
    """
    def __init__(self, bot_token: Optional[str] = None, chat_id: Optional[str] = None):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID")
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}" if self.bot_token else None

    def is_configured(self) -> bool:
        return bool(self.bot_token and self.chat_id and self.bot_token != "YOUR_TELEGRAM_BOT_TOKEN")

    def send_message(
        self,
        text: str,
        inline_keyboard: Optional[list] = None
    ) -> Tuple[bool, Optional[int], str]:
        if not self.is_configured():
            msg = "TELEGRAM NOT CONFIGURED: Set TELEGRAM_BOT_TOKEN/TELEGRAM_TOKEN and TELEGRAM_CHAT_ID in .env"
            logger.warning(msg)
            return False, None, msg

        url = f"{self.base_url}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML"
        }

        if inline_keyboard:
            payload["reply_markup"] = {"inline_keyboard": inline_keyboard}

        try:
            resp = requests.post(url, json=payload, timeout=10)
            data = resp.json()
            if data.get("ok"):
                msg_id = data["result"]["message_id"]
                return True, msg_id, "Message sent successfully"
            else:
                return False, None, f"Telegram API error: {data.get('description')}"
        except Exception as e:
            return False, None, f"HTTP Exception sending Telegram message: {str(e)}"

    def poll_callback_query(self, offset: Optional[int] = None, timeout: int = 2) -> Tuple[Optional[str], Optional[int]]:
        if not self.is_configured():
            return None, offset

        url = f"{self.base_url}/getUpdates"
        params = {"timeout": timeout}
        if offset:
            params["offset"] = offset

        try:
            resp = requests.get(url, params=params, timeout=timeout + 5)
            data = resp.json()
            if data.get("ok") and data.get("result"):
                for update in data["result"]:
                    new_offset = update["update_id"] + 1
                    if "callback_query" in update:
                        cb_data = update["callback_query"].get("data")
                        return cb_data, new_offset
                return None, data["result"][-1]["update_id"] + 1
        except Exception:
            pass
        return None, offset
