#!/usr/bin/env python3
"""
Replay Production Trend-Pullback Setup for research/edge branch.
Uses cached bar datasets in .cache/research_edge_bars.
Evaluates 70/30 In-Sample vs Holdout performance.
"""
import os
import sys
import json
import logging
from datetime import datetime, time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from core.models import RegimeType
from core.regime_filter import calculate_ema, calculate_adx, calculate_atr
from backtest.cost_model import calculate_indian_transaction_costs

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("replay_trend_pullback")

CACHE_DIR = PROJECT_ROOT / ".cache" / "research_edge_bars"

def load_cached_datasets():
    daily_path = CACHE_DIR / "daily_bars.pkl"
    intraday_path = CACHE_DIR / "intraday_5m_bars.pkl"
    meta_path = CACHE_DIR / "dataset_metadata.json"

    if not daily_path.exists() or not intraday_path.exists():
        raise RuntimeError("Cached bars not found in .cache/research_edge_bars. Run build_research_dataset.py first.")

    daily_dict = pd.read_pickle(daily_path)
    intraday_dict = pd.read_pickle(intraday_path)
    meta = json.loads(meta_path.read_text())
    return daily_dict, intraday_dict, meta

def compute_intraday_nifty_regime(nifty_5m: pd.DataFrame) -> pd.DataFrame:
    df = nifty_5m.sort_values("timestamp").reset_index(drop=True).copy()
    close = df['close']
    ema_50 = calculate_ema(close, 50)
    adx_14 = calculate_adx(df, 14)
    atr_14 = calculate_atr(df, 14)
    atr_pct = (atr_14 / close) * 100.0
    avg_atr_pct_20d = atr_pct.rolling(20).mean()

    is_high_risk = atr_pct > 2.0 * avg_atr_pct_20d
    is_range = adx_14 <= 20.0
    is_trend_up = (close > ema_50) & (adx_14 > 20.0) & (~is_high_risk)
    is_trend_down = (close < ema_50) & (adx_14 > 20.0) & (~is_high_risk)

    regime_series = pd.Series("HIGH_RISK", index=df.index)
    regime_series[is_range & (~is_high_risk)] = "RANGE"
    regime_series[is_trend_up] = "TREND_UP"
    regime_series[is_trend_down] = "TREND_DOWN"

    df["intraday_nifty_regime"] = regime_series
    return df

def compute_daily_nifty_regimes(nifty_daily: pd.DataFrame) -> Dict[pd.Timestamp, str]:
    df = nifty_daily.sort_values("timestamp").reset_index(drop=True).copy()
    close = df['close']
    ema_50 = calculate_ema(close, 50)
    adx_14 = calculate_adx(df, 14)
    atr_14 = calculate_atr(df, 14)
    atr_pct = (atr_14 / close) * 100.0
    avg_atr_pct_20d = atr_pct.rolling(20).mean()

    is_high_risk = atr_pct > 2.0 * avg_atr_pct_20d
    is_range = adx_14 <= 20.0
    is_trend_up = (close > ema_50) & (adx_14 > 20.0) & (~is_high_risk)
    is_trend_down = (close < ema_50) & (adx_14 > 20.0) & (~is_high_risk)

    regime_series = pd.Series("HIGH_RISK", index=df.index)
    regime_series[is_range & (~is_high_risk)] = "RANGE"
    regime_series[is_trend_up] = "TREND_UP"
    regime_series[is_trend_down] = "TREND_DOWN"

    df["date"] = pd.to_datetime(df["timestamp"]).dt.date
    regime_map = {}
    for i in range(1, len(df)):
        current_date = df.loc[i, "date"]
        prior_regime = regime_series.iloc[i-1]
        regime_map[current_date] = prior_regime

    return regime_map

