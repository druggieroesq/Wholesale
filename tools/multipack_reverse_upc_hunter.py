#!/usr/bin/env python3
"""
tools/multipack_reverse_upc_hunter.py (v1.0)
--------------------------------------------
Reverse-queries Keepa for:
  1. Direct UPC batch lookup (100 UPCs/call) across Atlantic Spice (345 SKUs),
     Frontier Co-op (24,327 SKUs), and any Marlo Beauty / EE Distribution catalogs.
  2. Targeted Multipack Loophole discovery ('Pack of 2', '3-Pack', '4-Pack', '6-Pack')
     across Marlo Beauty salon brands, Frontier Co-op / Atlantic Spice lines, and
     EE Distribution wholesale toy/collectible lines.
  3. Saves all qualifying deals (>= $2.25/unit on Multipacks, >= $3.00/unit on Singles)
     directly into verified_deals and candidate_cache in data/hunter_engine.db.
"""

import os
import re
import sys
import time
import json
import logging
import sqlite3
import argparse
import unicodedata
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] MultipackUPCHunter: %(message)s"
)
logger = logging.getLogger("MultipackUPCHunter")

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
if not DATA_DIR.exists():
    DATA_DIR = ROOT_DIR / "data"

DB_PATH = DATA_DIR / "hunter_engine.db"
CATALOG_DIR = DATA_DIR / "catalogs"
IP_FILE = DATA_DIR / "ip_blocklist.json"

KEEPA_KEY = os.getenv("KEEPA_API_KEY", "").strip()

PACK_REGEX = re.compile(
    r"(?:(\d+)\s*[- ]*(?:pack|pk|count|ct|pcs|pieces|set|bundle)|pack\s*of\s*(\d+)|set\s*of\s*(\d+))",
    re.IGNORECASE,
)

# Verified wholesale-friendly brands stocked at Marlo Beauty, Frontier Co-op, Atlantic Spice & EE Distribution
SPECIALTY_DISTRIBUTOR_BRANDS: Dict[str, List[str]] = {
    "Marlo Beauty Supply": [
        "BARBICIDE", "CLUBMAN", "CLUBMAN PINAUD", "MARIANNA", "DIANE", "FRAMAR",
        "BETTY DAIN", "GODEFROY", "ARDELL", "IBD", "GELISH", "ORLY", "CHINA GLAZE",
        "SECHE", "CUCCIO", "BIOTONE", "BON VITAL", "GIGI", "SATIN SMOOTH",
        "CLEAN + EASY", "HESS", "ANDIS", "OSTER"
    ],
    "Frontier Co-op": [
        "FRONTIER CO-OP", "SIMPLY ORGANIC", "AURA CACIA", "HERITAGE STORE",
        "ALAFFIA", "ACURE", "ANDALOU NATURALS", "BADGER", "DESERT ESSENCE",
        "DR. BRONNER'S", "EVERYDAY SHEA", "GIOVANNI", "JASON", "MINERAL FUSION",
        "PACIFICA", "THAYERS", "WELEDA"
    ],
    "Atlantic Spice": [
        "ATLANTIC SPICE", "SPICE ISLANDS", "BADIA", "TONY CHACHERE'S",
        "OLD BAY", "LAWRY'S", "MORTON & BASSETT"
    ],
    "EE Distribution": [
        "FUNKO", "HASBRO", "MATTEL", "SPIN MASTER", "BANDAI", "JAZWARES",
        "NECA", "MCFARLANE TOYS", "DIAMOND SELECT", "SUPER7", "LOUNGEFLY"
    ],
}


def norm_brand(b: Any) -> str:
    s = unicodedata.normalize("NFKD", str(b or "")).encode("ascii", "ignore").decode("ascii")
    return s.upper().strip()


def load_blocked_set() -> Set[str]:
    blocked: Set[str] = set()
    for p in (IP_FILE, ROOT_DIR / "data" / "ip_blocklist.json"):
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                items = raw if isinstance(raw, list) else raw.get("brands", [])
                blocked |= {norm_brand(b) for b in items if b}
            except Exception:
                pass
    return blocked


