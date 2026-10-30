"""
tools/walmart_morning_scout.py
Walmart Reverse Distributor Discovery Scout.
Finds high-demand, zero-WFS products on Walmart, dynamically calculates the target
wholesale cost based on .env's MIN_ROI_FLOOR, checks the IP firewall, and generates
distributor search leads for outreach.
"""

import os
import sys
import json
import logging
import urllib.parse
from pathlib import Path
from typing import List, Dict, Any, Optional

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from dotenv import load_dotenv
load_dotenv(ROOT_DIR / ".env", override=True)

from tools.ip_guard import is_brand_blocked

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] WalmartScout: %(message)s")
logger = logging.getLogger("WalmartScout")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Dynamic runtime settings from .env
_raw_roi = float(os.getenv("MIN_ROI_FLOOR", os.getenv("MIN_ROI", 1.00)))
TARGET_MIN_ROI = _raw_roi if _raw_roi <= 2.0 else (_raw_roi / 100.0)
PREP_FEE = float(os.getenv("PARKLAND_PREP_FEE_UNIT", 0.06))
WALMART_REFERRAL_PCT = 0.15


def estimate_wfs_fee(weight_lbs: float) -> float:
    if weight_lbs <= 1.0:
        return 3.45
    elif weight_lbs <= 2.0:
        return 4.95
    elif weight_lbs <= 3.0:
        return 5.45
    return round(5.75 + max(0.0, weight_lbs - 4.0) * 0.40, 2)


def calculate_max_allowable_cost(retail_price: float, weight_lbs: float) -> float:
    """
    Computes Maximum Allowable Wholesale Cost (MAC) to achieve dynamic TARGET_MIN_ROI.
    Net Profit = Retail - Referral - WFS Fee - Inbound Freight - Prep - Cost
    Target: Net Profit >= TARGET_MIN_ROI * Total Landed Cost
    """
    inbound_freight = round(max(0.65, weight_lbs * 0.40), 2)
    prep_and_freight = PREP_FEE + inbound_freight
    walmart_deductions = (retail_price * WALMART_REFERRAL_PCT) + estimate_wfs_fee(weight_lbs)

    net_before_cost = retail_price - walmart_deductions
    # Wholesale_Cost <= (Net_Before_Cost - (1 + ROI) * Overhead) / (1 + ROI)
    max_cost = (net_before_cost - ((1.0 + TARGET_MIN_ROI) * prep_and_freight)) / (1.0 + TARGET_MIN_ROI)
    return max(0.0, round(max_cost, 2))


def evaluate_walmart_lead(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    brand = item.get("brand", "").strip()
    title = item.get("title", "").strip()
    mpn = item.get("mpn", item.get("model", "")).strip()

    # 1. Layer 0 IP Firewall Check
    blocked, reason = is_brand_blocked(brand=brand, title=title)
    if blocked:
        logger.debug(f"Skipping brand '{brand}': {reason}")
        return None

    retail_price = float(item.get("price", 0.0))
    monthly_sales = int(item.get("monthly_sales_est", 0))
    wfs_sellers = int(item.get("wfs_sellers", 0))
    merchant_sellers = int(item.get("merchant_sellers", 0))
    weight_lbs = float(item.get("weight_lbs", 1.5))

    if retail_price < 25.0 or monthly_sales < 15 or wfs_sellers > 0:
        return None

    target_buy_cost = calculate_max_allowable_cost(retail_price, weight_lbs)
    if target_buy_cost < 3.0:
        return None

    inbound_freight = round(max(0.65, weight_lbs * 0.40), 2)
    landed_cost = target_buy_cost + inbound_freight + PREP_FEE
    fees = (retail_price * WALMART_REFERRAL_PCT) + estimate_wfs_fee(weight_lbs)
    projected_net_profit = round(retail_price - fees - landed_cost, 2)
    projected_roi = round((projected_net_profit / landed_cost) * 100, 1) if landed_cost > 0 else 0.0

    query_str = f'"{brand}" wholesale distributor OR "authorized dealer" USA'
    search_url = f"https://www.google.com/search?q={urllib.parse.quote(query_str)}"

    return {
        "item_id": item.get("item_id", ""),
        "title": title,
        "brand": brand,
        "mpn": mpn,
        "upc": item.get("upc", ""),
        "retail_price": retail_price,
        "target_buy_cost": target_buy_cost,
        "projected_net_profit": projected_net_profit,
        "projected_roi": projected_roi,
        "monthly_velocity": monthly_sales,
        "merchant_sellers": merchant_sellers,
        "weight_lbs": weight_lbs,
        "search_url": search_url
    }


def send_distributor_leads_telegram(leads: List[Dict[str, Any]]):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not leads:
        logger.warning("Telegram credentials missing or no leads to send.")
        return

    import telebot
    from telebot import types

    bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode="HTML")
    bot.send_message(
        TELEGRAM_CHAT_ID,
        f"🎯 <b>WALMART REVERSE DISTRIBUTOR DISCOVERY</b>\n"
        f"Identified <b>{len(leads)}</b> velocity products with <b>0 WFS competition</b>.\n"
        f"<i>Target costs calibrated for {int(TARGET_MIN_ROI * 100)}%+ Net ROI.</i>"
    )

    for lead in leads[:8]:
        card = (
            f"📦 <b>{lead['brand'].upper()}</b> — {lead['title'][:55]}...\n"
            f"• MPN / Model: <code>{lead['mpn'] or 'See Title'}</code> | UPC: <code>{lead['upc'] or 'N/A'}</code>\n"
            f"• Walmart Retail Price: <b>${lead['retail_price']:.2f}</b>\n"
            f"• <b>Target Wholesale Cost: &le; ${lead['target_buy_cost']:.2f}</b> (For {int(TARGET_MIN_ROI * 100)}% ROI)\n"
            f"• Estimated Demand: <b>~{lead['monthly_velocity']} units/month</b>\n"
            f"• Competition: <b>0 WFS Sellers</b> ({lead['merchant_sellers']} Merchant/FBM)\n"
            f"• Projected Profit: <b>${lead['projected_net_profit']:.2f} / unit</b> ({lead['projected_roi']}% ROI)"
        )

        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("🔍 Find Distributors", url=lead["search_url"]),
            types.InlineKeyboardButton("🚫 Deny Brand", callback_data=f"block_brand_{lead['brand'][:15]}")
        )

        bot.send_message(TELEGRAM_CHAT_ID, card, reply_markup=markup, disable_web_page_preview=True)


if __name__ == "__main__":
    logger.info("Walmart Reverse Distributor Discovery Scout initialized.")
