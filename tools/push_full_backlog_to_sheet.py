#!/usr/bin/env python3
"""
tools/push_full_backlog_to_sheet.py (v2.5 — Permanent Backlog Recovery + Multipack Loophole + 784-Brand Shield)
-------------------------------------------------------------------------------------------------------------
Mirrors Code.gs v20.12 end-to-end when streaming leads from data/hunter_engine.db:
  1. Recovers and evaluates ALL valid candidate_cache records (including UNMAPPED_CANDIDATE and
     non-manufacturer 0-FBA rows) so the pipeline never drops to 34 rows.
  2. Tier 1A — Verified Distributor Multipack Loophole (Pack >= 2):
       - Min Buy Box: >= $13.50 | Min Net/Unit: >= $2.25 | Min Monthly Pool: >= $180/mo | Min Vel: >= 25 u/mo
  3. Tier 1B — Verified Distributor Single-Unit (Pack == 1):
       - Min Buy Box: >= $13.50 | Min Net/Unit: >= $3.00 | Min Monthly Pool: >= $250/mo | Min Vel: >= 25 u/mo
  4. Tier 2 — Unmapped Keepa Candidates (Needs Catalog Match):
       - Min Buy Box: >= $13.50 | Min Net/Unit: >= $5.00 | Min Monthly Pool: >= $500/mo | Min Vel: >= 60 u/mo
  5. Full 784-Brand Shield + exact 'PRO' / 'GENERIC' block.
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

VERIFIED_DISTRIBUTOR_KEYWORDS = (
    "ZORO", "GRAINGER", "MOTION", "BCI", "EE DISTRIBUTION", "CONTOUR",
    "FRONTIER", "ATLANTIC SPICE", "JB TOOLS", "PETEDGE", "GLOBAL INDUSTRIAL",
    "MARLO BEAUTY", "MSC DIRECT", "MIDLAND METAL"
)

EXACT_GENERIC_BRANDS: Set[str] = {
    "PRO", "GENERIC", "UNKNOWN", "UNBRANDED", "N/A", "NONE", "OEM"
}

EMBEDDED_784_BLOCKED_BRANDS: Set[str] = {
    "365 BY WHOLE FOODS", "3M", "ABUS", "ACANA", "ACDELCO", "ADAFRUIT", "ADDLON",
    "ADIDAS", "ADVANTAGE", "ADVANTAGE II", "ADVANTIX", "ADVIL", "AFINA", "AIR WICK",
    "ALEXA", "ALITOVE", "ALL-CLAD", "ALLEN", "ALTERNA", "AMAZON", "AMAZON AWARE",
    "AMAZON BASICS", "AMAZON COLLECTION", "AMAZON COMMERCIAL", "AMAZON ELEMENTS",
    "AMAZON ESSENTIALS", "AMAZON FRESH", "AMAZONBASICS", "AMICO", "AMIKA",
    "ANBOSON", "ANDIS", "ANKER", "ANTARCTIC STAR", "ANYCUBIC", "AOBEN", "AOLEMI",
    "APEX TOOL GROUP", "APLENTY", "APOQUEL", "APPLE", "AQARA", "AQUAHOMEGROUP",
    "ARANA", "ARCCAPTAIN", "ARDUINO", "ARLO", "ARM & HAMMER", "ARM AND HAMMER",
    "ARM HAMMER", "ARMANI BEAUTY", "ARMOR ALL", "ASTROAI", "AUDIBLE", "AUGUST",
    "AUKEY", "AUSSIE", "AVEDA", "AVEENO", "AXE", "BABYBJORN", "BABYLISSPRO",
    "BAG BALM", "BALL", "BAMBU LAB", "BANG", "BARBIE", "BARILLA", "BASEUS",
    "BASIC CARE", "BAYER", "BDELI", "BEATS", "BEETLES GEL POLISH", "BELKIN",
    "BENADRYL", "BENCHMADE", "BENEBONE", "BENEFIT", "BEYBLADE", "BIG AGNES",
    "BIG GREEN EGG", "BIGELOW", "BIL JAC", "BIOLAGE", "BIRKENSTOCK", "BISSELL",
    "BLACK & DECKER", "BLACK AND DECKER", "BLACK DIAMOND", "BLACK FLAG",
    "BLACKSTONE", "BLENDTEC", "BLINK", "BLUE BUFFALO", "BLUETTI",
    "BOEHRINGER INGELHEIM", "BONIDE", "BOPPY", "BOSCH", "BOSE", "BOSTITCH",
    "BOUDREAUXS", "BOUNTY", "BOWFLEX", "BRAUN", "BRAVECTO", "BREVILLE", "BRIOGEO",
    "BRITA", "BUCK KNIVES", "BULINA", "BUMBLE AND BUMBLE", "BURT'S BEES",
    "BURTS BEES", "BUTTONED DOWN", "CADET", "CALPHALON", "CAMELBAK", "CAMP CHEF",
    "CANON", "CANTU", "CAPSTAR", "CAR PASS", "CARE BEARS", "CARHARTT", "CARTERS",
    "CASCADE", "CASETIFY", "CASIO", "CASTROL", "CELESTIAL SEASONINGS", "CELLUCOR",
    "CELSIUS", "CENTRUM", "CERAVE", "CETAPHIL", "CHANNELLOCK", "CHARMIN",
    "CHEMICAL GUYS", "CHICCO", "CHUCKIT", "CLARITIN", "CLIF", "CLINIQUE",
    "CLOROX", "COACH", "COCOMELON", "COLEMAN", "COLGATE", "COLOR WOW", "COLUMBIA",
    "COMFORTIS", "COMMAND", "CONAIR", "CONTIGO", "CONVERSE", "CORE 10", "CORELLE",
    "CORSAIR", "COSEQUIN", "COVERGIRL", "CRAFTSMAN", "CRAYOLA", "CREALITY",
    "CREDELIO", "CRESCENT", "CREST", "CROCK-POT", "CROCS", "CRUCIAL", "CUISINART",
    "DAILY RITUAL", "DASUQUIN", "DAWN", "DEATH WISH", "DEEPLEE", "DEEPN",
    "DEGREE", "DEKOPRO", "DENSO", "DEPSTECH", "DERMALOGICA", "DETROIT AXLE",
    "DEWALT", "DEWALT INDUSTRIAL", "DIABLO", "DISNEY", "DJI", "DOORES", "DOVE",
    "DOWNY", "DR DENNIS GROSS", "DR. BROWNS", "DREMEL", "DRIFT", "DRUNK ELEPHANT",
    "DRYBAR", "DUCK TAPE", "DUNKIN", "DURAMIC 3D", "DURO", "DYMATIZE", "DYMO",
    "DYSON", "E.L.F.", "ECHO", "ECOFLOW", "EERO", "EGO POWER", "ELANCO", "ELEGOO",
    "ELEMIS", "ELMERS", "ENFAMIL", "ERGOBABY", "ERYONE", "ESTEE LAUDER",
    "ESTWING", "ESUN", "ETEKCITY", "EUCERIN", "EUFY", "EUFY SECURITY", "EUKANUBA",
    "EUREKA", "EZVALO", "FANTTIK", "FEBREZE", "FENTY BEAUTY", "FESTOOL", "FINISH",
    "FIRE TV", "FIRST ALERT", "FISHER-PRICE", "FITBIT", "FLASHFORGE", "FLEX SEAL",
    "FLIR", "FLUKE", "FMHOTU", "FOLGERS", "FOODSAVER", "FORIOUS", "FOSSIL",
    "FOXWELL", "FRAM", "FRESH ROASTED COFFEE", "FRESH STEP", "FRIDA BABY",
    "FRIDABABY", "FRISKIES", "FRONTGUARD", "FRONTLINE", "FUJIFILM", "FULL SHINE",
    "FURMINATOR", "G-SHOCK", "GAIAM", "GAIN", "GAMES WORKSHOP", "GARDEN OF LIFE",
    "GARMIN", "GARNIER", "GATORADE", "GEARWRENCH", "GENERAL MILLS", "GENERIC",
    "GERBER", "GHD", "GHOST", "GILLETTE", "GLAD", "GLADE", "GOAL ZERO",
    "GOLD BOND", "GOO GONE", "GOO GOO", "GOODTHREADS", "GOOGLE", "GOOLOO",
    "GOPRO", "GORILLA GRIP", "GOVEE", "GRACO", "GREENIES", "GREENLEE",
    "GREENWORKS", "GREGORY", "GUCCI", "HAKKO", "HAMILTON BEACH", "HANOTIC", "HAPE",
    "HAPPY BABY", "HAPPY BELLY", "HARMAN KARDON", "HARTZ", "HATCHBOX",
    "HATCHIMALS", "HAUSSE", "HEAD & SHOULDERS", "HEARTGARD", "HEFTY", "HEINZ",
    "HENCKELS", "HERBAL ESSENCES", "HIBBENT", "HIKOKI", "HILLS SCIENCE DIET",
    "HILTI", "HOKA", "HONEYWELL", "HOOVER", "HOT WHEELS", "HUGGIES", "HUSKY",
    "HUSQVARNA", "HYDRO FLASK", "HYDROXYCUT", "HYPERICE", "HYPERX", "IAMS",
    "ICARCOVER", "IGK", "ILLY", "INSTA360", "INSTANT POT", "IROBOT", "IRWIN",
    "IRWIN TOOLS", "IT'S A 10", "ITRUSOU", "ITS A 10", "IXIRPRO", "JABRA",
    "JACKERY", "JACKSON PALMER", "JAKE SALES", "JBL", "JELLYCAT", "JENGA",
    "JESVERTY", "JETBOIL", "JOBLIKEBREZ", "JOHN PAUL MITCHELL", "JOHNSONS", "K&N",
    "K18", "K9 ADVANTIX", "KAMADO JOE", "KELLOGGS", "KELTY", "KERASTASE",
    "KERSHAW", "KEURIG", "KEVIN MURPHY", "KEYSIGHT", "KIDDE", "KIEHLS", "KINDLE",
    "KINGSTON", "KITCHENAID", "KKBESTPACK", "KLEIN", "KLEIN TOOLS", "KNIPEX",
    "KOBALT", "KODAK", "KONAMI", "KONG", "KONG COMPANY", "KOOTETA", "KRAFT",
    "KWIKSET", "L'OREAL", "L'OREAL PARIS", "LA ROCHE-POSAY", "LANCOME", "LARABAR",
    "LARK & RO", "LASKO", "LAURA GELLER", "LAURA GELLER NEW YORK", "LAVAZZA",
    "LE CREUSET", "LEAPFROG", "LEATHERMAN", "LEGO", "LENOX", "LEVIS", "LIFEPROOF",
    "LIFESTRAW", "LIONMAX", "LIQUID I.V.", "LIQUID NAILS", "LISTERINE",
    "LIVING PROOF", "LODGE", "LOGITECH", "LOREAL", "LOREAL PARIS", "LOREX",
    "LOUIS VUITTON", "LULULEMON", "LUTRON", "LYSOL", "MAC COSMETICS",
    "MAGIC: THE GATHERING", "MAKITA", "MAMA BEAR", "MANDUKA", "MARMOT",
    "MARSHALL", "MARVEL", "MASTER LOCK", "MATRIX", "MAXTITE", "MAXWELL HOUSE",
    "MAYBELLINE", "MEDELA", "MEDIX 5.5", "MEGA BLOKS", "MEGUIARS", "MEITE",
    "MELISSA & DOUG", "MEOW MIX", "MERRICK", "META QUEST", "METABO", "METHOD",
    "MICHAEL KORS", "MICHAEL TODD", "MICHELIN", "MICOMLAN", "MIGHTY FASTENERS",
    "MILK-BONE", "MILWAUKEE", "MIRACLE-GRO", "MOBIL 1", "MOLESKINE", "MONOPOLY",
    "MONSTER", "MOON AND BACK", "MOPAR", "MOPHIE", "MOROCCANOIL", "MOTORCRAFT",
    "MOTRIN", "MR. CLEAN", "MRS MEYERS", "MRS. MEYERS", "MUCINEX", "MUGEASY",
    "MUNCHKIN", "MUSCLETECH", "MUZOJET", "NAACOO", "NALGENE", "NANIT", "NARS",
    "NATURE MADE", "NATURE VALLEY", "NERF", "NESPRESSO", "NEST", "NESTLE",
    "NETGEAR", "NEUTROGENA", "NEW BALANCE", "NEXGARD", "NGK", "NICHOLSON", "NIKE",
    "NIKON", "NINJA", "NINTENDO", "NIOXIN", "NIVEA", "NORDICTRACK",
    "NOT YOUR MOTHERS", "NOVEX", "NUK", "NUTRAFOL", "NUTRAMAX", "NUTRO",
    "NYLABONE", "NYX", "OAKLEY", "OBAGI MEDICAL", "OFF!", "OLAPLEX", "OLAY",
    "OLD SPICE", "OLIGHT", "ONE A DAY", "OONI", "OPI", "OPTIMUM NUTRITION",
    "ORAL-B", "ORIBE", "ORIJEN", "ORTHO", "OSLTWI", "OSPREY", "OSTER",
    "OTTER PRODUCTS", "OTTERBOX", "OUAI", "OURA", "OVERTURE", "OWALA", "OWLET",
    "OXICLEAN", "OXILAM", "OXO", "PAMPERS", "PANTENE", "PAPER MATE", "PARTICLE",
    "PATAGONIA", "PAUL MITCHELL", "PAULAS CHOICE", "PAW PATROL", "PEDIALYTE",
    "PEDIGREE", "PEETS", "PELOTON", "PEPTO-BISMOL", "PERSIL", "PESRAE", "PETSAFE",
    "PETZL", "PHILIPS", "PHILIPS AVENT", "PHILIPS HUE", "PHILIPS SONICARE",
    "PINZON", "PLAY-DOH", "PLAYMOBIL", "PLAYSTATION", "PLEDGE", "POKEMON",
    "POLAROID", "POLYMAKER", "POPSOCKETS", "PORTER CABLE", "PORTER-CABLE",
    "POST-IT", "POWGRN", "PRESTO", "PRESTO!", "PRESTONE", "PRIME HYDRATION",
    "PROBRICO", "PROTO", "PRUSA", "PUMA", "PUREOLOGY", "PURINA", "PYREX", "QENOVO",
    "QUEST", "R+CO", "RADIANS", "RADIO SYSTEMS", "RAID", "RAIN-X",
    "RASPBERRY PI", "RAVENSBURGER", "RAY-BAN", "RAZER", "RED BULL", "REDKEN",
    "REEBOK", "REMINGTON", "REOLINK", "RESMED", "REVLON", "REVOLUTION", "REYNOLDS",
    "RHINO USA", "RIDGID", "RIGOL", "RING", "RIVET", "ROBITUSSIN", "ROBLOX",
    "ROBOROCK", "ROCKWELL", "ROKU", "ROLEX", "ROOMBA", "ROSSETTA", "ROUNDUP",
    "ROYAL CANIN", "RUBBERMAID", "RXBAR", "RYOBI", "SAMSUNG", "SANDISK",
    "SAWSTOP", "SAWYER", "SCHICK", "SCHLAGE", "SCHNEIDER ELECTRIC", "SCHWARZKOPF",
    "SCIENCE DIET", "SCOTCH", "SCOTCHGARD", "SCOTTS", "SEAGATE", "SEBASTIAN",
    "SECRET", "SEIKO", "SELTERM", "SENNHEISER", "SENSODYNE", "SERESTO",
    "SESAME STREET", "SEVENTH GENERATION", "SG TZH", "SHARK", "SHARPIE",
    "SHARPIE PRO", "SHEA MOISTURE", "SHEBA", "SHISEIDO", "SHOUT! FACTORY",
    "SIGLENT", "SIGNIFY", "SIMILAC", "SIMPARICA", "SIMPLE MODERN", "SIMPLISAFE",
    "SIRAYA TECH", "SK-II", "SKECHERS", "SKIL", "SKINCEUTICALS", "SKIP HOP",
    "SKULL SHAVER", "SKULLCANDY", "SNAP ON", "SNAP-ON", "SODASTREAM",
    "SOL DE JANEIRO", "SOLIMO", "SOLO STOVE", "SONOS", "SONY", "SOPOBY",
    "SPARKFUN", "SPIGEN", "SPOTTED ZEBRA", "SPRAYWAY", "SPYDERCO", "SQUARE D",
    "SQUISHMALLOWS", "STAEDTLER", "STAHLWILLE", "STANLEY", "STANLEY BLACK & DECKER",
    "STAR WARS", "STARBUCKS", "STEELSERIES", "STIHL", "STONE & BEAM", "STTORABOKS",
    "STYLE EDIT", "SUMMIT HYDRAULICS", "SUNBEAM", "SUNLU", "SWANN", "SWIFFER",
    "TAPIX", "TARTE", "TASTE OF THE WILD", "TATCHA", "TECBEARS", "TEKTRONIX",
    "TEMPTATIONS", "THE AMAZING WHIP-IT", "THE NORTH FACE", "THE ORDINARY",
    "THERABODY", "THERAGUN", "THERM-A-REST", "THERMOS", "THULE", "TIDE",
    "TIDY CATS", "TILE", "TIMEX", "TINECO", "TOMMEE TIPPEE", "TOMS OF MAINE",
    "TONKA", "TOO FACED", "TOPDON", "TORO", "TOTOLY JAKE", "TP-LINK", "TPOB",
    "TRAEGER", "TRANSFORMERS", "TRESEMME", "TRUESEAMOSS", "TUPPERWARE",
    "TURTLE WAX", "TWININGS", "TYLENOL", "U.S. SOLID", "UBIQUITI", "UGG", "UGREEN",
    "ULTIMATE EARS", "UNDER ARMOUR", "UNI-T", "UNIVERSAL MUSIC GROUP",
    "URBAN DECAY", "VALVOLINE", "VANS", "VASELINE", "VEKEN", "VENUS", "VEVOR",
    "VICKS", "VICTORINOX", "VIRBAC", "VISE-GRIP", "VITAMIX", "VORNADO", "VOXELAB",
    "VTECH", "WAG", "WAHL", "WARHAMMER", "WATERDROP", "WATERPIK", "WAVYTALK",
    "WEATHERTECH", "WEBER", "WEBER-STEPHEN", "WEDDELL", "WELLA", "WELLER",
    "WELLNESS", "WENNALIFE", "WENSILON", "WERA", "WESTERN DIGITAL", "WHIP-IT",
    "WHISKAS", "WHOLE FOODS MARKET", "WHOOP", "WICKEDLY PRIME", "WIHA", "WILLHI",
    "WINDEX", "WIZARDS OF THE COAST", "WOD TAPE", "WONDER BOUND", "WOOLITE", "WORX",
    "WUSTHOF", "WYZE", "XBOX", "YAKIMA", "YALE", "YANKEE CANDLE", "YESWELDER",
    "YETI", "YOGI", "YOOPAI", "YU-GI-OH", "ZEBRA", "ZESTY PAWS", "ZIPLOC", "ZIPPO",
    "ZOETIS", "ZUKES", "ZWILLING", "ZYRTEC",
}


def norm_brand(brand_str: Any) -> str:
    s = unicodedata.normalize("NFKD", str(brand_str or "")).encode("ascii", "ignore").decode("ascii")
    return s.upper().strip()


def is_verified_vendor(vendor_str: str) -> bool:
    v = str(vendor_str or "").upper().strip()
    if not v or "TARGET BRAND" in v or "UNMAPPED" in v or v in ("UNKNOWN", "NONE", "LOCAL HUB", "DISTRIBUTOR CATALOG"):
        return False
    return any(k in v for k in VERIFIED_DISTRIBUTOR_KEYWORDS)


def load_blocked_brands() -> Tuple[Set[str], List[str]]:
    blocked: Set[str] = {norm_brand(b) for b in EMBEDDED_784_BLOCKED_BRANDS if b}
    for p in (PRIMARY_IP_FILE, ROOT_DIR / "data" / "ip_blocklist.json"):
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                items = raw if isinstance(raw, list) else raw.get("brands", [])
                blocked |= {norm_brand(b) for b in items if b}
            except Exception:
                pass
    sorted_list = sorted(blocked)
    try:
        PRIMARY_IP_FILE.parent.mkdir(parents=True, exist_ok=True)
        PRIMARY_IP_FILE.write_text(json.dumps(sorted_list, indent=2), encoding="utf-8")
    except Exception:
        pass
    multi_word = [b for b in sorted_list if len(b) >= 5]
    return blocked, multi_word


def is_blocked_brand(brand_str: str, blocked_set: Set[str], multi_word_list: List[str]) -> bool:
    b = norm_brand(brand_str)
    if not b or len(b) < 3 or b in EXACT_GENERIC_BRANDS:
        return True
    if b in blocked_set:
        return True
    normalized = re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9\s&'-]", " ", b)).strip()
    if normalized in EXACT_GENERIC_BRANDS or normalized in blocked_set:
        return True
    for entry in multi_word_list:
        if normalized.startswith(entry + " ") or (" " + entry) in normalized:
            return True
    return False


def estimate_real_velocity(monthly_sold: Any, drops_90: Any) -> int:
    ms = int(monthly_sold or 0)
    if ms > 0 and ms not in (20, 25):
        return ms
    d90 = int(drops_90 or 0)
    if d90 > 0:
        return max(1, round((d90 / 3.0) * 1.15))
    return 0


def is_valid_fba_window(comp_fba: int, total_sellers: int, is_verified_multipack: bool) -> bool:
    min_fbm = 2 if is_verified_multipack else 3
    if comp_fba == 0 and total_sellers >= min_fbm:
        return True
    if 2 <= comp_fba <= 4:
        return True
    return False


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
    return "Unmapped (Needs Catalog Match)"


def load_catalog_upc_index() -> Dict[str, Dict[str, Any]]:
    upc_map: Dict[str, Dict[str, Any]] = {}
    if not CATALOG_DIR.exists():
        return upc_map

    files = list(CATALOG_DIR.rglob("*.csv")) + list(CATALOG_DIR.rglob("*.xlsx"))
    for f in files:
        try:
            df = pd.read_csv(f, dtype=str) if f.suffix.lower() == ".csv" else pd.read_excel(f, dtype=str)
            if df.empty:
                continue
            df.columns = [str(c).strip().lower() for c in df.columns]
            cost_c = next((c for c in df.columns if any(k in c for k in ("wholesale", "unit_cost", "cost", "price"))), None)
            upc_c = next((c for c in df.columns if any(k in c for k in ("upc", "ean", "gtin"))), None)
            if not cost_c or not upc_c:
                continue

            vendor = detect_vendor_from_path(f)
            for _, r in df.iterrows():
                try:
                    c_val = float(str(r.get(cost_c, "")).replace("$", "").replace(",", "").strip())
                except Exception:
                    continue
                if c_val <= 0.05:
                    continue
                u = normalize_upc(r.get(upc_c))
                if u and (u not in upc_map or c_val < upc_map[u]["unit_cost"]):
                    upc_map[u] = {"vendor": vendor, "unit_cost": c_val}
        except Exception:
            continue

    return upc_map


def get_table_columns(cur: sqlite3.Cursor, table_name: str) -> Set[str]:
    try:
        return {row[1] for row in cur.execute(f"PRAGMA table_info({table_name})").fetchall()}
    except Exception:
        return set()


def build_two_tier_pipeline_payloads(max_leads: int = 2000) -> List[Dict[str, Any]]:
    if not DB_PATH.exists():
        logger.error(f"SQLite database not found at {DB_PATH}")
        return []

    blocked_set, multi_word_list = load_blocked_brands()
    upc_map = load_catalog_upc_index()
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
            for gr in cur.execute("SELECT brand FROM brand_gating_state WHERE status = 'UNRESTRICTED'").fetchall():
                nb = norm_brand(gr["brand"])
                if nb:
                    ungated_brands.add(nb)

        # 1. Verified Deals first (includes Multipack Loophole + Reverse UPC Matches)
        if vd_cols:
            cur.execute("""
                SELECT * FROM verified_deals
                WHERE status IS NULL OR UPPER(TRIM(status)) NOT IN ('REJECTED', 'BLOCKED_IP_BRAND', 'DELETED')
                ORDER BY
                    CASE WHEN confidence_tier LIKE '%MULTIPACK%' OR confidence_tier LIKE '%UPC%' THEN 0 ELSE 1 END,
                    monthly_profit_pool DESC,
                    roi_percent DESC
            """)
            for row in cur.fetchall():
                if len(payloads) >= max_leads:
                    break
                d = dict(row)
                asin = str(d.get("asin") or "").strip().upper()
                brand = str(d.get("brand") or "").strip()
                if not asin or len(asin) != 10 or asin in seen_asins or is_blocked_brand(brand, blocked_set, multi_word_list):
                    continue

                bb = float(d.get("buy_box_price") or 0.0)
                cost = float(d.get("wholesale_cost") or 0.0)
                if bb < 13.50 or cost <= 0.05 or cost >= bb:
                    continue

                c_row = cur.execute("SELECT * FROM candidate_cache WHERE asin = ?", (asin,)).fetchone() if cc_cols else None
                cd = dict(c_row) if c_row else {}

                vendor = str(d.get("vendor") or "").strip()
                if not is_verified_vendor(vendor):
                    for uf in ("upc", "ean"):
                        u = normalize_upc(cd.get(uf))
                        if u and u in upc_map:
                            vendor = upc_map[u]["vendor"]
                            break
                if not is_verified_vendor(vendor):
                    vendor = "Unmapped (Needs Catalog Match)"

                verified_cost = is_verified_vendor(vendor)
                title = str(d.get("title") or "").strip()
                pack = extract_pack_qty(title, cd.get("amazon_pack_qty"))
                is_mp_loophole = verified_cost and (pack >= 2)

                fba = int(cd.get("fba_sellers") if cd.get("fba_sellers") is not None else (d.get("competitive_fba") or 0))
                total_sellers = int(cd.get("num_sellers") or (2 if is_mp_loophole and fba == 0 else (3 if fba == 0 else fba)))
                if not is_valid_fba_window(fba, total_sellers, is_mp_loophole):
                    continue

                drops = int(cd.get("drops_90") or 0)
                vel = estimate_real_velocity(cd.get("monthly_sold") or d.get("monthly_velocity"), drops)
                if verified_cost:
                    if vel < 25:
                        continue
                else:
                    if vel < 40 or (fba == 0 and vel < 60):
                        continue

                ref_pct = float(cd.get("referral_pct") or 0.15)
                fba_fee = float(cd.get("fba_fee") or max(3.50, bb * 0.18))
                net_profit = float(d.get("net_profit") or round(bb - (bb * ref_pct) - fba_fee - cost, 2))
                min_net = 2.25 if is_mp_loophole else (3.00 if verified_cost else 5.00)
                if net_profit < min_net or net_profit >= bb:
                    continue

                eff_comp = fba if fba > 0 else max(1, round(total_sellers * 0.45))
                our_share = max(1, round(vel / (eff_comp + 1)))
                monthly_pool = round(our_share * net_profit, 2)
                min_pool = 180.0 if is_mp_loophole else (250.0 if verified_cost else 500.0)
                if monthly_pool < min_pool:
                    continue

                logistics = get_logistics_profile(vendor)
                seen_asins.add(asin)
                payloads.append({
                    "asin": asin,
                    "title": title,
                    "brand": brand,
                    "vendor": vendor,
                    "prep_center": "Bundle Poly-Bag + FNSKU" if is_mp_loophole else logistics["facility"],
                    "pack_count": pack,
                    "sales_rank": int(cd.get("sales_rank") or d.get("sales_rank") or 0),
                    "monthly_velocity": vel,
                    "drops_90": drops,
                    "competitive_fba": fba,
                    "total_sellers": total_sellers,
                    "buy_box_price": bb,
                    "current_bb": float(cd.get("current_bb") or bb),
                    "avg90_bb": float(cd.get("avg90_bb") or bb),
                    "wholesale_cost": cost,
                    "landed_bundle_cogs": cost,
                    "net_profit": net_profit,
                    "roi_percent": float(d.get("roi_percent") or round((net_profit / cost) * 100.0, 1)),
                    "is_hazmat": "YES" if d.get("is_hazmat") else "NO",
                    "is_ungated": "YES" if (norm_brand(brand) in ungated_brands or str(d.get("status") or "").upper() == "UNGATED_READY") else "NO",
                })

        # 2. Candidate Cache (includes UNMAPPED_CANDIDATE & non-manufacturer 0-FBA discoveries)
        if cc_cols and len(payloads) < max_leads:
            cur.execute("""
                SELECT * FROM candidate_cache
                WHERE (
                    status IS NULL
                    OR UPPER(TRIM(status)) NOT IN (
                        'BLOCKED_IP_BRAND', 'RESTRICTED_BRAND', 'REJECTED', 'DELETED',
                        'STRUCTURAL_REJECT', 'HIGH_RETURN_CATEGORY', 'POOR_REVIEWS', 'OVERWEIGHT'
                    )
                )
                AND COALESCE(drop_reason, '') NOT LIKE '%Manufacturer/Brand Owner%'
                ORDER BY
                    CASE WHEN matched_vendor IS NOT NULL AND matched_vendor != '' THEN 0 ELSE 1 END,
                    monthly_sold DESC,
                    drops_90 DESC
            """)
            for row in cur.fetchall():
                if len(payloads) >= max_leads:
                    break
                d = dict(row)
                asin = str(d.get("asin") or "").strip().upper()
                brand = str(d.get("brand") or "").strip()
                if not asin or len(asin) != 10 or asin in seen_asins or is_blocked_brand(brand, blocked_set, multi_word_list):
                    continue

                bb = float(d.get("avg90_bb") or d.get("current_bb") or 0.0)
                if bb < 13.50:
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
                    ws_cost = round((matched["unit_cost"] * pack) + (1.25 if pack >= 2 else 0.75), 2)
                elif is_verified_vendor(str(d.get("matched_vendor") or "")) and float(d.get("wholesale_cost") or 0) > 0.05:
                    vendor = str(d.get("matched_vendor")).strip()
                    ws_cost = float(d.get("wholesale_cost"))
                else:
                    vendor = "Unmapped (Needs Catalog Match)"
                    ws_cost = round(bb * 0.36, 2)

                verified_cost = is_verified_vendor(vendor)
                is_mp_loophole = verified_cost and (pack >= 2)

                fba = int(d.get("fba_sellers") or 0)
                total_sellers = int(d.get("num_sellers") or 0)
                if not is_valid_fba_window(fba, total_sellers, is_mp_loophole):
                    continue

                drops = int(d.get("drops_90") or 0)
                vel = estimate_real_velocity(d.get("monthly_sold"), drops)
                if verified_cost:
                    if vel < 25:
                        continue
                else:
                    if vel < 40 or (fba == 0 and vel < 60):
                        continue

                if ws_cost <= 0.05 or ws_cost >= bb:
                    continue

                ref_pct = float(d.get("referral_pct") or 0.15)
                fba_fee = float(d.get("fba_fee") or max(3.50, bb * 0.18))
                net_profit = round(bb - (bb * ref_pct) - fba_fee - ws_cost, 2)
                min_net = 2.25 if is_mp_loophole else (3.00 if verified_cost else 5.00)
                if net_profit < min_net or net_profit >= bb:
                    continue

                roi_pct = round((net_profit / ws_cost) * 100.0, 1)
                if roi_pct < 15.0 or roi_pct > 350.0:
                    continue

                eff_comp = fba if fba > 0 else max(1, round(total_sellers * 0.45))
                our_share = max(1, round(vel / (eff_comp + 1)))
                monthly_pool = round(our_share * net_profit, 2)
                min_pool = 180.0 if is_mp_loophole else (250.0 if verified_cost else 500.0)
                if monthly_pool < min_pool:
                    continue

                logistics = get_logistics_profile(vendor)
                seen_asins.add(asin)
                payloads.append({
                    "asin": asin,
                    "title": title,
                    "brand": brand,
                    "vendor": vendor,
                    "prep_center": "Bundle Poly-Bag + FNSKU" if is_mp_loophole else logistics["facility"],
                    "pack_count": pack,
                    "sales_rank": int(d.get("sales_rank") or 0),
                    "monthly_velocity": vel,
                    "drops_90": drops,
                    "competitive_fba": fba,
                    "total_sellers": total_sellers,
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
                    "is_ungated": "YES" if norm_brand(brand) in ungated_brands else "NO",
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


def stream_backlog(batch_size: int = 100, max_leads: int = 2000, wipe_first: bool = False, dry_run: bool = False):
    webhook_url = get_verified_webhook_url()
    if not webhook_url and not dry_run:
        logger.error("No Google Sheets webhook URL configured.")
        sys.exit(1)

    leads = build_two_tier_pipeline_payloads(max_leads=max_leads)
    total = len(leads)
    dist_counts = Counter(l["vendor"] for l in leads)
    mp_count = sum(1 for l in leads if l["pack_count"] >= 2 and is_verified_vendor(l["vendor"]))

    logger.info(f"Prepared {total:,} Two-Tier Wholesale leads ({mp_count:,} Verified Multipack Loophole leads).")
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

    logger.info(f"Two-Tier Backlog sync complete! Total leads admitted: {total_admitted:,} / {total:,}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Permanent Two-Tier Multipack Loophole + 784-Brand Streamer (v2.5)")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--max-leads", type=int, default=2000)
    parser.add_argument("--wipe-first", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    stream_backlog(
        batch_size=args.batch_size,
        max_leads=args.max_leads,
        wipe_first=args.wipe_first,
        dry_run=args.dry_run,
    )
