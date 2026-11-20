#!/usr/bin/env python3
"""
tools/auto_cross_catalog_matcher_daemon.py (v2.3)
-------------------------------------------------
Permanent Cross-Catalog Matcher Daemon for Wholesale MRO & Specialty Pipelines:
- Loads the full 784-brand IP & Private-Label Blocklist (/app/data/ip_blocklist.json)
  with Unicode accent normalization and multi-word substring protection
- Syncs legacy /app/data/ip_blacklist.json to match ip_blocklist.json automatically
- Indexes BOTH .parquet AND .csv distributor catalogs across indexed/, incoming/,
  and normalized/ (including Jb_Tools_Catalog.csv, Atlantic_Spice_Catalog.csv,
  EEDistribution_Catalog.csv, Marlo_Beauty_Catalog.csv, and bci_normalized_index.csv)
- Scans BOTH verified_deals AND candidate_cache (72,000+ ASINs) using:
  1. Exact UPC / EAN / GTIN Barcode Match
  2. Brand-Verified Alphanumeric MPN / SKU Match
  3. High-Precision Brand + Title Token Match (for no-UPC catalogs like JB Tools & Atlantic Spice)
- Promotes matched leads directly into verified_deals with real wholesale COGS,
  net profit, ROI, and confidence tier so push_full_backlog_to_sheet.py streams them live
"""

import os
import sys
import glob
import json
import re
import time
import shutil
import logging
import sqlite3
import argparse
import unicodedata
from pathlib import Path
from typing import Dict, Any, List, Optional, Set, Tuple

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] CrossCatalogBot: %(message)s"
)
logger = logging.getLogger("CrossCatalogBot")

STOPWORDS = {
    "HEAVY", "PACK", "PURE", "GREEN", "BAGS", "HOME", "GAUGE",
    "TOOL", "AUTO", "SIZE", "SET", "PLUS", "PAIR", "16OZ", "32OZ",
    "PIECE", "DUTY", "STEEL", "BLACK", "WHITE", "PRO", "WITH", "AND",
    "FOR", "THE", "INCH", "PACKS", "COUNT", "OUNCE", "POUND", "CASE"
}


def norm_text(val: Any) -> str:
    if val is None or pd.isna(val):
        return ""
    s = unicodedata.normalize("NFKD", str(val)).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", s.upper().strip())


def clean_alphanumeric(val: Any) -> str:
    s = norm_text(val)
    if s.endswith(".0"):
        s = s[:-2]
    return re.sub(r"[^A-Z0-9]", "", s)


def extract_tokens(title: str) -> Set[str]:
    cleaned = norm_text(title)
    tokens = set(re.findall(r"[A-Z0-9]{3,}", cleaned))
    return {t for t in tokens if t not in STOPWORDS}


def safe_float(val: Any) -> float:
    if val is None or pd.isna(val):
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace("$", "").replace(",", "")
    match = re.search(r"[-+]?\d*\.\d+|\d+", s)
    return float(match.group(0)) if match else 0.0


