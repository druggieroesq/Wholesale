#!/usr/bin/env python3
"""
tools/ingest_eedistribution_catalog.py
--------------------------------------
Authenticated EE Distribution Catalog & Marketplace-Restriction Scraper
- Authenticates with EE_DIST_EMAIL & EE_DIST_PASSWORD from Railway / .env
- Crawls In-Stock brand listings across target toy/collectible brands
- Visits EVERY product detail page (PDP) to verify EE Distribution's
  3rd-Party / Amazon / eBay marketplace eligibility notices
- Immediately drops any SKU flagged with Amazon, eBay, or 3rd-party restrictions
- Normalizes Case Price / Pack Qty to true per-unit COGS (+ inbound freight)
- Filters out IP-blocked brands (data/ip_blocklist.json) & oversized items (> 45 lbs)
- Writes clean CSV to data/catalogs/wholesale_catalogs/incoming/EEDistribution_Catalog.csv
"""

import os
import re
import sys
import json
import time
import logging
import argparse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

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
    format="%(asctime)s [%(levelname)s] EEDistIngest: %(message)s"
)
logger = logging.getLogger("EEDistIngest")

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
if not DATA_DIR.exists():
    DATA_DIR = ROOT_DIR / "data"

INCOMING_DIR = DATA_DIR / "catalogs" / "wholesale_catalogs" / "incoming"
INCOMING_DIR.mkdir(parents=True, exist_ok=True)
OUT_CSV = INCOMING_DIR / "EEDistribution_Catalog.csv"
REJECT_LOG_CSV = DATA_DIR / "reports" / "ee_marketplace_restricted_skipped.csv"
REJECT_LOG_CSV.parent.mkdir(parents=True, exist_ok=True)
BLOCKLIST_PATH = DATA_DIR / "ip_blocklist.json"

BASE_URL = "https://www.eedistribution.com"
LOGIN_URL = f"{BASE_URL}/Login"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Explicit phrases EE Distribution uses when a brand/SKU prohibits Amazon or 3rd-party sales
EE_RESTRICTION_PATTERNS = [
    r"not\s+(?:eligible|allowed|permitted|authorized)\s+(?:for|to\s+sell\s+on)\s+(?:amazon|3rd\s*party|third\s*party)",
    r"no\s+(?:amazon|ebay|3rd\s*party|third\s*party|online\s+marketplace)",
    r"prohibited\s+(?:from|on)\s+(?:amazon|ebay|3rd\s*party|third\s*party)",
    r"restricted\s+(?:from|on)\s+(?:amazon|3rd\s*party|third\s*party)",
    r"brick\s*(?:&|and)\s*mortar\s+only",
    r"amazon\s+restriction",
    r"3rd\s*party\s+marketplace\s+restriction",
    r"cannot\s+be\s+sold\s+on\s+amazon",
]


def load_blocked_brands() -> set:
    if not BLOCKLIST_PATH.exists():
        return set()
    try:
        raw = json.loads(BLOCKLIST_PATH.read_text(encoding="utf-8"))
        items = raw if isinstance(raw, list) else raw.get("brands", [])
        return {str(b).upper().strip() for b in items if b}
    except Exception:
        return set()


def is_brand_blocked(brand: str, blocked: set) -> bool:
    b = brand.upper().strip()
    if not b or b in blocked:
        return True
    return any(blk in b or b in blk for blk in blocked if len(blk) >= 4)


def create_authenticated_session() -> requests.Session:
    email = os.getenv("EE_DIST_EMAIL", "").strip()
    password = os.getenv("EE_DIST_PASSWORD", "").strip()
    if not email or not password:
        raise RuntimeError("Missing EE_DIST_EMAIL or EE_DIST_PASSWORD in environment variables.")

    session = requests.Session()
    session.headers.update(HEADERS)

    # Fetch login page to capture hidden anti-forgery / ASP.NET form tokens
    resp = session.get(LOGIN_URL, timeout=20)
    soup = BeautifulSoup(resp.text, "html.parser")
    payload: Dict[str, str] = {}
    for inp in soup.find_all("input"):
        name = inp.get("name")
        if name:
            payload[name] = inp.get("value", "")

    # Populate common EE Distribution login field names
    payload["Email"] = email
    payload["Password"] = password
    payload["email"] = email
    payload["password"] = password
    payload["RememberMe"] = "true"

    post_resp = session.post(LOGIN_URL, data=payload, timeout=20, allow_redirects=True)
    if "logout" not in post_resp.text.lower() and "my account" not in post_resp.text.lower():
        logger.warning("EE Distribution login response did not show 'Logout' — verify credentials if prices are hidden.")
    else:
        logger.info("Authenticated EE Distribution wholesale session active.")
    return session


