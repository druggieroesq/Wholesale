
# ==============================================================================
# ACCESSORY MISMATCH & EXTREME ROI (>800%) FLOOR SHIELD
# ==============================================================================
ACCESSORY_TOKENS = {
    'bracket', 'screw', 'screws', 'bolt', 'bolts', 'gasket', 'seal', 
    'plate', 'cover', 'adapter', 'harness', 'hardware kit', 'mounting kit',
    'faceplate', 'strike', 'shim', 'clip', 'spring', 'spacer'
}

PRIMARY_EQUIPMENT_TOKENS = {
    'closer', 'motor', 'pump', 'actuator', 'compressor', 'tool', 'drill',
    'saw', 'sensor', 'detector', 'lockset', 'exit device', 'valve'
}

def is_accessory_mismatch(amz_title: str, dist_title: str, cogs: float, bb: float) -> tuple[bool, str]:
    cogs = float(cogs or 0.0)
    bb = float(bb or 0.0)
    
    # 1. Price Floor Ratio Check for items > 5
    if bb > 25.0 and cogs > 0:
        cost_ratio = cogs / bb
        if cost_ratio < 0.18:
            return True, f'SUB_FLOOR_COST_RATIO ({cost_ratio:.1%} < 18% floor | Potential accessory mismatch)'

    # 2. Equipment vs Accessory Token Check
    amz_t = str(amz_title).lower()
    dist_t = str(dist_title).lower()
    
    has_amz_equip = any(eq in amz_t for eq in PRIMARY_EQUIPMENT_TOKENS)
    has_dist_accessory = any(acc in dist_t for acc in ACCESSORY_TOKENS)
    has_amz_accessory = any(acc in amz_t for acc in ACCESSORY_TOKENS)
    
    if has_amz_equip and has_dist_accessory and not has_amz_accessory:
        return True, 'ACCESSORY_MISMATCH (Distributor title matches accessory token but Amazon listing is parent equipment)'
        
    return False, 'VALID'

