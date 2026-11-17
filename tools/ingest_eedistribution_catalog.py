#!/usr/bin/env python3
"""
tools/ingest_eedistribution_catalog.py (Checklist Item #333)
------------------------------------------------------------
Authenticated EE Distribution Wholesale Portal Scraper.
Writes normalized Parquet directly to:
  /app/data/catalogs/wholesale_catalogs/indexed/ee_distribution_normalized.parquet
so IndustrialCatalogEngine (core/engine.py) and CrossCatalogMatcherBot
(tools/auto_cross_catalog_matcher_daemon.py) automatically index and match every UPC & MPN.
"""

import os
import re
import sys
import json
import time
import random
import logging
import argparse
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import httpx
import pandas as pd
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

load_dotenv(ROOT_DIR / ".env", override=True)

from core.engine import is_over_weight_limit

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] EEDistScraper: %(message)s"
)
logger = logging.getLogger("EEDistScraper")

BASE_URL = "https://www.eedistribution.com"
LOGIN_URL = f"{BASE_URL}/Login"

DEFAULT_INDEXED_DIR = Path(os.getenv("INDEXED_CATALOG_DIR", "/app/data/catalogs/wholesale_catalogs/indexed"))
if not DEFAULT_INDEXED_DIR.parent.exists():
    DEFAULT_INDEXED_DIR = ROOT_DIR / "data" / "catalogs" / "wholesale_catalogs" / "indexed"

DEFAULT_BLOCKLIST_PATH = Path(os.getenv("IP_BLOCKLIST_PATH", "/app/data/ip_blocklist.json"))
if not DEFAULT_BLOCKLIST_PATH.exists():
    DEFAULT_BLOCKLIST_PATH = ROOT_DIR / "data" / "ip_blocklist.json"

TARGET_BRAND_SLUGS = [
    "funko",
    "hasbro",
    "mattel",
    "bandai",
    "mcfarlane-toys",
    "neca",
    "jazwares",
    "spin-master",
    "super7",
    "diamond-select-toys",
]


def load_blocked_brands() -> set:
    if not DEFAULT_BLOCKLIST_PATH.exists():
        return set()
    try:
        raw = json.loads(DEFAULT_BLOCKLIST_PATH.read_text(encoding="utf-8"))
        items = raw if isinstance(raw, list) else raw.get("brands", [])
        return {str(b).upper().strip() for b in items if b}
    except Exception as exc:
        logger.warning(f"Failed loading ip_blocklist.json: {exc}")
        return set()


def clean_alnum(val: Any) -> str:
    if val is None:
        return ""
    s = str(val).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def parse_pack_qty(text: str) -> int:
    if not text:
        return 1
    m = re.search(r"(?:case\s*of|pack\s*of|qty[:\s]*)(\d+)", text, re.IGNORECASE)
    if m:
        qty = int(m.group(1))
        return qty if 1 <= qty <= 48 else 1
    m2 = re.search(r"\b(\d+)[-\s]?pack\b", text, re.IGNORECASE)
    if m2:
        qty = int(m2.group(1))
        return qty if 1 <= qty <= 48 else 1
    return 1


def parse_price(text: str) -> Optional[float]:
    if not text:
        return None
    m = re.search(r"\$\s*([0-9,]+\.\d{2})", text)
    if not m:
        return None
    return float(m.group(1).replace(",", ""))


