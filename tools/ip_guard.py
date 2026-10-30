"""
tools/ip_guard.py
Central IP & Brand Restriction Enforcement Guard.
Blocks high-risk manufacturer brands across ALL distributors and scanners.
"""

import json
import logging
import re
from pathlib import Path
from typing import Tuple

logger = logging.getLogger("IPGuard")

BLOCKLIST_FILE = Path(__file__).resolve().parent.parent / "data" / "ip_blocklist.json"

_BLOCKLIST_SET = set()

def load_blocklist():
    global _BLOCKLIST_SET
    if BLOCKLIST_FILE.exists():
        try:
            with open(BLOCKLIST_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                _BLOCKLIST_SET = {b.strip().upper() for b in data.get("brands", []) if b}
            logger.info(f"Loaded {len(_BLOCKLIST_SET)} brands into IP Guard.")
        except Exception as e:
            logger.error(f"Failed to load IP blocklist: {e}")
    else:
        logger.warning(f"IP blocklist not found at {BLOCKLIST_FILE}")

load_blocklist()

def is_brand_blocked(brand: str, title: str = "", vendor: str = "") -> Tuple[bool, str]:
    """
    Returns (True, reason) if brand or item matches restricted manufacturer blacklist.
    """
    if not _BLOCKLIST_SET:
        load_blocklist()

    clean_brand = re.sub(r"[^A-Za-z0-9 ]+", " ", (brand or "")).strip().upper()
    clean_vendor = re.sub(r"[^A-Za-z0-9 ]+", " ", (vendor or "")).strip().upper()
    clean_title = re.sub(r"[^A-Za-z0-9 ]+", " ", (title or "")).strip().upper()

    for blocked in _BLOCKLIST_SET:
        # Exact brand match
        if blocked == clean_brand or blocked == clean_vendor:
            return True, f"Blocked brand: {blocked}"
        
        # Word boundary matching in title or brand (avoid substring accidents like 'AND' in 'ANDIS')
        pattern = r"\b" + re.escape(blocked) + r"\b"
        if re.search(pattern, clean_brand) or re.search(pattern, clean_title):
            return True, f"Blocked manufacturer match: {blocked}"

    return False, ""
