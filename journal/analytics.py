import pandas as pd
import numpy as np
from typing import Dict, Any
from data.db_models import DatabaseManager

class AnalyticsEngine:
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager

    def compute_performance_metrics(self) -> Dict[str, Any]:
        """
        Calculates performance analytics as a pure function of the trade_journal database table.
        """
        with self.db.get_connection() as conn:
            df = pd.read_sql_query("SELECT * FROM trade_journal", conn)

        if df.empty:
            return {
                "total_trades": 0,
                "win_rate_pct": 0.0,
                "profit_factor": 0.0,
                "net_pnl": 0.0,
                "gross_pnl": 0.0,
                "max_drawdown_pct": 0.0,
                "total_transaction_costs": 0.0
            }

        total_trades = len(df)
        winning_trades = df[df['net_pnl'] > 0]
        losing_trades = df[df['net_pnl'] < 0]

        win_rate = (len(winning_trades) / total_trades) * 100.0

        gross_profits = winning_trades['net_pnl'].sum()
        gross_losses = abs(losing_trades['net_pnl'].sum())

        profit_factor = (gross_profits / gross_losses) if gross_losses > 0 else (999.0 if gross_profits > 0 else 0.0)

        total_net_pnl = df['net_pnl'].sum()
        total_gross_pnl = df['gross_pnl'].sum()
        total_costs = df['transaction_costs'].sum()

        # Cumulative PnL and Max Drawdown calculation
        df['cum_pnl'] = df['net_pnl'].cumsum()
        peak = df['cum_pnl'].cummax()
        drawdown = peak - df['cum_pnl']
        max_drawdown = drawdown.max() if not drawdown.empty else 0.0

        return {
            "total_trades": total_trades,
            "win_rate_pct": float(win_rate),
            "profit_factor": float(profit_factor),
            "net_pnl": float(total_net_pnl),
            "gross_pnl": float(total_gross_pnl),
            "max_drawdown": float(max_drawdown),
            "total_transaction_costs": float(total_costs)
        }
