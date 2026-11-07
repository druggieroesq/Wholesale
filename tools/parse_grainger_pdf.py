cat << 'EOF' > /app/tools/parse_grainger_pdf.py
#!/usr/bin/env python3
"""
parse_grainger_pdf.py
High-throughput chunked PDF extractor for large Grainger catalogs.
Extracts: Grainger Item #, MPN/Model, Brand, Description, and Price.
Saves to SQLite checkpoint DB and exports to Parquet.
"""

import os
import re
import sys
import sqlite3
import argparse
from multiprocessing import Pool, cpu_count
from pathlib import Path
import pandas as pd

try:
    import pdfplumber
except ImportError:
    print("pdfplumber missing. Installing...")
    os.system("pip install pdfplumber pandas pyarrow")
    import pdfplumber

DB_PATH = "/app/data/grainger_parsed_cache.db"
OUTPUT_PARQUET = "/app/data/catalogs/wholesale_catalogs/normalized/grainger_catalog.parquet"

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS grainger_items (
            item_number TEXT PRIMARY KEY,
            mpn TEXT,
            brand TEXT,
            description TEXT,
            wholesale_cost REAL,
            page_num INTEGER
        )
    """)
    conn.commit()
    conn.close()

def parse_page_chunk(args):
    pdf_path, start_page, end_page = args
    extracted = []
    
    # Common Grainger item number pattern (e.g., 4BY42, 10A123, 1A001)
    item_pat = re.compile(r'\b([0-9][A-Z0-9]{3,7})\b')
    price_pat = re.compile(r'\$([0-9]{1,4}\.[0-9]{2})')

    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page_idx in range(start_page, min(end_page, len(pdf.pages))):
                page = pdf.pages[page_idx]
                text = page.extract_text() or ""
                
                # Check tables first
                tables = page.extract_tables() or []
                for table in tables:
                    for row in table:
                        clean_row = [str(c).strip() for c in row if c]
                        row_text = " ".join(clean_row)
                        item_match = item_pat.search(row_text)
                        price_match = price_pat.search(row_text)
                        
                        if item_match and price_match:
                            extracted.append((
                                item_match.group(1),
                                clean_row[1] if len(clean_row) > 1 else "",
                                clean_row[0] if len(clean_row) > 0 else "Grainger MRO",
                                row_text[:120],
                                float(price_match.group(1)),
                                page_idx + 1
                            ))
                
                # Fallback to line scanning if no tables
                if not tables:
                    for line in text.split("\n"):
                        item_match = item_pat.search(line)
                        price_match = price_pat.search(line)
                        if item_match and price_match:
                            extracted.append((
                                item_match.group(1),
                                "",
                                "Grainger MRO",
                                line[:120],
                                float(price_match.group(1)),
                                page_idx + 1
                            ))
    except Exception as e:
        print(f"Error on pages {start_page}-{end_page}: {e}")
        
    return extracted

def main():
    parser = argparse.ArgumentParser(description="Parse Grainger PDF Catalog")
    parser.add_argument("--input", required=True, help="Path to PDF")
    parser.add_argument("--workers", type=int, default=4, help="Worker count")
    parser.add_argument("--chunk_size", type=int, default=100, help="Pages per chunk")
    args = parser.parse_args()

    pdf_file = Path(args.input)
    if not pdf_file.exists():
        print(f"Error: File '{pdf_file}' not found.")
        sys.exit(1)

    init_db()

    with pdfplumber.open(str(pdf_file)) as pdf:
        total_pages = len(pdf.pages)
    print(f"Loaded '{pdf_file.name}' with {total_pages} pages.")

    chunks = []
    for start in range(0, total_pages, args.chunk_size):
        chunks.append((str(pdf_file), start, start + args.chunk_size))

    print(f"Dispatching {len(chunks)} chunks across {args.workers} workers...")
    all_results = []
    with Pool(processes=args.workers) as pool:
        for result in pool.imap_unordered(parse_page_chunk, chunks):
            if result:
                all_results.extend(result)
                conn = sqlite3.connect(DB_PATH)
                cur = conn.cursor()
                cur.executemany("""
                    INSERT OR IGNORE INTO grainger_items 
                    (item_number, mpn, brand, description, wholesale_cost, page_num)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, result)
                conn.commit()
                conn.close()
                print(f"Cached {len(result)} items (Total: {len(all_results)})")

    # Export cache to Parquet
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query("SELECT * FROM grainger_items", conn)
    conn.close()

    os.makedirs(os.path.dirname(OUTPUT_PARQUET), exist_ok=True)
    df.to_parquet(OUTPUT_PARQUET, index=False)
    print(f"Successfully extracted {len(df)} SKUs into {OUTPUT_PARQUET}")

if __name__ == "__main__":
    main()
EOF
chmod +x /app/tools/parse_grainger_pdf.py
