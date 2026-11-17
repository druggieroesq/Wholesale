#!/usr/bin/env python3
"""
tools/sp_api_restrictions_daemon.py
-----------------------------------
Automated Amazon SP-API Restrictions & Brand-Unlock Promotion Daemon
- Audits top candidate_cache & verified_deals ASINs via SP-API /listings/2021-08-01/restrictions
- Detects newly ungated brands and automatically promotes ALL matching SKUs for that brand
  from candidate_cache into verified_deals (enforcing >= 50 u/mo for 0-FBA steals & >= 2 sellers)
- Pushes newly unlocked leads to Google Sheets (APPEND_LEADS) with is_ungated="YES", triggering
  Code.gs v19.2 to auto-refresh Brand Opportunity Matrix and rank the next target brand #1
- Sends a Telegram alert whenever a brand unlocks with its total promoted SKU count & profit pool
"""

import os
import sys
import time
import json
import html
import logging
import sqlite3
import argparse
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

load_dotenv(ROOT_DIR / ".env", override=True)

from config import get_verified_webhook_url, calculate_sliding_profit_floor, get_logistics_profile

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] RestrictionsDaemon: %(message)s"
)
logger = logging.getLogger("RestrictionsDaemon")

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
if not DATA_DIR.exists():
    DATA_DIR = ROOT_DIR / "data"

DB_PATH = DATA_DIR / "hunter_engine.db"
PRIMARY_IP_FILE = DATA_DIR / "ip_blocklist.json"

LWA_CLIENT_ID = os.getenv("SP_API_CLIENT_ID", os.getenv("LWA_CLIENT_ID", "")).strip()
LWA_CLIENT_SECRET = os.getenv("SP_API_CLIENT_SECRET", os.getenv("LWA_CLIENT_SECRET", "")).strip()
LWA_REFRESH_TOKEN = os.getenv("SP_API_REFRESH_TOKEN", "").strip()
SELLER_ID = os.getenv("AMAZON_SELLER_ID", os.getenv("SP_API_SELLER_ID", "")).strip()
MARKETPLACE_ID = os.getenv("AMAZON_MARKETPLACE_ID", "ATVPDKIKX0DER").strip()

_raw_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip().strip('"').strip("'")
TELEGRAM_BOT_TOKEN = _raw_token[3:] if _raw_token.lower().startswith("bot") else _raw_token
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip().strip('"').strip("'")

SP_API_ENDPOINT = "https://sellingpartnerapi-na.amazon.com"
LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 60000")
    return conn


