"""
tools/ip_guard.py
Master Universal IP Guard & Brand Restriction Shield.
Enforces zero-tolerance brand protection across ALL suppliers and sourcing modules.
"""

import json
import logging
import re
from pathlib import Path
from typing import Tuple

logger = logging.getLogger("IPGuard")

BLOCKLIST_FILE = Path(__file__).resolve().parent.parent / "data" / "ip_blocklist.json"
_BLOCKLIST_SET = set()
_NORMALIZED_PATTERNS = []

def load_blocklist():
    global _BLOCKLIST_SET, _NORMALIZED_PATTERNS
    if BLOCKLIST_FILE.exists():
        try:
            with open(BLOCKLIST_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                raw_brands = data.get("brands", [])
                _BLOCKLIST_SET = {b.strip().upper() for b in raw_brands if b}
                
                # Pre-compile exact word boundaries for high-speed regex checking
                _NORMALIZED_PATTERNS = [
                    re.compile(r"\b" + re.escape(b) + r"\b", re.IGNORECASE)
                    for b in _BLOCKLIST_SET if len(b) > 2
                ]
            logger.info(f"Master IP Firewall armed: {len(_BLOCKLIST_SET)} manufacturer brands loaded.")
        except Exception as e:
            logger.error(f"Failed to load master IP blocklist: {e}")
    else:
        logger.warning(f"Master IP blocklist not found at {BLOCKLIST_FILE}")

load_blocklist()

def is_brand_blocked(brand: str, title: str = "", vendor: str = "", distributor: str = "") -> Tuple[bool, str]:
    """
    Universal checkpoint across all catalog, scan, and RFQ feeds.
    Returns (True, reason) if item matches any blacklisted manufacturer brand.
    """
    if not _BLOCKLIST_SET:
        load_blocklist()

    b_clean = (brand or "").strip().upper()
    v_clean = (vendor or "").strip().upper()
    t_clean = (title or "").strip().upper()

    # 1. Exact string match against Brand or Vendor Name
    if b_clean in _BLOCKLIST_SET:
        return True, f"Global Blacklist: Exact brand match '{b_clean}'"
    if v_clean in _BLOCKLIST_SET:
        return True, f"Global Blacklist: Exact vendor match '{v_clean}'"

    # 2. Word boundary scan across Title and Brand string
    for pattern in _NORMALIZED_PATTERNS:
        if pattern.search(b_clean):
            return True, f"Global Blacklist: Brand contains '{pattern.pattern[2:-2]}'"
        if pattern.search(t_clean):
            return True, f"Global Blacklist: Title contains protected brand '{pattern.pattern[2:-2]}'"

    return False, ""
