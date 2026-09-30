"""Deterministic, offline forward-outcome labels for normalized signals.

This module deliberately has no dependency on order placement, risk governance,
or live strategy code.  It consumes normalized signal dictionaries and supplied
five-minute bars, then writes results only to a separate learning database.
"""

from __future__ import annotations

import json
import logging
import math
import random
import sqlite3
import statistics
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

from backtest.cost_model import calculate_indian_transaction_costs


log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
REQUIRED_BAR_COLUMNS = {"timestamp", "open", "high", "low", "close", "volume"}


@dataclass(frozen=True)
class ShadowLabel:
    signal_id: str
    dedup_key: str
    symbol: str
    model_name: str
    strategy_version: str | None
    signal_ts_utc: str
    signal_day: str
    iso_week: str
    cohort: str
    intraday_regime: str | None
    eod_regime: str | None
    block_reason: str | None
    counter_trend: int
    status: str
    status_detail: str | None
    entry_time: str | None = None
    exit_time: str | None = None
    entry_price: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    atr: float | None = None
    gross_r: float | None = None
    mae: float | None = None
    mfe: float | None = None
    mae_r: float | None = None
    mfe_r: float | None = None
    baseline_qty: int | None = None
    scaled_qty: int | None = None
    baseline_net_r: float | None = None
    scaled_net_r: float | None = None
    baseline_cost_r: float | None = None
    scaled_cost_r: float | None = None
    baseline_costs: float | None = None
    scaled_costs: float | None = None
    scaled_cost_gate_2x: int | None = None
    scaled_cost_gate_3x: int | None = None
    scaled_cost_gate_5x: int | None = None


def _parse_ts_utc(value: object) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("MISSING_SIGNAL_TIMESTAMP")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("SIGNAL_TIMESTAMP_NOT_UTC_AWARE")
    return parsed.astimezone(timezone.utc)


