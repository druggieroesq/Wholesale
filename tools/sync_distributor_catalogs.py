#!/usr/bin/env python3
"""
tools/sync_distributor_catalogs.py
----------------------------------
Automated Two-Tier Catalog Synchronization & Normalization Runner (v10.1 Master)
- Scans wholesale and manufacturer catalog incoming folders.
- Strict filename deduplication: processes each catalog name exactly once per sweep.
- Normalizes discovered files (.xlsx, .xls, .csv, .parquet) via catalog_normalizer.py.
- Generates canonical indices in:
    * data/catalogs/wholesale_catalogs/indexed/
    * data/catalogs/manufacturer_catalogs/indexed/
"""

import os
import sys
import subprocess
import logging
from pathlib import Path
from typing import List, Tuple, Set

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] CatalogSync: %(message)s"
)
logger = logging.getLogger("CatalogSync")

CATALOG_DIR = ROOT_DIR / "data" / "catalogs"
WHOLESALE_INCOMING = CATALOG_DIR / "wholesale_catalogs" / "incoming"
WHOLESALE_INDEXED = CATALOG_DIR / "wholesale_catalogs" / "indexed"
MFR_INCOMING = CATALOG_DIR / "manufacturer_catalogs" / "incoming"
MFR_INDEXED = CATALOG_DIR / "manufacturer_catalogs" / "indexed"
ROOT_INCOMING = CATALOG_DIR / "incoming"

NORMALIZER_SCRIPT = ROOT_DIR / "tools" / "catalog_normalizer.py"
DRIVE_SYNC_SCRIPT = ROOT_DIR / "tools" / "sync_drive_manufacturer_catalogs.py"

for folder in [WHOLESALE_INCOMING, WHOLESALE_INDEXED, MFR_INCOMING, MFR_INDEXED, ROOT_INCOMING]:
    folder.mkdir(parents=True, exist_ok=True)


def sync_google_drive():
    if DRIVE_SYNC_SCRIPT.exists():
        logger.info("Executing Google Drive manufacturer catalog sync before normalization...")
        try:
            res = subprocess.run(
                [sys.executable, str(DRIVE_SYNC_SCRIPT)],
                capture_output=True,
                text=True,
                timeout=90
            )
            if res.returncode == 0:
                logger.info("Google Drive sync finished successfully.")
            else:
                logger.warning(f"Google Drive sync notice: {res.stderr.strip() or res.stdout.strip()}")
        except Exception as exc:
            logger.warning(f"Drive sync exception: {exc}")


def locate_catalogs_by_tier() -> List[Tuple[Path, Path, str]]:
    tasks: List[Tuple[Path, Path, str]] = []
    seen_filenames: Set[str] = set()

    # 1. Manufacturer Catalogs (Priority 1)
    if MFR_INCOMING.exists():
        for ext in ("*.xlsx", "*.xls", "*.csv"):
            for f in MFR_INCOMING.glob(ext):
                if not f.is_file() or f.name.startswith("~"):
                    continue
                fname_lower = f.name.lower()
                if fname_lower not in seen_filenames:
                    seen_filenames.add(fname_lower)
                    tasks.append((f, MFR_INDEXED, "MANUFACTURER"))

    # 2. Wholesale Distributor Catalogs (Priority 2)
    wholesale_sources = [WHOLESALE_INCOMING, ROOT_INCOMING, CATALOG_DIR / "wholesale_catalogs"]

    for s_dir in wholesale_sources:
        if not s_dir.exists():
            continue
        for ext in ("*.xlsx", "*.xls", "*.csv"):
            for f in s_dir.glob(ext):
                if not f.is_file() or f.name.startswith("~"):
                    continue
                fname_lower = f.name.lower()
                if "indexed" in f.parts or "normalized" in fname_lower:
                    continue
                if "manufacturer" in str(f).lower():
                    continue
                if fname_lower not in seen_filenames:
                    seen_filenames.add(fname_lower)
                    tasks.append((f, WHOLESALE_INDEXED, "WHOLESALE"))

    tasks.sort(key=lambda t: t[0].name.lower())
    return tasks


def run_catalog_sync():
    logger.info("Initiating full two-tier distributor and manufacturer catalog sweep...")
    sync_google_drive()

    if not NORMALIZER_SCRIPT.exists():
        logger.error(f"Catalog normalizer missing at {NORMALIZER_SCRIPT}")
        return

    catalogs_to_process = locate_catalogs_by_tier()
    if not catalogs_to_process:
        logger.info("No raw catalog spreadsheets found in data/catalogs to normalize.")
        return

    logger.info(f"Found {len(catalogs_to_process)} unique catalog(s) for normalization: {[f[0].name for f in catalogs_to_process]}")

    success_count = 0
    for cat_file, target_dir, tier in catalogs_to_process:
        logger.info(f"Normalizing [{tier}] {cat_file.name} -> {target_dir.name}")
        cmd = [
            sys.executable,
            str(NORMALIZER_SCRIPT),
            str(cat_file),
            str(target_dir)
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if res.returncode == 0:
                logger.info(f"Successfully normalized [{tier}] {cat_file.name}")
                success_count += 1
            else:
                logger.error(f"Failed to normalize {cat_file.name}:\n{res.stderr.strip() or res.stdout.strip()}")
        except Exception as e:
            logger.error(f"Execution error normalizing {cat_file.name}: {e}")

    logger.info(f"Two-tier catalog synchronization complete: {success_count}/{len(catalogs_to_process)} indexed.")


if __name__ == "__main__":
    run_catalog_sync()
