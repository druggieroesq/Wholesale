#!/usr/bin/env python3
"""
tools/ingest_eedistribution_catalog.py (v2.2)
---------------------------------------------
Authenticated EE Distribution Catalog & Strict Marketplace-Restriction Scraper
- Extracts __RequestVerificationToken from https://www.eedistribution.com/ homepage modal
  and POSTs AccNumOrEmail + Password to https://www.eedistribution.com/Account/Login
- Fail-closed authentication check (aborts immediately if login fails so public pages
  without account-level restriction notices are never scraped)
- 5-Layer 3rd-Party / Amazon / eBay Restriction Detector on both listing cards and
  every Product Detail Page (PDP):
    1. Listing grid restriction badges/icons
    2. Alert/warning banners, tooltips (title / aria-label / data-original-title), & restriction divs
    3. Specification table / attribute rows (e.g., "Amazon: No", "3rd Party Sales: Not Allowed")
    4. Full visible page text regex sweep (Amazon, eBay, Walmart, 3rd Party, Brick & Mortar Only)
    5. Embedded JSON / script state flags (amazonRestricted, thirdPartyRestricted, ebayRestricted)
- Logs every excluded SKU + exact trigger phrase to data/reports/ee_marketplace_restricted_skipped.csv
- Normalizes Case Price / Pack Qty to true per-unit COGS (+ inbound freight)
- Writes clean CSV to both incoming/ and indexed/ so all daemons index it immediately
"""

import os
import re
import sys
import json
import time
import shutil
import logging
import argparse
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
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
INDEXED_DIR = DATA_DIR / "catalogs" / "wholesale_catalogs" / "indexed"
INCOMING_DIR.mkdir(parents=True, exist_ok=True)
INDEXED_DIR.mkdir(parents=True, exist_ok=True)

OUT_CSV = INCOMING_DIR / "EEDistribution_Catalog.csv"
INDEXED_CSV = INDEXED_DIR / "EEDistribution_Catalog.csv"
REJECT_LOG_CSV = DATA_DIR / "reports" / "ee_marketplace_restricted_skipped.csv"
REJECT_LOG_CSV.parent.mkdir(parents=True, exist_ok=True)
BLOCKLIST_PATH = DATA_DIR / "ip_blocklist.json"

BASE_URL = "https://www.eedistribution.com"
HOME_URL = f"{BASE_URL}/"
LOGIN_POST_URL = f"{BASE_URL}/Account/Login"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

EE_RESTRICTION_PATTERNS = [
    r"not\s+(?:eligible|allowed|permitted|authorized|approved)\s+(?:for|to\s+sell\s+on|on)\s+(?:amazon|ebay|walmart|3rd\s*party|third\s*party|online\s+marketplace)",
    r"no\s+(?:amazon|ebay|walmart|3rd\s*party|third\s*party|online\s+marketplace)",
    r"(?:amazon|ebay|3rd\s*party|third\s*party)\s+(?:sales\s+)?(?:are\s+)?(?:not\s+allowed|not\s+permitted|not\s+eligible|prohibited|restricted|forbidden|blocked)",
    r"prohibited\s+(?:from|on)\s+(?:amazon|ebay|walmart|3rd\s*party|third\s*party|online\s+marketplace)",
    r"restricted\s+(?:from|on)\s+(?:amazon|ebay|walmart|3rd\s*party|third\s*party|online\s+marketplace)",
    r"brick\s*(?:&|and)\s*mortar\s+(?:stores?\s+)?only",
    r"physical\s+retail\s+store(?:front)?\s+only",
    r"amazon\s+restriction",
    r"ebay\s+restriction",
    r"3rd\s*party\s+marketplace\s+restriction",
    r"third\s*party\s+marketplace\s+restriction",
    r"cannot\s+be\s+(?:sold|listed)\s+on\s+(?:amazon|ebay|3rd\s*party|third\s*party)",
    r"may\s+not\s+be\s+(?:sold|listed)\s+on\s+(?:amazon|ebay|3rd\s*party|third\s*party)",
]

