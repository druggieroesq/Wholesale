"""
tools/walmart_morning_scout.py
Daily Morning Scout for Walmart & Emerging Wholesale Distributors.
Features:
- Scans Walmart catalog opportunities with WFS Ghost Inventory gaps (3P exists, 0 WFS sellers).
- Filters for proven velocity (estimated monthly units >= 15).
- Sends 10 daily unique distributor leads with inline Contacted/Denied buttons to Telegram.
"""

import os
import json
import logging
import requests
from typing import List, Dict, Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DENIED_SUPPLIERS_FILE = "data/denied_suppliers.json"

def load_denied_suppliers() -> List[str]:
    if os.path.exists(DENIED_SUPPLIERS_FILE):
        try:
            with open(DENIED_SUPPLIERS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def evaluate_walmart_wfs_gap(
    item: Dict[str, Any],
    min_monthly_velocity: int = 15,
    min_roi_floor: float = 1.00
) -> Dict[str, Any]:
    """
    Identifies items where WFS fulfillment gives an automatic monopoly over Merchant (FBM) sellers.
    """
    price = float(item.get("price", 0.0))
    cost = float(item.get("wholesale_cost", 0.0))
    monthly_sales = int(item.get("monthly_sales_est", 0))
    wfs_sellers = int(item.get("wfs_sellers", 0))
    merchant_sellers = int(item.get("merchant_sellers", 0))

    if monthly_sales < min_monthly_velocity or wfs_sellers > 0:
        return None

    # Walmart WFS fee modeling (~15% referral + ~$4.25 WFS fulfillment)
    fees = (price * 0.15) + 4.25
    prep = 0.06
    net_profit = price - fees - cost - prep
    roi = (net_profit / (cost + prep)) if (cost + prep) > 0 else 0.0

    if roi < min_roi_floor or net_profit < 4.50:
        return None

    return {
        "item_id": item.get("item_id"),
        "title": item.get("title"),
        "price": price,
        "wholesale_cost": cost,
        "net_profit": round(net_profit, 2),
        "roi_pct": round(roi * 100, 1),
        "monthly_velocity": monthly_sales,
        "wfs_sellers": wfs_sellers,
        "merchant_sellers": merchant_sellers,
        "lead_type": "WFS_GHOST_GAP_MONOPOLY"
    }

def send_morning_scout_telegram(deals: List[Dict[str, Any]]):
    """Sends formatted morning scout cards to Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not deals:
        logger.warning("Telegram credentials missing or no deals found.")
        return

    import telebot
    from telebot import types

    bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode="HTML")
    bot.send_message(
        TELEGRAM_CHAT_ID,
        f"☀️ <b>MORNING WHOLESALE SCOUT REPORT</b>\n"
        f"Discovered <b>{len(deals)}</b> low-competition velocity targets today:\n"
        f"<i>Zero BSR dependency — selected for unit run-rate & monopoly position.</i>"
    )

    for d in deals[:10]:
        msg = (
            f"🎯 <b>{d.get('title', 'Wholesale Item')}</b>\n"
            f"• Wholesale Cost: ${d['wholesale_cost']:.2f} | Retail: ${d['price']:.2f}\n"
            f"• Net Profit: <b>${d['net_profit']:.2f}</b> ({d['roi_pct']}% ROI)\n"
            f"• Velocity: <b>~{d['monthly_velocity']} units/mo</b>\n"
            f"• Competition: <b>{d['wfs_sellers']} WFS sellers</b> ({d['merchant_sellers']} merchant)\n"
            f"• Strategy: <code>{d['lead_type']}</code>"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("✅ Review Lead", callback_data=f"lead_ok_{d.get('item_id', '1')}"),
            types.InlineKeyboardButton("🚫 Deny", callback_data=f"lead_deny_{d.get('item_id', '1')}")
        )
        bot.send_message(TELEGRAM_CHAT_ID, msg, reply_markup=markup)

if __name__ == "__main__":
    logger.info("Walmart Morning Scout initialized.")
