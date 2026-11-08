#!/usr/bin/env python3
"""
tools/bci_ungating_cart_builder.py
Builds an optimized $750 BCI canary order across 15-20 target brands:
- >=10 units per brand line item (Amazon brand ungating standard)
- Sub-$4.00 single-unit wholesale cost
- Total cart value target: $750.00 - $780.00 (hits MOQ cleanly)
- Exports CSV order sheet and Seller Central flat-file canary loader
"""

import os
import sys
import glob
import json
import pandas as pd
import numpy as np

DATA_DIR = "/app/data"
OUTPUT_DIR = "/app/data/reports"
os.makedirs(OUTPUT_DIR, exist_ok=True)

def find_bci_catalog():
    patterns = [
        os.path.join(DATA_DIR, "catalogs", "**", "*bci*.xlsx"),
        os.path.join(DATA_DIR, "catalogs", "**", "*bci*.csv"),
        os.path.join(DATA_DIR, "**", "*bci*.xlsx"),
        os.path.join(DATA_DIR, "**", "*bci*.csv")
    ]
    for p in patterns:
        matches = glob.glob(p, recursive=True)
        if matches:
            return matches[0]
    return None

def main():
    cat_path = find_bci_catalog()
    df = None
    
    if cat_path:
        print(f"[*] Found BCI catalog at: {cat_path}")
        if cat_path.endswith(".xlsx"):
            df = pd.read_excel(cat_path)
        else:
            df = pd.read_csv(cat_path)
    else:
        # Fallback to cross_vendor_price_matrix.json if direct file missing
        matrix_path = os.path.join(DATA_DIR, "cross_vendor_price_matrix.json")
        if os.path.exists(matrix_path):
            print(f"[*] Reading BCI items from price matrix: {matrix_path}")
            with open(matrix_path) as f:
                raw = json.load(f)
            records = []
            for k, v in raw.items():
                if isinstance(v, dict) and "bci" in str(v.get("vendor", "")).lower():
                    records.append(v)
            if records:
                df = pd.DataFrame(records)

    if df is None or df.empty:
        print("[-] Could not locate BCI items in catalog directories.")
        print("[*] Checked catalog folders:")
        for root, dirs, files in os.walk(DATA_DIR):
            for f in files:
                if any(x in f.lower() for x in ["bci", "catalog", "leads"]):
                    print(f"    - {os.path.join(root, f)}")
        return

    # Normalize column names
    col_map = {}
    for c in df.columns:
        cl = str(c).lower().strip()
        if "brand" in cl:
            col_map[c] = "brand"
        elif any(x in cl for x in ["upc", "barcode"]):
            col_map[c] = "upc"
        elif any(x in cl for x in ["asin"]):
            col_map[c] = "asin"
        elif any(x in cl for x in ["sku", "item", "part", "mpn"]):
            if "sku" not in col_map.values():
                col_map[c] = "sku"
        elif any(x in cl for x in ["cost", "price", "wholesale"]):
            if "cost" not in col_map.values():
                col_map[c] = "cost"
        elif any(x in cl for x in ["desc", "title", "name"]):
            if "title" not in col_map.values():
                col_map[c] = "title"

    df = df.rename(columns=col_map)
    required = ["brand", "cost"]
    for req in required:
        if req not in df.columns:
            print(f"[-] Column '{req}' not found in catalog. Available: {df.columns.tolist()}")
            return

    # Clean numeric costs
    df["cost"] = pd.to_numeric(df["cost"].astype(str).str.replace(r"[\$,]", "", regex=True), errors="coerce").fillna(0.0)
    df = df[df["cost"] > 0]
    df["brand"] = df["brand"].astype(str).str.strip()
    df = df[df["brand"].str.len() > 1]

    # Filter for sub-$4.00 sacrifice/ungating line items
    cheap_df = df[(df["cost"] >= 0.50) & (df["cost"] <= 4.00)].copy()
    if cheap_df.empty:
        print("[!] No items <= $4.00 found. Expanding ceiling to <= $7.50.")
        cheap_df = df[(df["cost"] >= 0.50) & (df["cost"] <= 7.50)].copy()

    # Pick the cheapest item per unique brand
    best_per_brand = cheap_df.sort_values("cost").groupby("brand").first().reset_index()

    print(f"[*] Discovered {len(best_per_brand)} unique brands with eligible items.")

    # Target: 18-20 brands at 10 units each, topping up to hit $750 MOQ
    target_moq = 750.00
    selected = []
    current_total = 0.0

    for _, row in best_per_brand.iterrows():
        line_qty = 10
        line_cost = round(row["cost"] * line_qty, 2)
        selected.append({
            "brand": row["brand"],
            "sku": row.get("sku", f"BCI-{row['brand'][:4].upper()}-01"),
            "upc": str(row.get("upc", "")),
            "asin": str(row.get("asin", "")),
            "title": str(row.get("title", f"{row['brand']} Wholesale Line Item"))[:80],
            "unit_cost": round(row["cost"], 2),
            "quantity": line_qty,
            "line_total": line_cost
        })
        current_total += line_cost
        if current_total >= target_moq and len(selected) >= 15:
            break

    # If below $750, increase quantities incrementally
    idx = 0
    while current_total < target_moq and selected:
        selected[idx % len(selected)]["quantity"] += 2
        selected[idx % len(selected)]["line_total"] = round(
            selected[idx % len(selected)]["unit_cost"] * selected[idx % len(selected)]["quantity"], 2
        )
        current_total = sum(item["line_total"] for item in selected)
        idx += 1

    cart_df = pd.DataFrame(selected)

    print("\n" + "=" * 70)
    print(f"BCI $750 CANARY UNGATING CART OPTIMIZED")
    print(f"  [+] Unique Brands Targeted: {len(cart_df)}")
    print(f"  [+] Total Units Staged: {cart_df['quantity'].sum()}")
    print(f"  [+] Total Cart Spend: ${current_total:,.2f} (BCI $750 MOQ Met)")
    print("=" * 70)

    print(cart_df[["brand", "unit_cost", "quantity", "line_total"]].to_string(index=False))

    # Export Purchase Order CSV
    po_path = os.path.join(OUTPUT_DIR, "bci_canary_order_750.csv")
    cart_df.to_csv(po_path, index=False)
    print(f"\n[+] Saved BCI Canary PO to: {po_path}")

    # Generate Seller Central Inventory Loader Flat File
    loader_path = os.path.join(OUTPUT_DIR, "Inventory_Loader_Canary.txt")
    with open(loader_path, "w") as f:
        # Standard Amazon Inventory Loader header
        f.write("sku\tprice\tquantity\tproduct-id\tproduct-id-type\tcondition-type\tleadtime-to-ship\n")
        for item in selected:
            prod_id = item["asin"] if len(item["asin"]) == 10 else item["upc"]
            id_type = "1" if len(item["asin"]) == 10 else "4"  # 1 = ASIN, 4 = UPC
            # Stage as $99.99 with 0 quantity (zero-stock canary)
            f.write(f"{item['sku']}\t99.99\t0\t{prod_id}\t{id_type}\t11\t2\n")
    print(f"[+] Saved 0-stock Seller Central Canary flat file to: {loader_path}")

if __name__ == "__main__":
    main()