def authenticate_eedistribution(email: str, password: str) -> httpx.Client:
    logger.info(f"Launching Playwright auth session for {email}...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()
        page.goto(LOGIN_URL, wait_until="networkidle", timeout=45000)

        page.locator("input[name*='Email'], input[type='email'], #Email").first.fill(email)
        page.locator("input[name*='Password'], input[type='password'], #Password").first.fill(password)
        page.locator("button[type='submit'], input[type='submit']").first.click()
        page.wait_for_load_state("networkidle", timeout=45000)
        time.sleep(2.0)

        if "/login" in page.url.lower():
            browser.close()
            raise RuntimeError("EE Distribution login failed. Verify EE_DIST_EMAIL and EE_DIST_PASSWORD.")

        cookies = context.cookies()
        browser.close()

    jar = httpx.Cookies()
    for c in cookies:
        jar.set(c["name"], c["value"], domain=c.get("domain", ""), path=c.get("path", "/"))

    client = httpx.Client(
        cookies=jar,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        follow_redirects=True,
        timeout=30.0,
    )
    logger.info("Authenticated EE Distribution httpx session active.")
    return client


def parse_product_card(
    card: BeautifulSoup,
    default_brand: str,
    est_freight_per_unit: float,
    blocked_brands: set,
) -> Optional[Dict[str, Any]]:
    full_text = card.get_text(" ", strip=True)
    upper_text = full_text.upper()

    # Strictly filter to IN-STOCK items only
    if any(flag in upper_text for flag in ("PRE-ORDER", "PREORDER", "COMING SOON", "SOLD OUT", "OUT OF STOCK", "BACKORDER")):
        return None
    if "IN STOCK" not in upper_text and "ADD TO CART" not in upper_text:
        return None

    link_el = card.select_one("a[href*='/Product/'], a.product-title, h2 a, h3 a")
    if not link_el:
        return None
    title = link_el.get_text(" ", strip=True)
    product_url = urljoin(BASE_URL, link_el.get("href", ""))

    # 45-lb standard parcel guardrail (core/engine.py)
    over_wt, _ = is_over_weight_limit(title=title)
    if over_wt:
        return None

    brand_el = card.select_one(".brand, .manufacturer, [class*='brand']")
    brand = (brand_el.get_text(" ", strip=True) if brand_el else default_brand.replace("-", " ").title()).strip()
    if brand.upper() in blocked_brands:
        return None

    sku_match = re.search(r"(?:Item\s*#?|SKU[:\s]*)([A-Z0-9\-]{4,20})", full_text, re.IGNORECASE)
    sku = sku_match.group(1).upper() if sku_match else product_url.rstrip("/").split("/")[-1].upper()

    upc_match = re.search(r"\b(\d{12,13})\b", full_text)
    upc = upc_match.group(1) if upc_match else ""

    price_el = card.select_one(".price, .wholesale-price, .your-price, [class*='price']")
    case_price = parse_price(price_el.get_text(" ", strip=True) if price_el else full_text)
    if not case_price or case_price <= 0:
        return None

    pack_qty = parse_pack_qty(full_text)
    unit_cost = round((case_price / pack_qty) + est_freight_per_unit, 2)
    clean_m = clean_alnum(sku)

    return {
        "vendor": "EE DISTRIBUTION",
        "brand": brand,
        "title": title,
        "sku": sku,
        "mpn": sku,
        "clean_mpn": clean_m,
        "root_mpn": clean_m,
        "upc_raw": upc,
        "upc": upc,
        "gtin14": upc.zfill(14) if upc else "",
        "pack_qty": pack_qty,
        "case_price": case_price,
        "unit_cost": unit_cost,
        "raw_cost": unit_cost,
        "weight": 1.0,
        "availability": "IN_STOCK",
        "product_url": product_url,
    }


def enrich_detail_page(client: httpx.Client, item: Dict[str, Any], est_freight: float) -> Dict[str, Any]:
    if item.get("upc") and item.get("pack_qty", 1) > 1:
        return item
    try:
        time.sleep(random.uniform(1.2, 2.2))
        resp = client.get(item["product_url"])
        if resp.status_code != 200:
            return item
        soup = BeautifulSoup(resp.text, "html.parser")
        body_text = soup.get_text(" ", strip=True)

        upc_m = re.search(r"UPC[:\s#]*(\d{12,13})", body_text, re.IGNORECASE)
        if upc_m:
            u = upc_m.group(1)
            item["upc_raw"] = u
            item["upc"] = u
            item["gtin14"] = u.zfill(14)

        mpn_m = re.search(r"(?:Manufacturer\s*#?|MPN[:\s]*)([A-Z0-9\-]{3,25})", body_text, re.IGNORECASE)
        if mpn_m:
            mpn_val = mpn_m.group(1).upper()
            item["mpn"] = mpn_val
            item["clean_mpn"] = clean_alnum(mpn_val)
            item["root_mpn"] = clean_alnum(mpn_val)

        pack_qty = parse_pack_qty(body_text)
        if pack_qty > 1:
            item["pack_qty"] = pack_qty
            item["unit_cost"] = round((item["case_price"] / pack_qty) + est_freight, 2)
            item["raw_cost"] = item["unit_cost"]
    except Exception as exc:
        logger.debug(f"Detail enrichment notice for {item['sku']}: {exc}")
    return item


def scrape_ee_catalog(
    brands: List[str],
    max_pages: int = 4,
    est_freight_per_unit: float = 0.85,
    enrich_missing_upc: bool = True,
    output_parquet: Optional[Path] = None,
) -> Path:
    email = os.getenv("EE_DIST_EMAIL", "").strip()
    password = os.getenv("EE_DIST_PASSWORD", "").strip()
    if not email or not password:
        raise EnvironmentError("Missing EE_DIST_EMAIL or EE_DIST_PASSWORD in .env / Railway environment.")

    blocked_brands = load_blocked_brands()
    client = authenticate_eedistribution(email, password)
    all_rows: List[Dict[str, Any]] = []

    try:
        for slug in brands:
            for page_num in range(1, max_pages + 1):
                url = f"{BASE_URL}/Brand/{slug}?inStockOnly=true&page={page_num}"
                logger.info(f"Scraping {slug} page {page_num}: {url}")
                resp = client.get(url)
                if resp.status_code != 200:
                    logger.warning(f"Stopping {slug} at page {page_num} (HTTP {resp.status_code})")
                    break

                soup = BeautifulSoup(resp.text, "html.parser")
                cards = soup.select(".product-item, .product-card, .item-box, [data-product-id]")
                if not cards:
                    break

                page_hits = 0
                for card in cards:
                    parsed = parse_product_card(card, slug, est_freight_per_unit, blocked_brands)
                    if not parsed:
                        continue
                    if enrich_missing_upc and not parsed.get("upc"):
                        parsed = enrich_detail_page(client, parsed, est_freight_per_unit)
                    if parsed.get("upc") or parsed.get("clean_mpn"):
                        all_rows.append(parsed)
                        page_hits += 1

                logger.info(f"{slug} page {page_num}: captured {page_hits} in-stock items.")
                time.sleep(random.uniform(1.5, 2.8))
    finally:
        client.close()

    DEFAULT_INDEXED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = output_parquet or (DEFAULT_INDEXED_DIR / "ee_distribution_normalized.parquet")

    if not all_rows:
        logger.warning("No new in-stock rows scraped; existing Parquet left untouched.")
        return out_path

    df_new = pd.DataFrame(all_rows)
    if out_path.exists():
        try:
            df_old = pd.read_parquet(out_path)
            df_new = pd.concat([df_old, df_new], ignore_index=True).drop_duplicates(subset=["sku"], keep="last")
        except Exception as exc:
            logger.warning(f"Could not merge existing {out_path.name}: {exc}")

    df_new.to_parquet(out_path, index=False)
    logger.info(f"Saved {len(df_new):,} normalized EE Distribution SKUs to {out_path}")
    return out_path


def main():
    parser = argparse.ArgumentParser(description="Scrape In-Stock EE Distribution catalog into normalized Parquet.")
    parser.add_argument("--brands", nargs="*", default=TARGET_BRAND_SLUGS, help="EE Distribution brand slugs")
    parser.add_argument("--max-pages", type=int, default=4, help="Max pages per brand")
    parser.add_argument("--freight-per-unit", type=float, default=0.85, help="Estimated inbound freight/unit ($)")
    parser.add_argument("--no-detail-enrich", action="store_true", help="Skip detail page lookups for missing UPCs")
    args = parser.parse_args()

    scrape_ee_catalog(
        brands=args.brands,
        max_pages=args.max_pages,
        est_freight_per_unit=args.freight_per_unit,
        enrich_missing_upc=not args.no_detail_enrich,
    )


if __name__ == "__main__":
    main()
