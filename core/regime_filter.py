import pandas as pd
import numpy as np
from typing import Tuple
from core.models import RegimeType

def calculate_ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()

def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift()).abs()
    low_close = (df['low'] - df['close'].shift()).abs()
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = ranges.max(axis=1)
    return true_range.rolling(period).mean()

def calculate_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    up_move = df['high'] - df['high'].shift(1)
    down_move = df['low'].shift(1) - df['low']
    
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    
    tr = calculate_atr(df, 1)
    tr_smooth = tr.rolling(period).sum()
    
    plus_di = 100 * (pd.Series(plus_dm, index=df.index).rolling(period).sum() / tr_smooth)
    minus_di = 100 * (pd.Series(minus_dm, index=df.index).rolling(period).sum() / tr_smooth)
    
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx = dx.rolling(period).mean()
    return adx

def evaluate_regime(nifty_df: pd.DataFrame) -> Tuple[RegimeType, str]:
    """
    Evaluates market regime using Nifty 50 OHLC dataframe.
    Requires at least 50 candles.
    """
    if len(nifty_df) < 50:
        return RegimeType.HIGH_RISK, "Insufficient historical data for Nifty (<50 candles)"
    
    close = nifty_df['close'].iloc[-1]
    ema_50 = calculate_ema(nifty_df['close'], 50).iloc[-1]
    
    adx_series = calculate_adx(nifty_df, 14)
    adx_val = adx_series.iloc[-1] if not pd.isna(adx_series.iloc[-1]) else 0.0
    
    atr_series = calculate_atr(nifty_df, 14)
    atr_pct = (atr_series / nifty_df['close']) * 100.0
    current_atr_pct = atr_pct.iloc[-1]
    avg_atr_pct_20d = atr_pct.iloc[-20:].mean()
    
    # Check High Risk regime first
    if current_atr_pct > 2.0 * avg_atr_pct_20d:
        return RegimeType.HIGH_RISK, f"Daily ATR% ({current_atr_pct:.2f}%) > 2x 20d avg ({avg_atr_pct_20d:.2f}%)"
    
    if adx_val <= 20.0:
        return RegimeType.RANGE, f"ADX(14)={adx_val:.1f} <= 20 (Range-bound regime)"
    
    if close > ema_50 and adx_val > 20.0:
        return RegimeType.TREND_UP, f"Nifty Close ({close:.1f}) > 50-EMA ({ema_50:.1f}) & ADX={adx_val:.1f}"
    
    if close < ema_50 and adx_val > 20.0:
        return RegimeType.TREND_DOWN, f"Nifty Close ({close:.1f}) < 50-EMA ({ema_50:.1f}) & ADX={adx_val:.1f}"
        
    return RegimeType.HIGH_RISK, "Default fallback regime"
