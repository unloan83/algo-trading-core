import sqlite3
import os
from datetime import datetime, date
from typing import Optional, List, Dict, Any
from core.models import Position, Side

class DatabaseManager:
    def __init__(self, db_path: str = "trading_system.db"):
        self.db_path = db_path
        self._init_db()

    def get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                signal_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                stop_price REAL NOT NULL,
                target_price REAL NOT NULL,
                model_name TEXT NOT NULL,
                regime TEXT NOT NULL,
                rationale TEXT,
                timestamp TEXT NOT NULL
            );
            """)

            cursor.execute("""
            CREATE TABLE IF NOT EXISTS blocked_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                stop_price REAL NOT NULL,
                reason_code TEXT NOT NULL,
                timestamp TEXT NOT NULL
            );
            """)

            cursor.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                qty INTEGER NOT NULL,
                entry_price REAL NOT NULL,
                stop_price REAL NOT NULL,
                target_price REAL NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                broker_order_id TEXT,
                filled_price REAL,
                auto_executed_on_timeout INTEGER DEFAULT 0,
                is_paper INTEGER DEFAULT 1
            );
            """)

            cursor.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                position_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                qty INTEGER NOT NULL,
                entry_price REAL NOT NULL,
                current_price REAL NOT NULL,
                stop_price REAL NOT NULL,
                target_price REAL NOT NULL,
                unrealized_pnl REAL DEFAULT 0.0,
                realized_pnl REAL DEFAULT 0.0,
                opened_at TEXT NOT NULL,
                status TEXT DEFAULT 'OPEN',
                is_paper INTEGER DEFAULT 1
            );
            """)

            cursor.execute("PRAGMA table_info(positions);")
            cols = [info[1] for info in cursor.fetchall()]
            if 'status' not in cols:
                cursor.execute("ALTER TABLE positions ADD COLUMN status TEXT DEFAULT 'OPEN';")

            cursor.execute("""
            CREATE TABLE IF NOT EXISTS trade_journal (
                journal_id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                qty INTEGER NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                entry_time TEXT NOT NULL,
                exit_time TEXT NOT NULL,
                gross_pnl REAL NOT NULL,
                net_pnl REAL NOT NULL,
                slippage_bps REAL DEFAULT 0.0,
                transaction_costs REAL DEFAULT 0.0,
                exit_reason TEXT NOT NULL,
                auto_executed_on_timeout INTEGER DEFAULT 0,
                is_paper INTEGER DEFAULT 1
            );
            """)

            cursor.execute("""
            CREATE TABLE IF NOT EXISTS regime_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                regime TEXT NOT NULL,
                rationale TEXT NOT NULL,
                timestamp TEXT NOT NULL
            );
            """)

            cursor.execute("""
            CREATE TABLE IF NOT EXISTS circuit_breaker_state (
                date TEXT PRIMARY KEY,
                daily_pnl REAL DEFAULT 0.0,
                weekly_pnl REAL DEFAULT 0.0,
                monthly_pnl REAL DEFAULT 0.0,
                consecutive_losses INTEGER DEFAULT 0,
                last_loss_time TEXT,
                daily_circuit_hit INTEGER DEFAULT 0,
                weekly_circuit_hit INTEGER DEFAULT 0,
                monthly_circuit_hit INTEGER DEFAULT 0
            );
            """)

            conn.commit()

    def get_open_positions(self) -> List[Position]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM positions WHERE status = 'OPEN'")
            rows = cursor.fetchall()
            positions = []
            for r in rows:
                positions.append(Position(
                    position_id=r['position_id'],
                    symbol=r['symbol'],
                    side=Side(r['side']),
                    qty=r['qty'],
                    entry_price=r['entry_price'],
                    current_price=r['current_price'],
                    stop_price=r['stop_price'],
                    target_price=r['target_price'],
                    unrealized_pnl=r['unrealized_pnl'],
                    realized_pnl=r['realized_pnl'],
                    opened_at=datetime.fromisoformat(r['opened_at']),
                    is_paper=bool(r['is_paper'])
                ))
            return positions

    def increment_consecutive_losses(self, now: Optional[datetime] = None):
        current_time = (now or datetime.now()).isoformat()
        today_str = (now or datetime.now()).date().isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            UPDATE circuit_breaker_state
            SET consecutive_losses = consecutive_losses + 1, last_loss_time = ?
            WHERE date = ?;
            """, (current_time, today_str))
            conn.commit()

    def reset_consecutive_losses(self, now: Optional[datetime] = None):
        today_str = (now or datetime.now()).date().isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            UPDATE circuit_breaker_state
            SET consecutive_losses = 0
            WHERE date = ?;
            """, (today_str,))
            conn.commit()

    def record_blocked_signal(self, symbol: str, side: str, entry_price: float, stop_price: float, reason_code: str):
        with self.get_connection() as conn:
            conn.cursor().execute("""
            INSERT INTO blocked_signals (symbol, side, entry_price, stop_price, reason_code, timestamp)
            VALUES (?, ?, ?, ?, ?, ?);
            """, (symbol, side, entry_price, stop_price, reason_code, datetime.now().isoformat()))
            conn.commit()

    def record_regime(self, regime: str, rationale: str):
        with self.get_connection() as conn:
            conn.cursor().execute("""
            INSERT INTO regime_log (regime, rationale, timestamp)
            VALUES (?, ?, ?);
            """, (regime, rationale, datetime.now().isoformat()))
            conn.commit()
