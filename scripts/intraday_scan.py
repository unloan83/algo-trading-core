#!/usr/bin/env python3
import logging
import os
import sys
from datetime import time, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.circuit_tracker import CircuitTracker
from core.paper_engine import (
    execute_paper_signal,
    paper_starting_capital,
    pending_row_to_signal,
)
from core.regime_filter import evaluate_regime
from core.screener import Screener
from core.order_router import OrderRouter
from data.broker_client import UnifiedBrokerClient
from data.db_models import DatabaseManager
from data.observability import (
    feature_snapshot,
    new_scan_run,
    safe_record_scan_run,
    safe_set_signal_block_reason,
)
from data.universe_selector import load_active_universe
from scripts.runtime_common import (
    build_risk_governor,
    now_ist_naive,
    project_config,
    load_market_filters,
    write_runtime_marker,
)
from telegram_bot.approval_gate import ApprovalGate, compute_signal_id
from telegram_bot.telegram_client import TelegramClient

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("intraday_scan")
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


def _parse_hhmm(value: str) -> time:
    hh, mm = map(int, value.split(":"))
    return time(hh, mm)


def _risk_check(governor, sig, account, positions, pnl, halted, corp, now, candle_seconds, circuit_state):
    cash, equity = account
    daily, weekly, monthly = pnl
    return governor.validate_signal_against_invariants(
        signal=sig,
        equity_now=equity,
        available_cash=cash,
        open_positions=positions,
        daily_pnl=daily,
        weekly_pnl=weekly,
        monthly_pnl=monthly,
        consecutive_losses=circuit_state["consecutive_losses"],
        last_loss_time=circuit_state["last_loss_time"],
        halted_symbols=halted,
        corporate_action_symbols=corp,
        broker_account_ok=True,
        candle_interval_seconds=candle_seconds,
        now=now,
    )


