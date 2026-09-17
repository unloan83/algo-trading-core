import pandas as pd
from typing import Optional
from datetime import datetime
from core.models import Signal, Side, RegimeType
from core.regime_filter import calculate_ema

def evaluate_trend_pullback(
    symbol: str,
    df: pd.DataFrame,
    regime: RegimeType
) -> Optional[Signal]:
    """
    Evaluates Trend-pullback continuation setup (Swing, primary).
    Note: Cash Equities system ONLY supports BUY signals (long-only). Overnight cash shorting is prohibited by exchange.
    Requires at least 200 candles.
    """
    if len(df) < 200:
        return None

    # Trend pullback is active ONLY in TREND_UP or RANGE regimes (never TREND_DOWN or HIGH_RISK)
    if regime not in [RegimeType.TREND_UP, RegimeType.RANGE]:
        return None

    close = df['close']
    high = df['high']
    low = df['low']

    ema20 = calculate_ema(close, 20)
    ema50 = calculate_ema(close, 50)
    ema200 = calculate_ema(close, 200)

    # 1. Uptrend structure: Price > EMA20 > EMA50 > EMA200
    curr_close = close.iloc[-1]
    curr_high = high.iloc[-1]
    curr_ema20 = ema20.iloc[-1]
    curr_ema50 = ema50.iloc[-1]
    curr_ema200 = ema200.iloc[-1]

    prev_ema20 = ema20.iloc[-2]
    prev_ema50 = ema50.iloc[-2]

    uptrend = (curr_close > curr_ema20) and (curr_ema20 > curr_ema50) and (curr_ema50 > curr_ema200) and \
              (curr_ema20 > prev_ema20) and (curr_ema50 > prev_ema50)

    if not uptrend:
        return None

    # 2. Pullback day (candle -2 or -3 touched or came within 1% of 20-EMA)
    pullback_day = False
    pullback_low = low.iloc[-2]
    for i in range(-3, -1):
        dist_to_ema20 = abs(close.iloc[i] - ema20.iloc[i]) / ema20.iloc[i] * 100.0
        low_dist = abs(low.iloc[i] - ema20.iloc[i]) / ema20.iloc[i] * 100.0
        if dist_to_ema20 <= 1.0 or low_dist <= 1.0:
            pullback_day = True
            pullback_low = min(low.iloc[-3:-1])
            break

    if not pullback_day:
        return None

    # 3. Bullish confirmation close (today's close > yesterday's close and > yesterday's high)
    confirmation = (curr_close > close.iloc[-2]) and (curr_close > high.iloc[-2])
    if not confirmation:
        return None

    # Entry, Stop, Target calculation
    entry_price = float(curr_close)
    stop_price = float(pullback_low * 0.995) # 0.5% buffer below swing low
    risk_dist = entry_price - stop_price
    
    if risk_dist <= 0:
        return None

    target_price = float(entry_price + 1.5 * risk_dist)
    timestamp = df['timestamp'].iloc[-1] if 'timestamp' in df.columns else datetime.now()

    rationale = f"Uptrend pullback to 20-EMA ({curr_ema20:.1f}), bullish confirmation close above pullback high ({high.iloc[-2]:.1f})"

    try:
        signal = Signal(
            symbol=symbol,
            side=Side.BUY, # Explicitly long-only (BUY)
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            model_name="trend_pullback",
            regime=regime,
            rationale=rationale,
            timestamp=timestamp
        )
        return signal
    except ValueError:
        return None