def _feature_atr(feature_json: object) -> float | None:
    if not feature_json:
        return None
    try:
        payload = feature_json if isinstance(feature_json, dict) else json.loads(str(feature_json))
        for key in ("atr", "atr_14", "atr14"):
            value = payload.get(key)
            if value is not None and math.isfinite(float(value)) and float(value) > 0:
                return float(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return None


def _atr_before_entry(bars: pd.DataFrame, entry_index: int, period: int = 14) -> float | None:
    history = bars.iloc[:entry_index]
    if len(history) < period:
        return None
    previous_close = history["close"].shift(1)
    true_range = pd.concat(
        [
            history["high"] - history["low"],
            (history["high"] - previous_close).abs(),
            (history["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    value = true_range.iloc[-period:].mean()
    return float(value) if pd.notna(value) and math.isfinite(float(value)) and value > 0 else None


def _cohort(signal_day: date, block_reason: str | None) -> str:
    reason = str(block_reason or "").upper()
    if signal_day == date(2026, 9, 21) or reason in {
        "INV7_FALSE_POSITIVE",
        "STALE_INTRADAY_REGIME",
    }:
        return "sensitivity"
    if signal_day == date(2026, 9, 22):
        return "partial_scan_coverage"
    return "primary"


def _quantities(
    entry_price: float,
    risk_distance: float,
    *,
    equity: float,
    available_cash: float,
    risk_pct: float,
    max_positions: int,
) -> tuple[int, int]:
    if min(entry_price, risk_distance, equity, available_cash) <= 0 or max_positions <= 0:
        return 0, 0
    risk_qty = math.floor((equity * risk_pct / 100.0) / risk_distance)
    baseline_qty = risk_qty if risk_qty * entry_price <= available_cash else 0
    scaled_qty = math.floor(min(risk_qty, (available_cash / max_positions) / entry_price))
    return max(0, baseline_qty), max(0, scaled_qty)


def _cost_result(entry: float, exit_price: float, qty: int, risk_distance: float) -> tuple[float | None, float | None, float | None]:
    if qty <= 0:
        return None, None, None
    _gross, net, costs, _slippage = calculate_indian_transaction_costs(
        buy_price=entry,
        sell_price=exit_price,
        qty=qty,
        is_intraday=True,
        slippage_pct=0.05,
    )
    risk_rupees = qty * risk_distance
    return net / risk_rupees, costs / risk_rupees, costs


def label_signal(
    signal: Mapping[str, object],
    bars: pd.DataFrame,
    *,
    equity: float = 100_000.0,
    available_cash: float = 100_000.0,
    risk_pct: float = 0.5,
    max_positions: int = 4,
) -> ShadowLabel:
    """Label one first-of-day signal without mutating either input."""
    signal_id = str(signal.get("signal_id") or "")
    symbol = str(signal.get("symbol") or "")
    model = str(signal.get("model_name") or "unknown")
    signal_ts = _parse_ts_utc(signal.get("ts_utc"))
    signal_local = signal_ts.astimezone(IST).replace(tzinfo=None)
    signal_day = signal_local.date()
    dedup_key = str(signal.get("dedup_key") or f"{symbol}|{model}|{signal_day.isoformat()}")
    intraday_regime = signal.get("intraday_regime")
    eod_regime = signal.get("eod_regime")
    block_reason = signal.get("block_reason")
    base = {
        "signal_id": signal_id,
        "dedup_key": dedup_key,
        "symbol": symbol,
        "model_name": model,
        "strategy_version": signal.get("strategy_version"),
        "signal_ts_utc": signal_ts.isoformat(),
        "signal_day": signal_day.isoformat(),
        "iso_week": f"{signal_day.isocalendar().year}-W{signal_day.isocalendar().week:02d}",
        "cohort": _cohort(signal_day, str(block_reason) if block_reason else None),
        "intraday_regime": str(intraday_regime) if intraday_regime else None,
        "eod_regime": str(eod_regime) if eod_regime else None,
        "block_reason": str(block_reason) if block_reason else None,
        "counter_trend": int(intraday_regime == "TREND_UP" and eod_regime == "TREND_DOWN"),
    }

    if str(signal.get("side") or "BUY").upper() != "BUY":
        return ShadowLabel(**base, status="UNLABELABLE", status_detail="UNSUPPORTED_SIDE")
    if bars is None or bars.empty:
        return ShadowLabel(**base, status="UNLABELABLE", status_detail="MISSING_BARS")
    if not REQUIRED_BAR_COLUMNS.issubset(bars.columns):
        return ShadowLabel(**base, status="UNLABELABLE", status_detail="MISSING_BAR_COLUMNS")

    frame = bars.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    if getattr(frame["timestamp"].dt, "tz", None) is not None:
        frame["timestamp"] = frame["timestamp"].dt.tz_convert(IST).dt.tz_localize(None)
    frame = frame.sort_values("timestamp").drop_duplicates("timestamp", keep="last").reset_index(drop=True)
    numeric = ["open", "high", "low", "close", "volume"]
    frame[numeric] = frame[numeric].apply(pd.to_numeric, errors="coerce")
    frame = frame.dropna(subset=["open", "high", "low", "close"])
    frame = frame[(frame["high"] >= frame["low"]) & (frame["open"] > 0) & (frame["close"] > 0)]
    if frame.empty:
        return ShadowLabel(**base, status="UNLABELABLE", status_detail="MISSING_BARS")

    future_indexes = frame.index[frame["timestamp"] > signal_local].tolist()
    if not future_indexes:
        return ShadowLabel(**base, status="UNLABELABLE", status_detail="NO_NEXT_BAR")
    entry_index = future_indexes[0]
    entry_bar = frame.iloc[entry_index]
    entry_price = float(entry_bar["open"])
    atr = _feature_atr(signal.get("feature_json")) or _atr_before_entry(frame, entry_index)
    if atr is None:
        return ShadowLabel(**base, status="UNLABELABLE", status_detail="INSUFFICIENT_ATR")
    if frame.iloc[: entry_index + 1][["open", "high", "low", "close"]].nunique().max() <= 1:
        return ShadowLabel(**base, status="UNLABELABLE", status_detail="FLAT_BARS", atr=atr)

    risk_distance = 2.0 * atr
    stop = entry_price - risk_distance
    target = entry_price + 2.0 * risk_distance
    if stop <= 0:
        return ShadowLabel(**base, status="UNLABELABLE", status_detail="NON_POSITIVE_STOP", atr=atr)

    entry_day = entry_bar["timestamp"].date()
    forward = frame.iloc[entry_index:]
    forward = forward[forward["timestamp"].dt.date == entry_day]
    if forward.empty:
        return ShadowLabel(**base, status="UNLABELABLE", status_detail="NO_FORWARD_BARS", atr=atr)

    exit_price = float(forward.iloc[-1]["close"])
    exit_time = forward.iloc[-1]["timestamp"]
    exit_reason = "TIMEOUT"
    observed_low = entry_price
    observed_high = entry_price
    for _, bar in forward.iterrows():
        observed_low = min(observed_low, float(bar["low"]))
        observed_high = max(observed_high, float(bar["high"]))
        # Stop-first is intentional when both thresholds occur in one bar.
        if float(bar["low"]) <= stop:
            exit_price = stop
            exit_time = bar["timestamp"]
            exit_reason = "STOP"
            break
        if float(bar["high"]) >= target:
            exit_price = target
            exit_time = bar["timestamp"]
            exit_reason = "TARGET"
            break

    gross_r = (exit_price - entry_price) / risk_distance
    mae = max(0.0, entry_price - observed_low)
    mfe = max(0.0, observed_high - entry_price)
    baseline_qty, scaled_qty = _quantities(
        entry_price,
        risk_distance,
        equity=equity,
        available_cash=available_cash,
        risk_pct=risk_pct,
        max_positions=max_positions,
    )
    baseline_net_r, baseline_cost_r, baseline_costs = _cost_result(
        entry_price, exit_price, baseline_qty, risk_distance
    )
    scaled_net_r, scaled_cost_r, scaled_costs = _cost_result(
        entry_price, exit_price, scaled_qty, risk_distance
    )
    scaled_risk_rupees = scaled_qty * risk_distance

    return ShadowLabel(
        **base,
        status="LABELED",
        status_detail=None,
        entry_time=pd.Timestamp(entry_bar["timestamp"]).isoformat(),
        exit_time=pd.Timestamp(exit_time).isoformat(),
        entry_price=entry_price,
        stop_price=stop,
        target_price=target,
        exit_price=exit_price,
        exit_reason=exit_reason,
        atr=atr,
        gross_r=gross_r,
        mae=mae,
        mfe=mfe,
        mae_r=mae / risk_distance,
        mfe_r=mfe / risk_distance,
        baseline_qty=baseline_qty,
        scaled_qty=scaled_qty,
        baseline_net_r=baseline_net_r,
        scaled_net_r=scaled_net_r,
        baseline_cost_r=baseline_cost_r,
        scaled_cost_r=scaled_cost_r,
        baseline_costs=baseline_costs,
        scaled_costs=scaled_costs,
        scaled_cost_gate_2x=int(scaled_risk_rupees >= 2 * scaled_costs) if scaled_costs is not None else None,
        scaled_cost_gate_3x=int(scaled_risk_rupees >= 3 * scaled_costs) if scaled_costs is not None else None,
        scaled_cost_gate_5x=int(scaled_risk_rupees >= 5 * scaled_costs) if scaled_costs is not None else None,
    )


def dedupe_first_of_day(signals: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    ordered = sorted((dict(row) for row in signals), key=lambda row: str(row.get("ts_utc") or ""))
    selected: dict[str, dict[str, object]] = {}
    for row in ordered:
        ts = _parse_ts_utc(row.get("ts_utc"))
        day = ts.astimezone(IST).date().isoformat()
        key = str(row.get("dedup_key") or f"{row.get('symbol')}|{row.get('model_name')}|{day}")
        if row.get("first_of_day") in (0, "0", False):
            continue
        selected.setdefault(key, row)
    return list(selected.values())


def label_signals(
    signals: Iterable[Mapping[str, object]],
    bars_for_signal: Callable[[Mapping[str, object]], pd.DataFrame],
    **label_kwargs: object,
) -> list[ShadowLabel]:
    labels = []
    for signal in dedupe_first_of_day(signals):
        try:
            labels.append(label_signal(signal, bars_for_signal(signal), **label_kwargs))
        except Exception as exc:
            log.exception(
                "shadow label failed signal_id=%s exception=%s:%s",
                signal.get("signal_id"),
                type(exc).__name__,
                exc,
            )
            try:
                signal_ts = _parse_ts_utc(signal.get("ts_utc"))
                local_day = signal_ts.astimezone(IST).date()
                labels.append(
                    ShadowLabel(
                        signal_id=str(signal.get("signal_id") or ""),
                        dedup_key=str(signal.get("dedup_key") or ""),
                        symbol=str(signal.get("symbol") or ""),
                        model_name=str(signal.get("model_name") or "unknown"),
                        strategy_version=str(signal.get("strategy_version")) if signal.get("strategy_version") else None,
                        signal_ts_utc=signal_ts.isoformat(),
                        signal_day=local_day.isoformat(),
                        iso_week=f"{local_day.isocalendar().year}-W{local_day.isocalendar().week:02d}",
                        cohort=_cohort(local_day, str(signal.get("block_reason") or "")),
                        intraday_regime=str(signal.get("intraday_regime")) if signal.get("intraday_regime") else None,
                        eod_regime=str(signal.get("eod_regime")) if signal.get("eod_regime") else None,
                        block_reason=str(signal.get("block_reason")) if signal.get("block_reason") else None,
                        counter_trend=int(signal.get("intraday_regime") == "TREND_UP" and signal.get("eod_regime") == "TREND_DOWN"),
                        status="UNLABELABLE",
                        status_detail=f"{type(exc).__name__}:{exc}",
                    )
                )
            except Exception as fallback_exc:
                log.exception(
                    "shadow fallback failed signal_id=%s exception=%s:%s",
                    signal.get("signal_id"),
                    type(fallback_exc).__name__,
                    fallback_exc,
                )
    return labels


def clustered_report(labels: Sequence[ShadowLabel], *, bootstrap_samples: int = 2000, seed: int = 193) -> list[dict[str, object]]:
    rows = [asdict(label) for label in labels if label.status == "LABELED"]
    dimensions = ("iso_week", "model_name", "eod_regime", "intraday_regime", "block_reason", "cohort")
    scenarios = (("baseline", "baseline_net_r", "baseline_cost_r"), ("scaled", "scaled_net_r", "scaled_cost_r"))
    grouped: dict[tuple[object, ...], list[dict[str, object]]] = {}
    for row in rows:
        grouped.setdefault(tuple(row.get(key) for key in dimensions), []).append(row)

    report: list[dict[str, object]] = []
    for group_key, group in grouped.items():
        for scenario, outcome_key, cost_key in scenarios:
            usable = [row for row in group if row.get(outcome_key) is not None]
            days = sorted({str(row["signal_day"]) for row in usable})
            values = [float(row[outcome_key]) for row in usable]
            ci_low = ci_high = None
            sufficiency = "INSUFFICIENT" if len(days) < 30 else "SUFFICIENT"
            if values and days and bootstrap_samples > 0:
                by_day = {day: [float(row[outcome_key]) for row in usable if row["signal_day"] == day] for day in days}
                rng = random.Random(seed)
                estimates = []
                for _ in range(bootstrap_samples):
                    sampled = [rng.choice(days) for _day in days]
                    sample_values = [value for day in sampled for value in by_day[day]]
                    estimates.append(statistics.fmean(sample_values))
                estimates.sort()
                ci_low = estimates[int(0.025 * (len(estimates) - 1))]
                ci_high = estimates[int(0.975 * (len(estimates) - 1))]
            item = dict(zip(dimensions, group_key))
            item.update(
                {
                    "scenario": scenario,
                    "n": len(usable),
                    "effective_n": len(days),
                    "expectancy_r": statistics.fmean(values) if values else None,
                    "ci95_low": ci_low,
                    "ci95_high": ci_high,
                    "hit_rate": sum(value > 0 for value in values) / len(values) if values else None,
                    "mean_cost_r": statistics.fmean(float(row[cost_key]) for row in usable) if usable else None,
                    "sufficiency": sufficiency,
                }
            )
            report.append(item)
    return report


def initialize_output_db(connection: sqlite3.Connection) -> None:
    try:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS shadow_labels (
                signal_id TEXT NOT NULL,
                scenario_version TEXT NOT NULL DEFAULT '2atr_2r_v1',
                payload_json TEXT NOT NULL,
                labeled_at_utc TEXT NOT NULL,
                PRIMARY KEY (signal_id, scenario_version)
            );
            CREATE TABLE IF NOT EXISTS shadow_reports (
                report_key TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                generated_at_utc TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shadow_runs (
                run_id TEXT PRIMARY KEY,
                started_at_utc TEXT NOT NULL,
                completed_at_utc TEXT,
                status TEXT NOT NULL,
                detail TEXT
            );
            """
        )
        connection.commit()
    except Exception as exc:
        log.exception("shadow output schema write failed exception=%s:%s", type(exc).__name__, exc)
        raise


def write_results(connection: sqlite3.Connection, labels: Sequence[ShadowLabel], report: Sequence[Mapping[str, object]]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    try:
        connection.executemany(
            """
            INSERT INTO shadow_labels(signal_id, scenario_version, payload_json, labeled_at_utc)
            VALUES (?, '2atr_2r_v1', ?, ?)
            ON CONFLICT(signal_id, scenario_version) DO UPDATE SET
                payload_json=excluded.payload_json,
                labeled_at_utc=excluded.labeled_at_utc
            """,
            [(label.signal_id, json.dumps(asdict(label), sort_keys=True), now) for label in labels],
        )
        connection.execute(
            """
            INSERT INTO shadow_reports(report_key, payload_json, generated_at_utc)
            VALUES ('weekly_latest', ?, ?)
            ON CONFLICT(report_key) DO UPDATE SET
                payload_json=excluded.payload_json,
                generated_at_utc=excluded.generated_at_utc
            """,
            (json.dumps(list(report), sort_keys=True), now),
        )
        connection.commit()
    except Exception as exc:
        connection.rollback()
        log.exception("shadow result write failed exception=%s:%s", type(exc).__name__, exc)
        raise


def open_source_readonly(path: Path, *, timeout_seconds: float = 5.0) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    if not resolved.is_absolute() or not resolved.is_file():
        raise FileNotFoundError(f"SOURCE_JOURNAL_NOT_FOUND:{resolved}")
    connection = sqlite3.connect(
        f"file:{resolved}?mode=ro",
        uri=True,
        timeout=timeout_seconds,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute(f"PRAGMA busy_timeout={max(1, int(timeout_seconds * 1000))}")
    return connection


def read_normalized_signals(connection: sqlite3.Connection) -> list[dict[str, object]]:
    deadline = datetime.now().timestamp() + 10.0
    connection.set_progress_handler(lambda: int(datetime.now().timestamp() > deadline), 10_000)
    try:
        rows = connection.execute(
            """
            SELECT signal_id, dedup_key, first_of_day, symbol, side, ts_utc,
                   model_name, strategy_version, intraday_regime, eod_regime,
                   feature_json, block_reason
            FROM v_signals_norm
            WHERE first_of_day = 1
            ORDER BY ts_utc, signal_id
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.set_progress_handler(None, 0)

