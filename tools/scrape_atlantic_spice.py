#!/usr/bin/env python3
"""
tools/scrape_atlantic_spice.py (v2.1)
-------------------------------------
Live Product-Page Catalog Scraper for Atlantic Spice Co. (atlanticspice.com)
- Harvests all 2,882 _p_ product URLs from sitemap.xml
- Uses deterministic ASP-{id} MPN from URL so every product is uniquely deduplicated
- Properly distinguishes bulk bag weight (e.g., 1 lb / 5 lb bag -> pack_qty=1, weight<=45 lb)
  from true multi-unit cases/packs (e.g., "Pack of 6" / "Case of 12" -> normalized unit COGS)
- Detects third-party brands carried by Atlantic Spice (e.g., MICROPLANE) vs house brand
- Enforces 3rd-party / Amazon / eBay marketplace restriction shield + IP blocklist check
- Incremental crash-safe CSV saves every 25 verified SKUs + finally-block flush (Ctrl+C safe)
- Writes clean CSV to both incoming/ and indexed/ so all daemons index it immediately
- Optional --run-pipeline flag chains multipack_reverse_upc_hunter.py -> push_full_backlog_to_sheet.py
"""

import os
import re
import sys
import json
import time
import shutil
import logging
import argparse
import subprocess
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
import pandas as pd
from bs4 import BeautifulSoup
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

load_dotenv(ROOT_DIR / ".env", override=False)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] AtlanticSpiceScraper: %(message)s"
)
logger = logging.getLogger("AtlanticSpiceScraper")

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
if not DATA_DIR.exists():
    DATA_DIR = ROOT_DIR / "data"

INCOMING_DIR = DATA_DIR / "catalogs" / "wholesale_catalogs" / "incoming"
INDEXED_DIR = DATA_DIR / "catalogs" / "wholesale_catalogs" / "indexed"
INCOMING_DIR.mkdir(parents=True, exist_ok=True)
INDEXED_DIR.mkdir(parents=True, exist_ok=True)

OUT_CSV = INCOMING_DIR / "Atlantic_Spice_Catalog.csv"
INDEXED_CSV = INDEXED_DIR / "Atlantic_Spice_Catalog.csv"
REJECT_LOG_CSV = DATA_DIR / "reports" / "atlantic_spice_restricted_skipped.csv"
REJECT_LOG_CSV.parent.mkdir(parents=True, exist_ok=True)
BLOCKLIST_PATH = DATA_DIR / "ip_blocklist.json"

SITEMAP_URL = "https://www.atlanticspice.com/sitemap.xml"
XML_NS = {"ns": "http://www.sitemaps.org/schemas/sitemap/0.9"}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

MARKETPLACE_RESTRICTION_PATTERNS = [
    r"not\s+(?:eligible|allowed|permitted|authorized|approved)\s+(?:for|to\s+sell\s+on|on)\s+(?:amazon|ebay|walmart|3rd\s*party|third\s*party|online\s+marketplace)",
    r"no\s+(?:amazon|ebay|walmart|3rd\s*party|third\s*party|online\s+marketplace)",
    r"(?:amazon|ebay|3rd\s*party|third\s*party)\s+(?:sales\s+)?(?:are\s+)?(?:not\s+allowed|not\s+permitted|not\s+eligible|prohibited|restricted|forbidden|blocked)",
    r"prohibited\s+(?:from|on)\s+(?:amazon|ebay|walmart|3rd\s*party|third\s*party|online\s+marketplace)",
    r"restricted\s+(?:from|on)\s+(?:amazon|ebay|walmart|3rd\s*party|third\s*party|online\s+marketplace)",
    r"brick\s*(?:&|and)\s*mortar\s+(?:stores?\s+)?only",
    r"cannot\s+be\s+(?:sold|listed)\s+on\s+(?:amazon|ebay|3rd\s*party|third\s*party)",
]

