#!/usr/bin/env python3
"""
Reverse Amazon Infill Hunter (v2.0 Production)
-----------------------------------------------
Upgrades & Protections:
1. Multi-category rotation:
   - Industrial & Scientific (16310091)
   - Tools & Home Improvement (228013)
   - Office Products (1064954)
   - Health & Household (3760901)
2. Price floor lowered to $18.00 (captures high-margin MRO & commercial consumables).
3. BSR ceiling expanded to 250,000 for steady, low-competition wholesale velocity.
4. Physical size/weight gate (max 8.0 lbs, max 16.0 inches) to block commercial toilet paper and bulky oversize.
5. Strict Zero-FBA requirement (fbaOfferCount == 0 and buyBoxIsFBA is False) to exclude brand private label and false-positive Prime listings.
6. Token bucket guardrail: yields execution when Keepa balance falls below 50 tokens.
"""

import os
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

REPORTS_DIR.mkdir(parents=True, exist_ok=True)

TARGET_CATEGORIES = [
    (16310091, "Industrial & Scientific"),
    (228013, "Tools & Home Improvement"),
    (1064954, "Office Products"),
    (3760901, "Health & Household")
]


def get_token_balance() -> int:
    try:
        r = requests.get(f"https://api.keepa.com/token?key={KEEPA_KEY}", timeout=10).json()
        return int(r.get("tokensLeft", 0))
    except Exception as e:
        logger.warning(f"Failed to query Keepa token endpoint: {e}")
        return 0


def load_ip_blocklist():
    candidate_paths = [
        Path("/app/data/ip_blocklist.json"),
        Path("/app/ip_blocklist.json.bak"),
        Path("/app/ip_blocklist.json"),
        Path("data/ip_blocklist.json"),
        Path("ip_blocklist.json")
    ]
    target_path = next((p for p in candidate_paths if p.exists()), None)
    blocked_brands = set()
    blocked_asins = set()

    if not target_path:
        return blocked_brands, blocked_asins

    try:
        raw = json.loads(target_path.read_text(encoding="utf-8"))
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
        logger.info(f"Loaded IP Blocklist: {len(blocked_brands)} brands, {len(blocked_asins)} ASINs.")
    except Exception as e:
        logger.error(f"Failed to read IP blocklist: {e}")

    return blocked_brands, blocked_asins


