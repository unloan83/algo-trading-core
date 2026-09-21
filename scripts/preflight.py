#!/usr/bin/env python3
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env", override=False)

from core.order_router import OrderRouter
from data.broker_client import UnifiedBrokerClient
from data.db_models import DatabaseManager
from data.universe_selector import build_and_cache_dynamic_universe
from scripts.runtime_common import (
    project_config,
    save_market_filters,
    write_runtime_marker,
)
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
        capital = 0.0
        errors.append("PAPER_STARTING_CAPITAL must be a positive rupee amount")

    if not os.getenv("UPSTOX_ANALYTICS_TOKEN"):
        errors.append("UPSTOX_ANALYTICS_TOKEN is missing from project .env")

    tg = TelegramClient()
    if not tg.is_configured():
        errors.append(
            "Telegram requires TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID "
            "and TELEGRAM_ALLOWED_USER_ID"
        )

    broker = UnifiedBrokerClient(paper_mode=True)
    ok, msg = broker.validate_readonly_access()
    if not ok:
        errors.append(msg)

    symbols = []
    universe_meta = {}
    if not errors:
        try:
            universe_cfg = project_config("universe.yaml")["universe"]
            symbols, universe_meta = build_and_cache_dynamic_universe(
                broker,
                universe_cfg,
            )
        except Exception as exc:
            errors.append(
                f"DYNAMIC_UNIVERSE_BUILD_FAILED:{type(exc).__name__}:{exc}"
            )

    if not errors:
        for symbol in symbols:
            try:
                broker.resolve_instrument_key(symbol)
            except Exception as exc:
                errors.append(str(exc))

    if not errors:
        try:
            halted, corp = broker.get_trading_halts_and_corp_actions(symbols)
        except RuntimeError as e:
            if str(e) == "SUSPENDED_INSTRUMENT_DATA_IMPLAUSIBLE":
                print("PREFLIGHT FAILED: suspended-instrument data source returned an implausible "
                      "halt ratio — see logs for matched symbols. Not proceeding with a corrupted "
                      "market filter.")
                sys.exit(1)
            raise
        save_market_filters(halted, corp)
        try:
            db = DatabaseManager()
            db.record_preflight_success()
        except Exception:
            pass

    if errors:
        print("PREFLIGHT FAILED")
        for err in errors:
            print(f"- {err}")
        raise SystemExit(1)

    print("PREFLIGHT PASSED")
    print("mode=PAPER")
    print(f"paper_starting_capital=₹{capital:,.2f}")
    print(f"universe={len(symbols)}")
    print(f"universe_mode={universe_meta.get('mode')}")
    print(f"universe_source={universe_meta.get('source')}")
    print(
        "universe_eligible="
        f"{universe_meta.get('eligible_count', len(symbols))}"
    )
    print("upstox_analytics_token=VALID")
    print("upstox_live_ltp=OK")
    print("upstox_historical_data=OK")
    print("telegram=allowlisted long-polling configured")
    print(f"live_order_path={OrderRouter.live_order_path_status()}")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        write_runtime_marker(
            "preflight",
            "FAILED",
            f"{type(exc).__name__}:{exc}",
        )
        raise
    else:
        write_runtime_marker(
            "preflight",
            "SUCCESS",
            "Dynamic Top-100 universe and market filters ready",
        )
