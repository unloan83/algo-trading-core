import math
import logging
from typing import List
from core.models import RiskCheckResult, Position


log = logging.getLogger(__name__)


def calculate_position_size(
    equity_now: float,
    available_cash: float,
    entry_price: float,
    stop_price: float,
    open_positions: List[Position],
    max_concurrent_positions: int,
    risk_per_trade_pct: float = 0.5,
    max_open_risk_pct: float = 1.5
) -> RiskCheckResult:
    """
    Computes position size strictly following:
    risk_amount = equity_now * (risk_per_trade_pct / 100)
    risk_qty = risk_amount / abs(entry_price - stop_price)
    capital_qty = (available_cash / max_concurrent_positions) / entry_price
    qty = floor(min(risk_qty, capital_qty))

    Requires equity_now and available_cash with NO default values.
    Enforces risk and per-position capital constraints jointly, then hard blocks if
    the portfolio open-risk budget is exceeded.
    """
    if equity_now <= 0:
        return RiskCheckResult(
            passed=False,
            reason_code="EQUITY_ZERO_OR_NEGATIVE",
            computed_qty=0,
            rupee_risk=0.0,
            equity_now=equity_now # Report true equity_now, never a fake 1.0 placeholder
        )

    if max_concurrent_positions <= 0:
        return RiskCheckResult(
            passed=False,
            reason_code="INVALID_MAX_CONCURRENT_POSITIONS",
            computed_qty=0,
            rupee_risk=0.0,
            equity_now=equity_now
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
    max_position_value = available_cash / max_concurrent_positions
    risk_qty = risk_amount / risk_dist
    capital_qty = max_position_value / entry_price
    binding_constraint = (
        "risk_qty"
        if risk_qty < capital_qty
        else "capital_qty"
        if capital_qty < risk_qty
        else "equal"
    )
    raw_qty = math.floor(min(risk_qty, capital_qty))

    log.info(
        "position_sizing risk_qty=%.6f capital_qty=%.6f "
        "binding_constraint=%s computed_qty=%d available_cash=%.2f "
        "max_concurrent_positions=%d",
        risk_qty,
        capital_qty,
        binding_constraint,
        raw_qty,
        available_cash,
        max_concurrent_positions,
    )

    if raw_qty <= 0:
        return RiskCheckResult(
            passed=False,
            reason_code=(
                "CAPITAL_BUDGET_TOO_SMALL"
                if binding_constraint == "capital_qty"
                else "RISK_BUDGET_TOO_SMALL"
            ),
            computed_qty=0,
            rupee_risk=0.0,
            equity_now=equity_now
        )

    # Hard check open risk budget (sum of open position risks + new trade risk)
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
