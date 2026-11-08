#!/usr/bin/env python3
"""
scripts/fetchers/fetch_contarmarket.py

Automated Catalog Ingestion Fetcher: Contarmarket
Scheduled: Daily at 7:15 PM ET (post 7:00 PM Contarmarket rebuild)
Downloads the latest ContarPlus CSV, normalizes schema, and updates master index.
"""

import datetime
import os
import sys
import pandas as pd
import requests

# Base paths
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RAW_DIR = os.path.join(PROJECT_ROOT, "data", "catalogs", "raw")
MASTER_INDEX_PATH = os.path.join(PROJECT_ROOT, "data", "catalogs", "master_distributor_index.csv")

EXPORT_PAGE_URL = "https://account.contarmarket.com/pages/01a0aae8-e036-70df-856c-35d3070b949e"
CSV_DOWNLOAD_URL = os.getenv("CONTARMARKET_CSV_URL", "https://account.contarmarket.com/download/catalog.csv")
SESSION_COOKIE = os.getenv("CONTARMARKET_SESSION_COOKIE", "")


def fetch_and_ingest():
    os.makedirs(RAW_DIR, exist_ok=True)
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    dest_raw = os.path.join(RAW_DIR, f"contarplus-catalog-{today_str}.csv")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": EXPORT_PAGE_URL,
    }
    if SESSION_COOKIE:
        headers["Cookie"] = SESSION_COOKIE

    print(f"[{datetime.datetime.now().isoformat()}] Fetching Contarmarket catalog from {CSV_DOWNLOAD_URL}...")

    download_success = False
    try:
        response = requests.get(CSV_DOWNLOAD_URL, headers=headers, timeout=60, stream=True)
        content_type = response.headers.get("Content-Type", "").lower()
        if response.status_code == 200 and ("text" in content_type or "csv" in content_type):
            with open(dest_raw, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            print(f"Saved catalog to {dest_raw}")
            download_success = True
        else:
            print(f"Direct download returned HTTP {response.status_code}. Content-Type: {content_type}")
    except Exception as e:
        print(f"Network error during fetch: {e}")

    # Fallback to most recent local raw file if direct fetch fails
    if not download_success:
        print("Checking for existing raw file fallback...")
        candidates = sorted([
            os.path.join(RAW_DIR, f)
            for f in os.listdir(RAW_DIR)
            if f.startswith("contarplus") and f.endswith(".csv")
        ])
        if candidates:
            dest_raw = candidates[-1]
            print(f"Falling back to latest cached file: {dest_raw}")
        else:
            print("ERROR: No raw catalog file available to ingest.")
            sys.exit(1)

    # Ingest and normalize schema
    print(f"Reading {dest_raw}...")
    df = pd.read_csv(dest_raw)
    print(f"Found {len(df)} SKUs in Contarmarket feed.")

    # Load master index
    if os.path.exists(MASTER_INDEX_PATH):
        master_df = pd.read_csv(MASTER_INDEX_PATH)
    else:
        master_df = pd.DataFrame()

    # Clear existing Contarmarket records for a clean replacement
    if not master_df.empty and "distributor" in master_df.columns:
        master_df = master_df[master_df["distributor"] != "Contarmarket"]

    # Map Contarmarket export schema to Master Index schema
    contar_ingest = pd.DataFrame({
        "distributor": "Contarmarket",
        "sku": df["SKU"].astype(str),
        "title": df["Title"],
        "package_size": df["Title"].str.extract(
            r"(\d+\s*(?:Pack|CT|oz|Oz|lb|ct|count))", expand=False
        ).fillna("Case Pack"),
        "cost": df["Price (Contarplus+)"],
        "moq": 1,
        "free_freight_min": 350.00,
        "category": df["Category"],
    })

    # Save merged index
    updated_master = pd.concat([master_df, contar_ingest], ignore_index=True)
    os.makedirs(os.path.dirname(MASTER_INDEX_PATH), exist_ok=True)
    updated_master.to_csv(MASTER_INDEX_PATH, index=False)
    print(f"Updated {MASTER_INDEX_PATH}. Total indexed SKUs across all distributors: {len(updated_master)}")


if __name__ == "__main__":
    fetch_and_ingest()
