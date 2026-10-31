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

DATA_DIR = Path("/app/data")
REPORTS_DIR = DATA_DIR / "reports"
CHECKPOINTS_DIR = DATA_DIR / "checkpoints"
IP_BLOCKLIST_PATH = DATA_DIR / "ip_blocklist.json"

REPORTS_DIR.mkdir(parents=True, exist_ok=True)
CHECKPOINTS_DIR.mkdir(parents=True, exist_ok=True)


def load_ip_blocklist():
    """Loads brands and ASINs from /app/data/ip_blocklist.json supporting lists or dicts."""
    blocked_brands = set()
    blocked_asins = set()
    if not IP_BLOCKLIST_PATH.exists():
        logger.warning(f"No IP blocklist found at {IP_BLOCKLIST_PATH}. Proceeding without IP filter.")
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
            # Support generic key/value mappings
            for key, val in raw.items():
                if key not in ["brands", "asins"] and isinstance(val, (str, list)):
                    blocked_brands.add(str(key).strip().lower())
        logger.info(f"Loaded IP Blocklist: {len(blocked_brands)} brands, {len(blocked_asins)} ASINs protected.")
    except Exception as e:
        logger.error(f"Failed to read {IP_BLOCKLIST_PATH}: {e}")

    return blocked_brands, blocked_asins


BLOCKED_BRANDS, BLOCKED_ASINS = load_ip_blocklist()


