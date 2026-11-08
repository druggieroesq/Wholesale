#!/usr/bin/env python3
"""
tools/catalog_normalizer.py
---------------------------
Production Catalog Normalizer & Ingestion Engine (v7.1 Master)
- Standardizes vendor spreadsheets (BCI, Frontier, Contour, JB Tools, Midland, etc.).
- Auto-detects Excel header rows (skips empty banner/metadata rows like Frontier).
- Robust column alias matching across pricing variations (Whls Price, Unit price, etc.).
- Dual-key indexing: Generates clean_mpn AND stripped root_mpn (alphanumeric only)
  to ensure 100% match rate against Amazon Keepa titles and manufacturer part numbers.
- Dual-barcode normalization: GTIN-14 and raw stripped UPC.
- Converts raw packaging/UOM to true single-unit wholesale cost.
- Produces canonical Parquet tables with automatic CSV fallback.
- Auto-seeds discovered brands into brand_ungate_registry.json.
"""

import os
import sys
import re
import json
import logging
import argparse
from pathlib import Path
from typing import Dict, Optional, Any, List
import requests
import pandas as pd
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

load_dotenv(ROOT_DIR / ".env", override=False)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] Normalizer: %(message)s"
)
logger = logging.getLogger("Normalizer")

DATA_DIR = ROOT_DIR / "data"
REGISTRY_FILE = DATA_DIR / "brand_ungate_registry.json"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

UOM_MAP: Dict[str, Optional[int]] = {
    "EA": 1, "EACH": 1, "PC": 1, "PIECE": 1, "UNT": 1, "UNIT": 1, "ITEM": 1,
    "DZ": 12, "DOZ": 12, "DOZEN": 12,
    "PR": 2, "PAIR": 2,
    "C": 100, "HUND": 100, "CT": None,
    "M": 1000, "THOU": 1000,
    "PK": None, "PACK": None, "BX": None, "BOX": None, "CS": None, "CASE": None
}


def clean_brand_name(brand: str) -> str:
    if not brand or pd.isna(brand):
        return ""
    b = str(brand).upper().strip()
    b = re.sub(r'[^A-Z0-9\s]', ' ', b)
    b = re.sub(r'\b(LLC|INC|CORP|CORPORATION|CO|COMPANY|LTD|HOLDINGS|USA|ENTERPRISES)\b', '', b)
    return re.sub(r'\s+', ' ', b).strip()


def normalize_barcode(code: Any) -> str:
    if pd.isna(code) or not code:
        return ""
    clean = re.sub(r"[^0-9]", "", str(code).strip())
    if len(clean) in (11, 12, 13, 14):
        return clean.zfill(14)
    return ""


def clean_mpn(mpn: Any) -> str:
    if pd.isna(mpn) or not mpn:
        return ""
    val = str(mpn).strip().upper()
    if val in ("NAN", "NONE", "NULL", "N/A"):
        return ""
    if "E+" in val or "e+" in val:
        return ""
    return val


def extract_root_mpn(mpn: str) -> str:
    """Strips all non-alphanumeric characters and leading zeros for fuzzy catalog matching."""
    if not mpn:
        return ""
    clean = re.sub(r'[^A-Z0-9]', '', str(mpn).upper().strip())
    return clean.lstrip('0') or clean


def parse_pack_from_text(text: str) -> int:
    if not text or pd.isna(text):
        return 1
    t = str(text).lower()
    m_pack = re.search(r'\b(?:pack of|pk of|box of|case of|set of)\s*([0-9]{1,3})\b', t)
    if m_pack:
        return max(1, int(m_pack.group(1)))
    m_hyphen = re.search(r'\b([0-9]{1,3})\s*(?:-|–|\s)\s*(?:pack|pk|piece|pc|bundle)\b', t)
    if m_hyphen:
        return max(1, int(m_hyphen.group(1)))
    m_ct = re.search(r'\b([1-9]|[1-9][0-9]|100)\s*(?:count|ct)\b', t)
    if m_ct:
        return max(1, int(m_ct.group(1)))
    return 1