BLOCKED_BRANDS, BLOCKED_ASINS = load_ip_blocklist()


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
        f"📈 *Estimated Net ROI:* *{deal.get('est_roi'):.1f}%*\n"
        f"💰 *Estimated Net Profit:* *${deal.get('est_net_profit'):.2f}*\n"
    ) if deal.get("matched_vendor") else "🔍 *Internal Match:* None (Distributor Sourcing Lead)\n"

    text = (
        f"🎯 *PRIME MONOPOLY LEAD (0 FBA Competitors)*\n\n"
        f"📦 *Product:* {deal.get('title')}\n"
        f"🏷 *Brand:* `{deal.get('brand')}`\n"
        f"🆔 *ASIN:* `{deal.get('asin')}`\n"
        f"📊 *Sales Rank (BSR):* {deal.get('sales_rank'):,}\n"
        f"🛒 *Baseline FBM Price:* ${deal.get('baseline_price'):.2f}\n"
        f"🎯 *Target Prime Price:* ${deal.get('target_prime_price'):.2f} (+10% Prime Premium)\n"
        f"👥 *Competition:* FBM Sellers: {deal.get('seller_count')} | FBA Sellers: 0\n"
        f"⚖️ *Dimensions/Weight:* {deal.get('weight_lbs')} lbs | {deal.get('max_dim_in')}\"\n\n"
        f"{matched_text}\n"
        f"🔗 [Amazon Listing](https://www.amazon.com/dp/{deal.get('asin')})"
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        logger.error(f"Telegram dispatch failed: {e}")


def query_keepa_product_finder(category_id: int, min_rank: int, max_rank: int, min_price: float, per_page: int = 50):
    payload = {
        "rootCategory": category_id,
        "current_SALES_gte": min_rank,
        "current_SALES_lte": max_rank,
        "current_NEW_gte": int(min_price * 100),
        "productType": [0, 1, 2],
        "sort": [["current_SALES", "asc"]],
        "perPage": per_page
    }
    try:
        url = f"https://api.keepa.com/query?key={KEEPA_KEY}&domain=1"
        r = requests.post(url, json={"selection": json.dumps(payload)}, timeout=40)
        data = r.json()
        return data.get("asinList", []) or [], data.get("tokensLeft", 0)
    except Exception as e:
        logger.error(f"Keepa query failed: {e}")
        return [], 0


def fetch_products_detail(asins: list):
    if not asins:
        return [], 0
    codes = ",".join(asins)
    url = f"https://api.keepa.com/product?key={KEEPA_KEY}&domain=1&asin={codes}&stats=90"
    try:
        r = requests.get(url, timeout=40)
        data = r.json()
        return data.get("products", []) or [], data.get("tokensLeft", 0)
    except Exception as e:
        logger.error(f"Keepa detail fetch failed: {e}")
        return [], 0


def run_reverse_hunter(min_rank=1, max_rank=250000, min_fbm_price=18.0, min_tokens=50, max_weight_lbs=8.0, max_dim_inches=16.0):
    logger.info(f"Overnight Hunter v2 Active | Min Tokens: {min_tokens} | Min Price: ${min_fbm_price:.2f} | BSR: {min_rank:,}-{max_rank:,}")
    results_file = REPORTS_DIR / "reverse_prime_monopoly_leads.json"
    leads = []
    if results_file.exists():
        try:
            leads = json.loads(results_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    seen_asins = {l["asin"] for l in leads if "asin" in l}
    step_size = 5000

    while True:
        for cat_id, cat_name in TARGET_CATEGORIES:
            current_min_rank = min_rank

            while current_min_rank < max_rank:
                available_tokens = get_token_balance()
                if available_tokens < min_tokens:
                    logger.info(f"Token balance ({available_tokens}/300) below reserve {min_tokens}. Yielding 45s...")
                    time.sleep(45)
                    continue

                current_max_rank = min(current_min_rank + step_size, max_rank)
                logger.info(f"Sweep [{cat_name}] BSR {current_min_rank:,}-{current_max_rank:,} (Tokens: {available_tokens})...")

                asin_candidates, _ = query_keepa_product_finder(
                    category_id=cat_id,
                    min_rank=current_min_rank,
                    max_rank=current_max_rank,
                    min_price=min_fbm_price,
                    per_page=50
                )

                current_min_rank += step_size
                fresh_asins = [a for a in asin_candidates if a not in seen_asins]

                if not fresh_asins:
                    time.sleep(6)
                    continue

                batch_size = 10
                for i in range(0, len(fresh_asins), batch_size):
                    if get_token_balance() < 40:
                        logger.info("Tokens low (<40). Pausing batch processing...")
                        time.sleep(45)
                        break

                    batch = fresh_asins[i:i + batch_size]
                    products, _ = fetch_products_detail(batch)

                    for p in products:
                        if not p or not isinstance(p, dict):
                            continue

                        asin = (p.get("asin") or "").strip().upper()
                        brand = (p.get("brand") or "").strip().lower()
                        title = p.get("title") or "Unknown"

                        if asin in BLOCKED_ASINS or brand in BLOCKED_BRANDS:
                            seen_asins.add(asin)
                            continue

                        weight_lbs = round((p.get("packageWeight") or 0) * 0.00220462, 2)
                        max_dim_in = round(max(
                            p.get("packageLength") or 0,
                            p.get("packageWidth") or 0,
                            p.get("packageHeight") or 0
                        ) * 0.0393701, 1)

                        if weight_lbs > max_weight_lbs or max_dim_in > max_dim_inches:
                            seen_asins.add(asin)
                            continue

                        stats = p.get("stats") or {}
                        current = stats.get("current") or []

                        amazon_price = current[0] if len(current) > 0 else -1
                        fbm_price_raw = current[1] if len(current) > 1 else -1
                        sales_rank = current[3] if len(current) > 3 else -1

                        if amazon_price > 0:
                            seen_asins.add(asin)
                            continue

                        fba_count = stats.get("fbaOfferCount", 0)
                        buy_box_is_fba = stats.get("buyBoxIsFBA", False)

                        if fba_count > 0 or buy_box_is_fba is True:
                            seen_asins.add(asin)
                            continue

                        if fbm_price_raw <= 0:
                            seen_asins.add(asin)
                            continue

                        baseline_price = fbm_price_raw / 100.0
                        if baseline_price < min_fbm_price:
                            seen_asins.add(asin)
                            continue

                        target_prime_price = round(baseline_price * 1.10, 2)

                        upcs = p.get("upcList") or []
                        matched_distributor = None
                        for upc in upcs:
                            clean_upc = str(upc).strip()
                            if clean_upc in CATALOG_INDEX:
                                matched_distributor = CATALOG_INDEX[clean_upc]
                                break

                        est_roi = 0.0
                        net_profit = 0.0
                        if matched_distributor:
                            unit_cogs = matched_distributor["cost"]
                            est_fees = 4.20 + (target_prime_price * 0.15)
                            net_profit = target_prime_price - est_fees - unit_cogs
                            est_roi = (net_profit / unit_cogs) * 100.0 if unit_cogs > 0 else 0.0

                        lead = {
                            "asin": asin,
                            "title": title,
                            "brand": p.get("brand", "Unknown"),
                            "category": cat_name,
                            "sales_rank": sales_rank,
                            "weight_lbs": weight_lbs,
                            "max_dim_in": max_dim_in,
                            "baseline_price": round(baseline_price, 2),
                            "target_prime_price": target_prime_price,
                            "seller_count": stats.get("totalOfferCount", 1),
                            "fba_count": 0,
                            "upcs": upcs,
                            "matched_vendor": matched_distributor["vendor"] if matched_distributor else None,
                            "matched_sku": matched_distributor["sku"] if matched_distributor else None,
                            "matched_cost": matched_distributor["cost"] if matched_distributor else None,
                            "est_roi": round(est_roi, 1),
                            "est_net_profit": round(net_profit, 2)
                        }

                        leads.append(lead)
                        seen_asins.add(asin)
                        logger.info(f"Target Banked: {asin} | {title[:32]} | ${target_prime_price:.2f} | BSR {sales_rank:,} ({cat_name})")
                        dispatch_telegram_reverse_alert(lead)

                    results_file.write_text(json.dumps(leads, indent=2), encoding="utf-8")
                    time.sleep(10)

                time.sleep(12)

        logger.info("Full category cycle complete. Resting 30s before next rotation...")
        time.sleep(30)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reverse Amazon Infill Hunter v2")
    parser.add_argument("--min-rank", type=int, default=1)
    parser.add_argument("--max-rank", type=int, default=250000)
    parser.add_argument("--min-fbm-price", type=float, default=18.0)
    parser.add_argument("--min-tokens", type=int, default=50)
    parser.add_argument("--max-weight", type=float, default=8.0)
    parser.add_argument("--max-dim", type=float, default=16.0)
    args = parser.parse_args()

    run_reverse_hunter(
        min_rank=args.min_rank,
        max_rank=args.max_rank,
        min_fbm_price=args.min_fbm_price,
        min_tokens=args.min_tokens,
        max_weight_lbs=args.max_weight,
        max_dim_inches=args.max_dim
    )
