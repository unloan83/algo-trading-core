import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from core.models import RegimeType, Side, Signal
from data.db_models import DatabaseManager
from data.observability import (
    ScanRun,
    migrate_observability_schema,
    safe_record_scan_run,
    safe_set_signal_block_reason,
)


class ObservabilitySchemaTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        temp.close()
        self.db_path = Path(temp.name)
        self.db = DatabaseManager(str(self.db_path))

    def tearDown(self):
        self.db_path.unlink(missing_ok=True)

    def _columns(self, table):
        with self._readonly() as conn:
            return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}

    def _readonly(self):
        path = self.db_path.resolve()
        self.assertTrue(path.is_file())
        return sqlite3.connect(
            f"file:{path}?mode=ro", uri=True, timeout=1.0
        )

    def test_migration_is_idempotent_and_preserves_old_rows(self):
        signal = Signal(
            symbol="TCS",
            side=Side.BUY,
            entry_price=100.0,
            stop_price=99.0,
            target_price=102.0,
            model_name="breakout",
            regime=RegimeType.TREND_UP,
            rationale="fixture",
            timestamp=datetime(2026, 9, 21, 9, 25),
        )
        self.db.record_signal_evaluations([signal], evaluated_at=signal.timestamp)

        migrate_observability_schema(str(self.db_path))
        migrate_observability_schema(str(self.db_path))

        self.assertTrue(
            {
                "scan_id", "dedup_key", "first_of_day", "ts_utc",
                "strategy_version", "intraday_regime", "eod_regime",
                "feature_json", "block_reason",
            }.issubset(self._columns("signals"))
        )
        self.assertTrue(
            {"ts_utc", "input_asof_date", "input_candle_count", "source_mode"}
            .issubset(self._columns("regime_log"))
        )
        with self._readonly() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 1)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0], 0
            )
            views = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='view'"
                )
            }
            self.assertTrue({"v_signals_norm", "v_regime_norm"}.issubset(views))

    def test_signal_metadata_is_additive_and_block_reason_is_best_effort(self):
        migrate_observability_schema(str(self.db_path))
        timestamp = datetime(2026, 9, 21, 9, 25)
        signal = Signal(
            symbol="TCS",
            side=Side.BUY,
            entry_price=100.0,
            stop_price=99.0,
            target_price=102.0,
            model_name="breakout",
            regime=RegimeType.TREND_UP,
            rationale="fixture",
            timestamp=timestamp,
        )
        ids = self.db.record_signal_evaluations(
            [signal],
            evaluated_at=timestamp,
            scan_id="scan_fixture",
            strategy_version="abc123",
            intraday_regime="TREND_UP",
            feature_json_by_symbol={"TCS": json.dumps({"close": 100.0})},
        )
        self.assertEqual(len(ids), 1)
        self.assertTrue(
            safe_set_signal_block_reason(str(self.db_path), ids[0], "TEST_BLOCK")
        )
        with self._readonly() as conn:
            row = conn.execute(
                """
                SELECT scan_id, dedup_key, first_of_day, ts_utc,
                       strategy_version, intraday_regime, block_reason
                FROM signals WHERE signal_id=?
                """,
                (ids[0],),
            ).fetchone()
        self.assertEqual(row[0], "scan_fixture")
        self.assertEqual(row[1], "TCS|breakout|2026-09-21")
        self.assertEqual(row[2], 1)
        self.assertEqual(row[3], "2026-09-21T03:55:00Z")
        self.assertEqual(row[4:], ("abc123", "TREND_UP", "TEST_BLOCK"))

    def test_scan_run_invariant_is_stored(self):
        migrate_observability_schema(str(self.db_path))
        run = ScanRun(mode="intraday", started_at=datetime(2026, 9, 21, 9, 25))
        run.status = "SUCCESS"
        run.evaluated = 100
        run.passed = 5
        run.block("INV7")
        run.block("INV7")
        run.skip("existing_position")
        run.skip("daily_cap")
        run.ordered = 1
        self.assertTrue(safe_record_scan_run(str(self.db_path), run))
        with self._readonly() as conn:
            row = conn.execute(
                "SELECT passed, blocked, skipped, ordered FROM scan_runs"
            ).fetchone()
        self.assertEqual(row, (5, 2, 2, 1))

    def test_failed_scan_accounts_for_unprocessed_candidates(self):
        migrate_observability_schema(str(self.db_path))
        run = ScanRun(mode="eod", started_at=datetime(2026, 9, 21, 15, 45))
        run.passed = 3
        run.block("INV7")
        run.fail(RuntimeError("fixture failure"))
        self.assertEqual(run.skipped_internal_error, 2)
        self.assertTrue(safe_record_scan_run(str(self.db_path), run))
        with self._readonly() as conn:
            row = conn.execute(
                "SELECT passed, blocked, skipped, ordered, status FROM scan_runs"
            ).fetchone()
        self.assertEqual(row, (3, 1, 2, 0, "FAILED"))

    def test_missing_migration_never_raises_from_new_signal_metadata(self):
        timestamp = datetime(2026, 9, 21, 9, 25)
        signal = Signal(
            symbol="TCS", side=Side.BUY, entry_price=100.0, stop_price=99.0,
            target_price=102.0, model_name="breakout",
            regime=RegimeType.TREND_UP, rationale="fixture", timestamp=timestamp,
        )
        ids = self.db.record_signal_evaluations(
            [signal], evaluated_at=timestamp, scan_id="not_migrated"
        )
        self.assertEqual(len(ids), 1)
        with self._readonly() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
