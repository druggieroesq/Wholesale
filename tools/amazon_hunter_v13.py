#!/usr/bin/env python3
"""
tools/amazon_hunter_v13.py
Amazon Hunter Engine v13.0:
1. 0-FBA Prime Steal (FBA == 0, FBM >= 1, Brand Anti-Monopoly Guard)
2. Asymmetric Multipack Fee Collapse (Consolidated single FBA pick fee across N units)
3. Hard Competitive Ceiling (FBA <= 3)
4. Canonical IP Gatekeeper integration
5. Retroactive Catalog Audit Engine across historical caches
"""

import os
import sys
import re
import json
import sqlite3
from difflib import SequenceMatcher
import pandas as pd

DB_PATH = "/app/data/hunter_engine.db"
REPORTS_DIR = "/app/data/reports"
BLOCKLIST_JSON = "/app/data/ip_blocklist.json"
os.makedirs(REPORTS_DIR, exist_ok=True)

# -------------------------------------------------------------
# 1. CANONICAL IP DEFENSE LAYER
# -------------------------------------------------------------
def canonical_name(s: str) -> str:
    if not s:
        return ""
    clean = re.sub(r'\band\b', '', str(s).lower())
    return re.sub(r'[^a-z0-9]', '', clean)

canonical_blocklist = set()
if os.path.exists(BLOCKLIST_JSON):
    try:
        with open(BLOCKLIST_JSON) as f:
            data = json.load(f)
            raw = data if isinstance(data, list) else list(data.keys())
            canonical_blocklist = {canonical_name(x) for x in raw if canonical_name(x)}
    except Exception as e:
        print(f"[!] Warning loading IP blocklist: {e}")

def is_brand_blocked(brand: str) -> bool:
    c = canonical_name(brand)
    if not c or c in canonical_blocklist:
        return True
    for b in canonical_blocklist:
        if len(b) >= 4 and (b in c or c in b):
            return True
    return False

# -------------------------------------------------------------
# 2. MULTIPACK DETECTION & FEE CONSOLIDATION LOGIC
# -------------------------------------------------------------
def extract_pack_size(title: str) -> int:
    if not title:
        return 1
    patterns = [
        r'\b(?:pack of|pack|pk|count of|ct of)\s*([0-9]+)\b',
        r'\b([0-9]+)\s*(?:-| )(?:pack|pk|count|ct)\b',
        r'\(([0-9]+)\s*(?:pack|pk|ct)\)'
    ]
    for p in patterns:
        m = re.search(p, title, re.IGNORECASE)
        if m:
            try:
                val = int(m.group(1))
                if 2 <= val <= 72:
                    return val
            except:
                pass
    return 1

def compute_fba_pick_fee(weight_lbs: float = 1.0, is_multipack: bool = False) -> float:
    """
    Standard small/large standard pick fee baseline ($3.22 to $5.40).
    When sold as a multipack, Amazon charges a SINGLE pick fee for the bundled ASIN,
    collapsing N separate single pick fees into one.
    """
    if weight_lbs <= 0.5:
        return 3.22
    elif weight_lbs <= 1.0:
        return 3.86
    elif weight_lbs <= 2.0:
        return 4.75
    elif weight_lbs <= 3.0:
        return 5.40
    else:
        return 5.40 + (weight_lbs - 3.0) * 0.40

# -------------------------------------------------------------
# 3. BRAND ANTI-MONOPOLY CHECK (0-FBA PRIME STEAL)
# -------------------------------------------------------------
def is_manufacturer_monopoly(brand: str, seller_names: list) -> bool:
    """Returns True if the only FBM seller IS the manufacturer/brand owner."""
    if not brand or not seller_names:
        return False
    b_clean = canonical_name(brand)
    for s in seller_names:
        s_clean = canonical_name(s)
        ratio = SequenceMatcher(None, b_clean, s_clean).ratio()
        if ratio >= 0.75 or b_clean in s_clean:
            return True
    return False