EE_WHITELIST_OVERRIDE = {
    "FUNKO", "HASBRO", "MATTEL", "BANDAI", "MCFARLANE TOYS", "NECA",
    "SPIN MASTER", "JAZWARES", "DIAMOND SELECT", "SUPER7", "LOUNGEFLY",
    "MEZCO", "KOTOBUKIYA", "GOOD SMILE", "MONOGRAM", "ABYSSE"
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
        blocked = {norm_brand(b) for b in items if b} - EE_WHITELIST_OVERRIDE
        multi_word = [b for b in sorted(blocked) if len(b) >= 5]
        return blocked, multi_word
    except Exception:
        return set(), []


def is_brand_blocked(brand: str, blocked: Set[str], multi_word: List[str]) -> bool:
    b = norm_brand(brand)
    if not b or len(b) < 3:
        return True
    if b in EE_WHITELIST_OVERRIDE:
        return False
    if b in blocked:
        return True
    for entry in multi_word:
        if b.startswith(entry + " ") or (" " + entry) in b:
            return True
    return False


def create_authenticated_session() -> requests.Session:
    email = os.getenv("EE_DIST_EMAIL", "").strip()
    password = os.getenv("EE_DIST_PASSWORD", "").strip()
    if not email or not password:
        raise RuntimeError("FAIL-CLOSED: Missing EE_DIST_EMAIL or EE_DIST_PASSWORD in Railway environment variables.")

    session = requests.Session()
    session.headers.update(HEADERS)

    resp = session.get(HOME_URL, timeout=25)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # Locate the /Account/Login form to capture its exact __RequestVerificationToken
    login_form = None
    for form in soup.find_all("form"):
        action = str(form.get("action") or "")
        if "/account/login" in action.lower():
            login_form = form
            break

    payload: Dict[str, str] = {}
    target_scope = login_form if login_form is not None else soup
    for inp in target_scope.find_all("input"):
        name = inp.get("name")
        if name:
            payload[name] = inp.get("value", "")

    payload["AccNumOrEmail"] = email
    payload["Password"] = password
    payload["RememberMe"] = "true"
    payload.setdefault("ReturnUrl", "/")

    post_headers = {
        "Referer": HOME_URL,
        "Origin": BASE_URL,
    }
    post_resp = session.post(LOGIN_POST_URL, data=payload, headers=post_headers, timeout=25, allow_redirects=True)
    body_lower = post_resp.text.lower()

    is_logged_in = (
        any(tok in body_lower for tok in ("logout", "log out", "sign out", "my account", "account/orders", "account/logout"))
        and "accnumoremail" not in body_lower
    )
    if not is_logged_in:
        # Double-check with a GET to homepage to verify session cookie persistence
        check_resp = session.get(HOME_URL, timeout=20)
        check_lower = check_resp.text.lower()
        is_logged_in = any(tok in check_lower for tok in ("logout", "log out", "sign out", "account/logout"))

    if not is_logged_in:
        raise RuntimeError(
            "FAIL-CLOSED: EE Distribution /Account/Login authentication failed (AccNumOrEmail/Password rejected or token invalid). "
            "Refusing to scrape unauthenticated pages where marketplace restrictions may be hidden."
        )

    logger.info("Authenticated EE Distribution wholesale session verified via /Account/Login (FAIL-CLOSED guard passed).")
    return session


def check_ee_marketplace_eligibility(soup: BeautifulSoup, raw_html: str) -> Tuple[bool, str]:
    """
    5-Layer Marketplace Restriction Shield:
    Inspects the EE Distribution page for ANY Amazon, eBay, Walmart, or 3rd-party
    marketplace selling prohibition. Returns (is_allowed, reason).
    """
    # Layer 1: Explicit restriction banners, badges, and warning boxes
    alert_selectors = [
        ".restriction", ".restrictions", ".marketplace-restriction",
        ".product-restrictions", ".alert-warning", ".alert-danger",
        ".notice", ".warning", "[class*='restrict']", "[id*='restrict']",
        "[data-restriction]", "[data-marketplace]"
    ]
    for sel in alert_selectors:
        for el in soup.select(sel):
            txt = " ".join(el.get_text(" ", strip=True).split())
            if not txt:
                continue
            for pat in EE_RESTRICTION_PATTERNS:
                if re.search(pat, txt, re.IGNORECASE):
                    return False, f"Layer1 Badge/Alert: {txt[:130]}"

    # Layer 2: Tooltip attributes (title, aria-label, data-original-title, alt)
    for el in soup.select("[title], [aria-label], [data-original-title], img[alt]"):
        tip = " ".join(
            f"{el.get('title', '')} {el.get('aria-label', '')} {el.get('data-original-title', '')} {el.get('alt', '')}".split()
        )
        if not tip:
            continue
        for pat in EE_RESTRICTION_PATTERNS:
            if re.search(pat, tip, re.IGNORECASE):
                return False, f"Layer2 Tooltip/Icon: {tip[:130]}"

    # Layer 3: Product specification rows / key-value attributes (e.g., "Amazon: No", "3rd Party: Restricted")
    for row in soup.select("tr, li, dl, .spec-row, .product-attribute, .attribute-row"):
        row_txt = " ".join(row.get_text(" ", strip=True).split())
        if not row_txt or len(row_txt) > 240:
            continue
        if re.search(r"\b(?:amazon|ebay|walmart|3rd\s*party|third\s*party|online\s+marketplace)\b", row_txt, re.IGNORECASE):
            if re.search(r"\b(?:no|not\s+allowed|not\s+eligible|not\s+permitted|prohibited|restricted|blocked|ineligible|brick\s*(?:&|and)\s*mortar)\b", row_txt, re.IGNORECASE):
                return False, f"Layer3 Spec Row: {row_txt[:130]}"

    # Layer 4: Full visible page text sweep
    visible_text = " ".join(soup.get_text(" ", strip=True).split())
    for pat in EE_RESTRICTION_PATTERNS:
        m = re.search(pat, visible_text, re.IGNORECASE)
        if m:
            start = max(0, m.start() - 35)
            end = min(len(visible_text), m.end() + 55)
            return False, f"Layer4 Page Text: {visible_text[start:end]}"

    # Layer 5: Embedded JSON / script state flags
    if re.search(
        r'"(?:amazonRestricted|ebayRestricted|thirdPartyRestricted|marketplaceRestricted|noAmazon|noEbay|noThirdParty)"\s*:\s*(?:true|1|"true"|"yes")',
        raw_html,
        re.IGNORECASE,
    ):
        return False, "Layer5 JSON State Flag: marketplaceRestricted=true"

    if re.search(
        r'"(?:amazonAllowed|thirdPartyAllowed|ebayAllowed|eligibleForAmazon)"\s*:\s*(?:false|0|"false"|"no")',
        raw_html,
        re.IGNORECASE,
    ):
        return False, "Layer5 JSON State Flag: marketplaceAllowed=false"

    return True, "ALLOWED_3RD_PARTY"


def discover_brand_product_urls(
    session: requests.Session,
    brand_slug: str,
    max_pages: int = 5,
    rejected_records: Optional[List[Dict[str, str]]] = None,
) -> List[str]:
    urls: List[str] = []
    for page in range(1, max_pages + 1):
        list_url = f"{BASE_URL}/s/{brand_slug}?instock=1&page={page}"
        try:
            r = session.get(list_url, timeout=25)
            if r.status_code != 200:
                break
            soup = BeautifulSoup(r.text, "html.parser")
            found_on_page = 0
            cards = soup.select(".product-item, .product-card, .item-box, [data-productid]")
            if cards:
                for card in cards:
                    a = card.select_one("a[href*='/p/']")
                    if not a:
                        continue
                    full = urljoin(BASE_URL, a.get("href", "").split("?")[0])
                    card_txt = " ".join(card.get_text(" ", strip=True).split())
                    card_restricted = False
                    for pat in EE_RESTRICTION_PATTERNS:
                        if re.search(pat, card_txt, re.IGNORECASE):
                            card_restricted = True
                            if rejected_records is not None:
                                rejected_records.append({
                                    "sku": full.rstrip("/").split("/")[-1],
                                    "url": full,
                                    "reason": f"Grid Card Badge: {card_txt[:120]}"
                                })
                            break
                    if not card_restricted and full not in urls:
                        urls.append(full)
                        found_on_page += 1
            else:
                for a in soup.select("a[href*='/p/']"):
                    href = a.get("href", "").strip()
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
    blocked_brands: Set[str],
    blocked_multi: List[str],
    freight_per_unit: float
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, str]]]:
    try:
        resp = session.get(url, timeout=25)
        if resp.status_code != 200:
            return None, None

        raw_html = resp.text
        soup = BeautifulSoup(raw_html, "html.parser")

        # Verify session is still authenticated on PDP (never evaluate unauthenticated HTML)
        if "logout" not in raw_html.lower() and "log out" not in raw_html.lower() and "sign out" not in raw_html.lower():
            logger.warning(f"Session lost on {url} — skipping.")
            return None, None

        page_upper = soup.get_text(" ", strip=True).upper()
        if "OUT OF STOCK" in page_upper and "IN STOCK" not in page_upper:
            return None, None

        allowed, reason = check_ee_marketplace_eligibility(soup, raw_html)
        if not allowed:
            sku_guess = url.rstrip("/").split("/")[-1]
            return None, {"sku": sku_guess, "url": url, "reason": reason}

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

        if not brand or is_brand_blocked(brand, blocked_brands, blocked_multi):
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

        m_wt = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:lbs?|pounds?)\b", raw_html, re.IGNORECASE)
        if m_wt and float(m_wt.group(1)) > 45.0:
            return None, None

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
            "unit_cost": unit_cogs,
            "case_cost": round(raw_price, 2),
            "uom": "EA",
            "pack_qty": pack_qty,
            "marketplace_status": "ALLOWED_3RD_PARTY",
            "direct_url": url,
        }, None
    except Exception:
        return None, None


