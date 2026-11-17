#!/usr/bin/env python3
"""
ingest_motion_catalog.py (Checklist Item #320)
----------------------------------------------
Normalizes Motion.com product/quote feeds (CSV or XLSX) into:
  /app/data/catalogs/wholesale_catalogs/indexed/motion_industries_normalized.parquet
Strictly targets standalone non-conglomerate industrial OEMs (Gates, Timken, SKF, Parker Hannifin,
Lovejoy, Browning, Martin Sprocket, Dodge, Baldor) and enforces the 45-lb standard parcel ceiling.
"""

import os
import re
import sys
import json
import logging
import argparse
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

load_dotenv(ROOT_DIR / ".env", override=True)

from core.engine import is_over_weight_limit

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] MotionIngest: %(message)s"
)
logger = logging.getLogger("MotionIngest")

DEFAULT_INDEXED_DIR = Path(os.getenv("INDEXED_CATALOG_DIR", "/app/data/catalogs/wholesale_catalogs/indexed"))
if not DEFAULT_INDEXED_DIR.parent.exists():
    DEFAULT_INDEXED_DIR = ROOT_DIR / "data" / "catalogs" / "wholesale_catalogs" / "indexed"

DEFAULT_BLOCKLIST_PATH = Path(os.getenv("IP_BLOCKLIST_PATH", "/app/data/ip_blocklist.json"))
if not DEFAULT_BLOCKLIST_PATH.exists():
    DEFAULT_BLOCKLIST_PATH = ROOT_DIR / "data" / "ip_blocklist.json"

APPROVED_MOTION_OEMS = {
    "GATES",
    "TIMKEN",
    "SKF",
    "PARKER",
    "PARKER HANNIFIN",
    "LOVEJOY",
    "BROWNING",
    "MARTIN SPROCKET",
    "DODGE",
    "BALDOR",
    "NTN",
    "NSK",
    "IKO",
    "MCGILL",
}


def clean_alnum(val: Any) -> str:
    if val is None or pd.isna(val):
        return ""
    s = str(val).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def safe_float(val: Any) -> float:
    if val is None or pd.isna(val):
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace("$", "").replace(",", "")
    m = re.search(r"[-+]?\d*\.\d+|\d+", s)
    return float(m.group(0)) if m else 0.0


def parse_pack_qty(desc: str) -> int:
    m = re.search(r"(?:PK|PACK|BOX|CASE)\s*(?:OF)?\s*(\d+)", str(desc or ""), re.IGNORECASE)
    if m:
        val = int(m.group(1))
        return val if 1 <= val <= 48 else 1
    return 1


def load_blocked_brands() -> set:
    if not DEFAULT_BLOCKLIST_PATH.exists():
        return set()
    try:
        raw = json.loads(DEFAULT_BLOCKLIST_PATH.read_text(encoding="utf-8"))
        items = raw if isinstance(raw, list) else raw.get("brands", [])
        return {str(b).upper().strip() for b in items if b}
    except Exception:
        return set()


