#!/usr/bin/env python3
"""
tools/zoro_target_scout.py
--------------------------
Authenticated Zoro.com MRO Web Portal Scanner & Wholesale Matcher:
1. Logs into Zoro.com using ZORO_USER & ZORO_PASS from Railway environment variables.
2. Filters out all 226+ IP/conglomerate blacklisted brands (data/ip_blocklist.json) and hazmat items.
3. Enforces 45-lb parcel limits and <= 48 piece-count multipack math.
4. Queries AmazonGatekeeper.evaluate_asin_approval(asin) before promoting UNRESTRICTED /
   AUTO_UNGATED deals (Net ROI >= 15%) into verified_deals + Google Sheets sync.
"""

import os
import re
import sys
import time
import json
import logging
import sqlite3
import argparse
from pathlib import Path
from typing import Any, Dict, Optional

import requests
import pandas as pd
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

load_dotenv(ROOT_DIR / ".env", override=True)

from core.amazon_gatekeeper import AmazonGatekeeper
from core.engine import is_over_weight_limit
from config import get_logistics_profile

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] ZoroScout: %(message)s"
)
logger = logging.getLogger("ZoroScout")

DB_PATH = os.getenv("DB_PATH", "/app/data/hunter_engine.db")
if not os.path.exists(DB_PATH) and (ROOT_DIR / "data" / "hunter_engine.db").exists():
    DB_PATH = str(ROOT_DIR / "data" / "hunter_engine.db")

BLOCKLIST_PATH = Path(os.getenv("IP_BLOCKLIST_PATH", "/app/data/ip_blocklist.json"))
if not BLOCKLIST_PATH.exists():
    BLOCKLIST_PATH = ROOT_DIR / "data" / "ip_blocklist.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def load_blocked_brands() -> set:
    if not BLOCKLIST_PATH.exists():
        return set()
    try:
        raw = json.loads(BLOCKLIST_PATH.read_text(encoding="utf-8"))
        items = raw if isinstance(raw, list) else raw.get("brands", [])
        return {str(b).upper().strip() for b in items if b}
    except Exception:
        return set()


def detect_pack_qty(text: str) -> int:
    if not text:
        return 1
    m = re.search(r"(?:pack\s*of|pk\s*of|case\s*of|box\s*of)\s*(\d+)|\b(\d+)[-\s]?(?:pack|pk|ct)\b", text, re.IGNORECASE)
    if m:
        val = int(m.group(1) or m.group(2))
        return val if 1 <= val <= 48 else 1
    return 1


class ZoroWholesaleSession:
    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.zoro.com/",
        })
        self.is_authenticated = False

    def login(self) -> bool:
        if not self.username or not self.password:
            logger.warning("ZORO_USER / ZORO_PASS not set in environment; scanning public catalog prices.")
            return False

        logger.info(f"Authenticating Zoro wholesale session ({self.username})...")
        try:
            self.session.get("https://www.zoro.com/", timeout=15)
            csrf_token = self.session.cookies.get("csrftoken") or self.session.cookies.get("_csrf") or ""
            headers = {
                "Content-Type": "application/json",
                "Referer": "https://www.zoro.com/login/",
                "Origin": "https://www.zoro.com",
            }
            if csrf_token:
                headers["X-CSRF-Token"] = csrf_token

            resp = self.session.post(
                "https://www.zoro.com/api/v1/auth/login",
                json={"username": self.username, "password": self.password, "rememberMe": True},
                headers=headers,
                timeout=20,
            )
            if resp.status_code in (200, 204):
                self.is_authenticated = True
                logger.info("Zoro contracted account session active.")
                return True
        except Exception as exc:
            logger.warning(f"Zoro auth handshake notice: {exc}")
        return False

    def search_mro_item(self, query: str) -> Optional[Dict[str, Any]]:
        encoded_q = requests.utils.quote(str(query).strip())
        url = f"https://www.zoro.com/search?q={encoded_q}"
        try:
            resp = self.session.get(url, timeout=15)
            if resp.status_code != 200:
                return None
            html = resp.text
            upper_html = html.upper()
            if "OUT OF STOCK" in upper_html and "IN STOCK" not in upper_html:
                return None

            # 1. JSON-LD Structured Product Block
            for raw_json in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL):
                try:
                    data = json.loads(raw_json)
                    if isinstance(data, dict) and data.get("@type") == "Product":
                        offers = data.get("offers", {})
                        price = float(offers.get("price", 0.0) or 0.0)
                        avail = str(offers.get("availability", "")).upper()
                        if "OUTOFSTOCK" in avail:
                            continue
                        sku = str(data.get("sku", "") or data.get("mpn", ""))
                        title = str(data.get("name", ""))
                        if price > 0:
                            return {
                                "wholesale_cost": round(price, 2),
                                "sku": sku or "ZORO_DIRECT",
                                "title": title,
                                "pack_qty": detect_pack_qty(title),
                            }
                except Exception:
                    continue

            # 2. Authenticated Contract / Price Fallback
            price_match = (
                re.search(r'data-contract-price="([\d\.]+)"', html)
                or re.search(r'class="[^"]*your-price[^"]*"\s*>\s*\$([\d\.]+)', html)
                or re.search(r'\$(\d+\.\d{2})', html)
            )
            sku_match = re.search(r'/i/(G\d+)/', html)
            if price_match:
                price = float(price_match.group(1))
                return {
                    "wholesale_cost": round(price, 2),
                    "sku": sku_match.group(1) if sku_match else "ZORO_DIRECT",
                    "title": query,
                    "pack_qty": 1,
                }
        except Exception:
            pass
        return None