ATLANTIC_WHITELIST = {"ATLANTIC SPICE", "ATLANTIC SPICE CO", "ATLANTIC SPICE COMPANY", "MICROPLANE"}
KNOWN_RESELL_BRANDS = ["MICROPLANE", "FRONTIER", "SIMPLY ORGANIC", "MORTON & BASSETT", "BADIA", "WATKINS"]


def norm_brand(b: Any) -> str:
    s = unicodedata.normalize("NFKD", str(b or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", s.upper().strip())


def load_blocked_brands() -> Tuple[Set[str], List[str]]:
    if not BLOCKLIST_PATH.exists():
        return set(), []
    try:
        raw = json.loads(BLOCKLIST_PATH.read_text(encoding="utf-8"))
        items = raw if isinstance(raw, list) else raw.get("brands", [])
        blocked = {norm_brand(b) for b in items if b} - ATLANTIC_WHITELIST
        multi_word = [b for b in sorted(blocked) if len(b) >= 5]
        return blocked, multi_word
    except Exception:
        return set(), []


def is_brand_blocked(brand: str, blocked: Set[str], multi_word: List[str]) -> bool:
    b = norm_brand(brand)
    if not b or len(b) < 3:
        return True
    if b in ATLANTIC_WHITELIST:
        return False
    if b in blocked:
        return True
    for entry in multi_word:
        if b.startswith(entry + " ") or (" " + entry) in b:
            return True
    return False


def get_with_backoff(session: requests.Session, url: str, timeout: int = 20) -> Optional[requests.Response]:
    for attempt in range(1, 4):
        try:
            resp = session.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
            if resp.status_code == 429:
                wait_s = 8 * attempt
                logger.warning(f"Rate limited (HTTP 429) on {url} — sleeping {wait_s}s before retry {attempt}/3...")
                time.sleep(wait_s)
                continue
            return resp
        except Exception as exc:
            logger.warning(f"Request error ({url}): {exc}")
            time.sleep(2 * attempt)
    return None


def check_marketplace_eligibility(soup: BeautifulSoup, raw_html: str) -> Tuple[bool, str]:
    visible_text = " ".join(soup.get_text(" ", strip=True).split())
    for pat in MARKETPLACE_RESTRICTION_PATTERNS:
        m = re.search(pat, visible_text, re.IGNORECASE)
        if m:
            start = max(0, m.start() - 30)
            end = min(len(visible_text), m.end() + 50)
            return False, f"Page Notice: {visible_text[start:end]}"
    if re.search(r'"(?:amazonRestricted|thirdPartyRestricted|ebayRestricted)"\s*:\s*true', raw_html, re.IGNORECASE):
        return False, "JSON Flag: thirdPartyRestricted=true"
    return True, "ALLOWED_3RD_PARTY"


def parse_product_page(
    url: str,
    session: requests.Session,
    blocked_brands: Set[str],
    blocked_multi: List[str],
    freight_per_unit: float = 0.65,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, str]]]:
    resp = get_with_backoff(session, url, timeout=20)
    if resp is None or resp.status_code != 200:
        return None, None

    html = resp.text
    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text("\n", strip=True)

    if "OUT OF STOCK" in page_text.upper() and "ADD TO CART" not in page_text.upper():
        return None, None

    m_id = re.search(r"_p_(\d+)", url.lower())
    canonical_mpn = f"ASP-{m_id.group(1)}" if m_id else url.rstrip("/").split("/")[-1].upper()

    allowed, reason = check_marketplace_eligibility(soup, html)
    if not allowed:
        return None, {"sku": canonical_mpn, "url": url, "reason": reason}

    title, brand, upc, price = "", "ATLANTIC SPICE", "", 0.0

    for script in soup.find_all("script", type="application/ld+json"):
        if not script.string:
            continue
        try:
            data = json.loads(script.string)
            items = data if isinstance(data, list) else [data]
            for it in items:
                if isinstance(it, dict) and it.get("@type") == "Product":
                    title = str(it.get("name", "")).strip()
                    b_val = it.get("brand", "")
                    if isinstance(b_val, dict) and b_val.get("name"):
                        brand = str(b_val.get("name")).strip()
                    elif isinstance(b_val, str) and b_val.strip():
                        brand = b_val.strip()
                    raw_upc = str(it.get("gtin12") or it.get("gtin13") or it.get("gtin") or it.get("upc") or "")
                    upc = re.sub(r"\D", "", raw_upc)
                    offers = it.get("offers", {})
                    if isinstance(offers, dict):
                        if "OUTOFSTOCK" in str(offers.get("availability", "")).upper():
                            return None, None
                        price = float(offers.get("price", 0.0) or 0.0)
                    elif isinstance(offers, list) and offers:
                        price = float(offers[0].get("price", 0.0) or 0.0)
                    break
        except Exception:
            continue

    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(strip=True) if h1 else ""

    # Skip gift certificates & apparel/merch
    t_upper = title.upper()
    if any(skip_kw in t_upper for skip_kw in ("GIFT CERTIFICATE", "TEE SHIRT", "SWEATSHIRT", "SHOPPING BAG")):
        return None, None

    # Detect national brands carried by Atlantic Spice (e.g., Microplane)
    for kb in KNOWN_RESELL_BRANDS:
        if kb in t_upper:
            brand = kb
            break

    if is_brand_blocked(brand, blocked_brands, blocked_multi):
        return None, None

    if price <= 0:
        for sel in (".product-price", ".price", "[data-price]", "#price", ".your-price"):
            price_el = soup.select_one(sel)
            if price_el:
                m = re.search(r"\$([0-9,]+\.[0-9]{2})", price_el.get_text())
                if m:
                    price = float(m.group(1).replace(",", ""))
                    if price > 0:
                        break

    if not upc:
        m_upc = re.search(r"\b(?:UPC|GTIN|Barcode)\s*:?\s*([0-9]{12,13})\b", page_text, re.IGNORECASE | re.MULTILINE)
        if not m_upc:
            m_upc = re.search(r"\b(?:UPC|GTIN)[^0-9]{0,25}([0-9]{12,13})\b", html, re.IGNORECASE | re.DOTALL)
        if m_upc:
            upc = m_upc.group(1)

    if not title or price <= 0:
        return None, None

    # Enforce 45 lb bulk weight ceiling (do NOT turn bag weight into pack_qty)
    m_lb = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:lb|pound)s?\b", title, re.IGNORECASE)
    if m_lb and float(m_lb.group(1)) > 45.0:
        return None, None

    # Only set pack_qty > 1 if the listing is an explicit multi-unit case/pack
    pack_qty = 1
    m_pack = re.search(
        r"(?:case\s*(?:of|pack|qty)|pack\s*of|box\s*of)\s*:?\s*(\d+)|\b(\d+)\s*(?:pk|pack|ct|count|jars?|bottles?|bags?|pouches)\b",
        title,
        re.IGNORECASE,
    )
    if m_pack:
        q = int(m_pack.group(1) or m_pack.group(2))
        if 1 < q <= 48:
            pack_qty = q

    unit_cogs = round((price / pack_qty) + freight_per_unit, 2)

    return {
        "vendor": "ATLANTIC SPICE",
        "brand": brand.upper(),
        "mpn": canonical_mpn.upper(),
        "upc": upc if len(upc) in (12, 13) else "",
        "title": title[:180],
        "wholesale_cost": unit_cogs,
        "unit_cost": unit_cogs,
        "case_cost": round(price, 2),
        "uom": "EA",
        "pack_qty": pack_qty,
        "marketplace_status": "ALLOWED_3RD_PARTY",
        "direct_url": url,
    }, None


