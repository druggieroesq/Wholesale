#!/usr/bin/env python3
"""
tools/scrape_jb_tools.py
Production catalog builder for JB Tools via /content/sitemap_index.xml.
Outputs: /app/data/catalogs/wholesale_catalogs/incoming/Jb_Tools_Catalog.csv
"""

import os
import csv
import re
import sys
import time
import subprocess
import xml.etree.ElementTree as ET

OUT_CSV = "/app/data/catalogs/wholesale_catalogs/incoming/Jb_Tools_Catalog.csv"
INDEX_URL = "https://www.jbtools.com/content/sitemap_index.xml"
XML_NS = {"ns": "http://www.sitemaps.org/schemas/sitemap/0.9"}

COMMON_BRANDS = [
    "MILWAUKEE", "DEWALT", "MAKITA", "GEARWRENCH", "LISLE", "OTC",
    "SK TOOLS", "KTI", "K-TOOL", "ASTRO PNEUMATIC", "SUNEX", "GREY PNEUMATIC",
    "IRWIN", "PROTO", "WILLIAMS", "CHANNEL LOCK", "KLEIN", "KNIPEX",
    "STREAMLIGHT", "INGERSOLL RAND", "CHICAGO PNEUMATIC", "TITAN", "OEMTOOLS",
    "MAYHEW", "LANG TOOLS", "SUNEX TOOLS", "CAL-VAN", "TITAN TOOLS"
]

def curl_get(url: str) -> str:
    """Executes curl with desktop user-agent to bypass bot checks."""
    cmd = [
        "curl", "-sSL",
        "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "--compressed", url
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    return res.stdout if res.returncode == 0 else ""

def extract_meta(slug: str):
    slug = slug.strip("/").split("/")[-1].replace(".html", "")
    parts = re.sub(r"[^a-zA-Z0-9\-]", "", slug).split("-")
    brand = "JB TOOLS"
    mpn = parts[1].upper() if len(parts) > 1 else parts[0].upper()
    slug_upper = slug.upper().replace("-", " ")
    for b in COMMON_BRANDS:
        if slug_upper.startswith(b):
            brand = b
            b_words = len(b.split())
            if len(parts) > b_words:
                mpn = parts[b_words].upper()
            break
    title = slug.replace("-", " ").title()
    return brand, mpn, title

def run():
    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    print(f"==> Fetching master index: {INDEX_URL}")
    raw_index = curl_get(INDEX_URL)
    if not raw_index or "<" not in raw_index:
        print("Failed to fetch master index.")
        return 0

    root = ET.fromstring(raw_index.encode("utf-8"))
    sub_maps = [loc.text.strip() for loc in root.findall(".//ns:sitemap/ns:loc", XML_NS) if loc.text]
    print(f"Found {len(sub_maps)} sub-sitemap(s) registered in master index.")

    all_urls = []
    for idx, sm_url in enumerate(sub_maps, 1):
        print(f"[{idx}/{len(sub_maps)}] Downloading: {sm_url}")
        content = curl_get(sm_url)
        if not content:
            continue
        try:
            sub_root = ET.fromstring(content.encode("utf-8"))
            for loc in sub_root.findall(".//ns:url/ns:loc", XML_NS):
                if loc.text:
                    all_urls.append(loc.text.strip())
        except Exception as err:
            print(f"Error parsing {sm_url}: {err}")
        time.sleep(0.2)

    print(f"Extracted {len(all_urls):,} total raw URLs.")

    items = []
    seen = set()
    for u in all_urls:
        if any(x in u for x in ["/categories/", "/brands/", "/blog/", "/about", "/contact", "/shipping", "/account"]):
            continue
        slug = u.rstrip("/").split("/")[-1].replace(".html", "")
        if not slug or slug in seen:
            continue
        seen.add(slug)
        brand, mpn, title = extract_meta(slug)
        items.append({
            "vendor": "JB TOOLS",
            "brand": brand,
            "mpn": mpn,
            "upc": "",
            "title": title,
            "wholesale_cost": 25.00,
            "uom": "EA",
            "pack_qty": 1
        })

    if not items:
        print("No items parsed.")
        return 0

    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["vendor", "brand", "mpn", "upc", "title", "wholesale_cost", "uom", "pack_qty"])
        w.writeheader()
        w.writerows(items)

    print(f"==> Exported {len(items):,} items to {OUT_CSV}")
    return len(items)

if __name__ == "__main__":
    count = run()
    sys.exit(0 if count > 0 else 1)
