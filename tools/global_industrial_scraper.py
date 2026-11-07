#!/usr/bin/env python3
"""
tools/global_industrial_scraper.py
----------------------------------
Global Industrial (https://www.globalindustrial.com) Catalog Harvester.
Crawls key MRO, Material Handling, and Safety equipment categories,
extracts Model Numbers, UPCs, Brands, Titles, and Live Prices, and outputs to:
/app/data/catalogs/incoming/Global_Industrial_Catalog.csv
"""

import os
import sys
import re
import json
import time
import logging
from pathlib import Path
from typing import Dict, List, Optional
import requests
from bs4 import BeautifulSoup
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] GlobalIndustrialScraper: %(message)s"
)
logger = logging.getLogger("GlobalIndustrialScraper")

OUT_DIR = ROOT_DIR / "data" / "catalogs" / "incoming"
OUT_FILE = OUT_DIR / "Global_Industrial_Catalog.csv"
BASE_URL = "https://www.globalindustrial.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

SEED_CATEGORIES = [
    "https://www.globalindustrial.com/c/storage",
    "https://www.globalindustrial.com/c/material-handling",
    "https://www.globalindustrial.com/c/safety-security",
    "https://www.globalindustrial.com/c/tools-instruments",
    "https://www.globalindustrial.com/c/janitorial-maintenance",
    "https://www.globalindustrial.com/c/hvac",
]


def harvest_category_urls(cat_url: str, session: requests.Session, max_pages: int = 4) -> List[str]:
    product_urls = []
    for page in range(1, max_pages + 1):
        url = f"{cat_url}?page={page}"
        logger.info(f"Scanning category: {url}")
        try:
            r = session.get(url, headers=HEADERS, timeout=15)
            if r.status_code != 200:
                break
            soup = BeautifulSoup(r.text, "html.parser")
            links = soup.select("a[data-item-id], .product-title a, a.product-name, h3.title a")
            found = 0
            for a in links:
                href = a.get("href", "")
                if href:
                    if href.startswith("/"):
                        href = f"{BASE_URL}{href}"
                    if href.startswith(BASE_URL) and href not in product_urls:
                        if "/p/" in href or "/product/" in href:
                            product_urls.append(href)
                            found += 1
            if found == 0:
                break
            time.sleep(0.5)
        except Exception as e:
            logger.warning(f"Error accessing {url}: {e}")
            break
    return product_urls


def parse_product(url: str, session: requests.Session) -> Optional[Dict]:
    try:
        r = session.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "html.parser")

        title, brand, upc, sku, price = "", "Global Industrial", "", "", 0.0

        for script in soup.find_all("script", type="application/ld+json"):
            if not script.string:
                continue
            try:
                data = json.loads(script.string)
                items = data if isinstance(data, list) else [data]
                for it in items:
                    if isinstance(it, dict) and it.get("@type") == "Product":
                        title = it.get("name", "")
                        b_val = it.get("brand", "")
                        brand = b_val.get("name", "") if isinstance(b_val, dict) else (b_val or brand)
                        sku = it.get("sku", "") or it.get("mpn", "")
                        upc = it.get("gtin12") or it.get("gtin13") or it.get("gtin") or it.get("upc") or ""
                        offers = it.get("offers", {})
                        if isinstance(offers, dict):
                            price = float(offers.get("price", 0.0) or 0.0)
                        elif isinstance(offers, list) and offers:
                            price = float(offers[0].get("price", 0.0) or 0.0)
                        break
            except Exception:
                continue

        if not title:
            h1 = soup.find("h1")
            title = h1.text.strip() if h1 else ""

        if not price:
            p_elem = soup.select_one(".price, .pdp-price, [data-price], .item-price")
            if p_elem:
                m = re.search(r"\$([0-9,]+\.[0-9]{2})", p_elem.text)
                if m:
                    price = float(m.group(1).replace(",", ""))

        if not sku:
            m = re.search(r"Model\s*#?\s*:?\s*([A-Za-z0-9_-]+)", r.text, re.IGNORECASE)
            if m:
                sku = m.group(1)

        if not upc:
            m = re.search(r"UPC\s*#?\s*:?\s*([0-9]{12})", r.text, re.IGNORECASE)
            if m:
                upc = m.group(1)

        if title and price > 0:
            return {
                "Vendor": "GLOBAL INDUSTRIAL",
                "Vendor_SKU": sku or url.strip("/").split("/")[-1].replace(".html", ""),
                "UPC": upc,
                "Brand": brand,
                "Product_Title": title,
                "Wholesale_Cost": price,
                "Direct_URL": url,
            }
    except Exception:
        pass
    return None


def run_global_industrial_crawl(max_products: int = 1500):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    all_urls = []

    logger.info("Discovering Global Industrial product URLs from seed MRO categories...")
    for cat in SEED_CATEGORIES:
        urls = harvest_category_urls(cat, session, max_pages=3)
        all_urls.extend(urls)
        time.sleep(0.4)

    all_urls = list(dict.fromkeys(all_urls))
    logger.info(f"Discovered {len(all_urls)} Global Industrial product URLs.")

    records = []
    for idx, u in enumerate(all_urls[:max_products]):
        item = parse_product(u, session)
        if item:
            records.append(item)
            if len(records) % 50 == 0:
                logger.info(f"Collected {len(records)} Global Industrial products...")
        time.sleep(0.3)

    if records:
        df = pd.DataFrame(records)
        df.to_csv(OUT_FILE, index=False)
        logger.info(f"Successfully wrote {len(df)} records to {OUT_FILE}")
    else:
        logger.warning("No records could be extracted from Global Industrial.")


if __name__ == "__main__":
    run_global_industrial_crawl()