def dispatch_telegram_alert(deal: dict):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    text = (
        f"[QUALIFIED DEAL FOUND]\n\n"
        f"Product: {deal.get('title')}\n"
        f"Distributor: {deal.get('vendor')} | SKU: {deal.get('sku')}\n"
        f"ASIN: {deal.get('asin')}\n"
        f"Pack Size: {deal.get('pack_size')}\n"
        f"Offer Type: {deal.get('pricing_source')}\n\n"
        f"Unit Cost: ${deal.get('unit_cogs'):.2f} (Total: ${deal.get('total_cogs'):.2f})\n"
        f"Amazon Price: ${deal.get('amazon_price'):.2f}\n"
        f"Net ROI: {deal.get('roi_pct'):.1f}%\n"
        f"Net Profit: ${deal.get('net_profit'):.2f}\n"
        f"Sales Rank: {deal.get('sales_rank')}\n\n"
        f"Link: https://www.amazon.com/dp/{deal.get('asin')}"
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    except Exception as e:
        logger.error(f"Telegram dispatch failed: {e}")


def extract_pack_size(keepa_product: dict) -> int:
    pkg_qty = keepa_product.get("packageQuantity") or keepa_product.get("numberOfItems")
    if isinstance(pkg_qty, (int, float)) and 1 < pkg_qty <= 144:
        return int(pkg_qty)

    title = keepa_product.get("title", "") or ""
    patterns = [
        r"(?:pack|count|ct|pk|case of|carton of)\s*(?:of)?\s*(\d+)",
        r"(\d+)\s*-(?:pack|pk|count|ct)",
        r"\((\d+)\s*(?:pack|count|pieces|pk)\)"
    ]
    for p in patterns:
        m = re.search(p, title, re.IGNORECASE)
        if m:
            qty = int(m.group(1))
            if 1 < qty <= 144:
                return qty
    return 1


def detect_file_and_read(file_path: str) -> pd.DataFrame:
    if file_path.endswith((".xlsx", ".xls")):
        for hi in range(5):
            try:
                df = pd.read_excel(file_path, header=hi)
                cu = [str(c).strip().upper() for c in df.columns]
                if any("UPC" in c or "BARCODE" in c for c in cu):
                    logger.info(f"Excel header at row {hi} for {os.path.basename(file_path)}")
                    return df
            except Exception:
                continue
        return pd.read_excel(file_path)
    else:
        for sep in [",", "\t", ";", "|"]:
            try:
                df = pd.read_csv(file_path, sep=sep, nrows=15)
                cu = [str(c).strip().upper() for c in df.columns]
                if any("UPC" in c or "BARCODE" in c or "SKU" in c for c in cu):
                    return pd.read_csv(file_path, sep=sep, dtype=str)
            except Exception:
                continue
        return pd.read_csv(file_path, dtype=str)


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    col_map = {}
    for col in df.columns:
        cu = str(col).strip().upper()
        if "upc" not in col_map and any(k in cu for k in ["UPCE", "UPC12", "UPC", "BARCODE", "EAN"]):
            col_map["upc"] = col
        elif "title" not in col_map and any(k in cu for k in ["DESCRIPTION50", "DESCRIPTION", "PRODUCT_NAME", "NAME", "TITLE"]):
            col_map["title"] = col
        elif "brand" not in col_map and any(k in cu for k in ["MFGNAME", "VENDORNAME", "BRAND", "MANUFACTURER"]):
            col_map["brand"] = col
        elif "cost" not in col_map and any(k in cu for k in ["DEALERPRICE", "PRICE", "COST", "WHOLESALE"]):
            col_map["cost"] = col
        elif "sku" not in col_map and any(k in cu for k in ["ACTIVERECORDCODE", "ITEMNUMBER", "ITEM", "SKU", "PART"]):
            col_map["sku"] = col

    logger.info(f"Column normalization mapping: {col_map}")
    df_clean = pd.DataFrame()
    for std, orig in col_map.items():
        df_clean[std] = df[orig]
    return df_clean


def query_keepa_batch(upcs: list) -> tuple:
    codes = ",".join(upcs)
    url = f"https://api.keepa.com/product?key={KEEPA_KEY}&domain=1&code={codes}&stats=90"
    while True:
        try:
            r = requests.get(url, timeout=30)
            data = r.json()
            if r.status_code == 429:
                ws = data.get("refillIn", 30000) / 1000.0
                logger.warning(f"Keepa 429 (Tokens exhausted). Pausing {int(ws)}s for refill...")
                time.sleep(max(ws, 20))
                continue
            return data.get("products", []), data.get("tokensLeft", 0)
        except Exception as e:
            logger.error(f"Keepa error: {e}. Retrying in 10s...")
            time.sleep(10)


def scan_catalog(input_path: str, distributor: str):
    logger.info(f"Loading {os.path.basename(input_path)} | Distributor: {distributor} | Min ROI: {MIN_ROI * 100}%")
    df = detect_file_and_read(input_path)
    df = normalize_columns(df)

    if "upc" not in df.columns or "cost" not in df.columns:
        logger.error(f"Missing upc or cost in {input_path}")
        return

    df["clean_cost"] = pd.to_numeric(df["cost"].astype(str).str.replace(r"[$,]", "", regex=True), errors="coerce")
    df["clean_upc"] = df["upc"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    df = df[(df["clean_cost"] > 0) & (df["clean_upc"].str.len() >= 8)]

    # PRE-FILTER: Discard SKUs whose distributor brand matches the IP blocklist before spending Keepa tokens
    if "brand" in df.columns and BLOCKED_BRANDS:
        initial_count = len(df)
        df["brand_clean"] = df["brand"].astype(str).str.strip().str.lower()
        df = df[~df["brand_clean"].isin(BLOCKED_BRANDS)]
        dropped = initial_count - len(df)
        if dropped > 0:
            logger.info(f"🛡 Pre-filtered {dropped} SKUs matching IP Blocklist brands (0 tokens wasted).")

    valid_items = df.to_dict("records")
    logger.info(f"Pre-filter complete: {len(valid_items)} SKUs queued.")

    cp_file = CHECKPOINTS_DIR / f"{distributor.lower()}_checkpoint.json"
    rp_file = REPORTS_DIR / f"{distributor.lower()}_qualified_opportunities.json"
    start_idx = 0
    opportunities = []

    if cp_file.exists():
        try:
            d = json.loads(cp_file.read_text())
            start_idx = d.get("last_index", 0)
            opportunities = d.get("opportunities", [])
            logger.info(f"[RESUME] vendor: {distributor} from SKU {start_idx}/{len(valid_items)}")
        except Exception:
            pass

    batch_size = 5
    for i in range(start_idx, len(valid_items), batch_size):
        batch = valid_items[i:i + batch_size]
        products, tokens = query_keepa_batch([r["clean_upc"] for r in batch])

        for p in products:
            if not p or not isinstance(p, dict):
                continue

            asin = (p.get("asin") or "").strip().upper()
            keepa_brand = (p.get("brand") or "").strip().lower()

            # POST-FILTER: Check Keepa's identified brand and ASIN against IP blocklist
            if asin in BLOCKED_ASINS or keepa_brand in BLOCKED_BRANDS:
                logger.info(f"🛡 Skipping {asin} ('{keepa_brand}'): Match in IP Blocklist.")
                continue

            upcs = p.get("upcList") or []
            pcode = p.get("productCode") or ""
            row = next((r for r in batch if r["clean_upc"] in upcs or r["clean_upc"] == pcode), None)
            if row is None:
                continue

            stats = p.get("stats") or {}
            cp = stats.get("current") or []
            bbx = cp[18] if len(cp) > 18 else -1
            fba = cp[10] if len(cp) > 10 else -1
            new = cp[1] if len(cp) > 1 else -1

            price_raw, source = -1, "Unknown"
            if bbx > 0:
                price_raw, source = bbx, "Active Buy Box"
            elif fba > 0:
                price_raw, source = fba, "Suppressed Buy Box (Prime)"
            elif new > 0:
                price_raw, source = new, "Lowest New"
            else:
                continue

            amz = price_raw / 100.0
            unit_c = float(row["clean_cost"])
            pk = extract_pack_size(p)
            tot_c = unit_c * pk
            net_p = amz - (3.50 + (amz * 0.15)) - tot_c
            roi = (net_p / tot_c) if tot_c > 0 else 0

            ranks = p.get("salesRanks") or {}
            dsp_rank = next(iter(ranks.values()), cp[3] if len(cp) > 3 else -1) if ranks else -1

            if roi >= MIN_ROI:
                deal = {
                    "asin": asin,
                    "title": row.get("title") or p.get("title", "Unknown"),
                    "brand": p.get("brand", "Unknown"),
                    "vendor": distributor.upper(),
                    "sku": row.get("sku", "N/A"),
                    "pack_size": pk,
                    "pricing_source": source,
                    "unit_cogs": round(unit_c, 2),
                    "total_cogs": round(tot_c, 2),
                    "amazon_price": round(amz, 2),
                    "roi_pct": round(roi * 100.0, 1),
                    "net_profit": round(net_p, 2),
                    "sales_rank": dsp_rank,
                    "vendor_url": f"https://www.amazon.com/dp/{asin}"
                }
                opportunities.append(deal)
                logger.info(f"[DEAL] {deal['title']} [{source}] (Pack: {pk}) | ROI: {deal['roi_pct']}% | Net: ${deal['net_profit']}")
                dispatch_telegram_alert(deal)

        processed = min(i + batch_size, len(valid_items))
        logger.info(f"Processed {processed}/{len(valid_items)} SKUs. Tokens left: {tokens}. Opportunities: {len(opportunities)}")

        cp_file.write_text(json.dumps({"last_index": processed, "opportunities": opportunities}, indent=2))
        rp_file.write_text(json.dumps(opportunities, indent=2))

        if tokens <= 0:
            logger.info("Tokens momentarily exhausted. Pausing 20s...")
            time.sleep(20)
        else:
            time.sleep(10)

    logger.info(f"Finished {distributor}. Found {len(opportunities)} qualified deals.")
    if cp_file.exists():
        cp_file.unlink()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--distributor", required=True)
    a = p.parse_args()
    scan_catalog(a.input, a.distributor)
