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
        is_intraday: bool = False
    ) -> int:
        gross_pnl, net_pnl, total_costs, slippage_cost = calculate_indian_transaction_costs(
            buy_price=entry_price if side == "BUY" else exit_price,
            sell_price=exit_price if side == "BUY" else entry_price,
            qty=qty,
            is_intraday=is_intraday
        )

        slippage_bps = (slippage_cost / (entry_price * qty)) * 10000.0 if entry_price > 0 else 0.0

        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            INSERT INTO trade_journal (
                position_id, symbol, side, qty, entry_price, exit_price,
                entry_time, exit_time, gross_pnl, net_pnl, slippage_bps,
                transaction_costs, exit_reason, auto_executed_on_timeout, is_paper
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                position_id, symbol, side, qty, entry_price, exit_price,
                entry_time.isoformat(), exit_time.isoformat(), gross_pnl, net_pnl,
                slippage_bps, total_costs, exit_reason,
                1 if auto_executed_on_timeout else 0,
                1 if is_paper else 0
            ))
            conn.commit()
            last_id = cursor.lastrowid

        # Directly update consecutive loss streak based on actual trade outcome
        if net_pnl < 0:
            self.db.increment_consecutive_losses(now=exit_time)
        else:
            self.db.reset_consecutive_losses(now=exit_time) # Pass exit_time to update correct calendar row!

        return last_id
