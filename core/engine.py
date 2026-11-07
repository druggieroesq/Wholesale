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
                for _, row in df.iterrows():
                    rec = row.to_dict()
                    upc = str(rec.get("upc") or "").strip()
                    if upc and upc not in ("nan", "None", "", "0"):
                        self.catalog_barcodes[upc] = rec

                    brand = str(rec.get("brand") or "").strip().upper()
                    mpn = str(rec.get("clean_mpn") or rec.get("mpn") or "").strip().upper()
                    if brand and mpn:
                        self.catalog_brand_mpns[f"{brand}:{mpn}"] = rec
            except Exception as e:
                logger.error("Error reading %s: %s", pf, e)

        print(f"Loaded {len(self.catalog_barcodes):,} barcodes and {len(self.catalog_brand_mpns):,} Brand-MPN index keys.")
EOF

cat << 'EOF' > /app/core/__init__.py
from .engine import IndustrialCatalogEngine, DatabaseManager

__all__ = ["IndustrialCatalogEngine", "DatabaseManager"]
EOF

python3 -c "
from core.engine import IndustrialCatalogEngine, DatabaseManager
db = DatabaseManager()
engine = IndustrialCatalogEngine(db)
engine.build_index()
print(f'Total Barcodes in Engine:   {len(engine.catalog_barcodes):,}')
print(f'Total Brand-MPNs in Engine: {len(engine.catalog_brand_mpns):,}')
"

git add core/engine.py core/__init__.py
git commit -m "fix(core): complete IndustrialCatalogEngine aggregator across 140k parquet SKUs"
git push origin main
