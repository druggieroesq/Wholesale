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
    s = str(val).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return re.sub(r"[^A-Z0-9]", "", s.upper())


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
                 db_path: str = "/app/data/hunter_engine.db"):
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
        
        # Include both indexed and normalized folders
        parquet_files = sorted(
            glob.glob(os.path.join(self.parquet_dir, "*.parquet")) +
            glob.glob("/app/data/catalogs/wholesale_catalogs/normalized/*.parquet")
        )
        logger.info(f"Indexing {len(parquet_files)} distributor catalogs...")

        for pf in parquet_files:
            vendor = (
                os.path.basename(pf)
                .replace("_normalized.parquet", "")
                .replace("_catalog.parquet", "")
                .upper()
            )
            try:
                df = pd.read_parquet(pf)
                self.total_skus += len(df)
                cols = set(df.columns)

                # Identify price/cost column
                cost_col = next((c for c in cols if any(k in c.lower() for k in ["cost", "price", "wholesale", "net"])), None)

                for _, row in df.iterrows():
                    item = row.to_dict()
                    item["source_catalog"] = vendor

                    if cost_col:
                        item["raw_cost"] = safe_float(row[cost_col])

                    # Universal Barcode Check (upc_raw, gtin14, upc, barcode, gtin)
                    for u_col in ["upc_raw", "gtin14", "upc", "barcode", "gtin"]:
                        if u_col in cols:
                            u = clean_alphanumeric(item.get(u_col))
                            if u and u not in ("0", "NONE", "NAN") and len(u) >= 8:
                                self.upc_index.setdefault(u, []).append(item)

                    # Universal Part Number Check (clean_mpn, root_mpn, mpn, sku, item)
                    for m_col in ["clean_mpn", "root_mpn", "mpn", "sku", "item"]:
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

        # 2. SQLite Database sweeps (hunter_engine.db: verified_deals & candidate_cache)
        if os.path.isfile(self.db_path):
            with sqlite3.connect(self.db_path) as conn:
                for tbl in ["verified_deals", "candidate_cache"]:
                    try:
                        query = f"SELECT * FROM {tbl} WHERE asin IS NOT NULL"
                        for _, r in pd.read_sql_query(query, conn).iterrows():
                            leads.append(r.to_dict())
                    except Exception as e:
                        logger.warning(f"Could not read table {tbl} from {self.db_path}: {e}")

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
        """Performs cross-matching and computes live FBA profit margins."""
        verified_hits = []

        for lead in leads:
            asin = lead.get("asin") or lead.get("ASIN") or ""
            title = str(lead.get("title") or lead.get("Title") or "")
            brand = clean_alphanumeric(lead.get("brand") or lead.get("Brand") or "")
            bb = safe_float(lead.get("current_bb") or lead.get("buy_box") or lead.get("price"))
            fba = safe_float(lead.get("fba_fee"))
            ref_pct = safe_float(lead.get("referral_pct")) or 0.15

            # Parse UPCs
            candidate_upcs: Set[str] = set()
            raw_upcs = lead.get("upcs") or lead.get("upc") or lead.get("UPC") or lead.get("ean")
            if isinstance(raw_upcs, list):
                for u in raw_upcs:
                    c = clean_alphanumeric(u)
                    if len(c) >= 8: candidate_upcs.add(c)
            elif raw_upcs:
                for u in str(raw_upcs).split(","):
                    c = clean_alphanumeric(u)
                    if len(c) >= 8: candidate_upcs.add(c)

            # 1. Barcode Matches (High Confidence)
            for upc in candidate_upcs:
                if upc in self.upc_index:
                    for dist in self.upc_index[upc]:
                        dist_cost = safe_float(dist.get("unit_cost") or dist.get("raw_cost"))
                        if dist_cost <= 0.05: continue
                        
                        net_profit = bb - (bb * ref_pct) - fba - dist_cost if bb > 0 else 0.0
                        roi = (net_profit / dist_cost * 100) if dist_cost > 0 and bb > 0 else 0.0

                        verified_hits.append({
                            "asin": asin,
                            "brand": brand,
                            "title": title[:50],
                            "matched_key": upc,
                            "matched_vendor": dist["source_catalog"],
                            "matched_title": str(dist.get("title", ""))[:50],
                            "wholesale_cost": round(dist_cost, 2),
                            "buy_box": round(bb, 2),
                            "net_profit": round(net_profit, 2),
                            "roi_pct": round(roi, 1),
                            "match_type": "EXACT_UPC",
                            "confidence": "HIGH"
                        })

            # Parse MPN candidates
            candidate_mpns: Set[str] = set()
            for field in ["clean_mpn", "root_mpn", "mpn", "MPN", "model", "part_number", "sku"]:
                v = clean_alphanumeric(lead.get(field))
                if len(v) >= 4 and not v.startswith("63F4") and v not in STOPWORDS:
                    candidate_mpns.add(v)

            for token in re.findall(r"\b[A-Za-z0-9-]{4,15}\b", title):
                c = clean_alphanumeric(token)
                if c not in STOPWORDS and any(ch.isdigit() for ch in c):
                    candidate_mpns.add(c)

            # 2. Alphanumeric MPN Matches
            for mpn in candidate_mpns:
                if mpn in self.mpn_index:
                    for dist in self.mpn_index[mpn]:
                        dist_brand = clean_alphanumeric(dist.get("brand"))
                        if not brand or not dist_brand or (brand in dist_brand) or (dist_brand in brand):
                            dist_cost = safe_float(dist.get("unit_cost") or dist.get("raw_cost"))
                            if dist_cost <= 0.05: continue

                            net_profit = bb - (bb * ref_pct) - fba - dist_cost if bb > 0 else 0.0
                            roi = (net_profit / dist_cost * 100) if dist_cost > 0 and bb > 0 else 0.0

                            verified_hits.append({
                                "asin": asin,
                                "brand": brand,
                                "title": title[:50],
                                "matched_key": mpn,
                                "matched_vendor": dist["source_catalog"],
                                "matched_title": str(dist.get("title", ""))[:50],
                                "wholesale_cost": round(dist_cost, 2),
                                "buy_box": round(bb, 2),
                                "net_profit": round(net_profit, 2),
                                "roi_pct": round(roi, 1),
                                "match_type": "ALPHANUMERIC_MPN",
                                "confidence": "HIGH" if any(ch.isdigit() for ch in mpn) else "MEDIUM"
                            })

        if not verified_hits:
            return pd.DataFrame()

        df_hits = pd.DataFrame(verified_hits).drop_duplicates(
            subset=["asin", "matched_vendor", "wholesale_cost", "matched_key"]
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

        os.makedirs(self.reports_dir, exist_ok=True)
        out_csv = os.path.join(self.reports_dir, "auto_verified_cross_matches.csv")
        matches_df.to_csv(out_csv, index=False)
        logger.info(f"Cycle Complete. Saved {len(matches_df):,} verified matches to: {out_csv}")

        # Filter profitable deals
        profitable = matches_df[matches_df["net_profit"] >= 1.50].sort_values(by="net_profit", ascending=False)
        prof_csv = os.path.join(self.reports_dir, "profitable_cross_matches.csv")
        profitable.to_csv(prof_csv, index=False)
        logger.info(f"Saved {len(profitable):,} profitable deals (Net Profit >= $1.50) to: {prof_csv}")

        if not profitable.empty:
            print("\n--- TOP 15 PROFITABLE MATCHES ---")
            print(profitable[["asin", "brand", "matched_vendor", "wholesale_cost", "buy_box", "net_profit", "roi_pct"]].head(15).to_string(index=False))


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
