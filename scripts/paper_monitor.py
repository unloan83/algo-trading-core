#!/usr/bin/env python3
import logging
import os
import sys
from datetime import datetime, time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.models import Side
from core.order_router import OrderRouter
from data.broker_client import UnifiedBrokerClient
from data.db_models import DatabaseManager
from journal.trade_journal import ImmutableTradeJournal
from scripts.runtime_common import now_ist_naive, project_config
from telegram_bot.notifications import NotificationService
from telegram_bot.telegram_client import TelegramClient

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("paper_monitor")


def _parse_hhmm(v: str) -> time:
    hh, mm = map(int, v.split(":"))
    return time(hh, mm)


def main():
    db = DatabaseManager()
    positions = db.get_open_positions()
    if not positions:
        return

    now = now_ist_naive()
    if not (time(9, 15) <= now.time() <= time(15, 30)):
        return
    broker = UnifiedBrokerClient(paper_mode=True)
    ok, msg = broker.validate_readonly_access()
    if not broker.is_nse_trading_day(now.date()):
        return
    if not ok:
        log.error(msg)
        return

    router = OrderRouter(live_mode=False)
    journal = ImmutableTradeJournal(db)
    tg = TelegramClient()
    notifier = NotificationService(
        send_fn=(lambda text: tg.send_message(text)) if tg.is_configured() else None
    )
    square_off = _parse_hhmm(
        project_config("timing.yaml")["timing"]["intraday_scan"]["mandatory_square_off"]
    )

    for pos in positions:
        ltp = broker.get_ltp(pos.symbol)
        if ltp is None:
            continue
        db.update_position_price(pos.position_id, ltp)

        reason = None
        if pos.side == Side.BUY:
            if ltp <= pos.stop_price:
                reason = "STOP_LOSS"
            elif ltp >= pos.target_price:
                reason = "TARGET"
        else:
            if ltp >= pos.stop_price:
                reason = "STOP_LOSS"
            elif ltp <= pos.target_price:
                reason = "TARGET"

        if pos.is_intraday and now.time() >= square_off:
            reason = reason or "MANDATORY_INTRADAY_SQUARE_OFF"

        if not reason:
            continue

        exit_price = router.simulator.simulate_exit_price(pos.side, ltp)
        jid = journal.record_trade_exit(
            position_id=pos.position_id,
            symbol=pos.symbol,
            side=pos.side.value,
            qty=pos.qty,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            entry_time=pos.opened_at,
            exit_time=now,
            exit_reason=reason,
            is_paper=True,
            is_intraday=pos.is_intraday,
            slippage_already_applied=True,
        )
        # Journal has authoritative net P&L; read it back before closing position row.
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT net_pnl FROM trade_journal WHERE journal_id=?", (jid,)
            ).fetchone()
        net_pnl = float(row["net_pnl"])
        db.mark_position_closed(pos.position_id, net_pnl)
        notifier.notify_exit(
            pos.symbol, pos.side.value, pos.qty, pos.entry_price,
            exit_price, 0.0, net_pnl, reason
        )
        log.info("Closed PAPER position %s: %s, net %.2f", pos.symbol, reason, net_pnl)


if __name__ == "__main__":
    main()
