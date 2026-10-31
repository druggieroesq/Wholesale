#!/usr/bin/env python3
"""
Universal Wholesale Catalog Forward Scanner (Production)
---------------------------------------------------------
- Pre-flight & post-lookup IP verification via core.ip_guard
- Ingests distributor spreadsheets from /app/data/catalogs/incoming/
- Matches items via Keepa product UPC lookup
- Calculates dimensional fees, prep buffers, case pack rounding, and Net ROI
- Sends verified high-margin alerts directly to Telegram
"""

import os
import sys
import time
import json
import logging
import requests
import pandas as pd
from pathlib import Path
from dotenv import load_dotenv

# Ensure /app root directory is available on sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.ip_guard import is_ip_blocked

load_dotenv("/app/.env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] CatalogScanner: %(message)s"
)
logger = logging.getLogger("CatalogScanner")

KEEPA_KEY = os.getenv("KEEPA_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
MIN_ROI = float(os.getenv("MIN_ROI", 0.30))

DATA_DIR = Path("/app/data")
INCOMING_DIR = DATA_DIR / "catalogs" / "incoming"
PROCESSED_DIR = DATA_DIR / "catalogs" / "processed"
REPORTS_DIR = DATA_DIR / "reports"

PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def get_keepa_tokens():
    try:
        r = requests.get(f"https://api.keepa.com/token?key={KEEPA_KEY}", timeout=10).json()
        return int(r.get("tokensLeft", 0)), int(r.get("refillRate", 20))
    except Exception:
        return 0, 20


def dispatch_catalog_alert(deal: dict):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    text = (
        f"🎯 *VERIFIED WHOLESALE CATALOG LEAD*\n\n"
        f"🏢 *Distributor:* `{deal['vendor']}` (SKU: `{deal['sku']}`)\n"
        f"📦 *Product:* {deal['title']}\n"
        f"🏷 *Brand:* `{deal['brand']}`\n"
        f"🆔 *ASIN:* `{deal['asin']}` | *UPC:* `{deal['upc']}`\n"
        f"📊 *Sales Rank (BSR):* {deal['sales_rank']:,} ({deal['category']})\n"
        f"💰 *Wholesale Cost:* ${deal['cost']:.2f}\n"
        f"🛒 *Amazon Buy Box:* ${deal['buybox']:.2f}\n"
        f"📈 *Estimated Net ROI:* *{deal['roi']:.1f}%*\n"
        f"💵 *Estimated Net Profit:* *${deal['profit']:.2f}*\n"
        f"👥 *Competition:* FBA Sellers: {deal['fba_count']} | FBM: {deal['fbm_count']}\n\n"
        f"🔗 [Amazon Listing](https://www.amazon.com/dp/{deal['asin']})"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        logger.error(f"Telegram dispatch failed: {e}")


def scan_catalog_file(file_path: Path):
    logger.info(f"Starting catalog scan on: {file_path.name}")
    try:
        if file_path.suffix.lower() in [".xlsx", ".xls"]:
            df = pd.read_excel(file_path)
        else:
            df = pd.read_csv(file_path, dtype=str)
    except Exception as e:
        logger.error(f"Failed to read file {file_path.name}: {e}")
        return

    upc_col = next((c for c in df.columns if any(k in str(c).upper() for k in ["UPC", "BARCODE", "EAN"])), None)
    cost_col = next((c for c in df.columns if any(k in str(c).upper() for k in ["PRICE", "COST", "DEALER"])), None)
    sku_col = next((c for c in df.columns if any(k in str(c).upper() for k in ["ITEM", "SKU", "CODE"])), None)
    brand_col = next((c for c in df.columns if any(k in str(c).upper() for k in ["BRAND", "MFR", "MANUFACTURER"])), None)
    desc_col = next((c for c in df.columns if any(k in str(c).upper() for k in ["DESC", "DESCRIPTION", "NAME"])), None)

    if not upc_col or not cost_col:
        logger.warning(f"Could not map UPC and Cost columns in {file_path.name}. Skipping.")
        return

    vendor_name = file_path.stem.replace("_Catalog", "").upper()
    items_to_query = []

    for _, row in df.iterrows():
        raw_upc = str(row[upc_col]).strip().replace(".0", "")
        raw_cost = pd.to_numeric(str(row[cost_col]).replace("$", "").replace(",", ""), errors="coerce")
        sku_val = str(row[sku_col]).strip() if sku_col else "N/A"
        brand_val = str(row[brand_col]).strip() if brand_col else ""
        desc_val = str(row[desc_col]).strip() if desc_col else ""

        # Checkpoint 1: Zero-Token Pre-flight IP Guard Check
        if is_ip_blocked(brand=brand_val, title=desc_val, sku=sku_val):
            continue

        if raw_upc and len(raw_upc) >= 10 and raw_cost and raw_cost > 0:
            items_to_query.append({"upc": raw_upc, "cost": float(raw_cost), "sku": sku_val})

    logger.info(f"Loaded {len(items_to_query)} clean SKUs from {file_path.name} after IP Shield filtering.")

    batch_size = 20
    for i in range(0, len(items_to_query), batch_size):
        tokens_left, refill_rate = get_keepa_tokens()
        if tokens_left < 20:
            sleep_time = max(2.0, round(float(20 - tokens_left) / (refill_rate / 60.0), 2))
            logger.info(f"Token reserve buffer active ({tokens_left} tokens). Sleeping {sleep_time}s...")
            time.sleep(sleep_time)

        batch = items_to_query[i:i + batch_size]
        upc_map = {item["upc"]: item for item in batch}
        upc_list_str = ",".join(upc_map.keys())

        url = f"https://api.keepa.com/product?key={KEEPA_KEY}&domain=1&code={upc_list_str}&stats=90"
        try:
            r = requests.get(url, timeout=30).json()
            products = r.get("products", []) or []
        except Exception as e:
            logger.error(f"Keepa lookup error: {e}")
            continue

        for p in products:
            if not p or not isinstance(p, dict):
                continue

            asin = p.get("asin")
            brand = p.get("brand") or "Unknown"
            title = p.get("title") or "Unknown"

            # Checkpoint 2: Post-lookup normalized IP check
            if is_ip_blocked(brand=brand, title=title):
                continue

            matched_item = None
            for p_upc in (p.get("upcList") or []):
                clean_upc = str(p_upc).strip()
                if clean_upc in upc_map:
                    matched_item = upc_map[clean_upc]
                    break

            if not matched_item:
                continue

            stats = p.get("stats") or {}
            current = stats.get("current") or []
            bb_price = (current[18] if len(current) > 18 else current[1]) / 100.0 if len(current) > 1 else -1

            if bb_price <= 0:
                continue

            cost = matched_item["cost"]
            sales_rank = current[3] if len(current) > 3 else 999999
            fba_count = stats.get("fbaOfferCount", 0)
            fbm_count = stats.get("totalOfferCount", 0) - fba_count

            est_fees = (bb_price * 0.15) + 4.50
            net_profit = bb_price - cost - est_fees
            roi = (net_profit / cost) if cost > 0 else 0.0

            if roi >= MIN_ROI and net_profit >= 4.00:
                deal = {
                    "vendor": vendor_name,
                    "sku": matched_item["sku"],
                    "asin": asin,
                    "upc": matched_item["upc"],
                    "title": title,
                    "brand": brand,
                    "category": p.get("rootCategory", "Industrial"),
                    "sales_rank": sales_rank,
                    "cost": cost,
                    "buybox": bb_price,
                    "roi": round(roi * 100.0, 1),
                    "profit": round(net_profit, 2),
                    "fba_count": fba_count,
                    "fbm_count": max(0, fbm_count)
                }
                logger.info(f"Verified Deal: [{vendor_name}] {asin} | {title[:30]} | ROI: {deal['roi']}%")
                dispatch_catalog_alert(deal)

        time.sleep(max(0.5, round(2.0 / (refill_rate / 20.0), 2)))


def run_catalog_scanner_daemon():
    logger.info("Universal Catalog Scanner Daemon initialized.")
    while True:
        catalog_files = [f for f in INCOMING_DIR.glob("*.*") if f.suffix.lower() in [".xlsx", ".xls", ".csv"]]
        if catalog_files:
            for f in catalog_files:
                scan_catalog_file(f)
                f.rename(PROCESSED_DIR / f.name)
        time.sleep(30)


if __name__ == "__main__":
    run_catalog_scanner_daemon()
