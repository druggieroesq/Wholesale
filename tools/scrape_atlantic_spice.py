#!/usr/bin/env python3
"""
tools/scrape_atlantic_spice.py
------------------------------
Live Product-Page Catalog Scraper for Atlantic Spice Co. (atlanticspice.com)
- Harvests _p_ product URLs from sitemap.xml
- Fetches each product detail page for real live pricing, UPC/GTIN, SKU, and stock status
- Normalizes pack weights (1 lb vs 5 lb bulk bags) and filters IP-blocked brands
- Outputs normalized CSV to data/catalogs/wholesale_catalogs/incoming/Atlantic_Spice_Catalog.csv
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
    format="%(asctime)s [%(levelname)s] AtlanticSpiceScraper: %(message)s"
)
logger = logging.getLogger("AtlanticSpiceScraper")

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
if not DATA_DIR.exists():
    DATA_DIR = ROOT_DIR / "data"

INCOMING_DIR = DATA_DIR / "catalogs" / "wholesale_catalogs" / "incoming"
INCOMING_DIR.mkdir(parents=True, exist_ok=True)
OUT_CSV = INCOMING_DIR / "Atlantic_Spice_Catalog.csv"
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


def load_blocked_brands() -> set:
    if not BLOCKLIST_PATH.exists():
        return set()
    try:
        raw = json.loads(BLOCKLIST_PATH.read_text(encoding="utf-8"))
        items = raw if isinstance(raw, list) else raw.get("brands", [])
        return {str(b).upper().strip() for b in items if b}
    except Exception:
        return set()


def parse_product_page(url: str, session: requests.Session) -> Optional[Dict[str, Any]]:
    try:
        resp = session.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return None

        html = resp.text
        if "OUT OF STOCK" in html.upper() and "ADD TO CART" not in html.upper():
            return None

        soup = BeautifulSoup(html, "html.parser")
        title, brand, mpn, upc, price = "", "ATLANTIC SPICE", "", "", 0.0

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
                        if isinstance(b_val, dict):
                            brand = str(b_val.get("name") or brand).strip()
                        elif b_val:
                            brand = str(b_val).strip()
                        mpn = str(it.get("sku") or it.get("mpn") or "").strip()
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

        if not title:
            h1 = soup.find("h1")
            title = h1.get_text(strip=True) if h1 else ""

        if price <= 0:
            price_el = soup.select_one(".product-price, .price, [data-price], #price")
            if price_el:
                m = re.search(r"\$([0-9,]+\.[0-9]{2})", price_el.get_text())
                if m:
                    price = float(m.group(1).replace(",", ""))

        if not mpn:
            m_id = re.search(r"_p_(\d+)", url.lower())
            mpn = f"ASP-{m_id.group(1)}" if m_id else re.sub(r"[^A-Za-z0-9]", "", title)[:12].upper()

        if not upc:
            m_upc = re.search(r"\b(?:UPC|GTIN)[:\s#]*([0-9]{12,13})\b", html, re.IGNORECASE)
            if m_upc:
                upc = m_upc.group(1)

        if not title or price <= 0:
            return None

        pack_qty = 1
        m_lb = re.search(r"\b(\d+)\s*(?:lb|pound)s?\b", title, re.IGNORECASE)
        if m_lb:
            w = int(m_lb.group(1))
            if w > 45:
                return None
            pack_qty = max(1, min(w, 48))

        return {
            "vendor": "ATLANTIC SPICE",
            "brand": brand.upper(),
            "mpn": mpn,
            "upc": upc if len(upc) in (12, 13) else "",
            "title": title[:180],
            "wholesale_cost": round(price, 2),
            "uom": "EA",
            "pack_qty": pack_qty,
            "direct_url": url,
        }
    except Exception:
        return None


def run_atlantic_spice_ingestion(max_items: int = 350) -> int:
    logger.info(f"Fetching Atlantic Spice sitemap: {SITEMAP_URL}")
    session = requests.Session()
    blocked_brands = load_blocked_brands()

    try:
        resp = session.get(SITEMAP_URL, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as err:
        logger.error(f"Failed to retrieve Atlantic Spice sitemap: {err}")
        return 0

    product_urls: List[str] = []
    for loc in root.findall(".//ns:loc", XML_NS):
        link = (loc.text or "").strip()
        if "_p_" in link.lower() and "_c_" not in link.lower():
            product_urls.append(link)

    product_urls = list(dict.fromkeys(product_urls))
    logger.info(f"Discovered {len(product_urls)} product URLs. Harvesting up to {max_items} live SKUs...")

    records: List[Dict[str, Any]] = []
    for url in product_urls[:max_items]:
        item = parse_product_page(url, session)
        if item and item["brand"] not in blocked_brands:
            records.append(item)
            if len(records) % 25 == 0:
                logger.info(f"Extracted {len(records)} live Atlantic Spice SKUs...")
        time.sleep(0.35)

    if not records:
        logger.warning("No live product records extracted from Atlantic Spice.")
        return 0

    df = pd.DataFrame(records).drop_duplicates(subset=["mpn"])
    df.to_csv(OUT_CSV, index=False)
    logger.info(f"Exported {len(df)} verified Atlantic Spice SKUs -> {OUT_CSV}")
    return len(df)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Atlantic Spice Live Product Scraper")
    parser.add_argument("--max-items", type=int, default=350, help="Max product pages to crawl")
    args = parser.parse_args()
    count = run_atlantic_spice_ingestion(max_items=args.max_items)
    sys.exit(0 if count > 0 else 1)
