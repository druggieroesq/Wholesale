#!/usr/bin/env python3
# tools/reverse_amazon_finder.py - Complete Hunter with Dynamic BSR Sliding, Page Iteration, and Max-FBA Support
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

REPORTS_DIR.mkdir(parents=True, exist_ok=True)


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
        logger.warning("No IP blocklist found at /app/data or root. Running without IP blocklist.")
        return blocked_brands, blocked_asins

    try:
        dest_path = Path("/app/data/ip_blocklist.json")
        if target_path != dest_path and not dest_path.exists():
            try:
                dest_path.write_text(target_path.read_text(encoding="utf-8"), encoding="utf-8")
                logger.info(f"Mirrored {target_path} -> {dest_path} for persistent storage.")
            except Exception as e:
                logger.warning(f"Could not mirror blocklist to /app/data: {e}")

        logger.info(f"Loading IP blocklist from: {target_path}")
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
            for key, val in raw.items():
                if key not in ["brands", "asins"] and isinstance(val, (str, list, dict)):
                    blocked_brands.add(str(key).strip().lower())
        logger.info(f"Loaded IP Blocklist: {len(blocked_brands)} brands, {len(blocked_asins)} ASINs.")
    except Exception as e:
        logger.error(f"Failed to read {target_path}: {e}")

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

    fba_tag = "0 FBA MONOPOLY" if deal.get("fba_count", 0) == 0 else f"LOW FBA COMPETITION ({deal.get('fba_count')} FBA)"

    matched_text = (
        f"🏷 *Internal Match:* `{deal.get('matched_vendor')}` (SKU: `{deal.get('matched_sku')}` | Cost: ${deal.get('matched_cost'):.2f})\n"
        f"📈 *Estimated Net ROI:* *{deal.get('est_roi'):.1f}%*\n"
        f"💰 *Estimated Net Profit:* *${deal.get('est_net_profit'):.2f}*\n"
    ) if deal.get("matched_vendor") else "🔍 *Internal Match:* None (Distributor Sourcing Lead)\n"

    text = (
        f"🎯 *OPPORTUNITY TARGET [{fba_tag}]*\n\n"
        f"📦 *Product:* {deal.get('title')}\n"
        f"🏷 *Brand:* `{deal.get('brand')}`\n"
        f"🆔 *ASIN:* `{deal.get('asin')}`\n"
        f"📊 *Sales Rank (BSR):* {deal.get('sales_rank'):,}\n"
        f"🛒 *Baseline Price:* ${deal.get('baseline_price'):.2f}\n"
        f"🎯 *Target Selling Price:* ${deal.get('target_prime_price'):.2f} ({deal.get('pricing_strategy')})\n"
        f"👥 *Competition:* Total Sellers: {deal.get('seller_count')} | FBA Sellers: {deal.get('fba_count')}\n\n"
        f"{matched_text}\n"
        f"🔗 [Amazon Listing](https://www.amazon.com/dp/{deal.get('asin')}) | "
        f"[Google Sourcing Search](https://www.google.com/search?q={deal.get('upc_or_title_query')})"
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        logger.error(f"Telegram alert dispatch failed: {e}")


def query_keepa_product_finder(min_rank=1, max_rank=80000, max_fba=2, min_price=25.0, per_page=50, page=0):
    query_payload = {
        "current_SALES_gte": min_rank,
        "current_SALES_lte": max_rank,
        "current_NEW_gte": int(min_price * 100),
        "outOfStock_AMAZON": True,
        "productType": [0, 1, 2],
        "sort": [["current_SALES", "asc"]],
        "perPage": per_page,
        "page": page
    }

    if max_fba == 0:
        query_payload["outOfStock_FBA"] = True
    else:
        query_payload["fbaOfferCount_lte"] = max_fba

    try:
        url = f"https://api.keepa.com/query?key={KEEPA_KEY}&domain=1"
        r = requests.post(
            url,
            json={"selection": json.dumps(query_payload)},
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


def run_reverse_hunter(min_rank=1, max_rank=120000, max_fba=2, min_fbm_price=25.0):
    bsr_windows = [
        (1, 5000),
        (5001, 15000),
        (15001, 30000),
        (30001, 50000),
        (50001, 75000),
        (75001, 100000),
        (100001, 120000)
    ]
    window_idx = 0
    page_idx = 0

    logger.info(f"Starting Reverse Infill Hunter | Dynamic Sliding Tiers | Max FBA: {max_fba} | Min Price: ${min_fbm_price:.2f}")

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

        cur_min, cur_max = bsr_windows[window_idx]
        logger.info(f"Keepa sweep [BSR {cur_min:,}-{cur_max:,} | Page {page_idx}] (Max FBA: {max_fba}) [{mode_label}]...")

        asin_candidates, tokens = query_keepa_product_finder(
            min_rank=cur_min,
            max_rank=cur_max,
            max_fba=max_fba,
            min_price=min_fbm_price,
            per_page=50,
            page=page_idx
        )
        logger.info(f"Retrieved {len(asin_candidates)} candidates [Tier {cur_min:,}-{cur_max:,} | P{page_idx}]. Tokens: {tokens}")

        fresh_asins = [a for a in asin_candidates if a not in seen_asins]

        if not fresh_asins or page_idx >= 4:
            page_idx = 0
            window_idx = (window_idx + 1) % len(bsr_windows)
            if not fresh_asins:
                time.sleep(5)
                continue
        else:
            page_idx += 1

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
                    logger.info(f"Skipping blocked item: {asin} ('{brand}')")
                    seen_asins.add(asin)
                    continue

                stats = p.get("stats") or {}
                current = stats.get("current") or []

                amazon_price = current[0] if len(current) > 0 else -1
                fbm_price_raw = current[1] if len(current) > 1 else -1
                sales_rank = current[3] if len(current) > 3 else -1
                fba_price_raw = current[10] if len(current) > 10 else -1
                buy_box_raw = current[18] if len(current) > 18 else -1

                if amazon_price > 0:
                    seen_asins.add(asin)
                    continue

                fba_count = stats.get("fbaOfferCount", 0)
                if fba_count > max_fba:
                    seen_asins.add(asin)
                    continue

                target_prime_price = 0.0
                baseline_price = 0.0
                pricing_strategy = "Unknown"

                if fba_price_raw > 0 or buy_box_raw > 0:
                    benchmark_raw = buy_box_raw if buy_box_raw > 0 else fba_price_raw
                    baseline_price = benchmark_raw / 100.0
                    target_prime_price = round(baseline_price, 2)
                    pricing_strategy = f"Match FBA Buy Box ({fba_count} FBA competitor{'s' if fba_count != 1 else ''})"
                elif fbm_price_raw > 0:
                    baseline_price = fbm_price_raw / 100.0
                    target_prime_price = round(baseline_price * 1.10, 2)
                    pricing_strategy = "Prime Monopoly (+10% over FBM floor)"
                else:
                    seen_asins.add(asin)
                    continue

                if target_prime_price < min_fbm_price:
                    seen_asins.add(asin)
                    continue

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
                    est_fees = 3.50 + (target_prime_price * 0.15)
                    net_profit = target_prime_price - est_fees - unit_cogs
                    est_roi = (net_profit / unit_cogs) * 100.0 if unit_cogs > 0 else 0.0

                search_query = requests.utils.quote(f"{p.get('brand', '')} {title[:40]} wholesale")

                lead = {
                    "asin": asin,
                    "title": title,
                    "brand": p.get("brand", "Unknown"),
                    "sales_rank": sales_rank,
                    "baseline_price": round(baseline_price, 2),
                    "target_prime_price": target_prime_price,
                    "pricing_strategy": pricing_strategy,
                    "seller_count": stats.get("totalOfferCount", 1),
                    "fba_count": fba_count,
                    "upcs": upcs,
                    "matched_vendor": matched_distributor["vendor"] if matched_distributor else None,
                    "matched_sku": matched_distributor["sku"] if matched_distributor else None,
                    "matched_cost": matched_distributor["cost"] if matched_distributor else None,
                    "est_roi": round(est_roi, 1),
                    "est_net_profit": round(net_profit, 2),
                    "upc_or_title_query": search_query
                }

                leads.append(lead)
                seen_asins.add(asin)
                logger.info(
                    f"Target identified: {title[:40]} | BSR: {sales_rank} | "
                    f"Target: ${target_prime_price:.2f} ({pricing_strategy}) | Brand: {lead['brand']}"
                )
                dispatch_telegram_reverse_alert(lead)

            results_file.write_text(json.dumps(leads, indent=2), encoding="utf-8")

            if is_catalog_scanner_running():
                sleep_delay = 20 if tokens <= 0 else 15
            else:
                sleep_delay = 10 if tokens <= 0 else 4

            time.sleep(sleep_delay)

        logger.info("Batch cycle complete. Pausing 20s before next sweep...")
        time.sleep(20)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-rank", type=int, default=1)
    parser.add_argument("--max-rank", type=int, default=120000)
    parser.add_argument("--max-fba", type=int, default=2)
    parser.add_argument("--min-fbm-price", type=float, default=25.0)
    args = parser.parse_args()

    run_reverse_hunter(
        min_rank=args.min_rank,
        max_rank=args.max_rank,
        max_fba=args.max_fba,
        min_fbm_price=args.min_fbm_price
    )
