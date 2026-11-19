#!/usr/bin/env python3
"""
tools/push_full_backlog_to_sheet.py (v2.0 — BSR-Aware Full Database & Catalog Streamer)
--------------------------------------------------------------------------------------
Permanent repository script for streaming all qualifying deals from data/hunter_engine.db
into Google Sheets (Code.gs v20.5+):
  1. Resolves real monthly velocity via: monthly_sold -> drops_90 -> sales_rank (BSR)
     so commercial/industrial SKUs without a retail monthlySold badge are never dropped.
  2. Joins both verified_deals AND candidate_cache against all indexed distributor catalogs
     (BCI, Motion Industries, Zoro, EE Distribution, Marlo Beauty, JB Tools, Atlantic Spice)
     and brand_gating_state (SP-API UNRESTRICTED -> is_ungated='YES').
  3. Enforces >= 50 u/mo minimum velocity floor, <= 6 FBA sellers, and IP blocklist filtering.
  4. Streams in 100-row batches with skip_sync=True on intermediate chunks and full dashboard
     rebuild (Brand Opportunity Matrix, Distributor Breakdown, Ready for Purchase) on final chunk.
"""

import os
import re
import sys
import time
import json
import logging
import sqlite3
import argparse
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
import pandas as pd
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

load_dotenv(ROOT_DIR / ".env", override=True)

from config import get_verified_webhook_url, get_logistics_profile

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] BacklogStreamer: %(message)s"
)
logger = logging.getLogger("BacklogStreamer")

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
if not DATA_DIR.exists():
    DATA_DIR = ROOT_DIR / "data"

DB_PATH = DATA_DIR / "hunter_engine.db"
CATALOG_DIR = DATA_DIR / "catalogs"
PRIMARY_IP_FILE = DATA_DIR / "ip_blocklist.json"

PACK_REGEX = re.compile(
    r"(?:(\d+)\s*[- ]*(?:pack|pk|count|ct|pcs|pieces|set|bundle)|pack\s*of\s*(\d+)|set\s*of\s*(\d+))",
    re.IGNORECASE,
)

EXTRA_DIRECT_BRAND_BLOCKS = {
    "JAKE SALES", "TOTOLY JAKE", "ICARCOVER", "SUMMIT HYDRAULICS",
    "OSLTWI", "LIONMAX", "JACKSON PALMER", "KKBESTPACK", "DURAMIC 3D",
    "ESUN", "FLUKE", "HONEYWELL", "RESMED"
}


def load_blocked_brands() -> Set[str]:
    blocked = set(EXTRA_DIRECT_BRAND_BLOCKS)
    if PRIMARY_IP_FILE.exists():
        try:
            raw = json.loads(PRIMARY_IP_FILE.read_text(encoding="utf-8"))
            items = raw if isinstance(raw, list) else raw.get("brands", [])
            blocked |= {str(b).upper().strip() for b in items if b}
        except Exception as exc:
            logger.warning(f"Could not read IP blocklist: {exc}")
    return blocked


def estimate_real_velocity(monthly_sold: Any, drops_90: Any, bsr: Any) -> int:
    ms = int(monthly_sold or 0)
    if ms > 0 and ms not in (20, 25):
        return ms

    d90 = int(drops_90 or 0)
    if d90 > 0:
        return max(1, round((d90 / 3.0) * 1.15))

    rank = int(bsr or 0)
    if rank > 0:
        if rank <= 500:
            return 450
        if rank <= 1500:
            return 220
        if rank <= 5000:
            return 110
        if rank <= 15000:
            return 55
        if rank <= 35000:
            return 28
        if rank <= 75000:
            return 14
        if rank <= 150000:
            return 6
        if rank <= 300000:
            return 2
    return 0


def extract_pack_qty(title: str, db_pack: Any) -> int:
    try:
        p_val = int(db_pack or 1)
        if 2 <= p_val <= 48:
            return p_val
    except Exception:
        pass

    if title:
        m = PACK_REGEX.search(title)
        if m:
            detected = int(m.group(1) or m.group(2) or m.group(3) or 1)
            if 2 <= detected <= 48:
                return detected
    return 1


def normalize_upc(val: Any) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    s = str(val).strip()
    if "e+" in s.lower():
        try:
            s = f"{int(float(s))}"
        except Exception:
            pass
    digits = re.sub(r"\D", "", s).lstrip("0")
    return digits if len(digits) >= 8 else ""


