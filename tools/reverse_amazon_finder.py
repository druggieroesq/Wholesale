#!/usr/bin/env python3
"""
tools/reverse_amazon_finder.py (v20.12)
---------------------------------------
Reverse Amazon Hunter & Autonomous Sourcing Daemon:
  1. 13 Low-Return Commercial & Consumable Root Categories (< 4.5% FBA Return Rate):
       - Beauty & Personal Care (3760911 — Marlo Beauty / Salon Multipacks)
       - Grocery & Gourmet Food (16310101 — Frontier Co-op / Atlantic Spice Multipacks)
       - Toys & Games (165793011 — EE Distribution / Collectibles)
       - Health & Household (3760901 — Frontier Co-op / Personal Care)
       - Office Products (1064954 — B2B Consumables & Multipacks)
       - Pet Supplies (2619533011 — Grooming, Filters, Treats & Multipacks)
       - Arts, Crafts & Sewing (2617941011 — Adhesives, Blades, Thread & Craft Packs)
       - Patio, Lawn & Garden (2972638011 — Irrigation, Pool & Lawn Consumables)
       - Musical Instruments (11091801 — Strings, Reeds, Drumsticks & Cables Multipacks)
       - Sports & Outdoors (3375251 — Fishing, Golf, Grip Tape & Outdoor Consumables)
       - Industrial & Scientific (16310091 — Zoro / Motion / BCI / Contour)
       - Tools & Home Improvement (228013 — JB Tools / Zoro / BCI)
       - Automotive & Commercial Fleet (15684181 — Motion / Zoro)
  2. Multipack Loophole + Two-Tier Profit Gates:
       - Verified Multipack Loophole (Pack >= 2): >= $2.25/unit net profit, >= $180/mo pool, 0 (>=2 FBM) or 2-4 FBA
       - Verified Single-Unit Catalog Match (Pack == 1): >= $3.00/unit net profit, >= $250/mo pool, 0 (>=3 FBM) or 2-4 FBA
       - Caches qualifying unmapped Keepa discoveries into candidate_cache so push_full_backlog_to_sheet.py
         can evaluate them against >= $5.00/unit & >= $500/mo pool gates.
  3. Recursive Catalog Indexer across all subfolders + 784-brand Unicode-normalized IP shield.
"""

import os
import sys
import time
import json
import logging
import re
import fcntl
import sqlite3
import html
import unicodedata
from contextlib import closing
from pathlib import Path
from typing import Dict, List, Optional, Any, Set, Tuple
import requests
import pandas as pd
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

load_dotenv(ROOT_DIR / ".env", override=True)

from config import (
    get_logistics_profile,
    calculate_continuous_velocity,
    get_verified_webhook_url,
    MAX_DAYS_TO_FBA,
    estimate_days_to_fba,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] HunterEngine: %(message)s"
)
logger = logging.getLogger("HunterEngine")

LOCK_FILE_PATH = "/tmp/reverse_amazon_finder.lock"

KEEPA_API_KEY = os.getenv("KEEPA_API_KEY", "").strip()
BUY_LIST_WEBHOOK_URL = get_verified_webhook_url()

_raw_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip().strip('"').strip("'")
TELEGRAM_BOT_TOKEN = _raw_token[3:] if _raw_token.lower().startswith("bot") else _raw_token
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip().strip('"').strip("'")

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
if not DATA_DIR.exists():
    DATA_DIR = ROOT_DIR / "data"

CATALOG_BASE_DIR = DATA_DIR / "catalogs"
DB_PATH = DATA_DIR / "hunter_engine.db"
PRIMARY_IP_FILE = DATA_DIR / "ip_blocklist.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)
CATALOG_BASE_DIR.mkdir(parents=True, exist_ok=True)

STRICT_MAX_WEIGHT_LBS = 20.0
STRICT_MIN_WHOLESALE_COST = 0.50
STRICT_MAX_WHOLESALE_COST = 85.00
MIN_REVIEW_RATING = 3.8
MAX_SEARCH_PAGE_DEPTH = 100

ACCESSORY_TOKENS = {
    'bracket', 'screw', 'screws', 'bolt', 'bolts', 'gasket', 'seal',
    'plate', 'cover', 'adapter', 'harness', 'hardware kit', 'mounting kit',
    'faceplate', 'strike', 'shim', 'clip', 'spring', 'spacer'
}

PRIMARY_EQUIPMENT_TOKENS = {
    'closer', 'motor', 'pump', 'actuator', 'compressor', 'tool', 'drill',
    'saw', 'sensor', 'detector', 'lockset', 'exit device', 'valve'
}

# Block high-return apparel/footwear/decor items that occasionally cross-list into Sports/Home
HIGH_RETURN_TITLE_RE = re.compile(
    r"\b(shirt|t-shirt|hoodie|jacket|pants|shorts|leggings|socks|shoes|sneakers|boots|"
    r"sandals|slippers|hat|beanie|gloves apparel|jersey|dress|skirt|bra|underwear|"
    r"ceiling fan|chandelier|pendant light|floor lamp|table lamp|area rug|curtain|bedsheet)\b",
    re.IGNORECASE
)

DATED_RE = re.compile(
    r"\b(calendar|planner|agenda|dated|desk pad calendar|wall calendar|"
    r"weekly planner|monthly planner|daily planner|academic year|fiscal year|"
    r"appointment book|schedule book|202[4-7])\b",
    re.IGNORECASE
)