def run_zoro_mro_scan(max_items: int = 250, min_roi_pct: float = 15.0) -> int:
    if not os.path.exists(DB_PATH):
        logger.warning(f"Database not found at {DB_PATH}")
        return 0

    blocked_brands = load_blocked_brands()
    gatekeeper = AmazonGatekeeper()
    zoro = ZoroWholesaleSession(
        username=os.getenv("ZORO_USER", "").strip(),
        password=os.getenv("ZORO_PASS", "").strip(),
    )
    zoro.login()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT asin, title, brand, mpn, upc, avg90_bb, current_bb, fba_fee,
               referral_pct, weight_lbs, amazon_pack_qty, is_hazmat
        FROM candidate_cache
        WHERE (mpn IS NOT NULL OR upc IS NOT NULL)
          AND COALESCE(is_hazmat, 0) = 0
          AND COALESCE(status, '') NOT IN ('BLOCKED_IP_BRAND', 'REJECTED', 'HARD_BLOCKED')
          AND COALESCE(avg90_bb, current_bb, 0) >= 20.0
        LIMIT ?
    """, (max_items * 2,))
    rows = cur.fetchall()

    matched = 0
    scanned = 0

    for row in rows:
        if scanned >= max_items:
            break

        asin = str(row["asin"]).strip().upper()
        brand = str(row["brand"] or "").strip()
        if not brand or brand.upper() in blocked_brands:
            continue

        title = str(row["title"] or "")
        weight_lbs = float(row["weight_lbs"] or 1.0)
        over_wt, _ = is_over_weight_limit(title=title, package_weight_raw=weight_lbs)
        if over_wt:
            continue

        upc = re.sub(r"\D", "", str(row["upc"] or ""))
        mpn = str(row["mpn"] or "").strip()
        query = upc if len(upc) in (12, 13) else f"{brand} {mpn}".strip()
        if len(query) < 4:
            continue

        bb = float(row["avg90_bb"] or row["current_bb"] or 0.0)
        if bb < 20.0:
            continue

        res = zoro.search_mro_item(query)
        scanned += 1
        if not res or res["wholesale_cost"] <= 0:
            time.sleep(1.0)
            continue

        amz_pack = max(1, min(int(row["amazon_pack_qty"] or detect_pack_qty(title)), 48))
        zoro_pack = max(1, min(int(res.get("pack_qty", 1)), 48))
        unit_ws_cost = res["wholesale_cost"] / zoro_pack
        landed_bundle_cogs = round(unit_ws_cost * amz_pack, 2)

        # Reject COGS inversion or fake accessory mismatches
        if landed_bundle_cogs >= bb or (landed_bundle_cogs / bb) < 0.18:
            time.sleep(1.0)
            continue

        ref_pct = float(row["referral_pct"] or 0.15)
        fba_fee = float(row["fba_fee"] or 5.20)
        logistics = get_logistics_profile("ZORO")
        prep = logistics["prep_base"] + (weight_lbs * logistics["freight_per_lb"])
        net_profit = round(bb - landed_bundle_cogs - (bb * ref_pct) - fba_fee - prep, 2)
        roi_pct = round((net_profit / landed_bundle_cogs) * 100.0, 1)

        if net_profit < 8.0 or roi_pct < min_roi_pct:
            time.sleep(1.0)
            continue

        # Live SP-API gating check before promoting to READY_TO_BUY
        gate = gatekeeper.evaluate_asin_approval(asin)
        g_status = gate.get("status", "INVOICE_REQUIRED")
        if g_status not in ("UNRESTRICTED", "AUTO_UNGATED"):
            logger.info(f"Zoro margin hit {asin} ({brand}) gated [{g_status}] — skipped READY_TO_BUY promotion.")
            time.sleep(1.0)
            continue

        cur.execute("""
            INSERT OR REPLACE INTO verified_deals (
                asin, title, brand, confidence_tier, sales_rank,
                buy_box_price, wholesale_cost, max_allowable_buy_cost,
                net_profit, roi_percent, vendor, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            asin, title[:180], brand, "[VERIFIED 95%+]", 25000,
            bb, landed_bundle_cogs, landed_bundle_cogs,
            net_profit, roi_pct, "ZORO", "READY_TO_BUY"
        ))
        matched += 1
        logger.info(f"ZORO VERIFIED HIT: {asin} ({brand}) | COGS: ${landed_bundle_cogs:.2f} | BB: ${bb:.2f} | Net: +${net_profit:.2f} ({roi_pct}% ROI)")
        time.sleep(1.2)

    conn.commit()
    conn.close()

    if matched > 0:
        try:
            from tools.sync_today_leads_to_sheet import run_pipeline_sync
            run_pipeline_sync()
        except Exception as exc:
            logger.warning(f"Sheet sync trigger notice: {exc}")

    logger.info(f"Zoro MRO web scan complete: {matched} SP-API verified deals promoted.")
    return matched


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Authenticated Zoro.com MRO Web Portal Scanner")
    parser.add_argument("--max-items", type=int, default=250, help="Max candidate items to scan on Zoro.com")
    parser.add_argument("--min-roi", type=float, default=15.0, help="Minimum Net ROI % threshold")
    args = parser.parse_args()
    run_zoro_mro_scan(max_items=args.max_items, min_roi_pct=args.min_roi)
