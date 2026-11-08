#!/usr/bin/env python3
"""
tools/auto_cross_catalog_matcher_daemon.py
Automated cross-catalog matcher bot for wholesale MRO pipelines.
Continuously or periodically scans incoming distributor feeds against
Amazon buy-sheets, hunter reports, and SQLite leads.
"""

import os
import sys
import time
import glob
import json
import re
import logging
import sqlite3
import argparse
import pandas as pd
from typing import Dict, Any, List, Set

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] CrossCatalogBot: %(message)s"
)
logger = logging.getLogger("CrossCatalogBot")

STOPWORDS = {
    "HEAVY", "PACK", "PURE", "GREEN", "BAGS", "HOME", "GAUGE", 
    "TOOL", "AUTO", "SIZE", "SET", "PLUS", "PAIR", "16OZ", "32OZ",
    "PIECE", "DUTY", "STEEL", "BLACK", "WHITE", "PRO"
}

def clean_alphanumeric(val: Any) -> str:
    if val is None or pd.isna(val):
        return ""
    return re.sub(r"[^A-Z0-9]", "", str(val).upper())

def safe_float(val: Any) -> float:
    if val is None or pd.isna(val):
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace("$", "").replace(",", "")
    match = re.search(r"[-+]?\d*\.\d+|\d+", s)
    return float(match.group(0)) if match else 0.0

