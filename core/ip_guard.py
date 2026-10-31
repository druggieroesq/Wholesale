#!/usr/bin/env python3
"""
Core Zero-Token IP Guard Shield
--------------------------------
Centralized verification module that enforces dynamic, case-insensitive
substring matching against /app/data/ip_blocklist.json across all search engines.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger("IPGuard")

# Support both /app/data and relative data/ paths
IP_BLOCKLIST_FILE = Path("/app/data/ip_blocklist.json")
if not IP_BLOCKLIST_FILE.exists():
    IP_BLOCKLIST_FILE = Path(__file__).resolve().parent.parent / "data" / "ip_blocklist.json"


def load_ip_blocklist() -> dict:
    """Reads ip_blocklist.json dynamically on demand."""
    if not IP_BLOCKLIST_FILE.exists():
        return {"brands": set(), "skus": set()}
    try:
        raw = json.loads(IP_BLOCKLIST_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            return {
                "brands": set(str(b).strip().lower() for b in raw if str(b).strip()),
                "skus": set()
            }
        elif isinstance(raw, dict):
            brands = set(str(b).strip().lower() for b in raw.get("brands", []) if str(b).strip())
            skus = set(str(s).strip().upper() for s in raw.get("skus", []) if str(s).strip())
            return {"brands": brands, "skus": skus}
    except Exception as e:
        logger.error(f"Failed to parse {IP_BLOCKLIST_FILE}: {e}")
    return {"brands": set(), "skus": set()}


def is_ip_blocked(brand: str = "", title: str = "", sku: str = "") -> bool:
    """
    Evaluates brand, title, and SKU against blocked brands and restricted SKU tags.
    Returns True if an entry matches as a case-insensitive substring.
    """
    blocklist = load_ip_blocklist()
    b_clean = (brand or "").strip().lower()
    t_clean = (title or "").strip().lower()
    s_clean = (sku or "").strip().upper()

    for blocked_sku in blocklist["skus"]:
        if blocked_sku and blocked_sku in s_clean:
            return True

    for blocked_brand in blocklist["brands"]:
        if blocked_brand:
            if blocked_brand in b_clean or blocked_brand in t_clean:
                return True

    return False
