#!/usr/bin/env python3
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env", override=False)

from data.broker_client import UnifiedBrokerClient
from scripts.runtime_common import project_config, save_market_filters
from telegram_bot.telegram_client import TelegramClient


def main():
    errors = []
    if os.getenv("TRADING_MODE", "PAPER").upper() != "PAPER":
        errors.append("TRADING_MODE must be PAPER")

    try:
        capital = float(os.getenv("PAPER_STARTING_CAPITAL", "0"))
        if capital <= 0:
            raise ValueError
    except ValueError:
        errors.append("PAPER_STARTING_CAPITAL must be a positive rupee amount")

    # PAPER mode must use only the read-only Analytics Token.
    if not os.getenv("UPSTOX_ANALYTICS_TOKEN"):
        errors.append("UPSTOX_ANALYTICS_TOKEN is missing from project .env")

    tg = TelegramClient()
    if not tg.is_configured():
        errors.append(
            "Telegram requires TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID and TELEGRAM_ALLOWED_USER_ID"
        )

    broker = UnifiedBrokerClient(paper_mode=True)
    ok, msg = broker.validate_readonly_access()
    if not ok:
        errors.append(msg)

    symbols_cfg = project_config("universe.yaml")["universe"]
    symbols = symbols_cfg["equities"] + symbols_cfg["etfs"]
    for symbol in symbols:
        try:
            broker.resolve_instrument_key(symbol)
        except Exception as exc:
            errors.append(str(exc))

    if not errors:
        halted, corp = broker.get_trading_halts_and_corp_actions(symbols)
        save_market_filters(halted, corp)

    if errors:
        print("PREFLIGHT FAILED")
        for err in errors:
            print(f"- {err}")
        raise SystemExit(1)

    print("PREFLIGHT PASSED")
    print("mode=PAPER")
    print(f"universe={len(symbols)}")
    print("upstox=read-only market data OK")
    print("telegram=allowlisted long-polling configured")
    print("live_order_path=disabled")


if __name__ == "__main__":
    main()
