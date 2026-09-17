#!/usr/bin/env python3
import os
import sys
import yaml
import logging
import pandas as pd
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.models import RegimeType
from core.regime_filter import evaluate_regime
from core.screener import Screener
from core.risk_governor import RiskGovernor
from core.circuit_tracker import CircuitTracker
from core.order_router import OrderRouter
from telegram_bot.approval_gate import ApprovalGate
from data.db_models import DatabaseManager
from data.broker_client import UnifiedBrokerClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("eod_screener")

def main():
    logger.info("Running EOD Screener (15:45 - 16:05 IST)...")
    db = DatabaseManager()

    # 1. Fetch real broker account health, cash, and net equity
    broker = UnifiedBrokerClient(paper_mode=True)
    broker_ok, available_cash, equity_now, broker_msg = broker.get_account_health()

    if not broker_ok:
        logger.error(f"Broker health check failed: {broker_msg} — skipping cycle entirely")
        return # Fail closed: do not proceed with any signal validation

    logger.info(f"Broker Account OK. Cash: ₹{available_cash:,.2f}, Net Equity: ₹{equity_now:,.2f}")

    halted_symbols, corp_actions = broker.get_trading_halts_and_corp_actions()

    # 2. Query open positions and refresh LTPs
    open_positions = db.get_open_positions()
    incomplete_prices = False

    for pos in open_positions:
        ltp = broker.get_ltp(pos.symbol)
        if ltp is None:
            logger.warning(f"No live price available for open position {pos.symbol}; MTM incomplete")
            incomplete_prices = True
            continue
        pos.current_price = ltp

    if incomplete_prices and len(open_positions) > 0 and (sum(1 for p in open_positions if p.current_price is None) / len(open_positions)) > 0.5:
        logger.error("Majority of open position prices failed LTP fetch — failing closed for safety")
        return

    # 3. Compute MTM Drawdown Exposure
    circuit_tracker = CircuitTracker(db)
    circuit_tracker.check_and_update_rollover_state()
    daily_mtm_pnl, weekly_mtm_pnl, monthly_mtm_pnl = circuit_tracker.compute_mark_to_market_pnl(open_positions)

    logger.info(f"MTM Drawdown Exposure: Daily=₹{daily_mtm_pnl:.2f}, Weekly=₹{weekly_mtm_pnl:.2f}, Monthly=₹{monthly_mtm_pnl:.2f}")

    # Load Universe
    config_dir = os.path.join(os.path.dirname(__file__), "..", "config")
    with open(os.path.join(config_dir, "universe.yaml")) as f:
        uni = yaml.safe_load(f)
    symbols = uni['universe']['equities'] + uni['universe']['etfs']

    dates = pd.date_range(end=datetime.now(), periods=210, freq='D')
    nifty_df = pd.DataFrame({
        'timestamp': dates,
        'open': 24000.0,
        'high': 24200.0,
        'low': 23800.0,
        'close': 24100.0,
        'volume': 5000000
    })

    regime, rationale = evaluate_regime(nifty_df)
    logger.info(f"Evaluated Regime: {regime.value} ({rationale})")

    symbol_data = {}
    for s in symbols:
        prices = [100.0 + i * 0.5 for i in range(210)]
        df_s = pd.DataFrame({
            'timestamp': dates,
            'open': prices,
            'high': [p + 2.0 for p in prices],
            'low': [p - 1.0 for p in prices],
            'close': [p + 1.0 for p in prices],
            'volume': [100000 for _ in range(210)]
        })
        symbol_data[s] = df_s

    screener = Screener(universe_symbols=symbols)
    signals = screener.run_eod_screen(symbol_data, nifty_df, regime)

    logger.info(f"EOD Screener generated {len(signals)} candidate signal(s).")

    governor = RiskGovernor()
    approval_gate = ApprovalGate()
    router = OrderRouter(live_mode=False)

    for sig in signals:
        risk_res = governor.validate_signal_against_invariants(
            signal=sig,
            equity_now=equity_now, # Live broker net equity
            available_cash=available_cash, # Live broker cash
            open_positions=open_positions,
            daily_pnl=daily_mtm_pnl,
            weekly_pnl=weekly_mtm_pnl,
            monthly_pnl=monthly_mtm_pnl,
            consecutive_losses=0,
            last_loss_time=None,
            halted_symbols=halted_symbols,
            corporate_action_symbols=corp_actions,
            broker_account_ok=broker_ok,
            candle_interval_seconds=86400
        )

        if not risk_res.passed:
            logger.info(f"Signal for {sig.symbol} BLOCKED by Risk Governor: {risk_res.reason_code}")
            db.record_blocked_signal(sig.symbol, sig.side.value, sig.entry_price, sig.stop_price, risk_res.reason_code)
            continue

        logger.info("Signal passed Risk Governor! Executing 120s Telegram Approval Workflow...")
        gate_res = approval_gate.process_signal(sig, risk_res)
        
        if gate_res['action'] == "APPROVED":
            order = router.route_order(sig, risk_res.computed_qty, gate_res['auto_executed_on_timeout'])
            logger.info(f"Order Executed! Order ID: {order.order_id}, Fill Price: ₹{order.filled_price:.2f}")

if __name__ == "__main__":
    main()
