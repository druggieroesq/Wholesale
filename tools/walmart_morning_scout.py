"""
tools/walmart_morning_scout.py
Dual-Channel Morning Scout: Cross-References Amazon FBA Viability with Walmart WFS Gaps.
Features:
- Primary Gate: 100% Net ROI on Amazon FBA (Active Selling Channel).
- Secondary Multiplier: Walmart WFS Ghost Gap (0 WFS sellers, FBM presence).
- Enforces Layer 0 IP Guard (659+ protected brands).
- Sends actionable dual-channel lead cards to Telegram.
"""

import os
import sys
import json
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tools.ip_guard import is_brand_blocked

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DENIED_SUPPLIERS_FILE = ROOT_DIR / "data" / "denied_suppliers.json"

PREP_FEE = 0.06
AMAZON_REFERRAL_PCT = 0.15
WALMART_REFERRAL_PCT = 0.15


def load_denied_suppliers() -> List[str]:
    if DENIED_SUPPLIERS_FILE.exists():
        try:
            with open(DENIED_SUPPLIERS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def estimate_fba_fee(weight_lbs: float) -> float:
    if weight_lbs <= 1.0:
        return 3.86
    elif weight_lbs <= 2.0:
        return 5.40
    elif weight_lbs <= 3.0:
        return 6.10
    return round(6.10 + (weight_lbs - 3.0) * 0.45, 2)


def estimate_wfs_fee(weight_lbs: float) -> float:
    """Walmart standard fulfillment fee tiers (2026 schedule)."""
    if weight_lbs <= 1.0:
        return 3.45
    elif weight_lbs <= 2.0:
        return 4.95
    elif weight_lbs <= 3.0:
        return 5.45
    return round(5.75 + (weight_lbs - 4.0) * 0.40, 2)


def evaluate_dual_channel_opportunity(
    item: Dict[str, Any],
    min_roi_floor: float = 1.00
) -> Optional[Dict[str, Any]]:
    """
    Evaluates item viability on BOTH channels:
    1. Layer 0 IP Guard check.
    2. Amazon FBA: Must meet 100% Net ROI floor (immediate active sales).
    3. Walmart WFS: Flags 0-WFS gap for secondary scale.
    """
    brand = item.get("brand", "")
    title = item.get("title", "")
    vendor = item.get("vendor", "")

    # 1. Layer 0 IP Guard Check
    blocked, reason = is_brand_blocked(brand=brand, title=title, vendor=vendor)
    if blocked:
        logger.debug(f"Skipping {brand}: {reason}")
        return None

    cost = float(item.get("wholesale_cost", 0.0))
    amz_price = float(item.get("amz_price", item.get("amazon_price", 0.0)))
    walmart_price = float(item.get("walmart_price", amz_price))
    weight_lbs = float(item.get("weight_lbs", 1.5))

    if cost <= 0 or amz_price <= 0:
        return None

    # Inbound freight + prep
    inbound_freight = round(max(0.65, weight_lbs * 0.40), 2)
    landed_cost = cost + inbound_freight + PREP_FEE

    # 2. Primary Gate: Amazon FBA Financials
    amz_fees = (amz_price * AMAZON_REFERRAL_PCT) + estimate_fba_fee(weight_lbs)
    amz_net_profit = amz_price - amz_fees - landed_cost
    amz_roi = (amz_net_profit / landed_cost) if landed_cost > 0 else 0.0

    # Strict Amazon 100% Net ROI floor
    if amz_roi < min_roi_floor or amz_net_profit < 5.00:
        return None

    # 3. Secondary Gate: Walmart WFS Gap Analysis
    wfs_sellers = int(item.get("wfs_sellers", 0))
    merchant_sellers = int(item.get("merchant_sellers", 0))
    
    # Calculate projected Walmart financials
    wfs_fees = (walmart_price * WALMART_REFERRAL_PCT) + estimate_wfs_fee(weight_lbs)
    wm_net_profit = walmart_price - wfs_fees - landed_cost
    wm_roi = (wm_net_profit / landed_cost) if landed_cost > 0 else 0.0

    has_wfs_ghost_gap = (wfs_sellers == 0 and merchant_sellers > 0)

    return {
        "item_id": item.get("item_id", item.get("asin", "")),
        "asin": item.get("asin", ""),
        "upc": item.get("upc", ""),
        "title": title,
        "brand": brand,
        "vendor": vendor,
        "wholesale_cost": cost,
        "landed_cost": round(landed_cost, 2),
        "weight_lbs": weight_lbs,
        # Amazon Metrics (Active Execution)
        "amz_price": amz_price,
        "amz_net_profit": round(amz_net_profit, 2),
        "amz_roi_pct": round(amz_roi * 100, 1),
        # Walmart Metrics (Expansion Horizon)
        "walmart_price": walmart_price,
        "wm_net_profit": round(wm_net_profit, 2),
        "wm_roi_pct": round(wm_roi * 100, 1),
        "wfs_sellers": wfs_sellers,
        "merchant_sellers": merchant_sellers,
        "has_wfs_ghost_gap": has_wfs_ghost_gap,
        "lead_type": "DUAL_CHANNEL_AMZ_PROVEN_WFS_GAP" if has_wfs_ghost_gap else "AMZ_ACTIVE_ONLY"
    }


def send_morning_scout_telegram(deals: List[Dict[str, Any]]):
    """Sends formatted dual-channel scout cards to Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not deals:
        logger.warning("Telegram credentials missing or no deals found.")
        return

    import telebot
    from telebot import types

    bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode="HTML")
    bot.send_message(
        TELEGRAM_CHAT_ID,
        f"☀️ <b>DUAL-CHANNEL MORNING SCOUT REPORT</b>\n"
        f"Discovered <b>{len(deals)}</b> safe wholesale items today.\n"
        f"<i>100%+ Net ROI verified on Amazon FBA with ready WFS expansion upside.</i>"
    )

    for d in deals[:10]:
        wfs_badge = "🔥 <b>0 WFS SELLERS (Monopoly Ready)</b>" if d["has_wfs_ghost_gap"] else "Standard Competition"
        msg = (
            f"🎯 <b>{d.get('title', 'Wholesale Item')[:45]}</b>\n"
            f"• Brand: <code>{d.get('brand', 'N/A')}</code> | Vendor: <code>{d.get('vendor', 'N/A')}</code>\n"
            f"• Landed Cost: ${d['landed_cost']:.2f} (Cost: ${d['wholesale_cost']:.2f})\n\n"
            f"📦 <b>Amazon FBA (Live Channel):</b>\n"
            f"  Retail: ${d['amz_price']:.2f} | Net: <b>${d['amz_net_profit']:.2f}</b> (<b>{d['amz_roi_pct']}% ROI</b>)\n\n"
            f"🏪 <b>Walmart WFS Projection:</b>\n"
            f"  Retail: ${d['walmart_price']:.2f} | Net: ${d['wm_net_profit']:.2f} ({d['wm_roi_pct']}% ROI)\n"
            f"  Status: {wfs_badge}\n"
            f"• Strategy: <code>{d['lead_type']}</code>"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("✅ Review Lead", callback_data=f"lead_ok_{d.get('item_id', '1')}"),
            types.InlineKeyboardButton("🚫 Deny Supplier", callback_data=f"lead_deny_{d.get('vendor', '1')}")
        )
        bot.send_message(TELEGRAM_CHAT_ID, msg, reply_markup=markup)


if __name__ == "__main__":
    logger.info("Dual-Channel Morning Scout initialized.")