def ingest_motion_feed(
    feed_path: Path,
    est_freight_per_unit: float = 0.65,
    output_parquet: Optional[Path] = None,
) -> Path:
    if not feed_path.exists():
        raise FileNotFoundError(f"Motion catalog feed not found: {feed_path}")

    if feed_path.suffix.lower() in (".xlsx", ".xls"):
        df_raw = pd.read_excel(feed_path)
    else:
        df_raw = pd.read_csv(feed_path, dtype=str)

    blocked = load_blocked_brands()
    cols_lower = {c.strip().lower(): c for c in df_raw.columns}

    def pick_col(*candidates: str) -> Optional[str]:
        for cand in candidates:
            for k_low, orig in cols_lower.items():
                if cand in k_low:
                    return orig
        return None

    col_brand = pick_col("brand", "manufacturer", "mfg")
    col_mpn = pick_col("mpn", "manufacturer part", "part number", "part #")
    col_sku = pick_col("motion item", "item #", "sku")
    col_upc = pick_col("upc", "gtin", "barcode")
    col_title = pick_col("description", "title", "item name")
    col_price = pick_col("unit price", "net price", "price", "cost")
    col_avail = pick_col("availability", "stock", "status")
    col_weight = pick_col("weight", "lbs")
    col_pack = pick_col("pack", "qty")

    normalized_rows: List[Dict[str, Any]] = []

    for _, row in df_raw.iterrows():
        brand_raw = str(row.get(col_brand, "") if col_brand else "").strip().upper()
        if not brand_raw or brand_raw in blocked or brand_raw not in APPROVED_MOTION_OEMS:
            continue

        avail = str(row.get(col_avail, "IN_STOCK") if col_avail else "IN_STOCK").upper()
        if any(x in avail for x in ("OUT", "BACKORDER", "DISCONTINUED")):
            continue

        mpn_raw = str(row.get(col_mpn, "") if col_mpn else "").strip().upper()
        sku_raw = str(row.get(col_sku, mpn_raw) if col_sku else mpn_raw).strip().upper()
        if not mpn_raw and not sku_raw:
            continue

        title = str(row.get(col_title, f"{brand_raw} {mpn_raw}") if col_title else f"{brand_raw} {mpn_raw}").strip()
        weight_val = safe_float(row.get(col_weight, 1.0) if col_weight else 1.0) or 1.0

        over_wt, _ = is_over_weight_limit(title=title, package_weight_raw=weight_val)
        if over_wt:
            continue

        case_price = safe_float(row.get(col_price, 0.0) if col_price else 0.0)
        if case_price <= 0.05:
            continue

        pack_qty = int(safe_float(row.get(col_pack, 0)) if col_pack else 0) or parse_pack_qty(title)
        pack_qty = max(1, min(pack_qty, 48))
        unit_cost = round((case_price / pack_qty) + est_freight_per_unit, 2)

        upc_clean = re.sub(r"\D", "", str(row.get(col_upc, "") if col_upc else ""))
        if len(upc_clean) not in (12, 13, 14):
            upc_clean = ""

        clean_m = clean_alnum(mpn_raw or sku_raw)
        normalized_rows.append({
            "vendor": "MOTION INDUSTRIES",
            "brand": brand_raw,
            "title": title,
            "sku": sku_raw,
            "mpn": mpn_raw or sku_raw,
            "clean_mpn": clean_m,
            "root_mpn": clean_m,
            "upc_raw": upc_clean,
            "upc": upc_clean,
            "gtin14": upc_clean.zfill(14) if upc_clean else "",
            "pack_qty": pack_qty,
            "case_price": case_price,
            "unit_cost": unit_cost,
            "raw_cost": unit_cost,
            "weight": weight_val,
            "availability": "IN_STOCK",
            "product_url": f"https://www.motion.com/products/sku/{sku_raw}",
        })

    DEFAULT_INDEXED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = output_parquet or (DEFAULT_INDEXED_DIR / "motion_industries_normalized.parquet")

    if not normalized_rows:
        logger.warning(f"No qualifying standalone OEM rows found in {feed_path.name}.")
        return out_path

    df_new = pd.DataFrame(normalized_rows)
    if out_path.exists():
        try:
            df_old = pd.read_parquet(out_path)
            df_new = pd.concat([df_old, df_new], ignore_index=True).drop_duplicates(subset=["sku"], keep="last")
        except Exception as exc:
            logger.warning(f"Could not merge existing {out_path.name}: {exc}")

    df_new.to_parquet(out_path, index=False)
    logger.info(f"Saved {len(df_new):,} normalized Motion Industries OEM SKUs to {out_path}")
    return out_path


def main():
    parser = argparse.ArgumentParser(description="Ingest Motion.com product feed into normalized Parquet.")
    parser.add_argument("--feed", required=True, help="Path to Motion.com CSV or XLSX catalog/quote export")
    parser.add_argument("--freight-per-unit", type=float, default=0.65, help="Estimated inbound freight/unit ($)")
    args = parser.parse_args()

    ingest_motion_feed(Path(args.feed), est_freight_per_unit=args.freight_per_unit)


if __name__ == "__main__":
    main()
