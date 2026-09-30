#!/usr/bin/env python3
"""Run the offline shadow labeler against the read-only production journal."""

from __future__ import annotations

import argparse
import logging
import signal
import sqlite3
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.broker_client import UPSTOX_BASE, UnifiedBrokerClient
from learning.shadow_labeler import (
    IST,
    clustered_report,
    initialize_output_db,
    label_signals,
    open_source_readonly,
    read_normalized_signals,
    write_results,
)


log = logging.getLogger("shadow_labeler")
DEFAULT_SOURCE = Path("/home/ubuntu/projects/algo-trading-core/trading_system.db")
DEFAULT_OUTPUT = Path("/home/ubuntu/projects/algo-trading-core/learning/shadow_learning.db")


class UpstoxFiveMinuteBars:
    def __init__(self, timeout_seconds: int = 15):
        self.broker = UnifiedBrokerClient(paper_mode=True, timeout=timeout_seconds)
        self.cache = {}

    def __call__(self, signal_row):
        signal_ts = datetime.fromisoformat(str(signal_row["ts_utc"]).replace("Z", "+00:00"))
        local_day = signal_ts.astimezone(IST).date()
        symbol = str(signal_row["symbol"])
        today = datetime.now(IST).date()
        from_date = local_day - timedelta(days=7)
        to_date = min(today, local_day + timedelta(days=7))
        cache_key = (symbol, from_date, to_date)
        if cache_key not in self.cache:
            key = quote(self.broker.resolve_instrument_key(symbol), safe="")
            url = (
                f"{UPSTOX_BASE}/v3/historical-candle/{key}/minutes/5/"
                f"{to_date.isoformat()}/{from_date.isoformat()}"
            )
            historical = self.broker._candles_to_df(self.broker._get_json(url))
            if local_day == today:
                intraday_url = f"{UPSTOX_BASE}/v3/historical-candle/intraday/{key}/minutes/5"
                intraday = self.broker._candles_to_df(self.broker._get_json(intraday_url))
                historical = (
                    __import__("pandas").concat([historical, intraday], ignore_index=True)
                    .drop_duplicates("timestamp", keep="last")
                    .sort_values("timestamp")
                    .reset_index(drop=True)
                )
            self.cache[cache_key] = historical
        return self.cache[cache_key]


def _absolute_output(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_absolute():
        raise ValueError("OUTPUT_DB_MUST_BE_ABSOLUTE")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def _timeout(_signum, _frame):
    raise TimeoutError("SHADOW_LABELER_RUN_TIMEOUT")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-db", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-db", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout-seconds", type=int, default=240)
    args = parser.parse_args()
    if args.timeout_seconds <= 0:
        raise ValueError("TIMEOUT_MUST_BE_POSITIVE")

    run_id = f"shadow_{uuid.uuid4().hex}"
    started = datetime.now(timezone.utc).isoformat()
    source = output = None
    signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(args.timeout_seconds)
    try:
        source = open_source_readonly(args.source_db, timeout_seconds=min(15, args.timeout_seconds))
        output_path = _absolute_output(args.output_db)
        output = sqlite3.connect(output_path, timeout=min(15, args.timeout_seconds))
        initialize_output_db(output)
        try:
            output.execute(
                "INSERT INTO shadow_runs(run_id, started_at_utc, status) VALUES (?, ?, 'RUNNING')",
                (run_id, started),
            )
            output.commit()
        except Exception as exc:
            log.exception("shadow run-start write failed exception=%s:%s", type(exc).__name__, exc)
            raise

        signals = read_normalized_signals(source)
        labels = label_signals(signals, UpstoxFiveMinuteBars())
        report = clustered_report(labels)
        write_results(output, labels, report)
        try:
            output.execute(
                "UPDATE shadow_runs SET completed_at_utc=?, status='SUCCESS', detail=? WHERE run_id=?",
                (datetime.now(timezone.utc).isoformat(), f"signals={len(signals)} labels={len(labels)}", run_id),
            )
            output.commit()
        except Exception as exc:
            log.exception("shadow run-success write failed exception=%s:%s", type(exc).__name__, exc)
            raise
        print(f"SHADOW_LABELER_SUCCESS|signals={len(signals)}|labels={len(labels)}|reports={len(report)}")
        return 0
    except Exception as exc:
        log.exception("shadow labeler failed exception=%s:%s", type(exc).__name__, exc)
        if output is not None:
            try:
                output.execute(
                    "UPDATE shadow_runs SET completed_at_utc=?, status='FAILED', detail=? WHERE run_id=?",
                    (datetime.now(timezone.utc).isoformat(), f"{type(exc).__name__}:{exc}"[:500], run_id),
                )
                output.commit()
            except Exception as write_exc:
                log.exception("shadow run-failure write failed exception=%s:%s", type(write_exc).__name__, write_exc)
        return 1
    finally:
        signal.alarm(0)
        if source is not None:
            source.close()
        if output is not None:
            output.close()


if __name__ == "__main__":
    raise SystemExit(main())

