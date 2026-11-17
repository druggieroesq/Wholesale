#!/usr/bin/env python3
"""
tools/audit_backlog_leads.py (Checklist Item #321)
--------------------------------------------------
Backlog Lead & ASIN Rescan Audit against SP-API & Normalized Wholesale Parquet Catalogs:
1. Cross-references backlog ASINs from candidate_cache + exported Google Sheets CSV archive
   against all indexed wholesale Parquet feeds (EE Distribution, Motion, Zoro, BCI, etc.).
2. Filters out all 90+ conglomerate/IP-blacklisted brands (data/ip_blocklist.json) and hazmat items.
3. Queries AmazonGatekeeper.evaluate_asin_approval(asin) for live SP-API eligibility.
4. Isolates newly ungated hits with Net ROI >= 15%, saves them to:
     /app/data/reports/newly_ungated_backlog_hits.csv
   and promotes them into verified_deals + triggers sync_today_leads_to_sheet.py.
"""

import os
import sys
import csv
import json
import time
import logging
import sqlite3
import argparse
from pathlib import Path
from typing import Dict, Any, List, Optional

import pandas as pd
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

load_dotenv(ROOT_DIR / ".env", override=True)

from core.amazon_gatekeeper import AmazonGatekeeper
from core.engine import IndustrialCatalogEngine, DatabaseManager
from config import get_logistics_profile

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] BacklogAudit: %(message)s"
)
logger = logging.getLogger("BacklogAudit")

DB_PATH = os.getenv("DB_PATH", "/app/data/hunter_engine.db")
if not os.path.exists(DB_PATH) and (ROOT_DIR / "data" / "hunter_engine.db").exists():
    DB_PATH = str(ROOT_DIR / "data" / "hunter_engine.db")

PARQUET_DIR = os.getenv("INDEXED_CATALOG_DIR", "/app/data/catalogs/wholesale_catalogs/indexed")
if not os.path.exists(PARQUET_DIR):
    PARQUET_DIR = str(ROOT_DIR / "data" / "catalogs" / "wholesale_catalogs" / "indexed")

REPORTS_DIR = Path(os.getenv("REPORTS_DIR", "/app/data/reports"))
if not REPORTS_DIR.parent.exists():
    REPORTS_DIR = ROOT_DIR / "data" / "reports"

BLOCKLIST_PATH = Path(os.getenv("IP_BLOCKLIST_PATH", "/app/data/ip_blocklist.json"))
if not BLOCKLIST_PATH.exists():
    BLOCKLIST_PATH = ROOT_DIR / "data" / "ip_blocklist.json"


def load_blocklist() -> set:
    if not BLOCKLIST_PATH.exists():
        return set()
    try:
        raw = json.loads(BLOCKLIST_PATH.read_text(encoding="utf-8"))
        items = raw if isinstance(raw, list) else raw.get("brands", [])
        return {str(b).upper().strip() for b in items if b}
    except Exception:
        return set()


