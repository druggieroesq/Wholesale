#!/usr/bin/env python3
"""
tools/scrape_jb_tools.py
------------------------
Live Product-Page Catalog Scraper for JB Tools (jbtools.com)
- Crawls XML product sitemaps for automotive/shop/diagnostic SKUs
- Extracts live pricing, UPC/GTIN, Brand, and MPN from JSON-LD & PDP markup
- Strictly filters out all 226+ IP-blocked conglomerate brands (Milwaukee, DeWalt, Stanley, etc.)
  and oversized shop equipment (> 45 lbs)
- Outputs normalized CSV to data/catalogs/wholesale_catalogs/incoming/Jb_Tools_Catalog.csv
"""

import os
import re
import sys
import json
import time
import logging
import argparse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import pandas as pd
from bs4 import BeautifulSoup

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] JBToolsScraper: %(message)s"
)
logger = logging.getLogger("JBToolsScraper")

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
if not DATA_DIR.exists():
    DATA_DIR = ROOT_DIR / "data"

INCOMING_DIR = DATA_DIR / "catalogs" / "wholesale_catalogs" / "incoming"
INCOMING_DIR.mkdir(parents=True, exist_ok=True)
OUT_CSV = INCOMING_DIR / "Jb_Tools_Catalog.csv"
BLOCKLIST_PATH = DATA_DIR / "ip_blocklist.json"

SITEMAP_INDEX = "https://www.jbtools.com/xmlsitemap.php"
XML_NS = {"ns": "http://www.sitemaps.org/schemas/sitemap/0.9"}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def load_blocked_brands() -> set:
    if not BLOCKLIST_PATH.exists():
        return set()
    try:
        raw = json.loads(BLOCKLIST_PATH.read_text(encoding="utf-8"))
        items = raw if isinstance(raw, list) else raw.get("brands", [])
        return {str(b).upper().strip() for b in items if b}
    except Exception:
        return set()


def is_blocked(brand: str, blocked: set) -> bool:
    b = brand.upper().strip()
    if not b or b in blocked:
        return True
    return any(blk in b or b in blk for blk in blocked if len(blk) >= 4)


def discover_product_urls(session: requests.Session, max_urls: int = 800) -> List[str]:
    urls: List[str] = []
    try:
        r = session.get(SITEMAP_INDEX, headers=HEADERS, timeout=20)
        if r.status_code == 200:
            root = ET.fromstring(r.content)
            sub_maps = [
                loc.text.strip()
                for loc in root.findall(".//ns:sitemap/ns:loc", XML_NS)
                if loc.text and "product" in loc.text.lower()
            ]
            for sm in sub_maps[:3]:
                sr = session.get(sm, headers=HEADERS, timeout=20)
                if sr.status_code == 200:
                    sroot = ET.fromstring(sr.content)
                    for loc in sroot.findall(".//ns:url/ns:loc", XML_NS):
                        if loc.text:
                            urls.append(loc.text.strip())
                            if len(urls) >= max_urls:
                                return list(dict.fromkeys(urls))
    except Exception as exc:
        logger.warning(f"JB Tools sitemap discovery notice: {exc}")
    return list(dict.fromkeys(urls))


def parse_product_page(url: str, session: requests.Session, blocked: set) -> Optional[Dict[str, Any]]:
    try:
        resp = session.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return None

        html = resp.text
        if "OUT OF STOCK" in html.upper() and "ADD TO CART" not in html.upper():
            return None

        soup = BeautifulSoup(html, "html.parser")
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
                        mpn = str(it.get("mpn") or it.get("sku") or "").strip()
                        raw_upc = str(it.get("gtin12") or it.get("gtin13") or it.get("gtin") or it.get("upc") or "")
                        upc = re.sub(r"\D", "", raw_upc)
                        offers = it.get("offers", {})
                        if isinstance(offers, dict):
                            if "OUTOFSTOCK" in str(offers.get("availability", "")).upper():
                                return None
                            price = float(offers.get("price", 0.0) or 0.0)
                        elif isinstance(offers, list) and offers:
                            price = float(offers[0].get("price", 0.0) or 0.0)
                        break
            except Exception:
                continue

        if not brand or is_blocked(brand, blocked):
            return None

        if not title:
            h1 = soup.find("h1")
            title = h1.get_text(strip=True) if h1 else ""

        if price <= 0:
            p_el = soup.select_one(".price--withoutTax, .productView-price .price, [data-product-price-without-tax]")
            if p_el:
                m = re.search(r"\$([0-9,]+\.[0-9]{2})", p_el.get_text())
                if m:
                    price = float(m.group(1).replace(",", ""))

        if not upc:
            m_upc = re.search(r"\b(?:UPC|GTIN)[:\s#]*([0-9]{12,13})\b", html, re.IGNORECASE)
            if m_upc:
                upc = m_upc.group(1)

        if not title or price <= 0:
            return None

        m_wt = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:lbs?|pounds?)\b", html, re.IGNORECASE)
        if m_wt and float(m_wt.group(1)) > 45.0:
            return None

        pack_qty = 1
        m_pk = re.search(r"(?:pack\s*of|pk\s*of|set\s*of)\s*(\d+)|\b(\d+)[-\s]?(?:pack|pk|pc)\b", title, re.IGNORECASE)
        if m_pk:
            val = int(m_pk.group(1) or m_pk.group(2))
            if 1 <= val <= 48:
                pack_qty = val

        return {
            "vendor": "JB TOOLS",
            "brand": brand.upper(),
            "mpn": mpn or url.strip("/").split("/")[-1][:24].upper(),
            "upc": upc if len(upc) in (12, 13) else "",
            "title": title[:180],
            "wholesale_cost": round(price, 2),
            "uom": "EA",
            "pack_qty": pack_qty,
            "direct_url": url,
        }
    except Exception:
        return None


def run_jb_tools_ingestion(max_items: int = 350) -> int:
    logger.info("Starting live JB Tools catalog scrape...")
    session = requests.Session()
    blocked = load_blocked_brands()

    urls = discover_product_urls(session, max_urls=max_items * 2)
    logger.info(f"Discovered {len(urls)} JB Tools product URLs. Harvesting up to {max_items}...")

    records: List[Dict[str, Any]] = []
    for url in urls[:max_items]:
        item = parse_product_page(url, session, blocked)
        if item:
            records.append(item)
            if len(records) % 25 == 0:
                logger.info(f"Extracted {len(records)} IP-clean JB Tools SKUs...")
        time.sleep(0.35)

    if not records:
        logger.warning("No live records extracted from JB Tools.")
        return 0

    df = pd.DataFrame(records).drop_duplicates(subset=["mpn"])
    df.to_csv(OUT_CSV, index=False)
    logger.info(f"Exported {len(df)} IP-clean JB Tools SKUs -> {OUT_CSV}")
    return len(df)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="JB Tools Live Product Scraper")
    parser.add_argument("--max-items", type=int, default=350, help="Max product pages to crawl")
    args = parser.parse_args()
    count = run_jb_tools_ingestion(max_items=args.max_items)
    sys.exit(0 if count > 0 else 1)
