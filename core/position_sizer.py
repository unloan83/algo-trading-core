import math
from typing import List
from core.models import RiskCheckResult, Position

def calculate_position_size(
    equity_now: float,
    available_cash: float,
    entry_price: float,
    stop_price: float,
    open_positions: List[Position],
    risk_per_trade_pct: float = 0.5,
    max_open_risk_pct: float = 1.5
) -> RiskCheckResult:
    """
    Computes position size strictly following:
    risk_amount = equity_now * (risk_per_trade_pct / 100)
    qty = floor(risk_amount / abs(entry_price - stop_price))

    Requires equity_now and available_cash with NO default values.
    Hard blocks trade if cash is insufficient or open risk budget is exceeded (no silent scaling).
    """
    if equity_now <= 0:
        return RiskCheckResult(
            passed=False,
            reason_code="EQUITY_ZERO_OR_NEGATIVE",
            computed_qty=0,
            rupee_risk=0.0,
            equity_now=equity_now # Report true equity_now, never a fake 1.0 placeholder
        )

    risk_dist = abs(entry_price - stop_price)
    if risk_dist <= 1e-6:
        return RiskCheckResult(
            passed=False,
            reason_code="ZERO_STOP_DISTANCE",
            computed_qty=0,
            rupee_risk=0.0,
            equity_now=equity_now
        )

    risk_amount = equity_now * (risk_per_trade_pct / 100.0)
    raw_qty = math.floor(risk_amount / risk_dist)

    if raw_qty <= 0:
        return RiskCheckResult(
            passed=False,
            reason_code="RISK_BUDGET_TOO_SMALL",
            computed_qty=0,
            rupee_risk=0.0,
            equity_now=equity_now
        )

    # 1. Hard check cash availability (no silent partial-size scaling)
    total_cost = raw_qty * entry_price
    if total_cost > available_cash:
        return RiskCheckResult(
            passed=False,
            reason_code="INSUFFICIENT_CASH_FOR_FULL_SIZE",
            computed_qty=0,
            rupee_risk=0.0,
            equity_now=equity_now
        )

    # 2. Hard check open risk budget (sum of open position risks + new trade risk)
    existing_open_risk = sum(
        pos.qty * abs(pos.entry_price - pos.stop_price)
        for pos in open_positions
    )
    new_trade_risk = raw_qty * risk_dist
    total_new_open_risk = existing_open_risk + new_trade_risk
    max_allowed_open_risk = equity_now * (max_open_risk_pct / 100.0)

    if total_new_open_risk > max_allowed_open_risk:
        return RiskCheckResult(
            passed=False,
            reason_code="MAX_CONCURRENT_OPEN_RISK_EXCEEDED",
            computed_qty=0,
            rupee_risk=0.0,
            equity_now=equity_now
        )

    return RiskCheckResult(
        passed=True,
        reason_code="PASSED",
        computed_qty=raw_qty,
        rupee_risk=new_trade_risk,
        equity_now=equity_now
    )