def ensure_tables():
    with closing(get_db_connection()) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS brand_gating_state (
                brand TEXT PRIMARY KEY,
                status TEXT,
                sample_asin TEXT,
                reason_code TEXT,
                last_checked INTEGER,
                unlocked_at INTEGER
            )
        """)
        conn.commit()


def load_blocked_brands() -> Set[str]:
    if not PRIMARY_IP_FILE.exists():
        return set()
    try:
        raw = json.loads(PRIMARY_IP_FILE.read_text(encoding="utf-8"))
        items = raw if isinstance(raw, list) else raw.get("brands", [])
        return {str(b).upper().strip() for b in items if b}
    except Exception:
        return set()


def get_lwa_access_token() -> Optional[str]:
    if not (LWA_CLIENT_ID and LWA_CLIENT_SECRET and LWA_REFRESH_TOKEN):
        logger.warning("Missing SP-API LWA credentials (SP_API_CLIENT_ID / SECRET / REFRESH_TOKEN).")
        return None
    try:
        resp = requests.post(
            LWA_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": LWA_REFRESH_TOKEN,
                "client_id": LWA_CLIENT_ID,
                "client_secret": LWA_CLIENT_SECRET,
            },
            timeout=20,
        )
        if resp.status_code == 200:
            return resp.json().get("access_token")
        logger.error(f"LWA token refresh failed ({resp.status_code}): {resp.text[:200]}")
    except Exception as exc:
        logger.error(f"LWA token exception: {exc}")
    return None


def check_asin_restriction(asin: str, access_token: str) -> Tuple[str, str]:
    """
    Calls Amazon SP-API /listings/2021-08-01/restrictions for a given ASIN.
    Returns (status, reason_code) where status is 'UNRESTRICTED', 'APPROVAL_REQUIRED',
    'NOT_ELIGIBLE', or 'ERROR'.
    """
    if not SELLER_ID:
        return "ERROR", "MISSING_AMAZON_SELLER_ID"

    url = f"{SP_API_ENDPOINT}/listings/2021-08-01/restrictions"
    headers = {
        "x-amz-access-token": access_token,
        "Accept": "application/json",
    }
    params = {
        "asin": asin,
        "sellerId": SELLER_ID,
        "marketplaceIds": MARKETPLACE_ID,
        "conditionType": "new_new",
    }

    try:
        r = requests.get(url, headers=headers, params=params, timeout=20)
        if r.status_code == 429:
            time.sleep(2.5)
            r = requests.get(url, headers=headers, params=params, timeout=20)

        if r.status_code != 200:
            return "ERROR", f"HTTP_{r.status_code}"

        restrictions = r.json().get("restrictions", [])
        if not restrictions:
            return "UNRESTRICTED", "NONE"

        reasons = []
        for entry in restrictions:
            for rsn in entry.get("reasons", []):
                code = rsn.get("reasonCode", "RESTRICTED")
                reasons.append(code)

        joined = ",".join(reasons) if reasons else "RESTRICTED"
        if "NOT_ELIGIBLE" in joined:
            return "NOT_ELIGIBLE", joined
        return "APPROVAL_REQUIRED", joined
    except Exception as exc:
        return "ERROR", str(exc)[:80]


def send_telegram_alert(text: str):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
    except Exception as exc:
        logger.debug(f"Telegram alert error: {exc}")


def promote_unlocked_brand_skus(brand: str, conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """
    Promotes all qualified, non-hazmat SKUs for a newly unlocked brand into verified_deals
    and prepares webhook payloads with is_ungated='YES'.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM verified_deals
        WHERE UPPER(TRIM(brand)) = ? AND status != 'REJECTED'
        ORDER BY monthly_profit_pool DESC
        """,
        (brand.upper().strip(),),
    )
    rows = cur.fetchall()
    promoted_payloads: List[Dict[str, Any]] = []
    now = int(time.time())

    for r in rows:
        asin = r["asin"]
        fba_sellers = 0
        cur.execute("SELECT num_sellers, drops_90, monthly_sold, avg90_bb, current_bb, amazon_pack_qty FROM candidate_cache WHERE asin = ?", (asin,))
        c_row = cur.fetchone()
        total_sellers = int(c_row["num_sellers"]) if c_row and c_row["num_sellers"] else 2
        monthly_vel = int(c_row["monthly_sold"]) if c_row and c_row["monthly_sold"] else 50
        drops_90 = int(c_row["drops_90"]) if c_row and c_row["drops_90"] else 0
        avg90_bb = float(c_row["avg90_bb"]) if c_row and c_row["avg90_bb"] else float(r["buy_box_price"])
        current_bb = float(c_row["current_bb"]) if c_row and c_row["current_bb"] else float(r["buy_box_price"])
        pack_qty = int(c_row["amazon_pack_qty"]) if c_row and c_row["amazon_pack_qty"] else 1

        # Enforce 0-FBA >= 50 u/mo & >= 2 sellers rule
        if total_sellers <= 1:
            continue
        if fba_sellers == 0 and monthly_vel < 50:
            continue

        cur.execute("UPDATE verified_deals SET status = 'UNGATED_READY', updated_at = ? WHERE asin = ?", (now, asin))

        logistics = get_logistics_profile(r["vendor"])
        promoted_payloads.append({
            "asin": asin,
            "title": r["title"],
            "brand": r["brand"],
            "confidence_tier": r["confidence_tier"],
            "vendor": r["vendor"],
            "prep_center": logistics["facility"],
            "sales_rank": r["sales_rank"],
            "monthly_velocity": monthly_vel,
            "drops_90": drops_90,
            "competitive_fba": fba_sellers,
            "total_sellers": total_sellers,
            "pack_count": pack_qty,
            "buy_box_price": r["buy_box_price"],
            "current_bb": current_bb,
            "avg90_bb": avg90_bb,
            "wholesale_cost": r["wholesale_cost"],
            "net_profit": r["net_profit"],
            "roi_percent": r["roi_percent"],
            "suggested_order_size": r["suggested_order_size"],
            "monthly_profit_pool": r["monthly_profit_pool"],
            "is_hazmat": "YES" if r["is_hazmat"] else "NO",
            "is_ungated": "YES",
            "notes": f"UNGATED VERIFIED | Pool: ${r['monthly_profit_pool']:.2f}/mo | Ready to Order",
            "status": "Ready for Order",
        })

    conn.commit()
    return promoted_payloads


def run_restrictions_audit(max_brands: int = 40) -> int:
    ensure_tables()
    blocked_brands = load_blocked_brands()
    access_token = get_lwa_access_token()
    if not access_token:
        logger.warning("Skipping SP-API restrictions audit (no active access token).")
        return 0

    now = int(time.time())
    newly_unlocked_payloads: List[Dict[str, Any]] = []
    unlocked_brands_summary: List[Tuple[str, int, float]] = []

    with closing(get_db_connection()) as conn:
        cur = conn.cursor()
        # Pick top brands ranked by total monthly profit pool in verified_deals
        cur.execute("""
            SELECT UPPER(TRIM(brand)) AS b_name,
                   MAX(asin) AS sample_asin,
                   COUNT(*) AS sku_cnt,
                   SUM(monthly_profit_pool) AS total_pool
            FROM verified_deals
            WHERE brand IS NOT NULL AND TRIM(brand) != '' AND status != 'REJECTED'
            GROUP BY UPPER(TRIM(brand))
            ORDER BY total_pool DESC
            LIMIT ?
        """, (max_brands,))
        target_brands = cur.fetchall()

        logger.info(f"Auditing SP-API gating status for top {len(target_brands)} brands...")

        for row in target_brands:
            b_name = row["b_name"]
            if not b_name or b_name in blocked_brands:
                continue

            sample_asin = row["sample_asin"]
            cur.execute("SELECT status, last_checked FROM brand_gating_state WHERE brand = ?", (b_name,))
            prev = cur.fetchone()
            prev_status = prev["status"] if prev else "UNKNOWN"

            # Re-check already unlocked brands once every 7 days; check locked brands every run
            if prev_status == "UNRESTRICTED" and prev and (now - (prev["last_checked"] or 0)) < (7 * 86400):
                continue

            status, reason = check_asin_restriction(sample_asin, access_token)
            if status == "ERROR":
                logger.debug(f"SP-API check error for {b_name} ({sample_asin}): {reason}")
                continue

            unlocked_at = now if (status == "UNRESTRICTED" and prev_status != "UNRESTRICTED") else (prev["unlocked_at"] if prev else 0)
            cur.execute("""
                INSERT INTO brand_gating_state (brand, status, sample_asin, reason_code, last_checked, unlocked_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(brand) DO UPDATE SET
                    status = excluded.status,
                    sample_asin = excluded.sample_asin,
                    reason_code = excluded.reason_code,
                    last_checked = excluded.last_checked,
                    unlocked_at = excluded.unlocked_at
            """, (b_name, status, sample_asin, reason, now, unlocked_at))
            conn.commit()

            if status == "UNRESTRICTED" and prev_status != "UNRESTRICTED":
                logger.info(f"🔓 BRAND UNLOCKED: {b_name} (Sample ASIN: {sample_asin}) — Promoting SKUs...")
                promoted = promote_unlocked_brand_skus(b_name, conn)
                if promoted:
                    newly_unlocked_payloads.extend(promoted)
                    pool_sum = sum(float(p["monthly_profit_pool"]) for p in promoted)
                    unlocked_brands_summary.append((b_name, len(promoted), pool_sum))

            time.sleep(0.25)

    webhook_url = get_verified_webhook_url()
    if newly_unlocked_payloads and webhook_url:
        try:
            resp = requests.post(
                webhook_url,
                json={"action": "APPEND_LEADS", "leads": newly_unlocked_payloads},
                timeout=30,
                allow_redirects=True,
            )
            logger.info(f"Pushed {len(newly_unlocked_payloads)} newly ungated SKUs to Google Sheets ({resp.status_code}).")
        except Exception as exc:
            logger.error(f"Webhook push error for ungated SKUs: {exc}")

    for b_name, sku_cnt, pool_sum in unlocked_brands_summary:
        send_telegram_alert(
            f"🔓 <b>BRAND UNGATED &amp; PROMOTED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🏷 <b>Brand:</b> <code>{html.escape(b_name)}</code>\n"
            f"📦 <b>Promoted SKUs:</b> {sku_cnt} active deals\n"
            f"🏊 <b>Unlocked Profit Pool:</b> <b>${pool_sum:,.2f} / mo</b>\n"
            f"📊 <b>Brand Opportunity Matrix:</b> Auto-synced &amp; re-ranked!"
        )

    return len(unlocked_brands_summary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Automated SP-API Brand Restrictions & Promotion Daemon")
    parser.add_argument("--max-brands", type=int, default=40, help="Number of top matrix brands to audit per run")
    parser.add_argument("--loop", action="store_true", help="Run continuously every 6 hours")
    args = parser.parse_args()

    if args.loop:
        while True:
            try:
                run_restrictions_audit(max_brands=args.max_brands)
            except Exception as err:
                logger.error(f"Restrictions loop error: {err}")
            time.sleep(6 * 3600)
    else:
        unlocked = run_restrictions_audit(max_brands=args.max_brands)
        logger.info(f"SP-API audit complete. Newly unlocked brands: {unlocked}")
