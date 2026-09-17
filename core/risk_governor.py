from datetime import datetime, timedelta
from typing import List, Optional, Dict
from core.models import Signal, Position, RiskCheckResult, Side
from core.position_sizer import calculate_position_size

class RiskGovernor:
    def __init__(
        self,
        risk_per_trade_pct: float = 0.5,
        max_open_risk_pct: float = 1.5,
        daily_circuit_pct: float = 1.5,
        weekly_circuit_pct: float = 3.0,
        monthly_circuit_pct: float = 4.5,
        max_positions: int = 4,
        cooldown_losses_trigger: int = 3,
        cooldown_hours: int = 24,
        no_averaging_down: bool = True
    ):
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_open_risk_pct = max_open_risk_pct
        self.daily_circuit_pct = daily_circuit_pct
        self.weekly_circuit_pct = weekly_circuit_pct
        self.monthly_circuit_pct = monthly_circuit_pct
        self.max_positions = max_positions
        self.cooldown_losses_trigger = cooldown_losses_trigger
        self.cooldown_hours = cooldown_hours
        self.no_averaging_down = no_averaging_down

    def validate_signal_against_invariants(
        self,
        signal: Signal,
        equity_now: float,
        available_cash: float,
        open_positions: List[Position],
        daily_pnl: float,
        weekly_pnl: float,
        monthly_pnl: float,
        consecutive_losses: int,
        last_loss_time: Optional[datetime],
        halted_symbols: List[str],
        corporate_action_symbols: List[str],
        broker_account_ok: bool,
        candle_interval_seconds: int = 86400,
        now: Optional[datetime] = None
    ) -> RiskCheckResult:
        """
        Evaluates signal against all 11 hard invariants in strict order.
        Requires equity_now and available_cash with NO default values.
        """
        current_time = now or datetime.now()

        # Guard against zero or negative equity first (prevents ZeroDivisionError in circuit math)
        if equity_now <= 0:
            return RiskCheckResult(passed=False, reason_code="EQUITY_ZERO_OR_NEGATIVE", equity_now=equity_now)

        # Invariant 1 & 2: Stop price missing, invalid, or wrong side / equal entry
        if signal.stop_price <= 0:
            return RiskCheckResult(passed=False, reason_code="INV1_INVALID_STOP_PRICE", equity_now=equity_now)
        if signal.entry_price <= 0:
            return RiskCheckResult(passed=False, reason_code="INV1_INVALID_ENTRY_PRICE", equity_now=equity_now)

        if signal.side == Side.BUY and signal.stop_price >= signal.entry_price:
            return RiskCheckResult(passed=False, reason_code="INV1_BUY_STOP_ABOVE_ENTRY", equity_now=equity_now)
        if signal.side == Side.SELL and signal.stop_price <= signal.entry_price:
            return RiskCheckResult(passed=False, reason_code="INV1_SELL_STOP_BELOW_ENTRY", equity_now=equity_now)

        if abs(signal.entry_price - signal.stop_price) < 1e-6:
            return RiskCheckResult(passed=False, reason_code="INV2_ZERO_STOP_DISTANCE", equity_now=equity_now)

        # Invariant 4a: Max concurrent positions check
        if len(open_positions) >= self.max_positions:
            return RiskCheckResult(passed=False, reason_code="INV4_MAX_CONCURRENT_POSITIONS_REACHED", equity_now=equity_now)

        # Invariant 5: Circuit Breakers (Daily, Weekly, Monthly)
        daily_loss_pct = abs(min(daily_pnl, 0.0)) / equity_now * 100.0
        weekly_loss_pct = abs(min(weekly_pnl, 0.0)) / equity_now * 100.0
        monthly_loss_pct = abs(min(monthly_pnl, 0.0)) / equity_now * 100.0

        if daily_loss_pct >= self.daily_circuit_pct:
            return RiskCheckResult(
                passed=False,
                reason_code="INV5_DAILY_LOSS_CIRCUIT_BREACHED",
                equity_now=equity_now,
                daily_circuit_used_pct=daily_loss_pct
            )

        if weekly_loss_pct >= self.weekly_circuit_pct:
            return RiskCheckResult(
                passed=False,
                reason_code="INV5_WEEKLY_LOSS_CIRCUIT_BREACHED",
                equity_now=equity_now,
                weekly_circuit_used_pct=weekly_loss_pct
            )

        if monthly_loss_pct >= self.monthly_circuit_pct:
            return RiskCheckResult(
                passed=False,
                reason_code="INV5_MONTHLY_LOSS_CIRCUIT_BREACHED",
                equity_now=equity_now,
                monthly_circuit_used_pct=monthly_loss_pct
            )

        # Invariant 6: Consecutive loss cooldown
        if consecutive_losses >= self.cooldown_losses_trigger and last_loss_time:
            cooldown_end = last_loss_time + timedelta(hours=self.cooldown_hours)
            if current_time < cooldown_end:
                return RiskCheckResult(passed=False, reason_code="INV6_CONSECUTIVE_LOSS_COOLDOWN_ACTIVE", equity_now=equity_now)

        # Invariant 7: Trading halt / circuit filter
        if signal.symbol in halted_symbols:
            return RiskCheckResult(passed=False, reason_code="INV7_SYMBOL_TRADING_HALTED", equity_now=equity_now)

        # Invariant 8: Corporate action pending
        if signal.symbol in corporate_action_symbols:
            return RiskCheckResult(passed=False, reason_code="INV8_CORPORATE_ACTION_PENDING", equity_now=equity_now)

        # Invariant 9: Broker API account health
        if not broker_account_ok:
            return RiskCheckResult(passed=False, reason_code="INV9_BROKER_ACCOUNT_ERROR", equity_now=equity_now)

        # Invariant 10: Signal timestamp freshness
        signal_age_seconds = (current_time - signal.timestamp).total_seconds()
        if signal_age_seconds > candle_interval_seconds:
            return RiskCheckResult(passed=False, reason_code="INV10_STALE_SIGNAL_TIMESTAMP", equity_now=equity_now)

        # Invariant 11: Averaging down check
        if self.no_averaging_down:
            for pos in open_positions:
                if pos.symbol == signal.symbol:
                    is_losing = (pos.side == Side.BUY and pos.current_price < pos.entry_price) or \
                                (pos.side == Side.SELL and pos.current_price > pos.entry_price)
                    if is_losing:
                        return RiskCheckResult(passed=False, reason_code="INV11_NO_AVERAGING_DOWN", equity_now=equity_now)

        # Calculate position size & Check open risk budget (Invariants 3 & 4b)
        size_res = calculate_position_size(
            equity_now=equity_now,
            available_cash=available_cash,
            entry_price=signal.entry_price,
            stop_price=signal.stop_price,
            open_positions=open_positions,
            risk_per_trade_pct=self.risk_per_trade_pct,
            max_open_risk_pct=self.max_open_risk_pct
        )

        size_res.daily_circuit_used_pct = daily_loss_pct
        size_res.weekly_circuit_used_pct = weekly_loss_pct
        size_res.monthly_circuit_used_pct = monthly_loss_pct

        return size_res
