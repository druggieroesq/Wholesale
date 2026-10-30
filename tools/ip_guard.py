#!/usr/bin/env python3
"""
Master Universal IP Guard & Brand Restriction Shield
Enforces zero-tolerance brand & SKU protection across all catalogs,
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

HIGH_RISK_IP_BRANDS = {
    "3M", "DEWALT", "MILWAUKEE", "MAKITA", "BOSCH", "CRAFTSMAN", "BLACK+DECKER",
    "RYOBI", "STANLEY", "IRWIN", "KLEIN TOOLS", "CHANNELLOCK", "CRESCENT",
    "SCOTCH", "GORILLA GLUE", "LOCTITE", "WD-40", "PERMATEX", "RUST-OLEUM",
    "DYSON", "SHARK", "NINJA", "KITCHENAID", "CUISINART", "INSTANT POT", "OXO",
    "RUBBERMAID", "RUBBERMAID COMMERCIAL", "GRAINGER", "PYREX", "BRITA", "KEURIG",
    "NESPRESSO", "HYDRO FLASK", "YETI", "CONTIGO", "CAMELBAK", "OTTERBOX",
    "POPSOCKETS", "SPIGEN", "BELKIN", "APPLE", "SONY", "BOSE", "SAMSUNG",
    "ANKER", "LOGITECH", "JBL", "SKULLCANDY", "NIKE", "ADIDAS", "UNDER ARMOUR",
    "PUMA", "NEW BALANCE", "THE NORTH FACE", "CARHARTT", "COLUMBIA", "PATAGONIA",
    "LEVI'S", "CHAMPION", "LEGO", "HASBRO", "MATTEL", "NERF", "BARBIE",
    "HOT WHEELS", "FISHER-PRICE", "CREST", "ORAL-B", "COLGATE", "LISTERINE",
    "PHILIPS SONICARE", "DOVE", "OLAY", "NEUTROGENA", "L'OREAL", "CERAVE",
    "CETAPHIL", "AVEENO", "BURT'S BEES"
}

FRONTIER_RESTRICTED_SKUS = {
    "8506", "207431", "213697", "213754", "213756", "213914", "213921", "213924",
    "218854", "222334", "222620", "222621", "222622", "222623", "222624", "222625",
    "222655", "222656", "222657", "224633", "225246", "228162", "228169", "228424",
    "229945", "229947", "230959", "230961", "230963", "230965", "230966", "230968",
    "232256", "232260", "232262", "232264", "232265", "232275", "232277", "232278",
    "232286", "232289", "232290", "232291", "232293", "234315", "234783", "234786",
    "234788", "234795", "234798", "234800", "234802", "234804", "234805", "234817",
    "234822", "234858", "235095", "235096", "235097", "235099", "235720", "235722",
    "235723", "236742", "236748", "236753", "236755", "236758", "236759", "236761",
    "236762", "236766", "236767", "236768", "236770", "237308", "237309", "237316",
    "237317", "237322", "237372", "237373", "237738", "237747", "237749", "237752",
    "237792", "237793", "237795", "237797", "237800", "237803", "237805", "237806",
    "237807", "238135", "238136", "238137", "238397", "238399", "238400", "238401",
    "238402", "238408", "238409", "238410", "238412", "238413", "238414", "238415",
    "238416", "238759", "238760", "238761", "238986", "238991", "238992", "238993",
    "238994", "238997", "238998", "238999", "239000", "239002", "239065", "239068",
    "239070", "239160", "239161", "239164", "239165", "239166", "239243", "239244",
    "239245", "239819", "239826", "239827", "239828", "239829", "240373", "240374",
    "240375", "240376", "240377", "240378", "240419", "240623", "240626", "240627",
    "240628", "240629", "240630", "240631", "240632", "240633", "240634", "240637",
    "240638", "240639", "240640", "240641", "241008", "241189", "241190", "241191",
    "241192", "241533", "241534", "241537", "241538", "241539", "241540", "241541",
    "241542", "241543", "241544", "241546", "241547", "241548", "241549", "241550",
    "242049", "242050", "242053", "242054", "242056", "242057", "242058", "242059",
    "242298", "242299", "242301", "242302", "242303"
}

_BLOCKLIST_SET = set()
_BLOCKED_SKUS = set()
_NORMALIZED_PATTERNS = []


def load_blocklist():
    global _BLOCKLIST_SET, _BLOCKED_SKUS, _NORMALIZED_PATTERNS

    _BLOCKLIST_SET = BCI_RESTRICTED_BRANDS | FRONTIER_POLICY_BRANDS | HIGH_RISK_IP_BRANDS
    _BLOCKED_SKUS = set(FRONTIER_RESTRICTED_SKUS)

    if BLOCKLIST_FILE.exists():
        try:
            with open(BLOCKLIST_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                raw_brands = data.get("brands", [])
                raw_skus = data.get("skus", [])
                _BLOCKLIST_SET.update(b.strip().upper() for b in raw_brands if b)
                _BLOCKED_SKUS.update(str(s).strip() for s in raw_skus if s)
        except Exception as e:
            logger.error(f"Failed loading {BLOCKLIST_FILE}: {e}")

    if RULES_FILE.exists():
        try:
            with open(RULES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                raw_brands = data.get("restricted_brands", [])
                raw_skus = data.get("restricted_skus", [])
                _BLOCKLIST_SET.update(b.strip().upper() for b in raw_brands if b)
                _BLOCKED_SKUS.update(str(s).strip() for s in raw_skus if s)
        except Exception as e:
            logger.error(f"Failed loading {RULES_FILE}: {e}")

    _NORMALIZED_PATTERNS = [
        re.compile(r"\b" + re.escape(b) + r"\b", re.IGNORECASE)
        for b in _BLOCKLIST_SET if len(b) > 2
    ]

    logger.info(
        f"Master IP Firewall armed: {len(_BLOCKLIST_SET)} brands protected, "
        f"{len(_BLOCKED_SKUS)} item SKUs blacklisted."
    )


load_blocklist()


def is_sku_blocked(sku: str) -> Tuple[bool, str]:
    if not _BLOCKED_SKUS:
        load_blocklist()
    s_clean = str(sku or "").strip()
    if s_clean in _BLOCKED_SKUS:
        return True, f"Policy Blacklist: Restricted SKU '{s_clean}'"
    return False, ""


def is_brand_blocked(brand: str = "", title: str = "", vendor: str = "", distributor: str = "") -> Tuple[bool, str]:
    if not _BLOCKLIST_SET:
        load_blocklist()

    b_clean = (brand or "").strip().upper()
    v_clean = (vendor or "").strip().upper()
    t_clean = (title or "").strip().upper()

    if b_clean in _BLOCKLIST_SET:
        return True, f"Global Blacklist: Exact brand match '{b_clean}'"
    if v_clean in _BLOCKLIST_SET:
        return True, f"Global Blacklist: Exact vendor match '{v_clean}'"

    for pattern in _NORMALIZED_PATTERNS:
        if pattern.search(b_clean):
            return True, f"Global Blacklist: Brand contains '{pattern.pattern[2:-2]}'"
        if pattern.search(t_clean):
            return True, f"Global Blacklist: Title contains protected brand '{pattern.pattern[2:-2]}'"

    return False, ""
