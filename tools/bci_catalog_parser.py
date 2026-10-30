"""
tools/bci_catalog_parser.py
Filters BCI inventory against the 318-brand IP firewall and standard FBA physical limits.
Outputs a clean scanning queue in data/catalogs/bci_ready_for_keepa.json.
"""

import os
import sys
import json
import logging
import pandas as pd
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] BCICatalog: %(message)s"
)
logger = logging.getLogger("BCICatalog")

from tools.ip_guard import is_brand_blocked

INVENTORY_EXCEL = ROOT_DIR / "BCI Full Inventory with Pricing 10.12.26.xlsx"
OUTPUT_QUEUE = ROOT_DIR / "data" / "catalogs" / "bci_ready_for_keepa.json"


def prepare_bci_scan_queue():
    if not INVENTORY_EXCEL.exists():
        logger.error(f"Catalog not found at {INVENTORY_EXCEL}")
        return

    logger.info(f"Loading {INVENTORY_EXCEL.name}...")
    df = pd.read_excel(INVENTORY_EXCEL)
    logger.info(f"Raw SKUs loaded: {len(df)}")

    valid_queue = []
    dropped_ip_count = 0
    dropped_spec_count = 0

    for _, row in df.iterrows():
        brand = str(row.get("BRAND", "")).strip()
        vendor = str(row.get("VENDORNAME", "")).strip()
        title = str(row.get("DESCRIPTION50", "")).strip()
        
        # 1. IP Firewall Check
        blocked, reason = is_brand_blocked(brand=brand, title=title, vendor=vendor)
        if blocked:
            dropped_ip_count += 1
            continue

        # 2. Stock and Physical Specs Check
        qty_on_hand = pd.to_numeric(row.get("QTYONHAND", 0), errors="coerce") or 0
        dealer_cost = pd.to_numeric(row.get("DEALERPRICE", 0), errors="coerce") or 0.0
        weight = pd.to_numeric(row.get("WEIGHT", 0), errors="coerce") or 0.0
        ups_shippable = str(row.get("UPS_SHIPPABLE", "N")).strip().upper()

        if qty_on_hand < 10 or dealer_cost <= 0 or ups_shippable != "Y" or weight > 15.0:
            dropped_spec_count += 1
            continue

        upc_raw = str(row.get("UPC12", "")).split(".")[0].strip()
        if not upc_raw or upc_raw == "nan":
            continue

        valid_queue.append({
            "distributor": "BCI",
            "item_number": str(row.get("ITEM", "")),
            "upc": upc_raw.zfill(12),
            "brand": brand,
            "title": title,
            "wholesale_cost": round(float(dealer_cost), 2),
            "stock_qty": int(qty_on_hand),
            "weight_lbs": float(weight)
        })

    logger.info(
        f"BCI catalog filtered: "
        f"{dropped_ip_count} dropped by IP guard, "
        f"{dropped_spec_count} dropped by stock/weight specs. "
        f"{len(valid_queue)} clean SKUs queued for Keepa."
    )

    OUTPUT_QUEUE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_QUEUE, "w", encoding="utf-8") as f:
        json.dump(valid_queue, f, indent=2)

    logger.info(f"Queued scan payload saved to {OUTPUT_QUEUE}")

if __name__ == "__main__":
    prepare_bci_scan_queue()
