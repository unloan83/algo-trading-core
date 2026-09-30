import json
import logging
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
from zoneinfo import ZoneInfo


log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
SQLITE_TIMEOUT_SECONDS = 1.0


SIGNAL_COLUMNS = {
    "scan_id": "TEXT",
    "dedup_key": "TEXT",
    "first_of_day": "INTEGER",
    "ts_utc": "TEXT",
    "strategy_version": "TEXT",
    "intraday_regime": "TEXT",
    "eod_regime": "TEXT",
    "feature_json": "TEXT",
    "block_reason": "TEXT",
}

REGIME_COLUMNS = {
    "ts_utc": "TEXT",
    "input_asof_date": "TEXT",
    "input_candle_count": "INTEGER",
    "input_asof_stale": "INTEGER",
    "source_mode": "TEXT",
    "input_symbol": "TEXT",
    "input_timeframe": "TEXT",
}


def _absolute_existing_db(db_path: str) -> Path:
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"OBSERVABILITY_DB_NOT_FOUND:{path}")
    return path


def _connect(db_path: str) -> sqlite3.Connection:
    path = _absolute_existing_db(db_path)
    conn = sqlite3.connect(
        f"file:{path}?mode=rw",
        uri=True,
        timeout=SQLITE_TIMEOUT_SECONDS,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=1000")
    return conn


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: Dict[str, str]) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def migrate_observability_schema(db_path: str) -> None:
    """Apply the additive observability schema. Intended for the deploy migration step only."""
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_columns(conn, "signals", SIGNAL_COLUMNS)
        _ensure_columns(conn, "regime_log", REGIME_COLUMNS)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scan_runs (
                scan_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL,
                started_ts_utc TEXT NOT NULL,
                finished_ts_utc TEXT,
                strategy_version TEXT,
                status TEXT NOT NULL,
                model TEXT,
                evaluated INTEGER NOT NULL DEFAULT 0 CHECK(evaluated >= 0),
                passed INTEGER NOT NULL DEFAULT 0 CHECK(passed >= 0),
                blocked INTEGER NOT NULL DEFAULT 0 CHECK(blocked >= 0),
                skipped INTEGER NOT NULL DEFAULT 0 CHECK(skipped >= 0),
                ordered INTEGER NOT NULL DEFAULT 0 CHECK(ordered >= 0),
                skipped_existing_position INTEGER NOT NULL DEFAULT 0,
                skipped_daily_cap INTEGER NOT NULL DEFAULT 0,
                skipped_approval INTEGER NOT NULL DEFAULT 0,
                skipped_internal_error INTEGER NOT NULL DEFAULT 0,
                blocked_by_reason_json TEXT,
                universe_cache_age_seconds REAL,
                market_filter_cache_age_seconds REAL,
                error_type TEXT,
                error_message TEXT,
                CHECK(passed = blocked + skipped + ordered),
                CHECK(skipped = skipped_existing_position + skipped_daily_cap
                              + skipped_approval + skipped_internal_error)
            )
            """
        )
        conn.execute(
            """
            CREATE VIEW IF NOT EXISTS v_signals_norm AS
            WITH normalized AS (
                SELECT
                    s.*,
                    COALESCE(
                        s.ts_utc,
                        strftime('%Y-%m-%dT%H:%M:%fZ', s.timestamp, '-5 hours', '-30 minutes')
                    ) AS normalized_ts_utc,
                    COALESCE(
                        s.dedup_key,
                        upper(s.symbol) || '|' || s.model_name || '|' || substr(s.timestamp, 1, 10)
                    ) AS normalized_dedup_key,
                    COALESCE(
                        s.intraday_regime,
                        CASE WHEN s.eod_regime IS NULL THEN s.regime END
                    ) AS normalized_intraday_regime,
                    COALESCE(
                        s.eod_regime,
                        (
                            SELECT r.regime
                            FROM regime_log AS r
                            WHERE substr(r.timestamp, 1, 10) = substr(s.timestamp, 1, 10)
                              AND COALESCE(
                                    r.source_mode,
                                    CASE WHEN r.rationale LIKE 'intraday:%' THEN 'intraday' ELSE 'eod' END
                                  ) = 'eod'
                            ORDER BY r.timestamp DESC, r.id DESC
                            LIMIT 1
                        )
                    ) AS normalized_eod_regime,
                    COALESCE(
                        s.block_reason,
                        CASE WHEN substr(s.timestamp, 1, 10) = '2026-09-21'
                             THEN 'INV7_FALSE_POSITIVE' END
                    ) AS normalized_block_reason
                FROM signals AS s
            ), ranked AS (
                SELECT
                    normalized.*,
                    row_number() OVER (
                        PARTITION BY normalized_dedup_key
                        ORDER BY normalized_ts_utc, signal_id
                    ) AS normalized_rank
                FROM normalized
            )
            SELECT
                signal_id,
                normalized_dedup_key AS dedup_key,
                CASE WHEN normalized_rank = 1 THEN 1 ELSE 0 END AS first_of_day,
                symbol, side, entry_price, stop_price, target_price,
                model_name, strategy_version,
                normalized_intraday_regime AS intraday_regime,
                normalized_eod_regime AS eod_regime,
                feature_json,
                normalized_block_reason AS block_reason,
                normalized_ts_utc AS ts_utc,
                timestamp AS legacy_timestamp,
                CASE
                    WHEN substr(timestamp, 1, 10) = '2026-09-21' THEN 'SENSITIVITY'
                    WHEN substr(timestamp, 1, 10) = '2026-09-22' THEN 'PARTIAL_SCAN_COVERAGE'
                    ELSE 'PRIMARY'
                END AS data_cohort
            FROM ranked
            """
        )
        conn.execute(
            """
            CREATE VIEW IF NOT EXISTS v_regime_norm AS
            SELECT
                id, regime, rationale,
                COALESCE(
                    ts_utc,
                    CASE
                        WHEN timestamp < '2026-09-27T13:47:07'
                        THEN timestamp || 'Z'
                        ELSE strftime('%Y-%m-%dT%H:%M:%fZ', timestamp, '-5 hours', '-30 minutes')
                    END
                ) AS ts_utc,
                COALESCE(
                    source_mode,
                    CASE WHEN rationale LIKE 'intraday:%' THEN 'intraday' ELSE 'eod' END
                ) AS source_mode,
                input_asof_date, input_candle_count, input_asof_stale,
                input_symbol, input_timeframe,
                timestamp AS legacy_timestamp
            FROM regime_log
            """
        )
        conn.commit()


def utc_iso(value: Optional[datetime] = None) -> str:
    current = value or datetime.now(IST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=IST)
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def deployed_commit() -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        value = result.stdout.strip()
        return value or None
    except Exception as exc:
        log.error("OBSERVABILITY_VERSION_FAILED:%s:%s", type(exc).__name__, exc)
        return None


def _json_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    return str(value)


def feature_snapshot(signal, symbol_df, benchmark_df) -> Optional[str]:
    """Serialize only observed inputs/outputs; never participates in a decision."""
    try:
        last = symbol_df.iloc[-1]
        benchmark_last = benchmark_df.iloc[-1]
        payload = {
            "input_candle": {
                key: _json_scalar(last.get(key))
                for key in ("timestamp", "open", "high", "low", "close", "volume")
            },
            "input_candle_count": int(len(symbol_df)),
            "benchmark_asof": _json_scalar(benchmark_last.get("timestamp")),
            "benchmark_candle_count": int(len(benchmark_df)),
            "entry_price": float(signal.entry_price),
            "stop_price": float(signal.stop_price),
            "target_price": float(signal.target_price),
            "rationale": str(signal.rationale),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))
    except Exception as exc:
        log.error("OBSERVABILITY_FEATURE_FAILED:%s:%s", type(exc).__name__, exc)
        return None


def safe_update_signal_metadata(db_path: str, rows: Iterable[Dict[str, Any]]) -> bool:
    try:
        with _connect(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            for row in rows:
                session_date = str(row["session_date"])
                dedup_key = "|".join(
                    (str(row["symbol"]).upper(), str(row["model_name"]), session_date)
                )
                earlier = conn.execute(
                    """
                    SELECT 1 FROM signals
                    WHERE signal_id <> ? AND symbol = ? AND model_name = ?
                      AND substr(timestamp, 1, 10) = ?
                    LIMIT 1
                    """,
                    (row["signal_id"], row["symbol"], row["model_name"], session_date),
                ).fetchone()
                conn.execute(
                    """
                    UPDATE signals SET
                        scan_id=?, dedup_key=?, first_of_day=?, ts_utc=?,
                        strategy_version=?, intraday_regime=?, eod_regime=?,
                        feature_json=?, block_reason=?
                    WHERE signal_id=?
                    """,
                    (
                        row.get("scan_id"),
                        dedup_key,
                        0 if earlier else 1,
                        row.get("ts_utc"),
                        row.get("strategy_version"),
                        row.get("intraday_regime"),
                        row.get("eod_regime"),
                        row.get("feature_json"),
                        row.get("block_reason"),
                        row["signal_id"],
                    ),
                )
            conn.commit()
        return True
    except Exception as exc:
        log.error("OBSERVABILITY_SIGNAL_WRITE_FAILED:%s:%s", type(exc).__name__, exc)
        return False


def safe_set_signal_block_reason(db_path: str, signal_id: Optional[str], reason: str) -> bool:
    if not signal_id:
        return False
    try:
        with _connect(db_path) as conn:
            conn.execute(
                "UPDATE signals SET block_reason=? WHERE signal_id=?",
                (str(reason), signal_id),
            )
            conn.commit()
        return True
    except Exception as exc:
        log.error("OBSERVABILITY_BLOCK_WRITE_FAILED:%s:%s", type(exc).__name__, exc)
        return False


def safe_update_regime_metadata(db_path: str, regime_id: int, metadata: Dict[str, Any]) -> bool:
    try:
        with _connect(db_path) as conn:
            conn.execute(
                """
                UPDATE regime_log SET ts_utc=?, input_asof_date=?,
                    input_candle_count=?, input_asof_stale=?, source_mode=?,
                    input_symbol=?, input_timeframe=?
                WHERE id=?
                """,
                (
                    metadata.get("ts_utc"),
                    metadata.get("input_asof_date"),
                    metadata.get("input_candle_count"),
                    metadata.get("input_asof_stale"),
                    metadata.get("source_mode"),
                    metadata.get("input_symbol"),
                    metadata.get("input_timeframe"),
                    int(regime_id),
                ),
            )
            conn.commit()
        return True
    except Exception as exc:
        log.error("OBSERVABILITY_REGIME_WRITE_FAILED:%s:%s", type(exc).__name__, exc)
        return False


def _cache_age_seconds(path: Path, now: datetime) -> Optional[float]:
    try:
        if not path.is_file():
            return None
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        aware_now = now if now.tzinfo else now.replace(tzinfo=IST)
        return max(0.0, (aware_now.astimezone(timezone.utc) - modified).total_seconds())
    except Exception as exc:
        log.error("OBSERVABILITY_CACHE_AGE_FAILED:%s:%s", type(exc).__name__, exc)
        return None


@dataclass
class ScanRun:
    mode: str
    started_at: datetime
    scan_id: str = field(default_factory=lambda: f"scan_{uuid.uuid4().hex}")
    strategy_version: Optional[str] = None
    status: str = "RUNNING"
    model: Optional[str] = None
    evaluated: int = 0
    passed: int = 0
    blocked: int = 0
    skipped: int = 0
    ordered: int = 0
    skipped_existing_position: int = 0
    skipped_daily_cap: int = 0
    skipped_approval: int = 0
    skipped_internal_error: int = 0
    blocked_by_reason: Dict[str, int] = field(default_factory=dict)
    error_type: Optional[str] = None
    error_message: Optional[str] = None

    def block(self, reason: str) -> None:
        self.blocked += 1
        self.blocked_by_reason[reason] = self.blocked_by_reason.get(reason, 0) + 1

    def skip(self, reason: str, count: int = 1) -> None:
        self.skipped += count
        attr = f"skipped_{reason}"
        setattr(self, attr, getattr(self, attr) + count)

    def fail(self, exc: BaseException) -> None:
        remainder = max(0, self.passed - self.blocked - self.skipped - self.ordered)
        if remainder:
            self.skip("internal_error", remainder)
        self.status = "FAILED"
        self.error_type = type(exc).__name__
        self.error_message = str(exc)[:500]


def new_scan_run(mode: str, now: datetime) -> ScanRun:
    return ScanRun(mode=mode, started_at=now, strategy_version=deployed_commit())


def safe_record_scan_run(db_path: str, run: ScanRun, now: Optional[datetime] = None) -> bool:
    try:
        root = Path(__file__).resolve().parents[1]
        universe_age = _cache_age_seconds(root / ".cache" / "active_universe.json", now or datetime.now(IST))
        filter_age = _cache_age_seconds(root / ".cache" / "market_filters.json", now or datetime.now(IST))
        with _connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO scan_runs (
                    scan_id, mode, started_ts_utc, finished_ts_utc,
                    strategy_version, status, model, evaluated, passed,
                    blocked, skipped, ordered, skipped_existing_position,
                    skipped_daily_cap, skipped_approval, skipped_internal_error,
                    blocked_by_reason_json, universe_cache_age_seconds,
                    market_filter_cache_age_seconds, error_type, error_message
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run.scan_id, run.mode, utc_iso(run.started_at), utc_iso(now),
                    run.strategy_version, run.status, run.model, run.evaluated,
                    run.passed, run.blocked, run.skipped, run.ordered,
                    run.skipped_existing_position, run.skipped_daily_cap,
                    run.skipped_approval, run.skipped_internal_error,
                    json.dumps(run.blocked_by_reason, sort_keys=True),
                    universe_age, filter_age, run.error_type, run.error_message,
                ),
            )
            conn.commit()
        return True
    except Exception as exc:
        log.error("OBSERVABILITY_SCAN_WRITE_FAILED:%s:%s", type(exc).__name__, exc)
        return False
