import os
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import List

from core.models import Position, Signal, Side, RegimeType
from core.order_router import OrderRouter
from data.db_models import DatabaseManager


def paper_starting_capital() -> float:
    raw = os.getenv("PAPER_STARTING_CAPITAL", "").strip()
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if value <= 0:
        raise RuntimeError("PAPER_STARTING_CAPITAL_MISSING_OR_INVALID")
    return value


def execute_paper_signal(
    db: DatabaseManager,
    router: OrderRouter,
    signal: Signal,
    qty: int,
    *,
    auto_executed_on_timeout: bool,
    is_intraday: bool,
) -> Position:
    order = router.route_order(
        signal,
        qty,
        auto_executed_on_timeout=auto_executed_on_timeout,
        is_intraday=is_intraday,
    )
    return db.record_paper_order_and_open_position(order)


def pending_row_to_signal(row, actual_entry: float, gap_filter_pct: float) -> Signal:
    original_entry = float(row["entry_price"])
    gap_pct = abs(actual_entry - original_entry) / original_entry * 100.0
    if gap_pct > gap_filter_pct:
        raise ValueError(f"GAP_FILTER_EXCEEDED:{gap_pct:.2f}%")

    stop = float(row["stop_price"])
    if actual_entry <= stop:
        raise ValueError("NEXT_SESSION_ENTRY_AT_OR_BELOW_STOP")

    original_risk = original_entry - stop
    original_reward = float(row["target_price"]) - original_entry
    r_multiple = original_reward / original_risk if original_risk > 0 else 0.0
    if r_multiple <= 0:
        raise ValueError("INVALID_PENDING_R_MULTIPLE")

    target = actual_entry + r_multiple * (actual_entry - stop)
    return Signal(
        symbol=row["symbol"],
        side=Side(row["side"]),
        entry_price=actual_entry,
        stop_price=stop,
        target_price=target,
        model_name=row["model_name"],
        regime=RegimeType(row["regime"]),
        rationale=f"{row['rationale']} | next-session paper entry",
        timestamp=datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None),
    )
