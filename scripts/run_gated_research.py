#!/usr/bin/env python3
"""
Gated Intraday Breakout Research Engine for research/edge branch.
Applies production gate: Intraday NIFTY 50 regime at signal time == TREND_UP.
Uses cached data only (no new API pulls, no production changes).
Performs day-clustered bootstrap 95% confidence interval for expectancy.
Evaluates fidelity against Sep 23-30 entries.
"""
import os
import sys
import json
import logging
from datetime import datetime, time, timedelta
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
log = logging.getLogger("run_gated_research")

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

def compute_gated_vectorized_signals(
    df: pd.DataFrame,
    nifty_5m_gated: pd.DataFrame
) -> pd.DataFrame:
    df = df.sort_values("timestamp").reset_index(drop=True).copy()

    # Align Nifty timestamp & intraday regime
    nifty_merged = pd.merge_asof(
        df[["timestamp"]],
        nifty_5m_gated[["timestamp", "close", "intraday_nifty_regime"]].rename(columns={"close": "nifty_close"}).sort_values("timestamp"),
        on="timestamp",
        direction="backward"
    )
    df["nifty_close"] = nifty_merged["nifty_close"]
    df["intraday_nifty_regime"] = nifty_merged["intraday_nifty_regime"]

    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]
    nifty_close = df["nifty_close"]

    # 1. 15-bar consolidation high & low (bars t-15 to t-1)
    df["consol_high"] = high.shift(1).rolling(15).max()
    df["consol_low"] = low.shift(1).rolling(15).min()

    # 2. 20-bar average volume (bars t-21 to t-1)
    df["avg_vol_20"] = volume.shift(1).rolling(20).mean()

    # 3. Relative strength vs Nifty over 20 bars
    stock_perf = (close - close.shift(20)) / close.shift(20)
    nifty_perf = (nifty_close - nifty_close.shift(20)) / nifty_close.shift(20)
    df["rs_pos"] = stock_perf > nifty_perf

    # 4. ATR(14) and stop price
    df["atr_14"] = calculate_atr(df, 14)
    stop_atr = close - (2.0 * df["atr_14"])
    df["stop_price"] = np.maximum(stop_atr, df["consol_low"])
    df["stop_dist_pct"] = (close - df["stop_price"]) / close * 100.0

    # Production Intraday Gate: Intraday Nifty Regime at signal time MUST BE TREND_UP
    is_breakout_gated = (
        (df["intraday_nifty_regime"] == "TREND_UP") &
        (close > df["consol_high"]) &
        (df["avg_vol_20"] > 0) &
        (volume >= 1.5 * df["avg_vol_20"]) &
        df["rs_pos"] &
        (df["stop_price"] < close) &
        (df["stop_dist_pct"] >= 0.2) &
        (df["stop_dist_pct"] <= 25.0)
    )

    df["is_signal_gated"] = is_breakout_gated
    df["target_price"] = close + 2.0 * (close - df["stop_price"])

    return df

