#!/usr/bin/env python3
import os
import re
import sys
import time
import json
import logging
import argparse
import requests
import pandas as pd
from pathlib import Path
from dotenv import load_dotenv

load_dotenv("/app/.env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] ReverseHunter: %(message)s"
)
logger = logging.getLogger("ReverseHunter")

KEEPA_KEY = os.getenv("KEEPA_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

DATA_DIR = Path("/app/data")
REPORTS_DIR = DATA_DIR / "reports"
CATALOGS_DIR = DATA_DIR / "catalogs" / "incoming"
IP_BLOCKLIST_PATH = DATA_DIR / "ip_blocklist.json"

REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def load_ip_blocklist():
    blocked_brands = set()
    blocked_asins = set()
    if not IP_BLOCKLIST_PATH.exists():
        logger.warning(f"No IP blocklist found at {IP_BLOCKLIST_PATH}. Running without IP blocklist.")
        return blocked_brands, blocked_asins

    try:
        raw = json.loads(IP_BLOCKLIST_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            for item in raw:
                item_str = str(item).strip()
                if item_str.startswith("B0") and len(item_str) == 10:
                    blocked_asins.add(item_str.upper())
                else:
                    blocked_brands.add(item_str.lower())
        elif isinstance(raw, dict):
            for b in raw.get("brands", []):
                blocked_brands.add(str(b).strip().lower())
            for a in raw.get("asins", []):
                blocked_asins.add(str(a).strip().upper())
            for key, val in raw.items():
                if key not in ["brands", "asins"] and isinstance(val, (str, list, dict)):
                    blocked_brands.add(str(key).strip().lower())
        logger.info(f"Loaded IP Blocklist: {len(blocked_brands)} brands, {len(blocked_asins)} ASINs protected.")
    except Exception as e:
        logger.error(f"Failed to read {IP_BLOCKLIST_PATH}: {e}")

    return blocked_brands, blocked_asins


BLOCKED_BRANDS, BLOCKED_ASINS = load_ip_blocklist()


def is_catalog_scanner_running() -> bool:
    try:
        res = os.popen("pgrep -f universal_catalog_scanner.py").read().strip()
        return bool(res)
    except Exception:
        return False


def build_local_catalog_index():
    catalog_index = {}
    if not CATALOGS_DIR.exists():
        return catalog_index

    for file_path in CATALOGS_DIR.glob("*.*"):
        if file_path.suffix.lower() not in [".xlsx", ".xls", ".csv"]:
            continue
        try:
            if file_path.suffix.lower() in [".xlsx", ".xls"]:
                df = pd.read_excel(file_path, nrows=50000)
            else:
                df = pd.read_csv(file_path, nrows=50000, dtype=str)

            upc_col = next((c for c in df.columns if any(k in str(c).upper() for k in ["UPC", "BARCODE", "EAN"])), None)
            cost_col = next((c for c in df.columns if any(k in str(c).upper() for k in ["PRICE", "COST", "DEALER"])), None)
            sku_col = next((c for c in df.columns if any(k in str(c).upper() for k in ["ITEM", "SKU", "CODE"])), None)

            if upc_col and cost_col:
                vendor_name = file_path.stem.replace("_Catalog", "").upper()
                for _, row in df.iterrows():
                    raw_upc = str(row[upc_col]).strip().replace(".0", "")
                    raw_cost = pd.to_numeric(str(row[cost_col]).replace("$", "").replace(",", ""), errors="coerce")
                    if raw_upc and raw_cost and raw_cost > 0:
                        catalog_index[raw_upc] = {
                            "vendor": vendor_name,
                            "sku": str(row[sku_col]) if sku_col else "N/A",
                            "cost": float(raw_cost)
                        }
        except Exception as e:
            logger.warning(f"Could not index {file_path.name}: {e}")

    logger.info(f"Indexed {len(catalog_index)} UPCs across incoming distributor catalogs.")
    return catalog_index


CATALOG_INDEX = build_local_catalog_index()


def dispatch_telegram_reverse_alert(deal: dict):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    matched_text = (
        f"🏷 *Internal Match:* `{deal.get('matched_vendor')}` (SKU: `{deal.get('matched_sku')}` | Cost: ${deal.get('matched_cost'):.2f})\n"
        f"📈 *Estimated ROI:* *{deal.get('est_roi'):.1f}%*\n"
    ) if deal.get("matched_vendor") else "🔍 *Internal Match:* None (Source Online / Distributor Hunt)\n"

    text = (
        f"🎯 *PRIME MONOPOLIST TARGET (0 FBA)*\n\n"
        f"📦 *Product:* {deal.get('title')}\n"
        f"🏷 *Brand:* `{deal.get('brand')}`\n"
        f"🆔 *ASIN:* `{deal.get('asin')}`\n"
        f"📊 *Sales Rank (BSR):* {deal.get('sales_rank'):,}\n"
        f"🛒 *FBM Floor Price:* ${deal.get('fbm_price'):.2f}\n"
        f"🎯 *Target Prime Price (10% markup):* ${deal.get('target_prime_price'):.2f}\n"
        f"👥 *Active Sellers:* {deal.get('seller_count')} (FBA: 0 | FBM: {deal.get('seller_count')})\n\n"
        f"{matched_text}\n"
        f"🔗 [Amazon Listing](https://www.amazon.com/dp/{deal.get('asin')}) | "
        f"[Google Sourcing Search](https://www.google.com/search?q={deal.get('upc_or_title_query')})"
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        logger.error(f"Telegram alert dispatch failed: {e}")


def query_keepa_product_finder(min_rank=1, max_rank=80000, per_page=50):
    url = f"https://api.keepa.com/query?key={KEEPA_KEY}&domain=1&selection="
    query_payload = {
        "current_SALES_gte": min_rank,
        "current_SALES_lte": max_rank,
        "current_FBA_lte": -1,
        "current_AMAZON_lte": -1,
        "current_NEW_gte": 2000,
        "productType": [0, 1, 2],
        "sort": [["current_SALES", "asc"]],
        "perPage": per_page
    }

    try:
        r = requests.post(
            f"https://api.keepa.com/query?key={KEEPA_KEY}&domain=1",
            json=query_payload,
            timeout=40
        )
        if r.status_code == 429:
            data = r.json()
            refill = data.get("refillIn", 30000) / 1000.0
            logger.warning(f"Keepa 429 received. Pausing {int(refill)}s...")
            time.sleep(max(refill, 20))
            return [], 0

        data = r.json()
        asin_list = data.get("asinList", [])
        tokens_left = data.get("tokensLeft", 0)
        return asin_list, tokens_left
    except Exception as e:
        logger.error(f"Keepa Product Finder request failed: {e}")
        return [], 0


def fetch_products_detail(asins: list):
    if not asins:
        return [], 0
    codes = ",".join(asins)
    url = f"https://api.keepa.com/product?key={KEEPA_KEY}&domain=1&asin={codes}&stats=90"
    try:
        r = requests.get(url, timeout=40)
        data = r.json()
        return data.get("products", []), data.get("tokensLeft", 0)
    except Exception as e:
        logger.error(f"Keepa product fetch failed: {e}")
        return [], 0


def run_reverse_hunter(min_rank=1, max_rank=80000, min_fbm_price=20.0):
    logger.info(f"Starting Reverse Infill Hunter | BSR: {min_rank}-{max_rank} | Max FBA: 0 | Min Price: ${min_fbm_price:.2f}")

    results_file = REPORTS_DIR / "reverse_prime_monopoly_leads.json"
    leads = []
    if results_file.exists():
        try:
            leads = json.loads(results_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    seen_asins = {l["asin"] for l in leads if "asin" in l}

    while True:
        catalog_busy = is_catalog_scanner_running()
        mode_label = "50/50 Dual Allocation Mode" if catalog_busy else "100% Full-Throttle Mode"
        logger.info(f"Keepa 0-FBA Product Finder sweep [{mode_label}]...")

        asin_candidates, tokens = query_keepa_product_finder(min_rank, max_rank, per_page=40)
        logger.info(f"Retrieved {len(asin_candidates)} ASIN candidates. Tokens remaining: {tokens}")

        fresh_asins = [a for a in asin_candidates if a not in seen_asins]
        if not fresh_asins:
            logger.info("All returned ASINs already evaluated. Sleeping 60s before next sweep...")
            time.sleep(60)
            continue

        batch_size = 10
        for i in range(0, len(fresh_asins), batch_size):
            batch = fresh_asins[i:i + batch_size]
            products, tokens = fetch_products_detail(batch)

            for p in products:
                if not p or not isinstance(p, dict):
                    continue

                asin = (p.get("asin") or "").strip().upper()
                brand = (p.get("brand") or "").strip().lower()
                title = p.get("title") or "Unknown"

                if asin in BLOCKED_ASINS or brand in BLOCKED_BRANDS:
                    logger.info(f"🛡 [IP BLOCKED] Skipping {asin} ('{brand}')")
                    seen_asins.add(asin)
                    continue

                stats = p.get("stats") or {}
                current = stats.get("current") or []

                amazon_price = current[0] if len(current) > 0 else -1
                fbm_price_raw = current[1] if len(current) > 1 else -1
                sales_rank = current[3] if len(current) > 3 else -1
                fba_price = current[10] if len(current) > 10 else -1

                if amazon_price > 0 or fba_price > 0 or fbm_price_raw <= 0:
                    seen_asins.add(asin)
                    continue

                fbm_price = fbm_price_raw / 100.0
                if fbm_price < min_fbm_price:
                    seen_asins.add(asin)
                    continue

                target_prime_price = round(fbm_price * 1.10, 2)
                upcs = p.get("upcList") or []

                matched_distributor = None
                for upc in upcs:
                    clean_upc = str(upc).strip()
                    if clean_upc in CATALOG_INDEX:
                        matched_distributor = CATALOG_INDEX[clean_upc]
                        break

                est_roi = 0.0
                if matched_distributor:
                    unit_cogs = matched_distributor["cost"]
                    est_fees = 3.50 + (target_prime_price * 0.15)
                    net_profit = target_prime_price - est_fees - unit_cogs
                    est_roi = (net_profit / unit_cogs) * 100.0 if unit_cogs > 0 else 0.0

                search_query = requests.utils.quote(f"{p.get('brand', '')} {title[:40]} wholesale")

                lead = {
                    "asin": asin,
                    "title": title,
                    "brand": p.get("brand", "Unknown"),
                    "sales_rank": sales_rank,
                    "fbm_price": round(fbm_price, 2),
                    "target_prime_price": target_prime_price,
                    "seller_count": stats.get("totalOfferCount", 1),
                    "upcs": upcs,
                    "matched_vendor": matched_distributor["vendor"] if matched_distributor else None,
                    "matched_sku": matched_distributor["sku"] if matched_distributor else None,
                    "matched_cost": matched_distributor["cost"] if matched_distributor else None,
                    "est_roi": round(est_roi, 1),
                    "upc_or_title_query": search_query
                }

                leads.append(lead)
                seen_asins.add(asin)
                logger.info(f"🎯 [PRIME TARGET] {title[:45]} | BSR: {sales_rank} | FBM Floor: ${fbm_price:.2f} | Brand: {lead['brand']}")
                dispatch_telegram_reverse_alert(lead)

            results_file.write_text(json.dumps(leads, indent=2), encoding="utf-8")

            # Dynamic pacing: 50% split if catalog scanner is running, 100% capacity when it is finished
            if is_catalog_scanner_running():
                sleep_delay = 20 if tokens <= 0 else 15
            else:
                sleep_delay = 10 if tokens <= 0 else 4

            time.sleep(sleep_delay)

        logger.info("Batch cycle complete. Pausing 30s before next sweep...")
        time.sleep(30)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-rank", type=int, default=1)
    parser.add_argument("--max-rank", type=int, default=80000)
    parser.add_argument("--max-fba", type=int, default=0)
    parser.add_argument("--min-fbm-price", type=float, default=20.0)
    args = parser.parse_args()

    run_reverse_hunter(
        min_rank=args.min_rank,
        max_rank=args.max_rank,
        min_fbm_price=args.min_fbm_price
    )