def auto_seed_brands_to_registry(brands: set, vendor: str):
    """Auto-seeds brand names into ungate registry to preserve approval status."""
    if not brands or not REGISTRY_FILE.parent.exists():
        return
    try:
        from tools.anti_regating_pulse_daemon import load_registry, save_registry
        reg = load_registry()
        monitored = reg.setdefault("monitored_brands", {})
        for b in brands:
            clean_b = clean_brand_name(b)
            if clean_b and clean_b not in monitored:
                monitored[clean_b] = {
                    "distributor": vendor,
                    "approved_date": "2026-11-03",
                    "requires_ungate": True,
                    "anchor_asin": "",
                    "anchor_sku": f"KEEPER-{clean_b[:6].replace(' ', '')}-PENDING",
                    "buy_box_reference": 20.00,
                    "last_pulse_date": "2026-11-03",
                    "active_pulse": False,
                    "pulse_end_date": None
                }
        save_registry(reg)
    except Exception as e:
        logger.debug(f"Auto-seed registry notice: {e}")


def send_telegram_telemetry(vendor: str, total_rows: int, valid_costs: int, output_file: Path):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    msg = (
        f"📊 <b>CATALOG NORMALIZED: {vendor}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📁 <b>Output:</b> <code>{output_file.name}</code>\n"
        f"📦 <b>Total Rows Processed:</b> {total_rows:,}\n"
        f"✅ <b>Valid Normalized SKUs:</b> {valid_costs:,}\n"
        f"⚡ <b>Ready for In-Memory Sourcing Index (Dual MPN Indexed)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=8
        )
    except Exception as e:
        logger.warning(f"Telegram telemetry notice: {e}")


def load_dataframe_safely(input_path: Path) -> Optional[pd.DataFrame]:
    """Reads spreadsheet/csv with automatic header row sniffing for empty/banner headers."""
    suffix = input_path.suffix.lower()
    if suffix in (".xlsx", ".xls"):
        header_row = 0
        try:
            # Sniff first 10 rows to detect real headers (e.g., Frontier starts on row 1)
            preview = pd.read_excel(input_path, header=None, nrows=10)
            for idx, row in preview.iterrows():
                vals = [str(v).lower() for v in row.values if pd.notna(v)]
                if any(any(k in v for k in ["upc", "sku", "item number", "whls price", "unit price", "cost", "price"]) for v in vals):
                    header_row = idx
                    break
        except Exception:
            header_row = 0
        return pd.read_excel(input_path, header=header_row, dtype=str)

    elif suffix == ".parquet":
        return pd.read_parquet(input_path)
    else:
        try:
            return pd.read_csv(input_path, dtype=str, low_memory=False, encoding="utf-8", on_bad_lines="skip")
        except Exception:
            return pd.read_csv(input_path, dtype=str, sep="\t", low_memory=False, encoding="latin1")