def run_backlog_audit(
    archive_csv: Optional[str] = None,
    min_roi_pct: float = 15.0,
    max_asins: int = 300,
    push_to_sheet: bool = True,
) -> pd.DataFrame:
    blocked = load_blocklist()
    gatekeeper = AmazonGatekeeper()

    # Load all normalized wholesale Parquet catalogs (EE Dist, Motion, Zoro, BCI, etc.)
    db_mgr = DatabaseManager(db_path=str(ROOT_DIR / "data" / "mro_pipeline.db"))
    cat_engine = IndustrialCatalogEngine(db_manager=db_mgr, parquet_dir=PARQUET_DIR)
    cat_engine.build_index()

    candidates: List[Dict[str, Any]] = []

    # Source A: SQLite candidate_cache backlog
    if os.path.exists(DB_PATH):
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT asin, title, brand, upc, mpn, sales_rank, avg90_bb, current_bb,
                   monthly_sold, fba_fee, referral_pct, weight_lbs, is_hazmat
            FROM candidate_cache
            WHERE asin IS NOT NULL
              AND COALESCE(is_hazmat, 0) = 0
              AND COALESCE(status, '') NOT IN ('BLOCKED_IP_BRAND', 'REJECTED')
        """)
        for r in cur.fetchall():
            candidates.append(dict(r))
        conn.close()

    # Source B: Optional Google Sheets CSV archive export
    if archive_csv and os.path.exists(archive_csv):
        with open(archive_csv, "r", encoding="utf-8-sig", errors="replace") as f:
            rows = list(csv.reader(f))
        hdr_idx = next((i for i, r in enumerate(rows[:10]) if "ASIN" in r and "Brand" in r), 0)
        headers = rows[hdr_idx]
        col_map = {h.strip(): i for i, h in enumerate(headers)}

        def val(r: List[str], key: str) -> str:
            for k, idx in col_map.items():
                if key.lower() in k.lower() and idx < len(r):
                    return r[idx].strip()
            return ""

        for r in rows[hdr_idx + 1 :]:
            asin = val(r, "ASIN")
            if not asin or val(r, "Hazmat").upper() == "YES":
                continue
            candidates.append({
                "asin": asin,
                "brand": val(r, "Brand"),
                "title": val(r, "Amazon Title"),
                "avg90_bb": float(val(r, "Buy Box Price").replace("$", "") or 0),
                "archive_cogs": float(val(r, "Landed Bundle COGS").replace("$", "") or 0),
                "archive_vendor": val(r, "Wholesale Supplier"),
                "sales_rank": int(float(val(r, "90D Avg BSR") or 45000)),
            })

    # Deduplicate by ASIN
    seen = set()
    unique_candidates = []
    for c in candidates:
        a = str(c.get("asin") or "").strip().upper()
        if a and a not in seen:
            seen.add(a)
            unique_candidates.append(c)

    logger.info(f"Scanning {len(unique_candidates):,} unique non-hazmat backlog ASINs...")
    hits: List[Dict[str, Any]] = []
    audited = 0

    for item in unique_candidates:
        if audited >= max_asins:
            break
        asin = item["asin"]
        brand = str(item.get("brand") or "").strip()
        if not brand or brand.upper() in blocked:
            continue

        # Match against freshly indexed wholesale catalogs (EE Dist, Motion, Zoro, BCI)
        matched_rec = None
        upc = str(item.get("upc") or "").strip()
        mpn = str(item.get("mpn") or "").strip().upper()
        if upc and upc in cat_engine.catalog_barcodes:
            matched_rec = cat_engine.catalog_barcodes[upc]
        elif mpn and f"{brand.upper()}:{mpn}" in cat_engine.catalog_brand_mpns:
            matched_rec = cat_engine.catalog_brand_mpns[f"{brand.upper()}:{mpn}"]

        bb = float(item.get("avg90_bb") or item.get("current_bb") or 0.0)
        ws_cost = float(
            (matched_rec.get("unit_cost") if matched_rec else 0.0)
            or item.get("archive_cogs")
            or 0.0
        )
        vendor = (
            (matched_rec.get("vendor") if matched_rec else None)
            or item.get("archive_vendor")
            or "Wholesale"
        )
        if bb <= 0 or ws_cost <= 0 or ws_cost >= bb:
            continue

        ref_pct = float(item.get("referral_pct") or 0.15)
        fba_fee = float(item.get("fba_fee") or 4.50)
        logistics = get_logistics_profile(vendor)
        prep = logistics["prep_base"] + (float(item.get("weight_lbs") or 1.0) * logistics["freight_per_lb"])
        net_profit = round(bb - ws_cost - (bb * ref_pct) - fba_fee - prep, 2)
        roi_pct = round((net_profit / ws_cost) * 100.0, 1)

        if net_profit <= 0 or roi_pct < min_roi_pct:
            continue

        # Live SP-API check
        gate = gatekeeper.evaluate_asin_approval(asin)
        audited += 1
        g_status = gate.get("status", "")
        if g_status == "MOCK_OFFLINE":
            logger.error("SP-API credentials offline; stopping audit.")
            break

        if g_status in ("UNRESTRICTED", "AUTO_UNGATED"):
            hit = {
                "asin": asin,
                "brand": brand,
                "title": str(item.get("title") or "")[:80],
                "vendor": vendor,
                "buy_box_price": bb,
                "wholesale_cost": ws_cost,
                "net_profit": net_profit,
                "roi_pct": roi_pct,
                "gating_status": g_status,
                "prep_facility": logistics["facility"],
            }
            hits.append(hit)
            logger.info(f"NEWLY UNGATED HIT -> {asin} | {brand} | {vendor} | Net=${net_profit:.2f} ({roi_pct:.1f}% ROI)")

        time.sleep(0.25)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_csv = REPORTS_DIR / "newly_ungated_backlog_hits.csv"
    df_hits = pd.DataFrame(hits)
    if not df_hits.empty:
        df_hits = df_hits.sort_values(by=["roi_pct", "net_profit"], ascending=[False, False])
        df_hits.to_csv(out_csv, index=False)
        logger.info(f"Saved {len(df_hits)} newly ungated hits (ROI >= {min_roi_pct}%) to {out_csv}")

        if os.path.exists(DB_PATH):
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            for h in hits:
                cur.execute("""
                    INSERT OR REPLACE INTO verified_deals (
                        asin, title, brand, vendor, buy_box_price, wholesale_cost,
                        net_profit, roi_percent, sales_rank, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 45000, 'READY_TO_BUY')
                """, (
                    h["asin"], h["title"], h["brand"], h["vendor"],
                    h["buy_box_price"], h["wholesale_cost"], h["net_profit"], h["roi_pct"]
                ))
            conn.commit()
            conn.close()

        if push_to_sheet:
            try:
                from tools.sync_today_leads_to_sheet import run_pipeline_sync
                run_pipeline_sync()
            except Exception as exc:
                logger.warning(f"Sheet sync notice: {exc}")
    else:
        logger.info(f"Audited {audited} qualifying backlog ASINs; 0 newly unlocked hits found.")

    return df_hits


def main():
    parser = argparse.ArgumentParser(description="Backlog ASIN rescan audit against SP-API & wholesale catalogs.")
    parser.add_argument("--archive-csv", default=None, help="Optional Google Sheets Sourcing & Buy List CSV export")
    parser.add_argument("--min-roi", type=float, default=15.0, help="Minimum Net ROI % threshold")
    parser.add_argument("--max-asins", type=int, default=300, help="Maximum ASINs to query in this pass")
    parser.add_argument("--no-sheet-sync", action="store_true", help="Skip triggering Google Sheets webhook sync")
    args = parser.parse_args()

    run_backlog_audit(
        archive_csv=args.archive_csv,
        min_roi_pct=args.min_roi,
        max_asins=args.max_asins,
        push_to_sheet=not args.no_sheet_sync,
    )


if __name__ == "__main__":
    main()