#!/usr/bin/env python3
"""
tools/reverse_amazon_finder.py
------------------------------
Reverse Amazon Hunter & Autonomous Sourcing Daemon (v16.7 Continuous Velocity Master).
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
from contextlib import closing
from pathlib import Path
from typing import Dict, List, Optional, Any, Set
import requests
import pandas as pd
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
    MAX_DAYS_TO_FBA
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

DATA_DIR = ROOT_DIR / "data"
CATALOG_BASE_DIR = DATA_DIR / "catalogs"
DB_PATH = DATA_DIR / "hunter_engine.db"
PRIMARY_IP_FILE = DATA_DIR / "ip_blocklist.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)
CATALOG_BASE_DIR.mkdir(parents=True, exist_ok=True)

STRICT_MAX_WEIGHT_LBS = 20.0
STRICT_MIN_WHOLESALE_COST = 1.00
STRICT_MAX_WHOLESALE_COST = 65.00
MIN_REVIEW_RATING = 3.8
MAX_SEARCH_PAGE_DEPTH = 100

DATED_RE = re.compile(
    r"\b(calendar|planner|agenda|dated|desk pad calendar|wall calendar|"
    r"weekly planner|monthly planner|daily planner|academic year|fiscal year|"
    r"appointment book|schedule book|202[4-7])\b",
    re.IGNORECASE
)

SECTOR_DEFINITIONS: Dict[int, Dict[str, Any]] = {
    16310091: {"name": "Industrial & Scientific", "is_root": True, "ref_fee": 0.12},
    228013: {"name": "Tools & Home Improvement", "is_root": True, "ref_fee": 0.15},
    15684181: {"name": "Automotive & Commercial Fleet", "is_root": True, "ref_fee": 0.12},
    3760911: {"name": "Beauty & Personal Care", "is_root": True, "ref_fee": 0.15},
    318026011: {"name": "Commercial HVAC & Refrigeration", "is_root": False, "ref_fee": 0.12},
    318028011: {"name": "Hydraulics, Pneumatics & Plumbing", "is_root": False, "ref_fee": 0.12},
    2975317011: {"name": "Lab & Scientific Supplies", "is_root": False, "ref_fee": 0.12},
    383599011: {"name": "Commercial Fasteners", "is_root": False, "ref_fee": 0.12},
    318029011: {"name": "Commercial Power Transmission", "is_root": False, "ref_fee": 0.12},
    16310101: {"name": "Commercial Janitorial & Sanitation", "is_root": False, "ref_fee": 0.12},
    256225011: {"name": "Industrial Adhesives, Sealants & Lubricants", "is_root": False, "ref_fee": 0.12},
    318031011: {"name": "Occupational Health & Safety (PPE)", "is_root": False, "ref_fee": 0.12},
    318024011: {"name": "Commercial Electrical & Wiring", "is_root": False, "ref_fee": 0.12},
    16310151: {"name": "Material Handling & Packaging Supplies", "is_root": False, "ref_fee": 0.12}
}

SEARCH_SLICES = [
    {"price_min": 25, "price_max": 95, "bsr_min": 500, "bsr_max": 15000, "min_sellers": 0, "max_sellers": 4, "sort": "salesRankDrops30", "asc": False},
    {"price_min": 20, "price_max": 80, "bsr_min": 15001, "bsr_max": 35000, "min_sellers": 0, "max_sellers": 4, "sort": "salesRankDrops30", "asc": False},
    {"price_min": 18, "price_max": 60, "bsr_min": 35001, "bsr_max": 60000, "min_sellers": 0, "max_sellers": 3, "sort": "salesRankDrops30", "asc": False},
    {"price_min": 65, "price_max": 160, "bsr_min": 1000, "bsr_max": 40000, "min_sellers": 0, "max_sellers": 5, "sort": "salesRankDrops90", "asc": False}
]


def acquire_singleton_lock():
    try:
        lock_file = open(LOCK_FILE_PATH, "w")
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_file
    except (IOError, BlockingIOError):
        logger.warning("Duplicate process detected. Exiting.")
        sys.exit(0)


def clean_brand_name(brand: str) -> str:
    if not brand: return ""
    b = str(brand).upper().strip()
    b = re.sub(r'[^A-Z0-9\s]', ' ', b)
    b = re.sub(r'\b(LLC|INC|CORP|CORPORATION|CO|COMPANY|LTD|HOLDINGS|USA|ENTERPRISES)\b', '', b)
    return re.sub(r'\s+', ' ', b).strip()


def extract_root_mpn(mpn: str) -> str:
    if not mpn: return ""
    clean = re.sub(r'[^A-Z0-9]', '', str(mpn).upper().strip())
    return clean.lstrip('0') or clean


def normalize_barcode(code: str) -> str:
    clean = re.sub(r"[^0-9]", "", str(code or "").strip())
    if len(clean) in (12, 13, 14):
        return clean.zfill(14)
    return ""


def parse_pack_quantity(text: str, keepa_product: Optional[Dict[str, Any]] = None) -> int:
    if keepa_product:
        kp_qty = keepa_product.get("packageQuantity") or keepa_product.get("numberOfItems")
        if kp_qty and isinstance(kp_qty, int) and 1 < kp_qty <= 100:
            return kp_qty

    if not text:
        return 1

    t = text.lower().strip()
    m_pack = re.search(r'\b(?:pack|pk|box|case|set|bundle|lot)\s*(?:of|-|–|\s)\s*([0-9]{1,3})\b', t)
    if m_pack:
        val = int(m_pack.group(1))
        if 1 < val <= 100: return val

    m_hyphen = re.search(r'\b([0-9]{1,3})\s*(?:-|–|\s)?\s*(?:pack|pk|piece|pc|count|ct|bundle)\b', t)
    if m_hyphen:
        val = int(m_hyphen.group(1))
        if 1 < val <= 100: return val

    m_lead = re.search(r'\b([2-9]|1[0-2])\s*[xX]\s+[a-zA-Z0-9]', t)
    if m_lead:
        val = int(m_lead.group(1))
        if 1 < val <= 24: return val

    if re.search(r'\b(?:pair|two pack|2-pack|twin pack|double pack)\b', t):
        return 2

    return 1


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
                    status TEXT, drop_reason TEXT
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
            if not row: return True
            status = row["status"]
            elapsed = now - (row["last_evaluated"] or 0)
            if status in ("STRUCTURAL_REJECT", "POOR_REVIEWS", "OVERWEIGHT", "EXPENSIVE_BUY", "RESTRICTED_BRAND"):
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
                    is_hazmat, last_evaluated, status, drop_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    drop_reason = excluded.drop_reason
            """, (
                asin, data.get("title", ""), data.get("brand", ""), data.get("category_id", 0),
                data.get("upc", ""), data.get("ean", ""), data.get("mpn", ""), data.get("sales_rank", 0),
                data.get("drops_90", 0), data.get("monthly_sold", 0), data.get("avg90_bb", 0.0),
                data.get("current_bb", 0.0), data.get("weight_lbs", 0.0),
                data.get("referral_pct", 0.15), data.get("fba_fee", 4.15),
                data.get("num_sellers", 0), data.get("amazon_pack_qty", 1),
                1 if data.get("is_hazmat") else 0, now, status, drop_reason
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
                    buy_box_price = excluded.buy_box_price,
                    wholesale_cost = excluded.wholesale_cost,
                    net_profit = excluded.net_profit,
                    roi_percent = excluded.roi_percent,
                    monthly_profit_pool = excluded.monthly_profit_pool,
                    suggested_order_size = excluded.suggested_order_size,
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
        logger.info("Indexing catalog barcodes & Brand-Coupled MPNs...")
        self.catalog_barcodes.clear()
        self.catalog_brand_mpns.clear()
        files = []
        for p in [CATALOG_BASE_DIR / "manufacturer_catalogs" / "indexed", CATALOG_BASE_DIR / "wholesale_catalogs" / "indexed", CATALOG_BASE_DIR]:
            if p.exists():
                for ext in ("*.parquet", "*.csv"):
                    for f in p.glob(ext):
                        tier = "MANUFACTURER" if "manufacturer" in str(f).lower() else "WHOLESALE"
                        files.append((f, tier))

        for cat_file, tier in files:
            vendor_name = cat_file.stem.replace("_Catalog", "").replace("_normalized", "").upper()
            try:
                df = pd.read_parquet(cat_file) if cat_file.suffix == ".parquet" else pd.read_csv(cat_file, low_memory=False, dtype=str)
                cols = {str(c).lower().strip(): c for c in df.columns}
                upc_col = cols.get("gtin14") or cols.get("upc") or cols.get("barcode")
                sku_col = cols.get("clean_mpn") or cols.get("sku") or cols.get("part_number") or cols.get("item_number")
                cost_col = cols.get("unit_cost") or cols.get("wholesale_cost") or cols.get("cost")
                brand_col = cols.get("brand") or cols.get("manufacturer")

                if not cost_col: continue

                for _, row in df.iterrows():
                    raw_val = row.get(cost_col)
                    if pd.isna(raw_val): continue
                    try: cost = float(str(raw_val).replace("$", "").replace(",", "").strip())
                    except ValueError: continue
                    if cost <= 0: continue

                    brand_clean = clean_brand_name(str(row.get(brand_col, vendor_name)))
                    entry = {"vendor": vendor_name, "tier": tier, "unit_cost": cost, "brand": brand_clean}

                    if upc_col and pd.notna(row.get(upc_col)):
                        norm = normalize_barcode(row[upc_col])
                        if norm: self.catalog_barcodes[norm] = entry

                    sku_raw = str(row.get(sku_col, "")).strip().upper()
                    root_val = extract_root_mpn(sku_raw)
                    if root_val and brand_clean:
                        self.catalog_brand_mpns[f"{brand_clean}:{root_val}"] = entry

            except Exception as e:
                logger.error(f"Catalog ingest error on {cat_file.name}: {e}")

        logger.info(f"Strict Index ready: {len(self.catalog_barcodes):,} barcodes | {len(self.catalog_brand_mpns):,} brand-coupled MPNs.")

    def match(self, codes: List[str], brand: str, part_num: str) -> Optional[Dict[str, Any]]:
        # GUARD: Reject uncoupled short numeric MPNs (e.g. '8511') without brand match
        if part_num and str(part_num).strip().isdigit() and len(str(part_num).strip()) <= 5:
            # Allow only if we have high-confidence barcode or brand identity
            if not codes and not brand:
                return None
        for c in codes:
            norm = normalize_barcode(c)
            if norm and norm in self.catalog_barcodes:
                return self.catalog_barcodes[norm]

        b = clean_brand_name(brand)
        root_p = extract_root_mpn(part_num)
        if b and root_p:
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
        self._load_blocklist()

    def _load_blocklist(self):
        if PRIMARY_IP_FILE.exists():
            try:
                raw = json.loads(PRIMARY_IP_FILE.read_text(encoding="utf-8"))
                brand_list = raw if isinstance(raw, list) else raw.get("brands", [])
                self.blocked_brands = {clean_brand_name(b) for b in brand_list if b}
                logger.info(f"Loaded {len(self.blocked_brands)} restricted brands into hunter pre-filter.")
            except Exception as e:
                logger.error(f"Failed loading IP blocklist: {e}")

    def is_brand_blocked(self, brand: str) -> bool:
        clean = clean_brand_name(brand)
        if not clean: return True
        return clean in self.blocked_brands

    def evaluate_product(self, product: Dict[str, Any], sector_id: int) -> Optional[Dict[str, Any]]:
        # GUARD: 22% Price Floor & Short MPN Collision Shield
        try:
            _bb = float(product.get('current_bb') or product.get('buy_box') or product.get('price') or 0)
            _cost = float(product.get('wholesale_cost') or product.get('cost') or 0)
            _mpn = str(product.get('mpn') or product.get('part_number') or '').strip()
            if _bb > 25.0 and _cost > 0 and (_cost / _bb) < 0.22:
                return None
            if _mpn.isdigit() and len(_mpn) <= 5 and _bb > 0 and (_cost / _bb) < 0.25:
                return None
        except Exception:
            pass
        asin = product.get("asin", "")
        title = product.get("title") or ""
        brand = (product.get("brand") or "").strip()
        is_hazmat = bool(product.get("isHazMat", False) or product.get("dangerousGoods", False))

        if not brand or self.is_brand_blocked(brand):
            self.db.cache_candidate(asin, {"title": title, "brand": brand}, "RESTRICTED_BRAND", f"Brand '{brand}' on IP Blocklist")
            return None

        if DATED_RE.search(title):
            return None

        stats = product.get("stats") or {}
        current_prices = stats.get("current") or []
        rating_val = current_prices[16] / 10.0 if len(current_prices) > 16 and current_prices[16] and current_prices[16] > 0 else 4.0
        if rating_val < MIN_REVIEW_RATING:
            return None

        keepa_monthly_sold = product.get("monthlySold") or 0
        drops_90 = stats.get("salesRankDrops90") or 0
        drops_30 = stats.get("salesRankDrops30") or 0
        current_bsr = current_prices[3] if len(current_prices) > 3 and current_prices[3] and current_prices[3] > 0 else (stats.get("avg90") or [0,0,0,0])[3]

        monthly_units = calculate_continuous_velocity(
            sales_rank=current_bsr,
            drops_30=drops_30,
            drops_90=drops_90,
            monthly_sold=keepa_monthly_sold
        )

        if monthly_units < 30:
            return None

        pkg_weight_g = product.get("packageWeight") or 0
        weight_lbs = round(pkg_weight_g / 453.592, 2)
        if weight_lbs > STRICT_MAX_WEIGHT_LBS:
            return None

        avg90_prices = stats.get("avg90") or []
        effective_bb_cents = avg90_prices[18] if len(avg90_prices) > 18 and avg90_prices[18] and avg90_prices[18] > 0 else current_prices[18]
        if not effective_bb_cents or effective_bb_cents <= 0: return None
        buy_box_price = round(effective_bb_cents / 100.0, 2)
        if buy_box_price < 13.50: return None

        fba_stat = current_prices[11] if len(current_prices) > 11 and current_prices[11] is not None and current_prices[11] >= 0 else 0
        offers = product.get("offers") or []
        offers_fba = sum(1 for o in offers if o.get("isFBA") and o.get("condition") == 1)
        competitive_fba = max(fba_stat, offers_fba)

        upc_list = product.get("upcList") or []
        ean_list = product.get("eanList") or []
        codes = list(set([str(c) for c in (upc_list + ean_list) if c]))
        primary_upc = codes[0] if codes else ""
        part_num = product.get("model") or product.get("partNumber") or ""

        amazon_pack_qty = parse_pack_quantity(title, product)

        days_to_fba = estimate_days_to_fba(brand, is_hazmat=is_hazmat, pack_qty=amazon_pack_qty)
        if days_to_fba > MAX_DAYS_TO_FBA:
            self.db.cache_candidate(asin, {"title": title, "brand": brand}, "FILTERED", f"Days to FBA ({days_to_fba}d) exceeds {MAX_DAYS_TO_FBA}d ceiling")
            return None

        base_meta = {
            "title": title, "brand": brand, "category_id": sector_id,
            "upc": primary_upc, "is_hazmat": is_hazmat, "monthly_sold": monthly_units,
            "drops_90": drops_90, "avg90_bb": buy_box_price, "num_sellers": competitive_fba,
            "amazon_pack_qty": amazon_pack_qty
        }

        is_valid_comp, comp_reason, effective_share_velocity = validate_fba_competition(monthly_units, competitive_fba)
        if not is_valid_comp:
            self.db.cache_candidate(asin, base_meta, "EXCESSIVE_COMPETITION", comp_reason)
            return None

        profit_floor, req_roi = calculate_sliding_profit_floor(effective_share_velocity)
        cat_match = self.catalog.match(codes=codes, brand=brand, part_num=part_num)

        if cat_match:
            wholesale_cost = round(cat_match["unit_cost"] * amazon_pack_qty, 2)
            vendor = cat_match["vendor"]
            confidence_tier = "VERIFIED_WHOLESALE"
        else:
            wholesale_cost = round(buy_box_price * 0.40, 2)
            vendor = f"Target Brand: {brand}"
            confidence_tier = "TARGET_ACQUISITION"

        if wholesale_cost < STRICT_MIN_WHOLESALE_COST or wholesale_cost > STRICT_MAX_WHOLESALE_COST:
            self.db.cache_candidate(asin, base_meta, "FILTERED", f"Cost ${wholesale_cost} out of bounds")
            return None

        logistics = get_logistics_profile(vendor)
        multipack_prep_buffer = 0.45 if amazon_pack_qty > 1 else 0.00
        prep_base = logistics["prep_base"] + (0.60 if is_hazmat else 0.0) + multipack_prep_buffer
        inbound_freight = round(weight_lbs * (logistics["freight_per_lb"] + (0.20 if is_hazmat else 0.0)), 2)
        total_prep = round(prep_base + inbound_freight + 0.27, 2)

        s_meta = SECTOR_DEFINITIONS.get(sector_id, {"ref_fee": 0.12, "name": "Commercial MRO"})
        referral_fee = round(buy_box_price * s_meta["ref_fee"], 2)
        fba_fee = round(3.86 + (weight_lbs * 0.45), 2)
        total_overhead = referral_fee + fba_fee + total_prep + round(buy_box_price * 0.03, 2)

        net_profit = round(buy_box_price - (wholesale_cost + total_overhead), 2)
        landed_cost = wholesale_cost + total_prep
        roi_percent = round((net_profit / landed_cost) * 100.0, 1) if landed_cost > 0 else 0.0

        if net_profit < profit_floor or roi_percent < req_roi:
            self.db.cache_candidate(asin, base_meta, "FILTERED", f"Net ${net_profit:.2f} < ${profit_floor:.2f} or ROI {roi_percent}% < {req_roi}%")
            return None

        min_pool_required = get_dynamic_profit_hurdle(monthly_units)
        monthly_profit_pool = round(effective_share_velocity * net_profit, 2)
        if monthly_profit_pool < min_pool_required:
            self.db.cache_candidate(asin, base_meta, "FILTERED", f"Pool ${monthly_profit_pool:.2f} < ${min_pool_required:.2f}/mo requirement")
            return None

        suggested_order = max(12, int(effective_share_velocity * 0.5))
        return {
            "asin": asin, "title": title, "brand": brand,
            "vendor": vendor, "prep_hub": logistics["facility"],
            "confidence_tier": confidence_tier, "sales_rank": current_bsr,
            "buy_box_price": buy_box_price, "wholesale_cost": wholesale_cost,
            "max_allowable_buy_cost": round(buy_box_price - total_overhead - profit_floor, 2),
            "net_profit": net_profit, "roi_percent": roi_percent, "profit_floor": profit_floor,
            "req_roi": req_roi, "profit_per_dollar": round(net_profit / max(0.01, wholesale_cost), 2),
            "suggested_order_size": suggested_order, "expected_order_profit": round(net_profit * suggested_order, 2),
            "monthly_profit_pool": monthly_profit_pool,
            "monthly_units": monthly_units, "our_share_velocity": int(effective_share_velocity),
            "competitive_fba": competitive_fba, "drops_90": drops_90, "pack_count": amazon_pack_qty,
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
            vacant_tag = "🏆 <b>0 FBA SELLERS (100% BUY BOX)</b>\n" if deal["competitive_fba"] == 0 else ""

            msg = (
                f"💎 <b>CONTINUOUS VELOCITY DEAL</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"{vacant_tag}"
                f"📦 <a href=\"{deal['order_url']}\">{title_esc}</a>\n"
                f"🏷 <b>Brand:</b> <code>{brand_esc}</code> | <b>ASIN:</b> <code>{deal['asin']}</code>\n"
                f"🏢 <b>Vendor:</b> {vendor_esc} ({deal['prep_hub']})\n"
                f"⚡ {hazmat_tag}<b>Pack:</b> {deal['pack_count']}x | <b>Days:</b> {deal['days_to_fba']}d | <b>Weight:</b> {deal['weight_lbs']} lbs\n"
                f"📈 <b>True Velocity:</b> <b>{deal['monthly_units']} u/mo</b> | <b>Share:</b> {deal['our_share_velocity']} u/mo\n"
                f"👥 <b>FBA Sellers:</b> {deal['competitive_fba']} | <b>BSR:</b> #{deal['sales_rank']:,}\n"
                f"💰 <b>Buy Box:</b> ${deal['buy_box_price']:.2f} | <b>Cost:</b> ${deal['wholesale_cost']:.2f}\n"
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
                    "vendor": deal["vendor"],
                    "prep_center": deal["prep_hub"],
                    "sales_rank": deal["sales_rank"],
                    "monthly_velocity": deal["monthly_units"],
                    "competitive_fba": deal["competitive_fba"],
                    "our_share_velocity": deal["our_share_velocity"],
                    "pack_count": deal["pack_count"],
                    "days_to_fba": deal["days_to_fba"],
                    "buy_box_price": deal["buy_box_price"],
                    "wholesale_cost": deal["wholesale_cost"],
                    "net_profit": deal["net_profit"],
                    "roi_percent": deal["roi_percent"],
                    "suggested_order_size": deal["suggested_order_size"],
                    "monthly_profit_pool": deal["monthly_profit_pool"],
                    "is_hazmat": "YES" if deal.get("is_hazmat") else "NO",
                    "order_url": deal["order_url"],
                    "seller_check_url": deal["seller_check_url"],
                    "seller_approval_url": deal["seller_approval_url"],
                    "notes": f"Pool: ${deal['monthly_profit_pool']:.2f}/mo | FBA: {deal['competitive_fba']} | Share: {deal['our_share_velocity']}u/mo | Days: {deal['days_to_fba']}d",
                    "status": "Ready for Order"
                }
                requests.post(webhook_url, json=payload, timeout=15, allow_redirects=True)
            except Exception as e:
                logger.debug(f"Sheet dispatch error: {e}")

    def fetch_product_batch(self, asins: List[str]) -> List[Dict[str, Any]]:
        clean = [a.strip().upper() for a in asins if a and len(a.strip()) == 10 and self.db.should_evaluate(a.strip())]
        if not clean: return []
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
        logger.info("Executing continuous velocity sweep across active commercial slices...")
        if not KEEPA_API_KEY:
            logger.warning("No KEEPA_API_KEY configured.")
            return

        with closing(self.db.get_connection()) as conn:
            cur = conn.cursor()
            for sector_id in list(SECTOR_DEFINITIONS.keys()):
                for slice_idx, slice_cfg in enumerate(SEARCH_SLICES):
                    slice_key = f"{sector_id}:{slice_idx}"
                    cur.execute("SELECT current_page FROM search_cursors WHERE slice_id = ?", (slice_key,))
                    row = cur.fetchone()
                    page = row["current_page"] if row else 0

                    if page >= MAX_SEARCH_PAGE_DEPTH:
                        page = 0

                    selection = {
                        "current_COUNT_NEW_gte": slice_cfg["min_sellers"],
                        "current_COUNT_NEW_lte": slice_cfg["max_sellers"],
                        "buyBoxIsAmazon": False,
                        "current_BUY_BOX_SHIPPING_gte": slice_cfg["price_min"] * 100,
                        "current_BUY_BOX_SHIPPING_lte": slice_cfg["price_max"] * 100,
                        "current_SALES_gte": slice_cfg["bsr_min"],
                        "current_SALES_lte": slice_cfg["bsr_max"],
                        "sort": [[slice_cfg["sort"], "asc" if slice_cfg["asc"] else "desc"]],
                        "perPage": 100,
                        "page": page
                    }

                    s_def = SECTOR_DEFINITIONS.get(sector_id, {"is_root": True})
                    if s_def["is_root"]: selection["rootCategory"] = [sector_id]
                    else: selection["categories_include"] = [sector_id]

                    try:
                        res = requests.post("https://api.keepa.com/query", data={"key": KEEPA_API_KEY, "domain": 1, "selection": json.dumps(selection)}, timeout=30)
                        if res.status_code != 200: continue
                        asins = res.json().get("asinList", [])

                        if not asins:
                            cur.execute("INSERT OR REPLACE INTO search_cursors (slice_id, current_page, total_discovered, last_updated) VALUES (?, 0, 0, ?)", (slice_key, int(time.time())))
                            conn.commit()
                            continue

                        prods = self.fetch_product_batch(asins)
                        for p in prods:
                            deal = self.evaluate_product(p, sector_id)
                            if deal:
                                logger.info(f"💎 DEAL DISCOVERED: {deal['asin']} - {deal['brand']} | True Units: {deal['monthly_units']} u/mo | Pool: ${deal['monthly_profit_pool']}/mo")
                                self.push_deal(deal)

                        cur.execute("INSERT OR REPLACE INTO search_cursors (slice_id, current_page, total_discovered, last_updated) VALUES (?, ?, ?, ?)", (slice_key, page + 1, len(asins), int(time.time())))
                        conn.commit()
                    except Exception as e:
                        logger.debug(f"Slice query error: {e}")

    def start(self):
        logger.info("Hunter Engine v16.7 Active with Continuous Velocity Modeling.")
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


def apply_cost_certainty_penalty(raw_score: float, status: str, has_verified_distributor_sku: bool) -> float:
    """
    Enforces scoring integrity based on wholesale cost certainty:
    - Verified distributor catalog SKU (SCOUTED_DEAL / VERIFIED_CATALOG): 1.0x (Score 80-100)
    - Unlinked / reverse estimate (NO_CATALOG_MATCH): Hard capped at 25.0
    - Filtered competitive saturation: Capped at 10.0
    - Structural rejects: 0.0
    """
    if status == 'STRUCTURAL_REJECT':
        return 0.0
    if status == 'FILTERED':
        return min(float(raw_score) * 0.10, 10.0)
    if not has_verified_distributor_sku or status == 'NO_CATALOG_MATCH':
        # Hard ceiling at 25 for any estimated COGS
        return min(float(raw_score) * 0.25, 25.0)
    return float(raw_score)
