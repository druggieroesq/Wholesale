"""
tools/test_outbound_telegram.py
Sends an immediate outbound test message and a mock product card to your Telegram chat.
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()

from tools.telegram_polling_daemon import bot, send_product_card, TELEGRAM_CHAT_ID

if not bot or not TELEGRAM_CHAT_ID:
    print(f"ERROR: Missing credentials. Bot: {bool(bot)}, TELEGRAM_CHAT_ID: {TELEGRAM_CHAT_ID}")
    sys.exit(1)

print(f"Sending test outbound alert to Telegram Chat ID: {TELEGRAM_CHAT_ID}...")

# 1. Direct text message ping
msg = bot.send_message(
    TELEGRAM_CHAT_ID,
    "🚀 <b>MRO Arbitrage Engine: Outbound Broadcast Online</b>\n\n"
    "Bot token connection confirmed. Daily dispatches and queue actions will push here.",
    parse_mode="HTML"
)
print(f"Outbound ping delivered! Message ID: {msg.message_id}")

# 2. Interactive sample lead card with inline action buttons
mock_buy_item = {
    "asin": "B000TEST01",
    "title": "Parker Hannifin 3/8\" Brass Push-to-Connect Fitting (5-Pack)",
    "mpn_sku": "3101-08-14",
    "distributor": "Midland Metal / South FL Hub",
    "wholesale_cost": 12.50,
    "effective_price": 38.00,
    "net_profit": 15.20,
    "roi_pct": 121.6,
    "monthly_velocity": 35,
    "active_fba_sellers": 1
}

send_product_card(TELEGRAM_CHAT_ID, mock_buy_item, feed_type="BUY")
print("Interactive Buy Card sent successfully!")
