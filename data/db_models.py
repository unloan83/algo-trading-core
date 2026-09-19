import sqlite3
import os
import uuid
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple

from core.models import Position, Side, Signal, Order, RegimeType


class DatabaseManager:
    def __init__(self, db_path: str = "trading_system.db"):
        self.db_path = db_path
        self._init_db()

    def get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_column(self, cursor, table: str, column: str, ddl: str):
        cursor.execute(f"PRAGMA table_info({table});")
        cols = [info[1] for info in cursor.fetchall()]
        if column not in cols:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl};")

    def _init_db(self):
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute("""
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
            );""")
            c.execute("""
            CREATE TABLE IF NOT EXISTS blocked_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                stop_price REAL NOT NULL,
                reason_code TEXT NOT NULL,
                timestamp TEXT NOT NULL
            );""")
            c.execute("""
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
                is_paper INTEGER DEFAULT 1,
                is_intraday INTEGER DEFAULT 0
            );""")
            c.execute("""
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
                is_paper INTEGER DEFAULT 1,
                is_intraday INTEGER DEFAULT 0
            );""")
            c.execute("""
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
            );""")
            c.execute("""
            CREATE TABLE IF NOT EXISTS regime_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                regime TEXT NOT NULL,
                rationale TEXT NOT NULL,
                timestamp TEXT NOT NULL
            );""")
            c.execute("""
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
            );""")
            c.execute("""
            CREATE TABLE IF NOT EXISTS pending_signals (
                pending_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                stop_price REAL NOT NULL,
                target_price REAL NOT NULL,
                model_name TEXT NOT NULL,
                regime TEXT NOT NULL,
                rationale TEXT,
                signal_timestamp TEXT NOT NULL,
                approved_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING'
            );""")
            c.execute("""
            CREATE TABLE IF NOT EXISTS system_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                details TEXT,
                timestamp TEXT NOT NULL
            );""")
            c.execute("""
            CREATE TABLE IF NOT EXISTS telegram_callback_inbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                update_id INTEGER NOT NULL UNIQUE,
                action TEXT NOT NULL,
                signal_id TEXT,
                received_at TEXT NOT NULL
            );""")
            c.execute("""
            CREATE TABLE IF NOT EXISTS telegram_update_state (
                consumer TEXT PRIMARY KEY,
                next_offset INTEGER NOT NULL
            );""")
            self._ensure_column(c, "positions", "status", "TEXT DEFAULT 'OPEN'")
            self._ensure_column(c, "positions", "is_intraday", "INTEGER DEFAULT 0")
            self._ensure_column(c, "orders", "is_intraday", "INTEGER DEFAULT 0")
            conn.commit()

    def get_open_positions(self) -> List[Position]:
        with self.get_connection() as conn:
            rows = conn.execute("SELECT * FROM positions WHERE status = 'OPEN'").fetchall()
        return [
            Position(
                position_id=r["position_id"],
                symbol=r["symbol"],
                side=Side(r["side"]),
                qty=r["qty"],
                entry_price=r["entry_price"],
                current_price=r["current_price"],
                stop_price=r["stop_price"],
                target_price=r["target_price"],
                unrealized_pnl=r["unrealized_pnl"],
                realized_pnl=r["realized_pnl"],
                opened_at=datetime.fromisoformat(r["opened_at"]),
                is_paper=bool(r["is_paper"]),
                is_intraday=bool(r["is_intraday"]),
            )
            for r in rows
        ]

    def record_signal_evaluations(
        self,
        signals: List[Signal],
        evaluated_at: Optional[datetime] = None,
    ) -> None:
        timestamp = (evaluated_at or datetime.now()).isoformat()
        rows = [
            (
                f"evaluation_{uuid.uuid4().hex}",
                signal.symbol,
                signal.side.value,
                signal.entry_price,
                signal.stop_price,
                signal.target_price,
                signal.model_name,
                signal.regime.value,
                signal.rationale,
                timestamp,
            )
            for signal in signals
        ]
        if not rows:
            return
        with self.get_connection() as conn:
            conn.executemany(
                """
                INSERT INTO signals (
                    signal_id, symbol, side, entry_price, stop_price,
                    target_price, model_name, regime, rationale, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()

    def get_total_realized_pnl(self) -> float:
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(net_pnl), 0.0) AS pnl FROM trade_journal"
            ).fetchone()
        return float(row["pnl"])

    def paper_account(self, starting_capital: float, positions: Optional[List[Position]] = None):
        positions = positions if positions is not None else self.get_open_positions()
        realized = self.get_total_realized_pnl()
        committed = sum(
            p.entry_price * p.qty for p in positions if p.side == Side.BUY
        )
        current_value = sum(
            p.current_price * p.qty for p in positions if p.side == Side.BUY
        )
        available_cash = starting_capital + realized - committed
        equity = available_cash + current_value
        return max(0.0, available_cash), equity

    def record_paper_order_and_open_position(self, order: Order) -> Position:
        if not order.is_paper:
            raise RuntimeError("NON_PAPER_ORDER_REJECTED")
        with self.get_connection() as conn:
            c = conn.cursor()
            exists = c.execute(
                "SELECT 1 FROM positions WHERE symbol=? AND status='OPEN'",
                (order.symbol,),
            ).fetchone()
            if exists:
                raise RuntimeError(f"OPEN_POSITION_ALREADY_EXISTS:{order.symbol}")
            c.execute("""
                INSERT INTO orders (
                    order_id, symbol, side, qty, entry_price, stop_price, target_price,
                    status, created_at, broker_order_id, filled_price,
                    auto_executed_on_timeout, is_paper, is_intraday
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """, (
                order.order_id, order.symbol, order.side.value, order.qty,
                order.entry_price, order.stop_price, order.target_price,
                order.status.value, order.created_at.isoformat(),
                order.broker_order_id, order.filled_price,
                int(order.auto_executed_on_timeout), int(order.is_intraday),
            ))
            position_id = f"pos_{uuid.uuid4().hex[:12]}"
            fill = float(order.filled_price or order.entry_price)
            c.execute("""
                INSERT INTO positions (
                    position_id, symbol, side, qty, entry_price, current_price,
                    stop_price, target_price, opened_at, status, is_paper, is_intraday
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', 1, ?)
            """, (
                position_id, order.symbol, order.side.value, order.qty, fill, fill,
                order.stop_price, order.target_price, order.created_at.isoformat(),
                int(order.is_intraday),
            ))
            conn.commit()
        return Position(
            position_id=position_id,
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            entry_price=fill,
            current_price=fill,
            stop_price=order.stop_price,
            target_price=order.target_price,
            opened_at=order.created_at,
            is_paper=True,
            is_intraday=order.is_intraday,
        )

    def update_position_price(self, position_id: str, current_price: float):
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE positions SET current_price=? WHERE position_id=? AND status='OPEN'",
                (current_price, position_id),
            )
            conn.commit()

    def mark_position_closed(self, position_id: str, realized_pnl: float):
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE positions SET status='CLOSED', realized_pnl=? WHERE position_id=?",
                (realized_pnl, position_id),
            )
            conn.commit()

    def save_pending_signal(self, signal: Signal):
        pending_id = f"pending_{uuid.uuid4().hex[:12]}"
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO pending_signals (
                    pending_id, symbol, side, entry_price, stop_price, target_price,
                    model_name, regime, rationale, signal_timestamp, approved_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
            """, (
                pending_id, signal.symbol, signal.side.value, signal.entry_price,
                signal.stop_price, signal.target_price, signal.model_name,
                signal.regime.value, signal.rationale, signal.timestamp.isoformat(),
                datetime.now().isoformat(),
            ))
            conn.commit()
        return pending_id

    def get_pending_signals(self) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM pending_signals WHERE status='PENDING' ORDER BY approved_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_pending_signal(self, pending_id: str, status: str):
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE pending_signals SET status=? WHERE pending_id=?",
                (status, pending_id),
            )
            conn.commit()


    def get_latest_circuit_state(self) -> Dict[str, Any]:
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM circuit_breaker_state ORDER BY date DESC LIMIT 1"
            ).fetchone()
        if not row:
            return {"consecutive_losses": 0, "last_loss_time": None}
        last_loss_time = row["last_loss_time"]
        return {
            "consecutive_losses": int(row["consecutive_losses"] or 0),
            "last_loss_time": datetime.fromisoformat(last_loss_time) if last_loss_time else None,
        }

    def count_paper_orders_on_date(self, date_iso: str, intraday_only: bool = False) -> int:
        sql = "SELECT COUNT(*) AS n FROM orders WHERE is_paper=1 AND substr(created_at,1,10)=?"
        params = [date_iso]
        if intraday_only:
            sql += " AND is_intraday=1"
        with self.get_connection() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["n"] or 0)

    def increment_consecutive_losses(self, now: Optional[datetime] = None):
        current_time = (now or datetime.now()).isoformat()
        today_str = (now or datetime.now()).date().isoformat()
        with self.get_connection() as conn:
            conn.execute("""
                UPDATE circuit_breaker_state
                SET consecutive_losses = consecutive_losses + 1, last_loss_time = ?
                WHERE date = ?
            """, (current_time, today_str))
            conn.commit()

    def reset_consecutive_losses(self, now: Optional[datetime] = None):
        today_str = (now or datetime.now()).date().isoformat()
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE circuit_breaker_state SET consecutive_losses=0 WHERE date=?",
                (today_str,),
            )
            conn.commit()

    def record_blocked_signal(self, symbol, side, entry_price, stop_price, reason_code):
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO blocked_signals
                (symbol, side, entry_price, stop_price, reason_code, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                symbol, side, entry_price, stop_price, reason_code,
                datetime.now().isoformat(),
            ))
            conn.commit()

    def record_regime(self, regime: str, rationale: str):
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO regime_log (regime, rationale, timestamp)
                VALUES (?, ?, ?)
            """, (regime, rationale, datetime.now().isoformat()))
            conn.commit()

    def get_latest_regime(self, date_iso: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.execute(
                """
                SELECT regime, rationale, timestamp
                FROM regime_log
                WHERE substr(timestamp,1,10)=?
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                (date_iso,),
            ).fetchone()
        return dict(row) if row else None

    def get_today_summary(self, date_iso: str) -> Dict[str, Any]:
        with self.get_connection() as conn:
            signals = conn.execute(
                """
                SELECT COUNT(*) FROM signals
                WHERE substr(timestamp,1,10)=?
                """,
                (date_iso,),
            ).fetchone()[0]
            orders = conn.execute(
                """
                SELECT COUNT(*) FROM orders
                WHERE substr(created_at,1,10)=?
                """,
                (date_iso,),
            ).fetchone()[0]
            trades = conn.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(net_pnl),0)
                FROM trade_journal
                WHERE substr(exit_time,1,10)=?
                """,
                (date_iso,),
            ).fetchone()
            blocked = conn.execute(
                """
                SELECT COUNT(*) FROM blocked_signals
                WHERE substr(timestamp,1,10)=?
                """,
                (date_iso,),
            ).fetchone()[0]
            regime_events = conn.execute(
                """
                SELECT COUNT(*) FROM regime_log
                WHERE substr(timestamp,1,10)=?
                """,
                (date_iso,),
            ).fetchone()[0]

        return {
            "signals_evaluated_today": int(signals),
            "orders_today": int(orders),
            "trades_closed_today": int(trades[0]),
            "net_pnl_today": float(trades[1]),
            "blocked_signals_today": int(blocked),
            "regime_events_today": int(regime_events),
            "open_positions": len(self.get_open_positions()),
            "pending_signals": len(self.get_pending_signals()),
        }

    def record_preflight_success(self, timestamp: Optional[datetime] = None):
        ts = (timestamp or datetime.now()).isoformat()
        with self.get_connection() as conn:
            conn.execute(
                "INSERT INTO system_events (event_type, details, timestamp) VALUES (?, ?, ?)",
                ("PREFLIGHT_PASSED", "Preflight check passed", ts),
            )
            conn.commit()

    def get_first_preflight_time(self) -> Optional[datetime]:
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT timestamp FROM system_events WHERE event_type='PREFLIGHT_PASSED' ORDER BY timestamp ASC LIMIT 1"
            ).fetchone()
        if row and row["timestamp"]:
            return datetime.fromisoformat(row["timestamp"])
        return None

    def get_last_preflight_time(self) -> Optional[datetime]:
        with self.get_connection() as conn:
            row = conn.execute(
                """
                SELECT timestamp FROM system_events
                WHERE event_type='PREFLIGHT_PASSED'
                ORDER BY timestamp DESC LIMIT 1
                """
            ).fetchone()
        if row and row["timestamp"]:
            return datetime.fromisoformat(row["timestamp"])
        return None

    def get_completed_trades_count(self) -> int:
        with self.get_connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM trade_journal").fetchone()
        return int(row["n"]) if row else 0

    def get_telegram_update_offset(self) -> Optional[int]:
        with self.get_connection() as conn:
            row = conn.execute(
                """
                SELECT next_offset FROM telegram_update_state
                WHERE consumer='command_listener'
                """
            ).fetchone()
        return int(row["next_offset"]) if row else None

    def set_telegram_update_offset(self, next_offset: int) -> None:
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO telegram_update_state (consumer, next_offset)
                VALUES ('command_listener', ?)
                ON CONFLICT(consumer) DO UPDATE
                SET next_offset=excluded.next_offset
                """,
                (int(next_offset),),
            )
            conn.commit()

    def enqueue_telegram_callback(
        self,
        update_id: int,
        action: str,
        signal_id: Optional[str],
    ) -> None:
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO telegram_callback_inbox (
                    update_id, action, signal_id, received_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    int(update_id),
                    str(action).upper(),
                    signal_id,
                    datetime.now().isoformat(),
                ),
            )
            conn.commit()

    def pop_telegram_callback(
        self,
        signal_id: Optional[str] = None,
    ) -> Optional[Tuple[str, Optional[str]]]:
        with self.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if signal_id is None:
                row = conn.execute(
                    """
                    SELECT id, action, signal_id
                    FROM telegram_callback_inbox
                    ORDER BY id ASC LIMIT 1
                    """
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT id, action, signal_id
                    FROM telegram_callback_inbox
                    WHERE signal_id=?
                    ORDER BY id ASC LIMIT 1
                    """,
                    (signal_id,),
                ).fetchone()
            if not row:
                conn.commit()
                return None
            conn.execute(
                "DELETE FROM telegram_callback_inbox WHERE id=?",
                (row["id"],),
            )
            conn.commit()
        return str(row["action"]), row["signal_id"]