def find_trend_pullback_signals_for_symbol(
    symbol: str,
    df_5m: pd.DataFrame,
    nifty_5m_gated: pd.DataFrame
) -> pd.DataFrame:
    """
    Evaluates production trend_pullback on 5-min bars for a single symbol.
    """
    df = df_5m.sort_values("timestamp").reset_index(drop=True).copy()
    if len(df) < 200:
        return pd.DataFrame()

    # Merge intraday Nifty regime
    nifty_merged = pd.merge_asof(
        df[["timestamp"]],
        nifty_5m_gated[["timestamp", "intraday_nifty_regime"]].sort_values("timestamp"),
        on="timestamp",
        direction="backward"
    )
    df["intraday_nifty_regime"] = nifty_merged["intraday_nifty_regime"]

    close = df["close"]
    high = df["high"]
    low = df["low"]

    ema20 = calculate_ema(close, 20)
    ema50 = calculate_ema(close, 50)
    ema200 = calculate_ema(close, 200)

    # 1. Uptrend structure
    uptrend = (
        (close > ema20) &
        (ema20 > ema50) &
        (ema50 > ema200) &
        (ema20 > ema20.shift(1)) &
        (ema50 > ema50.shift(1))
    )

    # 2. Pullback check: close or low of t-1 or t-2 within 1% of EMA20
    dist_close_t1 = (close.shift(1) - ema20.shift(1)).abs() / ema20.shift(1) * 100.0
    dist_low_t1 = (low.shift(1) - ema20.shift(1)).abs() / ema20.shift(1) * 100.0
    dist_close_t2 = (close.shift(2) - ema20.shift(2)).abs() / ema20.shift(2) * 100.0
    dist_low_t2 = (low.shift(2) - ema20.shift(2)).abs() / ema20.shift(2) * 100.0

    pullback_t1 = (dist_close_t1 <= 1.0) | (dist_low_t1 <= 1.0)
    pullback_t2 = (dist_close_t2 <= 1.0) | (dist_low_t2 <= 1.0)
    pullback_day = pullback_t1 | pullback_t2

    pullback_low = np.minimum(low.shift(1), low.shift(2))

    # 3. Confirmation close: close[t] > close[t-1] and close[t] > high[t-1]
    confirmation = (close > close.shift(1)) & (close > high.shift(1))

    # 4. Gated condition: Intraday NIFTY regime MUST BE TREND_UP
    is_signal = (
        (df["intraday_nifty_regime"] == "TREND_UP") &
        uptrend &
        pullback_day &
        confirmation
    )

    stop_price = pullback_low * 0.995
    risk_dist = close - stop_price
    valid_risk = risk_dist > 0

    df["is_signal"] = is_signal & valid_risk
    df["stop_price"] = stop_price
    df["target_price"] = close + 2.0 * risk_dist # 2R target as specified in prompt

    # Filter to signal bars
    signals_df = df[df["is_signal"]].copy()
    if signals_df.empty:
        return pd.DataFrame()

    signals_df["bar_index"] = signals_df.index
    signals_df["date"] = pd.to_datetime(signals_df["timestamp"]).dt.date

    # Deduplicate: first signal per symbol per day
    signals_df = signals_df.groupby("date", as_index=False).first()

    return signals_df

def simulate_trade(
    signal_row: pd.Series,
    full_symbol_df: pd.DataFrame
) -> Optional[Dict]:
    sig_index = int(signal_row["bar_index"])
    if sig_index + 1 >= len(full_symbol_df):
        return None

    entry_bar = full_symbol_df.iloc[sig_index + 1]
    entry_timestamp = entry_bar["timestamp"]
    
    # Check if entry bar belongs to the same trading day or next day
    sig_date = signal_row["date"]
    entry_date = pd.to_datetime(entry_timestamp).date()
    if entry_date != sig_date:
        # Signal was on last bar of day, skip trade
        return None

    entry_price_raw = float(entry_bar["open"])
    # 0.05% slippage on entry
    entry_price_exec = entry_price_raw * 1.0005

    stop_price = float(signal_row["stop_price"])
    target_price = float(signal_row["target_price"])
    signal_close = float(signal_row["close"])
    r_unit = signal_close - stop_price

    if r_unit <= 0:
        return None

    FIXED_RISK_INR = 500.0
    qty = max(1, int(FIXED_RISK_INR / r_unit))

    # Vectorized search for exit starting from entry bar (sig_index + 1)
    remaining_bars = full_symbol_df.iloc[sig_index + 1:]
    
    rem_highs = remaining_bars["high"].values
    rem_lows = remaining_bars["low"].values
    rem_closes = remaining_bars["close"].values
    rem_times = remaining_bars["time"].values

    hit_stop = rem_lows <= stop_price
    hit_target = rem_highs >= target_price
    hit_time = rem_times >= time(15, 15)

    hit_any = hit_stop | hit_target | hit_time
    if not np.any(hit_any):
        exit_idx = len(remaining_bars) - 1
        exit_price_raw = float(rem_closes[exit_idx])
        exit_timestamp = remaining_bars.iloc[exit_idx]["timestamp"]
        exit_reason = "EOD"
    else:
        first_exit = int(np.argmax(hit_any))
        b_low = rem_lows[first_exit]
        b_high = rem_highs[first_exit]

        if b_low <= stop_price and b_high >= target_price:
            exit_price_raw = stop_price
            exit_reason = "STOP"
        elif b_low <= stop_price:
            exit_price_raw = stop_price
            exit_reason = "STOP"
        elif b_high >= target_price:
            exit_price_raw = target_price
            exit_reason = "TARGET"
        else:
            exit_price_raw = float(rem_closes[first_exit])
            exit_reason = "TIME_EXIT"
        exit_timestamp = remaining_bars.iloc[first_exit]["timestamp"]

    # Gross PnL and R
    gross_pnl_per_unit = exit_price_raw - entry_price_raw
    gross_r = gross_pnl_per_unit / r_unit

    # Costs and Net calculation
    gross_pnl_inr, net_pnl_inr, total_costs_inr, total_slippage_inr = calculate_indian_transaction_costs(
        entry_price_raw, exit_price_raw, qty, is_intraday=True, slippage_pct=0.05
    )
    net_pnl_per_unit = net_pnl_inr / qty
    net_r = net_pnl_per_unit / r_unit

    return {
        "symbol": signal_row["symbol"],
        "date": str(sig_date),
        "signal_timestamp": str(signal_row["timestamp"]),
        "entry_timestamp": str(entry_timestamp),
        "exit_timestamp": str(exit_timestamp),
        "signal_close": signal_close,
        "entry_price_raw": entry_price_raw,
        "entry_price_exec": entry_price_raw * 1.0005,
        "stop_price": stop_price,
        "target_price": target_price,
        "exit_price_raw": exit_price_raw,
        "exit_price_exec": exit_price_raw * 0.9995,
        "exit_reason": exit_reason,
        "qty": qty,
        "r_unit": r_unit,
        "gross_r": gross_r,
        "net_r": net_r,
        "total_cost_inr": total_costs_inr,
        "is_win": net_r > 0
    }

