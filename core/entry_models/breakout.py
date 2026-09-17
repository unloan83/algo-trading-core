import pandas as pd
from typing import Optional
from datetime import datetime
from core.models import Signal, Side, RegimeType
from core.regime_filter import calculate_atr

def evaluate_breakout(
    symbol: str,
    df: pd.DataFrame,
    nifty_df: pd.DataFrame,
    regime: RegimeType
) -> Optional[Signal]:
    """
    Evaluates Confirmed Breakout setup (Swing & Intraday when regime = TREND_UP).
    Note: Cash Equities system ONLY supports BUY signals (long-only).
    Requires at least 25 candles.
    """
    if len(df) < 25 or len(nifty_df) < 25:
        return None

    # Breakout is active ONLY in TREND_UP or RANGE regimes
    if regime not in [RegimeType.TREND_UP, RegimeType.RANGE]:
        return None

    close = df['close']
    high = df['high']
    low = df['low']
    volume = df['volume']

    curr_close = close.iloc[-1]
    curr_volume = volume.iloc[-1]

    # 1. Break >= 15-session consolidation high
    consolidation_high = high.iloc[-16:-1].max()
    consolidation_low = low.iloc[-16:-1].min()

    is_breakout = curr_close > consolidation_high
    if not is_breakout:
        return None

    # 2. Volume on breakout day >= 1.5x 20-day average volume
    avg_volume_20d = volume.iloc[-21:-1].mean()
    if avg_volume_20d > 0 and curr_volume < 1.5 * avg_volume_20d:
        return None

    # 3. Relative strength vs Nifty50 over trailing 20 sessions
    stock_perf = (close.iloc[-1] - close.iloc[-21]) / close.iloc[-21]
    nifty_perf = (nifty_df['close'].iloc[-1] - nifty_df['close'].iloc[-21]) / nifty_df['close'].iloc[-21]
    rs_positive = stock_perf > nifty_perf
    if not rs_positive:
        return None

    # Stop distance: tighter of base low or 2x ATR
    atr_14 = calculate_atr(df, 14).iloc[-1]
    stop_atr = curr_close - (2.0 * atr_14)
    stop_base = consolidation_low

    stop_price = max(stop_atr, stop_base)
    entry_price = float(curr_close)
    
    if stop_price >= entry_price:
        return None

    risk_dist = entry_price - stop_price
    target_price = float(entry_price + 2.0 * risk_dist)
    timestamp = df['timestamp'].iloc[-1] if 'timestamp' in df.columns else datetime.now()

    rationale = f"15-session consolidation breakout above {consolidation_high:.1f} with {curr_volume / avg_volume_20d:.1f}x avg vol & RS>0"

    try:
        signal = Signal(
            symbol=symbol,
            side=Side.BUY, # Explicitly long-only (BUY)
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            model_name="breakout",
            regime=regime,
            rationale=rationale,
            timestamp=timestamp
        )
        return signal
    except ValueError:
        return None
