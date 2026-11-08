#!/usr/bin/env python3
"""
tools/resurrect_cached_multipacks.py
Zero-Token Multipack & 0-FBA Prime Steal Cache Rescanner.

Scans cached Keepa products in /app/data/keepa_cache.db without burning API tokens:
1. Asymmetric Multipack Arbitrage: pack_size >= 2 with <= 2 FBA sellers.
2. 0-FBA Prime Steals: FBA count = 0, FBM count >= 1, brand-monopoly guard (>75% similarity check).
3. Hard competitor ceiling: <= 3 FBA sellers.
"""

import os
import sys
import json
import sqlite3
import re
from difflib import SequenceMatcher

DB_PATH = "/app/data/keepa_cache.db"
OUTPUT_DIR = "/app/data/reports"
os.makedirs(OUTPUT_DIR, exist_ok=True)

def similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()

def extract_pack_size(title: str) -> int:
    if not title:
        return 1
    patterns = [
        r'\b(?:pack of|pack|pk|count of|ct of)\s*([0-9]+)\b',
        r'\b([0-9]+)\s*(?:-| )(?:pack|pk|count|ct)\b',
        r'\(([0-9]+)\s*(?:pack|pk|ct)\)'
    ]
    for pattern in patterns:
        m = re.search(pattern, title, re.IGNORECASE)
        if m:
            try:
                val = int(m.group(1))
                if 2 <= val <= 48:
                    return val
            except ValueError:
                pass
    return 1

def main():
    if not os.path.exists(DB_PATH):
        print(f"[-] SQLite cache not found at {DB_PATH}.")
        if os.path.exists("/app/data"):
            print("[*] Available in /app/data:", os.listdir("/app/data"))
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = [row[0] for row in cursor.fetchall()]
    print(f"[*] Connected to {DB_PATH}. Tables: {tables}")

    target_table = None
    for candidate in ["keepa_cache", "products", "cache", "asin_cache"]:
        if candidate in tables:
            target_table = candidate
            break
    if not target_table and tables:
        target_table = tables[0]

    if not target_table:
        print("[-] No valid tables found in database.")
        conn.close()
        return

    cursor.execute(f"SELECT COUNT(*) FROM {target_table}")
    total_records = cursor.fetchone()[0]
    print(f"[*] Scanning {total_records:,} cached ASINs in '{target_table}'...")

    cursor.execute(f"SELECT * FROM {target_table}")
    col_names = [d[0] for d in cursor.description]
    rows = cursor.fetchall()

    multipack_rescued = []
    prime_steals = []

    for row in rows:
        record = dict(zip(col_names, row))
        raw_data = record.get("data") or record.get("payload") or record.get("json") or record.get("response")
        parsed = {}
        if isinstance(raw_data, str):
            try:
                parsed = json.loads(raw_data)
            except Exception:
                parsed = record
        elif isinstance(raw_data, dict):
            parsed = raw_data
        else:
            parsed = record

        asin = parsed.get("asin") or record.get("asin", "UNKNOWN")
        title = parsed.get("title") or record.get("title", "")
        brand = parsed.get("brand") or record.get("brand", "")
        buybox_price = float(parsed.get("buyBoxPrice") or parsed.get("price") or 0.0)
        if buybox_price > 500:
            buybox_price = round(buybox_price / 100.0, 2)

        fba_count = int(parsed.get("fbaSellersCount") or parsed.get("fba_count") or parsed.get("fbaCount") or 0)
        fbm_count = int(parsed.get("fbmSellersCount") or parsed.get("fbm_count") or parsed.get("fbmCount") or 0)
        seller_names = parsed.get("seller_names") or parsed.get("sellers") or []
        if isinstance(seller_names, str):
            seller_names = [s.strip() for s in seller_names.split(",")]

        pack_size = parsed.get("pack_size") or extract_pack_size(title)

        if pack_size >= 2 and fba_count <= 2:
            multipack_rescued.append({
                "asin": asin,
                "title": title[:70],
                "brand": brand,
                "pack_size": pack_size,
                "buybox_price": buybox_price,
                "fba_sellers": fba_count,
                "fbm_sellers": fbm_count,
                "arbitrage_thesis": "FBA Pick-Fee Collapse (Multipack <=2 FBA)"
            })

        if fba_count == 0 and fbm_count >= 1:
            brand_is_seller = any(similar(brand, s) >= 0.75 for s in seller_names)
            if not brand_is_seller:
                prime_steals.append({
                    "asin": asin,
                    "title": title[:70],
                    "brand": brand,
                    "pack_size": pack_size,
                    "buybox_price": buybox_price,
                    "fba_sellers": 0,
                    "fbm_sellers": fbm_count,
                    "arbitrage_thesis": "0-FBA Prime Steal (Clean brand, 100% rotation)"
                })

    conn.close()

    print("\n" + "=" * 70)
    print(f"CACHE RESCAN COMPLETE: {total_records:,} Records Analyzed")
    print(f"  [+] Rescued Multipacks (pack >= 2, FBA <= 2): {len(multipack_rescued)}")
    print(f"  [+] 0-FBA Prime Steals (FBA = 0, Clean Brand): {len(prime_steals)}")
    print("=" * 70)

    out_file = os.path.join(OUTPUT_DIR, "resurrected_leads.json")
    with open(out_file, "w") as f:
        json.dump({"multipacks": multipack_rescued, "prime_steals": prime_steals}, f, indent=2)
    print(f"\n[+] Full report saved to: {out_file}")

if __name__ == "__main__":
    main()
