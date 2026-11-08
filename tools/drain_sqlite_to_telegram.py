#!/usr/bin/env python3
"""
tools/drain_sqlite_to_telegram.py
---------------------------------
Permanent SQLite Cache Drain & Alert Dispatcher.
Extracts candidates stored in hunter_engine.db (candidate_cache & verified_deals),
evaluates them under the dynamic 50-unit step sliding-scale profit floor:
- $10.00 floor @ <= 50 units
- -$0.50 per +50 units
- $5.00 floor @ >= 500 units
- 68% down to 50% ROI hurdle
- Decoupled Logistics: Makki Prep (PA) for BCI; Detroit 3PL ($1.10 TBD) for MI vendors

Dispatches passing deals directly to Telegram with interactive action buttons.
"""

import os
import sys
import html
import time
import sqlite3
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
import requests
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

load_dotenv(ROOT_DIR / ".env", override=False)

from config import calculate_sliding_profit_floor, get_logistics_profile

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] CacheDrainer: %(message)s"
)
logger = logging.getLogger("CacheDrainer")

DB_PATH = ROOT_DIR / "data" / "hunter_engine.db"

# Telegram Token Sanitization
_raw_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip().strip('"').strip("'")
if _raw_token.lower().startswith("bot"):
    TELEGRAM_BOT_TOKEN = _raw_token[3:]
else:
    TELEGRAM_BOT_TOKEN = _raw_token

TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip().strip('"').strip("'")


def send_telegram_card(deal: Dict[str, Any]) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials missing in environment.")
        return False

    title_esc = html.escape(deal["title"][:45])
    brand_esc = html.escape(deal["brand"])
    vendor_esc = html.escape(deal["vendor"])
    hazmat_tag = "⚠️ <b>HAZMAT</b> | " if deal.get("is_hazmat") else ""

    msg = (
        f"💎 <b>QUALIFIED LEAD RESURRECTED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 <a href=\"{deal['order_url']}\">{title_esc}</a>\n"
        f"🏷 <b>Brand:</b> <code>{brand_esc}</code> | <b>ASIN:</b> <code>{deal['asin']}</code>\n"
        f"🏢 <b>Vendor / Hub:</b> {vendor_esc} ({deal['prep_hub']})\n"
        f"⚡ {hazmat_tag}<b>Pack:</b> {deal['pack_count']}x | <b>Weight:</b> {deal['weight_lbs']} lbs\n"
        f"📈 <b>Total Sales:</b> {deal['monthly_sales']} sold/mo | <b>Our Share:</b> {deal['our_share']} units/mo\n"
        f"👥 <b>FBA Sellers:</b> {deal['competitive_fba']} in rotation\n"
        f"💰 <b>Buy Box:</b> ${deal['buy_box_price']:.2f}\n"
        f"💵 <b>Wholesale Buy:</b> ${deal['wholesale_cost']:.2f} (Target MAC: ${deal['max_allowable_cost']:.2f})\n"
        f"🚀 <b>Net Profit:</b> <b>${deal['net_profit']:.2f} / unit</b> ({deal['roi_percent']}% ROI)\n"
        f"🎯 <b>Hurdles Applied:</b> Min ${deal['profit_floor']:.2f} Floor | {deal['req_roi']}% ROI\n"
        f"🏊 <b>Estimated Monthly Net:</b> <b>${deal['monthly_profit_pool']:.2f} / mo</b>\n"
        f"📦 <b>Suggested Order (14d):</b> {deal['suggested_order_size']} units (${deal['expected_order_profit']:.2f} net)\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔐 <b>Gating Check:</b> <a href=\"{deal['seller_check_url']}\">Seller Central</a> | <a href=\"{deal['seller_approval_url']}\">Request Ungate</a>"
    )

    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "✅ BUY LIST", "callback_data": f"BUYLIST:{deal['asin']}:{deal['brand']}"},
                {"text": "🚫 BLOCK IP", "callback_data": f"BLOCKIP:{deal['asin']}:{deal['brand']}"}
            ],
            [
                {"text": "🔓 CHECK GATING", "url": deal["seller_check_url"]},
                {"text": "📝 APPROVAL", "url": deal["seller_approval_url"]}
            ],
            [
                {"text": "⛔ PASS", "callback_data": f"PASS:{deal['asin']}"},
                {"text": "🛒 AMAZON", "url": deal["order_url"]}
            ]
        ]
    }

    try:
        res = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
                "parse_mode": "HTML",
                "reply_markup": reply_markup,
                "disable_web_page_preview": True
            },
            timeout=12
        )
        return res.status_code == 200
    except Exception as e:
        logger.warning(f"Telegram dispatch error: {e}")
        return False


