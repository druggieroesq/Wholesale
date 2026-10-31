#!/usr/bin/env python3
"""
Reverse Amazon Infill Hunter (Dynamic High-Throughput Edition)
---------------------------------------------------------------
Core Features:
1. Dynamic Token Adaptation:
   - Reads Keepa's live `refillRate` and `tokensLeft` dynamically.
   - Adjusts batch sizes and sleep delays proportionally to the account subscription tier.
   - Automatically accelerates when plan speed increases without requiring manual config changes.
2. Private Label / Brand-Owner Filter:
   - Enforces `totalOfferCount >= 2` to eliminate 1-seller DTC private label traps (e.g. Essential Values).
3. Zero-Prime & Strict Buy Box Lock:
   - Requires `fbaOfferCount == 0` and `buyBoxIsFBA is False`.
4. Physical Bulky-Freight Safety Gate:
   - Filters out unlisted dimension/weight metadata, weights > 8.0 lbs, or longest edge > 16.0 inches.
   - Keyword blocklist suppresses toilet paper, paper towels, trash liners, and pallet freight.
5. Continuous 4-Category Rotation:
   - Industrial & Scientific (16310091)
   - Tools & Home Improvement (228013)
   - Office Products (1064954)
   - Health & Household (3760901)
6. Real-time Distributor Cross-Match:
   - Checks Keepa `upcList` against indexed files in `/app/data/catalogs/incoming/`.
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

BULK_BLOCKLIST = [
    "toilet paper", "bath tissue", "paper towel", "facial tissue",
    "can liner", "trash bag", "gallon drum", "drum of", "pallet",
    "case of 48", "case of 96", "case of 80"
]


def get_keepa_meta():
    """Fetches real-time available tokens and refill rate from Keepa."""
    try:
        r = requests.get(f"https://api.keepa.com/token?key={KEEPA_KEY}", timeout=10).json()
        tokens_left = int(r.get("tokensLeft", 0))
        refill_rate = int(r.get("refillRate", 20))
        return tokens_left, refill_rate
    except Exception as e:
        logger.warning(f"Error checking Keepa token meta: {e}")
        return 0, 20


def load_ip_blocklist():
    """Loads known IP complaint brands and restricted ASINs."""
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
    """Indexes incoming distributor catalogs for real-time UPC cross-matching."""
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


def dispatch_telegram_alert(deal: dict):
    """Dispatches qualified Prime monopoly alert to Telegram."""
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
        f"📊 *Sales Rank (BSR):* {deal.get('sales_rank'):,} ({deal.get('category')})\n"
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


def run_reverse_hunter(min_rank=1, max_rank=250000, min_fbm_price=18.0, max_weight=8.0, max_dim=16.0):
    init_tokens, init_refill = get_keepa_meta()
    logger.info(
        f"Dynamic Reverse Hunter Active | Initial Refill: {init_refill}/min | "
        f"Tokens: {init_tokens} | BSR: {min_rank:,}-{max_rank:,} | Min Price: ${min_fbm_price:.2f}"
    )

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
            cur_min = min_rank

            while cur_min < max_rank:
                tokens_left, refill_rate = get_keepa_meta()

                # Dynamic reserve ceiling: half of 1 minute's refill capacity (floor at 10)
                dynamic_reserve = max(10, int(refill_rate * 0.5))
                if tokens_left < dynamic_reserve:
                    # Dynamically calculate exact sleep time to replenish back to reserve
                    sleep_sec = max(2.0, round(float(dynamic_reserve - tokens_left) / (refill_rate / 60.0), 2))
                    logger.info(f"Token balance ({tokens_left}) below reserve ({dynamic_reserve}). Resting {sleep_sec}s for refill ({refill_rate}/min)...")
                    time.sleep(sleep_sec)
                    continue

                cur_max = min(cur_min + step_size, max_rank)
                logger.info(f"Sweep [{cat_name}] BSR {cur_min:,}-{cur_max:,} | Tokens: {tokens_left} (Refill: {refill_rate}/min)...")

                asin_candidates, _ = query_keepa_product_finder(
                    category_id=cat_id,
                    min_rank=cur_min,
                    max_rank=cur_max,
                    min_price=min_fbm_price,
                    per_page=50
                )

                cur_min += step_size
                fresh_asins = [a for a in asin_candidates if a not in seen_asins]

                if not fresh_asins:
                    # Tiny cadence pause scaled to refill speed
                    time.sleep(max(0.5, round(2.0 / (refill_rate / 20.0), 2)))
                    continue

                # Batch sizing scales with refill capability (30 for fast plans, 10 for standard)
                batch_size = 30 if refill_rate >= 100 else (20 if refill_rate >= 50 else 10)

                for i in range(0, len(fresh_asins), batch_size):
                    tokens_left, refill_rate = get_keepa_meta()
                    dynamic_reserve = max(10, int(refill_rate * 0.5))
                    if tokens_left < dynamic_reserve:
                        sleep_sec = max(2.0, round(float(dynamic_reserve - tokens_left) / (refill_rate / 60.0), 2))
                        time.sleep(sleep_sec)
                        break

                    batch = fresh_asins[i:i + batch_size]
                    products, _ = fetch_products_detail(batch)

                    for p in products:
                        if not p or not isinstance(p, dict):
                            continue

                        asin = (p.get("asin") or "").strip().upper()
                        brand = (p.get("brand") or "").strip().lower()
                        title = p.get("title") or "Unknown"

                        # 1. IP Blocklist Filter
                        if asin in BLOCKED_ASINS or brand in BLOCKED_BRANDS:
                            seen_asins.add(asin)
                            continue

                        # 2. Bulky Commercial Paper & Trash Liners Filter
                        if any(k in title.lower() for k in BULK_BLOCKLIST):
                            seen_asins.add(asin)
                            continue

                        # 3. Physical Specs Extraction
                        weight_lbs = round((p.get("packageWeight") or 0) * 0.00220462, 2)
                        max_dim_in = round(max(
                            p.get("packageLength") or 0,
                            p.get("packageWidth") or 0,
                            p.get("packageHeight") or 0
                        ) * 0.0393701, 1)

                        # Drop missing metadata (null/0) and oversize/heavy items
                        if weight_lbs <= 0.05 or weight_lbs > max_weight or max_dim_in > max_dim:
                            seen_asins.add(asin)
                            continue

                        stats = p.get("stats") or {}
                        current = stats.get("current") or []

                        amazon_price = current[0] if len(current) > 0 else -1
                        fbm_price_raw = current[1] if len(current) > 1 else -1
                        sales_rank = current[3] if len(current) > 3 else -1

                        # 4. Amazon Retail Exclusion
                        if amazon_price > 0:
                            seen_asins.add(asin)
                            continue

                        # 5. Strict Zero-Prime Buy Box Gate
                        fba_count = stats.get("fbaOfferCount", 0)
                        buy_box_is_fba = stats.get("buyBoxIsFBA", False)
                        if fba_count > 0 or buy_box_is_fba is True:
                            seen_asins.add(asin)
                            continue

                        # 6. Minimum 2 Competing Sellers Gate (Blocks single-seller DTC private labels)
                        total_offers = stats.get("totalOfferCount", 0)
                        if total_offers < 2:
                            seen_asins.add(asin)
                            continue

                        # 7. Price Gate
                        if fbm_price_raw <= 0:
                            seen_asins.add(asin)
                            continue

                        baseline_price = fbm_price_raw / 100.0
                        if baseline_price < min_fbm_price:
                            seen_asins.add(asin)
                            continue

                        target_prime_price = round(baseline_price * 1.10, 2)

                        # 8. Distributor Catalog Cross-Match
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
                            "seller_count": total_offers,
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
                        logger.info(f"Verified Infill Lead: {asin} | {title[:32]} | BSR {sales_rank:,} | Base ${baseline_price:.2f}")
                        dispatch_telegram_alert(lead)

                    results_file.write_text(json.dumps(leads, indent=2), encoding="utf-8")

                    # Dynamic batch pause: scales down inversely with refill rate
                    inter_batch_delay = max(0.5, round(6.0 / (refill_rate / 20.0), 2))
                    time.sleep(inter_batch_delay)

                sweep_delay = max(1.0, round(4.0 / (refill_rate / 20.0), 2))
                time.sleep(sweep_delay)

        cycle_delay = max(2.0, round(10.0 / (refill_rate / 20.0), 2))
        logger.info(f"Category sweep complete. Cycling rotation in {cycle_delay}s...")
        time.sleep(cycle_delay)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reverse Amazon Infill Hunter (Dynamic Edition)")
    parser.add_argument("--min-rank", type=int, default=1)
    parser.add_argument("--max-rank", type=int, default=250000)
    parser.add_argument("--min-fbm-price", type=float, default=18.0)
    parser.add_argument("--max-weight", type=float, default=8.0)
    parser.add_argument("--max-dim", type=float, default=16.0)
    args = parser.parse_args()

    run_reverse_hunter(
        min_rank=args.min_rank,
        max_rank=args.max_rank,
        min_fbm_price=args.min_fbm_price,
        max_weight=args.max_weight,
        max_dim=args.max_dim
    )