def check_ee_marketplace_eligibility(soup: BeautifulSoup, raw_html: str) -> Tuple[bool, str]:
    """
    Inspects the EE Distribution product page for any Amazon, eBay, or 3rd-party
    marketplace selling prohibition. Returns (is_allowed, reason).
    """
    # 1. Check dedicated restriction badge / alert containers first
    alert_selectors = [
        ".restriction", ".restrictions", ".marketplace-restriction",
        ".product-restrictions", ".alert-warning", ".alert-danger",
        "[class*='restrict']", "[id*='restrict']", "[data-restriction]"
    ]
    for sel in alert_selectors:
        for el in soup.select(sel):
            txt = " ".join(el.get_text(" ", strip=True).split())
            if not txt:
                continue
            for pat in EE_RESTRICTION_PATTERNS:
                if re.search(pat, txt, re.IGNORECASE):
                    return False, f"Badge/Alert: {txt[:120]}"

    # 2. Check product specification rows (e.g., "3rd Party Allowed: No" / "Amazon: Restricted")
    for row in soup.select("tr, li, .spec-row, .product-attribute"):
        row_txt = " ".join(row.get_text(" ", strip=True).split())
        if len(row_txt) > 220:
            continue
        if re.search(r"\b(?:amazon|3rd\s*party|third\s*party|marketplace)\b", row_txt, re.IGNORECASE):
            if re.search(r"\b(?:no|not\s+allowed|prohibited|restricted|blocked|ineligible)\b", row_txt, re.IGNORECASE):
                return False, f"Spec Attribute: {row_txt[:120]}"

    # 3. Full-page scan against explicit EE Distribution restriction phrases
    visible_text = " ".join(soup.get_text(" ", strip=True).split())
    for pat in EE_RESTRICTION_PATTERNS:
        m = re.search(pat, visible_text, re.IGNORECASE)
        if m:
            start = max(0, m.start() - 30)
            end = min(len(visible_text), m.end() + 50)
            return False, f"Page Notice: {visible_text[start:end]}"

    # 4. Check structured JSON / data attributes embedded in page scripts
    if re.search(r'"(?:amazonRestricted|thirdPartyRestricted|noAmazon)"\s*:\s*true', raw_html, re.IGNORECASE):
        return False, "JSON Flag: thirdPartyRestricted=true"

    return True, "ALLOWED_3RD_PARTY"


def discover_brand_product_urls(session: requests.Session, brand_slug: str, max_pages: int = 4) -> List[str]:
    urls: List[str] = []
    for page in range(1, max_pages + 1):
        list_url = f"{BASE_URL}/s/{brand_slug}?instock=1&page={page}"
        try:
            r = session.get(list_url, timeout=20)
            if r.status_code != 200:
                break
            soup = BeautifulSoup(r.text, "html.parser")
            found_on_page = 0
            for a in soup.select("a[href*='/p/'], a.product-title, .product-item a"):
                href = a.get("href", "").strip()
                if "/p/" in href:
                    full = urljoin(BASE_URL, href.split("?")[0])
                    if full not in urls:
                        urls.append(full)
                        found_on_page += 1
            if found_on_page == 0:
                break
            time.sleep(0.4)
        except Exception as exc:
            logger.warning(f"Listing scan error ({list_url}): {exc}")
            break
    return urls


