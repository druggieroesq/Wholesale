#!/usr/bin/env python3
"""
Master Universal IP Guard & Brand Restriction Shield
Enforces zero-tolerance brand & SKU protection across ALL catalogs, 
Keepa scrapers, and sourcing modules with 0 API token burn.
"""

import json
import logging
import re
from pathlib import Path
from typing import Tuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("IPGuard")

BLOCKLIST_FILE = Path(__file__).resolve().parent.parent / "data" / "ip_blocklist.json"
RULES_FILE = Path(__file__).resolve().parent.parent / "data" / "rules" / "frontier_blacklist.json"

# ==============================================================================
# PERMANENT BUILT-IN RESTRICTION SETS (INDEPENDENT OF LOCAL STORAGE)
# ==============================================================================

# 1. BCI Agricultural, Equine, Pet, Wild Bird, & Farm Restrictive Brands
BCI_RESTRICTED_BRANDS = {
    "ACORN PET PRODUCTS", "ALLIED PRECISION", "ALZOO", "ANDIS", "ANI MED",
    "ANTLER KING TROPHY PRODUCT", "AQUATIC LIFE", "AQUATOP", "ASPECTS BIRD FEEDER",
    "BACKYARD NATURE PRODUCTS", "BANIXX", "BC ANIMAL SOLUTIONS", "BENEBONE",
    "BENGAL PRODUCTS", "BEST FRIEND EQUINE", "BICKMORE", "BIL- JAC", "BILLY BOBS",
    "BOGS BOOTS", "BROME", "BULLY TOOL", "CANADA POOCH WAG LAB", "CHAMPION TURF",
    "CHOKE FREE", "CHUCKIT", "CLASSIC BRANDS", "COASTAL PET PRODUCTS", "CORONA CLIPPER",
    "COWBOY MAGIC", "CRAZY DOG", "DOG IS GOOD", "DROLL YANKEES", "FARNAM",
    "FOUR PAWS", "KONG", "LAMBERT KAY", "LIFE DATA", "MANNA PRO",
    "MILLER MANUFACTURING", "PET AG", "PET NATURALS", "PETSAFE", "ROUDYBUSH",
    "TROPICLEAN", "ZOO MED", "BLUE BUFFALO", "PURINA", "HILL'S SCIENCE DIET",
    "GREENIES", "FRONTLINE", "ZOETIS", "MERIAL", "ELANCO"
}

# 2. Frontier Co-op Reselling Policy Restricted Brands
FRONTIER_POLICY_BRANDS = {
    "ALL GOOD", "ANDALOU NATURALS", "ARK NATURALS", "ATTITUDE", "BAUDELAIRE",
    "BUDDHA TEAS", "BUG SOOTHER", "CHILD LIFE", "COFFEESOCK", "COLOR KITCHEN INC",
    "CULTURES FOR HEALTH INC", "DR BRONNERS MAGIC SOAP", "E-CLOTH", "ECO LIPS, INC",
    "ECO LUNCH BOXES", "ERLEIA", "FLORAL ELIXIR CO", "FLOURISH PLANT, INC",
    "FOOD HUGGERS", "FOUR ELEMENTS ORGANICS", "GLUTEN FREE BROTHERS", "HIBAR",
    "HONESTLY PHRESH", "JADE AND PEARL INC", "JOYRIDE", "KAL", "KING ARTHUR BAKING",
    "KIRK'S", "LOTUS FOODS", "MAD HIPPIE", "MAGGIE'S ORGANICS", "MARYRUTH'S",
    "MASONTOPS INC", "MAYA KAIMAL FOODS LLC", "MELIORA", "MINERAL FUSION",
    "NATURE'S GURU", "OIAMIGA", "ORGANIC INDIA USA", "PLANTED CANDLES",
    "PUREECOSHEET", "PUREMEDY INC", "REFRESH X, INC.", "REVIVA LABS INC.",
    "RIDGE CREST HERBALS", "SAALT INC", "SEAWEED BATH", "SIMPLY DESSERTS",
    "SINOL", "SOLARAY", "SOUTH OF FRANCE", "SOVERIGN SILVER", "SUNWARRIOR",
    "SWEETLEAF SWEETENER", "TERRA ORIGIN", "THE GRANDPA SOAP CO.",
    "THE HUMBLY COMPANY", "TIIGA", "TINT'S OF NATURE", "TOM'S OF MAINE INC",
    "UKONSERVE", "UNPASTE", "UNREAL", "WATERDROP MICRODRINKS", "Z WRAPS", "ZHOU"
}