# 13 Low-Return Amazon Root Categories (< 4.5% Return Rate)
SECTOR_DEFINITIONS: Dict[int, Dict[str, Any]] = {
    3760911: {"name": "Beauty & Personal Care (Marlo Beauty)", "is_root": True, "ref_fee": 0.15},
    16310101: {"name": "Grocery & Gourmet Food (Frontier / Atlantic Spice)", "is_root": True, "ref_fee": 0.15},
    165793011: {"name": "Toys & Games (EE Distribution)", "is_root": True, "ref_fee": 0.15},
    3760901: {"name": "Health & Household (Frontier / Personal Care)", "is_root": True, "ref_fee": 0.15},
    1064954: {"name": "Office Products (Low-Return B2B Consumables)", "is_root": True, "ref_fee": 0.15},
    2619533011: {"name": "Pet Supplies (Consumables, Filters & Grooming)", "is_root": True, "ref_fee": 0.15},
    2617941011: {"name": "Arts, Crafts & Sewing (Adhesives, Blades & Bulk)", "is_root": True, "ref_fee": 0.15},
    2972638011: {"name": "Patio, Lawn & Garden (Irrigation, Pool & Parts)", "is_root": True, "ref_fee": 0.15},
    11091801: {"name": "Musical Instruments (Strings, Reeds & Multipacks)", "is_root": True, "ref_fee": 0.15},
    3375251: {"name": "Sports & Outdoors (Tackle, Golf & Grip Consumables)", "is_root": True, "ref_fee": 0.15},
    16310091: {"name": "Industrial & Scientific (Zoro / Motion / BCI)", "is_root": True, "ref_fee": 0.12},
    228013: {"name": "Tools & Home Improvement (JB Tools / Zoro / BCI)", "is_root": True, "ref_fee": 0.15},
    15684181: {"name": "Automotive & Commercial Fleet (Motion / Zoro)", "is_root": True, "ref_fee": 0.12},
}

# Dedicated Multipack Loophole slices (title="pack", $13.50-$48) + Core Wholesale slices
SEARCH_SLICES = [
    # Slice 0: Multipack Loophole Hunter (2+ Packs in Beauty, Spices, Office, Pet, Music, Health, Toys)
    {"price_min": 13.5, "price_max": 48, "bsr_min": 500, "bsr_max": 45000, "min_sellers": 2, "max_sellers": 5, "title_kw": "pack", "sort": "salesRankDrops30", "asc": False},
    # Slice 1: Fast-Moving Commercial & Consumable ($15-$65)
    {"price_min": 15, "price_max": 65, "bsr_min": 500, "bsr_max": 20000, "min_sellers": 2, "max_sellers": 5, "title_kw": "", "sort": "salesRankDrops30", "asc": False},
    # Slice 2: Mid-Velocity Niche Wholesale ($18-$85)
    {"price_min": 18, "price_max": 85, "bsr_min": 20001, "bsr_max": 50000, "min_sellers": 2, "max_sellers": 5, "title_kw": "", "sort": "salesRankDrops30", "asc": False},
    # Slice 3: High-Ticket MRO & Commercial Equipment ($55-$160)
    {"price_min": 55, "price_max": 160, "bsr_min": 1000, "bsr_max": 45000, "min_sellers": 2, "max_sellers": 5, "title_kw": "", "sort": "salesRankDrops90", "asc": False},
]


def norm_unicode_upper(text: Any) -> str:
    s = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii")
    return s.upper().strip()


def clean_brand_name(brand: str) -> str:
    b = norm_unicode_upper(brand)
    if not b:
        return ""
    b = re.sub(r'[^A-Z0-9\s&\'-]', ' ', b)
    b = re.sub(r'\b(LLC|INC|CORP|CORPORATION|CO|COMPANY|LTD|HOLDINGS|USA|ENTERPRISES)\b', '', b)
    return re.sub(r'\s+', ' ', b).strip()


def extract_root_mpn(mpn: str) -> str:
    if not mpn:
        return ""
    clean = re.sub(r'[^A-Z0-9]', '', str(mpn).upper().strip())
    return clean.lstrip('0') or clean


def normalize_barcode(code: str) -> str:
    s = str(code or "").strip()
    if "e+" in s.lower():
        try:
            s = f"{int(float(s))}"
        except Exception:
            pass
    clean = re.sub(r"[^0-9]", "", s)
    if len(clean) == 11:
        clean = "0" + clean
    if len(clean) in (12, 13, 14):
        return clean.zfill(14)
    return ""


def detect_vendor_from_path(filepath: Path) -> str:
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
    if "grainger" in fn:
        return "Grainger"
    if "global_industrial" in fn:
        return "Global Industrial"
    if "petedge" in fn:
        return "PetEdge"
    return filepath.stem.replace("_Catalog", "").replace("_normalized", "").upper()


def parse_pack_quantity(text: str, keepa_product: Optional[Dict[str, Any]] = None) -> int:
    if keepa_product:
        kp_qty = keepa_product.get("packageQuantity") or keepa_product.get("numberOfItems")
        if kp_qty and isinstance(kp_qty, int) and 1 < kp_qty <= 48:
            return kp_qty

    if not text:
        return 1

    t = text.lower().strip()
    m_pack = re.search(r'\b(?:pack|pk|box|case|set|bundle|lot)\s*(?:of|-|–|\s)\s*([0-9]{1,3})\b', t)
    if m_pack:
        val = int(m_pack.group(1))
        if 1 < val <= 48:
            return val

    m_hyphen = re.search(r'\b([0-9]{1,3})\s*(?:-|–|\s)?\s*(?:pack|pk|piece|pc|count|ct|bundle)\b', t)
    if m_hyphen:
        val = int(m_hyphen.group(1))
        if 1 < val <= 48:
            return val

    m_lead = re.search(r'\b([2-9]|1[0-2])\s*[xX]\s+[a-zA-Z0-9]', t)
    if m_lead:
        val = int(m_lead.group(1))
        if 1 < val <= 24:
            return val

    if re.search(r'\b(?:pair|two pack|2-pack|twin pack|double pack)\b', t):
        return 2

    return 1


