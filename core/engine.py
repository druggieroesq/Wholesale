"""
core/engine.py
In-memory aggregation & fast lookup across all normalized wholesale Parquet feeds,
with strict 45-lb standard parcel ceiling guardrails.
"""
import os
import re
import glob
import logging
import sqlite3
import pandas as pd
from typing import Dict, Any, Optional, Tuple

logger = logging.getLogger("IndustrialCatalogEngine")

def is_over_weight_limit(title: str = "", package_weight_raw: Any = 0, item_weight_raw: Any = 0, max_lbs: float = 45.0) -> Tuple[bool, str]:
    """
    Hard-rejects products >= 45 lbs.
    Guards against Keepa gram-to-pound parsing defects and missing weight metadata.
    """
    title_upper = str(title or "").upper()
    heavy_patterns = [
        r"\b([4-9][5-9]|\d{3,})\s*(LB|LBS|POUND|POUNDS)\b",  # Matches 45 lb, 50 lbs, 100 lb, etc.
        r"\b50\s*LB\b",
        r"\b50-LB\b",
        r"\b50LB\b"
    ]
    for pat in heavy_patterns:
        if re.search(pat, title_upper):
            return True, f"TITLE_HEAVY_TOKEN ({pat})"

    for raw_wt in [package_weight_raw, item_weight_raw]:
        try:
            wt_float = float(raw_wt or 0)
            if wt_float > 0:
                # If Keepa sends grams (>100 is almost certainly grams), convert to lbs
                lbs = wt_float / 453.592 if wt_float > 100 else wt_float
                if lbs >= max_lbs:
                    return True, f"WEIGHT_EXCEEDS_{max_lbs}LB ({lbs:.1f} lbs)"
        except (ValueError, TypeError):
            continue

    return False, "CLEAN"


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

                    # Heavy/bulky guardrail filter at indexing time
                    over_limit, _ = is_over_weight_limit(
                        title=rec.get("title") or rec.get("description") or "",
                        package_weight_raw=rec.get("weight") or rec.get("package_weight") or 0
                    )
                    if over_limit:
                        continue  # Skip indexing SKUs exceeding parcel limits

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
