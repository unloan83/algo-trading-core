from datetime import datetime
from data.db_models import DatabaseManager
from backtest.cost_model import calculate_indian_transaction_costs
from core.time_utils import now_ist_naive


class ImmutableTradeJournal:
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager

    def record_trade_exit(
        self,
        position_id: str,
        symbol: str,
        side: str,
        qty: int,
        entry_price: float,
        exit_price: float,
        entry_time: datetime,
        exit_time: datetime,
        exit_reason: str,
        entry_signal_price: float | None = None,
        exit_signal_price: float | None = None,
        auto_executed_on_timeout: bool = False,
        is_paper: bool = True,
        is_intraday: bool = False,
        slippage_already_applied: bool = False,
    ) -> int:
        gross_pnl, net_pnl, total_costs, slippage_cost = calculate_indian_transaction_costs(
            buy_price=entry_price if side == "BUY" else exit_price,
            sell_price=exit_price if side == "BUY" else entry_price,
            qty=qty,
            is_intraday=is_intraday,
            slippage_pct=0.0 if slippage_already_applied else 0.05,
        )
        if slippage_already_applied:
            if not entry_signal_price or not exit_signal_price:
                raise ValueError(
                    "EMBEDDED_SLIPPAGE_REQUIRES_ENTRY_AND_EXIT_SIGNAL_PRICES"
                )
            if side == "BUY":
                entry_slippage = entry_price - entry_signal_price
                exit_slippage = exit_signal_price - exit_price
            else:
                entry_slippage = entry_signal_price - entry_price
                exit_slippage = exit_price - exit_signal_price
            slippage_bps = (
                (entry_slippage / entry_signal_price)
                + (exit_slippage / exit_signal_price)
            ) * 10000.0
        else:
            slippage_bps = (
                (slippage_cost / (entry_price * qty)) * 10000.0
                if entry_price > 0 else 0.0
            )
        with self.db.get_connection() as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO trade_journal (
                    position_id, symbol, side, qty, entry_price, exit_price,
                    entry_time, exit_time, gross_pnl, net_pnl, slippage_bps,
                    transaction_costs, exit_reason, auto_executed_on_timeout, is_paper
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                position_id, symbol, side, qty, entry_price, exit_price,
                now_ist_naive(entry_time).isoformat(),
                now_ist_naive(exit_time).isoformat(), gross_pnl, net_pnl,
                slippage_bps, total_costs, exit_reason,
                int(auto_executed_on_timeout), int(is_paper),
            ))
            conn.commit()
            last_id = c.lastrowid

        if net_pnl < 0:
            self.db.increment_consecutive_losses(now=exit_time)
        else:
            self.db.reset_consecutive_losses(now=exit_time)
        return last_id
