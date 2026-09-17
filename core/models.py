from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, field_validator, model_validator
from datetime import datetime

class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

class RegimeType(str, Enum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    HIGH_RISK = "HIGH_RISK"

class OrderStatus(str, Enum):
    CREATED = "CREATED"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXIT_PENDING = "EXIT_PENDING"
    CLOSED = "CLOSED"
    UNKNOWN = "UNKNOWN"

class Candle(BaseModel):
    timestamp: datetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_ohlc_bounds(self):
        if not (self.low <= self.open <= self.high):
            raise ValueError(f"Open price {self.open} outside low-high range [{self.low}, {self.high}]")
        if not (self.low <= self.close <= self.high):
            raise ValueError(f"Close price {self.close} outside low-high range [{self.low}, {self.high}]")
        if self.low > self.high:
            raise ValueError(f"Low price {self.low} greater than High price {self.high}")
        return self

class Signal(BaseModel):
    symbol: str
    side: Side
    entry_price: float = Field(gt=0)
    stop_price: float = Field(gt=0)
    target_price: float = Field(gt=0)
    model_name: str
    regime: RegimeType
    rationale: str
    timestamp: datetime

    @model_validator(mode="after")
    def validate_prices_and_stops(self):
        if self.side == Side.BUY:
            if self.stop_price >= self.entry_price:
                raise ValueError(f"Buy stop price {self.stop_price} must be below entry price {self.entry_price}")
            if self.target_price <= self.entry_price:
                raise ValueError(f"Buy target price {self.target_price} must be above entry price {self.entry_price}")
        elif self.side == Side.SELL:
            if self.stop_price <= self.entry_price:
                raise ValueError(f"Sell stop price {self.stop_price} must be above entry price {self.entry_price}")
            if self.target_price >= self.entry_price:
                raise ValueError(f"Sell target price {self.target_price} must be below entry price {self.entry_price}")

        stop_dist_pct = abs(self.entry_price - self.stop_price) / self.entry_price * 100.0
        if stop_dist_pct < 0.2 or stop_dist_pct > 25.0:
            raise ValueError(f"Stop distance {stop_dist_pct:.2f}% outside sanity band [0.2%, 25.0%]")

        return self

class RiskCheckResult(BaseModel):
    passed: bool
    reason_code: str
    computed_qty: int = 0
    rupee_risk: float = 0.0
    equity_now: float  # Removed gt=0 constraint so non-positive/zero equity can be cleanly represented
    daily_circuit_used_pct: float = 0.0
    weekly_circuit_used_pct: float = 0.0
    monthly_circuit_used_pct: float = 0.0

class Order(BaseModel):
    order_id: str
    symbol: str
    side: Side
    qty: int = Field(gt=0)
    entry_price: float = Field(gt=0)
    stop_price: float = Field(gt=0)
    target_price: float = Field(gt=0)
    status: OrderStatus = OrderStatus.CREATED
    created_at: datetime
    broker_order_id: Optional[str] = None
    filled_price: Optional[float] = None
    auto_executed_on_timeout: bool = False
    is_paper: bool = True

class Position(BaseModel):
    position_id: str
    symbol: str
    side: Side
    qty: int = Field(gt=0)
    entry_price: float = Field(gt=0)
    current_price: float = Field(gt=0)
    stop_price: float = Field(gt=0)
    target_price: float = Field(gt=0)
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    opened_at: datetime
    is_paper: bool = True
