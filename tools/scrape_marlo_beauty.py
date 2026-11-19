#!/usr/bin/env python3
"""
tools/scrape_marlo_beauty.py (v2.0)
-----------------------------------
Live Product-Page Catalog Scraper for Marlo Beauty Supply (marlobeauty.com)
- Authenticates using MARLO_USER & MARLO_PASS from Railway environment variables (with login check)
- Harvests all product URLs across Shopify product sitemaps (supports --max-items 0 for full catalog)
- Normalizes multi-pack/box pricing to true per-unit COGS (+ inbound freight)
- Enforces 5-Layer 3rd-Party / Amazon / eBay / Salon-Only Restriction Shield + Unicode IP blocklist
- Filters aerosol/flammable/oxidizer beauty hazmat items before indexing
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
    format="%(asctime)s [%(levelname)s] MarloBeautyScraper: %(message)s"
)
logger = logging.getLogger("MarloBeautyScraper")

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
if not DATA_DIR.exists():
    DATA_DIR = ROOT_DIR / "data"

INCOMING_DIR = DATA_DIR / "catalogs" / "wholesale_catalogs" / "incoming"
INDEXED_DIR = DATA_DIR / "catalogs" / "wholesale_catalogs" / "indexed"
INCOMING_DIR.mkdir(parents=True, exist_ok=True)
INDEXED_DIR.mkdir(parents=True, exist_ok=True)

OUT_CSV = INCOMING_DIR / "Marlo_Beauty_Catalog.csv"
INDEXED_CSV = INDEXED_DIR / "Marlo_Beauty_Catalog.csv"
REJECT_LOG_CSV = DATA_DIR / "reports" / "marlo_beauty_restricted_skipped.csv"
REJECT_LOG_CSV.parent.mkdir(parents=True, exist_ok=True)
BLOCKLIST_PATH = DATA_DIR / "ip_blocklist.json"

SITEMAP_URL = "https://www.marlobeauty.com/sitemap.xml"
XML_NS = {"ns": "http://www.sitemaps.org/schemas/sitemap/0.9"}

HAZMAT_KEYWORDS = (
    "aerosol", "flammable", "acetone", "peroxide", "developer 20", "developer 30",
    "developer 40", "20 volume", "30 volume", "40 volume", "pressurized",
    "monomer", "ethyl methacrylate", "barbicide", "bleach powder", "lightener powder",
    "alcohol 99%", "isopropyl alcohol", "combustible"
)

MARKETPLACE_RESTRICTION_PATTERNS = [
    r"not\s+(?:eligible|allowed|permitted|authorized|approved)\s+(?:for|to\s+sell\s+on|on)\s+(?:amazon|ebay|walmart|3rd\s*party|third\s*party|online\s+marketplace)",
    r"no\s+(?:amazon|ebay|walmart|3rd\s*party|third\s*party|online\s+marketplace)",
    r"(?:amazon|ebay|3rd\s*party|third\s*party)\s+(?:sales\s+)?(?:are\s+)?(?:not\s+allowed|not\s+permitted|not\s+eligible|prohibited|restricted|forbidden|blocked)",
    r"prohibited\s+(?:from|on)\s+(?:amazon|ebay|walmart|3rd\s*party|third\s*party|online\s+marketplace)",
    r"restricted\s+(?:from|on)\s+(?:amazon|ebay|walmart|3rd\s*party|third\s*party|online\s+marketplace)",
    r"(?:for\s+)?(?:licensed\s+)?(?:salon|professional)\s+use\s+only\s*[-–—:]\s*not\s+for\s+(?:retail\s+)?resale",
    r"not\s+for\s+(?:online\s+)?resale",
    r"cannot\s+be\s+(?:sold|listed)\s+on\s+(?:amazon|ebay|3rd\s*party|third\s*party)",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def norm_brand(b: Any) -> str:
    s = unicodedata.normalize("NFKD", str(b or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", s.upper().strip())


def load_blocked_brands() -> Tuple[Set[str], List[str]]:
    if not BLOCKLIST_PATH.exists():
        return set(), []
    try:
        raw = json.loads(BLOCKLIST_PATH.read_text(encoding="utf-8"))
        items = raw if isinstance(raw, list) else raw.get("brands", [])
        blocked = {norm_brand(b) for b in items if b}
        multi_word = [b for b in sorted(blocked) if len(b) >= 4]
        return blocked, multi_word
    except Exception:
        return set(), []


def is_brand_blocked(brand: str, blocked: Set[str], multi_word: List[str]) -> bool:
    b = norm_brand(brand)
    if not b or len(b) < 2:
        return True
    if b in blocked:
        return True
    for entry in multi_word:
        if b == entry or b.startswith(entry + " ") or (" " + entry) in b:
            return True
    return False


def get_with_backoff(session: requests.Session, url: str, timeout: int = 20) -> Optional[requests.Response]:
    for attempt in range(1, 4):
        try:
            resp = session.get(url, timeout=timeout, allow_redirects=True)
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


def init_marlo_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    user = os.getenv("MARLO_USER", "").strip()
    pwd = os.getenv("MARLO_PASS", "").strip()
    if user and pwd:
        try:
            login_url = "https://www.marlobeauty.com/account/login"
            session.get(login_url, timeout=15)
            post_resp = session.post(
                login_url,
                data={"form_type": "customer_login", "utf8": "✓", "customer[email]": user, "customer[password]": pwd},
                headers={"Referer": login_url},
                timeout=20,
                allow_redirects=True,
            )
            if "/account" in post_resp.url and "/login" not in post_resp.url:
                logger.info("Authenticated Marlo Beauty pro session verified.")
            else:
                logger.info("Marlo Beauty session initialized (continuing with public/pro catalog access).")
        except Exception as exc:
            logger.warning(f"Marlo Beauty login notice: {exc}")
    return session


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


def discover_product_urls(session: requests.Session, max_urls: int = 0) -> List[str]:
    urls: List[str] = []
    try:
        r = get_with_backoff(session, SITEMAP_URL, timeout=25)
        if r is None or r.status_code != 200:
            return []
        root = ET.fromstring(r.content)
        sub_maps = [
            loc.text.strip()
            for loc in root.findall(".//ns:sitemap/ns:loc", XML_NS)
            if loc.text and "product" in loc.text.lower()
        ]
        target_maps = sub_maps if sub_maps else [SITEMAP_URL]
        for sm in target_maps:
            sr = get_with_backoff(session, sm, timeout=25)
            if sr is None or sr.status_code != 200:
                continue
            sroot = ET.fromstring(sr.content)
            for loc in sroot.findall(".//ns:url/ns:loc", XML_NS):
                if loc.text:
                    u = loc.text.strip()
                    if "/products/" in u or "/p/" in u:
                        urls.append(u)
                        if max_urls > 0 and len(urls) >= max_urls:
                            return list(dict.fromkeys(urls))
    except Exception as exc:
        logger.warning(f"Marlo Beauty sitemap notice: {exc}")
    return list(dict.fromkeys(urls))


def parse_product_page(
    url: str,
    session: requests.Session,
    blocked: Set[str],
    blocked_multi: List[str],
    freight_per_unit: float = 0.65,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, str]]]:
    resp = get_with_backoff(session, url, timeout=20)
    if resp is None or resp.status_code != 200:
        return None, None

    html = resp.text
    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text("\n", strip=True)
    lower_text = page_text.lower()

    if "out of stock" in lower_text and "add to cart" not in lower_text:
        return None, None

    slug_mpn = url.strip("/").split("/")[-1][:32].upper()
    allowed, reason = check_marketplace_eligibility(soup, html)
    if not allowed:
        return None, {"sku": slug_mpn, "url": url, "reason": reason}

    title, brand, mpn, upc, price = "", "", "", "", 0.0

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

    if not brand:
        b_el = soup.select_one(".product__vendor, .vendor, [itemprop='brand']")
        brand = b_el.get_text(strip=True) if b_el else ""

    if not brand or is_brand_blocked(brand, blocked, blocked_multi):
        return None, None

    if any(k in title.lower() for k in HAZMAT_KEYWORDS):
        return None, None

    if price <= 0:
        for sel in (".price__regular .price-item", ".price", ".product__price", "[data-product-price]"):
            p_el = soup.select_one(sel)
            if p_el:
                m = re.search(r"\$([0-9,]+\.[0-9]{2})", p_el.get_text())
                if m:
                    price = float(m.group(1).replace(",", ""))
                    if price > 0:
                        break

    if not upc:
        m_upc = re.search(r"\b(?:UPC|GTIN|Barcode)\s*:?\s*([0-9]{12,13})\b", page_text, re.IGNORECASE | re.MULTILINE)
        if not m_upc:
            m_upc = re.search(r"\b(?:UPC|GTIN|barcode)[^0-9]{0,25}([0-9]{12,13})\b", html, re.IGNORECASE | re.DOTALL)
        if m_upc:
            upc = m_upc.group(1)

    if not title or price <= 0:
        return None, None

    pack_qty = 1
    m_pk = re.search(
        r"(?:pack\s*of|pk\s*of|box\s*of|case\s*of)\s*:?\s*(\d+)|\b(\d+)[-\s]?(?:pack|pk|ct|count)\b",
        title,
        re.IGNORECASE,
    )
    if m_pk:
        val = int(m_pk.group(1) or m_pk.group(2))
        if 1 < val <= 48:
            pack_qty = val

    unit_cogs = round((price / pack_qty) + freight_per_unit, 2)
    final_mpn = (mpn or slug_mpn).upper()

    return {
        "vendor": "MARLO BEAUTY",
        "brand": norm_brand(brand),
        "mpn": final_mpn,
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


def run_marlo_beauty_ingestion(
    max_items: int = 0,
    freight_per_unit: float = 0.65,
    delay_sec: float = 0.4,
) -> int:
    logger.info("Starting live Marlo Beauty Supply catalog scrape...")
    session = init_marlo_session()
    blocked, blocked_multi = load_blocked_brands()

    urls = discover_product_urls(session, max_urls=(max_items * 2 if max_items > 0 else 0))
    target_urls = urls[:max_items] if max_items > 0 else urls
    logger.info(f"Discovered {len(urls)} Marlo Beauty product URLs. Harvesting {len(target_urls)}...")

    approved_records: List[Dict[str, Any]] = []
    rejected_records: List[Dict[str, str]] = []

    try:
        for idx, url in enumerate(target_urls, 1):
            rec, rej = parse_product_page(
                url, session, blocked, blocked_multi, freight_per_unit=freight_per_unit
            )
            if rec:
                approved_records.append(rec)
                if len(approved_records) % 25 == 0:
                    saved_cnt = flush_outputs(approved_records, rejected_records)
                    logger.info(f"[{idx}/{len(target_urls)}] Verified {saved_cnt} IP-clean Marlo Beauty SKUs...")
            elif rej:
                rejected_records.append(rej)
                logger.info(f"[EXCLUDED - MARKETPLACE RESTRICTED] {rej['sku']} -> {rej['reason']}")
            time.sleep(delay_sec)
    finally:
        final_cnt = flush_outputs(approved_records, rejected_records)
        if rejected_records:
            logger.info(f"Saved {len(rejected_records)} excluded Marlo Beauty restricted SKUs -> {REJECT_LOG_CSV}")
        if final_cnt > 0:
            logger.info(f"Saved {final_cnt} IP-clean Marlo Beauty SKUs -> {OUT_CSV} & {INDEXED_CSV}")

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
    parser = argparse.ArgumentParser(description="Marlo Beauty Live Product Scraper v2.0")
    parser.add_argument("--max-items", type=int, default=0, help="Max product pages to crawl (0 = all)")
    parser.add_argument("--freight-per-unit", type=float, default=0.65, help="Estimated inbound freight per unit")
    parser.add_argument("--delay", type=float, default=0.4, help="Delay in seconds between requests")
    parser.add_argument("--run-pipeline", action="store_true", help="Run Keepa UPC matcher and Google Sheets sync after scraping")
    args = parser.parse_args()

    count = run_marlo_beauty_ingestion(
        max_items=args.max_items,
        freight_per_unit=args.freight_per_unit,
        delay_sec=args.delay,
    )
    if args.run_pipeline and count > 0:
        trigger_downstream_match_and_sync()
    sys.exit(0 if count > 0 else 1)
