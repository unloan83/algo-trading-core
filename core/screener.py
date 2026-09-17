from typing import List, Dict
import pandas as pd
from core.models import Signal, RegimeType
from core.entry_models.trend_pullback import evaluate_trend_pullback
from core.entry_models.breakout import evaluate_breakout

class Screener:
    def __init__(self, universe_symbols: List[str]):
        self.universe_symbols = universe_symbols

    def run_eod_screen(
        self,
        symbol_data: Dict[str, pd.DataFrame],
        nifty_df: pd.DataFrame,
        regime: RegimeType
    ) -> List[Signal]:
        """
        Runs EOD swing screening (15:45-16:05 IST).
        Eligible in TREND_UP and RANGE regimes.
        """
        signals = []
        if regime == RegimeType.HIGH_RISK or regime == RegimeType.TREND_DOWN:
            return []

        for symbol in self.universe_symbols:
            if symbol not in symbol_data or symbol_data[symbol].empty:
                continue
            df = symbol_data[symbol]
            
            # Setup A: Trend Pullback
            sig_tp = evaluate_trend_pullback(symbol, df, regime)
            if sig_tp:
                signals.append(sig_tp)
                continue

            # Setup B: Breakout
            sig_bo = evaluate_breakout(symbol, df, nifty_df, regime)
            if sig_bo:
                signals.append(sig_bo)

        return signals

    def run_intraday_scan(
        self,
        symbol_data: Dict[str, pd.DataFrame],
        nifty_df: pd.DataFrame,
        regime: RegimeType
    ) -> List[Signal]:
        """
        Runs opportunistic intraday scanning (09:25-14:15 IST).
        Active ONLY when regime == TREND_UP.
        """
        signals = []
        if regime != RegimeType.TREND_UP:
            return []

        for symbol in self.universe_symbols:
            if symbol not in symbol_data or symbol_data[symbol].empty:
                continue
            df = symbol_data[symbol]
            
            sig_bo = evaluate_breakout(symbol, df, nifty_df, regime)
            if sig_bo:
                signals.append(sig_bo)

        return signals
