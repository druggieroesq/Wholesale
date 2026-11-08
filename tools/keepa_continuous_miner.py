#!/usr/bin/env python3
"""
Keepa Continuous ASIN Discovery & Cache Refresh Daemon
------------------------------------------------------
1. Loops through previously captured ASINs in candidate_cache to keep sales rank & BB live.
2. Continuously mines new high-turn Industrial & MRO categories using Keepa Product Finder.
3. Automatically ingests new ASINs directly into hunter_engine.db -> candidate_cache.
"""

import os
import time
import json
import sqlite3
import requests

KEEPA_KEY = os.getenv("KEEPA_API_KEY", "")
DB_PATH = os.getenv("DB_PATH", "/app/data/hunter_engine.db")

TARGET_CATEGORIES = [
    "228013",      # Tools & Home Improvement
    "16310091",    # Industrial & Scientific
    "256243011",   # Janitorial & Sanitation Supplies
    "318026011",   # Material Handling Products
    "318029011"    # Commercial Door Hardware & Locks
]

def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def refresh_existing_asins(limit=100):
    if not KEEPA_KEY:
        return

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT asin FROM candidate_cache 
        ORDER BY last_updated ASC 
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()

    if not rows:
        return

    asins = [r[0] for r in rows]
    print(f"[*] [Keepa Refresh] Updating metrics for {len(asins)} previously captured ASINs...")

    url = f"https://api.keepa.com/product?key={KEEPA_KEY}&domain=1&asin={','.join(asins)}&stats=90"
    try:
        r = requests.get(url, timeout=30)
        if r.status_code == 200:
            data = r.json()
            products = data.get("products", [])
            conn = get_db_connection()
            cur = conn.cursor()
            for p in products:
                asin = p.get("asin")
                stats = p.get("stats", {})
                current_bb = (stats.get("buyBoxPrice", 0) or 0) / 100.0
                sales_rank = stats.get("current", [0]*4)[3] if len(stats.get("current", [])) > 3 else 50000
                fba_sellers = stats.get("buyBoxIsFBA", 0)
                monthly_sold = max(10, int(round(120000 / max(100, sales_rank)))) if sales_rank > 0 else 25

                cur.execute("""
                    UPDATE candidate_cache 
                    SET current_bb = ?, sales_rank = ?, num_sellers = ?, monthly_sold = ?, last_updated = CURRENT_TIMESTAMP
                    WHERE asin = ?
                """, (current_bb, sales_rank, fba_sellers, monthly_sold, asin))
            conn.commit()
            conn.close()
            print(f"[+] [Keepa Refresh] Successfully updated {len(products)} ASINs.")
    except Exception as e:
        print(f"[!] Keepa refresh error: {e}")

def mine_new_category_products():
    if not KEEPA_KEY:
        return

    print("[*] [Keepa Discovery] Mining new high-turn catalog leads...")
    for cat_id in TARGET_CATEGORIES:
        query_payload = {
            "rootCategory": int(cat_id),
            "current_SALES_gte": 1,
            "current_SALES_lte": 65000,
            "current_BUY_BOX_SHIPPING_gte": 3000,
            "fbaCount_lte": 4,
            "sort": [["current_SALES", "asc"]],
            "perPage": 50
        }
        
        url = f"https://api.keepa.com/query?key={KEEPA_KEY}&domain=1&selection={requests.utils.quote(json.dumps(query_payload))}"
        try:
            r = requests.get(url, timeout=30)
            if r.status_code == 200:
                data = r.json()
                new_asins = data.get("asinList", [])
                if new_asins:
                    conn = get_db_connection()
                    cur = conn.cursor()
                    inserted = 0
                    for asin in new_asins:
                        cur.execute("""
                            INSERT OR IGNORE INTO candidate_cache (asin, status, last_updated)
                            VALUES (?, 'DISCOVERED', CURRENT_TIMESTAMP)
                        """, (asin,))
                        if cur.rowcount > 0:
                            inserted += 1
                    conn.commit()
                    conn.close()
                    print(f"[+] [Keepa Discovery] Cat {cat_id}: Cached {inserted} new ASINs.")
        except Exception as e:
            print(f"[!] Keepa discovery error on Cat {cat_id}: {e}")
        time.sleep(3.0)

def main():
    print("=" * 60)
    print("KEEPA CONTINUOUS ASIN DISCOVERY & REFRESH DAEMON ACTIVE")
    print("=" * 60)
    while True:
        try:
            refresh_existing_asins(limit=100)
            time.sleep(5.0)
            mine_new_category_products()
            print("[*] Cycle complete. Resting 120s...\n")
            time.sleep(120.0)
        except Exception as loop_err:
            print(f"[!] Loop error: {loop_err}")
            time.sleep(30.0)

if __name__ == "__main__":
    main()
