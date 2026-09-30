import gzip
import hashlib
import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


log = logging.getLogger(__name__)
DEFAULT_MAX_PAYLOAD_BYTES = 5 * 1024 * 1024
DEFAULT_TOTAL_BYTES = 10 * 1024 * 1024


def safe_snapshot_file(
    source: str,
    source_path: Path,
    *,
    session_date: Optional[date] = None,
    provenance_db: Optional[Path] = None,
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
    max_total_bytes: int = DEFAULT_TOTAL_BYTES,
) -> bool:
    """Best-effort bounded cache provenance; never raises into a trading path."""
    try:
        source_path = source_path.expanduser().resolve()
        if not source_path.is_file():
            raise FileNotFoundError(f"PROVENANCE_SOURCE_NOT_FOUND:{source_path}")
        with source_path.open("rb") as handle:
            payload = handle.read(max_payload_bytes + 1)
        if len(payload) > max_payload_bytes:
            raise ValueError(f"PROVENANCE_PAYLOAD_TOO_LARGE:{len(payload)}")
        digest = hashlib.sha256(payload).hexdigest()
        compressed = gzip.compress(payload)
        target = (provenance_db or source_path.parent / "input_provenance.db").expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        captured = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        day = (session_date or datetime.now().date()).isoformat()
        with sqlite3.connect(target, timeout=1.0) as conn:
            conn.execute("PRAGMA busy_timeout=1000")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS provenance_snapshots (
                    source TEXT NOT NULL,
                    session_date TEXT NOT NULL,
                    captured_ts_utc TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    byte_count INTEGER NOT NULL,
                    encoding TEXT NOT NULL,
                    payload BLOB NOT NULL,
                    PRIMARY KEY(source, session_date, sha256)
                )
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO provenance_snapshots
                (source, session_date, captured_ts_utc, sha256, byte_count, encoding, payload)
                VALUES (?, ?, ?, ?, ?, 'gzip', ?)
                """,
                (source, day, captured, digest, len(payload), compressed),
            )
            cutoff = (date.fromisoformat(day) - timedelta(days=30)).isoformat()
            conn.execute("DELETE FROM provenance_snapshots WHERE session_date < ?", (cutoff,))
            while conn.execute(
                "SELECT COALESCE(SUM(length(payload)),0) FROM provenance_snapshots"
            ).fetchone()[0] > max_total_bytes:
                conn.execute(
                    """
                    DELETE FROM provenance_snapshots WHERE rowid = (
                        SELECT rowid FROM provenance_snapshots
                        ORDER BY session_date, captured_ts_utc LIMIT 1
                    )
                    """
                )
            conn.commit()
        log.info("PROVENANCE_SNAPSHOT source=%s date=%s sha256=%s bytes=%d", source, day, digest, len(payload))
        return True
    except Exception as exc:
        log.error("PROVENANCE_SNAPSHOT_FAILED:%s:%s", type(exc).__name__, exc)
        return False
