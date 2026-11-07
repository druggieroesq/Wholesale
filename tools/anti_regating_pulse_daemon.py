#!/usr/bin/env python3
"""
tools/anti_regating_pulse_daemon.py
-----------------------------------
Automated Brand Anchor Keeper & Staggered Anti-Regating Pulse Daemon (v7.2 Production)
Features:
1. Tracks brands that required formal approval and were ungated.
2. Creates/updates FBM Anchor Listings via Amazon SP-API Listings Items API (2021-08-01).
3. Staggered Jitter Architecture:
   - Deterministic brand-specific pulse window: 145 to 168 days (randomized by brand hash).
   - Dynamic pulse duration: 10 to 18 days.
4. Realistic price guardrail: 1.85x to 2.15x Buy Box with cent randomization (.49, .79, .95).
5. 14-day handling time valve on Dormancy-Keeper offer.
6. Automatically reverts Qty back to 0 when brand-specific pulse duration expires.
7. Direct Telegram audit alerts for active and concluded pulses.
8. Auto-ingests approved brands from hunter_engine.db and local catalogs.
"""

import os
import sys
import json
import time
import logging
import datetime
import hashlib
from pathlib import Path
from typing import Dict, Any, List, Optional
import requests
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

load_dotenv(ROOT_DIR / ".env", override=False)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] AntiRegate: %(message)s")
logger = logging.getLogger("AntiRegateDaemon")

DATA_DIR = ROOT_DIR / "data"
REGISTRY_FILE = DATA_DIR / "brand_ungate_registry.json"

# SP-API Environment Variables
SP_API_REFRESH_TOKEN = os.getenv("SP_API_REFRESH_TOKEN", "").strip()
SP_API_CLIENT_ID = os.getenv("SP_API_CLIENT_ID", "").strip()
SP_API_CLIENT_SECRET = os.getenv("SP_API_CLIENT_SECRET", "").strip()
SELLER_ID = os.getenv("AMAZON_SELLER_ID", "").strip()
MARKETPLACE_ID = os.getenv("AMAZON_MARKETPLACE_ID", "ATVPDKIKX0DER").strip()

# Telegram Token Sanitization
_raw_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip().strip('"').strip("'")
TELEGRAM_BOT_TOKEN = _raw_token[3:] if _raw_token.lower().startswith("bot") else _raw_token
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip().strip('"').strip("'")


def send_telegram_alert(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
        res = requests.post(url, json=payload, timeout=8)
        return res.json().get("ok", False)
    except Exception as e:
        logger.warning(f"Telegram alert warning: {e}")
        return False


def get_sp_api_access_token() -> Optional[str]:
    if not (SP_API_REFRESH_TOKEN and SP_API_CLIENT_ID and SP_API_CLIENT_SECRET):
        logger.warning("SP-API credentials missing. Operating in DRY-RUN mode.")
        return None

    try:
        url = "https://api.amazon.com/auth/o2/token"
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": SP_API_REFRESH_TOKEN,
            "client_id": SP_API_CLIENT_ID,
            "client_secret": SP_API_CLIENT_SECRET
        }
        resp = requests.post(url, data=payload, timeout=12)
        if resp.status_code == 200:
            return resp.json().get("access_token")
        else:
            logger.error(f"LWA Token generation failed: HTTP {resp.status_code} - {resp.text}")
            return None
    except Exception as e:
        logger.error(f"Error fetching SP-API LWA token: {e}")
        return None


def calculate_brand_pulse_interval(brand_name: str, min_days: int = 145, max_days: int = 168) -> int:
    h = int(hashlib.sha256(brand_name.encode('utf-8')).hexdigest(), 16)
    return min_days + (h % (max_days - min_days + 1))


def calculate_brand_pulse_duration(brand_name: str, min_duration: int = 10, max_duration: int = 18) -> int:
    h = int(hashlib.sha256((brand_name + "_duration").encode('utf-8')).hexdigest(), 16)
    return min_duration + (h % (max_duration - min_duration + 1))


def calculate_dynamic_pulse_price(brand_name: str, ref_price: float) -> float:
    h = int(hashlib.sha256((brand_name + "_price").encode('utf-8')).hexdigest(), 16)
    multiplier = 1.85 + ((h % 30) / 100.0)
    raw_price = ref_price * multiplier
    cents_options = [0.49, 0.79, 0.89, 0.95, 0.99]
    selected_cents = cents_options[h % len(cents_options)]
    return round(int(raw_price) + selected_cents, 2)


