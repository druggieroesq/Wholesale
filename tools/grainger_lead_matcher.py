# 1. Ensure the file is written to tools/grainger_lead_matcher.py
cat << 'EOF' > /app/tools/grainger_lead_matcher.py
#!/usr/bin/env python3
"""
tools/grainger_lead_matcher.py
Two-stage Grainger MRO lead matcher & direct URL targeter.

Stage 1: Matches active wholesale buy-sheet leads against the parsed Grainger catalog
         by MPN and model tokens without web requests.
Stage 2: Generates exact Grainger product URLs (https://www.grainger.com/product/<ITEM_NO>)
         and exports high-confidence verified targets to CSV and JSON.
"""

import os
import re
import sys
import glob
import json
import sqlite3
import argparse
import pandas as pd
from typing import Dict, Any, List, Set

STOPWORDS: Set[str] = {
    "HEAVY", "PACK", "PURE", "GREEN", "BAGS", "HOME", "GAUGE",
    "TOOL", "AUTO", "SIZE", "SET", "PLUS", "PAIR", "16OZ", "32OZ",
    "PIECE", "DUTY", "STEEL", "BLACK", "WHITE", "PRO", "SERIES",
    "UNIVERSAL", "STANDARD", "REPLACEMENT", "PREMIUM", "INDUSTRIAL"
}

def clean_key(val: Any) -> str:
    """Sanitizes alphanumeric keys, stripping punctuation and whitespace."""
    if val is None or pd.isna(val):
        return ""
    return re.sub(r"[^A-Z0-9]", "", str(val).upper())

def safe_float(val: Any) -> float:
    """Extracts numeric float from formatted price strings or non-numeric tokens."""
    if val is None or pd.isna(val):
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace("$", "").replace(",", "")
    match = re.search(r"[-+]?\d*\.\d+|\d+", s)
    return float(match.group(0)) if match else 0.0

