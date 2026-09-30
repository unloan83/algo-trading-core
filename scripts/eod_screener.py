#!/usr/bin/env python3
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.circuit_tracker import CircuitTracker
from core.paper_engine import paper_starting_capital
from core.regime_filter import evaluate_regime
from core.screener import Screener
from data.broker_client import UnifiedBrokerClient
from data.db_models import DatabaseManager
from data.observability import (
    feature_snapshot,
    new_scan_run,
    safe_record_scan_run,
    safe_set_signal_block_reason,
)
from data.provenance import safe_snapshot_file
from data.universe_selector import load_active_universe
from scripts.runtime_common import (
    build_risk_governor,
    now_ist_naive,
    project_config,
    save_market_filters,
    write_runtime_marker,
)
from telegram_bot.approval_gate import ApprovalGate, compute_signal_id
from telegram_bot.telegram_client import TelegramClient

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("eod_screener")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
_active_scan_run = None


def _finish_scan(db_path: str = "trading_system.db", exc: BaseException = None):
    global _active_scan_run
    run = _active_scan_run
    if run is None:
        return
    if exc is not None:
        run.fail(exc)
    elif run.status == "RUNNING":
        run.status = "SUCCESS"
    safe_record_scan_run(db_path, run)
    _active_scan_run = None


def main():
    global _active_scan_run
    now = now_ist_naive()
    _active_scan_run = new_scan_run("eod", now)
    broker = UnifiedBrokerClient(paper_mode=True)
    db = DatabaseManager()

    ok, msg = broker.validate_readonly_access()
    if not ok:
        raise SystemExit(msg)
    if not broker.is_nse_trading_day(now.date()):
        log.info("NSE not trading today; EOD scan skipped.")
        write_runtime_marker("eod_screener", "SKIPPED", "NSE not trading today")
        _active_scan_run.status = "SKIPPED"
        _finish_scan(db.db_path)
        return

    universe_cfg = project_config("universe.yaml")["universe"]
    symbols = load_active_universe(universe_cfg)

    nifty_df = broker.get_historical_data("NIFTY 50", days=210)
    if len(nifty_df) < 200:
        raise SystemExit(f"INSUFFICIENT_NIFTY_DAILY_DATA:{len(nifty_df)}")

    symbol_data = {}
    for symbol in symbols:
        df = broker.get_historical_data(symbol, days=210)
        if len(df) >= 25:
            symbol_data[symbol] = df

    regime, rationale = evaluate_regime(nifty_df)
    input_asof = nifty_df.iloc[-1]["timestamp"].date()
    db.record_regime(
        regime.value,
        rationale,
        observed_at=now,
        input_asof_date=input_asof.isoformat(),
        input_candle_count=len(nifty_df),
        input_asof_stale=input_asof != now.date(),
        source_mode="eod",
        input_symbol="NIFTY 50",
        input_timeframe="1day",
    )
    signals = Screener(symbols).run_eod_screen(symbol_data, nifty_df, regime)
    _active_scan_run.evaluated = len(symbol_data)
    _active_scan_run.passed = len(signals)
    _active_scan_run.model = "mixed"
    features = {
        sig.symbol: feature_snapshot(sig, symbol_data[sig.symbol], nifty_df)
        for sig in signals
    }
    signal_ids = db.record_signal_evaluations(
        signals,
        evaluated_at=now,
        scan_id=_active_scan_run.scan_id,
        strategy_version=_active_scan_run.strategy_version,
        eod_regime=regime.value,
        feature_json_by_symbol=features,
    )
    signal_id_by_object = {
        id(sig): signal_id for sig, signal_id in zip(signals, signal_ids)
    }
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
    safe_snapshot_file(
        "suspended_feed_cache",
        PROJECT_ROOT / ".cache" / "upstox_suspended.json",
        session_date=now.date(),
    )
    safe_snapshot_file(
        "active_universe_cache",
        PROJECT_ROOT / ".cache" / "active_universe.json",
        session_date=now.date(),
    )
    save_market_filters(halted, corp_actions)
    governor = build_risk_governor()
    circuit_state = db.get_latest_circuit_state()

    risk_cfg = project_config("risk_limits.yaml")
    timing_cfg = project_config("timing.yaml")["timing"]
    telegram = TelegramClient(callback_store=db)
    approval = ApprovalGate(
        timeout_seconds=int(timing_cfg["telegram_approval_timeout_seconds"]),
        default_action_on_timeout=risk_cfg["default_action_on_timeout"],
    )

    approved_count = 0
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
            _active_scan_run.block(risk.reason_code)
            safe_set_signal_block_reason(
                db.db_path,
                signal_id_by_object.get(id(sig)),
                risk.reason_code,
            )
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
            human_response_callback=lambda signal_id=compute_signal_id(sig): (
                telegram.poll_callback_query(signal_id=signal_id)
            ),
        )

        if decision["action"] == "APPROVED":
            pending_id = db.save_pending_signal(
                sig,
                auto_executed_on_timeout=decision.get(
                    "auto_executed_on_timeout",
                    False,
                ),
            )
            approved_count += 1
            _active_scan_run.ordered += 1
            log.info(
                "Approved EOD signal queued for next-session execution: %s (%s)",
                sig.symbol,
                pending_id,
            )
        else:
            _active_scan_run.skip("approval")

    write_runtime_marker(
        "eod_screener",
        "SUCCESS",
        (
            f"universe={len(symbols)} data_ready={len(symbol_data)} "
            f"signals={len(signals)} approved={approved_count}"
        ),
    )
    _finish_scan(db.db_path)


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        _finish_scan(exc=exc)
        write_runtime_marker(
            "eod_screener",
            "FAILED",
            f"{type(exc).__name__}:{exc}",
        )
        raise
