#!/usr/bin/env python3
"""
tools/web_distributor_scout.py
------------------------------
Public Web & Portal Distributor Scout Engine (v6.1 Production)
Sweeps uncataloged Keepa candidates from SQLite candidate_cache:
1. Enforces strict high-velocity filtering (monthly_sold >= 100 units/mo).
2. Queries public distributor portals and search endpoints for MPN and UPC.
3. Extracts live B2B wholesale prices using regex and HTML heuristics.
4. Tests profitability against:
    * Net Profit Floor: >= $10.00
    * High Velocity (>= 500 units/mo): 58.0% ROI
    * Standard Velocity (100 - 499 units/mo): 75.0% ROI
5. Elevates confirmed deals into verified_deals and broadcasts to Telegram/Sheets.
"""

import os
import sys
import time
import json
import logging
import sqlite3
import re
import urllib.parse
from pathlib import Path
from typing import Dict, Any, Optional
import requests
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

load_dotenv(ROOT_DIR / ".env", override=False)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] WebScout: %(message)s"
)
logger = logging.getLogger("WebScout")

DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "hunter_engine.db"
CREDS_FILE = DATA_DIR / "distributor_credentials.json"
BUY_LIST_WEBHOOK_URL = (
    os.getenv("BUY_LIST_WEBHOOK_URL")
    or os.getenv("GOOGLE_SHEET_WEBHOOK_URL")
    or ""
).strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

MIN_MONTHLY_VELOCITY = 100
HIGH_VELOCITY_THRESHOLD = 500
PROFIT_FLOOR = 10.00

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
]


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def extract_price_from_text(text: str) -> Optional[float]:
    if not text:
        return None
    matches = re.findall(r'\$\s*([0-9]+(?:\.[0-9]{2})?)', text)
    valid_prices = []
    for m in matches:
        try:
            val = float(m)
            if 0.50 <= val <= 2500.0:
                valid_prices.append(val)
        except ValueError:
            continue
    return min(valid_prices) if valid_prices else None


def search_zoro(mpn: str) -> Optional[Dict[str, Any]]:
    if not mpn or len(mpn) < 3:
        return None
    url = f"https://www.zoro.com/search?q={urllib.parse.quote(mpn)}"
    headers = {"User-Agent": USER_AGENTS[0]}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            price = extract_price_from_text(r.text)
            if price:
                return {"vendor": "Zoro Industrial", "unit_cost": price, "url": url}
    except Exception:
        pass
    return None


def search_midland(mpn: str) -> Optional[Dict[str, Any]]:
    if not mpn or len(mpn) < 3:
        return None
    url = f"https://www.midlandmetal.com/search?searchKeyword={urllib.parse.quote(mpn)}"
    headers = {"User-Agent": USER_AGENTS[0]}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            price = extract_price_from_text(r.text)
            if price:
                return {"vendor": "Midland Metal", "unit_cost": price, "url": url}
    except Exception:
        pass
    return None


def probe_web_for_wholesale_cost(mpn: str, brand: str) -> Optional[Dict[str, Any]]:
    res = search_midland(mpn)
    if res:
        return res
    res = search_zoro(mpn)
    if res:
        return res
    return None


