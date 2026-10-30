"""
tools/test_outbound_telegram.py
Self-healing test runner for Telegram outbound broadcast verification.
"""

import os
import sys
from pathlib import Path

# Resolve project root and force it onto sys.path to eliminate ModuleNotFoundError
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Safe .env loader that tolerates unquoted lines or syntax quirks
from dotenv import load_dotenv

env_path = PROJECT_ROOT / ".env"
if env_path.exists():
    try:
        load_dotenv(dotenv_path=env_path)
    except Exception as e:
        print(f"[WARN] Standard .env parsing failed ({e}). Applying line-by-line fallback...")
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip("'\"")
                if key and key not in os.environ:
                    os.environ[key] = val

from tools.telegram_polling_daemon import bot, send_product_card, TELEGRAM_CHAT_ID

if not bot or not TELEGRAM_CHAT_ID:
    print(
        f"ERROR: Bot or Chat ID missing. bot_initialized={bool(bot)}, "
        f"TELEGRAM_CHAT_ID={TELEGRAM_CHAT_ID}"
    )
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

# 2. Interactive Confirmed Buy Card
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
