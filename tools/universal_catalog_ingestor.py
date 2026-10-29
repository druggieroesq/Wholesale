#!/usr/bin/env python3
"""
Universal Catalog CSV/Excel Ingestion Engine
Maps raw distributor catalogs into standardized indexed records:
- SKU / MPN
- Brand
- Description
- UPC / EAN
- Net Wholesale Cost (applies distributor specific discounts e.g. 10% for Global Industrial)
- Weight in LBS (strictly rejects >45.0 lbs)
"""

import csv
import os
import json
from typing import Dict, List, Any

OUTPUT_MASTER_INDEX = "data/catalogs/master_distributor_index.csv"

FIELD_ALIASES = {
    "sku": ["sku", "item #", "item number", "part number", "item_id", "product number", "item no"],
    "mpn": ["mpn", "mfr part #", "mfr part number", "manufacturer part #", "model"],
    "brand": ["brand", "manufacturer", "mfr", "make"],
    "title": ["description", "product description", "title", "product name", "item description"],
    "upc": ["upc", "ean", "barcode", "upc code"],
    "price": ["dealer price", "net price", "wholesale price", "cost", "price", "unit cost", "list price"],
    "weight": ["weight", "weight lbs", "shipping weight", "weight (lbs)", "pkg weight"]
}

def detect_column_mapping(header: List[str]) -> Dict[str, int]:
    mapping = {}
    lower_header = [h.strip().lower() for h in header]
    for standard_field, aliases in FIELD_ALIASES.items():
        for idx, col_name in enumerate(lower_header):
            if any(alias in col_name for alias in aliases):
                mapping[standard_field] = idx
                break
    return mapping

def ingest_catalog(file_path: str, distributor_id: str, base_discount: float = 0.0) -> int:
    if not os.path.exists(file_path):
        print(f"File not found: {file_path}")
        return 0

    os.makedirs(os.path.dirname(OUTPUT_MASTER_INDEX), exist_ok=True)
    file_exists = os.path.exists(OUTPUT_MASTER_INDEX)

    parsed_records = []
    rejected_heavy_count = 0

    with open(file_path, "r", encoding="utf-8-sig", errors="ignore") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            return 0

        mapping = detect_column_mapping(header)

        for row in reader:
            if not row or len(row) <= max(mapping.values(), default=0):
                continue

            # Weight extraction & 45 lb hard limit
            weight_val = 1.0
            if "weight" in mapping:
                raw_weight = row[mapping["weight"]].replace("lbs", "").replace("lb", "").strip()
                try:
                    weight_val = float(raw_weight)
                except ValueError:
                    weight_val = 1.0

            if weight_val > 45.0:
                rejected_heavy_count += 1
                continue

            # Price extraction & discount calculation
            raw_price = 0.0
            if "price" in mapping:
                price_str = row[mapping["price"]].replace("$", "").replace(",", "").strip()
                try:
                    raw_price = float(price_str)
                except ValueError:
                    raw_price = 0.0

            if raw_price <= 0.0:
                continue

            net_wholesale_cost = round(raw_price * (1.0 - base_discount), 2)

            sku = row[mapping["sku"]].strip() if "sku" in mapping else ""
            mpn = row[mapping["mpn"]].strip() if "mpn" in mapping else sku
            brand = row[mapping["brand"]].strip() if "brand" in mapping else ""
            title = row[mapping["title"]].strip() if "title" in mapping else ""
            upc = row[mapping["upc"]].strip() if "upc" in mapping else ""

            parsed_records.append({
                "distributor_id": distributor_id,
                "sku": sku,
                "mpn": mpn,
                "brand": brand,
                "title": title,
                "upc": upc,
                "list_price": raw_price,
                "net_wholesale_cost": net_wholesale_cost,
                "weight_lbs": weight_val
            })

    # Append to master catalog
    with open(OUTPUT_MASTER_INDEX, "a", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fieldnames=[
            "distributor_id", "sku", "mpn", "brand", "title", "upc", 
            "list_price", "net_wholesale_cost", "weight_lbs"
        ])
        if not file_exists:
            writer.writeheader()
        for rec in parsed_records:
            writer.writerow(rec)

    print(f"Ingested {len(parsed_records)} records from {distributor_id}. (Rejected {rejected_heavy_count} items > 45 lbs)")
    return len(parsed_records)

if __name__ == "__main__":
    sample_file = "data/catalogs/raw/sample_global_industrial.csv"
    os.makedirs(os.path.dirname(sample_file), exist_ok=True)
    with open(sample_file, "w", newline="") as sf:
        w = csv.writer(sf)
        w.writerow(["Item #", "MFR Part Number", "Manufacturer", "Product Description", "UPC Code", "List Price", "Weight Lbs"])
        w.writerow(["GI-101", "3M-7001", "3M", "Safety Respirator Half Face", "051131070012", "25.00", "0.9"])
        w.writerow(["GI-102", "DRUM-88", "Vestil", "Steel Drum Dolly 55 Gallon", "084221008811", "145.00", "52.0"])
    
    ingest_catalog(sample_file, "global_industrial", base_discount=0.10)