def detect_vendor_from_path(filepath: Path) -> str:
    fn = str(filepath).lower()
    if "bci" in fn:
        return "BCI"
    if "motion" in fn:
        return "Motion Industries"
    if "zoro" in fn:
        return "Zoro"
    if "ee_dist" in fn or "eedist" in fn or "entertainment" in fn:
        return "EE Distribution"
    if "marlo" in fn:
        return "Marlo Beauty Supply"
    if "jb_tool" in fn or "jbtool" in fn:
        return "JB Tools"
    if "atlantic" in fn:
        return "Atlantic Spice"
    if "grainger" in fn:
        return "Grainger"
    if "frontier" in fn:
        return "Frontier Co-op"
    if "contour" in fn:
        return "Contour Abrasives"
    return filepath.stem


def load_catalog_indexes() -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str]]:
    upc_map: Dict[str, Dict[str, Any]] = {}
    brand_map: Dict[str, str] = {}
    if not CATALOG_DIR.exists():
        return upc_map, brand_map

    files = list(CATALOG_DIR.rglob("*.csv")) + list(CATALOG_DIR.rglob("*.xlsx"))
    for f in files:
        try:
            df = pd.read_csv(f, dtype=str) if f.suffix.lower() == ".csv" else pd.read_excel(f, dtype=str)
            if df.empty:
                continue
            df.columns = [str(c).strip().lower() for c in df.columns]
            cost_c = next((c for c in df.columns if any(k in c for k in ("wholesale", "unit_cost", "cost", "price"))), None)
            upc_c = next((c for c in df.columns if any(k in c for k in ("upc", "ean", "gtin"))), None)
            brand_c = next((c for c in df.columns if any(k in c for k in ("brand", "manufacturer", "mfr"))), None)
            if not cost_c:
                continue

            vendor = detect_vendor_from_path(f)
            for _, r in df.iterrows():
                try:
                    c_val = float(str(r.get(cost_c, "")).replace("$", "").replace(",", "").strip())
                except Exception:
                    continue
                if c_val <= 0.05:
                    continue
                if upc_c:
                    u = normalize_upc(r.get(upc_c))
                    if u and (u not in upc_map or c_val < upc_map[u]["unit_cost"]):
                        upc_map[u] = {"vendor": vendor, "unit_cost": c_val}
                if brand_c and pd.notna(r.get(brand_c)):
                    bu = str(r.get(brand_c)).upper().strip()
                    if len(bu) >= 2 and bu not in brand_map:
                        brand_map[bu] = vendor
        except Exception:
            continue

    return upc_map, brand_map


def get_table_columns(cur: sqlite3.Cursor, table_name: str) -> Set[str]:
    try:
        return {row[1] for row in cur.execute(f"PRAGMA table_info({table_name})").fetchall()}
    except Exception:
        return set()