def main():
    global _active_scan_run
    now = now_ist_naive()
    timing = project_config("timing.yaml")["timing"]["intraday_scan"]
    start = _parse_hhmm(timing["entry_window_start"])
    end = _parse_hhmm(timing["entry_window_end"])
    if not (start <= now.time() <= end):
        return

    _active_scan_run = new_scan_run("intraday", now)

    broker = UnifiedBrokerClient(paper_mode=True)
    ok, msg = broker.validate_readonly_access()
    if not ok:
        raise SystemExit(msg)
    if not broker.is_nse_trading_day(now.date()):
        write_runtime_marker("intraday_scan", "SKIPPED", "NSE not trading today")
        _active_scan_run.status = "SKIPPED"
        _finish_scan()
        return

    db = DatabaseManager()
    universe_cfg = project_config("universe.yaml")["universe"]
    symbols = load_active_universe(universe_cfg)

    open_positions = db.get_open_positions()
    for pos in open_positions:
        ltp = broker.get_ltp(pos.symbol)
        if ltp is not None:
            pos.current_price = ltp
            db.update_position_price(pos.position_id, ltp)

    capital = paper_starting_capital()
    account = db.paper_account(capital, open_positions)
    tracker = CircuitTracker(db)
    tracker.check_and_update_rollover_state(now)
    pnl = tracker.compute_mark_to_market_pnl(open_positions, as_of_date=now.date())
    halted, corp = load_market_filters()
    governor = build_risk_governor()
    circuit_state = db.get_latest_circuit_state()
    router = OrderRouter(live_mode=False)
    risk_cfg = project_config("risk_limits.yaml")

    telegram = TelegramClient(callback_store=db)
    if not telegram.is_configured():
        raise SystemExit("TELEGRAM_NOT_CONFIGURED_OR_ALLOWLIST_MISSING")
    approval = ApprovalGate(
        timeout_seconds=int(project_config("timing.yaml")["timing"]["telegram_approval_timeout_seconds"]),
        default_action_on_timeout=risk_cfg["default_action_on_timeout"],
    )

    for row in db.get_pending_signals():
        if any(p.symbol == row["symbol"] for p in open_positions):
            db.mark_pending_signal(row["pending_id"], "SKIPPED_DUPLICATE_POSITION")
            continue
        ltp = broker.get_ltp(row["symbol"])
        if not ltp:
            continue
        try:
            sig = pending_row_to_signal(row, ltp, float(timing["gap_filter_pct"]))
        except ValueError as exc:
            db.mark_pending_signal(row["pending_id"], f"SKIPPED:{exc}")
            continue
        risk = _risk_check(
            governor, sig, account, open_positions, pnl, halted, corp, now, 86400, circuit_state
        )
        if not risk.passed:
            db.mark_pending_signal(row["pending_id"], f"BLOCKED:{risk.reason_code}")
            continue
        pos = execute_paper_signal(
            db, router, sig, risk.computed_qty,
            auto_executed_on_timeout=bool(row["auto_executed_on_timeout"]),
            is_intraday=False,
        )
        db.mark_pending_signal(row["pending_id"], "EXECUTED")
        open_positions.append(pos)
        account = db.paper_account(capital, open_positions)
        log.info("Opened next-session PAPER position: %s", sig.symbol)

    nifty = broker.get_intraday_history("NIFTY 50", interval_minutes=5, lookback_days=7)
    cutoff = now - timedelta(minutes=5)
    nifty = nifty[nifty["timestamp"] <= cutoff].reset_index(drop=True)
    if len(nifty) < 50:
        raise RuntimeError(f"INSUFFICIENT_5MIN_NIFTY_HISTORY:{len(nifty)}")
    regime, rationale = evaluate_regime(nifty)
    db.record_regime(
        regime.value,
        f"intraday:{rationale}",
        observed_at=now,
        input_asof_date=nifty.iloc[-1]["timestamp"].date().isoformat(),
        input_candle_count=len(nifty),
        input_asof_stale=False,
        source_mode="intraday",
        input_symbol="NIFTY 50",
        input_timeframe="5minute",
    )

    symbol_data = {}
    for symbol in symbols:
        df = broker.get_intraday_history(symbol, interval_minutes=5, lookback_days=7)
        df = df[df["timestamp"] <= cutoff].reset_index(drop=True)
        if len(df) >= 25:
            symbol_data[symbol] = df

    signals = Screener(symbols).run_intraday_scan(symbol_data, nifty, regime)
    _active_scan_run.evaluated = len(symbol_data)
    _active_scan_run.passed = len(signals)
    _active_scan_run.model = "breakout"
    features = {
        sig.symbol: feature_snapshot(sig, symbol_data[sig.symbol], nifty)
        for sig in signals
    }
    signal_ids = db.record_signal_evaluations(
        signals,
        evaluated_at=now,
        scan_id=_active_scan_run.scan_id,
        strategy_version=_active_scan_run.strategy_version,
        intraday_regime=regime.value,
        feature_json_by_symbol=features,
    )
    signal_id_by_object = {
        id(sig): signal_id for sig, signal_id in zip(signals, signal_ids)
    }
    max_orders = int(timing["max_orders_per_day"])
    for index, sig in enumerate(signals):
        if db.count_paper_orders_on_date(now.date().isoformat(), intraday_only=True) >= max_orders:
            log.info("Intraday paper-order cap reached (%d).", max_orders)
            _active_scan_run.skip("daily_cap", len(signals) - index)
            break
        if any(p.symbol == sig.symbol for p in open_positions):
            _active_scan_run.skip("existing_position")
            continue

        account = db.paper_account(capital, open_positions)
        pnl = tracker.compute_mark_to_market_pnl(open_positions, as_of_date=now.date())
        risk = _risk_check(
            governor, sig, account, open_positions, pnl, halted, corp, now, 360, circuit_state
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

        decision = approval.process_signal(
            sig, risk,
            telegram_send_fn=telegram.send_approval,
            human_response_callback=lambda signal_id=compute_signal_id(sig): (
                telegram.poll_callback_query(signal_id=signal_id)
            ),
        )

        if decision["action"] == "APPROVED":
            pos = execute_paper_signal(
                db, router, sig, risk.computed_qty,
                auto_executed_on_timeout=decision.get("auto_executed_on_timeout", False),
                is_intraday=True,
            )
            open_positions.append(pos)
            _active_scan_run.ordered += 1
            log.info("Opened intraday PAPER position: %s", sig.symbol)
        else:
            _active_scan_run.skip("approval")

    write_runtime_marker(
        "intraday_scan",
        "SUCCESS",
        f"universe={len(symbols)} data_ready={len(symbol_data)} signals={len(signals)}",
    )
    _finish_scan(db.db_path)


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        _finish_scan(exc=exc)
        write_runtime_marker(
            "intraday_scan",
            "FAILED",
            f"{type(exc).__name__}:{exc}",
        )
        raise