# 3. High-Risk IP Enforcers (MRO, Power Tools, Industrial, Consumer)
HIGH_RISK_IP_BRANDS = {
    "3M", "DEWALT", "MILWAUKEE", "MAKITA", "BOSCH", "CRAFTSMAN", "BLACK+DECKER",
    "RYOBI", "STANLEY", "IRWIN", "KLEIN TOOLS", "CHANNELLOCK", "CRESCENT",
    "SCOTCH", "GORILLA GLUE", "LOCTITE", "WD-40", "PERMATEX", "RUST-OLEUM",
    "DYSON", "SHARK", "NINJA", "KITCHENAID", "CUISINART", "INSTANT POT", "OXO",
    "RUBBERMAID", "PYREX", "BRITA", "KEURIG", "NESPRESSO", "HYDRO FLASK", "YETI",
    "CONTIGO", "CAMELBAK", "OTTERBOX", "POPSOCKETS", "SPIGEN", "BELKIN",
    "APPLE", "SONY", "BOSE", "SAMSUNG", "ANKER", "LOGITECH", "JBL", "SKULLCANDY",
    "NIKE", "ADIDAS", "UNDER ARMOUR", "PUMA", "NEW BALANCE", "THE NORTH FACE",
    "CARHARTT", "COLUMBIA", "PATAGONIA", "LEVI'S", "CHAMPION", "LEGO", "HASBRO",
    "MATTEL", "NERF", "BARBIE", "HOT WHEELS", "FISHER-PRICE", "CREST", "ORAL-B",
    "COLGATE", "LISTERINE", "PHILIPS SONICARE", "DOVE", "OLAY", "NEUTROGENA",
    "L'OREAL", "CERAVE", "CETAPHIL", "AVEENO", "BURT'S BEES"
}

# 4. Frontier Restricted SKUs / Item Numbers
FRONTIER_RESTRICTED_SKUS = {
    "8506", "213754", "213756", "213914", "218854", "224633", "228424", "232256",
    "232275", "234800", "234822", "236748", "236753", "236755", "236768", "237308",
    "237309", "237752", "238135", "238136", "238137", "238397", "238399", "238400",
    "238401", "238402", "238413", "238986", "238993", "238999", "239002", "239160",
    "239161", "239164", "239165", "239166", "239819", "239827", "240373", "240374",
    "240375", "240377", "240378", "240632", "240633", "240638", "240640", "241537",
    "241538", "241539", "241540", "241541", "241543", "241546", "241547", "241548",
    "241549", "242049", "242050", "242054", "242059", "242298", "242299", "242301",
    "242302", "242303", "213921", "213924", "222620", "222621", "222622", "222623",
    "222624", "222625", "222655", "222656", "222657", "230968", "235095", "235096",
    "235097", "235099", "237322", "237372", "237373", "237738", "238997", "239828",
    "239829", "240419", "240628", "240631", "241544", "241550", "242056", "213697",
    "237792", "237793", "237795", "237797", "237800", "237803", "237805", "237806",
    "237807", "240623", "225246", "228162", "230965", "238408", "240626", "240627",
    "222334", "239065", "239068", "239070", "230966", "232277", "232278", "234858",
    "238412", "238994", "228169", "229945", "229947", "230959", "230961", "230963",
    "232260", "232262", "232264", "232265", "232291", "234783", "234786", "234788",
    "234795", "234798", "234802", "234804", "234805", "234817", "235723", "236742",
    "236758", "236759", "236761", "236762", "236766", "236767", "236770", "237747",
    "237749", "238409", "238410", "238414", "238415", "238416", "238759", "238760",
    "238761", "238991", "238992", "238998", "239000", "240629", "240630", "240637",
    "240641", "241008", "241189", "241190", "241191", "241192", "241533", "241534",
    "241542", "242053", "242057", "232286", "232289", "232290", "239243", "234315",
    "237316", "237317", "240376", "242058", "239244", "239245", "239826", "240639",
    "207431", "232293", "235720", "235722", "240634"
}

# ==============================================================================
# STATE & INITIALIZATION
# ==============================================================================

