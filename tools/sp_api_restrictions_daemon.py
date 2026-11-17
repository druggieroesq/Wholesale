#!/usr/bin/env python3
"""
tools/sp_api_restrictions_daemon.py (Checklist Item #317)
---------------------------------------------------------
Automated SP-API Listings Restrictions Daemon for Railway.
Runs on a 6-hour loop (or --once):
1. Queries candidate_cache in /app/data/hunter_engine.db for unverified/pending ASINs.
2. Evaluates each ASIN via AmazonGatekeeper.evaluate_asin_approval(asin)
   (with Q4 Holiday Toy + CPC/ASTM safety detection and automatic soft-ungate POST).
3. Updates candidate_cache status and promotes UNRESTRICTED / AUTO_UNGATED deals
   with Net ROI >= 15% into verified_deals and triggers sync_today_leads_to_sheet.py.
"""

import os
import sys
import json
import time
import logging
import sqlite3
import argparse
from pathlib import Path
from typing import Dict, Any, List

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

load_dotenv(ROOT_DIR / ".env", override=True)

from core.amazon_gatekeeper import AmazonGatekeeper
from config import get_logistics_profile, validate_fba_competition

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] SPAPIRestrictionsDaemon: %(message)s"
)
logger = logging.getLogger("SPAPIRestrictionsDaemon")

DB_PATH = os.getenv("DB_PATH", "/app/data/hunter_engine.db")
if not os.path.exists(DB_PATH) and (ROOT_DIR / "data" / "hunter_engine.db").exists():
    DB_PATH = str(ROOT_DIR / "data" / "hunter_engine.db")

BLOCKLIST_PATH = Path(os.getenv("IP_BLOCKLIST_PATH", "/app/data/ip_blocklist.json"))
if not BLOCKLIST_PATH.exists():
    BLOCKLIST_PATH = ROOT_DIR / "data" / "ip_blocklist.json"

SIX_HOURS_SECONDS = int(os.getenv("SP_API_AUDIT_INTERVAL_SECONDS", 21600))


def load_blocklist() -> set:
    if not BLOCKLIST_PATH.exists():
        return set()
    try:
        raw = json.loads(BLOCKLIST_PATH.read_text(encoding="utf-8"))
        items = raw if isinstance(raw, list) else raw.get("brands", [])
        return {str(b).upper().strip() for b in items if b}
    except Exception:
        return set()