class CrossCatalogMatcherBot:
    def __init__(
        self,
        catalog_dir: str = "/app/data/catalogs/wholesale_catalogs/indexed",
        incoming_dir: str = "/app/data/catalogs/wholesale_catalogs/incoming",
        reports_dir: str = "/app/data/reports",
        db_path: str = "/app/data/hunter_engine.db",
        blacklist_path: str = "/app/data/ip_blocklist.json",
    ):
        self.catalog_dir = catalog_dir
        self.incoming_dir = incoming_dir
        self.reports_dir = reports_dir
        self.db_path = db_path
        self.blacklist_path = blacklist_path

        self.upc_index: Dict[str, List[Dict[str, Any]]] = {}
        self.mpn_index: Dict[str, List[Dict[str, Any]]] = {}
        self.brand_index: Dict[str, List[Dict[str, Any]]] = {}
        self.total_skus: int = 0
        self.ip_blacklist: Set[str] = set()
        self.ip_multi_word: List[str] = []
        self._load_blacklist()

    def _load_blacklist(self):
        primary = "/app/data/ip_blocklist.json"
        legacy = "/app/data/ip_blacklist.json"
        if os.path.exists(primary):
            try:
                shutil.copy2(primary, legacy)
            except Exception:
                pass

        candidates = [
            self.blacklist_path,
            primary,
            legacy,
        ]
        for path in candidates:
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        raw = json.load(f)
                    items = raw if isinstance(raw, list) else raw.get("brands", [])
                    self.ip_blacklist = {norm_text(b) for b in items if b}
                    self.ip_multi_word = [b for b in sorted(self.ip_blacklist) if len(b) >= 4]
                    logger.info(
                        f"Loaded {len(self.ip_blacklist):,} blacklisted IP brands from {path}."
                    )
                    return
                except Exception as e:
                    logger.error(f"Error loading blacklist from {path}: {e}")

    def is_brand_blocked(self, brand_raw: Any) -> bool:
        b = norm_text(brand_raw)
        if not b or len(b) < 2:
            return True
        if b in self.ip_blacklist:
            return True
        for entry in self.ip_multi_word:
            if b == entry or b.startswith(entry + " ") or (" " + entry) in b:
                return True
        return False

    def sync_incoming_to_indexed(self):
        os.makedirs(self.catalog_dir, exist_ok=True)
        if not os.path.isdir(self.incoming_dir):
            return
        for csv_path in glob.glob(os.path.join(self.incoming_dir, "*.csv")):
            dest = os.path.join(self.catalog_dir, os.path.basename(csv_path))
            try:
                if not os.path.exists(dest) or os.path.getmtime(csv_path) > os.path.getmtime(dest):
                    shutil.copy2(csv_path, dest)
            except Exception as exc:
                logger.warning(f"Could not sync {csv_path} -> {dest}: {exc}")

    def load_distributor_indexes(self):
        self.sync_incoming_to_indexed()
        self.upc_index.clear()
        self.mpn_index.clear()
        self.brand_index.clear()
        self.total_skus = 0

        catalog_files = sorted(
            glob.glob(os.path.join(self.catalog_dir, "*.parquet"))
            + glob.glob(os.path.join(self.catalog_dir, "*.csv"))
            + glob.glob("/app/data/catalogs/wholesale_catalogs/normalized/*.parquet")
            + glob.glob("/app/data/catalogs/wholesale_catalogs/normalized/*.csv")
        )
        logger.info(f"Indexing {len(catalog_files)} distributor catalogs (.parquet + .csv)...")

        for cf in catalog_files:
            base = os.path.basename(cf)
            vendor = (
                re.sub(r"\.(parquet|csv)$", "", base, flags=re.IGNORECASE)
                .replace("_normalized", "")
                .replace("_Catalog", "")
                .replace("_catalog", "")
                .replace("_", " ")
                .upper()
                .strip()
            )
            try:
                if cf.lower().endswith(".parquet"):
                    df = pd.read_parquet(cf)
                else:
                    df = pd.read_csv(cf, low_memory=False)

                if df.empty:
                    continue

                self.total_skus += len(df)
                cols = set(df.columns)
                cost_col = next(
                    (
                        c
                        for c in ["unit_cost", "wholesale_cost", "cost", "price", "net_price"]
                        if c in cols
                    ),
                    next(
                        (
                            c
                            for c in cols
                            if any(k in c.lower() for k in ["cost", "price", "wholesale", "net"])
                        ),
                        None,
                    ),
                )

                for _, row in df.iterrows():
                    item = row.to_dict()
                    raw_vendor = norm_text(item.get("vendor")) or vendor
                    item["source_catalog"] = raw_vendor

                    dist_cost = safe_float(item.get(cost_col)) if cost_col else 0.0
                    if dist_cost <= 0.05:
                        continue
                    item["raw_cost"] = dist_cost

                    b_clean = clean_alphanumeric(item.get("brand"))
                    if b_clean and not self.is_brand_blocked(item.get("brand")):
                        item["_tokens"] = extract_tokens(str(item.get("title", "")))
                        self.brand_index.setdefault(b_clean, []).append(item)

                    for u_col in ["upc_raw", "gtin14", "upc", "barcode", "gtin"]:
                        if u_col in cols:
                            u = clean_alphanumeric(item.get(u_col))
                            if u and u not in ("0", "NONE", "NAN") and len(u) >= 8:
                                self.upc_index.setdefault(u, []).append(item)

                    for m_col in ["clean_mpn", "root_mpn", "mpn", "sku", "item"]:
                        if m_col in cols:
                            m = clean_alphanumeric(item.get(m_col))
                            if m and len(m) >= 4 and not m.startswith("63F4") and m not in STOPWORDS:
                                self.mpn_index.setdefault(m, []).append(item)
            except Exception as e:
                logger.error(f"Failed to read catalog {cf}: {e}")

        logger.info(f"Loaded {self.total_skus:,} total distributor SKUs.")
        logger.info(f"Active Barcode Search Keys: {len(self.upc_index):,}")
        logger.info(f"Active Clean MPN Search Keys: {len(self.mpn_index):,}")
        logger.info(f"Active Brand-Token Search Groups: {len(self.brand_index):,}")

    def gather_leads(self) -> List[Dict[str, Any]]:
        leads_by_asin: Dict[str, Dict[str, Any]] = {}
        if not os.path.isfile(self.db_path):
            return []

        with sqlite3.connect(self.db_path) as conn:
            try:
                q_vd = """
                SELECT
                    vd.asin,
                    vd.title,
                    vd.brand,
                    vd.sales_rank,
                    vd.buy_box_price,
                    vd.wholesale_cost AS existing_cost,
                    vd.vendor AS existing_vendor,
                    vd.status AS current_status,
                    cc.upc,
                    cc.ean,
                    cc.mpn,
                    cc.fba_fee,
                    cc.referral_pct,
                    cc.monthly_sold,
                    cc.drops_90
                FROM verified_deals vd
                LEFT JOIN candidate_cache cc ON vd.asin = cc.asin
                WHERE vd.asin IS NOT NULL
                  AND vd.status NOT IN ('BLOCKED_IP_BRAND', 'REJECTED');
                """
                for row in pd.read_sql_query(q_vd, conn).to_dict(orient="records"):
                    leads_by_asin[row["asin"]] = row
            except Exception as e:
                logger.warning(f"Error querying verified_deals: {e}")

            try:
                q_cc = """
                SELECT
                    asin,
                    title,
                    brand,
                    sales_rank,
                    COALESCE(current_bb, avg90_bb, 0) AS buy_box_price,
                    0.0 AS existing_cost,
                    '' AS existing_vendor,
                    status AS current_status,
                    upc,
                    ean,
                    mpn,
                    fba_fee,
                    referral_pct,
                    monthly_sold,
                    drops_90
                FROM candidate_cache
                WHERE asin IS NOT NULL
                  AND status IN ('QUALIFIED', 'NO_CATALOG_MATCH', 'UNMAPPED_CANDIDATE', 'LOW_VELOCITY')
                  AND COALESCE(current_bb, avg90_bb, 0) >= 12.0;
                """
                for row in pd.read_sql_query(q_cc, conn).to_dict(orient="records"):
                    if row["asin"] not in leads_by_asin:
                        leads_by_asin[row["asin"]] = row
            except Exception as e:
                logger.warning(f"Error querying candidate_cache: {e}")

        leads = list(leads_by_asin.values())
        logger.info(f"Fetched {len(leads):,} combined candidate & backlog ASINs for cross-matching.")
        return leads

    def _build_hit(
        self,
        lead: Dict[str, Any],
        dist: Dict[str, Any],
        matched_key: str,
        match_type: str,
        confidence: str,
    ) -> Optional[Dict[str, Any]]:
        bb = safe_float(lead.get("buy_box_price"))
        fba = safe_float(lead.get("fba_fee"))
        ref_pct = safe_float(lead.get("referral_pct")) or 0.15
        dist_cost = safe_float(dist.get("unit_cost") or dist.get("raw_cost"))

        if dist_cost <= 0.05 or bb <= 0:
            return None
        if dist_cost >= bb * 0.72:
            return None

        net_profit = bb - (bb * ref_pct) - fba - dist_cost
        roi = (net_profit / dist_cost) * 100.0 if dist_cost > 0 else 0.0
        if net_profit < 1.50 or roi < 15.0:
            return None

        return {
            "asin": lead.get("asin", ""),
            "brand": norm_text(lead.get("brand")),
            "title": str(lead.get("title", ""))[:160],
            "sales_rank": int(safe_float(lead.get("sales_rank")) or 999999),
            "matched_key": matched_key,
            "matched_vendor": dist["source_catalog"],
            "matched_title": str(dist.get("title", ""))[:160],
            "direct_url": str(dist.get("direct_url") or dist.get("url") or ""),
            "wholesale_cost": round(dist_cost, 2),
            "buy_box": round(bb, 2),
            "max_allowable_buy_cost": round(max(0.0, bb * (1.0 - ref_pct) - fba - 2.25), 2),
            "net_profit": round(net_profit, 2),
            "roi_pct": round(roi, 1),
            "match_type": match_type,
            "confidence": confidence,
        }

    def match_leads(self, leads: List[Dict[str, Any]]) -> pd.DataFrame:
        best_by_asin: Dict[str, Dict[str, Any]] = {}

        for lead in leads:
            if self.is_brand_blocked(lead.get("brand")):
                continue

            asin = lead.get("asin") or ""
            title = str(lead.get("title") or "")
            brand = clean_alphanumeric(lead.get("brand"))
            candidate_hits: List[Dict[str, Any]] = []

            # 1. Exact Barcode (UPC / EAN / GTIN)
            candidate_upcs: Set[str] = set()
            for u_field in ["upc", "ean", "upcs"]:
                val = lead.get(u_field)
                if val:
                    for u in str(val).split(","):
                        c = clean_alphanumeric(u)
                        if len(c) >= 8:
                            candidate_upcs.add(c)

            for upc in candidate_upcs:
                for dist in self.upc_index.get(upc, []):
                    hit = self._build_hit(lead, dist, upc, "EXACT_UPC", "HIGH")
                    if hit:
                        candidate_hits.append(hit)

            # 2. Brand-Verified Alphanumeric MPN
            if not candidate_hits:
                candidate_mpns: Set[str] = set()
                for field in ["clean_mpn", "root_mpn", "mpn", "sku"]:
                    v = clean_alphanumeric(lead.get(field))
                    if len(v) >= 4 and not v.startswith("63F4") and v not in STOPWORDS:
                        candidate_mpns.add(v)

                for token in re.findall(r"\b[A-Za-z0-9-]{4,16}\b", title):
                    c = clean_alphanumeric(token)
                    if c not in STOPWORDS and any(ch.isdigit() for ch in c) and any(ch.isalpha() for ch in c):
                        candidate_mpns.add(c)

                for mpn in candidate_mpns:
                    for dist in self.mpn_index.get(mpn, []):
                        dist_brand = clean_alphanumeric(dist.get("brand"))
                        if brand and dist_brand and ((brand in dist_brand) or (dist_brand in brand)):
                            hit = self._build_hit(lead, dist, mpn, "BRAND_MPN", "HIGH")
                            if hit:
                                candidate_hits.append(hit)

            # 3. High-Precision Brand + Title Token Overlap (for no-UPC catalogs like JB Tools & Atlantic Spice)
            if not candidate_hits and brand and brand in self.brand_index:
                lead_tokens = extract_tokens(title)
                if len(lead_tokens) >= 3:
                    for dist in self.brand_index[brand]:
                        d_tokens = dist.get("_tokens") or set()
                        if len(d_tokens) < 3:
                            continue
                        overlap = lead_tokens & d_tokens
                        jaccard = len(overlap) / max(1, min(len(lead_tokens), len(d_tokens)))
                        if len(overlap) >= 4 and jaccard >= 0.65:
                            key_str = " ".join(sorted(list(overlap))[:4])
                            hit = self._build_hit(lead, dist, key_str, "BRAND_TOKEN_65", "MEDIUM")
                            if hit:
                                candidate_hits.append(hit)

            if candidate_hits:
                best = sorted(candidate_hits, key=lambda x: (-x["net_profit"], x["wholesale_cost"]))[0]
                best_by_asin[asin] = best

        if not best_by_asin:
            return pd.DataFrame()
        return pd.DataFrame(list(best_by_asin.values()))

    def persist_matches(self, df_hits: pd.DataFrame) -> int:
        if df_hits.empty or not os.path.isfile(self.db_path):
            return 0

        os.makedirs(self.reports_dir, exist_ok=True)
        report_csv = os.path.join(self.reports_dir, "cross_catalog_verified_hits.csv")
        df_hits.to_csv(report_csv, index=False)

        promoted = 0
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cols = {c[1] for c in cur.execute("PRAGMA table_info(verified_deals)")}

            for _, row in df_hits.iterrows():
                asin = row["asin"]
                exists = cur.execute("SELECT id FROM verified_deals WHERE asin = ?", (asin,)).fetchone()
                if exists:
                    update_fields = [
                        "vendor = ?",
                        "wholesale_cost = ?",
                        "max_allowable_buy_cost = ?",
                        "net_profit = ?",
                        "confidence_tier = ?",
                        "status = 'QUALIFIED'",
                    ]
                    params: List[Any] = [
                        row["matched_vendor"],
                        row["wholesale_cost"],
                        row["max_allowable_buy_cost"],
                        row["net_profit"],
                        row["confidence"],
                    ]
                    if "roi_pct" in cols:
                        update_fields.append("roi_pct = ?")
                        params.append(row["roi_pct"])
                    if "direct_url" in cols and row["direct_url"]:
                        update_fields.append("direct_url = ?")
                        params.append(row["direct_url"])
                    params.append(asin)
                    cur.execute(
                        f"UPDATE verified_deals SET {', '.join(update_fields)} WHERE asin = ?",
                        params,
                    )
                    promoted += 1
                else:
                    insert_cols = [
                        "asin", "title", "brand", "confidence_tier", "sales_rank",
                        "buy_box_price", "wholesale_cost", "max_allowable_buy_cost",
                        "net_profit", "vendor", "status"
                    ]
                    insert_vals: List[Any] = [
                        asin, row["title"], row["brand"], row["confidence"], row["sales_rank"],
                        row["buy_box"], row["wholesale_cost"], row["max_allowable_buy_cost"],
                        row["net_profit"], row["matched_vendor"], "QUALIFIED"
                    ]
                    if "roi_pct" in cols:
                        insert_cols.append("roi_pct")
                        insert_vals.append(row["roi_pct"])
                    if "direct_url" in cols:
                        insert_cols.append("direct_url")
                        insert_vals.append(row["direct_url"])
                    placeholders = ", ".join(["?"] * len(insert_cols))
                    cur.execute(
                        f"INSERT INTO verified_deals ({', '.join(insert_cols)}) VALUES ({placeholders})",
                        insert_vals,
                    )
                    promoted += 1

                cur.execute(
                    "UPDATE candidate_cache SET status = 'QUALIFIED' WHERE asin = ?",
                    (asin,),
                )
            conn.commit()

        logger.info(
            f"Promoted {promoted:,} vendor-verified leads into verified_deals -> report saved to {report_csv}"
        )
        return promoted

    def run_cycle(self) -> int:
        logger.info("Executing automated cross-match cycle...")
        self.load_distributor_indexes()
        leads = self.gather_leads()
        df_hits = self.match_leads(leads)
        if df_hits.empty:
            logger.info("No new cross-catalog matches identified in this cycle.")
            return 0
        logger.info(f"Identified {len(df_hits):,} profitable vendor-matched leads!")
        return self.persist_matches(df_hits)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Automated Cross-Catalog Wholesale Matcher Daemon v2.3")
    parser.add_argument("--run-once", action="store_true", help="Run a single cross-match cycle and exit")
    parser.add_argument("--interval", type=int, default=1800, help="Seconds between daemon cycles")
    args = parser.parse_args()

    bot = CrossCatalogMatcherBot()
    if args.run_once:
        bot.run_cycle()
        sys.exit(0)

    while True:
        try:
            bot.run_cycle()
        except Exception as exc:
            logger.error(f"Cross-match cycle error: {exc}")
        time.sleep(args.interval)
