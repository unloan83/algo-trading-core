#!/usr/bin/env python3
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.circuit_tracker import CircuitTracker
from core.paper_engine import paper_starting_capital
from core.regime_filter import evaluate_regime
from core.screener import Screener
from data.broker_client import UnifiedBrokerClient
from data.db_models import DatabaseManager
from scripts.runtime_common import build_risk_governor, now_ist_naive, project_config, save_market_filters
from telegram_bot.approval_gate import ApprovalGate
from telegram_bot.telegram_client import TelegramClient

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("eod_screener")


def main():
    now = now_ist_naive()
    broker = UnifiedBrokerClient(paper_mode=True)
    db = DatabaseManager()

    ok, msg = broker.validate_readonly_access()
    if not ok:
        raise SystemExit(msg)
    if not broker.is_nse_trading_day(now.date()):
        log.info("NSE not trading today; EOD scan skipped.")
        return

    universe_cfg = project_config("universe.yaml")["universe"]
    symbols = universe_cfg["equities"] + universe_cfg["etfs"]

    nifty_df = broker.get_historical_data("NIFTY 50", days=210)
    if len(nifty_df) < 200:
        raise SystemExit(f"INSUFFICIENT_NIFTY_DAILY_DATA:{len(nifty_df)}")

    symbol_data = {}
    for symbol in symbols:
        df = broker.get_historical_data(symbol, days=210)
        if len(df) >= 25:
            symbol_data[symbol] = df

    regime, rationale = evaluate_regime(nifty_df)
    db.record_regime(regime.value, rationale)
    signals = Screener(symbols).run_eod_screen(symbol_data, nifty_df, regime)
    log.info("Real-data EOD candidates: %d", len(signals))

    open_positions = db.get_open_positions()
    for pos in open_positions:
        ltp = broker.get_ltp(pos.symbol)
        if ltp is not None:
            pos.current_price = ltp
            db.update_position_price(pos.position_id, ltp)

    capital = paper_starting_capital()
    available_cash, equity_now = db.paper_account(capital, open_positions)
    tracker = CircuitTracker(db)
    tracker.check_and_update_rollover_state(now)
    daily_pnl, weekly_pnl, monthly_pnl = tracker.compute_mark_to_market_pnl(
        open_positions, as_of_date=now.date()
    )

    halted, corp_actions = broker.get_trading_halts_and_corp_actions(symbols)
    save_market_filters(halted, corp_actions)
    governor = build_risk_governor()
    circuit_state = db.get_latest_circuit_state()

    risk_cfg = project_config("risk_limits.yaml")
    timing_cfg = project_config("timing.yaml")["timing"]
    telegram = TelegramClient()
    approval = ApprovalGate(
        timeout_seconds=int(timing_cfg["telegram_approval_timeout_seconds"]),
        default_action_on_timeout=risk_cfg["default_action_on_timeout"],
    )

    for sig in signals:
        risk = governor.validate_signal_against_invariants(
            signal=sig,
            equity_now=equity_now,
            available_cash=available_cash,
            open_positions=open_positions,
            daily_pnl=daily_pnl,
            weekly_pnl=weekly_pnl,
            monthly_pnl=monthly_pnl,
            consecutive_losses=circuit_state["consecutive_losses"],
            last_loss_time=circuit_state["last_loss_time"],
            halted_symbols=halted,
            corporate_action_symbols=corp_actions,
            broker_account_ok=True,
            candle_interval_seconds=86400,
            now=now,
        )
        if not risk.passed:
            db.record_blocked_signal(
                sig.symbol, sig.side.value, sig.entry_price, sig.stop_price, risk.reason_code
            )
            continue

        if not telegram.is_configured():
            raise SystemExit("TELEGRAM_NOT_CONFIGURED_OR_ALLOWLIST_MISSING")
        decision = approval.process_signal(
            sig,
            risk,
            telegram_send_fn=telegram.send_approval,
            human_response_callback=telegram.poll_callback_query,
        )

        if decision["action"] == "APPROVED":
            pending_id = db.save_pending_signal(sig)
            log.info(
                "Approved EOD signal queued for next-session execution: %s (%s)",
                sig.symbol,
                pending_id,
            )


if __name__ == "__main__":
    main()