def flush_outputs(approved_records: List[Dict[str, Any]], rejected_records: List[Dict[str, str]]) -> int:
    if rejected_records:
        pd.DataFrame(rejected_records).drop_duplicates(subset=["url"]).to_csv(REJECT_LOG_CSV, index=False)

    if approved_records:
        df = pd.DataFrame(approved_records).drop_duplicates(subset=["mpn"], keep="last")
        df.to_csv(OUT_CSV, index=False)
        shutil.copy2(OUT_CSV, INDEXED_CSV)
        return len(df)
    return 0


def run_atlantic_spice_ingestion(
    max_items: int = 0,
    freight_per_unit: float = 0.65,
    delay_sec: float = 0.4,
) -> int:
    logger.info(f"Fetching Atlantic Spice sitemap: {SITEMAP_URL}")
    session = requests.Session()
    blocked_brands, blocked_multi = load_blocked_brands()

    resp = get_with_backoff(session, SITEMAP_URL, timeout=30)
    if resp is None or resp.status_code != 200:
        logger.error("Failed to retrieve Atlantic Spice sitemap.")
        return 0

    try:
        root = ET.fromstring(resp.content)
    except Exception as err:
        logger.error(f"Failed to parse Atlantic Spice sitemap XML: {err}")
        return 0

    product_urls: List[str] = []
    for loc in root.findall(".//ns:loc", XML_NS):
        link = (loc.text or "").strip()
        if "_p_" in link.lower() and "_c_" not in link.lower():
            product_urls.append(link)

    product_urls = list(dict.fromkeys(product_urls))
    target_urls = product_urls[:max_items] if max_items > 0 else product_urls
    logger.info(f"Discovered {len(product_urls)} product URLs in sitemap. Harvesting {len(target_urls)} live SKUs...")

    approved_records: List[Dict[str, Any]] = []
    rejected_records: List[Dict[str, str]] = []

    try:
        for idx, url in enumerate(target_urls, 1):
            rec, rej = parse_product_page(
                url, session, blocked_brands, blocked_multi, freight_per_unit=freight_per_unit
            )
            if rec:
                approved_records.append(rec)
                if len(approved_records) % 50 == 0:
                    saved_cnt = flush_outputs(approved_records, rejected_records)
                    logger.info(f"[{idx}/{len(target_urls)}] Verified {saved_cnt} clean Atlantic Spice SKUs...")
            elif rej:
                rejected_records.append(rej)
                logger.info(f"[EXCLUDED - MARKETPLACE RESTRICTED] {rej['sku']} -> {rej['reason']}")
            time.sleep(delay_sec)
    finally:
        final_cnt = flush_outputs(approved_records, rejected_records)
        if rejected_records:
            logger.info(f"Saved {len(rejected_records)} excluded Atlantic Spice restricted SKUs -> {REJECT_LOG_CSV}")
        if final_cnt > 0:
            logger.info(f"Saved {final_cnt} verified Atlantic Spice SKUs -> {OUT_CSV} & {INDEXED_CSV}")

    return len(approved_records)


