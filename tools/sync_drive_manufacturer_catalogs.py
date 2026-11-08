#!/usr/bin/env python3
"""
tools/sync_drive_manufacturer_catalogs.py
-----------------------------------------
Automated Google Drive Manufacturer Catalog Syncer
- Downloads spreadsheets (.xlsx, .xls, .csv) from the shared Google Drive folder.
- Stashes files in data/catalogs/manufacturer_catalogs/incoming/.
- Integrates with gdown and handles retry backoff.
"""

import os
import sys
import logging
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] DriveSync: %(message)s"
)
logger = logging.getLogger("DriveSync")

TARGET_INCOMING = ROOT_DIR / "data" / "catalogs" / "manufacturer_catalogs" / "incoming"
TARGET_INCOMING.mkdir(parents=True, exist_ok=True)

# Google Drive folder ID: 1qHvpfI3WpkwdiPcGAXjogVCJqRzK59Qc
FOLDER_ID = "1qHvpfI3WpkwdiPcGAXjogVCJqRzK59Qc"
DRIVE_FOLDER_URL = f"https://drive.google.com/drive/folders/{FOLDER_ID}"


def sync_manufacturer_folder():
    logger.info("Checking Google Drive 'Manufacturer_Catalogs' folder for new price lists...")
    try:
        import gdown
    except ImportError:
        logger.error("gdown module is missing. Add 'gdown>=5.1.0' to requirements.txt.")
        return

    try:
        # download_folder retrieves new or updated files into the incoming folder
        downloaded = gdown.download_folder(
            url=DRIVE_FOLDER_URL,
            output=str(TARGET_INCOMING),
            quiet=False,
            use_cookies=False,
            
        )
        if downloaded:
            logger.info(f"Drive sync successful: {len(downloaded)} files processed.")
        else:
            logger.info("Drive folder checked. No new files found.")
    except Exception as e:
        logger.error(f"Failed to sync Drive folder: {e}")


def run_continuous_sync(interval_seconds: int = 120):
    """Runs periodic sync in background."""
    logger.info(f"Starting continuous Drive synchronization (interval: {interval_seconds}s)...")
    while True:
        try:
            sync_manufacturer_folder()
            time.sleep(interval_seconds)
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Error in continuous sync loop: {e}")
            time.sleep(30)


if __name__ == "__main__":
    if "--daemon" in sys.argv:
        run_continuous_sync(interval_seconds=120)
    else:
        sync_manufacturer_folder()
