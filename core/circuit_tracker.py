from datetime import datetime, date
from typing import List, Tuple, Dict, Any, Optional
from data.db_models import DatabaseManager
from core.models import Position, Side

class CircuitTracker:
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager

    def compute_mark_to_market_pnl(
        self,
        open_positions: List[Position],
        as_of_date: Optional[date] = None
    ) -> Tuple[float, float, float]:
        """
        Computes (daily_total_pnl, weekly_total_pnl, monthly_total_pnl)
        including both Realized P&L from trade_journal and Mark-to-Market (MTM) Unrealized P&L on open positions.
        """
        target_date = as_of_date or datetime.now().date()
        target_year, target_week, _ = target_date.isocalendar()
        target_month = target_date.month
        target_year_num = target_date.year

        # 1. Unrealized MTM P&L on currently open positions
        open_mtm_pnl = 0.0
        for pos in open_positions:
            if pos.side == Side.BUY:
                pos_mtm = (pos.current_price - pos.entry_price) * pos.qty
            else:
                pos_mtm = (pos.entry_price - pos.current_price) * pos.qty
            open_mtm_pnl += pos_mtm

        # 2. Query Realized P&L from trade_journal database table
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT exit_time, net_pnl FROM trade_journal")
            rows = cursor.fetchall()

        daily_realized = 0.0
        weekly_realized = 0.0
        monthly_realized = 0.0

        for row in rows:
            exit_dt = datetime.fromisoformat(row['exit_time']).date()
            exit_year, exit_week, _ = exit_dt.isocalendar()
            exit_month = exit_dt.month
            exit_year_num = exit_dt.year
            net_pnl = float(row['net_pnl'])

            if exit_dt == target_date:
                daily_realized += net_pnl
            if (exit_year, exit_week) == (target_year, target_week):
                weekly_realized += net_pnl
            if (exit_year_num, exit_month) == (target_year_num, target_month):
                monthly_realized += net_pnl

        daily_total_pnl = daily_realized + open_mtm_pnl
        weekly_total_pnl = weekly_realized + open_mtm_pnl
        monthly_total_pnl = monthly_realized + open_mtm_pnl

        return daily_total_pnl, weekly_total_pnl, monthly_total_pnl

    def check_and_update_rollover_state(self, current_dt: Optional[datetime] = None) -> Dict[str, Any]:
        """
        Handles exact daily, weekly (Monday ISO week), and monthly (1st of month) boundary resets.
        Consecutive loss counter state is carried forward verbatim and owned ONLY by trade_journal.py.
        """
        now_dt = current_dt or datetime.now()
        now_date = now_dt.date()
        today_str = now_date.isoformat()
        iso_year, iso_week, _ = now_date.isocalendar()

        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM circuit_breaker_state ORDER BY date DESC LIMIT 1")
            last_row = cursor.fetchone()

            if not last_row:
                cursor.execute("""
                INSERT INTO circuit_breaker_state (date, daily_pnl, weekly_pnl, monthly_pnl, consecutive_losses, daily_circuit_hit)
                VALUES (?, 0.0, 0.0, 0.0, 0, 0)
                """, (today_str,))
                conn.commit()
                return {"daily_reset": True, "weekly_reset": True, "monthly_reset": True}

            last_date = datetime.fromisoformat(last_row['date']).date()
            last_iso_year, last_iso_week, _ = last_date.isocalendar()

            daily_reset = now_date != last_date
            weekly_reset = (iso_year, iso_week) != (last_iso_year, last_iso_week)
            monthly_reset = (now_date.year, now_date.month) != (last_date.year, last_date.month)

            if daily_reset:
                cursor.execute("""
                INSERT OR REPLACE INTO circuit_breaker_state (
                    date, daily_pnl, weekly_pnl, monthly_pnl,
                    consecutive_losses, last_loss_time,
                    daily_circuit_hit, weekly_circuit_hit, monthly_circuit_hit
                ) VALUES (?, 0.0, ?, ?, ?, ?, 0, ?, ?);
                """, (
                    today_str,
                    0.0 if weekly_reset else last_row['weekly_pnl'],
                    0.0 if monthly_reset else last_row['monthly_pnl'],
                    last_row['consecutive_losses'],  # Carried forward verbatim
                    last_row['last_loss_time'],      # Carried forward verbatim
                    0 if weekly_reset else last_row['weekly_circuit_hit'],
                    0 if monthly_reset else last_row['monthly_circuit_hit']
                ))
                conn.commit()

            return {
                "daily_reset": daily_reset,
                "weekly_reset": weekly_reset,
                "monthly_reset": monthly_reset,
                "date": today_str
            }
