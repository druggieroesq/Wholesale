import os
import sqlite3
import pandas as pd
import requests
from typing import List, Dict

DB_PATH = "data/hunter_engine.db"
TARGET_BRANDS = ["GATES", "TIMKEN", "SKF", "PARKER", "BROWNING", "SEALMASTER", "LOVEJOY"]

def init_candidate_table():
    """Ensure candidate_cache table is ready for multi-distributor ingestion."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS candidate_cache (
            sku TEXT PRIMARY KEY,
            distributor TEXT,
            brand TEXT,
            mpn TEXT,
            title TEXT,
            wholesale_cost REAL,
            in_stock INTEGER,
            branch_pickup TEXT,
            asin TEXT,
            status TEXT DEFAULT 'PENDING_EVAL',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def ingest_motion_csv(file_path: str):
    """
    Parses Motion wholesale CSV catalog export or price sheet,
    filters by positive-margin target brands, and writes to database.
    """
    init_candidate_table()
    df = pd.read_csv(file_path)
    
    # Standardize column headers
    df.columns = [c.strip().lower() for c in df.columns]
    
    # Expected columns: motion_sku, brand, mpn, item_description, net_price, qty_avail
    records_to_insert = []
    
    for _, row in df.iterrows():
        brand = str(row.get('brand', '')).upper().strip()
        
        # Priority brand filtering to avoid ingesting low-velocity non-standard SKUs
        if any(target in brand for target in TARGET_BRANDS):
            cost = float(row.get('net_price', 0.0) or 0.0)
            if cost <= 0:
                continue
                
            sku = str(row.get('motion_sku', row.get('item_number', ''))).strip()
            mpn = str(row.get('mpn', row.get('mfr_part_number', ''))).strip()
            title = str(row.get('item_description', '')).strip()
            qty = int(row.get('qty_avail', 0) or 0)
            
            records_to_insert.append((
                f"MOTION_{sku}",
                "MOTION",
                brand,
                mpn,
                title,
                cost,
                1 if qty > 0 else 0,
                "POMPANO_SAMPLE_RD",
                None,
                "PENDING_EVAL"
            ))
            
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.executemany("""
        INSERT OR REPLACE INTO candidate_cache 
        (sku, distributor, brand, mpn, title, wholesale_cost, in_stock, branch_pickup, asin, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, records_to_insert)
    conn.commit()
    conn.close()
    
    print(f"Ingested {len(records_to_insert)} high-priority Motion candidate SKUs into candidate_cache.")

if __name__ == "__main__":
    init_candidate_table()