def is_accessory_mismatch(amz_title: str, dist_title: str, cogs: float, bb: float) -> Tuple[bool, str]:
    cogs = float(cogs or 0.0)
    bb = float(bb or 0.0)

    if bb > 25.0 and cogs > 0:
        cost_ratio = cogs / bb
        if cost_ratio < 0.20:
            return True, f"SUB_FLOOR_COST_RATIO ({cost_ratio:.1%} < 20% floor | Potential accessory mismatch)"

    amz_t = str(amz_title).lower()
    dist_t = str(dist_title).lower()

    has_amz_equip = any(eq in amz_t for eq in PRIMARY_EQUIPMENT_TOKENS)
    has_dist_accessory = any(acc in dist_t for acc in ACCESSORY_TOKENS)
    has_amz_accessory = any(acc in amz_t for acc in ACCESSORY_TOKENS)

    if has_amz_equip and has_dist_accessory and not has_amz_accessory:
        return True, "ACCESSORY_MISMATCH (Distributor title matches accessory token but Amazon listing is parent equipment)"

    return False, "VALID"


def validate_real_buybox_and_brand_safety(
    current_bb: float,
    avg90_bb: float,
    lowest_fba: float,
    lowest_fbm: float,
    fba_sellers: int,
    total_sellers: int,
    brand: str,
    monthly_units: int = 0,
    is_multipack: bool = False
) -> Tuple[bool, float, str]:
    if not brand or str(brand).strip().upper() in ("", "UNKNOWN", "GENERIC", "N/A"):
        return False, 0.0, "MISSING_OR_GENERIC_BRAND"

    # Enforce 0 FBA (>= 2 FBM on Multipack Loophole, >= 3 FBM otherwise) OR 2 to 4 FBA sellers
    if fba_sellers == 1 or fba_sellers > 4:
        return False, 0.0, f"FBA_WINDOW_REJECTED (fba={fba_sellers}, must be 0 or 2-4)"

    min_fbm = 2 if is_multipack else 3
    if fba_sellers == 0 and total_sellers < min_fbm:
        return False, 0.0, f"0_FBA_INSUFFICIENT_FBM ({total_sellers} < {min_fbm})"

    min_vel = 25 if is_multipack else (60 if fba_sellers == 0 else 40)
    if monthly_units < min_vel:
        return False, 0.0, f"VELOCITY_BELOW_TIER_FLOOR ({monthly_units} < {min_vel} u/mo)"

    active_offers = [p for p in (current_bb, lowest_fba, lowest_fbm) if p and p > 0]
    if not active_offers:
        return False, 0.0, "NO_ACTIVE_OFFERS_BUYBOX_SUPPRESSED"

    real_market_price = min(active_offers)
    if avg90_bb and avg90_bb > 0 and real_market_price > (avg90_bb * 1.35):
        return False, 0.0, f"SUPPRESSED_BUYBOX_PRICE_SPIKE (${real_market_price:.2f} vs 90d ${avg90_bb:.2f})"

    effective_price = min(real_market_price, avg90_bb) if (avg90_bb and avg90_bb > 0) else real_market_price
    tier_tag = "MULTIPACK_LOOPHOLE" if is_multipack else ("0_FBA_BUYBOX_STEAL" if fba_sellers == 0 else "VERIFIED_ACTIVE_BUYBOX")
    return True, round(effective_price, 2), tier_tag


def acquire_singleton_lock():
    try:
        lock_file = open(LOCK_FILE_PATH, "w")
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_file
    except (IOError, BlockingIOError):
        logger.warning("Duplicate process detected. Exiting.")
        sys.exit(0)


