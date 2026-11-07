#!/usr/bin/env python3
"""
tools/sync_today_leads_to_sheet.py
----------------------------------
Continuous Google Sheet Sync Daemon (v16.7 Continuous Velocity Master).
"""

import os
import sys
import time
import json
import sqlite3
import logging
from pathlib import Path
from typing import Dict, Any, Set
import requests
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

load_dotenv(ROOT_DIR / ".env", override=True)

from config import (
    calculate_sliding_profit_floor,
    get_logistics_profile,
    validate_fba_competition,
    estimate_days_to_fba,
    get_dynamic_profit_hurdle,
    calculate_continuous_velocity,
    get_verified_webhook_url,
    MAX_DAYS_TO_FBA,
    MAX_COMPETITIVE_FBA
)
from tools.reverse_amazon_finder import parse_pack_quantity, clean_brand_name

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] SheetSyncDaemon: %(message)s"
)
logger = logging.getLogger("SheetSyncDaemon")

DB_PATH = ROOT_DIR / "data" / "hunter_engine.db"
PRIMARY_IP_FILE = ROOT_DIR / "data" / "ip_blocklist.json"
BUY_LIST_WEBHOOK_URL = get_verified_webhook_url()


def load_blocked_brands() -> Set[str]:
    if not PRIMARY_IP_FILE.exists():
        return set()
    try:
        raw = json.loads(PRIMARY_IP_FILE.read_text(encoding="utf-8"))
        brand_list = raw if isinstance(raw, list) else raw.get("brands", [])
        return {clean_brand_name(b) for b in brand_list if b}
    except Exception as e:
        logger.error(f"Error loading IP blocklist: {e}")
        return set()


def recalibrate_sqlite_cache(conn: sqlite3.Connection):
    """Recomputes cached monthly_sold to remove the 1200 plateau."""
    cur = conn.cursor()
    cur.execute("SELECT asin, sales_rank, drops_90, monthly_sold FROM candidate_cache")
    rows = cur.fetchall()
    if not rows:
        return

    updated = 0
    for r in rows:
        rank = r["sales_rank"] or 0
        d90 = r["drops_90"] or 0
        current_sold = r["monthly_sold"] or 0

        if current_sold == 1200 or current_sold < 30:
            d30 = int(d90 / 3.0) if d90 else 0
            new_val = calculate_continuous_velocity(rank, drops_30=d30, drops_90=d90)
            cur.execute("UPDATE candidate_cache SET monthly_sold = ? WHERE asin = ?", (new_val, r["asin"]))
            updated += 1

    conn.commit()
    logger.info(f"Recalibrated {updated} candidates with continuous velocity.")


def push_lead_payload(lead: Dict[str, Any]) -> bool:
    if not BUY_LIST_WEBHOOK_URL:
        return False
    try:
        r = requests.post(BUY_LIST_WEBHOOK_URL, json=lead, timeout=20, allow_redirects=True)
        return r.status_code in (200, 201)
    except Exception as e:
        logger.error(f"Error posting ASIN {lead.get('asin')}: {e}")
        return False


