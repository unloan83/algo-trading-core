#!/usr/bin/env python3
"""
Fast Vectorized Point-In-Time Breakout Edge Replay Script for research/edge branch.
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
from core.regime_filter import evaluate_regime, calculate_atr
from backtest.cost_model import calculate_indian_transaction_costs

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("replay_breakout_edge")

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

def compute_daily_regime_map(nifty_daily_df: pd.DataFrame) -> Dict[datetime.date, Tuple[RegimeType, str]]:
    nifty_df = nifty_daily_df.sort_values("timestamp").reset_index(drop=True)
    nifty_df["session_date"] = pd.to_datetime(nifty_df["timestamp"]).dt.date

    unique_dates = sorted(nifty_df["session_date"].unique())
    regime_map = {}

    for i in range(1, len(unique_dates)):
        curr_date = unique_dates[i]
        prior_nifty = nifty_df.iloc[:i]
        if len(prior_nifty) >= 50:
            regime, rationale = evaluate_regime(prior_nifty)
        else:
            regime, rationale = RegimeType.HIGH_RISK, "Insufficient history"
        regime_map[curr_date] = (regime, rationale)

    return regime_map

def compute_vectorized_signals(
    df: pd.DataFrame,
    nifty_df: pd.DataFrame
) -> pd.DataFrame:
    """
    Computes point-in-time breakout signals for a DataFrame using rolling windows.
    Strictly uses prior completed bars for indicators.
    """
    df = df.sort_values("timestamp").reset_index(drop=True).copy()
    
    # Align Nifty timestamp
    nifty_merged = pd.merge_asof(
        df[["timestamp"]],
        nifty_df[["timestamp", "close"]].rename(columns={"close": "nifty_close"}).sort_values("timestamp"),
        on="timestamp",
        direction="backward"
    )
    df["nifty_close"] = nifty_merged["nifty_close"]

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

    # Signal mask
    is_breakout = (
        (close > df["consol_high"]) &
        (df["avg_vol_20"] > 0) &
        (volume >= 1.5 * df["avg_vol_20"]) &
        df["rs_pos"] &
        (df["stop_price"] < close) &
        (df["stop_dist_pct"] >= 0.2) &
        (df["stop_dist_pct"] <= 25.0)
    )

    df["is_signal"] = is_breakout
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
        "prior_regime": signal_row['prior_regime']
    }

def compute_group_metrics(trades: List[Dict]) -> Dict:
    if not trades:
        return {
            "signals": 0,
            "unique_days": 0,
            "avg_signals_per_day": 0.0,
            "max_signals_per_day": 0,
            "win_rate_pct": 0.0,
            "avg_win_r": 0.0,
            "avg_loss_r": 0.0,
            "gross_expectancy_r": 0.0,
            "net_expectancy_r": 0.0,
            "breakeven_win_rate_pct": 0.0
        }

    df = pd.DataFrame(trades)
    n_signals = len(df)

    days_series = df["session_date"].value_counts()
    unique_days = len(days_series)
    avg_signals_per_day = float(n_signals / unique_days) if unique_days > 0 else 0.0
    max_signals_per_day = int(days_series.max()) if unique_days > 0 else 0

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

    return {
        "signals": n_signals,
        "unique_days": unique_days,
        "avg_signals_per_day": round(avg_signals_per_day, 2),
        "max_signals_per_day": max_signals_per_day,
        "win_rate_pct": round(win_rate_pct, 2),
        "avg_win_r": round(avg_win_r, 4),
        "avg_loss_r": round(avg_loss_r, 4),
        "gross_expectancy_r": round(gross_expectancy_r, 4),
        "net_expectancy_r": round(net_expectancy_r, 4),
        "breakeven_win_rate_pct": round(breakeven_win_rate_pct, 2)
    }

def main():
    log.info("Starting Fast Vectorized Point-In-Time Replay Engine...")
    daily_dict, intraday_dict, meta = load_cached_datasets()

    nifty_daily = daily_dict["NIFTY 50"]
    nifty_5m = intraday_dict["NIFTY 50"]

    regime_map = compute_daily_regime_map(nifty_daily)
    universe_symbols = [s for s in intraday_dict.keys() if s != "NIFTY 50"]

    nifty_5m["session_date"] = pd.to_datetime(nifty_5m["timestamp"]).dt.date
    session_dates = sorted(nifty_5m["session_date"].unique())

    log.info(f"Loaded dataset across {len(session_dates)} session dates ({session_dates[0]} to {session_dates[-1]}).")

    # 1. Intraday 5m Breakout Replay
    intraday_trades = []

    for symbol in universe_symbols:
        s_df = intraday_dict[symbol].copy()
        s_df["symbol"] = symbol
        s_df["session_date"] = pd.to_datetime(s_df["timestamp"]).dt.date

        # Compute vectorized signal mask
        sig_df = compute_vectorized_signals(s_df, nifty_5m)
        sig_df["prior_regime"] = sig_df["session_date"].map(lambda d: regime_map.get(d, (RegimeType.HIGH_RISK, ""))[0])

        # Filter only signal rows
        signals_only = sig_df[sig_df["is_signal"]].copy()
        if signals_only.empty:
            continue

        # Enforce FIRST signal per symbol per day
        signals_first_per_day = signals_only.groupby("session_date").first().reset_index()

        # Simulate trades
        for _, sig_row in signals_first_per_day.iterrows():
            sig_ts = sig_row["timestamp"]
            sig_date = sig_row["session_date"]

            # Remaining 5m bars of session starting strictly at next bar
            day_bars = s_df[(s_df["session_date"] == sig_date) & (s_df["timestamp"] > sig_ts)].reset_index(drop=True)
            if not day_bars.empty:
                trade = simulate_trade_lifecycle(sig_row, day_bars)
                if trade:
                    intraday_trades.append(trade)

    log.info(f"Intraday 5m Breakout Replay completed: {len(intraday_trades)} signals.")

    # 2. Daily EOD Breakout Replay
    daily_trades = []
    nifty_daily["session_date"] = pd.to_datetime(nifty_daily["timestamp"]).dt.date

    for symbol in universe_symbols:
        d_df = daily_dict[symbol].copy()
        d_df["symbol"] = symbol
        d_df["session_date"] = pd.to_datetime(d_df["timestamp"]).dt.date

        sig_d_df = compute_vectorized_signals(d_df, nifty_daily)
        sig_d_df["prior_regime"] = sig_d_df["session_date"].map(lambda d: regime_map.get(d, (RegimeType.HIGH_RISK, ""))[0])

        signals_only = sig_d_df[sig_d_df["is_signal"]].copy()
        if signals_only.empty:
            continue

        for _, sig_row in signals_only.iterrows():
            sig_date = sig_row["session_date"]
            # Find next session date
            date_idx = session_dates.index(sig_date) if sig_date in session_dates else -1
            if date_idx >= 0 and date_idx + 1 < len(session_dates):
                next_date = session_dates[date_idx + 1]
                s_5m = intraday_dict[symbol]
                s_5m["session_date"] = pd.to_datetime(s_5m["timestamp"]).dt.date
                day_5m = s_5m[s_5m["session_date"] == next_date].reset_index(drop=True)
                if not day_5m.empty:
                    trade = simulate_trade_lifecycle(sig_row, day_5m)
                    if trade:
                        daily_trades.append(trade)

    log.info(f"Daily EOD Breakout Replay completed: {len(daily_trades)} signals.")

    # Split dates: last 30% holdout
    split_idx = int(len(session_dates) * 0.70)
    in_sample_dates = set(session_dates[:split_idx])
    holdout_dates = set(session_dates[split_idx:])

    def build_report_block(trade_list: List[Dict], mode_name: str) -> Dict:
        t_is = [t for t in trade_list if t["session_date"] in in_sample_dates]
        t_oos = [t for t in trade_list if t["session_date"] in holdout_dates]

        def format_partition(t_sub: List[Dict]) -> Dict:
            t_td = [t for t in t_sub if t["prior_regime"] == RegimeType.TREND_DOWN]
            t_not_td = [t for t in t_sub if t["prior_regime"] != RegimeType.TREND_DOWN]
            t_cg = [t for t in t_sub if t["cost_gate_passed"]]
            t_aligned_cg = [t for t in t_not_td if t["cost_gate_passed"]]

            return {
                "all_signals": compute_group_metrics(t_sub),
                "regime_trend_down": compute_group_metrics(t_td),
                "regime_not_trend_down": compute_group_metrics(t_not_td),
                "cost_gate_all": compute_group_metrics(t_cg),
                "cost_gate_aligned": compute_group_metrics(t_aligned_cg),
            }

        return {
            "mode": mode_name,
            "in_sample": format_partition(t_is),
            "holdout": format_partition(t_oos),
            "full_dataset": format_partition(trade_list)
        }

    final_report = {
        "metadata": meta,
        "date_ranges": {
            "full_period": [str(session_dates[0]), str(session_dates[-1])],
            "in_sample_period": [str(session_dates[0]), str(session_dates[split_idx-1])],
            "holdout_period": [str(session_dates[split_idx]), str(session_dates[-1])],
            "total_sessions": len(session_dates),
            "in_sample_sessions": len(in_sample_dates),
            "holdout_sessions": len(holdout_dates),
        },
        "survivorship_bias_label": meta["survivorship_bias_label"],
        "intraday_5m_breakout": build_report_block(intraday_trades, "Intraday 5m Breakout"),
        "daily_eod_breakout": build_report_block(daily_trades, "Daily EOD Breakout")
    }

    report_file = CACHE_DIR / "replay_research_report.json"
    report_file.write_text(json.dumps(final_report, indent=2))
    log.info(f"Saved replay report to {report_file}")

if __name__ == "__main__":
    main()