def ensure_tracking_columns(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(candidate_cache)")
    existing_cols = {row[1] for row in cur.fetchall()}
    for col_name, col_type in [
        ("gating_status", "TEXT"),
        ("gating_reasons", "TEXT"),
        ("last_gating_check", "TEXT"),
        ("wholesale_cost", "REAL"),
        ("matched_vendor", "TEXT"),
    ]:
        if col_name not in existing_cols:
            try:
                cur.execute(f"ALTER TABLE candidate_cache ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError:
                pass
    conn.commit()


def run_audit_cycle(batch_limit: int = 250, min_roi_pct: float = 15.0) -> Dict[str, int]:
    if not os.path.exists(DB_PATH):
        logger.warning(f"Database not found at {DB_PATH}. Waiting for hunter_engine.db initialization.")
        return {}

    gatekeeper = AmazonGatekeeper()
    blocked_brands = load_blocklist()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    ensure_tracking_columns(conn)
    cur = conn.cursor()

    cur.execute("""
        SELECT asin, title, brand, sales_rank, avg90_bb, current_bb, monthly_sold,
               drops_90, fba_fee, referral_pct, weight_lbs, num_sellers, is_hazmat,
               wholesale_cost, matched_vendor, gating_status
        FROM candidate_cache
        WHERE asin IS NOT NULL
          AND COALESCE(status, '') NOT IN ('BLOCKED_IP_BRAND', 'REJECTED', 'HARD_BLOCKED')
          AND COALESCE(gating_status, '') NOT IN ('UNRESTRICTED', 'AUTO_UNGATED', 'HARD_BLOCKED')
        ORDER BY COALESCE(last_gating_check, '1970-01-01') ASC
        LIMIT ?
    """, (batch_limit,))
    rows = cur.fetchall()

    logger.info(f"Auditing {len(rows)} candidate_cache ASINs against SP-API Listings Restrictions...")
    stats = {
        "UNRESTRICTED": 0,
        "AUTO_UNGATED": 0,
        "INVOICE_REQUIRED": 0,
        "Q4_HOLIDAY_RESTRICTED": 0,
        "CPC_SAFETY_REQUIRED": 0,
        "HARD_BLOCKED": 0,
        "PROMOTED_READY_TO_BUY": 0,
    }

    for row in rows:
        asin = row["asin"]
        brand = (row["brand"] or "").strip()
        if brand.upper() in blocked_brands:
            cur.execute("UPDATE candidate_cache SET status = 'BLOCKED_IP_BRAND' WHERE asin = ?", (asin,))
            continue

        res = gatekeeper.evaluate_asin_approval(asin)
        g_status = res.get("status", "INVOICE_REQUIRED")
        if g_status in ("MOCK_OFFLINE", "RATE_LIMITED", "ERROR"):
            if g_status == "MOCK_OFFLINE":
                logger.error("SP-API credentials offline; aborting cycle early.")
                break
            continue

        stats[g_status] = stats.get(g_status, 0) + 1
        reasons_txt = " | ".join(res.get("reasons", []))[:500]

        cur.execute("""
            UPDATE candidate_cache
            SET gating_status = ?,
                gating_reasons = ?,
                last_gating_check = datetime('now')
            WHERE asin = ?
        """, (g_status, reasons_txt, asin))

        # Promote UNRESTRICTED or AUTO_UNGATED items with Net ROI >= 15% into verified_deals
        if g_status in ("UNRESTRICTED", "AUTO_UNGATED"):
            bb = float(row["avg90_bb"] or row["current_bb"] or 0.0)
            ws_cost = float(row["wholesale_cost"] or 0.0)
            vendor = row["matched_vendor"] or "Wholesale"
            if bb > 0 and ws_cost > 0:
                ref_pct = float(row["referral_pct"] or 0.15)
                fba_fee = float(row["fba_fee"] or 4.50)
                logistics = get_logistics_profile(vendor)
                prep = logistics["prep_base"] + (float(row["weight_lbs"] or 1.0) * logistics["freight_per_lb"])
                net_profit = round(bb - ws_cost - (bb * ref_pct) - fba_fee - prep, 2)
                roi_pct = round((net_profit / ws_cost) * 100.0, 1)

                if net_profit > 0 and roi_pct >= min_roi_pct:
                    cur.execute("""
                        INSERT OR REPLACE INTO verified_deals (
                            asin, title, brand, vendor, buy_box_price, wholesale_cost,
                            net_profit, roi_percent, sales_rank, status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'READY_TO_BUY')
                    """, (
                        asin, row["title"], brand, vendor, bb, ws_cost,
                        net_profit, roi_pct, int(row["sales_rank"] or 45000)
                    ))
                    stats["PROMOTED_READY_TO_BUY"] += 1

        time.sleep(0.25)

    conn.commit()
    conn.close()

    logger.info(f"Audit pass complete: {stats}")
    if stats["PROMOTED_READY_TO_BUY"] > 0:
        try:
            from tools.sync_today_leads_to_sheet import run_pipeline_sync
            run_pipeline_sync()
        except Exception as exc:
            logger.warning(f"Sheet sync trigger notice: {exc}")

    return stats


def main():
    parser = argparse.ArgumentParser(description="Automated 6-Hour SP-API Listings Restrictions Daemon")
    parser.add_argument("--once", action="store_true", help="Run a single audit cycle and exit")
    parser.add_argument("--batch-limit", type=int, default=250, help="ASINs per 6-hour audit pass")
    parser.add_argument("--min-roi", type=float, default=15.0, help="Minimum Net ROI % for Ready-to-Buy promotion")
    args = parser.parse_args()

    if args.once:
        run_audit_cycle(batch_limit=args.batch_limit, min_roi_pct=args.min_roi)
        return

    logger.info(f"Starting continuous SP-API Listings Restrictions Daemon (interval={SIX_HOURS_SECONDS}s)...")
    while True:
        try:
            run_audit_cycle(batch_limit=args.batch_limit, min_roi_pct=args.min_roi)
        except Exception as exc:
            logger.error(f"SP-API daemon loop error: {exc}")
        time.sleep(SIX_HOURS_SECONDS)


if __name__ == "__main__":
    main()
