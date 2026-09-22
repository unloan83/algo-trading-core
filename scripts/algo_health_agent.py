#!/usr/bin/env python3

import argparse
import logging
import os
import shutil
import sys
from dataclasses import dataclass, field
from datetime import time, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env", override=False)

from core.circuit_tracker import CircuitTracker
from core.entry_models.breakout import evaluate_breakout
from core.entry_models.trend_pullback import evaluate_trend_pullback
from core.paper_engine import paper_starting_capital, paper_gate_status
from core.screener import Screener
from data.broker_client import UnifiedBrokerClient
from data.db_models import DatabaseManager
from data.universe_selector import (
    load_active_universe,
    load_active_universe_metadata,
)
from journal.analytics import AnalyticsEngine
from scripts.runtime_common import (
    build_risk_governor,
    load_market_filters,
    now_ist_naive,
    project_config,
    read_runtime_marker,
    write_runtime_marker,
)
from telegram_bot.approval_gate import ApprovalGate
from telegram_bot.telegram_client import TelegramClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("algo_health_agent")


@dataclass
class HealthCheckResult:
    domain: str
    status: str
    message: str
    details: Dict = field(default_factory=dict)


class AlgoHealthAgent:

    def __init__(
        self,
        db: Optional[DatabaseManager] = None,
        broker: Optional[UnifiedBrokerClient] = None,
        starting_capital: Optional[float] = None,
    ):
        self.db = db or DatabaseManager()
        self.broker = broker or UnifiedBrokerClient(paper_mode=True)
        if starting_capital is not None:
            self.starting_capital = starting_capital
        else:
            try:
                self.starting_capital = paper_starting_capital()
            except Exception:
                self.starting_capital = 30000.0
        self.telegram = TelegramClient()
        self.governor = build_risk_governor()

    def _active_symbols(self) -> List[str]:
        cfg = project_config("universe.yaml")["universe"]
        return load_active_universe(cfg)

    # ---------------------------------------------------------
    # 1. MODEL HEALTH
    # ---------------------------------------------------------
    def check_model_health(self) -> HealthCheckResult:
        try:
            symbols = self._active_symbols()

            Screener(symbols)

            return HealthCheckResult(
                "MODEL_HEALTH",
                "OK",
                "Breakout, trend-pullback and screener modules loaded",
                {
                    "breakout": evaluate_breakout.__name__,
                    "trend_pullback": evaluate_trend_pullback.__name__,
                    "active_symbols": len(symbols),
                },
            )

        except Exception as exc:
            return HealthCheckResult(
                "MODEL_HEALTH",
                "BLOCKER",
                f"Model initialization failed: {type(exc).__name__}: {exc}",
            )

    # ---------------------------------------------------------
    # 2. RISK / CIRCUIT HEALTH
    # Uses same limits as real RiskGovernor
    # ---------------------------------------------------------
    def check_system_blockers(self) -> HealthCheckResult:
        try:
            now = now_ist_naive()

            tracker = CircuitTracker(self.db)
            tracker.check_and_update_rollover_state(now)

            positions = self.db.get_open_positions()

            capital = self.starting_capital
            _, equity = self.db.paper_account(capital, positions)

            if equity <= 0:
                return HealthCheckResult(
                    "SYSTEM_BLOCKERS",
                    "BLOCKER",
                    "Paper account equity is zero or negative",
                )

            daily, weekly, monthly = tracker.compute_mark_to_market_pnl(
                positions,
                as_of_date=now.date(),
            )

            state = self.db.get_latest_circuit_state()
            consecutive_losses = int(state.get("consecutive_losses", 0))
            last_loss_time = state.get("last_loss_time")

            cfg = project_config("risk_limits.yaml")
            cooldown_cfg = cfg["consecutive_loss_cooldown"]

            blockers = []

            daily_loss_pct = abs(min(daily, 0.0)) / equity * 100.0
            weekly_loss_pct = abs(min(weekly, 0.0)) / equity * 100.0
            monthly_loss_pct = abs(min(monthly, 0.0)) / equity * 100.0

            if daily_loss_pct >= self.governor.daily_circuit_pct:
                blockers.append(
                    f"Daily circuit breached: {daily_loss_pct:.2f}%"
                )

            if weekly_loss_pct >= self.governor.weekly_circuit_pct:
                blockers.append(
                    f"Weekly circuit breached: {weekly_loss_pct:.2f}%"
                )

            if monthly_loss_pct >= self.governor.monthly_circuit_pct:
                blockers.append(
                    f"Monthly circuit breached: {monthly_loss_pct:.2f}%"
                )

            trigger = int(cooldown_cfg["trigger_after_losses"])
            cooldown_hours = int(cooldown_cfg["cooldown_hours"])

            if consecutive_losses >= trigger and last_loss_time:
                cooldown_end = last_loss_time + timedelta(hours=cooldown_hours)
                if now < cooldown_end:
                    blockers.append(
                        f"Consecutive-loss cooldown active: "
                        f"{consecutive_losses} losses"
                    )

            if blockers:
                return HealthCheckResult(
                    "SYSTEM_BLOCKERS",
                    "BLOCKER",
                    "; ".join(blockers),
                    {
                        "daily_pnl": daily,
                        "weekly_pnl": weekly,
                        "monthly_pnl": monthly,
                        "equity": equity,
                        "daily_circuit_used_pct": daily_loss_pct,
                        "weekly_circuit_used_pct": weekly_loss_pct,
                        "monthly_circuit_used_pct": monthly_loss_pct,
                    },
                )

            return HealthCheckResult(
                "SYSTEM_BLOCKERS",
                "OK",
                "Risk governor circuits clear",
                {
                    "daily_pnl": daily,
                    "weekly_pnl": weekly,
                    "monthly_pnl": monthly,
                    "equity": equity,
                    "open_positions": len(positions),
                    "daily_circuit_used_pct": daily_loss_pct,
                    "weekly_circuit_used_pct": weekly_loss_pct,
                    "monthly_circuit_used_pct": monthly_loss_pct,
                },
            )

        except Exception as exc:
            return HealthCheckResult(
                "SYSTEM_BLOCKERS",
                "BLOCKER",
                f"Risk-state check failed: {type(exc).__name__}: {exc}",
            )

    # ---------------------------------------------------------
    # 3. DATA + FAKE DATA + INSTRUMENT ID HEALTH
    # ---------------------------------------------------------
    def check_fake_data_and_quality(self) -> HealthCheckResult:
        try:
            now = now_ist_naive()

            df = self.broker.get_intraday_history(
                "NIFTY 50",
                interval_minutes=5,
                lookback_days=3,
            )

            if df.empty:
                return HealthCheckResult(
                    "DATA_INTEGRITY",
                    "BLOCKER",
                    "Upstox returned no NIFTY 50 candles",
                )

            required = {
                "timestamp",
                "open",
                "high",
                "low",
                "close",
                "volume",
            }

            if not required.issubset(df.columns):
                return HealthCheckResult(
                    "DATA_INTEGRITY",
                    "BLOCKER",
                    f"Missing candle fields: {required - set(df.columns)}",
                )

            invalid = df[
                (df["open"] <= 0)
                | (df["high"] <= 0)
                | (df["low"] <= 0)
                | (df["close"] <= 0)
            ]

            if not invalid.empty:
                return HealthCheckResult(
                    "DATA_INTEGRITY",
                    "BLOCKER",
                    f"{len(invalid)} candles contain non-positive prices",
                )

            duplicates = int(df["timestamp"].duplicated().sum())

            if duplicates:
                return HealthCheckResult(
                    "DATA_INTEGRITY",
                    "WARNING",
                    f"{duplicates} duplicate candle timestamps detected",
                )

            latest_ts = df["timestamp"].max()

            # During active market hours the feed must remain fresh.
            if (
                now.weekday() < 5
                and time(9, 25) <= now.time() <= time(15, 30)
            ):
                age_minutes = (now - latest_ts).total_seconds() / 60.0

                if age_minutes > 15:
                    return HealthCheckResult(
                        "DATA_INTEGRITY",
                        "BLOCKER",
                        f"Market feed stale by {age_minutes:.1f} minutes",
                    )

            # Validate every configured instrument ID.
            symbols = self._active_symbols()

            bad_symbols = []
            instrument_keys = set()

            for symbol in symbols:
                try:
                    key = self.broker.resolve_instrument_key(symbol)

                    if not key:
                        bad_symbols.append(symbol)
                        continue

                    if key in instrument_keys:
                        return HealthCheckResult(
                            "DATA_INTEGRITY",
                            "BLOCKER",
                            f"Duplicate instrument key detected: {key}",
                        )

                    instrument_keys.add(key)

                except Exception:
                    bad_symbols.append(symbol)

            if bad_symbols:
                return HealthCheckResult(
                    "DATA_INTEGRITY",
                    "BLOCKER",
                    "Invalid/unresolved instrument IDs: "
                    + ", ".join(bad_symbols),
                )

            return HealthCheckResult(
                "DATA_INTEGRITY",
                "OK",
                "Upstox data and dynamic Top-100 universe verified",
                {
                    "latest_candle": str(latest_ts),
                    "candles": len(df),
                    "instrument_ids_checked": len(symbols),
                },
            )

        except Exception as exc:
            return HealthCheckResult(
                "DATA_INTEGRITY",
                "BLOCKER",
                f"Data integrity check failed: {type(exc).__name__}: {exc}",
            )

    # ---------------------------------------------------------
    # 4. UPSTOX TOKEN / API
    # ---------------------------------------------------------
    def check_token_blockages(self) -> HealthCheckResult:
        try:
            if not os.getenv("UPSTOX_ANALYTICS_TOKEN"):
                return HealthCheckResult(
                    "TOKEN_VALIDITY",
                    "BLOCKER",
                    "UPSTOX_ANALYTICS_TOKEN missing",
                )

            ok, msg = self.broker.validate_readonly_access()

            if not ok:
                return HealthCheckResult(
                    "TOKEN_VALIDITY",
                    "BLOCKER",
                    msg,
                )

            return HealthCheckResult(
                "TOKEN_VALIDITY",
                "OK",
                "Upstox read-only analytics access verified",
            )

        except Exception as exc:
            return HealthCheckResult(
                "TOKEN_VALIDITY",
                "BLOCKER",
                f"Upstox validation failed: {type(exc).__name__}: {exc}",
            )

    # ---------------------------------------------------------
    # 5. DATABASE
    # ---------------------------------------------------------
    def check_logic_and_database(self) -> HealthCheckResult:
        try:
            with self.db.get_connection() as conn:
                row = conn.execute("PRAGMA integrity_check").fetchone()
                integrity = str(row[0]).lower()

            if integrity != "ok":
                return HealthCheckResult(
                    "LOGIC_DB",
                    "BLOCKER",
                    f"SQLite integrity_check failed: {integrity}",
                )

            pending = self.db.get_pending_signals()
            open_positions = self.db.get_open_positions()

            return HealthCheckResult(
                "LOGIC_DB",
                "OK",
                "SQLite database integrity verified",
                {
                    "pending_signals": len(pending),
                    "open_positions": len(open_positions),
                },
            )

        except Exception as exc:
            return HealthCheckResult(
                "LOGIC_DB",
                "BLOCKER",
                f"Database check failed: {type(exc).__name__}: {exc}",
            )

    # ---------------------------------------------------------
    # 6. TELEGRAM / APPROVAL GATE
    # ---------------------------------------------------------
    def check_action_decision_gate(self) -> HealthCheckResult:
        try:
            risk_cfg = project_config("risk_limits.yaml")
            timing_cfg = project_config("timing.yaml")["timing"]

            approval = ApprovalGate(
                timeout_seconds=int(
                    timing_cfg["telegram_approval_timeout_seconds"]
                ),
                default_action_on_timeout=risk_cfg[
                    "default_action_on_timeout"
                ],
            )

            if not self.telegram.is_configured():
                return HealthCheckResult(
                    "ACTION_DECISION_GATE",
                    "BLOCKER",
                    "Telegram credentials/allowlist not configured",
                )

            return HealthCheckResult(
                "ACTION_DECISION_GATE",
                "OK",
                "Telegram approval gate configured",
                {
                    "timeout_seconds": approval.timeout_seconds,
                    "default_action": approval.default_action_on_timeout,
                },
            )

        except Exception as exc:
            return HealthCheckResult(
                "ACTION_DECISION_GATE",
                "BLOCKER",
                f"ApprovalGate check failed: {type(exc).__name__}: {exc}",
            )

    # ---------------------------------------------------------
    # 7. ACTUAL RUNTIME ACTIVITY
    # Proves that preflight / intraday / EOD actually ran.
    # ---------------------------------------------------------
    @staticmethod
    def _marker_age_minutes(marker, now):
        if not marker or not marker.get("timestamp"):
            return None
        return (now - marker["timestamp"]).total_seconds() / 60.0

    def _require_success_today(
        self,
        service,
        label,
        now,
        blockers,
        max_age_minutes=None,
    ):
        marker = read_runtime_marker(service)
        if not marker:
            blockers.append(f"{label} execution marker missing")
            return None

        if marker["timestamp"].date() != now.date():
            blockers.append(f"{label} has no execution evidence today")
            return marker

        if marker["status"] != "SUCCESS":
            suffix = f": {marker['message']}" if marker.get("message") else ""
            blockers.append(f"{label} last status={marker['status']}{suffix}")
            return marker

        if max_age_minutes is not None:
            age = self._marker_age_minutes(marker, now)
            if age is None or age > max_age_minutes:
                blockers.append(
                    f"{label} heartbeat stale: {age:.1f} minutes"
                    if age is not None
                    else f"{label} heartbeat timestamp missing"
                )
        return marker

    def check_runtime_activity(self) -> HealthCheckResult:
        try:
            now = now_ist_naive()
            trading_day = self.broker.is_nse_trading_day(now.date())
            blockers = []
            details = {}

            if now.time() >= time(8, 50):
                marker = self._require_success_today(
                    "preflight", "Morning preflight", now, blockers
                )
                details["preflight"] = marker
                if marker and marker["status"] == "SUCCESS":
                    try:
                        load_market_filters()
                        self._active_symbols()
                    except Exception as exc:
                        blockers.append(f"Preflight artifacts invalid: {exc}")

            if trading_day and now.time() >= time(9, 45):
                max_age = 20.0 if now.time() <= time(14, 20) else None
                marker = self._require_success_today(
                    "intraday_scan",
                    "Intraday scanner",
                    now,
                    blockers,
                    max_age_minutes=max_age,
                )
                details["intraday_scan"] = marker

            if trading_day and now.time() >= time(9, 20):
                max_age = 5.0 if now.time() <= time(15, 30) else None
                marker = self._require_success_today(
                    "paper_monitor",
                    "Paper exit monitor",
                    now,
                    blockers,
                    max_age_minutes=max_age,
                )
                details["paper_monitor"] = marker

            if trading_day and now.time() >= time(16, 5):
                marker = self._require_success_today(
                    "eod_screener", "EOD screener", now, blockers
                )
                details["eod_screener"] = marker

            if trading_day and now.time() >= time(15, 20):
                intraday_open = [
                    p.symbol
                    for p in self.db.get_open_positions()
                    if p.is_intraday
                ]
                if intraday_open:
                    blockers.append(
                        "Intraday positions remain open after mandatory "
                        "square-off: " + ", ".join(intraday_open)
                    )

            if blockers:
                return HealthCheckResult(
                    "RUNTIME_ACTIVITY",
                    "BLOCKER",
                    "; ".join(blockers),
                    details,
                )

            return HealthCheckResult(
                "RUNTIME_ACTIVITY",
                "OK",
                "Scheduled trading runtime heartbeats verified",
                details,
            )

        except Exception as exc:
            return HealthCheckResult(
                "RUNTIME_ACTIVITY",
                "BLOCKER",
                f"Runtime activity check failed: {type(exc).__name__}: {exc}",
            )

    # ---------------------------------------------------------
    # 8. OCI RESOURCE HEALTH
    # ---------------------------------------------------------
    def check_oci_resources(self) -> HealthCheckResult:
        try:
            total, used, free = shutil.disk_usage("/")
            disk_pct = used / total * 100.0

            mem_total = 0
            mem_available = 0

            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        mem_total = int(line.split()[1])
                    elif line.startswith("MemAvailable:"):
                        mem_available = int(line.split()[1])

            memory_pct = (
                (mem_total - mem_available) / mem_total * 100.0
                if mem_total
                else 0.0
            )

            load_1m = os.getloadavg()[0]
            cpu_count = os.cpu_count() or 1

            blockers = []
            warnings = []

            if disk_pct >= 95:
                blockers.append(f"Disk usage critical: {disk_pct:.1f}%")
            elif disk_pct >= 85:
                warnings.append(f"Disk usage high: {disk_pct:.1f}%")

            if memory_pct >= 95:
                blockers.append(
                    f"Memory usage critical: {memory_pct:.1f}%"
                )
            elif memory_pct >= 85:
                warnings.append(
                    f"Memory usage high: {memory_pct:.1f}%"
                )

            if load_1m > cpu_count * 1.5:
                warnings.append(
                    f"CPU load high: {load_1m:.2f} "
                    f"on {cpu_count} CPU(s)"
                )

            if blockers:
                return HealthCheckResult(
                    "OCI_RESOURCE_HEALTH",
                    "BLOCKER",
                    "; ".join(blockers),
                )

            if warnings:
                return HealthCheckResult(
                    "OCI_RESOURCE_HEALTH",
                    "WARNING",
                    "; ".join(warnings),
                )

            return HealthCheckResult(
                "OCI_RESOURCE_HEALTH",
                "OK",
                "OCI CPU, memory and disk resources healthy",
                {
                    "disk_used_pct": round(disk_pct, 1),
                    "memory_used_pct": round(memory_pct, 1),
                    "load_1m": round(load_1m, 2),
                },
            )

        except Exception as exc:
            return HealthCheckResult(
                "OCI_RESOURCE_HEALTH",
                "WARNING",
                f"OCI resource inspection failed: "
                f"{type(exc).__name__}: {exc}",
            )

    # ---------------------------------------------------------
    # ALL CHECKS
    # ---------------------------------------------------------
    def run_all_checks(self) -> List[HealthCheckResult]:
        return [
            self.check_model_health(),
            self.check_system_blockers(),
            self.check_fake_data_and_quality(),
            self.check_token_blockages(),
            self.check_logic_and_database(),
            self.check_action_decision_gate(),
            self.check_runtime_activity(),
            self.check_oci_resources(),
        ]

    def today_summary(self, date_iso: Optional[str] = None) -> Dict:
        today = date_iso or now_ist_naive().date().isoformat()
        return self.db.get_today_summary(today)

    # ---------------------------------------------------------
    # TELEGRAM REPORTS
    # ---------------------------------------------------------
    def send_morning_report(self) -> bool:
        results = self.run_all_checks()

        blockers = [r for r in results if r.status == "BLOCKER"]
        warnings = [r for r in results if r.status == "WARNING"]

        if blockers:
            overall = "🔴 BLOCKED"
        elif warnings:
            overall = "🟡 WARNING"
        else:
            overall = "🟢 READY"

        capital = self.starting_capital
        gate = paper_gate_status(self.db)
        gate_str = "CLEARED 🟢" if gate["gate_cleared"] else f"HOLD ({gate['days_elapsed']}/{gate['days_required']} days, {gate['trades_completed']}/{gate['trades_required']} trades)"
        universe_meta = load_active_universe_metadata()
        universe_count = len(universe_meta.get("selected", []))

        lines = [
            "🌅 <b>ALGO SYSTEM — MORNING HEALTH</b>",
            "",
            f"• <b>Time:</b> "
            f"{now_ist_naive().strftime('%Y-%m-%d %H:%M:%S IST')}",
            f"• <b>Status:</b> {overall}",
            f"• <b>Mode:</b> PAPER",
            f"• <b>Starting Capital:</b> ₹{capital:,.2f}",
            f"• <b>Dynamic Universe:</b> {universe_count} / NIFTY 200",
            f"• <b>Paper Gate:</b> {gate_str}",
            "",
        ]

        for result in results:
            icon = {
                "OK": "🟢",
                "WARNING": "🟡",
                "BLOCKER": "🔴",
            }[result.status]

            lines.append(
                f"{icon} <b>{result.domain}</b>: {result.message}"
            )

        text = "\n".join(lines)
        print(text)

        if not self.telegram.is_configured():
            log.error("Telegram not configured")
            return False

        ok, _, error = self.telegram.send_message(text)

        if not ok:
            log.error("Morning Telegram failed: %s", error)

        return ok

    def send_eod_report(self) -> bool:
        results = self.run_all_checks()
        summary = self.today_summary()

        blockers = [r for r in results if r.status == "BLOCKER"]
        warnings = [r for r in results if r.status == "WARNING"]

        if blockers:
            overall = "🔴 ISSUE DETECTED"
        elif warnings:
            overall = "🟡 WARNING"
        else:
            overall = "🟢 HEALTHY"

        analytics = AnalyticsEngine(self.db)
        cumulative = analytics.compute_performance_metrics()

        configured_symbols = len(self._active_symbols())

        lines = [
            "🌆 <b>ALGO SYSTEM — END OF DAY REPORT</b>",
            "",
            f"• <b>Time:</b> "
            f"{now_ist_naive().strftime('%Y-%m-%d %H:%M:%S IST')}",
            f"• <b>System Health:</b> {overall}",
            f"• <b>Dynamic Universe:</b> {configured_symbols} / NIFTY 200",
            "",
            "<b>Today's Runtime</b>",
            f"• Signals evaluated: {summary['signals_evaluated_today']}",
            f"• Orders opened: {summary['orders_today']}",
            f"• Trades closed: {summary['trades_closed_today']}",
            f"• Net realized P&L: "
            f"₹{summary['net_pnl_today']:,.2f}",
            f"• Blocked signals: "
            f"{summary['blocked_signals_today']}",
            f"• Regime/scan events: "
            f"{summary['regime_events_today']}",
            f"• Open positions: {summary['open_positions']}",
            f"• Pending signals: {summary['pending_signals']}",
            "",
            "<b>Cumulative Paper Performance</b>",
            f"• Total trades: {cumulative['total_trades']}",
            f"• Win rate: {cumulative['win_rate_pct']:.1f}%",
            f"• Profit factor: {cumulative['profit_factor']:.2f}",
            f"• Net P&L: ₹{cumulative['net_pnl']:,.2f}",
            f"• Paper Gate: {'CLEARED 🟢' if paper_gate_status(self.db)['gate_cleared'] else 'HOLD (' + str(paper_gate_status(self.db)['days_elapsed']) + '/' + str(paper_gate_status(self.db)['days_required']) + ' days, ' + str(paper_gate_status(self.db)['trades_completed']) + '/' + str(paper_gate_status(self.db)['trades_required']) + ' trades)'}",
            "",
            "<b>Health Exceptions</b>",
        ]

        issues = [
            r
            for r in results
            if r.status in ("WARNING", "BLOCKER")
        ]

        if not issues:
            lines.append("🟢 None")
        else:
            for result in issues:
                icon = "🔴" if result.status == "BLOCKER" else "🟡"
                lines.append(
                    f"{icon} {result.domain}: {result.message}"
                )

        text = "\n".join(lines)
        print(text)

        if not self.telegram.is_configured():
            log.error("Telegram not configured")
            return False

        ok, _, error = self.telegram.send_message(text)

        if not ok:
            log.error("EOD Telegram failed: %s", error)

        return ok

    def send_issue_alert_if_any(
        self,
        results: Optional[List[HealthCheckResult]] = None,
    ) -> bool:

        results = results or self.run_all_checks()

        issues = [
            r
            for r in results
            if r.status in ("WARNING", "BLOCKER")
        ]

        if not issues:
            return True

        lines = [
            "🚨 <b>ALGO SYSTEM — HEALTH ALERT</b>",
            "",
            f"• <b>Time:</b> "
            f"{now_ist_naive().strftime('%Y-%m-%d %H:%M:%S IST')}",
            "",
        ]

        for result in issues:
            icon = "🔴" if result.status == "BLOCKER" else "🟡"
            lines.append(
                f"{icon} <b>{result.domain}</b>: {result.message}"
            )

        text = "\n".join(lines)
        print(text)

        if not self.telegram.is_configured():
            return False

        ok, _, error = self.telegram.send_message(text)

        if not ok:
            log.error("Telegram health alert failed: %s", error)

        return ok


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--mode",
        choices=["morning", "eod", "check"],
        default="check",
    )

    args = parser.parse_args()

    agent = AlgoHealthAgent()

    if args.mode == "morning":
        return args.mode, 0 if agent.send_morning_report() else 1

    if args.mode == "eod":
        return args.mode, 0 if agent.send_eod_report() else 1

    results = agent.run_all_checks()

    issues = [
        r
        for r in results
        if r.status in ("WARNING", "BLOCKER")
    ]

    blockers = [
        r
        for r in results
        if r.status == "BLOCKER"
    ]

    if issues:
        delivered = agent.send_issue_alert_if_any(results)

        if not delivered:
            return args.mode, 1

    if blockers:
        return args.mode, 1

    print("HEALTH CHECK PASSED")
    return args.mode, 0


if __name__ == "__main__":
    try:
        health_mode, exit_code = main()
    except BaseException as exc:
        log.error(
            "Health agent failed: %s: %s",
            type(exc).__name__,
            exc,
        )
        write_runtime_marker(
            "algo_health_agent",
            "FAILED",
            f"{type(exc).__name__}:{exc}",
        )
        raise
    else:
        write_runtime_marker(
            "algo_health_agent",
            "SUCCESS" if exit_code == 0 else "FAILED",
            f"mode={health_mode} exit_code={exit_code}",
        )
        raise SystemExit(exit_code)
