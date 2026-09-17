from datetime import datetime
from data.db_models import DatabaseManager
from backtest.cost_model import calculate_indian_transaction_costs


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
                entry_time.isoformat(), exit_time.isoformat(), gross_pnl, net_pnl,
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