class DatabaseManager:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=60.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 60000")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def _init_db(self):
        with closing(self.get_connection()) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS candidate_cache (
                    asin TEXT PRIMARY KEY,
                    title TEXT, brand TEXT, category_id INTEGER,
                    upc TEXT, ean TEXT, mpn TEXT, sales_rank INTEGER,
                    drops_90 INTEGER, monthly_sold INTEGER, avg90_bb REAL,
                    current_bb REAL, weight_lbs REAL, max_dim_in REAL,
                    volume_cuft REAL, referral_pct REAL, fba_fee REAL,
                    num_sellers INTEGER, amazon_pack_qty INTEGER,
                    is_hazmat INTEGER DEFAULT 0, last_evaluated INTEGER,
                    status TEXT, drop_reason TEXT,
                    wholesale_cost REAL, matched_vendor TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS verified_deals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asin TEXT UNIQUE, title TEXT, brand TEXT, confidence_tier TEXT,
                    sales_rank INTEGER, buy_box_price REAL, wholesale_cost REAL,
                    max_allowable_buy_cost REAL, net_profit REAL, roi_percent REAL,
                    profit_per_dollar REAL, expected_order_profit REAL,
                    monthly_profit_pool REAL, suggested_order_size INTEGER,
                    vendor TEXT, is_hazmat INTEGER DEFAULT 0, status TEXT,
                    created_at INTEGER, updated_at INTEGER
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS search_cursors (
                    slice_id TEXT PRIMARY KEY, current_page INTEGER,
                    total_discovered INTEGER, consecutive_failures INTEGER DEFAULT 0,
                    last_updated INTEGER
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sheet_synced_leads (
                    asin TEXT PRIMARY KEY, synced_at INTEGER
                )
            """)
            conn.commit()

    def should_evaluate(self, asin: str) -> bool:
        now = int(time.time())
        with closing(self.get_connection()) as conn:
            cur = conn.cursor()
            cur.execute("SELECT status, last_evaluated FROM candidate_cache WHERE asin = ?", (asin,))
            row = cur.fetchone()
            if not row:
                return True
            status = str(row["status"] or "").upper()
            elapsed = now - (row["last_evaluated"] or 0)
            if status in ("STRUCTURAL_REJECT", "POOR_REVIEWS", "OVERWEIGHT", "RESTRICTED_BRAND", "BLOCKED_IP_BRAND", "PRIVATE_LABEL_TRAP", "HIGH_RETURN_CATEGORY"):
                return elapsed > (30 * 86400)
            return elapsed > (3 * 86400)

    def cache_candidate(self, asin: str, data: Dict[str, Any], status: str, drop_reason: str):
        now = int(time.time())
        with closing(self.get_connection()) as conn:
            conn.execute("""
                INSERT INTO candidate_cache (
                    asin, title, brand, category_id, upc, ean, mpn, sales_rank,
                    drops_90, monthly_sold, avg90_bb, current_bb, weight_lbs,
                    referral_pct, fba_fee, num_sellers, amazon_pack_qty,
                    is_hazmat, last_evaluated, status, drop_reason,
                    wholesale_cost, matched_vendor
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(asin) DO UPDATE SET
                    sales_rank = excluded.sales_rank,
                    drops_90 = excluded.drops_90,
                    monthly_sold = excluded.monthly_sold,
                    avg90_bb = excluded.avg90_bb,
                    current_bb = excluded.current_bb,
                    num_sellers = excluded.num_sellers,
                    amazon_pack_qty = excluded.amazon_pack_qty,
                    last_evaluated = excluded.last_evaluated,
                    status = excluded.status,
                    drop_reason = excluded.drop_reason,
                    wholesale_cost = COALESCE(excluded.wholesale_cost, candidate_cache.wholesale_cost),
                    matched_vendor = COALESCE(excluded.matched_vendor, candidate_cache.matched_vendor)
            """, (
                asin, data.get("title", ""), data.get("brand", ""), data.get("category_id", 0),
                data.get("upc", ""), data.get("ean", ""), data.get("mpn", ""), data.get("sales_rank", 0),
                data.get("drops_90", 0), data.get("monthly_sold", 0), data.get("avg90_bb", 0.0),
                data.get("current_bb", 0.0), data.get("weight_lbs", 0.0),
                data.get("referral_pct", 0.15), data.get("fba_fee", 4.15),
                data.get("num_sellers", 0), data.get("amazon_pack_qty", 1),
                1 if data.get("is_hazmat") else 0, now, status, drop_reason,
                data.get("wholesale_cost"), data.get("matched_vendor")
            ))
            conn.commit()

    def record_deal(self, deal: Dict[str, Any]):
        now = int(time.time())
        with closing(self.get_connection()) as conn:
            conn.execute("""
                INSERT INTO verified_deals (
                    asin, title, brand, confidence_tier, sales_rank, buy_box_price,
                    wholesale_cost, max_allowable_buy_cost, net_profit, roi_percent,
                    profit_per_dollar, expected_order_profit, monthly_profit_pool,
                    suggested_order_size, vendor, is_hazmat, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(asin) DO UPDATE SET
                    confidence_tier = excluded.confidence_tier,
                    buy_box_price = excluded.buy_box_price,
                    wholesale_cost = excluded.wholesale_cost,
                    net_profit = excluded.net_profit,
                    roi_percent = excluded.roi_percent,
                    monthly_profit_pool = excluded.monthly_profit_pool,
                    suggested_order_size = excluded.suggested_order_size,
                    vendor = excluded.vendor,
                    updated_at = excluded.updated_at
            """, (
                deal["asin"], deal["title"], deal["brand"], deal["confidence_tier"],
                deal["sales_rank"], deal["buy_box_price"], deal["wholesale_cost"],
                deal["max_allowable_buy_cost"], deal["net_profit"], deal["roi_percent"],
                deal["profit_per_dollar"], deal["expected_order_profit"],
                deal["monthly_profit_pool"], deal["suggested_order_size"],
                deal["vendor"], 1 if deal.get("is_hazmat") else 0, "QUALIFIED", now, now
            ))
            conn.commit()


class IndustrialCatalogEngine:
    def __init__(self, db: DatabaseManager):
        self.db = db
        self.catalog_barcodes: Dict[str, Dict[str, Any]] = {}
        self.catalog_brand_mpns: Dict[str, Dict[str, Any]] = {}

    def build_index(self):
        logger.info("Recursively indexing all distributor catalogs (MRO, Beauty, Frontier Co-op, Atlantic Spice, EE Distribution, Pet, Office)...")
        self.catalog_barcodes.clear()
        self.catalog_brand_mpns.clear()

        if not CATALOG_BASE_DIR.exists():
            return

        seen_paths: Set[str] = set()
        files: List[Path] = []
        for ext in ("*.parquet", "*.csv", "*.xlsx"):
            for f in sorted(CATALOG_BASE_DIR.rglob(ext)):
                if f.name not in seen_paths:
                    seen_paths.add(f.name)
                    files.append(f)

        for cat_file in files:
            vendor_name = detect_vendor_from_path(cat_file)
            tier = "MANUFACTURER" if "manufacturer" in str(cat_file).lower() else "WHOLESALE"
            try:
                if cat_file.suffix.lower() == ".parquet":
                    df = pd.read_parquet(cat_file)
                elif cat_file.suffix.lower() == ".csv":
                    df = pd.read_csv(cat_file, low_memory=False, dtype=str)
                else:
                    df = pd.read_excel(cat_file, dtype=str)

                if df.empty:
                    continue

                cols = {str(c).lower().strip(): c for c in df.columns}
                upc_col = next((v for k, v in cols.items() if any(t in k for t in ("gtin", "upc", "ean", "barcode"))), None)
                sku_col = next((v for k, v in cols.items() if any(t in k for t in ("clean_mpn", "mpn", "part_number", "item_number", "sku", "model"))), None)
                cost_col = next((v for k, v in cols.items() if any(t in k for t in ("unit_cost", "wholesale_cost", "wholesale", "cost", "price"))), None)
                brand_col = next((v for k, v in cols.items() if any(t in k for t in ("brand", "manufacturer", "mfg"))), None)
                title_col = next((v for k, v in cols.items() if any(t in k for t in ("title", "product_title", "description", "name"))), None)

                if not cost_col:
                    continue

                for _, row in df.iterrows():
                    raw_val = row.get(cost_col)
                    if pd.isna(raw_val):
                        continue
                    try:
                        cost = float(str(raw_val).replace("$", "").replace(",", "").strip())
                    except ValueError:
                        continue
                    if cost <= 0.10:
                        continue

                    brand_clean = clean_brand_name(str(row.get(brand_col, vendor_name))) if brand_col else clean_brand_name(vendor_name)
                    dist_title = str(row.get(title_col, "")).strip() if title_col else ""
                    entry = {
                        "vendor": vendor_name,
                        "tier": tier,
                        "unit_cost": cost,
                        "brand": brand_clean,
                        "dist_title": dist_title,
                    }

                    if upc_col and pd.notna(row.get(upc_col)):
                        norm = normalize_barcode(row[upc_col])
                        if norm and (norm not in self.catalog_barcodes or cost < self.catalog_barcodes[norm]["unit_cost"]):
                            self.catalog_barcodes[norm] = entry

                    if sku_col and pd.notna(row.get(sku_col)):
                        sku_raw = str(row.get(sku_col, "")).strip().upper()
                        root_val = extract_root_mpn(sku_raw)
                        if root_val and len(root_val) >= 4 and brand_clean:
                            self.catalog_brand_mpns[f"{brand_clean}:{root_val}"] = entry

            except Exception as e:
                logger.error(f"Catalog ingest error on {cat_file.name}: {e}")

        logger.info(f"Recursive Index ready: {len(self.catalog_barcodes):,} barcodes | {len(self.catalog_brand_mpns):,} brand-coupled MPNs.")

    def match(self, codes: List[str], brand: str, part_num: str) -> Optional[Dict[str, Any]]:
        for c in codes:
            norm = normalize_barcode(c)
            if norm and norm in self.catalog_barcodes:
                return self.catalog_barcodes[norm]

        b = clean_brand_name(brand)
        root_p = extract_root_mpn(part_num)
        if b and root_p and len(root_p) >= 4:
            key = f"{b}:{root_p}"
            if key in self.catalog_brand_mpns:
                return self.catalog_brand_mpns[key]

        return None


class ReverseAmazonHunter:
    def __init__(self):
        self.db = DatabaseManager()
        self.catalog = IndustrialCatalogEngine(self.db)
        self.catalog.build_index()
        self.blocked_brands: Set[str] = set()
        self.blocked_multi_word: List[str] = []
        self._load_blocklist()

    def _load_blocklist(self):
        if PRIMARY_IP_FILE.exists():
            try:
                raw = json.loads(PRIMARY_IP_FILE.read_text(encoding="utf-8"))
                brand_list = raw if isinstance(raw, list) else raw.get("brands", [])
                self.blocked_brands = {norm_unicode_upper(b) for b in brand_list if b}
                self.blocked_multi_word = [b for b in sorted(self.blocked_brands) if len(b) >= 5]
                logger.info(f"Loaded {len(self.blocked_brands)} restricted brands into hunter pre-filter.")
            except Exception as e:
                logger.error(f"Failed loading IP blocklist: {e}")

    def is_brand_blocked(self, brand: str) -> bool:
        b = norm_unicode_upper(brand)
        if not b or len(b) < 3:
            return True
        if b in self.blocked_brands:
            return True
        normalized = re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9\s&'-]", " ", b)).strip()
        if normalized in self.blocked_brands:
            return True
        for entry in self.blocked_multi_word:
            if normalized.startswith(entry + " ") or (" " + entry) in normalized:
                return True
        return False

    def evaluate_product(self, product: Dict[str, Any], sector_id: int) -> Optional[Dict[str, Any]]:
        asin = product.get("asin", "")
        title = product.get("title") or ""
        brand = (product.get("brand") or "").strip()
        is_hazmat = bool(product.get("isHazMat", False) or product.get("dangerousGoods", False))

        if not brand or self.is_brand_blocked(brand):
            self.db.cache_candidate(asin, {"title": title, "brand": brand}, "BLOCKED_IP_BRAND", f"Brand '{brand}' on 784-Brand Blocklist")
            return None

        if DATED_RE.search(title):
            return None

        if HIGH_RETURN_TITLE_RE.search(title):
            self.db.cache_candidate(asin, {"title": title, "brand": brand}, "HIGH_RETURN_CATEGORY", "Apparel/Footwear/Decor high-return item")
            return None

        stats = product.get("stats") or {}
        current_prices = stats.get("current") or []
        avg90_prices = stats.get("avg90") or []

        rating_val = current_prices[16] / 10.0 if len(current_prices) > 16 and current_prices[16] and current_prices[16] > 0 else 4.0
        if rating_val < MIN_REVIEW_RATING:
            return None

        keepa_monthly_sold = product.get("monthlySold") or 0
        drops_90 = stats.get("salesRankDrops90") or 0
        drops_30 = stats.get("salesRankDrops30") or 0
        current_bsr = current_prices[3] if len(current_prices) > 3 and current_prices[3] and current_prices[3] > 0 else (avg90_prices[3] if len(avg90_prices) > 3 else 0)

        monthly_units = calculate_continuous_velocity(
            sales_rank=current_bsr,
            drops_30=drops_30,
            drops_90=drops_90,
            monthly_sold=keepa_monthly_sold
        )

        if monthly_units < 25:
            return None

        pkg_weight_g = product.get("packageWeight") or 0
        weight_lbs = round(pkg_weight_g / 453.592, 2)
        if weight_lbs > STRICT_MAX_WEIGHT_LBS:
            return None

        curr_bb_cents = current_prices[18] if len(current_prices) > 18 and current_prices[18] and current_prices[18] > 0 else 0
        avg90_bb_cents = avg90_prices[18] if len(avg90_prices) > 18 and avg90_prices[18] and avg90_prices[18] > 0 else 0
        lowest_fba_cents = current_prices[10] if len(current_prices) > 10 and current_prices[10] and current_prices[10] > 0 else 0
        lowest_fbm_cents = current_prices[7] if len(current_prices) > 7 and current_prices[7] and current_prices[7] > 0 else 0

        current_bb = round(curr_bb_cents / 100.0, 2)
        avg90_bb = round(avg90_bb_cents / 100.0, 2)
        lowest_fba = round(lowest_fba_cents / 100.0, 2)
        lowest_fbm = round(lowest_fbm_cents / 100.0, 2)

        offers = product.get("offers") or []
        new_offers = [o for o in offers if o.get("condition") == 1]
        offers_fba = sum(1 for o in new_offers if o.get("isFBA"))
        fba_stat = int(stats.get("offerCountFBA") or 0)
        competitive_fba = max(fba_stat, offers_fba)

        total_new_stat = current_prices[11] if len(current_prices) > 11 and current_prices[11] and current_prices[11] > 0 else 0
        total_sellers = max(total_new_stat, len(new_offers), competitive_fba)

        brand_clean_upper = clean_brand_name(brand)
        for o in new_offers:
            seller_name = clean_brand_name(str(o.get("sellerName") or ""))
            if seller_name and brand_clean_upper and (seller_name == brand_clean_upper or brand_clean_upper in seller_name):
                self.db.cache_candidate(asin, {"title": title, "brand": brand}, "PRIVATE_LABEL_TRAP", f"Manufacturer/Brand Owner selling directly ({seller_name})")
                return None

        amazon_pack_qty = parse_pack_quantity(title, product)
        if amazon_pack_qty > 48:
            self.db.cache_candidate(asin, {"title": title, "brand": brand}, "STRUCTURAL_REJECT", f"Pack qty {amazon_pack_qty} exceeds 48-unit ceiling")
            return None

        upc_list = product.get("upcList") or []
        ean_list = product.get("eanList") or []
        codes = list(set([str(c) for c in (upc_list + ean_list) if c]))
        primary_upc = codes[0] if codes else ""
        part_num = product.get("model") or product.get("partNumber") or ""

        cat_match = self.catalog.match(codes=codes, brand=brand, part_num=part_num)
        is_multipack_loophole = bool(cat_match) and (amazon_pack_qty >= 2)

        is_bb_safe, buy_box_price, tier_or_reason = validate_real_buybox_and_brand_safety(
            current_bb=current_bb,
            avg90_bb=avg90_bb,
            lowest_fba=lowest_fba,
            lowest_fbm=lowest_fbm,
            fba_sellers=competitive_fba,
            total_sellers=total_sellers,
            brand=brand,
            monthly_units=monthly_units,
            is_multipack=is_multipack_loophole
        )
        if not is_bb_safe or buy_box_price < 13.50:
            self.db.cache_candidate(asin, {"title": title, "brand": brand, "current_bb": current_bb, "avg90_bb": avg90_bb, "num_sellers": total_sellers}, "PRIVATE_LABEL_TRAP", tier_or_reason)
            return None

        s_meta = SECTOR_DEFINITIONS.get(sector_id, {"ref_fee": 0.15, "name": "Commercial Wholesale"})
        referral_fee = round(buy_box_price * s_meta["ref_fee"], 2)
        fba_fee = round(3.86 + (weight_lbs * 0.45), 2)

        base_meta = {
            "title": title, "brand": brand, "category_id": sector_id,
            "upc": primary_upc, "mpn": str(part_num), "sales_rank": current_bsr,
            "is_hazmat": is_hazmat, "monthly_sold": monthly_units,
            "drops_90": drops_90, "avg90_bb": avg90_bb, "current_bb": current_bb,
            "weight_lbs": weight_lbs, "referral_pct": s_meta["ref_fee"], "fba_fee": fba_fee,
            "num_sellers": total_sellers, "amazon_pack_qty": amazon_pack_qty
        }

        if not cat_match:
            self.db.cache_candidate(asin, base_meta, "UNMAPPED_CANDIDATE", "Awaiting catalog UPC/MPN match")
            return None

        wholesale_cost = round(cat_match["unit_cost"] * amazon_pack_qty, 2)
        vendor = cat_match["vendor"]
        dist_title = cat_match.get("dist_title", "")
        base_meta["wholesale_cost"] = wholesale_cost
        base_meta["matched_vendor"] = vendor

        mismatch, mismatch_reason = is_accessory_mismatch(title, dist_title, wholesale_cost, buy_box_price)
        if mismatch:
            self.db.cache_candidate(asin, base_meta, "STRUCTURAL_REJECT", mismatch_reason)
            return None

        if wholesale_cost < STRICT_MIN_WHOLESALE_COST or wholesale_cost > STRICT_MAX_WHOLESALE_COST:
            self.db.cache_candidate(asin, base_meta, "FILTERED", f"Cost ${wholesale_cost} out of bounds")
            return None

        days_to_fba = estimate_days_to_fba(brand, is_hazmat=is_hazmat, pack_qty=amazon_pack_qty)
        if days_to_fba > MAX_DAYS_TO_FBA:
            self.db.cache_candidate(asin, base_meta, "FILTERED", f"Days to FBA ({days_to_fba}d) exceeds {MAX_DAYS_TO_FBA}d ceiling")
            return None

        eff_comp = competitive_fba if competitive_fba > 0 else max(1, round(total_sellers * 0.45))
        effective_share_velocity = max(1, round(monthly_units / (eff_comp + 1)))

        logistics = get_logistics_profile(vendor)
        multipack_prep_buffer = 0.75 if is_multipack_loophole else 0.00
        prep_base = logistics["prep_base"] + (0.60 if is_hazmat else 0.0) + multipack_prep_buffer
        inbound_freight = round(weight_lbs * (logistics["freight_per_lb"] + (0.20 if is_hazmat else 0.0)), 2)
        total_prep = round(prep_base + inbound_freight + 0.27, 2)

        total_overhead = referral_fee + fba_fee + total_prep + round(buy_box_price * 0.03, 2)
        net_profit = round(buy_box_price - (wholesale_cost + total_overhead), 2)
        landed_cost = wholesale_cost + total_prep
        roi_percent = round((net_profit / landed_cost) * 100.0, 1) if landed_cost > 0 else 0.0

        # Two-Tier Profit Gate: Multipack Loophole (>= $2.25/unit, >= $180/mo) vs Single-Unit (>= $3.00/unit, >= $250/mo)
        profit_floor = 2.25 if is_multipack_loophole else 3.00
        min_pool_required = 180.0 if is_multipack_loophole else 250.0
        if net_profit < profit_floor or roi_percent < 18.0:
            self.db.cache_candidate(asin, base_meta, "FILTERED", f"Net ${net_profit:.2f} < ${profit_floor:.2f} or ROI {roi_percent}% < 18%")
            return None

        monthly_profit_pool = round(effective_share_velocity * net_profit, 2)
        if monthly_profit_pool < min_pool_required:
            self.db.cache_candidate(asin, base_meta, "FILTERED", f"Pool ${monthly_profit_pool:.2f} < ${min_pool_required:.2f}/mo requirement")
            return None

        self.db.cache_candidate(asin, base_meta, "APPROVED", "Verified wholesale match")
        confidence_tier = "[VERIFIED MULTIPACK LOOPHOLE]" if is_multipack_loophole else ("[0-FBA PRIME STEAL]" if competitive_fba == 0 else "[VERIFIED WHOLESALE]")
        suggested_order = max(12, int(effective_share_velocity * 0.5))

        return {
            "asin": asin, "title": title, "brand": brand,
            "vendor": vendor, "prep_hub": "Bundle Poly-Bag + FNSKU" if is_multipack_loophole else logistics["facility"],
            "confidence_tier": confidence_tier, "sales_rank": current_bsr,
            "buy_box_price": buy_box_price, "current_bb": current_bb, "avg90_bb": avg90_bb,
            "lowest_fbm_price": lowest_fbm, "wholesale_cost": landed_cost,
            "max_allowable_buy_cost": round(buy_box_price - total_overhead - profit_floor, 2),
            "net_profit": net_profit, "roi_percent": roi_percent, "profit_floor": profit_floor,
            "req_roi": 18.0, "profit_per_dollar": round(net_profit / max(0.01, landed_cost), 2),
            "suggested_order_size": suggested_order, "expected_order_profit": round(net_profit * suggested_order, 2),
            "monthly_profit_pool": monthly_profit_pool,
            "monthly_units": monthly_units, "our_share_velocity": int(effective_share_velocity),
            "competitive_fba": competitive_fba, "total_sellers": total_sellers,
            "drops_90": drops_90, "pack_count": amazon_pack_qty,
            "days_to_fba": days_to_fba, "is_hazmat": is_hazmat, "weight_lbs": weight_lbs,
            "order_url": f"https://www.amazon.com/dp/{asin}",
            "seller_check_url": f"https://sellercentral.amazon.com/product-search/search?q={asin}",
            "seller_approval_url": f"https://sellercentral.amazon.com/hz/approvalrequest/restrictions/approve?asin={asin}",
            "status": "Ready for Order"
        }

    def push_deal(self, deal: Dict[str, Any]):
        self.db.record_deal(deal)

        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            title_esc = html.escape(deal["title"][:45])
            brand_esc = html.escape(deal["brand"])
            vendor_esc = html.escape(deal["vendor"])
            hazmat_tag = "⚠️ <b>HAZMAT</b> | " if deal.get("is_hazmat") else ""
            steal_tag = f"📦 <b>MULTIPACK LOOPHOLE ({deal['pack_count']}x Bundle)</b>\n" if deal["pack_count"] >= 2 else (
                f"🏆 <b>0-FBA BUY BOX STEAL ({deal['total_sellers']} FBM Sellers)</b>\n" if deal["competitive_fba"] == 0 else ""
            )

            msg = (
                f"💎 <b>VERIFIED WHOLESALE DEAL</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"{steal_tag}"
                f"📦 <a href=\"{deal['order_url']}\">{title_esc}</a>\n"
                f"🏷 <b>Brand:</b> <code>{brand_esc}</code> | <b>ASIN:</b> <code>{deal['asin']}</code>\n"
                f"🏢 <b>Vendor:</b> {vendor_esc} ({deal['prep_hub']})\n"
                f"⚡ {hazmat_tag}<b>Pack:</b> {deal['pack_count']}x | <b>Days:</b> {deal['days_to_fba']}d | <b>Weight:</b> {deal['weight_lbs']} lbs\n"
                f"📈 <b>True Velocity:</b> <b>{deal['monthly_units']} u/mo</b> | <b>Share:</b> {deal['our_share_velocity']} u/mo\n"
                f"👥 <b>FBA Sellers:</b> {deal['competitive_fba']} (Total: {deal['total_sellers']}) | <b>BSR:</b> #{deal['sales_rank']:,}\n"
                f"💰 <b>Target Price:</b> ${deal['buy_box_price']:.2f} | <b>Landed COGS:</b> ${deal['wholesale_cost']:.2f}\n"
                f"🚀 <b>Net Profit:</b> <b>${deal['net_profit']:.2f} / unit</b> ({deal['roi_percent']}% ROI)\n"
                f"🏊 <b>Monthly Pool:</b> <b>${deal['monthly_profit_pool']:.2f} / mo</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━"
            )

            try:
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True},
                    timeout=8
                )
            except Exception as e:
                logger.warning(f"Telegram dispatch error: {e}")

        webhook_url = get_verified_webhook_url()
        if webhook_url:
            try:
                payload = {
                    "action": "ADD_BUY_LEAD",
                    "asin": deal["asin"],
                    "title": deal["title"],
                    "brand": deal["brand"],
                    "confidence_tier": deal["confidence_tier"],
                    "vendor": deal["vendor"],
                    "prep_center": deal["prep_hub"],
                    "sales_rank": deal["sales_rank"],
                    "monthly_velocity": deal["monthly_units"],
                    "competitive_fba": deal["competitive_fba"],
                    "total_sellers": deal["total_sellers"],
                    "our_share_velocity": deal["our_share_velocity"],
                    "pack_count": deal["pack_count"],
                    "days_to_fba": deal["days_to_fba"],
                    "buy_box_price": deal["buy_box_price"],
                    "current_bb": deal["current_bb"],
                    "avg90_bb": deal["avg90_bb"],
                    "lowest_fbm_price": deal["lowest_fbm_price"],
                    "wholesale_cost": deal["wholesale_cost"],
                    "landed_bundle_cogs": deal["wholesale_cost"],
                    "net_profit": deal["net_profit"],
                    "roi_percent": deal["roi_percent"],
                    "suggested_order_size": deal["suggested_order_size"],
                    "monthly_profit_pool": deal["monthly_profit_pool"],
                    "is_hazmat": "YES" if deal.get("is_hazmat") else "NO",
                    "order_url": deal["order_url"],
                    "seller_check_url": deal["seller_check_url"],
                    "seller_approval_url": deal["seller_approval_url"],
                    "notes": f"Pool: ${deal['monthly_profit_pool']:.2f}/mo | Pack: {deal['pack_count']}x | FBA: {deal['competitive_fba']} / Total: {deal['total_sellers']}",
                    "status": "Ready for Order"
                }
                requests.post(webhook_url, json=payload, timeout=15, allow_redirects=True)
            except Exception as e:
                logger.debug(f"Sheet dispatch error: {e}")

    def fetch_product_batch(self, asins: List[str]) -> List[Dict[str, Any]]:
        clean = [a.strip().upper() for a in asins if a and len(a.strip()) == 10 and self.db.should_evaluate(a.strip())]
        if not clean:
            return []
        batch = clean[:100]
        try:
            r = requests.get(
                "https://api.keepa.com/product",
                params={"key": KEEPA_API_KEY, "domain": "1", "asin": ",".join(batch), "stats": "180", "offers": "20"},
                timeout=45
            )
            if r.status_code == 200:
                return r.json().get("products", [])
        except Exception as e:
            logger.error(f"Keepa fetch batch error: {e}")
        return []

    def run_sweep(self):
        logger.info("Executing balanced sweep across 13 low-return categories (Beauty, Grocery/Spices, Office, Pet, Crafts, Garden, Music, Sports, Toys & MRO)...")
        if not KEEPA_API_KEY:
            logger.warning("No KEEPA_API_KEY configured.")
            return

        with closing(self.db.get_connection()) as conn:
            cur = conn.cursor()
            for slice_idx, slice_cfg in enumerate(SEARCH_SLICES):
                for sector_id in list(SECTOR_DEFINITIONS.keys()):
                    slice_key = f"{sector_id}:{slice_idx}"
                    cur.execute("SELECT current_page FROM search_cursors WHERE slice_id = ?", (slice_key,))
                    row = cur.fetchone()
                    page = row["current_page"] if row else 0

                    if page >= MAX_SEARCH_PAGE_DEPTH:
                        page = 0

                    selection: Dict[str, Any] = {
                        "current_COUNT_NEW_gte": slice_cfg["min_sellers"],
                        "current_COUNT_NEW_lte": slice_cfg["max_sellers"],
                        "buyBoxIsAmazon": False,
                        "current_BUY_BOX_SHIPPING_gte": int(slice_cfg["price_min"] * 100),
                        "current_BUY_BOX_SHIPPING_lte": int(slice_cfg["price_max"] * 100),
                        "current_SALES_gte": slice_cfg["bsr_min"],
                        "current_SALES_lte": slice_cfg["bsr_max"],
                        "sort": [[slice_cfg["sort"], "asc" if slice_cfg["asc"] else "desc"]],
                        "perPage": 100,
                        "page": page
                    }
                    if slice_cfg.get("title_kw"):
                        selection["title"] = slice_cfg["title_kw"]

                    s_def = SECTOR_DEFINITIONS.get(sector_id, {"is_root": True})
                    if s_def["is_root"]:
                        selection["rootCategory"] = [sector_id]
                    else:
                        selection["categories_include"] = [sector_id]

                    try:
                        res = requests.post("https://api.keepa.com/query", data={"key": KEEPA_API_KEY, "domain": 1, "selection": json.dumps(selection)}, timeout=30)
                        if res.status_code != 200:
                            continue
                        asins = res.json().get("asinList", [])

                        if not asins:
                            cur.execute("INSERT OR REPLACE INTO search_cursors (slice_id, current_page, total_discovered, last_updated) VALUES (?, 0, 0, ?)", (slice_key, int(time.time())))
                            conn.commit()
                            continue

                        prods = self.fetch_product_batch(asins)
                        for p in prods:
                            deal = self.evaluate_product(p, sector_id)
                            if deal:
                                logger.info(f"💎 DEAL DISCOVERED: {deal['asin']} - {deal['brand']} ({deal['pack_count']}x) | {deal['vendor']} | Pool: ${deal['monthly_profit_pool']}/mo")
                                self.push_deal(deal)

                        cur.execute("INSERT OR REPLACE INTO search_cursors (slice_id, current_page, total_discovered, last_updated) VALUES (?, ?, ?, ?)", (slice_key, page + 1, len(asins), int(time.time())))
                        conn.commit()
                    except Exception as e:
                        logger.debug(f"Slice query error: {e}")

    def start(self):
        logger.info("Hunter Engine v20.12 Active — 13 Low-Return Root Categories + Multipack Loophole + 784-Brand Shield.")
        while True:
            try:
                self.run_sweep()
                time.sleep(10)
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.critical(f"Daemon fatal loop error: {e}")
                time.sleep(15)


if __name__ == "__main__":
    _lock = acquire_singleton_lock()
    hunter = ReverseAmazonHunter()
    hunter.start()