def normalize_catalog(input_path: Path, output_dir: Path, vendor_override: Optional[str] = None) -> Optional[Path]:
    if not input_path.exists():
        logger.error(f"Input file not found: {input_path}")
        return None

    vendor = (vendor_override or input_path.stem.replace("_Catalog", "").replace("_catalog", "").split("_")[0]).upper()
    logger.info(f"Processing catalog file {input_path.name} for vendor {vendor}...")

    try:
        df = load_dataframe_safely(input_path)
    except Exception as e:
        logger.error(f"Failed to read file {input_path.name}: {e}")
        return None

    if df is None or df.empty:
        logger.warning(f"File {input_path.name} is empty.")
        return None

    cols_map = {re.sub(r'[^a-z0-9]', '', str(c).lower()): c for c in df.columns}

    def find_col(candidates: List[str]) -> Optional[str]:
        for cand in candidates:
            cleaned = re.sub(r'[^a-z0-9]', '', cand)
            if cleaned in cols_map:
                return cols_map[cleaned]
        for cand in candidates:
            cleaned = re.sub(r'[^a-z0-9]', '', cand)
            for col_clean, col_orig in cols_map.items():
                if cleaned in col_clean:
                    return col_orig
        return None

    upc_col = find_col(["upc", "barcode", "gtin", "gtin14", "ean"])
    sku_col = find_col([
        "clean_mpn", "mpn", "itemnumber", "item_number", "partnumber", 
        "part_number", "part_no", "item_no", "sku", "vendor_sku", "vendorpartnumber"
    ])
    cost_col = find_col([
        "unitprice", "unit_price", "whlsprice", "whls_price", "wholesale_cost",
        "wholesalecost", "unit_cost", "unitcost", "cost", "dealer_price", "price",
        "pricecontarplus", "net_price"
    ])
    title_col = find_col(["longproductname", "product_title", "title", "description", "item_description", "product_name", "desc"])
    brand_col = find_col(["brand", "manufacturer", "mfr", "vendor"])
    uom_col = find_col(["uom", "unit_of_measure", "unit", "pkg", "packaging"])
    pack_col = find_col(["casecount", "case_count", "pack_qty", "pack", "case_qty", "qty_per_pack", "inner_pack"])

    if not cost_col:
        logger.error(f"Cannot normalize {input_path.name}: No recognizable wholesale price/cost column found.")
        return None

    logger.info(f"Mapping detected for {input_path.name}: UPC='{upc_col}', SKU='{sku_col}', Cost='{cost_col}', Title='{title_col}'")

    records = []
    discovered_brands = set()

    for _, row in df.iterrows():
        raw_cost = row.get(cost_col)
        if pd.isna(raw_cost):
            continue

        raw_cost_str = re.sub(r'[^0-9.]', '', str(raw_cost).strip())
        try:
            cost_val = float(raw_cost_str)
        except ValueError:
            continue

        if cost_val <= 0:
            continue

        title_val = str(row.get(title_col, "")).strip() if title_col and pd.notna(row.get(title_col)) else ""
        uom_raw = str(row.get(uom_col, "EA")).strip().upper() if uom_col and pd.notna(row.get(uom_col)) else "EA"
        uom_mult = UOM_MAP.get(uom_raw, 1)

        if uom_mult is None:
            pack_from_col = None
            if pack_col and pd.notna(row.get(pack_col)):
                try:
                    pack_from_col = int(float(re.sub(r'[^0-9.]', '', str(row[pack_col]))))
                except ValueError:
                    pass
            uom_mult = pack_from_col or parse_pack_from_text(title_val)

        unit_cost = round(cost_val / max(1, uom_mult), 4)

        raw_upc = row.get(upc_col) if upc_col else ""
        barcode_14 = normalize_barcode(raw_upc)
        raw_upc_clean = re.sub(r'[^0-9]', '', str(raw_upc).strip())

        raw_sku = row.get(sku_col) if sku_col else ""
        sku_val = clean_mpn(raw_sku)
        root_sku = extract_root_mpn(sku_val)

        raw_brand = row.get(brand_col) if brand_col else vendor
        brand_clean = clean_brand_name(raw_brand) or vendor
        if brand_clean:
            discovered_brands.add(brand_clean)

        records.append({
            "vendor": vendor,
            "brand": brand_clean,
            "clean_mpn": sku_val,
            "root_mpn": root_sku,
            "gtin14": barcode_14,
            "upc_raw": raw_upc_clean,
            "title": title_val,
            "unit_cost": unit_cost,
            "raw_cost": cost_val,
            "uom": uom_raw,
            "pack_qty": max(1, uom_mult)
        })

    if not records:
        logger.warning(f"No valid records extracted from {input_path.name}.")
        return None

    # Auto-seed discovered brands into the anti-regate registry
    auto_seed_brands_to_registry(discovered_brands, vendor)

    out_df = pd.DataFrame(records)
    output_dir.mkdir(parents=True, exist_ok=True)

    target_parquet = output_dir / f"{vendor.lower()}_normalized.parquet"
    target_csv = output_dir / f"{vendor.lower()}_normalized.csv"

    saved_file = None
    try:
        out_df.to_parquet(target_parquet, index=False)
        saved_file = target_parquet
        logger.info(f"Saved {len(records):,} records to Parquet: {target_parquet.name}")
    except (ImportError, Exception) as e:
        logger.warning(f"Parquet engine unavailable ({e}). Falling back to CSV...")
        out_df.to_csv(target_csv, index=False)
        saved_file = target_csv
        logger.info(f"Saved {len(records):,} records to CSV fallback: {target_csv.name}")

    send_telegram_telemetry(vendor, len(df), len(records), saved_file)
    return saved_file


def main():
    parser = argparse.ArgumentParser(description="Normalize catalog spreadsheets to canonical indexed tables.")
    parser.add_argument("input_file", type=str, help="Path to raw catalog spreadsheet (CSV/XLSX/Parquet)")
    parser.add_argument("output_dir", type=str, nargs="?", default="data/catalogs/wholesale_catalogs/indexed", help="Output directory")
    parser.add_argument("vendor_override", type=str, nargs="?", default=None, help="Vendor name override")

    args = parser.parse_args()
    in_path = Path(args.input_file)
    out_dir = Path(args.output_dir)

    result = normalize_catalog(in_path, out_dir, args.vendor_override)
    if not result:
        sys.exit(1)


if __name__ == "__main__":
    main()
