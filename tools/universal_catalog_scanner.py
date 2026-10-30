#!/usr/bin/env python3
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
    format="%(asctime)s [%(levelname)s] UniversalScanner: %(message)s"
)
logger = logging.getLogger("UniversalScanner")

KEEPA_KEY = os.getenv("KEEPA_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
MIN_ROI = float(os.getenv("MIN_ROI", 0.50))
REPORTS_DIR = Path("/app/data/reports")
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

def dispatch_telegram_alert(deal: dict):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    text = (
        f"🔥 *QUALIFIED DEAL FOUND*\n\n"
        f"📦 *Product:* {deal.get('title')}\n"
        f"🏷 *Distributor:* {deal.get('vendor')} | *SKU:* `{deal.get('sku')}`\n"
        f"🆔 *ASIN:* `{deal.get('asin')}`\n\n"
        f"💵 *Wholesale Cost:* ${deal.get('wholesale_cost'):.2f}\n"
        f"🛒 *Amazon Price:* ${deal.get('amazon_price'):.2f}\n"
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
    url = f"https://api.keepa.com/product?key={KEEPA_KEY}&domain=1&code={codes}"
    while True:
        try:
            r = requests.get(url, timeout=30)
            data = r.json()
            if r.status_code == 429:
                wait_sec = data.get("refillIn", 60000) / 1000.0
                logger.warning(f"Keepa 429 (Tokens exhausted). Pausing {int(wait_sec)}s for refill...")
                time.sleep(max(wait_sec, 45))
                continue
            tokens_left = data.get("tokensLeft", 0)
            products = data.get("products") or []
            return products, tokens_left
        except Exception as e:
            logger.error(f"Keepa network error: {e}. Retrying in 15s...")
            time.sleep(15)

def scan_catalog(input_path: str, distributor: str):
    logger.info(f"Loading {os.path.basename(input_path)} | Distributor: {distributor} | Min ROI: {MIN_ROI * 100}%")
    df = detect_file_and_read(input_path)
    df = normalize_columns(df)

    if "upc" not in df.columns or "cost" not in df.columns:
        logger.error(f"Missing essential 'upc' or 'cost' columns in {input_path}")
        return

    # Clean numeric costs and pad UPCs
    df["clean_cost"] = pd.to_numeric(df["cost"].astype(str).str.replace(r"[$,]", "", regex=True), errors="coerce")
    df["clean_upc"] = df["upc"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    df = df[df["clean_cost"] > 0]
    df = df[df["clean_upc"].str.len() >= 8]
    valid_items = df.to_dict("records")

    logger.info(f"Pre-filter complete: {len(valid_items)} SKUs queued.")

    batch_size = 5
    opportunities = []
    report_file = REPORTS_DIR / f"{distributor.lower()}_qualified_opportunities.json"

    for i in range(0, len(valid_items), batch_size):
        batch = valid_items[i:i + batch_size]
        upc_list = [item["clean_upc"] for item in batch]

        products, tokens_left = query_keepa_batch(upc_list)

        for p in products:
            if not p or not isinstance(p, dict):
                continue

            # Hardened NoneType guards
            code_list = p.get("upcList") or []
            product_code = p.get("productCode") or ""

            matched_row = next((r for r in batch if r["clean_upc"] in code_list or r["clean_upc"] == product_code), None)
            if matched_row is None:
                continue

            asin = p.get("asin")
            stats = p.get("stats") or {}
            current_prices = stats.get("current") or []
            buybox_price = current_prices[18] if len(current_prices) > 18 else -1
            new_price = current_prices[1] if len(current_prices) > 1 else -1

            price_raw = buybox_price if buybox_price > 0 else new_price
            if price_raw <= 0:
                continue

            amz_price = price_raw / 100.0
            cost = float(matched_row["clean_cost"])
            
            # Simple Fee Estimation ($3.50 base FBA + 15% referral)
            est_fees = 3.50 + (amz_price * 0.15)
            net_profit = amz_price - est_fees - cost
            roi = (net_profit / cost) if cost > 0 else 0

            if roi >= MIN_ROI:
                display_group = (p.get("salesRanks") or {}).get("displayGroup", "N/A")
                deal = {
                    "asin": asin,
                    "title": matched_row.get("title") or p.get("title", "Unknown"),
                    "vendor": distributor.upper(),
                    "sku": matched_row.get("sku", "N/A"),
                    "pack_size": 1,
                    "wholesale_cost": round(cost, 2),
                    "amazon_price": round(amz_price, 2),
                    "roi_pct": round(roi * 100.0, 1),
                    "net_profit": round(net_profit, 2),
                    "sales_rank": display_group,
                    "vendor_url": f"https://www.amazon.com/dp/{asin}"
                }
                opportunities.append(deal)
                logger.info(f"🔥 Qualified Deal: {deal['title']} | ROI: {deal['roi_pct']}% | Net: ${deal['net_profit']}")
                
                # Append incrementally to report
                report_file.write_text(json.dumps(opportunities, indent=2))
                dispatch_telegram_alert(deal)

        logger.info(f"Processed {min(i + batch_size, len(valid_items))}/{len(valid_items)} SKUs. Tokens left: {tokens_left}. Opportunities: {len(opportunities)}")

        if tokens_left < 10:
            logger.info("Tokens low. Pacing for refill (45s)...")
            time.sleep(45)
        else:
            time.sleep(3)

    logger.info(f"Finished {distributor}. Found {len(opportunities)} qualified deals.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--distributor", required=True)
    args = parser.parse_args()
    scan_catalog(args.input, args.distributor)
