#!/usr/bin/env python3
"""
tools/universal_catalog_scanner.py
Universal Distributor Catalog Scanner with robust header auto-detection,
proper pandas Series matching, token-bucket 429 backoff, and 50% ROI gate.
"""

import os
import sys
import time
import json
import logging
import argparse
from pathlib import Path
from typing import Dict, Any, List, Optional
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv("/app/.env", override=False)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] UniversalScanner: %(message)s")
logger = logging.getLogger("UniversalScanner")

KEEPA_API_KEY = os.getenv("KEEPA_API_KEY", "").strip()
MIN_ROI = float(os.getenv("MIN_ROI", os.getenv("MIN_ROI_FLOOR", "0.50")))
REPORTS_DIR = Path("/app/data/reports")
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

try:
    from tools.catalog_bot import send_deal_card
except ImportError:
    send_deal_card = None

try:
    from tools.ip_guard import IPGuard
    ip_guard = IPGuard()
except Exception:
    ip_guard = None


def find_and_load_table(file_path: Path) -> pd.DataFrame:
    """Auto-detects header row offset across different distributor templates."""
    if file_path.suffix.lower() == ".csv":
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = [f.readline() for _ in range(25)]
        
        header_row = 0
        for idx, line in enumerate(lines):
            l_lower = line.lower()
            if any(k in l_lower for k in ["upc", "sku", "item #", "item no", "barcode", "price", "cost"]):
                header_row = idx
                break
        logger.info(f"CSV header detected at row {header_row} for {file_path.name}")
        return pd.read_csv(file_path, skiprows=header_row, dtype=str)

    elif file_path.suffix.lower() in [".xlsx", ".xls"]:
        xl = pd.ExcelFile(file_path)
        sheet_name = xl.sheet_names[0]
        preview = pd.read_excel(xl, sheet_name=sheet_name, nrows=25, header=None)

        header_row = 0
        for idx, row in preview.iterrows():
            row_str = " ".join([str(val).lower() for val in row if pd.notna(val)])
            if any(k in row_str for k in ["upc", "sku", "item #", "item no", "part number", "whls price", "cost", "price", "dealerprice"]):
                header_row = idx
                break

        logger.info(f"Excel header detected at row {header_row} for {file_path.name}")
        return pd.read_excel(xl, sheet_name=sheet_name, skiprows=header_row, dtype=str)

    raise ValueError(f"Unsupported file format: {file_path.suffix}")


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Normalizes vendor columns to canonical names."""
    df.columns = [str(c).strip() for c in df.columns]
    mapping = {}

    for c in df.columns:
        cl = c.lower()
        if not mapping.get("upc") and any(k in cl for k in ["upc12", "upc_a", "upc", "barcode", "ean"]):
            mapping["upc"] = c
        elif not mapping.get("sku") and any(k in cl for k in ["activerecordcode", "distributor sku", "item #", "item no", "part #", "sku", "mpn", "code"]):
            mapping["sku"] = c
        elif not mapping.get("cost") and any(k in cl for k in ["dealerprice", "whls price", "wholesale", "cost", "unit price", "price"]):
            mapping["cost"] = c
        elif not mapping.get("title") and any(k in cl for k in ["description50", "description", "title", "item description", "product name"]):
            mapping["title"] = c
        elif not mapping.get("brand") and any(k in cl for k in ["mfgnumber", "brand", "mfg", "manufacturer"]):
            mapping["brand"] = c

    logger.info(f"Column normalization mapping: {mapping}")

    clean_df = pd.DataFrame()
    clean_df["raw_upc"] = df[mapping["upc"]] if "upc" in mapping else ""
    clean_df["sku"] = df[mapping["sku"]] if "sku" in mapping else ""
    clean_df["cost"] = df[mapping["cost"]] if "cost" in mapping else "0"
    clean_df["title"] = df[mapping["title"]] if "title" in mapping else ""
    clean_df["brand"] = df[mapping["brand"]] if "brand" in mapping else ""

    def clean_barcode(val):
        if pd.isna(val) or val is None:
            return ""
        s = str(val).strip()
        if s.endswith(".0"):
            s = s[:-2]
        digits = "".join([ch for ch in s if ch.isdigit()])
        if len(digits) in [11, 12, 13]:
            return digits.zfill(12) if len(digits) == 11 else digits
        return digits

    def clean_price(val):
        if pd.isna(val) or val is None:
            return 0.0
        val_str = str(val).replace("$", "").replace(",", "").strip()
        try:
            return float(val_str)
        except ValueError:
            return 0.0

    clean_df["clean_upc"] = clean_df["raw_upc"].apply(clean_barcode)
    clean_df["clean_cost"] = clean_df["cost"].apply(clean_price)
    return clean_df


def query_keepa_batch(upcs: List[str]) -> Dict[str, Any]:
    """Queries Keepa API by UPC batch and handles 429 token-bucket throttling."""
    if not KEEPA_API_KEY:
        return {}
    url = f"https://api.keepa.com/product?key={KEEPA_API_KEY}&domain=1&code={','.join(upcs)}"
    
    while True:
        try:
            res = requests.get(url, timeout=30)
            if res.status_code == 200:
                return res.json()
            elif res.status_code == 429:
                err_data = {}
                try:
                    err_data = res.json()
                except Exception:
                    pass
                refill_ms = err_data.get("refillIn", 60000)
                wait_sec = max(5, int(refill_ms / 1000) + 2)
                logger.warning(f"Keepa 429 (Tokens exhausted). Pausing {wait_sec}s for token refill...")
                time.sleep(wait_sec)
                continue
            else:
                logger.error(f"Keepa API HTTP {res.status_code}: {res.text[:200]}")
                return {}
        except Exception as e:
            logger.error(f"Keepa query connection error: {e}")
            time.sleep(5)
            return {}


def scan_catalog(file_path: str, distributor: str):
    path = Path(file_path)
    logger.info(f"Loading {path.name} | Distributor: {distributor.upper()} | Min ROI: {MIN_ROI*100:.1f}%")

    raw_df = find_and_load_table(path)
    df = normalize_columns(raw_df)

    blocked_count = 0
    valid_rows = []
    for _, row in df.iterrows():
        brand = row["brand"]
        sku = row["sku"]
        if ip_guard and (ip_guard.is_blocked_brand(brand) or ip_guard.is_blocked_sku(sku)):
            blocked_count += 1
            continue
        if row["clean_upc"] and row["clean_cost"] > 0:
            valid_rows.append(row)

    logger.info(f"Pre-filter complete: {len(valid_rows)} SKUs queued. IP Guard blocked {blocked_count} items.")

    if not valid_rows:
        logger.warning(f"No valid items found in {path.name}.")
        report_file = REPORTS_DIR / f"{distributor.lower()}_qualified_opportunities.json"
        report_file.write_text("[]")
        return

    batch_size = 20
    opportunities = []

    for i in range(0, len(valid_rows), batch_size):
        batch = valid_rows[i:i+batch_size]
        upc_list = [r["clean_upc"] for r in batch if len(r["clean_upc"]) >= 10]
        if not upc_list:
            continue

        keepa_data = query_keepa_batch(upc_list)
        products = keepa_data.get("products", [])

        for p in products:
            asin = p.get("asin")
            code_list = p.get("upcList", [])
            csv_data = p.get("csv", [])
            if not csv_data:
                continue

            buybox = None
            if len(csv_data) > 18 and csv_data[18]:
                buybox = csv_data[18][-1]
            elif len(csv_data) > 1 and csv_data[1]:
                buybox = csv_data[1][-1]

            if not buybox or buybox <= 0:
                continue

            amz_price = buybox / 100.0

            # Safe None check for pandas Series
            matched_row = next((r for r in batch if r["clean_upc"] in code_list or r["clean_upc"] == p.get("productCode")), None)
            if matched_row is None:
                continue

            cogs = matched_row["clean_cost"]
            fba_fee_est = max(4.50, amz_price * 0.15 + 3.50)
            prep_fee = 0.35
            net_profit = amz_price - fba_fee_est - prep_fee - cogs
            roi = (net_profit / cogs) if cogs > 0 else 0

            if roi >= MIN_ROI:
                deal = {
                    "asin": asin,
                    "title": matched_row["title"] or p.get("title", "Catalog Opportunity"),
                    "vendor": distributor.upper(),
                    "sku": matched_row["sku"],
                    "pack_size": 1,
                    "wholesale_cost": cogs,
                    "amazon_price": amz_price,
                    "roi_pct": round(roi * 100, 1),
                    "net_profit": round(net_profit, 2),
                    "sales_rank": p.get("salesRanks", {}).get("displayGroup", "N/A"),
                    "vendor_url": f"https://www.amazon.com/dp/{asin}"
                }
                opportunities.append(deal)
                logger.info(f"🔥 Qualified Deal: {deal['title'][:35]} | ROI: {deal['roi_pct']}% | Net: ${deal['net_profit']}")
                if send_deal_card:
                    send_deal_card(deal)

        tokens_left = keepa_data.get("tokensLeft", 0)
        logger.info(f"Processed {min(i+batch_size, len(valid_rows))}/{len(valid_rows)} SKUs. Tokens left: {tokens_left}. Opportunities: {len(opportunities)}")

        # Respect Starter plan token limit (refills ~20 tokens/minute)
        if tokens_left < 25:
            logger.info("Tokens low. Pacing for refill (45s)...")
            time.sleep(45)
        else:
            time.sleep(2.5)

    report_file = REPORTS_DIR / f"{distributor.lower()}_qualified_opportunities.json"
    report_file.write_text(json.dumps(opportunities, indent=2))
    logger.info(f"Catalog scan finished: {report_file} ({len(opportunities)} opportunities).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--distributor", required=True)
    args = parser.parse_args()

    scan_catalog(args.input, args.distributor)
