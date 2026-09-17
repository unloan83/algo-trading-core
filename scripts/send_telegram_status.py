#!/usr/bin/env python3
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from telegram_bot.telegram_client import TelegramClient
from data.db_models import DatabaseManager
from journal.analytics import AnalyticsEngine

def main():
    print(f"[{datetime.now().isoformat()}] Checking Telegram Bot Status...")
    client = TelegramClient()

    if not client.is_configured():
        print("\n⚠️ TELEGRAM BOT NOT ACTIVE YET!")
        print("To activate Telegram notifications:")
        print("Set TELEGRAM_BOT_TOKEN / TELEGRAM_TOKEN and TELEGRAM_CHAT_ID in .env")
        return

    db = DatabaseManager()
    analytics = AnalyticsEngine(db)
    metrics = analytics.compute_performance_metrics()

    max_dd = metrics.get('max_drawdown', metrics.get('max_drawdown_pct', 0.0))

    status_msg = (
        f"🟢 <b>AUTOMATED TRADING SYSTEM — STATUS REPORT</b>\n\n"
        f"• <b>Status:</b> Active (Paper Trading Mode)\n"
        f"• <b>Timestamp:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}\n"
        f"• <b>Total Trades Executed:</b> {metrics['total_trades']}\n"
        f"• <b>Win Rate:</b> {metrics['win_rate_pct']:.1f}%\n"
        f"• <b>Profit Factor:</b> {metrics['profit_factor']:.2f}\n"
        f"• <b>Net Realized P&L:</b> ₹{metrics['net_pnl']:.2f}\n"
        f"• <b>Max Drawdown:</b> ₹{max_dd:.2f}\n\n"
        f"<i>Risk Governor active. 120s inline approval gate ready.</i>"
    )

    success, msg_id, res_msg = client.send_message(status_msg)
    if success:
        print(f"✅ Telegram Status Message Sent Successfully! (Message ID: {msg_id})")
    else:
        print(f"❌ Telegram Dispatch Result: {res_msg}")

if __name__ == "__main__":
    main()