def simulate_trade_lifecycle(
    signal_row: pd.Series,
    session_5m_bars: pd.DataFrame
) -> Optional[Dict]:
    if session_5m_bars.empty:
        return None

    entry_bar = session_5m_bars.iloc[0]
    entry_timestamp = entry_bar['timestamp']
    entry_price_raw = float(entry_bar['open'])

    stop_price = float(signal_row['stop_price'])
    target_price = float(signal_row['target_price'])
    sig_entry = float(signal_row['close'])
    stop_risk_per_unit = sig_entry - stop_price

    if stop_risk_per_unit <= 0:
        return None

    FIXED_RISK_INR = 500.0
    qty = max(1, int(FIXED_RISK_INR / stop_risk_per_unit))
    total_stop_risk_inr = qty * stop_risk_per_unit

    exit_price = None
    exit_timestamp = None
    exit_reason = None

    for i in range(len(session_5m_bars)):
        bar = session_5m_bars.iloc[i]
        b_time = pd.to_datetime(bar['timestamp'])
        b_high = float(bar['high'])
        b_low = float(bar['low'])
        b_open = float(bar['open'])
        b_close = float(bar['close'])

        hit_stop = b_low <= stop_price
        hit_target = b_high >= target_price

        # Stop-first on ties
        if hit_stop and hit_target:
            exit_reason = "STOP_LOSS_TIE"
            exit_price = min(stop_price, b_open)
            exit_timestamp = bar['timestamp']
            break
        elif hit_stop:
            exit_reason = "STOP_LOSS"
            exit_price = min(stop_price, b_open)
            exit_timestamp = bar['timestamp']
            break
        elif hit_target:
            exit_reason = "TARGET"
            exit_price = max(target_price, b_open)
            exit_timestamp = bar['timestamp']
            break
        elif b_time.time() >= time(15, 15):
            exit_reason = "EOD_SQUAREOFF_1515"
            exit_price = b_close
            exit_timestamp = bar['timestamp']
            break

    if exit_price is None:
        last_bar = session_5m_bars.iloc[-1]
        exit_reason = "EOD_LAST_BAR"
        exit_price = float(last_bar['close'])
        exit_timestamp = last_bar['timestamp']

    gross_pnl, net_pnl, total_costs, total_slippage = calculate_indian_transaction_costs(
        buy_price=entry_price_raw,
        sell_price=exit_price,
        qty=qty,
        is_intraday=True,
        slippage_pct=0.05
    )

    gross_r = gross_pnl / total_stop_risk_inr if total_stop_risk_inr > 0 else 0.0
    net_r = net_pnl / total_stop_risk_inr if total_stop_risk_inr > 0 else 0.0
    cost_r = total_costs / total_stop_risk_inr if total_stop_risk_inr > 0 else 0.0
    cost_gate_passed = (total_costs <= 0.15 * total_stop_risk_inr)

    return {
        "symbol": signal_row['symbol'],
        "session_date": pd.to_datetime(signal_row['timestamp']).date(),
        "signal_timestamp": signal_row['timestamp'],
        "entry_timestamp": entry_timestamp,
        "exit_timestamp": exit_timestamp,
        "entry_price_raw": entry_price_raw,
        "exit_price": exit_price,
        "signal_entry_price": sig_entry,
        "stop_price": stop_price,
        "target_price": target_price,
        "qty": qty,
        "stop_risk_inr": total_stop_risk_inr,
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl,
        "total_costs": total_costs,
        "total_slippage": total_slippage,
        "gross_r": gross_r,
        "net_r": net_r,
        "cost_r": cost_r,
        "cost_gate_passed": cost_gate_passed,
        "exit_reason": exit_reason,
        "intraday_nifty_regime": signal_row['intraday_nifty_regime']
    }

def day_clustered_bootstrap_ci(trades: List[Dict], n_bootstraps: int = 10000, seed: int = 42) -> Dict:
    if not trades:
        return {
            "gross_expectancy_mean": 0.0,
            "gross_expectancy_ci_95": [0.0, 0.0],
            "net_expectancy_mean": 0.0,
            "net_expectancy_ci_95": [0.0, 0.0]
        }

    df = pd.DataFrame(trades)
    session_dates = df["session_date"].unique()
    n_days = len(session_dates)

    if n_days < 2:
        return {
            "gross_expectancy_mean": float(df["gross_r"].mean()),
            "gross_expectancy_ci_95": [float(df["gross_r"].mean()), float(df["gross_r"].mean())],
            "net_expectancy_mean": float(df["net_r"].mean()),
            "net_expectancy_ci_95": [float(df["net_r"].mean()), float(df["net_r"].mean())]
        }

    rng = np.random.default_rng(seed)

    gross_by_date = [group["gross_r"].to_numpy() for d, group in df.groupby("session_date")]
    net_by_date = [group["net_r"].to_numpy() for d, group in df.groupby("session_date")]
    n_groups = len(gross_by_date)

    gross_means = np.empty(n_bootstraps)
    net_means = np.empty(n_bootstraps)

    for i in range(n_bootstraps):
        idxs = rng.integers(0, n_groups, size=n_groups)
        g_concat = np.concatenate([gross_by_date[idx] for idx in idxs])
        n_concat = np.concatenate([net_by_date[idx] for idx in idxs])
        gross_means[i] = g_concat.mean()
        net_means[i] = n_concat.mean()

    gross_ci_low, gross_ci_high = np.percentile(gross_means, [2.5, 97.5])
    net_ci_low, net_ci_high = np.percentile(net_means, [2.5, 97.5])

    return {
        "gross_expectancy_mean": round(float(np.mean(gross_means)), 4),
        "gross_expectancy_ci_95": [round(float(gross_ci_low), 4), round(float(gross_ci_high), 4)],
        "net_expectancy_mean": round(float(np.mean(net_means)), 4),
        "net_expectancy_ci_95": [round(float(net_ci_low), 4), round(float(net_ci_high), 4)]
    }