def run_full_sync_pass() -> int:
    if not DB_PATH.exists():
        logger.warning(f"Database not found at {DB_PATH}")
        return 0

    blocked_brands = load_blocked_brands()
    conn = sqlite3.connect(str(DB_PATH), timeout=60.0)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    recalibrate_sqlite_cache(conn)

    cur.execute("CREATE TABLE IF NOT EXISTS sheet_synced_leads (asin TEXT PRIMARY KEY, synced_at INTEGER)")
    cur.execute("SELECT asin FROM sheet_synced_leads")
    already_synced: Set[str] = {r["asin"] for r in cur.fetchall()}

    cur.execute("""
        SELECT asin, title, brand, sales_rank, avg90_bb, current_bb, monthly_sold, 
               drops_90, amazon_pack_qty, referral_pct, fba_fee, weight_lbs, num_sellers, is_hazmat
        FROM candidate_cache 
        WHERE COALESCE(num_sellers, 0) <= 5
        ORDER BY COALESCE(monthly_sold, 0) DESC
    """)
    candidates = cur.fetchall()

    newly_pushed = 0
    now_ts = int(time.time())

    for r in candidates:
        asin = r["asin"]
        if asin in already_synced:
            continue

        raw_brand = str(r["brand"] or "").strip()
        clean_b = clean_brand_name(raw_brand)
        if not clean_b or clean_b in blocked_brands:
            continue

        bb = float(r["avg90_bb"] or r["current_bb"] or 0.0)
        rank_val = int(r["sales_rank"] or 0)
        sold_val = int(r["monthly_sold"] or 0)
        drops_90_val = int(r["drops_90"] or 0)
        d30_val = int(drops_90_val / 3.0) if drops_90_val else 0

        sales = calculate_continuous_velocity(
            sales_rank=rank_val,
            drops_30=d30_val,
            drops_90=drops_90_val,
            monthly_sold=sold_val if (sold_val and sold_val != 1200) else 0
        )

        if bb < 13.50 or sales < 30:
            continue

        comp_sellers = max(0, int(r["num_sellers"] or 0))
        if comp_sellers > MAX_COMPETITIVE_FBA:
            continue

        is_valid_comp, comp_reason, our_share = validate_fba_competition(sales, comp_sellers)
        if not is_valid_comp:
            continue

        title_str = str(r["title"] or "")
        pack_qty = parse_pack_quantity(title_str)
        is_hazmat = bool(r["is_hazmat"])

        days_to_fba = estimate_days_to_fba(raw_brand, is_hazmat=is_hazmat, pack_qty=pack_qty)
        if days_to_fba > MAX_DAYS_TO_FBA or days_to_fba <= 0:
            continue

        logistics = get_logistics_profile(raw_brand)
        weight = float(r["weight_lbs"] or 1.0)

        multipack_prep_buffer = 0.45 if pack_qty > 1 else 0.00
        prep_base = logistics["prep_base"] + (0.60 if is_hazmat else 0.0) + multipack_prep_buffer
        inbound_freight = round(weight * (logistics["freight_per_lb"] + (0.20 if is_hazmat else 0.0)), 2)
        total_prep = round(prep_base + inbound_freight + 0.27, 2)

        referral_fee = round(bb * float(r["referral_pct"] or 0.12), 2)
        fba_fee = float(r["fba_fee"] or (3.86 + (weight * 0.45)))
        total_overhead = referral_fee + fba_fee + total_prep + round(bb * 0.03, 2)
        payout = round(bb - total_overhead, 2)

        profit_floor, req_roi = calculate_sliding_profit_floor(our_share)
        max_allowable_cost = round(payout - profit_floor, 2)
        if max_allowable_cost < 1.00:
            continue

        cur.execute("SELECT wholesale_cost, vendor FROM verified_deals WHERE asin = ?", (asin,))
        verified = cur.fetchone()
        if verified and verified["wholesale_cost"] and verified["wholesale_cost"] > 0:
            wholesale_cost = float(verified["wholesale_cost"])
            vendor = verified["vendor"] or raw_brand
        else:
            wholesale_cost = min(round(bb * 0.40, 2), max_allowable_cost)
            vendor = f"Target Brand: {raw_brand}"

        if wholesale_cost < 1.00 or wholesale_cost > 65.00:
            continue

        net_profit = round(payout - wholesale_cost, 2)
        landed_cost = wholesale_cost + total_prep
        roi_percent = round((net_profit / landed_cost) * 100.0, 1) if landed_cost > 0 else 0.0

        if net_profit < profit_floor or roi_percent < req_roi:
            continue

        min_pool_required = get_dynamic_profit_hurdle(sales)
        monthly_profit_pool = round(our_share * net_profit, 2)
        if monthly_profit_pool < min_pool_required:
            continue

        vacant_note = " [0 FBA SELLERS - 100% BUY BOX]" if comp_sellers == 0 else ""

        payload = {
            "action": "ADD_BUY_LEAD",
            "asin": asin,
            "title": title_str[:80],
            "brand": raw_brand,
            "vendor": vendor,
            "prep_center": logistics["facility"],
            "sales_rank": rank_val,
            "monthly_velocity": sales,
            "competitive_fba": comp_sellers,
            "our_share_velocity": int(our_share),
            "pack_count": pack_qty,
            "days_to_fba": days_to_fba,
            "buy_box_price": bb,
            "wholesale_cost": wholesale_cost,
            "net_profit": net_profit,
            "roi_percent": roi_percent,
            "suggested_order_size": max(12, int(our_share * 0.5)),
            "monthly_profit_pool": monthly_profit_pool,
            "is_hazmat": "YES" if is_hazmat else "NO",
            "order_url": f"https://www.amazon.com/dp/{asin}",
            "seller_check_url": f"https://sellercentral.amazon.com/product-search/search?q={asin}",
            "seller_approval_url": f"https://sellercentral.amazon.com/hz/approvalrequest/restrictions/approve?asin={asin}",
            "notes": f"Pool: ${monthly_profit_pool:.2f}/mo | FBA: {comp_sellers} | Share: {int(our_share)}u/mo | Velocity: {sales}u/mo | Days: {days_to_fba}d{vacant_note}",
            "status": "Ready for Order"
        }
        if push_lead_payload(payload):
            already_synced.add(asin)
            cur.execute("INSERT OR REPLACE INTO sheet_synced_leads (asin, synced_at) VALUES (?, ?)", (asin, now_ts))
            newly_pushed += 1
            print(f"  -> Synced: {asin} | {raw_brand[:16]} | True Velocity: {sales} u/mo | Our Share: {int(our_share)} u/mo | Net: ${net_profit:.2f}/u | Pool: ${monthly_profit_pool:.2f}/mo")
            time.sleep(0.08)

    conn.commit()
    conn.close()
    return newly_pushed


if __name__ == "__main__":
    webhook_url = get_verified_webhook_url()
    if webhook_url:
        try:
            print("Wiping previous 1200-plateau rows from sheet...")
            r_wipe = requests.post(webhook_url, json={"action": "WIPE_AND_RESET"}, timeout=20, allow_redirects=True)
            print(f"Sheet Reset: HTTP {r_wipe.status_code}")
        except Exception as e:
            print(f"Sheet wipe notice: {e}")

    c = sqlite3.connect(str(DB_PATH), timeout=60.0)
    c.execute("DELETE FROM sheet_synced_leads")
    c.commit()
    c.close()

    print("Running realistic continuous velocity sync pass...")
    synced = run_full_sync_pass()
    print(f"Sync complete. Pushed {synced} leads with natural, diversified velocity.")