def drain_database(batch_limit: int = 150):
    if not DB_PATH.exists():
        logger.error(f"Database not found at {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    logger.info("Querying candidate_cache for stored QUALIFIED, SCOUTED_DEAL, and FILTERED records...")
    cur.execute("""
        SELECT asin, title, brand, category_id, sales_rank, avg90_bb, current_bb, monthly_sold, 
               drops_90, amazon_pack_qty, referral_pct, fba_fee, weight_lbs, num_sellers, 
               is_hazmat, status
        FROM candidate_cache 
        WHERE status IN ('QUALIFIED', 'SCOUTED_DEAL', 'FILTERED')
          AND monthly_sold >= 50
        ORDER BY monthly_sold DESC
    """)
    candidates = cur.fetchall()
    logger.info(f"Loaded {len(candidates)} candidates from cache. Applying sliding-scale evaluation...")

    dispatched = 0

    for r in candidates:
        asin = r["asin"]
        bb = float(r["avg90_bb"] or r["current_bb"] or 0.0)
        sales = int(r["monthly_sold"] or 0)
        brand = str(r["brand"] or "MRO Commercial").strip()
        title = str(r["title"] or "Catalog SKU")
        fba_sellers = max(1, int(r["num_sellers"] or 1))
        weight = float(r["weight_lbs"] or 1.0)
        is_hazmat = bool(r["is_hazmat"])
        pack_qty = int(r["amazon_pack_qty"] or 1)

        if bb < 13.50 or sales < 50:
            continue

        # Decoupled Logistics Routing Matrix
        logistics = get_logistics_profile(brand)
        prep_base = logistics["prep_base"]
        freight_rate = logistics["freight_per_lb"]
        if is_hazmat:
            prep_base += 0.60
            freight_rate += 0.20

        inbound_freight = round(weight * freight_rate, 2)
        total_prep = round(prep_base + inbound_freight + 0.27, 2)

        referral_fee = round(bb * float(r["referral_pct"] or 0.15), 2)
        fba_fee = float(r["fba_fee"] or (3.86 + (weight * 0.45)))
        return_drag = round(bb * 0.05, 2)
        total_overhead = referral_fee + fba_fee + total_prep + return_drag
        payout = round(bb - total_overhead, 2)

        # Fair share rotation & sliding floor
        our_share = sales / (fba_sellers + 1)
        profit_floor, req_roi = calculate_sliding_profit_floor(our_share)

        max_allowable_cost = round(payout - profit_floor, 2)
        if max_allowable_cost < 1.00:
            continue

        # Check if item has a verified wholesale catalog cost in verified_deals
        cur.execute("SELECT wholesale_cost, vendor FROM verified_deals WHERE asin = ?", (asin,))
        verified = cur.fetchone()

        if verified and verified["wholesale_cost"] and verified["wholesale_cost"] > 0:
            wholesale_cost = float(verified["wholesale_cost"])
            vendor_display = verified["vendor"] or brand
        else:
            # Target Acquisition ratio benchmark (40% of Buy Box)
            estimated_cost = round(bb * 0.40, 2)
            wholesale_cost = min(estimated_cost, max_allowable_cost)
            vendor_display = f"Target Brand: {brand}"

        if wholesale_cost < 1.00 or wholesale_cost > 65.00:
            continue

        net_profit = round(payout - wholesale_cost, 2)
        landed_cost = wholesale_cost + total_prep
        roi_percent = round((net_profit / landed_cost) * 100.0, 1) if landed_cost > 0 else 0.0

        # Enforce sliding scale qualification
        if net_profit >= profit_floor and roi_percent >= req_roi:
            suggested_order = max(12, int(our_share * 0.5))
            deal_data = {
                "asin": asin,
                "title": title,
                "brand": brand,
                "vendor": vendor_display,
                "prep_hub": logistics["facility"],
                "pack_count": pack_qty,
                "weight_lbs": weight,
                "is_hazmat": is_hazmat,
                "monthly_sales": sales,
                "our_share": int(our_share),
                "competitive_fba": fba_sellers,
                "buy_box_price": bb,
                "wholesale_cost": wholesale_cost,
                "max_allowable_cost": max_allowable_cost,
                "net_profit": net_profit,
                "roi_percent": roi_percent,
                "profit_floor": profit_floor,
                "req_roi": req_roi,
                "monthly_profit_pool": round(our_share * net_profit, 2),
                "suggested_order_size": suggested_order,
                "expected_order_profit": round(net_profit * suggested_order, 2),
                "order_url": f"https://www.amazon.com/dp/{asin}",
                "seller_check_url": f"https://sellercentral.amazon.com/product-search/search?q={asin}",
                "seller_approval_url": f"https://sellercentral.amazon.com/hz/approvalrequest/restrictions/approve?asin={asin}"
            }

            if send_telegram_card(deal_data):
                dispatched += 1
                # Mark as qualified in SQLite
                cur.execute("UPDATE candidate_cache SET status = 'QUALIFIED' WHERE asin = ?", (asin,))
                time.sleep(0.35)  # Telegram API rate limit protection

            if dispatched >= batch_limit:
                logger.info(f"Reached batch limit of {batch_limit} dispatches. Pausing execution.")
                break

    conn.commit()
    conn.close()
    logger.info(f"Drain run completed. Successfully delivered {dispatched} leads to Telegram.")


if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 150
    drain_database(batch_limit=limit)