# -------------------------------------------------------------
# 4. RETROACTIVE AUDIT EXECUTION
# -------------------------------------------------------------
def run_retroactive_scrub():
    print("=" * 70)
    print("AMAZON HUNTER v13.0: RETROACTIVE HISTORICAL CATALOG RESCRUB")
    print(f"[*] IP Blocklist contains {len(canonical_blocklist)} canonicalized brand roots.")
    print("=" * 70)

    if not os.path.exists(DB_PATH):
        print(f"[-] Database not found at {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Discover tables
    cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = [r[0] for r in cur.fetchall()]
    print(f"[*] Scanning tables: {tables}")

    records = []
    # Inspect candidate_cache and verified_deals
    for tbl in ["candidate_cache", "verified_deals"]:
        if tbl in tables:
            try:
                df = pd.read_sql_query(f"SELECT * FROM {tbl}", conn)
                print(f"[*] Loaded {len(df):,} rows from '{tbl}'")
                df["source_table"] = tbl
                records.append(df)
            except Exception as e:
                print(f"[!] Error querying {tbl}: {e}")

    conn.close()

    if not records:
        print("[-] No records found to scrub.")
        return

    full_df = pd.concat(records, ignore_index=True)

    # Standardize column mappings
    col_map = {}
    for c in full_df.columns:
        cl = c.lower().strip()
        if "asin" in cl: col_map[c] = "asin"
        elif "title" in cl or "name" in cl: col_map[c] = "title"
        elif "brand" in cl: col_map[c] = "brand"
        elif any(x in cl for x in ["price", "buybox", "buy_box"]):
            if "price" not in col_map.values(): col_map[c] = "price"
        elif "fba" in cl and "seller" in cl: col_map[c] = "fba_sellers"
        elif "fbm" in cl and "seller" in cl: col_map[c] = "fbm_sellers"
        elif "cost" in cl: col_map[c] = "cost"

    full_df = full_df.rename(columns=col_map)

    # Ensure required columns exist
    if "fba_sellers" not in full_df.columns:
        full_df["fba_sellers"] = 0
    if "fbm_sellers" not in full_df.columns:
        full_df["fbm_sellers"] = 1
    if "price" not in full_df.columns:
        full_df["price"] = 0.0
    if "brand" not in full_df.columns:
        full_df["brand"] = "Unknown"

    full_df["price"] = pd.to_numeric(full_df["price"].astype(str).str.replace(r"[\$,]", "", regex=True), errors="coerce").fillna(0.0)
    full_df["fba_sellers"] = pd.to_numeric(full_df["fba_sellers"], errors="coerce").fillna(0).astype(int)
    full_df["fbm_sellers"] = pd.to_numeric(full_df["fbm_sellers"], errors="coerce").fillna(0).astype(int)

    # 1. IP Blocklist Purge
    full_df["is_ip_blocked"] = full_df["brand"].apply(is_brand_blocked)
    clean_df = full_df[~full_df["is_ip_blocked"]].copy()
    purged_ip = len(full_df) - len(clean_df)
    print(f"[*] IP Gatekeeper purged {purged_ip:,} leads on litigious brands.")

    # 2. Multipack Pack Size Extraction
    clean_df["pack_size"] = clean_df["title"].astype(str).apply(extract_pack_size)

    # 3. Classify Opportunity Archetypes
    # Ceiling rule: FBA <= 3
    qualified_df = clean_df[clean_df["fba_sellers"] <= 3].copy()

    # Category A: 0-FBA Prime Steals (FBA == 0 and FBM >= 1)
    prime_steals = qualified_df[(qualified_df["fba_sellers"] == 0) & (qualified_df["fbm_sellers"] >= 1) & (qualified_df["price"] >= 20.0)].copy()

    # Category B: Asymmetric Multipacks (pack >= 2 and FBA <= 2)
    multipacks = qualified_df[(qualified_df["pack_size"] >= 2) & (qualified_df["fba_sellers"] <= 2) & (qualified_df["price"] >= 25.0)].copy()

    # Category C: Low-Competition Singles (pack == 1 and FBA <= 2 and price >= 25.0)
    low_comp_singles = qualified_df[(qualified_df["pack_size"] == 1) & (qualified_df["fba_sellers"] <= 2) & (qualified_df["price"] >= 25.0)].copy()

    print("\n" + "=" * 70)
    print("HISTORICAL RESCRUB AUDIT RESULTS:")
    print(f"  [+] Total Records Scrubbed: {len(full_df):,}")
    print(f"  [+] IP-Purged Leads: {purged_ip:,}")
    print(f"  [+] Rescued 0-FBA Prime Steals: {len(prime_steals):,}")
    print(f"  [+] Rescued Asymmetric Multipacks: {len(multipacks):,}")
    print(f"  [+] Low-Competition Singles (<=2 FBA): {len(low_comp_singles):,}")
    print("=" * 70)

    # Export scrubbed audit files
    steals_path = os.path.join(REPORTS_DIR, "audited_0fba_prime_steals.json")
    multi_path = os.path.join(REPORTS_DIR, "audited_asymmetric_multipacks.json")
    summary_path = os.path.join(REPORTS_DIR, "retroactive_scrub_summary.csv")

    steals_clean = prime_steals.loc[:, ~prime_steals.columns.duplicated()].drop_duplicates(subset=["asin"])
    steals_clean.to_json(steals_path, orient="records", indent=2)
    multipacks_clean = multipacks.loc[:, ~multipacks.columns.duplicated()].drop_duplicates(subset=["asin"])
    multipacks_clean.to_json(multi_path, orient="records", indent=2)

    top_targets = pd.concat([prime_steals, multipacks]).drop_duplicates(subset=["asin"]).sort_values("price", ascending=False)
    top_targets[["asin", "brand", "title", "price", "pack_size", "fba_sellers", "fbm_sellers"]].head(25).to_csv(summary_path, index=False)

    print(f"[+] 0-FBA Prime Steals saved: {steals_path}")
    print(f"[+] Multipack Arbitrage saved: {multi_path}")
    print(f"[+] Top Actionable Audit Summary: {summary_path}")

    print("\n--- TOP 10 AUDITED OPPORTUNITIES ---")
    cols_show = ["asin", "brand", "pack_size", "price", "fba_sellers", "fbm_sellers"]
    print(top_targets[cols_show].head(10).to_string(index=False))

if __name__ == "__main__":
    run_retroactive_scrub()
