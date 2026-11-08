#!/usr/bin/env python3
"""
tools/distributor_credential_sync.py
------------------------------------
Distributor Credential & Terms Ingest Engine (v6.0 Production Master)
- Syncs credentials, MOQs, Free Freight thresholds, and catalog methods
  from the Distributor List Google Sheet Webhook into a local keyed store.
- Auto-classifies vendors into 'MANUFACTURER' vs 'WHOLESALE' tiers.
- Sanitizes threshold currencies ($1,500 -> 1500.0) for the Logistics Engine.
- Employs atomic temp-file replacement to avoid partial reads on container reboot.
- Emits Telegram telemetry cards on updated supplier terms.
"""

import os
import sys
import json
import logging
import tempfile
import requests
from pathlib import Path
from typing import Dict, Any, Optional
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

load_dotenv(ROOT_DIR / ".env", override=False)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] DistSync: %(message)s")
logger = logging.getLogger("DistSync")

DATA_DIR = ROOT_DIR / "data"
CREDENTIALS_CACHE = DATA_DIR / "distributor_credentials.json"
WEBHOOK_URL = os.getenv(
    "DISTRIBUTOR_LIST_WEBHOOK_URL",
    "https://script.google.com/macros/s/AKfycbzNH1wVSEtKC1T2hus65xU_HKJCXZaNEqikfRq2GyUHH9DyS_LjNXHOA8fAN4_TddxVfw/exec"
).strip()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

KNOWN_MFR_KEYWORDS = ["MANUFACTURER", "DIRECT", "MFR", "FACTORY", "OEM"]


def send_telegram_alert(msg: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    try:
        requests.post(url, json=payload, timeout=8)
    except Exception as e:
        logger.error(f"Telegram alert delivery error: {e}")


def parse_clean_currency(val: Any) -> float:
    """Parses clean float from '$2,500', '500.00', or raw numbers."""
    if val is None:
        return 0.0
    val_clean = str(val).replace("$", "").replace(",", "").strip()
    try:
        return float(val_clean)
    except ValueError:
        return 0.0


def normalize_distributor_payload(raw_data: Any) -> Dict[str, Dict[str, Any]]:
    """
    Normalizes webhook payload (whether dict or list) into a canonical
    uppercase-keyed dictionary format required across hunter engines.
    """
    normalized: Dict[str, Dict[str, Any]] = {}
    
    items = []
    if isinstance(raw_data, list):
        items = raw_data
    elif isinstance(raw_data, dict):
        items = raw_data.get("distributors", raw_data.get("data", []))
        if not items and "name" in raw_data:
            items = [raw_data]

    for item in items:
        if not isinstance(item, dict):
            continue
        
        name = str(item.get("Name") or item.get("name") or "").strip()
        if not name:
            continue

        category = str(item.get("Major Area / Category") or item.get("category") or "").strip()
        is_mfr = any(k in category.upper() for k in KNOWN_MFR_KEYWORDS)
        tier = "MANUFACTURER" if is_mfr else "WHOLESALE"

        moq = parse_clean_currency(item.get("MOQ") or item.get("moq", 0))
        ffa = parse_clean_currency(
            item.get("Free freight Threshold ($ or units)") 
            or item.get("free_freight_minimum") 
            or item.get("free_freight", 0)
        )

        normalized[name.upper()] = {
            "name": name,
            "category": category,
            "tier": tier,
            "login_email": str(item.get("Login Email") or item.get("login_email") or "").strip(),
            "password": str(item.get("Password") or item.get("password") or "").strip(),
            "order_method": str(item.get("Order Method") or item.get("order_method") or "ONLINE").strip(),
            "moq": moq,
            "free_freight_minimum": ffa,
            "portal_url": str(item.get("Portal URL") or item.get("portal_url") or "").strip(),
            "rep_contact": str(item.get("Rep Contact") or item.get("rep_contact") or "").strip(),
            "notes": str(item.get("Notes") or item.get("notes") or "").strip()
        }

    return normalized


def sync_distributors() -> Optional[Dict[str, Dict[str, Any]]]:
    logger.info("Fetching active distributor terms and credentials from webhook...")
    try:
        session = requests.Session()
        res = session.get(WEBHOOK_URL, timeout=15, allow_redirects=True)
        if res.status_code != 200:
            logger.error(f"Failed to fetch distributors. HTTP {res.status_code}")
            return None

        raw_payload = res.json()
        dist_dict = normalize_distributor_payload(raw_payload)
        
        if not dist_dict:
            logger.warning("Webhook returned zero valid distributor profiles.")
            return None

        logger.info(f"Retrieved and normalized {len(dist_dict)} distributor profiles.")

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        
        # Atomic file write to avoid partial reads by concurrent workers
        with tempfile.NamedTemporaryFile("w", dir=DATA_DIR, delete=False, encoding="utf-8") as tf:
            json.dump(dist_dict, tf, indent=2)
            temp_path = Path(tf.name)
        
        temp_path.replace(CREDENTIALS_CACHE)
        logger.info(f"Cached credentials securely to {CREDENTIALS_CACHE}")

        # Count distribution
        mfr_count = sum(1 for d in dist_dict.values() if d.get("tier") == "MANUFACTURER")
        whls_count = len(dist_dict) - mfr_count

        send_telegram_alert(
            f"🔄 *DISTRIBUTOR TERMS SYNC COMPLETE*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 *Total Profiles:* {len(dist_dict)}\n"
            f"🏭 *Direct Manufacturers:* {mfr_count}\n"
            f"📦 *Wholesale Distributors:* {whls_count}\n"
            f"💾 *Store:* `{CREDENTIALS_CACHE.name}`\n"
            f"🚀 *Status:* Logistics and Hunter credentials hot-reloaded."
        )

        return dist_dict

    except Exception as e:
        logger.error(f"Error syncing distributor credentials: {e}")
        return None


if __name__ == "__main__":
    sync_distributors()
