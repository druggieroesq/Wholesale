#!/usr/bin/env python3
"""
Universal Catalog & Multipack Scanner
Parses distributor catalogs (BCI, Frontier, Contour), executes zero-token pre-filtering
via Master IP Guard, evaluates single & multipack variations via Keepa API, computes
true landed FBA fees and packaging buffers, and outputs qualified purchasing opportunities.
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

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.ip_guard import is_brand_blocked, is_sku_blocked

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("UniversalScanner")

ENV_FILE = Path("/app/.env")
OUTPUT_DIR = Path("/app/data/reports")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def load_env():
    if not os.getenv("KEEPA_API_KEY") and ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith("KEEPA_API_KEY=") and not line.startswith("#"):
                os.environ["KEEPA_API_KEY"] = line.split("=", 1)[1].strip().strip('"').strip("'")
            elif line.startswith("MIN_ROI=") and not line.startswith("#"):
                os.environ["MIN_ROI"] = line.split("=", 1)[1].strip().strip('"').strip("'")


def calculate_fba_fees(price: float, weight_lbs: float = 1.0) -> float:
    referral_fee = price * 0.15
    fba_pick_pack = 4.25 if weight_lbs <= 1.0 else 5.75 + (max(0, weight_lbs - 2.0) * 0.40)
    return round(referral_fee + fba_pick_pack, 2)


def get_keepa_product_data(api_key: str, identifiers: list) -> list:
    if not identifiers:
        return []
    url = f"https://api.keepa.com/product?key={api_key}&domain=1&code={','.join(identifiers)}"
    try:
        resp = requests.get(url, timeout=45)
        if resp.status_code == 200:
            data = resp.json()
            refill_in = data.get("refillIn", 0)
            tokens_left = data.get("tokensLeft", 0)
            if tokens_left < 15 and refill_in > 0:
                sleep_sec = max(5, int(refill_in / 1000) + 1)
                logger.info(f"Keepa token buffer low ({tokens_left}). Throttling {sleep_sec}s...")
                time.sleep(sleep_sec)
            return data.get("products", [])
        elif resp.status_code == 429:
            logger.warning("Keepa 429 Rate Limit hit. Backing off 300s...")
            time.sleep(300)
    except Exception as e:
        logger.error(f"Keepa product lookup failed: {e}")
    return []


def process_catalog(file_path: Path, distributor: str):
    load_env()
    api_key = os.getenv("KEEPA_API_KEY", "").strip()
    min_roi = float(os.getenv("MIN_ROI", "1.00"))  # Default 100% ROI floor

    if not api_key:
        logger.error("KEEPA_API_KEY missing. Cannot proceed.")
        return

    logger.info(f"Loading catalog: {file_path.name} | Distributor: {distributor.upper()} | Min ROI: {min_roi * 100}%")

    if file_path.suffix.lower() == ".csv":
        df = pd.read_csv(file_path, low_memory=False)
    else:
        df = pd.read_excel(file_path)

    # Standardize column headers
    cols = {str(c).strip().lower(): c for c in df.columns}
    sku_col = cols.get("sku") or cols.get("item") or cols.get("item #") or cols.get("item number") or cols.get("material") or cols.get("vendorsku")
    upc_col = cols.get("upc") or cols.get("barcode") or cols.get("upc code") or cols.get("gtin")
    cost_col = cols.get("cost") or cols.get("price") or cols.get("wholesale price") or cols.get("unit price") or cols.get("dealer price")
    brand_col = cols.get("brand") or cols.get("manufacturer") or cols.get("mfr") or cols.get("vendor")
    title_col = cols.get("title") or cols.get("description") or cols.get("item description")

    records_to_scan = []
    skipped_ip_count = 0

    for _, row in df.iterrows():
        sku = str(row[sku_col]).strip() if sku_col and pd.notna(row[sku_col]) else ""
        brand = str(row[brand_col]).strip() if brand_col and pd.notna(row[brand_col]) else ""
        title = str(row[title_col]).strip() if title_col and pd.notna(row[title_col]) else ""
        upc = str(row[upc_col]).strip().replace(".0", "") if upc_col and pd.notna(row[upc_col]) else ""
        try:
            cost = float(str(row[cost_col]).replace("$", "").replace(",", "").strip()) if cost_col and pd.notna(row[cost_col]) else 0.0
        except ValueError:
            cost = 0.0

        if cost <= 0 or (not upc and not sku):
            continue

        # Zero-token IP Guard verification
        blocked_sku, r_sku = is_sku_blocked(sku)
        if blocked_sku:
            skipped_ip_count += 1
            continue

        blocked_brand, r_brand = is_brand_blocked(brand=brand, title=title)
        if blocked_brand:
            skipped_ip_count += 1
            continue

        records_to_scan.append({
            "sku": sku,
            "upc": upc.zfill(12) if len(upc) in (10, 11) else upc,
            "cost": cost,
            "brand": brand,
            "title": title
        })

    logger.info(f"Pre-filter complete: {len(records_to_scan)} SKUs queued. Blocked {skipped_ip_count} restricted items via IP Guard.")

    qualified_opportunities = []
    batch_size = 20

    for i in range(0, len(records_to_scan), batch_size):
        batch = records_to_scan[i:i + batch_size]
        upcs = [b["upc"] for b in batch if b["upc"]]
        if not upcs:
            continue

        products = get_keepa_product_data(api_key, upcs)
        prod_map = {}
        for p in products:
            for upc_code in (p.get("upcList") or []):
                prod_map[upc_code] = p

        for item in batch:
            prod = prod_map.get(item["upc"])
            if not prod:
                continue

            # Basic sales rank filter: BSR <= 80,000
            stats = prod.get("stats", {})
            current_sales = stats.get("current", [0] * 30)[3] if "current" in stats else 999999
            if current_sales is None or current_sales > 80000 or current_sales <= 0:
                continue

            # Buy Box price in cents converted to dollars
            buy_box_cents = stats.get("buyBoxPrice", 0)
            if not buy_box_cents or buy_box_cents <= 0:
                continue
            buy_box = buy_box_cents / 100.0

            # Evaluate Single Pack and Multipacks (1, 2, 3, 4, 6)
            for pack_size in [1, 2, 3, 4, 6]:
                prep_cost = 0.35 * pack_size if pack_size > 1 else 0.0
                total_cogs = (item["cost"] * pack_size) + prep_cost
                fba_fees = calculate_fba_fees(buy_box, weight_lbs=0.8 * pack_size)
                inbound_freight = 0.50 * pack_size

                net_profit = buy_box - (total_cogs + fba_fees + inbound_freight)
                roi = net_profit / total_cogs if total_cogs > 0 else 0

                if roi >= min_roi:
                    qualified_opportunities.append({
                        "Distributor": distributor.upper(),
                        "Distributor_SKU": item["sku"],
                        "UPC": item["upc"],
                        "ASIN": prod.get("asin"),
                        "Brand": item["brand"],
                        "Title": prod.get("title", item["title"]),
                        "Pack_Size": pack_size,
                        "Unit_Cost": item["cost"],
                        "Total_COGS": round(total_cogs, 2),
                        "BuyBox_Price": round(buy_box, 2),
                        "FBA_Fees": round(fba_fees, 2),
                        "Net_Profit": round(net_profit, 2),
                        "ROI_Pct": round(roi * 100, 2),
                        "Sales_Rank": current_sales
                    })

        logger.info(f"Processed {min(i + batch_size, len(records_to_scan))}/{len(records_to_scan)} SKUs. Qualified items: {len(qualified_opportunities)}")

    out_file = OUTPUT_DIR / f"{distributor.lower()}_qualified_opportunities.json"
    out_file.write_text(json.dumps(qualified_opportunities, indent=2))
    logger.info(f"Scan complete. Output saved to {out_file} ({len(qualified_opportunities)} opportunities).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Universal Catalog Scanner")
    parser.add_argument("--input", required=True, help="Path to input catalog file (.xlsx or .csv)")
    parser.add_argument("--distributor", default="general", help="Distributor profile (bci, frontier, contour)")
    args = parser.parse_args()

    process_catalog(Path(args.input), args.distributor)