class GraingerLeadMatcher:
    def __init__(
        self,
        grainger_parquet: str = "/app/data/catalogs/wholesale_catalogs/indexed/grainger_normalized.parquet",
        grainger_db: str = "/app/data/grainger_parsed_cache.db",
        reports_dir: str = "/app/data/reports"
    ):
        self.grainger_parquet = grainger_parquet
        self.grainger_db = grainger_db
        self.reports_dir = reports_dir
        self.grainger_mpn_index: Dict[str, Dict[str, Any]] = {}
        self.total_grainger_skus: int = 0

    def load_grainger_catalog(self) -> None:
        self.grainger_mpn_index.clear()
        self.total_grainger_skus = 0

        if os.path.isfile(self.grainger_parquet):
            print(f"Loading Grainger catalog from Parquet: {self.grainger_parquet}")
            df_g = pd.read_parquet(self.grainger_parquet)
            self.total_grainger_skus = len(df_g)
            for _, row in df_g.iterrows():
                clean_m = clean_key(row.get("clean_mpn") or row.get("mpn"))
                item_no = str(row.get("root_mpn") or row.get("item_number") or "").strip()
                if clean_m and len(clean_m) >= 4 and clean_m not in STOPWORDS:
                    self.grainger_mpn_index[clean_m] = {
                        "item_number": item_no,
                        "model_no": str(row.get("mpn") or clean_m),
                        "title": str(row.get("title") or ""),
                        "page_num": int(row.get("page_num") or 0)
                    }
        elif os.path.isfile(self.grainger_db):
            print(f"Loading Grainger catalog from SQLite cache: {self.grainger_db}")
            with sqlite3.connect(self.grainger_db) as conn:
                cur = conn.cursor()
                cur.execute("SELECT item_number, mpn, description, page_num FROM grainger_items")
                rows = cur.fetchall()
                self.total_grainger_skus = len(rows)
                for item_no, mpn, desc, p_num in rows:
                    clean_m = clean_key(mpn)
                    if clean_m and len(clean_m) >= 4 and clean_m not in STOPWORDS:
                        self.grainger_mpn_index[clean_m] = {
                            "item_number": str(item_no).strip(),
                            "model_no": str(mpn).strip(),
                            "title": str(desc).strip(),
                            "page_num": int(p_num or 0)
                        }

        print(f"Indexed {self.total_grainger_skus:,} raw Grainger catalog items.")
        print(f"Active Clean MPN Search Keys: {len(self.grainger_mpn_index):,}")

    def collect_leads(self) -> List[Dict[str, Any]]:
        leads: List[Dict[str, Any]] = []
        candidate_paths = [
            "/app/data/live_sheet_leads.csv",
            "/app/data/reports/verified_high_confidence_matches.csv",
            "/app/data/reports/auto_verified_cross_matches.csv"
        ] + glob.glob("/app/data/*.csv")

        for cp in sorted(set(candidate_paths)):
            if not os.path.isfile(cp) or "grainger" in cp.lower():
                continue
            try:
                cdf = pd.read_csv(cp)
                cols = {c.lower(): c for c in cdf.columns}
                if "asin" in cols:
                    asin_col = cols["asin"]
                    for _, r in cdf.iterrows():
                        row_dict = r.to_dict()
                        if pd.notna(row_dict.get(asin_col)):
                            leads.append(row_dict)
            except Exception:
                pass

        print(f"Gathered {len(leads):,} candidate buy-sheet leads across source files.")
        return leads

    def match(self, leads: List[Dict[str, Any]]) -> pd.DataFrame:
        matches: List[Dict[str, Any]] = []

        for lead in leads:
            asin = str(lead.get("asin") or lead.get("ASIN") or "").strip()
            title = str(lead.get("title") or lead.get("Title") or lead.get("lead_title") or "").strip()
            current_cost = safe_float(lead.get("unit_cost") or lead.get("wholesale_cost") or lead.get("matched_cost") or lead.get("cost"))
            existing_vendor = str(lead.get("matched_vendor") or lead.get("vendor") or "EXISTING").strip()

            candidate_keys: Set[str] = set()
            for field in ["clean_mpn", "mpn", "MPN", "model", "part_number", "matched_key", "matched_sku"]:
                val = clean_key(lead.get(field))
                if len(val) >= 4 and val not in STOPWORDS:
                    candidate_keys.add(val)

            for token in re.findall(r"\b[A-Za-z0-9-]{4,20}\b", title):
                ck = clean_key(token)
                if ck not in STOPWORDS and any(ch.isdigit() for ch in ck):
                    candidate_keys.add(ck)

            for ck in candidate_keys:
                if ck in self.grainger_mpn_index:
                    g_item = self.grainger_mpn_index[ck]
                    item_no = g_item["item_number"]
                    direct_url = f"https://www.grainger.com/product/{item_no}"

                    matches.append({
                        "asin": asin,
                        "lead_title": title[:60],
                        "matched_key": ck,
                        "grainger_model_no": g_item["model_no"],
                        "grainger_item_no": item_no,
                        "grainger_url": direct_url,
                        "catalog_page": g_item["page_num"],
                        "current_pipeline_cost": current_cost,
                        "existing_vendor": existing_vendor,
                        "match_type": "EXACT_MODEL_MPN"
                    })

        if not matches:
            return pd.DataFrame()

        df_matches = pd.DataFrame(matches).drop_duplicates(subset=["asin", "grainger_item_no"])
        return df_matches

    def run(self) -> None:
        print("=" * 65)
        print("  GRAINGER TWO-STAGE LEAD MATCHER & URL GENERATOR")
        print("=" * 65)
        self.load_grainger_catalog()
        if not self.grainger_mpn_index:
            print("No indexed Grainger keys available. Ensure parsing cache is populated.")
            return

        leads = self.collect_leads()
        df_out = self.match(leads)

        os.makedirs(self.reports_dir, exist_ok=True)
        out_csv = os.path.join(self.reports_dir, "grainger_verified_opportunities.csv")
        out_json = os.path.join(self.reports_dir, "grainger_verified_opportunities.json")

        df_out.to_csv(out_csv, index=False)
        df_out.to_json(out_json, orient="records", indent=2)

        print("\n" + "=" * 65)
        print(f"VERIFIED GRAINGER OPPORTUNITIES FOUND: {len(df_out):,}")
        print("=" * 65)
        if not df_out.empty:
            display_cols = ["asin", "matched_key", "grainger_item_no", "grainger_url"]
            print(df_out[display_cols].head(15).to_string(index=False))
            print(f"\nSaved verified opportunities with direct URLs to:")
            print(f"  - {out_csv}")
            print(f"  - {out_json}")
        else:
            print("No immediate lead overlap identified against currently cached Grainger SKUs.")

def main():
    parser = argparse.ArgumentParser(description="Grainger Lead Matcher & Targeter")
    parser.add_argument("--run-once", action="store_true", default=True, help="Execute match cycle and exit")
    args = parser.parse_args()

    matcher = GraingerLeadMatcher()
    matcher.run()

if __name__ == "__main__":
    main()
EOF

chmod +x /app/tools/grainger_lead_matcher.py

# 2. Stage, commit, and push to GitHub origin main
cd /app
git add tools/grainger_lead_matcher.py
git commit -m "feat(grainger): add two-stage lead matcher and direct URL generator"
git push origin main
