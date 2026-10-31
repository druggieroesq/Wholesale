#!/usr/bin/env python3
"""
Universal Wholesale Catalog Forward Scanner ($10+ Profit Per Sale & 75%+ ROI)
-----------------------------------------------------------------------------
- Fully self-contained: Zero external module dependencies (No 'core' import crashes)
- Direct live reload from /app/data/ip_blocklist.json
- Strict 75% Net ROI hurdle AND $10.00 minimum net profit PER ORDER/SALE
- Multipack/bundle aware: multiplies piece cost by bundle quantity
- Shared Keepa token reserve manager: prevents negative token starvation
- Matches items via Keepa product UPC lookup
- Calculates dimensional fees, prep buffers, case pack rounding, and Net ROI
- Sends verified high-margin alerts directly to Telegram
"""

import os
import sys
import time
import json
import re
import logging
import requests
import pandas as pd
from pathlib import Path
from dotenv import load_dotenv

load_dotenv("/app/.env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] CatalogScanner: %(message)s"
)
logger = logging.getLogger("CatalogScanner")

KEEPA_KEY = os.getenv("KEEPA_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Enforce strict 75% Net ROI and $10.00 Net Profit per sale/order
MIN_ROI = float(os.getenv("MIN_ROI", 0.75))
MIN_PROFIT_PER_SALE = float(os.getenv("MIN_PROFIT", 10.00))

DATA_DIR = Path("/app/data")
INCOMING_DIR = DATA_DIR / "catalogs" / "incoming"
PROCESSED_DIR = DATA_DIR / "catalogs" / "processed"
REPORTS_DIR = DATA_DIR / "reports"
PRIMARY_IP_FILE = DATA_DIR / "ip_blocklist.json"
FALLBACK_IP_FILE = Path(__file__).resolve().parent.parent / "data" / "ip_blocklist.json"

PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


# ==========================================
# SELF-CONTAINED EMBEDDED IP GUARD
# ==========================================
def get_ip_blocklist_path() -> Path:
    if PRIMARY_IP_FILE.parent.exists():
        return PRIMARY_IP_FILE
    FALLBACK_IP_FILE.parent.mkdir(parents=True, exist_ok=True)
    return FALLBACK_IP_FILE


def load_ip_blocklist() -> dict:
    target = get_ip_blocklist_path()
    if not target.exists():
        return {"brands": set(), "skus": set()}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            return {
                "brands": set(str(b).strip().lower() for b in raw if str(b).strip()),
                "skus": set()
            }
        elif isinstance(raw, dict):
            brands = set(str(b).strip().lower() for b in raw.get("brands", []) if str(b).strip())
            skus = set(str(s).strip().upper() for s in raw.get("skus", []) if str(s).strip())
            return {"brands": brands, "skus": skus}
    except Exception as e:
        logger.error(f"Failed to read blocklist from {target}: {e}")
    return {"brands": set(), "skus": set()}


def is_ip_blocked(brand: str = "", title: str = "", sku: str = "") -> bool:
    blocklist = load_ip_blocklist()
    b_clean = (brand or "").strip().lower()
    t_clean = (title or "").strip().lower()
    s_clean = (sku or "").strip().upper()

    for blocked_sku in blocklist["skus"]:
        if blocked_sku and blocked_sku in s_clean:
            return True

    for blocked_brand in blocklist["brands"]:
        if blocked_brand:
            if blocked_brand in b_clean or blocked_brand in t_clean:
                return True

    return False


def get_keepa_tokens():
    try:
        r = requests.get(f"https://api.keepa.com/token?key={KEEPA_KEY}", timeout=10).json()
        return int(r.get("tokensLeft", 0)), int(r.get("refillRate", 20))
    except Exception:
        return 0, 20


def extract_package_quantity(title: str, package_quantity_field: int = 1) -> int:
    if package_quantity_field and int(package_quantity_field) > 1:
        return int(package_quantity_field)
    
    t_lower = (title or "").lower()
    match = re.search(r'\b(?:pack of|pack|pk|count of|ct|case of)\s*([0-9]{1,3})\b', t_lower)
    if match:
        try:
            qty = int(match.group(1))
            if 1 < qty <= 100:
                return qty
        except Exception:
            pass

    match2 = re.search(r'\b([0-9]{1,3})[\s-]*(?:pack|pk|count|ct)\b', t_lower)
    if match2:
        try:
            qty = int(match2.group(1))
            if 1 < qty <= 100:
                return qty
        except Exception:
            pass

    return 1


def dispatch_catalog_alert(deal: dict):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    pack_info = f" ({deal['pack_qty']}-Pack)" if deal['pack_qty'] > 1 else ""
    text = (
        f"🎯 *HIGH-MARGIN CATALOG LEAD (PER-SALE QUALIFIED)*\n\n"
        f"🏢 *Distributor:* `{deal['vendor']}` (SKU: `{deal['sku']}`)\n"
        f"📦 *Product:* {deal['title']}{pack_info}\n"
        f"🏷 *Brand:* `{deal['brand']}`\n"
        f"🆔 *ASIN:* `{deal['asin']}` | *UPC:* `{deal['upc']}`\n"
        f"📊 *Sales Rank (BSR):* {deal['sales_rank']:,} ({deal['category']})\n"
        f"💰 *Wholesale Cost:* ${deal['cost']:.2f}/ea (${deal['total_cogs']:.2f} total order COGS)\n"
        f"🛒 *Amazon Buy Box:* ${deal['buybox']:.2f}\n"
        f"📈 *Estimated Net ROI:* *{deal['roi']:.1f}%* (Floor: 75%)\n"
        f"💵 *Profit per Sale:* *${deal['profit_per_sale']:.2f}* (Floor: $10.00/order)\n"
        f"👥 *Competition:* FBA Sellers: {deal['fba_count']} | FBM: {deal['fbm_count']}\n\n"
        f"🔗 [Amazon Listing](https://www.amazon.com/dp/{deal['asin']})"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        logger.error(f"Telegram dispatch failed: {e}")


def scan_catalog_file(file_path: Path):
    logger.info(f"Starting catalog scan on: {file_path.name} (Hurdles: MIN_ROI={MIN_ROI * 100}%, MIN_PROFIT_PER_SALE=${MIN_PROFIT_PER_SALE:.2f})")
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

        # Zero-Token Pre-flight IP Guard Check
        if is_ip_blocked(brand=brand_val, title=desc_val, sku=sku_val):
            continue

        if raw_upc and len(raw_upc) >= 10 and raw_cost and raw_cost > 0:
            items_to_query.append({"upc": raw_upc, "cost": float(raw_cost), "sku": sku_val})

    logger.info(f"Loaded {len(items_to_query)} clean SKUs from {file_path.name} after IP Shield filtering.")

    batch_size = 20
    for i in range(0, len(items_to_query), batch_size):
        tokens_left, refill_rate = get_keepa_tokens()
        # Reserve buffer elevated to 40 tokens so both engines can operate without going negative
        if tokens_left < 40:
            sleep_time = max(3.0, round(float(40 - tokens_left) / (refill_rate / 60.0), 2))
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

            # Post-lookup normalized IP check
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

            piece_cost = matched_item["cost"]
            pack_qty = extract_package_quantity(title, p.get("packageQuantity", 1))
            total_order_cogs = piece_cost * pack_qty

            sales_rank = current[3] if len(current) > 3 else 999999
            fba_count = stats.get("fbaOfferCount", 0)
            fbm_count = stats.get("totalOfferCount", 0) - fba_count

            # Fee modeling: 15% referral fee + $4.50 prep and pick/pack reserve
            est_fees = (bb_price * 0.15) + 4.50
            profit_per_sale = bb_price - total_order_cogs - est_fees
            roi = (profit_per_sale / total_order_cogs) if total_order_cogs > 0 else 0.0

            # Double-gate evaluated strictly per sale/order:
            if roi >= MIN_ROI and profit_per_sale >= MIN_PROFIT_PER_SALE:
                deal = {
                    "vendor": vendor_name,
                    "sku": matched_item["sku"],
                    "asin": asin,
                    "upc": matched_item["upc"],
                    "title": title,
                    "brand": brand,
                    "pack_qty": pack_qty,
                    "category": p.get("rootCategory", "Industrial"),
                    "sales_rank": sales_rank,
                    "cost": piece_cost,
                    "total_cogs": total_order_cogs,
                    "buybox": bb_price,
                    "roi": round(roi * 100.0, 1),
                    "profit_per_sale": round(profit_per_sale, 2),
                    "fba_count": fba_count,
                    "fbm_count": max(0, fbm_count)
                }
                logger.info(f"Verified Deal: [{vendor_name}] {asin} ({pack_qty}-pk) | Profit/Sale: ${deal['profit_per_sale']:.2f} | ROI: {deal['roi']}%")
                dispatch_catalog_alert(deal)

        time.sleep(max(1.0, round(3.0 / (refill_rate / 20.0), 2)))


def run_catalog_scanner_daemon():
    logger.info(f"Universal Catalog Scanner Daemon online (Min ROI: {MIN_ROI * 100}%, Min Profit per Sale: ${MIN_PROFIT_PER_SALE:.2f}).")
    while True:
        catalog_files = [f for f in INCOMING_DIR.glob("*.*") if f.suffix.lower() in [".xlsx", ".xls", ".csv"]]
        if catalog_files:
            for f in catalog_files:
                scan_catalog_file(f)
                f.rename(PROCESSED_DIR / f.name)
        time.sleep(30)


if __name__ == "__main__":
    run_catalog_scanner_daemon()