def run_bootstrap_ci(r_series: pd.Series, day_series: pd.Series, n_resamples: int = 10000, seed: int = 42) -> Tuple[float, float]:
    if len(r_series) == 0:
        return 0.0, 0.0
    
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({"r": r_series.values, "day": day_series.values})
    unique_days = df["day"].unique()
    n_days = len(unique_days)

    if n_days == 0:
        return 0.0, 0.0

    # Map day to array of r values
    day_to_r = {d: group["r"].values for d, group in df.groupby("day")}

    means = []
    for _ in range(n_resamples):
        sampled_days = rng.choice(unique_days, size=n_days, replace=True)
        sampled_r = np.concatenate([day_to_r[d] for d in sampled_days])
        means.append(np.mean(sampled_r))

    ci_lower = np.percentile(means, 2.5)
    ci_upper = np.percentile(means, 97.5)
    return float(ci_lower), float(ci_upper)

def compute_metrics(trades_df: pd.DataFrame) -> Dict:
    if trades_df.empty:
        return {
            "signals": 0,
            "day_clusters": 0,
            "win_rate_pct": 0.0,
            "avg_win_r": 0.0,
            "avg_loss_r": 0.0,
            "gross_expectancy_r": 0.0,
            "net_expectancy_r": 0.0,
            "net_expectancy_ci_95": [0.0, 0.0],
            "gross_expectancy_ci_95": [0.0, 0.0],
            "break_even_win_rate_pct": 0.0
        }

    n_signals = len(trades_df)
    n_days = trades_df["date"].nunique()
    wins = trades_df[trades_df["net_r"] > 0]
    losses = trades_df[trades_df["net_r"] <= 0]

    win_rate = (len(wins) / n_signals) * 100.0
    avg_win_r = wins["net_r"].mean() if len(wins) > 0 else 0.0
    avg_loss_r = losses["net_r"].mean() if len(losses) > 0 else 0.0

    gross_exp = trades_df["gross_r"].mean()
    net_exp = trades_df["net_r"].mean()

    # Day-clustered bootstrap CI for net and gross
    net_ci_lower, net_ci_upper = run_bootstrap_ci(trades_df["net_r"], trades_df["date"])
    gross_ci_lower, gross_ci_upper = run_bootstrap_ci(trades_df["gross_r"], trades_df["date"])

    # Break-even win rate calculation: W_be = |avg_loss_r| / (avg_win_r + |avg_loss_r|)
    abs_loss = abs(avg_loss_r) if avg_loss_r != 0 else 1.0
    be_win_rate = (abs_loss / (avg_win_r + abs_loss)) * 100.0 if (avg_win_r + abs_loss) > 0 else 0.0

    return {
        "signals": n_signals,
        "day_clusters": n_days,
        "win_rate_pct": round(win_rate, 2),
        "avg_win_r": round(float(avg_win_r), 4),
        "avg_loss_r": round(float(avg_loss_r), 4),
        "gross_expectancy_r": round(float(gross_exp), 4),
        "net_expectancy_r": round(float(net_exp), 4),
        "net_expectancy_ci_95": [round(net_ci_lower, 4), round(net_ci_upper, 4)],
        "gross_expectancy_ci_95": [round(gross_ci_lower, 4), round(gross_ci_upper, 4)],
        "break_even_win_rate_pct": round(be_win_rate, 2)
    }