def run_ee_ingestion(brands: List[str], max_pages: int = 5, freight_per_unit: float = 0.85) -> int:
    session = create_authenticated_session()
    blocked_brands, blocked_multi = load_blocked_brands()

    all_urls: List[str] = []
    rejected_records: List[Dict[str, str]] = []

    for b in brands:
        logger.info(f"Scanning EE Distribution In-Stock listings for brand: {b}")
        b_urls = discover_brand_product_urls(session, b, max_pages=max_pages, rejected_records=rejected_records)
        logger.info(f"  -> Found {len(b_urls)} candidate product URLs for '{b}'")
        all_urls.extend(b_urls)

    all_urls = list(dict.fromkeys(all_urls))
    logger.info(f"Inspecting {len(all_urls)} EE Distribution PDPs with 5-Layer 3rd-Party Shield...")

    approved_records: List[Dict[str, Any]] = []

    for url in all_urls:
        rec, rej = parse_ee_product_page(url, session, blocked_brands, blocked_multi, freight_per_unit)
        if rec:
            approved_records.append(rec)
            if len(approved_records) % 25 == 0:
                logger.info(f"Verified {len(approved_records)} Amazon/3rd-party-eligible EE SKUs...")
        elif rej:
            rejected_records.append(rej)
            logger.info(f"[EXCLUDED - MARKETPLACE RESTRICTED] {rej['sku']} -> {rej['reason']}")
        time.sleep(0.35)

    if rejected_records:
        pd.DataFrame(rejected_records).drop_duplicates(subset=["url"]).to_csv(REJECT_LOG_CSV, index=False)
        logger.info(f"Logged {len(rejected_records)} excluded EE restricted SKUs -> {REJECT_LOG_CSV}")

    if not approved_records:
        logger.warning("No Amazon-eligible EE Distribution SKUs extracted.")
        return 0

    df = pd.DataFrame(approved_records).drop_duplicates(subset=["mpn"])
    df.to_csv(OUT_CSV, index=False)
    shutil.copy2(OUT_CSV, INDEXED_CSV)
    logger.info(f"Exported {len(df)} verified 3rd-party-eligible EE Distribution SKUs -> {OUT_CSV} & {INDEXED_CSV}")
    return len(df)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Authenticated EE Distribution Scraper v2.2 with 5-Layer Marketplace Gate")
    parser.add_argument(
        "--brands",
        nargs="+",
        default=[
            "funko", "hasbro", "mattel", "bandai", "mcfarlane-toys", "neca",
            "spin-master", "jazwares", "diamond-select", "super7", "loungefly",
            "mezco", "kotobukiya", "good-smile", "monogram", "abysse"
        ],
        help="Brand slugs to crawl on eedistribution.com"
    )
    parser.add_argument("--max-pages", type=int, default=5, help="Max pagination pages per brand")
    parser.add_argument("--freight-per-unit", type=float, default=0.85, help="Estimated inbound freight per unit")
    args = parser.parse_args()

    count = run_ee_ingestion(
        brands=args.brands,
        max_pages=args.max_pages,
        freight_per_unit=args.freight_per_unit
    )
    sys.exit(0 if count > 0 else 1)
