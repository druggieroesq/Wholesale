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
    format="%(asctime)s [%(levelname)s] UniversalScanner: %(message)s"
)
logger = logging.getLogger("UniversalScanner")

KEEPA_KEY = os.getenv("KEEPA_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
MIN_ROI = float(os.getenv("MIN_ROI", 0.50))

REPORTS_DIR = Path("/app/data/reports")
CHECKPOINTS_DIR = Path("/app/data/checkpoints")
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
CHECKPOINTS_DIR.mkdir(parents=True, exist_ok=True)


def dispatch_telegram_alert(deal: dict):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    text = (
        f"🔥 *QUALIFIED DEAL FOUND*\n\n"
        f"📦 *Product:* {deal.get('title')}\n"
        f"🏷 *Distributor:* {deal.get('vendor')} | *SKU:* `{deal.get('sku')}`\n"
        f"🆔 *ASIN:* `{deal.get('asin')}`\n"
        f"📦 *Pack Size:* {deal.get('pack_size')}\n"
        f"🏷 *Offer Type:* `{deal.get('pricing_source')}`\n\n"
        f"💵 *Unit Cost:* ${deal.get('unit_cogs'):.2f} (Total COGS: ${deal.get('total_cogs'):.2f})\n"
        f"🛒 *Effective Amazon Price:* ${deal.get('amazon_price'):.2f}\n"
        f"📈 *Net ROI:* *{deal.get('roi_pct'):.1f}%*\n"
        f"💰 *Net Profit:* *${deal.get('net_profit'):.2f}*\n"
        f"📊 *Sales Rank:* {deal.get('sales_rank')}\n\n"
        f"🔗 [View on Amazon]({deal.get('vendor_url')})"
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        logger.error(f"Telegram dispatch failed: {e}")


def extract_pack_size(keepa_product: dict) -> int:
    """Extracts pack size from Keepa metadata and title to avoid multipack ROI inflation."""
    pkg_qty = keepa_product.get("packageQuantity") or keepa_product.get("numberOfItems")
    if isinstance(pkg_qty, (int, float)) and 1 < pkg_qty <= 144:
        return int(pkg_qty)

    title = keepa_product.get("title", "") or ""
    patterns = [
        r"(?:pack|count|ct|pk|case of|carton of)\s*(?:of)?\s*(\d+)",
        r"(\d+)\s*-(?:pack|pk|count|ct)",
        r"\((\d+)\s*(?:pack|count|pieces|pk)\)"
    ]
    for pattern in patterns:
        m = re.search(pattern, title, re.IGNORECASE)
        if m:
            qty = int(m.group(1))
            if 1 < qty <= 144:
                return qty
    return 1


def detect_file_and_read(file_path: str) -> pd.DataFrame:
    if file_path.endswith((".xlsx", ".xls")):
        for header_idx in range(5):
            try:
                df = pd.read_excel(file_path, header=header_idx)
                cols_upper = [str(c).strip().upper() for c in df.columns]
                if any("UPC" in c or "BARCODE" in c for c in cols_upper):
                    logger.info(f"Excel header detected at row {header_idx} for {os.path.basename(file_path)}")
                    return df
            except Exception:
                continue
        return pd.read_excel(file_path)
    else:
        for sep in [",", "\t", ";", "|"]:
            try:
                df = pd.read_csv(file_path, sep=sep, nrows=15)
                cols_upper = [str(c).strip().upper() for c in df.columns]
                if any("UPC" in c or "BARCODE" in c or "SKU" in c for c in cols_upper):
                    return pd.read_csv(file_path, sep=sep, dtype=str)
            except Exception:
                continue
        return pd.read_csv(file_path, dtype=str)


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    col_map = {}
    for col in df.columns:
        c_upper = str(col).strip().upper()
        if "upc" not in col_map and any(k in c_upper for k in ["UPC12", "UPC", "BARCODE", "EAN"]):
            col_map["upc"] = col
        elif "title" not in col_map and any(k in c_upper for k in ["DESCRIPTION50", "DESCRIPTION", "PRODUCT_NAME", "NAME", "TITLE"]):
            col_map["title"] = col
        elif "brand" not in col_map and any(k in c_upper for k in ["MFGNAME", "VENDORNAME", "BRAND", "MANUFACTURER"]):
            col_map["brand"] = col
        elif "cost" not in col_map and any(k in c_upper for k in ["DEALERPRICE", "PRICE", "COST", "WHOLESALE"]):
            col_map["cost"] = col
        elif "sku" not in col_map and any(k in c_upper for k in ["ACTIVERECORDCODE", "ITEMNUMBER", "ITEM", "SKU", "PART"]):
            col_map["sku"] = col

    logger.info(f"Column normalization mapping: {col_map}")
    df_clean = pd.DataFrame()
    for standard_col, orig_col in col_map.items():
        df_clean[standard_col] = df[orig_col]
    return df_clean


def query_keepa_batch(upcs: list) -> tuple:
    codes = ",".join(upcs)
    url = f"https://api.keepa.com/product?key={KEEPA_KEY}&domain=1&code={codes}&stats=90"
    while True:
        try:
            r = requests.get(url, timeout=30)
            data = r.json()
            if r.status_code == 429:
                wait_sec = data.get("refillIn", 30000) / 1000.0
                logger.warning(f"Keepa 429 (Tokens exhausted). Pausing {int(wait_sec)}s for refill...")
                time.sleep(max(wait_sec, 20))
                continue
            tokens_left = data.get("tokensLeft", 0)
            products = data.get("products") or []
            return products, tokens_left
        except Exception as e:
            logger.error(f"Keepa network error: {e}. Retrying in 10s...")
            time.sleep(10)


def scan_catalog(input_path: str, distributor: str):
    logger.info(f"Loading {os.path.basename(input_path)} | Distributor: {distributor} | Min ROI: {MIN_ROI * 100}%")
    df = detect_file_and_read(input_path)
    df = normalize_columns(df)

    if "upc" not in df.columns or "cost" not in df.columns:
        logger.error(f"Missing essential 'upc' or 'cost' columns in {input_path}")
        return

    df["clean_cost"] = pd.to_numeric(df["cost"].astype(str).str.replace(r"[$,]", "", regex=True), errors="coerce")
    df["clean_upc"] = df["upc"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    df = df[df["clean_cost"] > 0]
    df = df[df["clean_upc"].str.len() >= 8]
    valid_items = df.to_dict("records")

    logger.info(f"Pre-filter complete: {len(valid_items)} SKUs queued.")

    checkpoint_file = CHECKPOINTS_DIR / f"{distributor.lower()}_checkpoint.json"
    report_file = REPORTS_DIR / f"{distributor.lower()}_qualified_opportunities.json"

    start_idx = 0
    opportunities = []

    # Checkpoint recovery: Resume exactly where execution left off
    if checkpoint_file.exists():
        try:
            cp_data = json.loads(checkpoint_file.read_text())
            start_idx = cp_data.get("last_index", 0)
            opportunities = cp_data.get("opportunities", [])
            logger.info(f"🎯 Resuming {distributor} from SKU index {start_idx}/{len(valid_items)} (Preserved {len(opportunities)} found deals)")
        except Exception as e:
            logger.warning(f"Failed to read checkpoint: {e}. Starting fresh.")

    batch_size = 5

    for i in range(start_idx, len(valid_items), batch_size):
        batch = valid_items[i:i + batch_size]
        upc_list = [item["clean_upc"] for item in batch]

        products, tokens_left = query_keepa_batch(upc_list)

        for p in products:
            if not p or not isinstance(p, dict):
                continue

            code_list = p.get("upcList") or []
            product_code = p.get("productCode") or ""

            matched_row = next((r for r in batch if r["clean_upc"] in code_list or r["clean_upc"] == product_code), None)
            if matched_row is None:
                continue

            asin = p.get("asin")
            stats = p.get("stats") or {}
            current_prices = stats.get("current") or []

            # ---------------------------------------------------------
            # Enhanced Price Extraction (Captures Suppressed Buy Boxes)
            # Index 18 = Active Buy Box
            # Index 10 = FBA / Prime 3rd-Party offer
            # Index 1  = New 3rd-Party merchant offer
            # ---------------------------------------------------------
            buybox_price = current_prices[18] if len(current_prices) > 18 else -1
            fba_price    = current_prices[10] if len(current_prices) > 10 else -1
            new_price    = current_prices[1]  if len(current_prices) > 1  else -1

            price_raw = -1
            pricing_source = "None"

            if buybox_price > 0:
                price_raw = buybox_price
                pricing_source = "Active Buy Box"
            elif fba_price > 0:
                price_raw = fba_price
                pricing_source = "Suppressed Buy Box (Prime Offer)"
            elif new_price > 0:
                price_raw = new_price
                pricing_source = "Lowest New Offer"

            if price_raw <= 0:
                continue

            amz_price = price_raw / 100.0
            unit_cost = float(matched_row["clean_cost"])

            # Multipack validation
            pack_size = extract_pack_size(p)
            total_cogs = unit_cost * pack_size

            # Fee modeling: $3.50 standard FBA baseline + 15% referral fee
            est_fees = 3.50 + (amz_price * 0.15)
            net_profit = amz_price - est_fees - total_cogs
            roi = (net_profit / total_cogs) if total_cogs > 0 else 0

            # Current sales rank
            current_rank = current_prices[3] if len(current_prices) > 3 else -1
            sales_ranks = p.get("salesRanks") or {}
            display_group = next(iter(sales_ranks.values()), current_rank) if sales_ranks else current_rank

            if roi >= MIN_ROI:
                deal = {
                    "asin": asin,
                    "title": matched_row.get("title") or p.get("title", "Unknown"),
                    "vendor": distributor.upper(),
                    "sku": matched_row.get("sku", "N/A"),
                    "pack_size": pack_size,
                    "pricing_source": pricing_source,
                    "unit_cogs": round(unit_cost, 2),
                    "total_cogs": round(total_cogs, 2),
                    "amazon_price": round(amz_price, 2),
                    "roi_pct": round(roi * 100.0, 1),
                    "net_profit": round(net_profit, 2),
                    "sales_rank": display_group,
                    "vendor_url": f"https://www.amazon.com/dp/{asin}"
                }
                opportunities.append(deal)
                logger.info(f"🔥 Qualified Deal: {deal['title']} [{pricing_source}] (Pack: {pack_size}) | ROI: {deal['roi_pct']}% | Net: ${deal['net_profit']}")
                dispatch_telegram_alert(deal)

        processed_count = min(i + batch_size, len(valid_items))
        logger.info(f"Processed {processed_count}/{len(valid_items)} SKUs. Tokens left: {tokens_left}. Opportunities: {len(opportunities)}")

        # Atomic checkpoint save
        checkpoint_data = {
            "last_index": processed_count,
            "opportunities": opportunities
        }
        checkpoint_file.write_text(json.dumps(checkpoint_data, indent=2))
        report_file.write_text(json.dumps(opportunities, indent=2))

        # Pacing: Smooth 4s drip; if exhausted, sleep 15s instead of stalling for 45s
        if tokens_left <= 0:
            logger.info("Tokens momentarily exhausted. Pausing 15s for refill...")
            time.sleep(15)
        else:
            time.sleep(4)

    logger.info(f"Finished {distributor}. Found {len(opportunities)} qualified deals.")
    if checkpoint_file.exists():
        checkpoint_file.unlink()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--distributor", required=True)
    args = parser.parse_args()
    scan_catalog(args.input, args.distributor)