def broadcast_scout_deal(deal: Dict[str, Any]):
    if BUY_LIST_WEBHOOK_URL:
        try:
            payload = {
                "action": "ADD_BUY_LEAD",
                "asin": deal["asin"],
                "title": deal["title"],
                "brand": deal["brand"],
                "pack_count": deal["pack_count"],
                "category": "Industrial & Scientific",
                "sales_rank": deal["sales_rank"],
                "monthly_velocity": deal["monthly_sold"],
                "deal_rating": min(99, int(70 + (deal["roi_percent"] / 20.0))),
                "buy_box_price": deal["buy_box_price"],
                "wholesale_cost": deal["wholesale_cost"],
                "net_profit": deal["net_profit"],
                "roi_percent": deal["roi_percent"],
                "suggested_order_size": max(10, int(deal["monthly_sold"] * 0.5)),
                "profit_per_dollar": round(deal["net_profit"] / deal["wholesale_cost"], 2),
                "expected_order_profit": round(deal["net_profit"] * max(10, int(deal["monthly_sold"] * 0.5)), 2),
                "vendor": deal["vendor"],
                "is_hazmat": "NO",
                "order_url": f"https://www.amazon.com/dp/{deal['asin']}",
                "notes": f"[WEB_SCOUT] Found via {deal['vendor']} ({deal['monthly_sold']}/mo) - {deal['source_url']}",
                "status": "Active Target"
            }
            requests.post(BUY_LIST_WEBHOOK_URL, json=payload, timeout=30)
        except Exception as e:
            logger.error(f"Failed posting to sheets: {e}")

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            msg = (
                f"🌐 <b>WEB SCOUT HIGH-VELOCITY DISCOVERY</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📦 <b>Product:</b> {deal['title'][:50]}\n"
                f"🏷 <b>Brand:</b> <code>{deal['brand']}</code> | <b>ASIN:</b> <code>{deal['asin']}</code>\n"
                f"📈 <b>Demand:</b> #{deal['sales_rank']:,} | <b>{deal['monthly_sold']} units/mo</b>\n"
                f"💰 <b>Buy Box:</b> ${deal['buy_box_price']:.2f}\n"
                f"💵 <b>Scouted Cost:</b> ${deal['wholesale_cost']:.2f} ({deal['vendor']})\n"
                f"🚀 <b>Net Profit:</b> ${deal['net_profit']:.2f} ({deal['roi_percent']}% ROI | Hurdle: {deal['roi_hurdle']}%)\n"
                f"🔗 <a href=\"{deal['source_url']}\">Supplier Source Page</a>\n"
                f"━━━━━━━━━━━━━━━━━━━━"
            )
            reply_markup = {
                "inline_keyboard": [
                    [
                        {"text": "✅ BUY LIST", "callback_data": f"BUYLIST:{deal['asin']}:{deal['brand']}"},
                        {"text": "🚫 BLOCK IP", "callback_data": f"BLOCKIP:{deal['asin']}:{deal['brand']}"}
                    ],
                    [
                        {"text": "⛔ PASS", "callback_data": f"PASS:{deal['asin']}"},
                        {"text": "🛒 AMAZON", "url": f"https://www.amazon.com/dp/{deal['asin']}"}
                    ]
                ]
            }
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML", "reply_markup": reply_markup},
                timeout=20
            )
        except Exception as e:
            logger.error(f"Telegram dispatch error: {e}")


def sweep_uncataloged_candidates():
    logger.info(f"Starting Web Scout sweep across uncataloged cache (>= {MIN_MONTHLY_VELOCITY} units/mo)...")
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT asin, title, brand, mpn, avg90_bb, fba_fee, referral_pct, weight_lbs, monthly_sold, sales_rank
        FROM candidate_cache
        WHERE status = 'NO_CATALOG_MATCH' 
          AND mpn IS NOT NULL AND mpn != ''
          AND monthly_sold >= ?
        ORDER BY monthly_sold DESC
        LIMIT 250
    """, (MIN_MONTHLY_VELOCITY,))
    rows = cur.fetchall()
    conn.close()

    logger.info(f"Targeting {len(rows)} verified high-velocity candidate(s) for web scouting.")

    for r in rows:
        mpn = str(r["mpn"]).strip()
        brand = str(r["brand"]).strip()
        asin = r["asin"]
        bb = r["avg90_bb"]
        monthly_units = r["monthly_sold"] or MIN_MONTHLY_VELOCITY

        match = probe_web_for_wholesale_cost(mpn, brand)
        if not match:
            time.sleep(1.0)
            continue

        cost = match["unit_cost"]
        ref_fee = round(bb * (r["referral_pct"] or 0.15), 2)
        overhead = ref_fee + (r["fba_fee"] or 5.0) + 0.35 + round((r["weight_lbs"] or 1.0) * 0.45, 2)
        net_profit = round(bb - overhead - cost, 2)
        roi = round((net_profit / cost) * 100.0, 1) if cost > 0 else 0.0

        # Calibrated hurdle: 58% if >= 500/mo, else 75%
        roi_hurdle = 58.0 if monthly_units >= HIGH_VELOCITY_THRESHOLD else 75.0

        if net_profit >= PROFIT_FLOOR and roi >= roi_hurdle:
            logger.info(f"PROFITABLE WEB DEAL: {asin} ({brand}) | Vol: {monthly_units}/mo | Net: ${net_profit} ({roi}% vs {roi_hurdle}%)")
            deal_data = {
                "asin": asin,
                "title": r["title"],
                "brand": brand,
                "pack_count": 1,
                "sales_rank": r["sales_rank"],
                "monthly_sold": monthly_units,
                "buy_box_price": bb,
                "wholesale_cost": cost,
                "net_profit": net_profit,
                "roi_percent": roi,
                "roi_hurdle": roi_hurdle,
                "vendor": match["vendor"],
                "source_url": match["url"]
            }
            broadcast_scout_deal(deal_data)

            conn = get_db_connection()
            conn.execute("UPDATE candidate_cache SET status = 'SCOUTED_DEAL' WHERE asin = ?", (asin,))
            conn.commit()
            conn.close()

        time.sleep(2.0)


if __name__ == "__main__":
    while True:
        try:
            sweep_uncataloged_candidates()
            time.sleep(300)
        except Exception as e:
            logger.error(f"Web scout loop error: {e}")
            time.sleep(60)
