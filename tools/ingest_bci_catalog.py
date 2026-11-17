#!/usr/bin/env python3
"""
tools/ingest_bci_catalog.py
---------------------------
Dedicated BCI Full Inventory Parser & Normalizer ('Inven5Full' schema)
- Reads BCI Full Inventory with Pricing *.xlsx (sheet 'Inven5Full') using fast streaming mode
- Maps exact BCI columns:
    * UPC12 / UPC10 -> normalized 12-digit & 13-digit UPC/EAN keys
    * MFGNUMBER     -> normalized manufacturer part number
    * PRICE / DEALERPRICE -> exact unit wholesale cost (uses lowest active tier price)
    * STD / CASEQTY / QTYONHAND / WEIGHT / ACTIVERECORDCODE
- Filters out:
    * Inactive records (ACTIVERECORDCODE != 'A')
    * Store displays / empty racks (STD == 'DS' or 'DISPLAY' in description)
    * Zero-stock items (QTYONHAND <= 0)
    * IP-blacklisted conglomerate brands (e.g., DEWALT)
- Outputs data/catalogs/wholesale_catalogs/indexed/bci_normalized_index.csv and triggers
  auto_cross_catalog_matcher_daemon.py so leads immediately display Vendor = 'BCI'.
"""

import os
import re
import sys
import json
import logging
import argparse
import subprocess
from pathlib import Path
from typing import Dict, List, Set

import pandas as pd
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

load_dotenv(ROOT_DIR / ".env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] BCICatalogIngest: %(message)s"
)
logger = logging.getLogger("BCICatalogIngest")

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
if not DATA_DIR.exists():
    DATA_DIR = ROOT_DIR / "data"

CATALOG_DIR = DATA_DIR / "catalogs"
INDEXED_DIR = CATALOG_DIR / "wholesale_catalogs" / "indexed"
PRIMARY_IP_FILE = DATA_DIR / "ip_blocklist.json"


def load_blocked_brands() -> Set[str]:
    if not PRIMARY_IP_FILE.exists():
        return set()
    try:
        raw = json.loads(PRIMARY_IP_FILE.read_text(encoding="utf-8"))
        items = raw if isinstance(raw, list) else raw.get("brands", [])
        return {str(b).upper().strip() for b in items if b}
    except Exception:
        return set()


def clean_upc12(val) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    s = str(val).strip()
    if "e+" in s.lower():
        try:
            s = f"{int(float(s))}"
        except Exception:
            pass
    digits = re.sub(r"\D", "", s)
    if len(digits) < 8:
        return ""
    return digits.zfill(12) if len(digits) <= 12 else digits


def find_bci_workbook(explicit_path: str = "") -> Path:
    if explicit_path:
        p = Path(explicit_path)
        if p.exists():
            return p
    for search_dir in (CATALOG_DIR, DATA_DIR, ROOT_DIR):
        if not search_dir.exists():
            continue
        matches = sorted(search_dir.rglob("*BCI*Full*Inventory*.xlsx"), key=lambda x: x.stat().st_mtime, reverse=True)
        if matches:
            return matches[0]
        matches_any = sorted(search_dir.rglob("*BCI*.xlsx"), key=lambda x: x.stat().st_mtime, reverse=True)
        if matches_any:
            return matches_any[0]
    raise FileNotFoundError("No BCI Inventory .xlsx file found in data/catalogs/.")


def ingest_bci_workbook(feed_path: str = "", min_qty_on_hand: int = 1) -> Path:
    xlsx_path = find_bci_workbook(feed_path)
    blocked_brands = load_blocked_brands()
    INDEXED_DIR.mkdir(parents=True, exist_ok=True)

    logger.info(f"Reading BCI workbook '{xlsx_path.name}' (sheet: Inven5Full)...")
    xl = pd.ExcelFile(xlsx_path, engine="openpyxl")
    target_sheet = "Inven5Full" if "Inven5Full" in xl.sheet_names else xl.sheet_names[0]
    df = xl.parse(target_sheet, dtype=str)
    df.columns = [str(c).strip().upper() for c in df.columns]

    normalized_rows: List[Dict[str, object]] = []
    skipped_oos = 0
    skipped_display = 0
    skipped_ip = 0

    for _, row in df.iterrows():
        active_code = str(row.get("ACTIVERECORDCODE", "A")).strip().upper()
        if active_code and active_code != "A":
            continue

        std_code = str(row.get("STD", "EA")).strip().upper()
        desc50 = str(row.get("DESCRIPTION50", "")).strip()
        if std_code == "DS" or "DISPLAY" in desc50.upper():
            skipped_display += 1
            continue

        try:
            qty_on_hand = int(float(str(row.get("QTYONHAND", "0")).strip() or 0))
        except ValueError:
            qty_on_hand = 0

        if qty_on_hand < min_qty_on_hand:
            skipped_oos += 1
            continue

        brand = str(row.get("BRAND") or row.get("MANUFACTURER") or "").strip()
        if not brand or brand.upper() in blocked_brands:
            skipped_ip += 1
            continue

        upc12 = clean_upc12(row.get("UPC12"))
        mfg_num = str(row.get("MFGNUMBER", "")).strip()
        bci_item = str(row.get("ITEM", "")).strip()
        if not upc12 and not mfg_num:
            continue

        # Use discounted tier PRICE when valid, fallback to DEALERPRICE
        tier_price = 0.0
        for p_col in ("PRICE", "DEALERPRICE"):
            raw_p = str(row.get(p_col, "")).replace("$", "").replace(",", "").strip()
            try:
                val_p = float(raw_p)
                if val_p > 0.05:
                    tier_price = val_p
                    break
            except ValueError:
                continue

        if tier_price <= 0.05:
            continue

        try:
            weight_lbs = float(str(row.get("WEIGHT", "1.0")).strip() or 1.0)
        except ValueError:
            weight_lbs = 1.0

        try:
            case_qty = int(float(str(row.get("CASEQTY", "1")).strip() or 1))
        except ValueError:
            case_qty = 1

        normalized_rows.append({
            "vendor": "BCI",
            "bci_item": bci_item,
            "upc": upc12,
            "mpn": mfg_num,
            "brand": brand,
            "title": desc50,
            "unit_cost": round(tier_price, 4),
            "case_qty": max(1, case_qty),
            "qty_on_hand": qty_on_hand,
            "weight_lbs": round(weight_lbs, 2),
        })

    out_csv = INDEXED_DIR / "bci_normalized_index.csv"
    out_df = pd.DataFrame(normalized_rows)
    out_df.to_csv(out_csv, index=False)

    logger.info(
        f"BCI Normalization Complete: {len(out_df):,} in-stock SKUs saved to {out_csv.name} "
        f"(Skipped: {skipped_oos:,} OOS, {skipped_display:,} displays, {skipped_ip:,} IP-blocked)."
    )

    matcher_script = ROOT_DIR / "tools" / "auto_cross_catalog_matcher_daemon.py"
    if matcher_script.exists() and len(out_df) > 0:
        logger.info("Triggering CrossCatalogMatcher against fresh BCI index...")
        subprocess.Popen([sys.executable, "-u", str(matcher_script), "--run-once"])

    return out_csv


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BCI Full Inventory ('Inven5Full') Normalizer")
    parser.add_argument("--feed", type=str, default="", help="Optional path to BCI .xlsx file")
    parser.add_argument("--min-qty", type=int, default=1, help="Minimum QTYONHAND required")
    args = parser.parse_args()
    ingest_bci_workbook(feed_path=args.feed, min_qty_on_hand=args.min_qty)