def compute_gated_slice_metrics(trades: List[Dict]) -> Dict:
    if not trades:
        return {
            "signals": 0,
            "unique_days": 0,
            "avg_signals_per_day": 0.0,
            "distinct_symbols_per_day": 0.0,
            "max_signals_per_day": 0,
            "win_rate_pct": 0.0,
            "avg_win_r": 0.0,
            "avg_loss_r": 0.0,
            "gross_expectancy_r": 0.0,
            "net_expectancy_r": 0.0,
            "breakeven_win_rate_pct": 0.0,
            "bootstrap_ci": day_clustered_bootstrap_ci([])
        }

    df = pd.DataFrame(trades)
    n_signals = len(df)

    days_series = df["session_date"].value_counts()
    unique_days = len(days_series)
    avg_signals_per_day = float(n_signals / unique_days) if unique_days > 0 else 0.0
    max_signals_per_day = int(days_series.max()) if unique_days > 0 else 0

    symbols_per_day = df.groupby("session_date")["symbol"].nunique()
    avg_distinct_symbols_per_day = float(symbols_per_day.mean()) if unique_days > 0 else 0.0

    wins = df[df["net_pnl"] > 0]
    losses = df[df["net_pnl"] <= 0]

    win_rate_pct = float(len(wins) / n_signals * 100.0)
    avg_win_r = float(wins["net_r"].mean()) if len(wins) > 0 else 0.0
    avg_loss_r = float(losses["net_r"].mean()) if len(losses) > 0 else 0.0

    gross_expectancy_r = float(df["gross_r"].mean())
    net_expectancy_r = float(df["net_r"].mean())

    abs_avg_loss_r = abs(avg_loss_r)
    if avg_win_r + abs_avg_loss_r > 0:
        breakeven_win_rate_pct = float(abs_avg_loss_r / (avg_win_r + abs_avg_loss_r) * 100.0)
    else:
        breakeven_win_rate_pct = 0.0

    bootstrap_ci = day_clustered_bootstrap_ci(trades, n_bootstraps=10000)

    return {
        "signals": n_signals,
        "unique_days": unique_days,
        "avg_signals_per_day": round(avg_signals_per_day, 2),
        "distinct_symbols_per_day": round(avg_distinct_symbols_per_day, 2),
        "max_signals_per_day": max_signals_per_day,
        "win_rate_pct": round(win_rate_pct, 2),
        "avg_win_r": round(avg_win_r, 4),
        "avg_loss_r": round(avg_loss_r, 4),
        "gross_expectancy_r": round(gross_expectancy_r, 4),
        "net_expectancy_r": round(net_expectancy_r, 4),
        "breakeven_win_rate_pct": round(breakeven_win_rate_pct, 2),
        "bootstrap_ci": bootstrap_ci
    }