def norm_upc(val: Any) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    s = str(val).strip()
    if "e+" in s.lower():
        try:
            s = f"{int(float(s))}"
        except Exception:
            pass
    d = re.sub(r"\D", "", s)
    if len(d) == 11:
        d = "0" + d
    return d if len(d) in (12, 13, 14) else ""


def extract_pack_qty(title: str) -> int:
    if not title:
        return 1
    m = PACK_REGEX.search(title)
    if m:
        pk = int(m.group(1) or m.group(2) or m.group(3) or 1)
        if 2 <= pk <= 24:
            return pk
    return 1


def detect_vendor(filepath: Path) -> str:
    fn = str(filepath).lower()
    if "atlantic" in fn:
        return "Atlantic Spice"
    if "frontier" in fn:
        return "Frontier Co-op"
    if "marlo" in fn:
        return "Marlo Beauty Supply"
    if "ee_dist" in fn or "eedist" in fn or "entertainment" in fn:
        return "EE Distribution"
    if "bci" in fn:
        return "BCI"
    if "motion" in fn:
        return "Motion Industries"
    if "zoro" in fn:
        return "Zoro"
    if "jb_tool" in fn or "jbtool" in fn:
        return "JB Tools"
    if "contour" in fn:
        return "Contour Abrasives"
    return "Distributor Catalog"


def load_target_catalog_upcs(max_frontier: int = 2000) -> Dict[str, Dict[str, Any]]:
    upc_map: Dict[str, Dict[str, Any]] = {}
    if not CATALOG_DIR.exists():
        return upc_map

    files = list(CATALOG_DIR.rglob("*.csv")) + list(CATALOG_DIR.rglob("*.xlsx"))
    # Prioritize Atlantic Spice, Marlo, EE Distribution, and Frontier Co-op first
    files.sort(key=lambda f: (
        0 if "atlantic" in str(f).lower() else
        1 if "marlo" in str(f).lower() else
        2 if "ee" in str(f).lower() else
        3 if "frontier" in str(f).lower() else 4
    ))

    frontier_count = 0
    for f in files:
        vendor = detect_vendor(f)
        if vendor not in ("Atlantic Spice", "Frontier Co-op", "Marlo Beauty Supply", "EE Distribution"):
            continue
        try:
            df = pd.read_csv(f, dtype=str) if f.suffix.lower() == ".csv" else pd.read_excel(f, dtype=str)
            if df.empty:
                continue
            df.columns = [str(c).strip().lower() for c in df.columns]
            upc_c = next((c for c in df.columns if any(k in c for k in ("upc", "ean", "gtin"))), None)
            cost_c = next((c for c in df.columns if any(k in c for k in ("wholesale", "unit_cost", "cost", "price"))), None)
            title_c = next((c for c in df.columns if any(k in c for k in ("desc", "name", "title", "item"))), None)
            if not upc_c or not cost_c:
                continue

            for _, r in df.iterrows():
                if vendor == "Frontier Co-op" and frontier_count >= max_frontier:
                    break
                u = norm_upc(r.get(upc_c))
                if not u or u in upc_map:
                    continue
                try:
                    c_val = float(str(r.get(cost_c, "")).replace("$", "").replace(",", "").strip())
                except Exception:
                    continue
                if c_val <= 0.25:
                    continue
                upc_map[u] = {
                    "vendor": vendor,
                    "cost": c_val,
                    "title": str(r.get(title_c) or "").strip(),
                }
                if vendor == "Frontier Co-op":
                    frontier_count += 1
        except Exception as exc:
            logger.warning(f"Skipping {f.name}: {exc}")

    return upc_map


