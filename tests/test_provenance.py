import gzip
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from data.provenance import safe_snapshot_file


class ProvenanceTests(unittest.TestCase):
    def test_snapshot_is_idempotent_hashed_and_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "active_universe.json"
            target = root / "input_provenance.db"
            source.write_bytes(b'{"symbols":["TCS"]}')

            self.assertTrue(
                safe_snapshot_file(
                    "active_universe_cache",
                    source,
                    session_date=date(2026, 9, 21),
                    provenance_db=target,
                    max_total_bytes=1024,
                )
            )
            self.assertTrue(
                safe_snapshot_file(
                    "active_universe_cache",
                    source,
                    session_date=date(2026, 9, 21),
                    provenance_db=target,
                    max_total_bytes=1024,
                )
            )
            resolved = target.resolve()
            self.assertTrue(resolved.is_file())
            with sqlite3.connect(
                f"file:{resolved}?mode=ro", uri=True, timeout=1.0
            ) as conn:
                rows = conn.execute(
                    "SELECT sha256, byte_count, encoding, payload FROM provenance_snapshots"
                ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][1], len(source.read_bytes()))
            self.assertEqual(rows[0][2], "gzip")
            self.assertEqual(gzip.decompress(rows[0][3]), source.read_bytes())

    def test_missing_or_oversize_source_is_nonfatal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertFalse(safe_snapshot_file("missing", root / "missing.json"))
            source = root / "large.json"
            source.write_bytes(b"x" * 20)
            self.assertFalse(
                safe_snapshot_file("large", source, max_payload_bytes=10)
            )


if __name__ == "__main__":
    unittest.main()