def trigger_downstream_match_and_sync():
    logger.info("Triggering Keepa UPC/Multipack matching and Google Sheets backlog sync...")
    for cmd in (
        [sys.executable, str(ROOT_DIR / "tools" / "multipack_reverse_upc_hunter.py")],
        [sys.executable, str(ROOT_DIR / "tools" / "push_full_backlog_to_sheet.py"), "--wipe-first"],
    ):
        script_path = Path(cmd[1])
        if script_path.exists():
            logger.info(f"Running {script_path.name}...")
            subprocess.run(cmd, check=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Atlantic Spice Live Product Scraper v2.1")
    parser.add_argument("--max-items", type=int, default=0, help="Max product pages to crawl (0 = all 2,882)")
    parser.add_argument("--freight-per-unit", type=float, default=0.65, help="Estimated inbound freight per unit")
    parser.add_argument("--delay", type=float, default=0.4, help="Delay in seconds between requests")
    parser.add_argument("--run-pipeline", action="store_true", help="Run Keepa UPC matcher and Google Sheets sync after scraping")
    args = parser.parse_args()

    count = run_atlantic_spice_ingestion(
        max_items=args.max_items,
        freight_per_unit=args.freight_per_unit,
        delay_sec=args.delay,
    )
    if args.run_pipeline and count > 0:
        trigger_downstream_match_and_sync()
    sys.exit(0 if count > 0 else 1)
