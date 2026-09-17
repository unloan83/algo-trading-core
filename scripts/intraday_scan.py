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
logger = logging.getLogger("intraday_scan")

def main():
    logger.info("Running Opportunistic Intraday Scan...")
    db = DatabaseManager()

    # 1. Fetch real broker account health, cash, and net equity
    broker = UnifiedBrokerClient(paper_mode=True)
    broker_ok, available_cash, equity_now, broker_msg = broker.get_account_health()

    if not broker_ok:
        logger.error(f"Intraday scan halted: Broker account error ({broker_msg})")
        return # Fail closed

    logger.info(f"Broker Health OK. Cash: ₹{available_cash:,.2f}, Equity: ₹{equity_now:,.2f}")

    halted_symbols, corp_actions = broker.get_trading_halts_and_corp_actions()

    # 2. Query open positions and refresh LTPs
    open_positions = db.get_open_positions()
    for pos in open_positions:
        ltp = broker.get_ltp(pos.symbol)
        if ltp:
            pos.current_price = ltp

    # 3. Compute MTM Drawdown Exposure & Check Rollover
    circuit_tracker = CircuitTracker(db)
    circuit_tracker.check_and_update_rollover_state()
    daily_mtm_pnl, weekly_mtm_pnl, monthly_mtm_pnl = circuit_tracker.compute_mark_to_market_pnl(open_positions)

    logger.info(f"Intraday Scan active. 5-min candles (candle_interval_seconds=300). Max 6 orders/day cap.")
    logger.info(f"Current MTM Drawdown: Daily=₹{daily_mtm_pnl:.2f}, Weekly=₹{weekly_mtm_pnl:.2f}, Monthly=₹{monthly_mtm_pnl:.2f}")

if __name__ == "__main__":
    main()