def parse_keepa_product(
    p: Dict[str, Any],
    vendor: str,
    unit_cost: float,
    blocked_set: Set[str],
) -> Optional[Dict[str, Any]]:
    asin = str(p.get("asin") or "").strip().upper()
    brand = str(p.get("brand") or "").strip()
    title = str(p.get("title") or "").strip()
    if not asin or len(asin) != 10 or not brand or norm_brand(brand) in blocked_set:
        return None

    stats = p.get("stats") or {}
    curr = stats.get("current") or []
    bb_cents = stats.get("buyBoxPrice") or -1
    if bb_cents <= 0 and len(curr) > 18 and curr[18] and curr[18] > 0:
        bb_cents = curr[18]
    if bb_cents <= 0 and len(curr) > 1 and curr[1] and curr[1] > 0:
        bb_cents = curr[1]
    if bb_cents < 1350:
        return None

    bb = round(bb_cents / 100.0, 2)
    avg90_list = stats.get("avg90") or []
    avg90_cents = avg90_list[18] if len(avg90_list) > 18 and avg90_list[18] and avg90_list[18] > 0 else bb_cents
    avg90_bb = round(avg90_cents / 100.0, 2)
    effective_bb = min(bb, avg90_bb) if avg90_bb > 0 else bb
    if effective_bb < 13.50:
        return None

    # Seller counts: stats.offerCountFBA / offerCountFBM
    fba_count = int(stats.get("offerCountFBA") or 0)
    fbm_count = int(stats.get("offerCountFBM") or 0)
    total_sellers = max(fba_count + fbm_count, int(curr[11] if len(curr) > 11 and curr[11] and curr[11] > 0 else 0))

    pack_qty = extract_pack_qty(title)
    is_multipack = pack_qty >= 2

    # FBA window: 0 FBA (>= 2 FBM on multipacks, >= 3 FBM on singles) OR 2-4 FBA sellers
    if fba_count == 1 or fba_count > 4:
        return None
    if fba_count == 0 and total_sellers < (2 if is_multipack else 3):
        return None

    ms = int(p.get("monthlySold") or 0)
    d90 = int(stats.get("salesRankDrops90") or 0)
    vel = ms if ms > 0 else (max(1, round((d90 / 3.0) * 1.15)) if d90 > 0 else 0)
    if vel < 25:
        return None

    bundle_prep = 1.25 if is_multipack else 0.75
    landed_cogs = round((unit_cost * pack_qty) + bundle_prep, 2)
    if landed_cogs >= effective_bb:
        return None

    ref_pct = (float(p.get("referralFeePercent") or 1500) / 10000.0) if p.get("referralFeePercent") else 0.15
    if ref_pct <= 0 or ref_pct > 0.25:
        ref_pct = 0.15

    fba_fees_obj = p.get("fbaFees") or {}
    fba_fee = round(float(fba_fees_obj.get("pickAndPackFee") or 0) / 100.0, 2)
    if fba_fee <= 1.0:
        fba_fee = round(max(3.50, effective_bb * 0.18), 2)

    net_profit = round(effective_bb - (effective_bb * ref_pct) - fba_fee - landed_cogs, 2)
    roi_pct = round((net_profit / landed_cogs) * 100.0, 1)

    min_net = 2.25 if is_multipack else 3.00
    min_pool = 180.0 if is_multipack else 250.0
    if net_profit < min_net or roi_pct < 20.0:
        return None

    eff_comp = fba_count if fba_count > 0 else max(1, round(total_sellers * 0.45))
    our_share = max(1, round(vel / (eff_comp + 1)))
    monthly_pool = round(our_share * net_profit, 2)
    if monthly_pool < min_pool:
        return None

    bsr = int(curr[3] if len(curr) > 3 and curr[3] and curr[3] > 0 else 0)
    upc_list = [norm_upc(u) for u in (p.get("upcList") or []) if norm_upc(u)]

    return {
        "asin": asin,
        "title": title,
        "brand": brand,
        "vendor": vendor,
        "pack_qty": pack_qty,
        "is_multipack": is_multipack,
        "buy_box_price": effective_bb,
        "wholesale_cost": landed_cogs,
        "net_profit": net_profit,
        "roi_percent": roi_pct,
        "monthly_velocity": vel,
        "drops_90": d90,
        "sales_rank": bsr,
        "fba_sellers": fba_count,
        "num_sellers": total_sellers,
        "our_share": our_share,
        "monthly_profit_pool": monthly_pool,
        "upc": upc_list[0] if upc_list else "",
        "referral_pct": ref_pct,
        "fba_fee": fba_fee,
    }


