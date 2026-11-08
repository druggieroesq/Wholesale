cat << 'EOF' > /app/core/engine.py
"""
core/engine.py
In-memory aggregation & fast lookup across all normalized wholesale Parquet feeds.
"""
import os
import glob
import logging
import sqlite3
import pandas as pd
from typing import Dict, Any, Optional

logger = logging.getLogger("IndustrialCatalogEngine")

class DatabaseManager:
    def __init__(self, db_path: str = "/app/data/mro_pipeline.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS scan_checkpoints (
                    catalog_name TEXT PRIMARY KEY,
                    last_offset INTEGER DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

class IndustrialCatalogEngine:
    def __init__(self, db_manager: Optional[DatabaseManager] = None, parquet_dir: str = "/app/data/catalogs/wholesale_catalogs/indexed"):
        self.db = db_manager or DatabaseManager()
        self.parquet_dir = parquet_dir
        self.catalog_barcodes: Dict[str, Dict[str, Any]] = {}
        self.catalog_brand_mpns: Dict[str, Dict[str, Any]] = {}

    def build_index(self) -> None:
        parquet_files = sorted(glob.glob(os.path.join(self.parquet_dir, "*.parquet")))
        self.catalog_barcodes.clear()
        self.catalog_brand_mpns.clear()

        for pf in parquet_files:
            try:
                df = pd.read_parquet(pf)
                cols = set(df.columns)
                for _, row in df.iterrows():
                    rec = row.to_dict()
                    for u_col in ["upc_raw", "gtin14", "upc", "barcode"]:
                        if u_col in cols:
                            upc = str(rec.get(u_col) or "").strip()
                            if upc and upc not in ("nan", "None", "", "0"):
                                self.catalog_barcodes[upc] = rec

                    brand = str(rec.get("brand") or "").strip().upper()
                    for m_col in ["clean_mpn", "root_mpn", "mpn"]:
                        if m_col in cols:
                            mpn = str(rec.get(m_col) or "").strip().upper()
                            if brand and mpn:
                                self.catalog_brand_mpns[f"{brand}:{mpn}"] = rec
            except Exception as e:
                logger.error("Error reading %s: %s", pf, e)

        print(f"Loaded {len(self.catalog_barcodes):,} barcodes and {len(self.catalog_brand_mpns):,} Brand-MPN index keys.")
EOF

cat << 'EOF' > /app/tools/parse_grainger_pdf.py
#!/usr/bin/env python3
"""
parse_grainger_pdf.py
Multiprocess chunked extractor for large Grainger PDF catalogs.
Extracts: Item #, MPN, Brand, Description, and Wholesale Cost.
"""
import os
import re
import sys
import sqlite3
import argparse
from multiprocessing import Pool, cpu_count
import pandas as pd

try:
    import pdfplumber
except ImportError:
    os.system("pip install -q pdfplumber pandas pyarrow")
    import pdfplumber

DB_PATH = "/app/data/grainger_parsed_cache.db"
OUTPUT_PARQUET = "/app/data/catalogs/wholesale_catalogs/normalized/grainger_catalog.parquet"

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS grainger_items (
                item_number TEXT PRIMARY KEY,
                mpn TEXT,
                brand TEXT,
                description TEXT,
                wholesale_cost REAL,
                page_num INTEGER
            )
        """)

def parse_page_chunk(args_tuple):
    pdf_path, start_page, end_page = args_tuple
    extracted = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            actual_end = min(end_page, len(pdf.pages))
            for p_num in range(start_page, actual_end):
                page = pdf.pages[p_num]
                text = page.extract_text() or ""
                matches = re.findall(r"([0-9A-Z]{5,8})\s+([A-Za-z0-9\-\.\/]{3,20})?\s+(.*?)\s+\$(\d+\.\d{2})", text)
                for item_num, mpn, desc, price in matches:
                    extracted.append((
                        item_num.strip(),
                        (mpn or item_num).strip(),
                        "GRAINGER",
                        desc.strip()[:100],
                        float(price),
                        p_num + 1
                    ))
    except Exception as e:
        print(f"Error on pages {start_page}-{end_page}: {e}")
    return extracted

def main():
    parser = argparse.ArgumentParser(description="Grainger PDF Parser")
    parser.add_argument("pdf", help="Path to raw Grainger PDF")
    parser.add_argument("--workers", type=int, default=4, help="Worker pool size")
    parser.add_argument("--chunk_size", type=int, default=50, help="Pages per chunk")
    args = parser.parse_args()

    if not os.path.isfile(args.pdf):
        print(f"Error: {args.pdf} not found.")
        sys.exit(1)

    init_db()
    with pdfplumber.open(args.pdf) as pdf:
        total_pages = len(pdf.pages)
    print(f"Opened {args.pdf}: {total_pages} total pages.")

    chunks = [
        (args.pdf, start, start + args.chunk_size)
        for start in range(0, total_pages, args.chunk_size)
    ]

    print(f"Dispatching {len(chunks)} chunks across {args.workers} workers...")
    total_cached = 0
    with Pool(processes=args.workers) as pool:
        for result in pool.imap_unordered(parse_page_chunk, chunks):
            if result:
                with sqlite3.connect(DB_PATH) as conn:
                    conn.executemany("""
                        INSERT OR IGNORE INTO grainger_items
                        (item_number, mpn, brand, description, wholesale_cost, page_num)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, result)
                total_cached += len(result)
                print(f"Cached batch: +{len(result)} items (Running Total: {total_cached})")

    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql_query("SELECT * FROM grainger_items", conn)

    # Standardize column schema for universal catalog aggregator
    df["vendor"] = "GRAINGER"
    df["clean_mpn"] = df["mpn"]
    df["root_mpn"] = df["item_number"]
    df["upc_raw"] = ""
    df["gtin14"] = ""
    df["unit_cost"] = df["wholesale_cost"]
    df["raw_cost"] = df["wholesale_cost"]
    df["title"] = df["description"]
    df["uom"] = "EA"
    df["pack_qty"] = 1

    os.makedirs(os.path.dirname(OUTPUT_PARQUET), exist_ok=True)
    df.to_parquet(OUTPUT_PARQUET, index=False)
    print(f"Extraction complete: {len(df):,} items exported to {OUTPUT_PARQUET}")

