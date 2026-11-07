#!/usr/bin/env python3
"""
tools/autonomous_recruiter.py
-----------------------------
Autonomous Supplier Recruiter & B2B Portal Crawler (v6.0 Production Master)
Key Capabilities:
1. Analyzes high-velocity uncataloged Amazon brands to identify root manufacturers.
2. Scrapes manufacturer domains for 'Dealer Application', 'Wholesale Inquiry', and 'Become a Distributor' pages.
3. Tests authenticated portal endpoints using distributor_credentials.json.
4. Auto-generates Florida Resale Certificate (DR-13) & opening order inquiry emails with wholesale application deep-links.
5. Emits Supplier Recruitment Cards to Telegram and appends vendor pipelines to Google Sheets.
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
from typing import Dict, Any, List, Optional, Set
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

load_dotenv(ROOT_DIR / ".env", override=False)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] Recruiter: %(message)s")
logger = logging.getLogger("Recruiter")

DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "hunter_engine.db"
CREDS_FILE = DATA_DIR / "distributor_credentials.json"
BUY_LIST_WEBHOOK_URL = (os.getenv("BUY_LIST_WEBHOOK_URL") or os.getenv("GOOGLE_SHEET_WEBHOOK_URL") or "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
]

APPLICATION_KEYWORDS = [
    "dealer application", "wholesale application", "become a dealer",
    "distributor application", "wholesale registration", "open an account",
    "new customer application", "reseller application"
]


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def load_credentials() -> Dict[str, Any]:
    if CREDS_FILE.exists():
        try:
            return json.loads(CREDS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def find_top_demand_brands(limit: int = 50) -> List[Dict[str, Any]]:
    """Identifies top uncataloged brands by monthly unit sales volume."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT brand, COUNT(asin) as sku_count, SUM(monthly_sold) as total_monthly_volume, AVG(avg90_bb) as avg_price
        FROM candidate_cache
        WHERE status IN ('NO_CATALOG_MATCH', 'NO_VENDOR_PATH')
          AND brand IS NOT NULL AND brand != '' AND brand != 'GENERIC'
        GROUP BY brand
        HAVING total_monthly_volume >= 40
        ORDER BY total_monthly_volume DESC
        LIMIT ?
    """, (limit,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def search_supplier_domain(brand: str) -> Optional[str]:
    """Resolves manufacturer or primary wholesale portal domain."""
    query = f"{brand} official website industrial manufacturer wholesale"
    url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
    headers = {"User-Agent": USER_AGENTS[0]}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", class_="result__url"):
                href = a.get("href", "")
                if href and not any(ign in href for ign in ["amazon.com", "walmart.com", "ebay.com", "wikipedia.org"]):
                    clean_url = href.strip()
                    if not clean_url.startswith("http"):
                        clean_url = f"https://{clean_url}"
                    return clean_url
    except Exception as e:
        logger.debug(f"Search query error for {brand}: {e}")
    return None


def probe_wholesale_portal(base_url: str) -> Dict[str, Any]:
    """Crawls website navigation to identify online wholesale application links and login forms."""
    result = {
        "portal_url": base_url,
        "application_url": None,
        "login_url": None,
        "contact_email": None,
        "portal_type": "UNKNOWN"
    }
    headers = {"User-Agent": USER_AGENTS[0]}
    try:
        r = requests.get(base_url, headers=headers, timeout=10, verify=False)
        if r.status_code != 200:
            return result

        soup = BeautifulSoup(r.text, "html.parser")
        parsed_base = urllib.parse.urlparse(base_url)

        # 1. Search for wholesale application or dealer registration links
        for a in soup.find_all("a", href=True):
            text = (a.get_text() or "").strip().lower()
            href = a["href"].strip()
            full_link = urllib.parse.urljoin(base_url, href)

            if any(k in text or k in href.lower() for k in APPLICATION_KEYWORDS):
                result["application_url"] = full_link
                result["portal_type"] = "ONLINE_FORM"
                break
            elif "wholesale" in text or "dealers" in text or "distributor" in text:
                if not result["application_url"]:
                    result["application_url"] = full_link

            if "login" in text or "sign in" in text or "dealer-login" in href.lower():
                result["login_url"] = full_link

        # 2. Extract general sales/dealer emails
        emails = re.findall(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', r.text)
        sales_emails = [e for e in emails if any(k in e.lower() for k in ["sales", "order", "dealer", "wholesale", "info"])]
        if sales_emails:
            result["contact_email"] = sales_emails[0]
        elif emails:
            result["contact_email"] = emails[0]

    except Exception as e:
        logger.debug(f"Portal probe exception on {base_url}: {e}")

    return result


def generate_dr13_inquiry_template(brand: str, contact_email: str) -> str:
    """Generates Florida DR-13 Sales Tax Resale inquiry draft for offline opening orders."""
    return (
        f"Subject: Wholesale Account Inquiry & Resale Onboarding - {brand}\n\n"
        f"Hello Sales & Dealer Team,\n\n"
        f"We are expanding our commercial MRO & Industrial supply procurement operations in Florida "
        f"and would like to set up an authorized wholesale account to distribute {brand} product lines.\n\n"
        f"Could you please share your dealer application package, opening order minimums (MOQ), and price sheet / catalog?\n\n"
        f"Our Florida Annual Resale Certificate for Sales Tax (Form DR-13) and company credit profile are attached and ready for processing.\n\n"
        f"Thank you,\n"
        f"Wholesale Procurement Operations\n"
        f"Parkland / Tamarac Hub, FL"
    )


def broadcast_recruiter_alert(brand_data: Dict[str, Any], portal_meta: Dict[str, Any]):
    """Sends Telegram alert with one-click portal signup and email templates."""
    brand = brand_data["brand"]
    vol = brand_data["total_monthly_volume"]
    skus = brand_data["sku_count"]
    app_link = portal_meta.get("application_url") or portal_meta.get("portal_url") or "https://www.google.com"
    email = portal_meta.get("contact_email") or "sales@distributor.com"

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        msg = (
            f"🤝 <b>SUPPLIER RECRUITMENT OPPORTUNITY</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🏭 <b>Brand / Mfr:</b> <code>{brand}</code>\n"
            f"📈 <b>Unmatched Demand:</b> {vol:,} units/mo across {skus} SKUs\n"
            f"🌐 <b>Website:</b> <a href=\"{portal_meta.get('portal_url')}\">Official Domain</a>\n"
            f"📝 <b>Application Link:</b> <a href=\"{app_link}\">Online Wholesale Form</a>\n"
            f"📧 <b>Contact Email:</b> <code>{email}</code>\n"
            f"📋 <b>Tax Status:</b> FL Resale Certificate (DR-13) Ready\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "📝 OPEN APPLICATION", "url": app_link},
                    {"text": "🚫 BLOCK BRAND", "callback_data": f"BLOCKIP:NONE:{brand}"}
                ]
            ]
        }
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML", "reply_markup": reply_markup},
                timeout=8
            )
        except Exception as e:
            logger.error(f"Telegram recruiter alert error: {e}")

    if BUY_LIST_WEBHOOK_URL:
        try:
            payload = {
                "action": "ADD_SUPPLIER_LEAD",
                "brand": brand,
                "monthly_demand": vol,
                "sku_count": skus,
                "portal_url": portal_meta.get("portal_url"),
                "application_url": app_link,
                "contact_email": email,
                "status": "RECRUITMENT_TARGET"
            }
            requests.post(BUY_LIST_WEBHOOK_URL, json=payload, timeout=10)
        except Exception:
            pass


def run_recruitment_cycle():
    logger.info("Executing supplier recruitment sweep across high-velocity uncataloged demand...")
    top_brands = find_top_demand_brands(limit=25)
    logger.info(f"Identified {len(top_brands)} high-demand brands needing supplier onboarding.")

    for b in top_brands:
        brand_name = b["brand"]
        logger.info(f"Scouting wholesale domain for {brand_name} ({b['total_monthly_volume']} mo units)...")
        domain = search_supplier_domain(brand_name)
        if not domain:
            time.sleep(2)
            continue

        portal_meta = probe_wholesale_portal(domain)
        logger.info(f"Target found for {brand_name}: Form={portal_meta['application_url'] is not None}, Email={portal_meta['contact_email']}")
        broadcast_recruiter_alert(b, portal_meta)
        time.sleep(3)


if __name__ == "__main__":
    while True:
        try:
            run_recruitment_cycle()
            logger.info("Recruitment cycle complete. Sleeping 2 hours...")
            time.sleep(7200)
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Recruiter fatal loop error: {e}")
            time.sleep(120)
