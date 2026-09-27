import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from core.models import Signal, Side, RegimeType
from core.order_router import OrderRouter
from data.db_models import DatabaseManager
from journal.trade_journal import ImmutableTradeJournal


class TestPaperSafety(unittest.TestCase):
    def setUp(self):
        os.environ["TRADING_MODE"] = "PAPER"

    def test_live_router_is_physically_disabled(self):
        with self.assertRaises(RuntimeError):
            OrderRouter(live_mode=True)

    def test_paper_fill_persists_open_position(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            db = DatabaseManager(f.name)
            sig = Signal(
                symbol="TCS",
                side=Side.BUY,
                entry_price=100.0,
                stop_price=95.0,
                target_price=110.0,
                model_name="test",
                regime=RegimeType.TREND_UP,
                rationale="test",
                timestamp=datetime.now(),
            )
            order = OrderRouter().route_order(
                sig,
                10,
                auto_executed_on_timeout=True,
                is_intraday=True,
            )
            pos = db.record_paper_order_and_open_position(order)
            self.assertEqual(pos.symbol, "TCS")
            self.assertTrue(pos.is_paper)
            self.assertTrue(pos.is_intraday)
            self.assertTrue(pos.auto_executed_on_timeout)
            self.assertEqual(pos.entry_signal_price, 100.0)
            self.assertEqual(len(db.get_open_positions()), 1)

            with db.get_connection() as conn:
                persisted = conn.execute("SELECT * FROM orders").fetchone()
            self.assertEqual(persisted["submission_ts"], order.submission_ts.isoformat())
            self.assertEqual(persisted["ack_ts"], order.ack_ts.isoformat())
            self.assertEqual(persisted["fill_ts"], order.fill_ts.isoformat())

    def test_exit_journal_preserves_timeout_and_embedded_slippage(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            db = DatabaseManager(f.name)
            sig = Signal(
                symbol="TCS",
                side=Side.BUY,
                entry_price=100.0,
                stop_price=95.0,
                target_price=110.0,
                model_name="test",
                regime=RegimeType.TREND_UP,
                rationale="test",
                timestamp=datetime.now(),
            )
            order = OrderRouter(slippage_pct=0.05).route_order(
                sig,
                10,
                auto_executed_on_timeout=True,
                is_intraday=True,
            )
            pos = db.record_paper_order_and_open_position(order)
            exit_signal_price = 110.0
            exit_price = OrderRouter(slippage_pct=0.05).simulator.simulate_exit_price(
                pos.side,
                exit_signal_price,
            )

            journal_id = ImmutableTradeJournal(db).record_trade_exit(
                position_id=pos.position_id,
                symbol=pos.symbol,
                side=pos.side.value,
                qty=pos.qty,
                entry_price=pos.entry_price,
                exit_price=exit_price,
                entry_time=pos.opened_at,
                exit_time=datetime.now(),
                exit_reason="TARGET",
                entry_signal_price=pos.entry_signal_price,
                exit_signal_price=exit_signal_price,
                auto_executed_on_timeout=pos.auto_executed_on_timeout,
                is_paper=True,
                is_intraday=True,
                slippage_already_applied=True,
            )

            with db.get_connection() as conn:
                row = conn.execute(
                    "SELECT * FROM trade_journal WHERE journal_id=?",
                    (journal_id,),
                ).fetchone()
            self.assertEqual(row["auto_executed_on_timeout"], 1)
            self.assertAlmostEqual(row["slippage_bps"], 10.0, places=6)

    def test_pending_signal_preserves_manual_approval_provenance(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            db = DatabaseManager(f.name)
            sig = Signal(
                symbol="TCS",
                side=Side.BUY,
                entry_price=100.0,
                stop_price=95.0,
                target_price=110.0,
                model_name="test",
                regime=RegimeType.TREND_UP,
                rationale="test",
                timestamp=datetime.now(),
            )
            db.save_pending_signal(
                sig,
                auto_executed_on_timeout=False,
            )

            row = db.get_pending_signals()[0]
            self.assertEqual(row["auto_executed_on_timeout"], 0)

    def test_database_timestamps_and_equity_snapshot_use_ist_helper(self):
        fixed = datetime(2026, 9, 27, 12, 0, 0)
        aware_utc = datetime(2026, 9, 27, 6, 30, 0, tzinfo=timezone.utc)
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            db = DatabaseManager(f.name)
            with patch("data.db_models.now_ist_naive", return_value=fixed):
                db.record_blocked_signal("TCS", "BUY", 100.0, 95.0, "TEST")
                db.record_regime("RANGE", "test")
                snapshot_id = db.record_equity_snapshot(100123.45, "TEST")
            db.record_preflight_success(aware_utc)

            with db.get_connection() as conn:
                blocked_ts = conn.execute(
                    "SELECT timestamp FROM blocked_signals"
                ).fetchone()[0]
                regime_ts = conn.execute(
                    "SELECT timestamp FROM regime_log"
                ).fetchone()[0]
                snapshot = conn.execute(
                    "SELECT timestamp, equity, trigger FROM equity_snapshots WHERE id=?",
                    (snapshot_id,),
                ).fetchone()
                preflight_ts = conn.execute(
                    "SELECT timestamp FROM system_events"
                ).fetchone()[0]

            self.assertEqual(blocked_ts, fixed.isoformat())
            self.assertEqual(regime_ts, fixed.isoformat())
            self.assertEqual(snapshot["timestamp"], fixed.isoformat())
            self.assertEqual(snapshot["equity"], 100123.45)
            self.assertEqual(snapshot["trigger"], "TEST")
            self.assertEqual(preflight_ts, fixed.isoformat())

    def test_paper_monitor_records_minute_equity_snapshot_without_positions(self):
        from scripts import paper_monitor

        db = MagicMock()
        db.get_open_positions.return_value = []
        db.paper_account.return_value = (100000.0, 100000.0)
        with patch.object(
            paper_monitor,
            "now_ist_naive",
            return_value=datetime(2026, 9, 27, 10, 0, 0),
        ), patch.object(
            paper_monitor,
            "DatabaseManager",
            return_value=db,
        ), patch.object(
            paper_monitor,
            "paper_starting_capital",
            return_value=100000.0,
        ), patch.object(paper_monitor, "write_runtime_marker"):
            paper_monitor.main()

        db.record_equity_snapshot.assert_called_once_with(
            100000.0,
            "PAPER_MONITOR",
        )

    def test_paper_gate_status(self):
        from core.paper_engine import paper_gate_status
        from datetime import timedelta
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            db = DatabaseManager(f.name)
            status = paper_gate_status(db)
            self.assertFalse(status["gate_cleared"])
            self.assertEqual(status["days_elapsed"], 0)
            self.assertEqual(status["trades_completed"], 0)

            # Record preflight passed 50 days ago
            past = datetime.now() - timedelta(days=50)
            db.record_preflight_success(past)
            status = paper_gate_status(db)
            self.assertEqual(status["days_elapsed"], 50)
            self.assertFalse(status["gate_cleared"]) # trades_completed still 0 < 20


if __name__ == "__main__":
    unittest.main()