if __name__ == "__main__":
    main()
EOF

chmod +x /app/tools/parse_grainger_pdf.py

pip install -q gdown pdfplumber pandas pyarrow

python3 -c "
import os
import gdown

out_file = '/app/data/catalogs/wholesale_catalogs/incoming/grainger_raw.pdf'
os.makedirs(os.path.dirname(out_file), exist_ok=True)
url = 'https://drive.google.com/uc?id=1ZiV9PKmXbEzOsDaG2NPPUYDSjz_MHmqm'
print('Downloading Grainger.pdf via gdown...')
gdown.download(url, out_file, quiet=False)
sz_mb = os.path.getsize(out_file) / (1024 * 1024)
print(f'Verified file on disk: {out_file} ({sz_mb:.2f} MB)')
"

python3 /app/tools/parse_grainger_pdf.py \
  /app/data/catalogs/wholesale_catalogs/incoming/grainger_raw.pdf \
  --workers 4 \
  --chunk_size 50

cp /app/data/catalogs/wholesale_catalogs/normalized/grainger_catalog.parquet \
   /app/data/catalogs/wholesale_catalogs/indexed/grainger_normalized.parquet

python3 -c "
from core.engine import IndustrialCatalogEngine, DatabaseManager
engine = IndustrialCatalogEngine(DatabaseManager())
engine.build_index()
print(f'Updated Barcodes in Memory:   {len(engine.catalog_barcodes):,}')
print(f'Updated Brand-MPNs in Memory: {len(engine.catalog_brand_mpns):,}')
"

git add core/engine.py tools/parse_grainger_pdf.py
git commit -m "feat(grainger): implement end-to-end PDF parsing and catalog indexing pipeline"
git push origin main