def build_full_pipeline_payloads(min_velocity: int = 50, min_roi: float = 15.0, max_leads: int = 5000) -> List[Dict[str, Any]]:
    if not DB_PATH.exists():
        logger.error(f"SQLite database not found at {DB_PATH}")
        return []

    blocked_brands = load_blocked_brands()
    upc_map, brand_map = load_catalog_indexes()
    payloads: List[Dict[str, Any]] = []
    seen_asins: Set[str] = set()

    with closing(sqlite3.connect(DB_PATH, timeout=60.0)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 60000")
        cur = conn.cursor()

        vd_cols = get_table_columns(cur, "verified_deals")
        cc_cols = get_table_columns(cur, "candidate_cache")
        has_gating = bool(get_table_columns(cur, "brand_gating_state"))

        ungated_brands: Set[str] = set()
        if has_gating:
            for gr in cur.execute("SELECT UPPER(TRIM(brand)) AS b FROM brand_gating_state WHERE status = 'UNRESTRICTED'").fetchall():
                if gr["b"]:
                    ungated_brands.add(gr["b"])

        # 1. Verified Deals first
        if vd_cols:
            cur.execute("""
                SELECT * FROM verified_deals
                WHERE status IS NULL OR UPPER(TRIM(status)) NOT IN ('REJECTED', 'BLOCKED_IP_BRAND', 'DELETED')
                ORDER BY monthly_profit_pool DESC, roi_percent DESC
            """)
            for row in cur.fetchall():
                d = dict(row)
                asin = str(d.get("asin") or "").strip().upper()
                brand = str(d.get("brand") or "").strip()
                brand_upper = brand.upper()
                if not asin or len(asin) != 10 or asin in seen_asins or not brand or brand_upper in blocked_brands:
                    continue

                bb = float(d.get("buy_box_price") or 0.0)
                cost = float(d.get("wholesale_cost") or 0.0)
                if bb < 13.50 or cost <= 0.05 or cost >= bb:
                    continue

                c_row = cur.execute("SELECT * FROM candidate_cache WHERE asin = ?", (asin,)).fetchone() if cc_cols else None
                cd = dict(c_row) if c_row else {}
                fba = int(cd.get("fba_sellers") if cd.get("fba_sellers") is not None else (d.get("competitive_fba") or 0))
                rank = int(cd.get("sales_rank") or d.get("sales_rank") or 0)
                drops = int(cd.get("drops_90") or 0)
                vel = estimate_real_velocity(cd.get("monthly_sold") or d.get("monthly_velocity"), drops, rank)
                if fba > 6 or vel < min_velocity:
                    continue

                vendor = str(d.get("vendor") or "").strip()
                if not vendor or "TARGET BRAND" in vendor.upper() or vendor.upper() in ("UNKNOWN", "NONE", "UNMAPPED"):
                    vendor = brand_map.get(brand_upper, "Unmapped (Needs Catalog Match)")

                logistics = get_logistics_profile(vendor)
                seen_asins.add(asin)
                payloads.append({
                    "asin": asin,
                    "title": str(d.get("title") or "").strip(),
                    "brand": brand,
                    "vendor": vendor,
                    "prep_center": logistics["facility"],
                    "pack_count": extract_pack_qty(d.get("title"), cd.get("amazon_pack_qty")),
                    "sales_rank": rank,
                    "monthly_velocity": vel,
                    "drops_90": drops,
                    "competitive_fba": fba,
                    "total_sellers": max(2, int(cd.get("num_sellers") or (fba + 1))),
                    "buy_box_price": bb,
                    "current_bb": float(cd.get("current_bb") or bb),
                    "avg90_bb": float(cd.get("avg90_bb") or bb),
                    "wholesale_cost": cost,
                    "landed_bundle_cogs": cost,
                    "net_profit": float(d.get("net_profit") or 0.0),
                    "roi_percent": float(d.get("roi_percent") or 0.0),
                    "is_hazmat": "YES" if d.get("is_hazmat") else "NO",
                    "is_ungated": "YES" if (brand_upper in ungated_brands or str(d.get("status") or "").upper() == "UNGATED_READY") else "NO",
                })

        # 2. All qualifying candidate_cache ASINs (BSR / drops_90 / monthly_sold >= min_velocity)
        if cc_cols and len(payloads) < max_leads:
            cur.execute("""
                SELECT * FROM candidate_cache
                WHERE status IS NULL OR UPPER(TRIM(status)) NOT IN ('BLOCKED_IP_BRAND', 'REJECTED', 'DELETED')
                ORDER BY monthly_sold DESC, drops_90 DESC, sales_rank ASC
            """)
            for row in cur.fetchall():
                if len(payloads) >= max_leads:
                    break
                d = dict(row)
                asin = str(d.get("asin") or "").strip().upper()
                brand = str(d.get("brand") or "").strip()
                brand_upper = brand.upper()
                if not asin or len(asin) != 10 or asin in seen_asins or not brand or brand_upper in blocked_brands:
                    continue

                bb = float(d.get("avg90_bb") or d.get("current_bb") or 0.0)
                if bb < 13.50:
                    continue

                fba = int(d.get("fba_sellers") or 0)
                rank = int(d.get("sales_rank") or 0)
                drops = int(d.get("drops_90") or 0)
                vel = estimate_real_velocity(d.get("monthly_sold"), drops, rank)
                if fba > 6 or vel < min_velocity:
                    continue

                title = str(d.get("title") or "").strip()
                pack = extract_pack_qty(title, d.get("amazon_pack_qty"))
                matched = None
                for uf in ("upc", "ean"):
                    u = normalize_upc(d.get(uf))
                    if u and u in upc_map:
                        matched = upc_map[u]
                        break

                if matched:
                    vendor = matched["vendor"]
                    ws_cost = round((matched["unit_cost"] * pack) + 1.25, 2)
                else:
                    vendor = brand_map.get(brand_upper, "Unmapped (Needs Catalog Match)")
                    ws_cost = round(bb * 0.36, 2)

                if ws_cost <= 0.05 or ws_cost >= bb:
                    continue

                ref_pct = float(d.get("referral_pct") or 0.15)
                fba_fee = float(d.get("fba_fee") or max(3.50, bb * 0.18))
                net_profit = round(bb - (bb * ref_pct) - fba_fee - ws_cost, 2)
                if net_profit < 1.50:
                    continue

                roi_pct = round((net_profit / ws_cost) * 100.0, 1)
                if roi_pct < min_roi or roi_pct > 350.0:
                    continue

                logistics = get_logistics_profile(vendor)
                seen_asins.add(asin)
                payloads.append({
                    "asin": asin,
                    "title": title,
                    "brand": brand,
                    "vendor": vendor,
                    "prep_center": logistics["facility"],
                    "pack_count": pack,
                    "sales_rank": rank,
                    "monthly_velocity": vel,
                    "drops_90": drops,
                    "competitive_fba": fba,
                    "total_sellers": max(2, int(d.get("num_sellers") or (fba + 1))),
                    "buy_box_price": bb,
                    "current_bb": float(d.get("current_bb") or bb),
                    "avg90_bb": bb,
                    "wholesale_cost": ws_cost,
                    "landed_bundle_cogs": ws_cost,
                    "net_profit": net_profit,
                    "roi_percent": roi_pct,
                    "referral_pct": ref_pct,
                    "fba_fee": fba_fee,
                    "is_hazmat": "YES" if d.get("is_hazmat") else "NO",
                    "is_ungated": "YES" if brand_upper in ungated_brands else "NO",
                })

    return payloads


def post_batch_with_retry(webhook_url: str, action: str, batch: List[Dict[str, Any]], skip_sync: bool, max_retries: int = 3) -> Optional[Dict[str, Any]]:
    body = {"action": action, "leads": batch, "skip_sync": skip_sync}
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(webhook_url, json=body, timeout=60, allow_redirects=True)
            if resp.status_code == 200:
                try:
                    return resp.json()
                except Exception:
                    return {"status": "ok", "raw": resp.text[:200]}
            logger.warning(f"Attempt {attempt}/{max_retries} returned HTTP {resp.status_code}: {resp.text[:160]}")
        except Exception as exc:
            logger.warning(f"Attempt {attempt}/{max_retries} exception: {exc}")
        time.sleep(2.5 * attempt)
    return None


def stream_backlog(batch_size: int = 100, min_velocity: int = 50, min_roi: float = 15.0, max_leads: int = 5000, wipe_first: bool = False, dry_run: bool = False):
    webhook_url = get_verified_webhook_url()
    if not webhook_url and not dry_run:
        logger.error("No Google Sheets webhook URL configured.")
        sys.exit(1)

    leads = build_full_pipeline_payloads(min_velocity=min_velocity, min_roi=min_roi, max_leads=max_leads)
    total = len(leads)
    dist_counts = Counter(l["vendor"] for l in leads)

    logger.info(f"Prepared {total:,} qualifying {min_velocity}+ u/mo leads across database.")
    for dist, cnt in dist_counts.most_common():
        logger.info(f"   -> {dist}: {cnt:,}")

    if total == 0 or dry_run:
        return

    total_batches = (total + batch_size - 1) // batch_size
    total_admitted = 0

    for idx in range(0, total, batch_size):
        batch_num = (idx // batch_size) + 1
        chunk = leads[idx:idx + batch_size]
        is_last = (batch_num == total_batches)
        action = "BATCH_UPDATE_LEADS" if (wipe_first and batch_num == 1) else "APPEND_LEADS"

        logger.info(f"Streaming batch {batch_num}/{total_batches} ({len(chunk)} leads, skip_sync={not is_last})...")
        result = post_batch_with_retry(webhook_url, action, chunk, skip_sync=(not is_last))
        if result and result.get("status") == "success":
            admitted = int(result.get("count", len(chunk)))
            total_admitted += admitted
            logger.info(
                f"Batch {batch_num}/{total_batches} synced -> admitted={admitted}, "
                f"sheet_total={result.get('total_rows')}, matrix_brands={result.get('matrix_brands_synced')}, "
                f"distributors={result.get('distributors_synced')}, ready_to_buy={result.get('ready_for_purchase_synced')}"
            )
        else:
            logger.error(f"Batch {batch_num}/{total_batches} failed: {result}")

        time.sleep(0.9)

    logger.info(f"Full backlog sync complete! Total leads admitted: {total_admitted:,} / {total:,}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BSR-Aware Full Database & Catalog Backlog Streamer")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--min-velocity", type=int, default=50)
    parser.add_argument("--min-roi", type=float, default=15.0)
    parser.add_argument("--max-leads", type=int, default=5000)
    parser.add_argument("--wipe-first", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    stream_backlog(
        batch_size=args.batch_size,
        min_velocity=args.min_velocity,
        min_roi=args.min_roi,
        max_leads=args.max_leads,
        wipe_first=args.wipe_first,
        dry_run=args.dry_run,
    )
