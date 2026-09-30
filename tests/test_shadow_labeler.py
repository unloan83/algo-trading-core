import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from learning.shadow_labeler import (
    clustered_report,
    dedupe_first_of_day,
    initialize_output_db,
    label_signal,
    open_source_readonly,
    read_normalized_signals,
    write_results,
)


def signal(signal_id="s1", ts="2026-09-23T04:00:00+00:00", **overrides):
    row = {
        "signal_id": signal_id,
        "dedup_key": "ABC|breakout|2026-09-23",
        "first_of_day": 1,
        "symbol": "ABC",
        "side": "BUY",
        "ts_utc": ts,
        "model_name": "breakout",
        "strategy_version": "b193d24",
        "intraday_regime": "TREND_UP",
        "eod_regime": "TREND_DOWN",
        "feature_json": json.dumps({"atr": 1.0}),
        "block_reason": None,
    }
    row.update(overrides)
    return row


def bars(tie=False):
    start = datetime(2026, 9, 23, 8, 30)
    rows = []
    for index in range(20):
        price = 99.0 + (index % 3) * 0.2
        rows.append(
            {
                "timestamp": start + timedelta(minutes=5 * index),
                "open": price,
                "high": price + 0.6,
                "low": price - 0.4,
                "close": price + 0.1,
                "volume": 1000 + index,
            }
        )
    # Signal is 09:30 IST. Strictly later next-bar open is 09:35.
    entry = next(row for row in rows if row["timestamp"] == datetime(2026, 9, 23, 9, 35))
    entry.update(open=100.0, high=104.5 if tie else 101.0, low=97.5 if tie else 99.5, close=100.0)
    return pd.DataFrame(rows)


class ShadowLabelerTests(unittest.TestCase):
    def test_next_bar_open_and_stop_first_same_bar_tie(self):
        result = label_signal(signal(), bars(tie=True))
        self.assertEqual(result.status, "LABELED")
        self.assertEqual(result.entry_time, "2026-09-23T09:35:00")
        self.assertEqual(result.entry_price, 100.0)
        self.assertEqual(result.stop_price, 98.0)
        self.assertEqual(result.target_price, 104.0)
        self.assertEqual(result.exit_reason, "STOP")
        self.assertEqual(result.gross_r, -1.0)
        self.assertLess(result.scaled_net_r, result.gross_r)
        self.assertGreater(result.scaled_cost_r, 0)
        self.assertEqual(result.counter_trend, 1)

    def test_timeout_mae_mfe_and_scaled_size(self):
        result = label_signal(signal(), bars(), equity=100_000, available_cash=100_000)
        self.assertEqual(result.exit_reason, "TIMEOUT")
        self.assertGreaterEqual(result.mae, 0)
        self.assertGreaterEqual(result.mfe, 0)
        self.assertGreater(result.scaled_qty, 0)
        self.assertLessEqual(result.scaled_qty * result.entry_price, 25_000)

    def test_unlabelable_missing_and_flat_bars_are_explicit(self):
        missing = label_signal(signal(), pd.DataFrame())
        self.assertEqual((missing.status, missing.status_detail), ("UNLABELABLE", "MISSING_BARS"))
        flat = pd.DataFrame(
            [
                {"timestamp": datetime(2026, 9, 23, 9, 35) + timedelta(minutes=5 * i), "open": 100, "high": 100, "low": 100, "close": 100, "volume": 0}
                for i in range(15)
            ]
        )
        flat_result = label_signal(signal(), flat)
        self.assertEqual(flat_result.status, "UNLABELABLE")
        self.assertEqual(flat_result.status_detail, "FLAT_BARS")

    def test_first_of_day_dedupe_and_defect_cohorts(self):
        second = signal("s2", ts="2026-09-23T04:05:00+00:00")
        self.assertEqual([row["signal_id"] for row in dedupe_first_of_day([second, signal()])], ["s1"])
        sensitivity = label_signal(
            signal(ts="2026-09-21T04:20:00+00:00", dedup_key="ABC|breakout|2026-09-21"),
            bars(tie=True).assign(timestamp=lambda frame: frame.timestamp - timedelta(days=2)),
        )
        partial = label_signal(
            signal(ts="2026-09-22T04:20:00+00:00", dedup_key="ABC|breakout|2026-09-22"),
            bars(tie=True).assign(timestamp=lambda frame: frame.timestamp - timedelta(days=1)),
        )
        self.assertEqual(sensitivity.cohort, "sensitivity")
        self.assertEqual(partial.cohort, "partial_scan_coverage")

    def test_clustered_ci_uses_days_and_marks_under_30_insufficient(self):
        base = label_signal(signal(), bars(tie=True))
        labels = [replace(base, signal_id=f"s{i}", signal_day=f"2026-09-{23+i:02d}") for i in range(2)]
        report = clustered_report(labels, bootstrap_samples=50)
        self.assertTrue(report)
        self.assertTrue(all(row["effective_n"] == 2 for row in report))
        self.assertTrue(all(row["sufficiency"] == "INSUFFICIENT" for row in report))
        self.assertTrue(all(row["ci95_low"] is not None for row in report))

    def test_source_is_read_only_and_output_is_separate_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            source_path = Path(temp) / "journal.db"
            writable = sqlite3.connect(source_path)
            writable.execute(
                """CREATE TABLE normalized(signal_id TEXT, dedup_key TEXT, first_of_day INTEGER,
                symbol TEXT, side TEXT, ts_utc TEXT, model_name TEXT, strategy_version TEXT,
                intraday_regime TEXT, eod_regime TEXT, feature_json TEXT, block_reason TEXT)"""
            )
            writable.execute(
                "INSERT INTO normalized VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(signal().values()),
            )
            writable.execute("CREATE VIEW v_signals_norm AS SELECT * FROM normalized")
            writable.commit()
            writable.close()

            source = open_source_readonly(source_path)
            rows = read_normalized_signals(source)
            self.assertEqual(len(rows), 1)
            with self.assertRaises(sqlite3.OperationalError):
                source.execute("DELETE FROM normalized")

            output = sqlite3.connect(Path(temp) / "shadow.db")
            initialize_output_db(output)
            labeled = label_signal(rows[0], bars(tie=True))
            report = clustered_report([labeled], bootstrap_samples=10)
            write_results(output, [labeled], report)
            write_results(output, [labeled], report)
            self.assertEqual(output.execute("SELECT COUNT(*) FROM shadow_labels").fetchone()[0], 1)
            self.assertEqual(output.execute("SELECT COUNT(*) FROM shadow_reports").fetchone()[0], 1)
            source.close()
            output.close()

    def test_module_does_not_import_order_or_risk_modules(self):
        source = Path("learning/shadow_labeler.py").read_text(encoding="utf-8")
        self.assertNotIn("order_router", source)
        self.assertNotIn("risk_governor", source)
        self.assertNotIn("position_sizer", source)


if __name__ == "__main__":
    unittest.main()