class CrossCatalogMatcherBot:
    def __init__(self,
                 parquet_dir: str = "/app/data/catalogs/wholesale_catalogs/indexed",
                 reports_dir: str = "/app/data/reports",
                 db_path: str = "/app/data/mro_pipeline.db"):
        self.parquet_dir = parquet_dir
        self.reports_dir = reports_dir
        self.db_path = db_path
        
        self.upc_index: Dict[str, List[Dict[str, Any]]] = {}
        self.mpn_index: Dict[str, List[Dict[str, Any]]] = {}
        self.total_skus: int = 0

    def load_distributor_indexes(self):
        """Indexes all normalized Parquet catalogs with dynamic schema handling."""
        self.upc_index.clear()
        self.mpn_index.clear()
        self.total_skus = 0
        
        parquet_files = sorted(glob.glob(os.path.join(self.parquet_dir, "*.parquet")))
        logger.info(f"Indexing {len(parquet_files)} distributor catalogs...")

        for pf in parquet_files:
            vendor = os.path.basename(pf).replace("_normalized.parquet", "").upper()
            try:
                df = pd.read_parquet(pf)
                self.total_skus += len(df)
                cols = set(df.columns)

                for _, row in df.iterrows():
                    item = row.to_dict()
                    item["source_catalog"] = vendor

                    # Universal Barcode Check (upc_raw, gtin14, upc, barcode)
                    for u_col in ["upc_raw", "gtin14", "upc", "barcode"]:
                        if u_col in cols:
                            u = clean_alphanumeric(item.get(u_col))
                            if u and u not in ("0", "NONE", "NAN"):
                                self.upc_index.setdefault(u, []).append(item)

                    # Universal Part Number Check (clean_mpn, root_mpn, mpn, sku)
                    for m_col in ["clean_mpn", "root_mpn", "mpn", "sku"]:
                        if m_col in cols:
                            m = clean_alphanumeric(item.get(m_col))
                            if m and len(m) >= 4 and not m.startswith("63F4") and m not in STOPWORDS:
                                self.mpn_index.setdefault(m, []).append(item)
            except Exception as e:
                logger.error(f"Failed to read catalog {pf}: {e}")

        logger.info(f"Loaded {self.total_skus:,} total distributor SKUs.")
        logger.info(f"Active Barcode Search Keys: {len(self.upc_index):,}")
        logger.info(f"Active Clean MPN Search Keys: {len(self.mpn_index):,}")

    def gather_leads(self) -> List[Dict[str, Any]]:
        """Collects leads across JSON report files, CSV buysheets, and SQLite."""
        leads = []
        
        # 1. JSON Report sweeps
        json_files = glob.glob(os.path.join(self.reports_dir, "*.json")) + glob.glob("/app/data/*.json")
        for jf in json_files:
            if "checkpoint" in jf or "cross_matches" in jf:
                continue
            try:
                with open(jf, "r") as f:
                    d = json.load(f)
                    items = d if isinstance(d, list) else [it for v in d.values() if isinstance(v, list) for it in v]
                    for it in items:
                        if isinstance(it, dict) and ("asin" in it or "ASIN" in it):
                            leads.append(it)
            except Exception:
                pass

        # 2. SQLite Database sweeps
        if os.path.isfile(self.db_path):
            with sqlite3.connect(self.db_path) as conn:
                for tbl in ["leads", "hunter_leads", "opportunities", "buylist"]:
                    try:
                        for _, r in pd.read_sql_query(f"SELECT * FROM {tbl}", conn).iterrows():
                            leads.append(r.to_dict())
                    except Exception:
                        pass

        # 3. CSV Buysheet sweeps
        for csv_path in glob.glob("/app/data/*.csv"):
            if "cross_matches" in csv_path or "checkpoint" in csv_path:
                continue
            try:
                cdf = pd.read_csv(csv_path)
                if "asin" in cdf.columns or "ASIN" in cdf.columns:
                    for _, r in cdf.iterrows():
                        leads.append(r.to_dict())
            except Exception:
                pass

        logger.info(f"Gathered {len(leads):,} candidate lead entries.")
        return leads

    def match_leads(self, leads: List[Dict[str, Any]]) -> pd.DataFrame:
        """Performs robust cross-matching with stopword & structure guards."""
        verified_hits = []

        for lead in leads:
            asin = lead.get("asin") or lead.get("ASIN") or ""
            title = str(lead.get("title") or lead.get("Title") or "")
            brand = clean_alphanumeric(lead.get("brand") or lead.get("Brand") or "")
            current_cost = safe_float(lead.get("matched_cost") or lead.get("wholesale_cost") or lead.get("unit_cost") or lead.get("cost"))
            current_vendor = lead.get("matched_vendor") or lead.get("vendor") or "EXISTING"

            # Parse UPCs (single or lists)
            candidate_upcs: Set[str] = set()
            raw_upcs = lead.get("upcs") or lead.get("upc") or lead.get("UPC")
            if isinstance(raw_upcs, list):
                for u in raw_upcs:
                    c = clean_alphanumeric(u)
                    if len(c) >= 8: candidate_upcs.add(c)
            elif raw_upcs:
                for u in str(raw_upcs).split(","):
                    c = clean_alphanumeric(u)
                    if len(c) >= 8: candidate_upcs.add(c)

            # 1. Barcode Matches (100% High Confidence)
            for upc in candidate_upcs:
                if upc in self.upc_index:
                    for dist in self.upc_index[upc]:
                        dist_cost = safe_float(dist.get("unit_cost") or dist.get("raw_cost"))
                        verified_hits.append({
                            "asin": asin,
                            "title": title[:50],
                            "matched_key": upc,
                            "matched_vendor": dist["source_catalog"],
                            "matched_title": str(dist.get("title", ""))[:50],
                            "matched_cost": dist_cost,
                            "current_cost": current_cost,
                            "current_vendor": current_vendor,
                            "match_type": "EXACT_UPC",
                            "confidence": "HIGH"
                        })

            # Parse MPN candidates from fields & title
            candidate_mpns: Set[str] = set()
            for field in ["clean_mpn", "root_mpn", "mpn", "MPN", "model", "part_number", "matched_sku"]:
                v = clean_alphanumeric(lead.get(field))
                if len(v) >= 4 and not v.startswith("63F4") and v not in STOPWORDS:
                    candidate_mpns.add(v)

            # Regex token extraction (require digit for non-barcode MPN)
            for token in re.findall(r"\b[A-Za-z0-9-]{4,15}\b", title):
                c = clean_alphanumeric(token)
                if c not in STOPWORDS and any(ch.isdigit() for ch in c):
                    candidate_mpns.add(c)

            # 2. Alphanumeric MPN Matches
            for mpn in candidate_mpns:
                if mpn in self.mpn_index:
                    for dist in self.mpn_index[mpn]:
                        dist_brand = clean_alphanumeric(dist.get("brand"))
                        # Validate brand alignment if available
                        if not brand or not dist_brand or (brand in dist_brand) or (dist_brand in brand):
                            dist_cost = safe_float(dist.get("unit_cost") or dist.get("raw_cost"))
                            verified_hits.append({
                                "asin": asin,
                                "title": title[:50],
                                "matched_key": mpn,
                                "matched_vendor": dist["source_catalog"],
                                "matched_title": str(dist.get("title", ""))[:50],
                                "matched_cost": dist_cost,
                                "current_cost": current_cost,
                                "current_vendor": current_vendor,
                                "match_type": "ALPHANUMERIC_MPN",
                                "confidence": "HIGH" if any(ch.isdigit() for ch in mpn) else "MEDIUM"
                            })

        if not verified_hits:
            return pd.DataFrame()

        df_hits = pd.DataFrame(verified_hits).drop_duplicates(
            subset=["asin", "matched_vendor", "matched_cost", "matched_key"]
        )
        return df_hits

    def execute_match_cycle(self):
        logger.info("Executing automated cross-match cycle...")
        self.load_distributor_indexes()
        leads = self.gather_leads()
        matches_df = self.match_leads(leads)

        if matches_df.empty:
            logger.info("No cross-catalog matches identified in this cycle.")
            return

        out_csv = os.path.join(self.reports_dir, "auto_verified_cross_matches.csv")
        os.makedirs(self.reports_dir, exist_ok=True)
        matches_df.to_csv(out_csv, index=False)
        logger.info(f"Cycle Complete. Saved {len(matches_df):,} verified matches to: {out_csv}")

def main():
    parser = argparse.ArgumentParser(description="Automated Cross-Catalog Matcher Bot")
    parser.add_argument("--run-once", action="store_true", help="Run a single match cycle and exit")
    parser.add_argument("--interval", type=int, default=3600, help="Loop interval in seconds (default: 3600)")
    args = parser.parse_args()

    bot = CrossCatalogMatcherBot()

    if args.run_once:
        bot.execute_match_cycle()
        sys.exit(0)

    logger.info(f"Starting CrossCatalogMatcherBot daemon (interval: {args.interval}s)...")
    while True:
        try:
            bot.execute_match_cycle()
        except Exception as e:
            logger.error(f"Error during matcher cycle: {e}")
        time.sleep(args.interval)

if __name__ == "__main__":
    main()
EOF

# Make executable and run a test execution
chmod +x /app/tools/auto_cross_catalog_matcher_daemon.py
python3 /app/tools/auto_cross_catalog_matcher_daemon.py --run-once

# Stage, commit, and push to GitHub so it is permanently tracked
git add tools/auto_cross_catalog_matcher_daemon.py
git commit -m "feat(bot): add automated cross-catalog matcher daemon with multi-schema parsing and stopword guard"
git push origin main
