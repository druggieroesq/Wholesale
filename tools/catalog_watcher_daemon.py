#!/usr/bin/env python3
"""
tools/catalog_watcher_daemon.py
-------------------------------
Hot-Reload Catalog Watcher Daemon
- Continuously polls incoming catalog directories for new/modified spreadsheets.
- Auto-normalizes files into Parquet via catalog_normalizer.py.
- Moves raw files to 'processed/' or updates the ledger.
- Signals ReverseAmazonHunter to reload its in-memory catalog index and re-score candidates.
"""

import os
import sys
import time
import json
import logging
import subprocess
from pathlib import Path
from typing import Dict

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] CatalogWatcher: %(message)s"
)
logger = logging.getLogger("CatalogWatcher")

CATALOG_DIR = ROOT_DIR / "data" / "catalogs"
WATCH_DIRS = [
    CATALOG_DIR / "wholesale_catalogs" / "incoming",
    CATALOG_DIR / "manufacturer_catalogs" / "incoming",
    CATALOG_DIR / "incoming",
    CATALOG_DIR
]
INDEXED_WHOLESALE = CATALOG_DIR / "wholesale_catalogs" / "indexed"
INDEXED_MFR = CATALOG_DIR / "manufacturer_catalogs" / "indexed"
NORMALIZER_SCRIPT = ROOT_DIR / "tools" / "catalog_normalizer.py"
STATE_FILE = ROOT_DIR / "data" / "catalog_watcher_state.json"

INDEXED_WHOLESALE.mkdir(parents=True, exist_ok=True)
INDEXED_MFR.mkdir(parents=True, exist_ok=True)


def load_state() -> Dict[str, float]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state: Dict[str, float]):
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"Failed to save watcher state: {e}")


def scan_for_changes(state: Dict[str, float]) -> Dict[Path, Path]:
    """Finds new or modified files. Returns mapping: file_path -> target_indexed_dir."""
    to_process = {}
    valid_exts = (".xlsx", ".xls", ".csv")

    for d in WATCH_DIRS:
        if not d.exists():
            continue
        for ext in valid_exts:
            for f in d.glob(ext):
                # Never re-normalize indexed output files or already normalized files
                if "indexed" in f.parts or "normalized" in f.name.lower() or "processed" in f.parts:
                    continue

                try:
                    mtime = f.stat().st_mtime
                    f_key = str(f.resolve())
                    # If file is new or modified since last check
                    if f_key not in state or state[f_key] < mtime:
                        target_dir = INDEXED_MFR if "manufacturer" in str(f) else INDEXED_WHOLESALE
                        to_process[f] = target_dir
                except Exception:
                    continue

    return to_process


def process_new_catalog(file_path: Path, target_dir: Path) -> bool:
    logger.info(f"⚡ New price list detected: {file_path.name}. Beginning automatic ingestion...")
    cmd = [
        sys.executable,
        str(NORMALIZER_SCRIPT),
        str(file_path),
        str(target_dir)
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode == 0:
            logger.info(f"✅ Successfully normalized: {file_path.name} -> {target_dir.name}")
            return True
        else:
            logger.error(f"❌ Failed normalizing {file_path.name}:\n{res.stderr or res.stdout}")
            return False
    except Exception as e:
        logger.error(f"Execution error on {file_path.name}: {e}")
        return False


def trigger_candidate_rescore():
    """Signals hunter to re-score uncataloged gap inventory against newly indexed keys."""
    logger.info("Triggering zero-token candidate re-score against updated catalog index...")
    script = (
        "import sys; sys.path.insert(0, '.'); "
        "from tools.reverse_amazon_finder import ReverseAmazonHunter; "
        "hunter = ReverseAmazonHunter(); hunter.rescore_cached_candidates()"
    )
    cmd = [sys.executable, "-c", script]
    try:
        subprocess.Popen(cmd)
    except Exception as e:
        logger.warning(f"Failed to launch background re-score: {e}")


def watch_loop():
    logger.info("Catalog Hot-Reload Watcher is ACTIVE. Watching incoming catalog directories...")
    state = load_state()

    while True:
        try:
            new_files = scan_for_changes(state)
            if new_files:
                any_success = False
                for f_path, out_dir in new_files.items():
                    # Wait 2 seconds to make sure large uploads/writes are finished
                    time.sleep(2)
                    if process_new_catalog(f_path, out_dir):
                        state[str(f_path.resolve())] = f_path.stat().st_mtime
                        any_success = True

                save_state(state)

                if any_success:
                    trigger_candidate_rescore()

            time.sleep(15)  # Poll every 15 seconds
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Watcher loop error: {e}")
            time.sleep(10)


if __name__ == "__main__":
    watch_loop()
