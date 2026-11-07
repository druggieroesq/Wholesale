#!/usr/bin/env python3
"""
Core Zero-Token IP Guard Shield
--------------------------------
Centralized verification module that enforces dynamic, case-insensitive
substring matching against ip_blocklist.json across all search engines.
Includes auto-appending capabilities for live Telegram IP blocking.
"""

import os
import json
import logging
from pathlib import Path

logger = logging.getLogger("IPGuard")

# Resolves both container /app/data path and local repo relative data path
PRIMARY_FILE = Path("/app/data/ip_blocklist.json")
FALLBACK_FILE = Path(__file__).resolve().parent.parent / "data" / "ip_blocklist.json"


def get_ip_blocklist_path() -> Path:
    if PRIMARY_FILE.parent.exists():
        return PRIMARY_FILE
    FALLBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
    return FALLBACK_FILE


def load_ip_blocklist() -> dict:
    target = get_ip_blocklist_path()
    if not target.exists():
        return {"brands": set(), "skus": set()}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
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
        logger.error(f"Failed to read blocklist from {target}: {e}")
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


def append_brand_to_blocklist(brand: str) -> bool:
    """Appends a new brand to ip_blocklist.json permanently."""
    brand_clean = (brand or "").strip().lower()
    if not brand_clean:
        return False

    target = get_ip_blocklist_path()
    data = {"version": "2.2.0", "description": "Dynamic IP Guard Shield", "brands": [], "skus": []}

    if target.exists():
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                data["brands"] = [str(b).strip().upper() for b in raw if str(b).strip()]
            elif isinstance(raw, dict):
                data["brands"] = [str(b).strip().upper() for b in raw.get("brands", []) if str(b).strip()]
                data["skus"] = raw.get("skus", [])
        except Exception as e:
            logger.error(f"Error reading {target} for append: {e}")

    existing_set = set(b.lower() for b in data["brands"])
    if brand_clean not in existing_set:
        data["brands"].append(brand.strip().upper())
        data["brands"] = sorted(list(set(data["brands"])))
        target.write_text(json.dumps(data, indent=2), encoding="utf-8")
        logger.info(f"Appended brand '{brand.strip().upper()}' to IP blocklist at {target}")
        return True
    return False