_BLOCKLIST_SET = set()
_BLOCKED_SKUS = set()
_NORMALIZED_PATTERNS = []


def load_blocklist():
    """Initializes and merges internal sets with external JSON overrides."""
    global _BLOCKLIST_SET, _BLOCKED_SKUS, _NORMALIZED_PATTERNS

    _BLOCKLIST_SET = BCI_RESTRICTED_BRANDS | FRONTIER_POLICY_BRANDS | HIGH_RISK_IP_BRANDS
    _BLOCKED_SKUS = set(FRONTIER_RESTRICTED_SKUS)

    # Ingest data/ip_blocklist.json if present
    if BLOCKLIST_FILE.exists():
        try:
            with open(BLOCKLIST_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                raw_brands = data.get("brands", [])
                _BLOCKLIST_SET.update(b.strip().upper() for b in raw_brands if b)
        except Exception as e:
            logger.error(f"Failed loading {BLOCKLIST_FILE}: {e}")

    # Ingest data/rules/frontier_blacklist.json if present
    if RULES_FILE.exists():
        try:
            with open(RULES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                _BLOCKLIST_SET.update(b.strip().upper() for b in data.get("restricted_brands", []) if b)
                _BLOCKED_SKUS.update(str(s).strip() for s in data.get("restricted_skus", []) if s)
        except Exception as e:
            logger.error(f"Failed loading {RULES_FILE}: {e}")

    # Pre-compile exact word boundaries for high-speed regex evaluation
    _NORMALIZED_PATTERNS = [
        re.compile(r"\b" + re.escape(b) + r"\b", re.IGNORECASE)
        for b in _BLOCKLIST_SET if len(b) > 2
    ]

    logger.info(
        f"Master IP Firewall armed: {len(_BLOCKLIST_SET)} brands protected, "
        f"{len(_BLOCKED_SKUS)} item SKUs blacklisted."
    )


load_blocklist()

# ==============================================================================
# PUBLIC CHECKPOINTS
# ==============================================================================

def is_sku_blocked(sku: str) -> Tuple[bool, str]:
    """Check if item/distributor SKU is explicitly blacklisted."""
    if not _BLOCKED_SKUS:
        load_blocklist()
    s_clean = str(sku or "").strip()
    if s_clean in _BLOCKED_SKUS:
        return True, f"Policy Blacklist: Restricted SKU '{s_clean}'"
    return False, ""


def is_brand_blocked(brand: str, title: str = "", vendor: str = "", distributor: str = "") -> Tuple[bool, str]:
    """
    Universal checkpoint across all catalog, scan, and RFQ feeds.
    Returns (True, reason) if item matches any blacklisted brand or token.
    """
    if not _BLOCKLIST_SET:
        load_blocklist()

    b_clean = (brand or "").strip().upper()
    v_clean = (vendor or "").strip().upper()
    t_clean = (title or "").strip().upper()

    # 1. Exact string match against Brand or Vendor Name
    if b_clean in _BLOCKLIST_SET:
        return True, f"Global Blacklist: Exact brand match '{b_clean}'"
    if v_clean in _BLOCKLIST_SET:
        return True, f"Global Blacklist: Exact vendor match '{v_clean}'"

    # 2. Word boundary scan across Title and Brand string
    for pattern in _NORMALIZED_PATTERNS:
        if pattern.search(b_clean):
            return True, f"Global Blacklist: Brand contains '{pattern.pattern[2:-2]}'"
        if pattern.search(t_clean):
            return True, f"Global Blacklist: Title contains protected brand '{pattern.pattern[2:-2]}'"

    return False, ""


if __name__ == "__main__":
    test_cases = [
        ("KONG", "", ""),
        ("Andis", "", ""),
        ("Dr Bronners Magic Soap", "", ""),
        ("Safe Brand", "Mrs. Anderson Ceramic Pie Weights", ""),
        ("Unknown", "Regular Item", "")
    ]
    for b, t, v in test_cases:
        blocked, reason = is_brand_blocked(b, t, v)
        print(f"[{'BLOCKED' if blocked else 'PASS'}] Brand: '{b}', Title: '{t}' -> {reason}")

    sku_tests = ["213921", "999999"]
    for s in sku_tests:
        blocked, reason = is_sku_blocked(s)
        print(f"[{'BLOCKED' if blocked else 'PASS'}] SKU: '{s}' -> {reason}")