def parse_ee_product_page(
    url: str,
    session: requests.Session,
    blocked_brands: set,
    freight_per_unit: float
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, str]]]:
    """
    Fetches and parses a single EE Distribution product page.
    Returns (valid_record, None) if eligible, or (None, reject_record) if blocked/restricted.
    """
    try:
        resp = session.get(url, timeout=20)
        if resp.status_code != 200:
            return None, None

        raw_html = resp.text
        soup = BeautifulSoup(raw_html, "html.parser")

        # 1. Verify In-Stock status
        page_upper = soup.get_text(" ", strip=True).upper()
        if "OUT OF STOCK" in page_upper and "IN STOCK" not in page_upper:
            return None, None

        # 2. Mandatory EE Distribution 3rd-Party / Amazon Restriction Check
        allowed, reason = check_ee_marketplace_eligibility(soup, raw_html)
        if not allowed:
            sku_guess = url.rstrip("/").split("/")[-1]
            return None, {"sku": sku_guess, "url": url, "reason": reason}

        # 3. Extract product details via JSON-LD + PDP selectors
        title, brand, mpn, upc, raw_price = "", "", "", "", 0.0
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
                        brand = str(b_val.get("name") if isinstance(b_val, dict) else b_val or "").strip()
                        mpn = str(it.get("sku") or it.get("mpn") or "").strip()
                        raw_upc = str(it.get("gtin12") or it.get("gtin13") or it.get("upc") or "")
                        upc = re.sub(r"\D", "", raw_upc)
                        offers = it.get("offers", {})
                        if isinstance(offers, dict):
                            raw_price = float(offers.get("price", 0.0) or 0.0)
                        elif isinstance(offers, list) and offers:
                            raw_price = float(offers[0].get("price", 0.0) or 0.0)
                        break
            except Exception:
                continue

        if not title:
            h1 = soup.find("h1")
            title = h1.get_text(strip=True) if h1 else ""

        if not brand:
            b_el = soup.select_one(".brand-name, [itemprop='brand'], .manufacturer")
            brand = b_el.get_text(strip=True) if b_el else ""

        if not brand or is_brand_blocked(brand, blocked_brands):
            return None, None

        if raw_price <= 0:
            p_el = soup.select_one(".your-price, .wholesale-price, .product-price, [data-price]")
            if p_el:
                m = re.search(r"\$([0-9,]+\.[0-9]{2})", p_el.get_text())
                if m:
                    raw_price = float(m.group(1).replace(",", ""))

        if not upc:
            m_upc = re.search(r"\b(?:UPC|GTIN)[:\s#]*([0-9]{12,13})\b", raw_html, re.IGNORECASE)
            if m_upc:
                upc = m_upc.group(1)

        if not mpn:
            m_sku = re.search(r"\b(?:Item|SKU|Model)\s*#?\s*:?\s*([A-Za-z0-9_-]+)\b", raw_html, re.IGNORECASE)
            mpn = m_sku.group(1) if m_sku else url.rstrip("/").split("/")[-1].upper()

        if not title or raw_price <= 0:
            return None, None

        # 4. Enforce 45-lb parcel limit
        m_wt = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:lbs?|pounds?)\b", raw_html, re.IGNORECASE)
        if m_wt and float(m_wt.group(1)) > 45.0:
            return None, None

        # 5. Detect Case Pack Qty and calculate true per-unit COGS
        pack_qty = 1
        m_case = re.search(
            r"(?:case\s*(?:of|pack|qty)|master\s*carton|pack\s*of)\s*:?\s*(\d+)|\b(\d+)\s*(?:pcs|pieces|units)\s*/\s*case\b",
            f"{title} {soup.get_text(' ', strip=True)[:1500]}",
            re.IGNORECASE,
        )
        if m_case:
            qty_val = int(m_case.group(1) or m_case.group(2))
            if 1 < qty_val <= 48:
                pack_qty = qty_val

        unit_cogs = round((raw_price / pack_qty) + freight_per_unit, 2)

        return {
            "vendor": "EE DISTRIBUTION",
            "brand": brand.upper(),
            "mpn": mpn.upper(),
            "upc": upc if len(upc) in (12, 13) else "",
            "title": title[:180],
            "wholesale_cost": unit_cogs,
            "case_cost": round(raw_price, 2),
            "uom": "EA",
            "pack_qty": pack_qty,
            "marketplace_status": "ALLOWED_3RD_PARTY",
            "direct_url": url,
        }, None
    except Exception:
        return None, None


def run_ee_ingestion(brands: List[str], max_pages: int = 4, freight_per_unit: float = 0.85) -> int:
    session = create_authenticated_session()
    blocked_brands = load_blocked_brands()

    all_urls: List[str] = []
    for b in brands:
        logger.info(f"Scanning EE Distribution In-Stock listings for brand: {b}")
        b_urls = discover_brand_product_urls(session, b, max_pages=max_pages)
        logger.info(f"  -> Found {len(b_urls)} product URLs for '{b}'")
        all_urls.extend(b_urls)

    all_urls = list(dict.fromkeys(all_urls))
    logger.info(f"Inspecting {len(all_urls)} total EE Distribution product pages for 3rd-party eligibility & COGS...")

    approved_records: List[Dict[str, Any]] = []
    rejected_records: List[Dict[str, str]] = []

    for url in all_urls:
        rec, rej = parse_ee_product_page(url, session, blocked_brands, freight_per_unit)
        if rec:
            approved_records.append(rec)
            if len(approved_records) % 25 == 0:
                logger.info(f"Verified {len(approved_records)} Amazon-eligible EE SKUs...")
        elif rej:
            rejected_records.append(rej)
            logger.info(f"[SKIPPED - 3RD PARTY RESTRICTED] {rej['sku']} -> {rej['reason']}")
        time.sleep(0.35)

    if rejected_records:
        pd.DataFrame(rejected_records).to_csv(REJECT_LOG_CSV, index=False)
        logger.info(f"Logged {len(rejected_records)} EE 3rd-party restricted SKUs -> {REJECT_LOG_CSV.name}")

    if not approved_records:
        logger.warning("No Amazon-eligible EE Distribution SKUs extracted.")
        return 0

    df = pd.DataFrame(approved_records).drop_duplicates(subset=["mpn"])
    df.to_csv(OUT_CSV, index=False)
    logger.info(f"Exported {len(df)} Amazon-eligible EE Distribution SKUs -> {OUT_CSV}")
    return len(df)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Authenticated EE Distribution Scraper with Marketplace Gate")
    parser.add_argument(
        "--brands",
        nargs="+",
        default=["funko", "hasbro", "mattel", "bandai", "mcfarlane-toys", "neca"],
        help="Brand slugs to crawl on eedistribution.com"
    )
    parser.add_argument("--max-pages", type=int, default=4, help="Max pagination pages per brand")
    parser.add_argument("--freight-per-unit", type=float, default=0.85, help="Estimated inbound freight per unit")
    args = parser.parse_args()

    count = run_ee_ingestion(
        brands=args.brands,
        max_pages=args.max_pages,
        freight_per_unit=args.freight_per_unit
    )
    sys.exit(0 if count > 0 else 1)