def main():
    log.info("Loading cached daily and intraday 5m bar datasets...")
    daily_dict, intraday_dict, meta = load_cached_datasets()

    nifty_key = "NIFTY 50" if "NIFTY 50" in intraday_dict else "NSE_INDEX|Nifty 50"
    nifty_5m = intraday_dict[nifty_key]
    nifty_daily = daily_dict[nifty_key]

    log.info("Computing intraday Nifty regime...")
    nifty_5m_gated = compute_intraday_nifty_regime(nifty_5m)
    
    log.info("Computing daily Nifty prior-session regime map...")
    prior_daily_regime_map = compute_daily_nifty_regimes(nifty_daily)

    all_trades = []

    stock_symbols = [s for s in intraday_dict.keys() if s != nifty_key]
    log.info(f"Scanning {len(stock_symbols)} stocks for trend_pullback setups...")

    for symbol in stock_symbols:
        df_5m = intraday_dict[symbol].copy()
        df_5m["symbol"] = symbol
        dt_series = pd.to_datetime(df_5m["timestamp"])
        df_5m["dt_timestamp"] = dt_series
        df_5m["time"] = dt_series.dt.time

        signals = find_trend_pullback_signals_for_symbol(symbol, df_5m, nifty_5m_gated)
        if signals.empty:
            continue

        for _, sig in signals.iterrows():
            trade = simulate_trade(sig, df_5m)
            if trade:
                trade["prior_daily_regime"] = prior_daily_regime_map.get(pd.to_datetime(trade["date"]).date(), "UNKNOWN")
                all_trades.append(trade)

    trades_df = pd.DataFrame(all_trades)
    log.info(f"Total simulated trend_pullback trades: {len(trades_df)}")

    if trades_df.empty:
        log.warning("No trades generated for trend_pullback setup.")
        return

    # Chronological 70/30 In-Sample vs Holdout Split
    unique_dates = sorted(trades_df["date"].unique())
    n_total_dates = len(unique_dates)
    n_is_dates = int(n_total_dates * 0.70)
    is_dates = set(unique_dates[:n_is_dates])
    holdout_dates = set(unique_dates[n_is_dates:])

    is_trades = trades_df[trades_df["date"].isin(is_dates)].copy()
    holdout_trades = trades_df[trades_df["date"].isin(holdout_dates)].copy()

    is_metrics = compute_metrics(is_trades)
    holdout_metrics = compute_metrics(holdout_trades)
    full_metrics = compute_metrics(trades_df)

    log.info("=== TREND PULLBACK REPLAY RESULTS ===")
    log.info(f"In-Sample Metrics: {json.dumps(is_metrics, indent=2)}")
    log.info(f"Holdout Metrics: {json.dumps(holdout_metrics, indent=2)}")
    log.info(f"Full Period Metrics: {json.dumps(full_metrics, indent=2)}")

    # Check Kill Rule
    # "if holdout gross expectancy < +0.3R or its CI includes 0, mark REJECT per strategy_research.md and stop."
    holdout_gross_exp = holdout_metrics["gross_expectancy_r"]
    gross_ci = holdout_metrics["gross_expectancy_ci_95"]

    kill_triggered = False
    kill_reasons = []

    if holdout_gross_exp < 0.3:
        kill_triggered = True
        kill_reasons.append(f"Holdout gross expectancy ({holdout_gross_exp}R) < +0.3R threshold")
    
    if gross_ci[0] <= 0 and gross_ci[1] >= 0:
        kill_triggered = True
        kill_reasons.append(f"Holdout 95% gross CI [{gross_ci[0]}, {gross_ci[1]}] includes 0")
    elif gross_ci[0] < 0:
        kill_triggered = True
        kill_reasons.append(f"Holdout 95% gross CI lower bound ({gross_ci[0]}) <= 0")

    verdict = "REJECT" if kill_triggered else "PASS"

    report = {
        "strategy": "trend_pullback",
        "in_sample": is_metrics,
        "holdout": holdout_metrics,
        "full_period": full_metrics,
        "verdict": verdict,
        "kill_reasons": kill_reasons
    }

    report_path = CACHE_DIR / "trend_pullback_research_report.json"
    report_path.write_text(json.dumps(report, indent=2))
    log.info(f"Saved research report to {report_path}")

if __name__ == "__main__":
    main()
