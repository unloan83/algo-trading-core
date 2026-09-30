import json
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd

from core.models import Position, RegimeType, Side, Signal
from core.risk_governor import RiskGovernor


CANONICAL_DECISIONS = (
    '{"blocked":[],"ordered":['
    '{"is_intraday":false,"qty":100,"symbol":"XYZ"},'
    '{"is_intraday":true,"qty":100,"symbol":"ABC"}'
    '],"pending_status":[["p1","EXECUTED"]]}'
)


class TestObservabilityGoldenMaster(unittest.TestCase):
    def test_intraday_observability_does_not_change_decisions_or_quantities(self):
        """Lock the pre-observability pending + intraday orchestration result."""
        from scripts import intraday_scan

        now = datetime(2026, 9, 30, 10, 0, 0)
        os.environ["TRADING_MODE"] = "PAPER"

        bars = pd.DataFrame(
            {
                "timestamp": [
                    now - timedelta(minutes=5 * offset)
                    for offset in range(60, 0, -1)
                ],
                "open": [100.0] * 60,
                "high": [101.0] * 60,
                "low": [99.0] * 60,
                "close": [100.5] * 60,
                "volume": [10_000] * 60,
            }
        )
        intraday_signal = Signal(
            symbol="ABC",
            side=Side.BUY,
            entry_price=100.0,
            stop_price=95.0,
            target_price=110.0,
            model_name="breakout",
            regime=RegimeType.TREND_UP,
            rationale="golden master",
            timestamp=now,
        )
        pending = {
            "pending_id": "p1",
            "symbol": "XYZ",
            "side": "BUY",
            "entry_price": 100.0,
            "stop_price": 95.0,
            "target_price": 110.0,
            "model_name": "trend_pullback",
            "regime": "TREND_UP",
            "rationale": "approved at EOD",
            "auto_executed_on_timeout": False,
        }

        outcome = {
            "blocked": [],
            "ordered": [],
            "pending_status": [],
        }

        db = MagicMock()
        db.get_open_positions.return_value = []
        db.get_pending_signals.return_value = [pending]
        db.get_latest_circuit_state.return_value = {
            "consecutive_losses": 0,
            "last_loss_time": None,
        }
        db.count_paper_orders_on_date.return_value = 0

        def paper_account(starting_capital, positions):
            committed = sum(position.entry_price * position.qty for position in positions)
            current_value = sum(position.current_price * position.qty for position in positions)
            return starting_capital - committed, starting_capital - committed + current_value

        def persist_order(order):
            outcome["ordered"].append(
                {
                    "is_intraday": order.is_intraday,
                    "qty": order.qty,
                    "symbol": order.symbol,
                }
            )
            fill = float(order.filled_price or order.entry_price)
            return Position(
                position_id=f"pos-{order.symbol}",
                symbol=order.symbol,
                side=order.side,
                qty=order.qty,
                entry_price=fill,
                current_price=fill,
                stop_price=order.stop_price,
                target_price=order.target_price,
                opened_at=order.created_at,
                entry_signal_price=order.entry_price,
                auto_executed_on_timeout=order.auto_executed_on_timeout,
                is_paper=True,
                is_intraday=order.is_intraday,
            )

        db.paper_account.side_effect = paper_account
        db.record_paper_order_and_open_position.side_effect = persist_order
        db.mark_pending_signal.side_effect = (
            lambda pending_id, status: outcome["pending_status"].append(
                [pending_id, status]
            )
        )
        db.record_blocked_signal.side_effect = (
            lambda symbol, *_args, **_kwargs: outcome["blocked"].append(symbol)
        )

        broker = MagicMock()
        broker.validate_readonly_access.return_value = (True, "OK")
        broker.is_nse_trading_day.return_value = True
        broker.get_ltp.return_value = 100.0
        broker.get_intraday_history.return_value = bars

        tracker = MagicMock()
        tracker.compute_mark_to_market_pnl.return_value = (0.0, 0.0, 0.0)

        telegram = MagicMock()
        telegram.is_configured.return_value = True
        telegram.poll_callback_query.return_value = None

        def config(name):
            if name == "timing.yaml":
                return {
                    "timing": {
                        "intraday_scan": {
                            "entry_window_start": "09:25",
                            "entry_window_end": "14:15",
                            "gap_filter_pct": 1.5,
                            "max_orders_per_day": 6,
                        },
                        "telegram_approval_timeout_seconds": 0,
                    }
                }
            if name == "risk_limits.yaml":
                return {"default_action_on_timeout": "system_recommendation"}
            if name == "universe.yaml":
                return {"universe": {"top_n": 1}}
            raise AssertionError(f"Unexpected config request: {name}")

        screener = MagicMock()
        screener.run_intraday_scan.return_value = [intraday_signal]

        with patch.object(intraday_scan, "now_ist_naive", return_value=now), patch.object(
            intraday_scan, "project_config", side_effect=config
        ), patch.object(
            intraday_scan, "UnifiedBrokerClient", return_value=broker
        ), patch.object(
            intraday_scan, "DatabaseManager", return_value=db
        ), patch.object(
            intraday_scan, "load_active_universe", return_value=["ABC"]
        ), patch.object(
            intraday_scan, "paper_starting_capital", return_value=100_000.0
        ), patch.object(
            intraday_scan, "CircuitTracker", return_value=tracker
        ), patch.object(
            intraday_scan, "load_market_filters", return_value=([], [])
        ), patch.object(
            intraday_scan, "build_risk_governor", return_value=RiskGovernor()
        ), patch.object(
            intraday_scan, "TelegramClient", return_value=telegram
        ), patch.object(
            intraday_scan, "evaluate_regime", return_value=(RegimeType.TREND_UP, "fixture")
        ), patch.object(
            intraday_scan, "Screener", return_value=screener
        ), patch.object(intraday_scan, "write_runtime_marker"):
            intraday_scan.main()

        actual = json.dumps(outcome, separators=(",", ":"))
        self.assertEqual(actual, CANONICAL_DECISIONS)


if __name__ == "__main__":
    unittest.main()