def run_multipack_reverse_hunt(max_frontier: int = 2000):
    if not KEEPA_KEY:
        logger.error("KEEPA_API_KEY not found in environment.")
        sys.exit(1)

    blocked_set = load_blocked_set()
    upc_map = load_target_catalog_upcs(max_frontier=max_frontier)
    all_upcs = list(upc_map.keys())
    logger.info(f"Loaded {len(all_upcs):,} specialty catalog UPCs (Atlantic Spice + Frontier Co-op + Marlo + EE).")

    saved_hits = 0
    with closing(sqlite3.connect(DB_PATH, timeout=60.0)) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        cur = conn.cursor()

        # 1. Batch UPC Lookup (100 UPCs per Keepa API call)
        for i in range(0, len(all_upcs), 100):
            batch = all_upcs[i:i + 100]
            try:
                resp = requests.get(
                    "https://api.keepa.com/product",
                    params={"key": KEEPA_KEY, "domain": 1, "code": ",".join(batch), "stats": 90, "buybox": 1},
                    timeout=45,
                )
                if resp.status_code != 200:
                    logger.warning(f"UPC batch {i // 100 + 1} HTTP {resp.status_code}")
                    time.sleep(4)
                    continue
                payload = resp.json()
                tokens_left = payload.get("tokensLeft", "?")
                products = payload.get("products") or []

                for p in products:
                    p_upcs = [norm_upc(u) for u in (p.get("upcList") or []) if norm_upc(u)]
                    matched_u = next((u for u in p_upcs if u in upc_map), None)
                    if not matched_u:
                        continue
                    cat = upc_map[matched_u]
                    deal = parse_keepa_product(p, cat["vendor"], cat["cost"], blocked_set)
                    if not deal:
                        continue

                    tier = "MULTIPACK_LOOPHOLE" if deal["is_multipack"] else "CATALOG_UPC_MATCH"
                    cur.execute("""
                        INSERT OR REPLACE INTO verified_deals
                        (asin, title, brand, confidence_tier, sales_rank, buy_box_price, wholesale_cost,
                         net_profit, roi_percent, monthly_profit_pool, vendor, status, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'APPROVED', datetime('now'))
                    """, (
                        deal["asin"], deal["title"], deal["brand"], tier, deal["sales_rank"],
                        deal["buy_box_price"], deal["wholesale_cost"], deal["net_profit"],
                        deal["roi_percent"], deal["monthly_profit_pool"], deal["vendor"]
                    ))
                    cur.execute("""
                        INSERT OR REPLACE INTO candidate_cache
                        (asin, title, brand, upc, sales_rank, drops_90, monthly_sold, avg90_bb, current_bb,
                         referral_pct, fba_fee, status, num_sellers, amazon_pack_qty, wholesale_cost, matched_vendor, last_evaluated)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'APPROVED', ?, ?, ?, ?, datetime('now'))
                    """, (
                        deal["asin"], deal["title"], deal["brand"], deal["upc"], deal["sales_rank"],
                        deal["drops_90"], deal["monthly_velocity"], deal["buy_box_price"], deal["buy_box_price"],
                        deal["referral_pct"], deal["fba_fee"], deal["num_sellers"], deal["pack_qty"],
                        deal["wholesale_cost"], deal["vendor"]
                    ))
                    saved_hits += 1
                    logger.info(
                        f"[HIT #{saved_hits}] {deal['vendor']} | {deal['asin']} ({deal['pack_qty']}-Pk) | "
                        f"{deal['brand']} | BB ${deal['buy_box_price']:.2f} | COGS ${deal['wholesale_cost']:.2f} | "
                        f"Net ${deal['net_profit']:.2f} ({deal['roi_percent']:.0f}% ROI) | {deal['monthly_velocity']} u/mo"
                    )

                conn.commit()
                logger.info(f"UPC Batch {i // 100 + 1}/{(len(all_upcs) + 99) // 100} complete | tokensLeft={tokens_left}")
                time.sleep(1.5)
            except Exception as exc:
                logger.warning(f"Batch error: {exc}")
                time.sleep(3)

        # 2. Targeted Multipack Brand Search for Marlo Beauty, Frontier Co-op, Atlantic Spice & EE Distribution
        for vendor, brands in SPECIALTY_DISTRIBUTOR_BRANDS.items():
            for br in brands:
                if norm_brand(br) in blocked_set:
                    continue
                try:
                    query_json = json.dumps({
                        "brand": [br],
                        "title": "pack",
                        "current_BUY_BOX_SHIPPING_gte": 1350,
                        "monthlySold_gte": 25,
                        "offerCountFBA_lte": 4,
                        "perPage": 50,
                        "page": 0,
                    })
                    q_resp = requests.get(
                        "https://api.keepa.com/query",
                        params={"key": KEEPA_KEY, "domain": 1, "selection": query_json},
                        timeout=30,
                    )
                    if q_resp.status_code != 200:
                        continue
                    asin_list = (q_resp.json().get("asinList") or [])[:40]
                    if not asin_list:
                        continue

                    p_resp = requests.get(
                        "https://api.keepa.com/product",
                        params={"key": KEEPA_KEY, "domain": 1, "asin": ",".join(asin_list), "stats": 90, "buybox": 1},
                        timeout=45,
                    )
                    if p_resp.status_code != 200:
                        continue
                    for p in (p_resp.json().get("products") or []):
                        title = str(p.get("title") or "").strip()
                        pk = extract_pack_qty(title)
                        if pk < 2:
                            continue
                        stats = p.get("stats") or {}
                        bb_c = stats.get("buyBoxPrice") or -1
                        if bb_c < 1350:
                            continue
                        bb = round(bb_c / 100.0, 2)
                        # Modeled wholesale bundle COGS (~34% of Buy Box for beauty/spice/collectible multipacks)
                        est_unit_cost = round((bb * 0.34) / pk, 2)
                        deal = parse_keepa_product(p, vendor, est_unit_cost, blocked_set)
                        if not deal:
                            continue

                        cur.execute("""
                            INSERT OR IGNORE INTO verified_deals
                            (asin, title, brand, confidence_tier, sales_rank, buy_box_price, wholesale_cost,
                             net_profit, roi_percent, monthly_profit_pool, vendor, status, updated_at)
                            VALUES (?, ?, ?, 'MULTIPACK_LOOPHOLE_BRAND', ?, ?, ?, ?, ?, ?, ?, 'APPROVED', datetime('now'))
                        """, (
                            deal["asin"], deal["title"], deal["brand"], deal["sales_rank"],
                            deal["buy_box_price"], deal["wholesale_cost"], deal["net_profit"],
                            deal["roi_percent"], deal["monthly_profit_pool"], vendor
                        ))
                        saved_hits += 1
                        logger.info(
                            f"[MP-BRAND #{saved_hits}] {vendor} | {deal['asin']} ({pk}-Pk) | {deal['brand']} | "
                            f"BB ${deal['buy_box_price']:.2f} | Net ${deal['net_profit']:.2f} | {deal['monthly_velocity']} u/mo"
                        )
                    conn.commit()
                    time.sleep(1.2)
                except Exception:
                    continue

    logger.info(f"Multipack & Reverse UPC Hunt Complete! Total new specialty leads added: {saved_hits:,}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multipack Loophole & Specialty Catalog Reverse UPC Hunter")
    parser.add_argument("--max-frontier", type=int, default=2000, help="Max Frontier Co-op UPCs to query")
    args = parser.parse_args()
    run_multipack_reverse_hunt(max_frontier=args.max_frontier)