def main():
    log.info("Starting Fast Gated Intraday Breakout Research Engine...")
    daily_dict, intraday_dict, meta = load_cached_datasets()

    nifty_5m = intraday_dict["NIFTY 50"].copy()
    nifty_5m["session_date"] = pd.to_datetime(nifty_5m["timestamp"]).dt.date
    session_dates = sorted(nifty_5m["session_date"].unique())

    nifty_5m_gated = compute_intraday_nifty_regime(nifty_5m)

    trend_up_bars = (nifty_5m_gated["intraday_nifty_regime"] == "TREND_UP").sum()
    total_bars = len(nifty_5m_gated)
    log.info(f"Intraday Nifty 5m regime evaluated: TREND_UP active in {trend_up_bars}/{total_bars} bars ({trend_up_bars/total_bars*100:.2f}%).")

    universe_symbols = [s for s in intraday_dict.keys() if s != "NIFTY 50"]

    gated_trades = []

    for symbol in universe_symbols:
        s_df = intraday_dict[symbol].copy()
        s_df["symbol"] = symbol
        s_df["session_date"] = pd.to_datetime(s_df["timestamp"]).dt.date

        sig_df = compute_gated_vectorized_signals(s_df, nifty_5m_gated)
        signals_only = sig_df[sig_df["is_signal_gated"]].copy()

        if signals_only.empty:
            continue

        signals_first_per_day = signals_only.groupby("session_date").first().reset_index()

        for _, sig_row in signals_first_per_day.iterrows():
            sig_ts = sig_row["timestamp"]
            sig_date = sig_row["session_date"]

            day_bars = s_df[(s_df["session_date"] == sig_date) & (s_df["timestamp"] > sig_ts)].reset_index(drop=True)
            if not day_bars.empty:
                trade = simulate_trade_lifecycle(sig_row, day_bars)
                if trade:
                    gated_trades.append(trade)

    log.info(f"Gated Intraday Breakout Replay completed: {len(gated_trades)} total signals generated.")

    # Chronological Split: 70% In-Sample, 30% Holdout
    split_idx = int(len(session_dates) * 0.70)
    in_sample_dates = set(session_dates[:split_idx])
    holdout_dates = set(session_dates[split_idx:])

    trades_is = [t for t in gated_trades if t["session_date"] in in_sample_dates]
    trades_oos = [t for t in gated_trades if t["session_date"] in holdout_dates]

    metrics_is = compute_gated_slice_metrics(trades_is)
    metrics_oos = compute_gated_slice_metrics(trades_oos)
    metrics_full = compute_gated_slice_metrics(gated_trades)

    # Sep 21-30 period trades for fidelity inspection
    sep_dates = [d for d in session_dates if d >= datetime(2026, 9, 21).date()]
    sep_gated_trades = [
        {
            "symbol": t["symbol"],
            "session_date": str(t["session_date"]),
            "signal_timestamp": str(t["signal_timestamp"]),
            "entry_timestamp": str(t["entry_timestamp"]),
            "entry_price_raw": t["entry_price_raw"],
            "stop_price": t["stop_price"],
            "target_price": t["target_price"]
        }
        for t in gated_trades if t["session_date"] in set(sep_dates)
    ]

    final_gated_report = {
        "metadata": meta,
        "date_ranges": {
            "full_period": [str(session_dates[0]), str(session_dates[-1])],
            "in_sample_period": [str(session_dates[0]), str(session_dates[split_idx-1])],
            "holdout_period": [str(session_dates[split_idx]), str(session_dates[-1])],
            "total_sessions": len(session_dates),
            "in_sample_sessions": len(in_sample_dates),
            "holdout_sessions": len(holdout_dates),
        },
        "in_sample_metrics": metrics_is,
        "holdout_metrics": metrics_oos,
        "full_period_metrics": metrics_full,
        "sep_21_30_gated_trades": sep_gated_trades
    }

    report_file = CACHE_DIR / "gated_research_report.json"
    report_file.write_text(json.dumps(final_gated_report, indent=2))
    log.info(f"Saved gated research report to {report_file}")

if __name__ == "__main__":
    main()