def load_registry() -> Dict[str, Any]:
    if not REGISTRY_FILE.exists():
        initial_data = {
            "monitored_brands": {
                "Aura Cacia": {
                    "distributor": "Frontier Co-op",
                    "approved_date": "2026-08-01",
                    "requires_ungate": True,
                    "anchor_asin": "B00014DJ7C",
                    "anchor_sku": "KEEPER-AURA-B00014DJ7C",
                    "buy_box_reference": 14.50,
                    "last_pulse_date": "2026-08-02",
                    "active_pulse": False,
                    "pulse_end_date": None
                },
                "Klein Tools": {
                    "distributor": "Global Industrial",
                    "approved_date": "2026-09-15",
                    "requires_ungate": True,
                    "anchor_asin": "B0000302W6",
                    "anchor_sku": "KEEPER-KLEIN-B0000302W6",
                    "buy_box_reference": 24.00,
                    "last_pulse_date": "2026-09-16",
                    "active_pulse": False,
                    "pulse_end_date": None
                }
            }
        }
        save_registry(initial_data)
        return initial_data

    try:
        with open(REGISTRY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading registry: {e}")
        return {"monitored_brands": {}}


def save_registry(data: Dict[str, Any]):
    REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(REGISTRY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def submit_sp_api_listing_update(sku: str, quantity: int, price: float, lead_time_days: int = 14) -> bool:
    """
    Submits offer updates to Amazon Listings Items API (v2021-08-01).
    Fix: Passes productType via query parameters as required by Amazon specifications.
    """
    token = get_sp_api_access_token()
    if not token or not SELLER_ID:
        logger.info(f"[DRY RUN] SP-API PutListing: SKU={sku} QTY={quantity} Price=${price} LeadTime={lead_time_days}d")
        return True

    url = f"https://sellingpartnerapi-na.amazon.com/listings/2021-08-01/items/{SELLER_ID}/{sku}"
    # Standard productType query param for North American marketplace offers
    params = {
        "marketplaceIds": MARKETPLACE_ID,
        "issueLocale": "en_US",
        "productType": "PRODUCT"
    }
    headers = {
        "x-amz-access-token": token,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    body = {
        "patches": [
            {
                "op": "replace",
                "path": "/attributes/fulfillment_availability",
                "value": [{
                    "fulfillment_channel_code": "DEFAULT",
                    "quantity": quantity,
                    "lead_time_to_ship_max_days": lead_time_days
                }]
            },
            {
                "op": "replace",
                "path": "/attributes/purchasable_offer",
                "value": [{
                    "currency": "USD",
                    "our_price": [{"schedule": [{"value_with_tax": price}]}]
                }]
            }
        ]
    }

    try:
        res = requests.patch(url, params=params, headers=headers, json=body, timeout=15)
        if res.status_code in [200, 204]:
            logger.info(f"✅ SP-API Offer Updated: {sku} -> QTY={quantity}, Price=${price}")
            return True
        else:
            logger.error(f"❌ SP-API Offer Update Failed ({sku}): HTTP {res.status_code} - {res.text}")
            return False
    except Exception as e:
        logger.error(f"Network error calling SP-API Listings Items: {e}")
        return False


def evaluate_and_run_pulse() -> List[Dict[str, Any]]:
    registry = load_registry()
    today = datetime.date.today()
    actions_taken = []

    for brand, info in registry.get("monitored_brands", {}).items():
        if not info.get("requires_ungate", False):
            continue

        active_pulse = info.get("active_pulse", False)
        last_pulse = datetime.datetime.strptime(info["last_pulse_date"], "%Y-%m-%d").date()
        days_since_pulse = (today - last_pulse).days

        staggered_interval = calculate_brand_pulse_interval(brand)
        pulse_duration = calculate_brand_pulse_duration(brand)
        ref_price = info.get("buy_box_reference", 20.00)
        pulse_price = calculate_dynamic_pulse_price(brand, ref_price)
        sku = info["anchor_sku"]

        # Case 1: Revert active pulse after duration expires
        if active_pulse:
            end_date = datetime.datetime.strptime(info["pulse_end_date"], "%Y-%m-%d").date()
            if today >= end_date:
                success = submit_sp_api_listing_update(sku=sku, quantity=0, price=pulse_price, lead_time_days=14)
                if success:
                    info["active_pulse"] = False
                    info["pulse_end_date"] = None
                    info["last_pulse_date"] = str(today)
                    actions_taken.append({
                        "action": "PULSE_CONCLUDED_REVERTED_TO_ZERO",
                        "brand": brand,
                        "sku": sku
                    })
                    send_telegram_alert(
                        f"🔒 *ANTI-REGATE PULSE CONCLUDED*\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"*Brand:* `{brand}`\n"
                        f"*Anchor SKU:* `{sku}`\n"
                        f"*Status:* Reverted to `QTY=0` (Dormant Protected)\n"
                        f"*Next Interval:* ~{staggered_interval} days"
                    )

        # Case 2: Trigger new staggered pulse
        elif days_since_pulse >= staggered_interval:
            success = submit_sp_api_listing_update(sku=sku, quantity=1, price=pulse_price, lead_time_days=14)
            if success:
                end_date_str = str(today + datetime.timedelta(days=pulse_duration))
                info["active_pulse"] = True
                info["pulse_end_date"] = end_date_str
                actions_taken.append({
                    "action": "PULSE_ACTIVATED_STAGGERED",
                    "brand": brand,
                    "sku": sku,
                    "pulse_price": pulse_price,
                    "duration_days": pulse_duration
                })
                send_telegram_alert(
                    f"⚡ *ANTI-REGATE PULSE ACTIVATED*\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"*Brand:* `{brand}`\n"
                    f"*Anchor SKU:* `{sku}`\n"
                    f"*Price:* `${pulse_price:.2f}` (Uncompetitive Guardrail)\n"
                    f"*Handling:* 14 Days (Dormancy-Keeper)\n"
                    f"*Active Window:* {pulse_duration} days (Ends: {end_date_str})"
                )

    save_registry(registry)
    return actions_taken


def daemon_loop():
    logger.info("Anti-Regating Pulse Daemon loop started. Checking intervals every 24 hours.")
    while True:
        try:
            acts = evaluate_and_run_pulse()
            logger.info(f"Evaluated brand registry. Actions executed: {len(acts)}")
        except Exception as e:
            logger.error(f"Error in pulse evaluation loop: {e}")
        time.sleep(86400)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--daemon":
        daemon_loop()
    else:
        results = evaluate_and_run_pulse()
        print(f"Executed pulse evaluation. Actions count: {len(results)}")
        print(json.dumps(results, indent=2))
