#!/usr/bin/env python3
"""
tools/google_sheets_sync.py
---------------------------
Direct Google Sheets Synchronization Module (v7.0 Production Master)
Appends approved buy leads directly to the 'Sourcing & Buy List' tab of:
'MRO Wholesale - Sourcing & Buy List Pipeline'

Key Enhancements:
1. Surfaces Hazmat / ORM-D flags, multipack quantities, and prep requirements.
2. Transmits full 4-part Landed Cost, Net Profit, and ROI hurdle metrics.
3. Surfaces 3PL Inbound Strategy, Case Breakdown, and Regional FC Routing (Cols AJ-AL).
4. Implements local dead-letter queue (failed_webhook_queue.csv) for offline resilience.
5. Environment-driven spreadsheet target configuration.
"""

import os
import sys
import csv
import time
import logging
from pathlib import Path
from typing import Dict, Any, Optional
import requests
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

load_dotenv(ROOT_DIR / ".env", override=False)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] SheetsSync: %(message)s")
logger = logging.getLogger("GoogleSheetsSync")

DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
FAILED_QUEUE_FILE = DATA_DIR / "failed_webhook_queue.csv"

SPREADSHEET_ID = os.getenv("BUY_LIST_SPREADSHEET_ID", "1-Q53nA20E84vHePQZueM06H9imkbMD3eQo7OqpxwoWM").strip()
SHEET_TAB_NAME = os.getenv("BUY_LIST_TAB_NAME", "Sourcing & Buy List").strip()


def enqueue_failed_lead(lead_data: Dict[str, Any]) -> None:
    """Saves unposted leads to local disk so they can be retried without data loss."""
    file_exists = FAILED_QUEUE_FILE.exists()
    try:
        with open(FAILED_QUEUE_FILE, mode="a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow([
                    "timestamp", "asin", "title", "brand", "pack_count",
                    "buybox_price", "wholesale_cost", "net_profit", "roi_percent",
                    "vendor", "is_hazmat", "status", "inbound_strategy",
                    "case_breakdown", "destination_3pl", "destination_routing"
                ])
            writer.writerow([
                int(time.time()),
                lead_data.get("asin"),
                lead_data.get("title", "MRO Item"),
                lead_data.get("brand", "MRO Commercial"),
                lead_data.get("pack_count", 1),
                lead_data.get("buy_box_price", lead_data.get("buybox_price", 0.0)),
                lead_data.get("wholesale_cost", lead_data.get("unit_cost", 0.0)),
                lead_data.get("net_profit", 0.0),
                lead_data.get("roi_percent", 0.0),
                lead_data.get("vendor", "BCI"),
                "YES" if lead_data.get("is_hazmat") else "NO",
                lead_data.get("status", "PENDING_VERIFICATION"),
                lead_data.get("inbound_strategy", "MINIMAL_SPLIT_LOCAL_PA"),
                lead_data.get("case_breakdown", "1 case @ 1/box"),
                lead_data.get("destination_3pl", "Makki Prep Center (Harrisburg, PA)"),
                lead_data.get("destination_routing", "PA Central Hub")
            ])
        logger.warning(f"Saved failed lead {lead_data.get('asin')} to local recovery queue ({FAILED_QUEUE_FILE.name}).")
    except Exception as e:
        logger.error(f"Error persisting to failed webhook queue: {e}")


def post_to_sheets_webhook(lead_data: Dict[str, Any]) -> bool:
    """Dispatches a structured lead payload to the Google Apps Script Webhook."""
    webhook_url = (
        os.getenv("BUY_LIST_WEBHOOK_URL") or 
        os.getenv("GOOGLE_SHEET_WEBHOOK_URL") or 
        ""
    ).strip()
    
    if not webhook_url:
        logger.warning("No BUY_LIST_WEBHOOK_URL configured. Caching lead to recovery queue.")
        enqueue_failed_lead(lead_data)
        return False
        
    asin = lead_data.get("asin")
    buy_box = float(lead_data.get("buy_box_price", lead_data.get("buybox_price", 0.0)))
    wholesale = float(lead_data.get("wholesale_cost", lead_data.get("unit_cost", 0.0)))
    net_profit = float(lead_data.get("net_profit", 0.0))
    roi = float(lead_data.get("roi_percent", 0.0))
    is_hazmat = bool(lead_data.get("is_hazmat", False))

    # Resolve 3PL logistics routing defaults
    vendor = lead_data.get("vendor", "BCI")
    default_3pl = "Makki Prep Center (Harrisburg, PA)" if "bci" in vendor.lower() else "Tamarac Hub"
    inbound_strategy = lead_data.get("inbound_strategy", "MINIMAL_SPLIT_LOCAL_PA")
    case_breakdown = lead_data.get("case_breakdown", f"{lead_data.get('pack_count', 1)} pk")
    destination_routing = lead_data.get("destination_routing", "Single PA Hub (AVP1/ABE2)")

    payload = {
        "action": "ADD_BUY_LEAD",
        "sheet_id": SPREADSHEET_ID,
        "tab_name": SHEET_TAB_NAME,
        "asin": asin,
        "title": lead_data.get("title", "MRO Commercial Item"),
        "brand": lead_data.get("brand", "MRO Commercial"),
        "pack_count": lead_data.get("pack_count", 1),
        "category": lead_data.get("category", "Industrial & Scientific"),
        "sales_rank": lead_data.get("sales_rank", 0),
        "monthly_velocity": lead_data.get("monthly_velocity", lead_data.get("monthly_units", 0)),
        "deal_rating": lead_data.get("deal_rating", min(99, int(70 + (roi / 20.0)))),
        "buy_box_price": buy_box,
        "wholesale_cost": wholesale,
        "net_profit": net_profit,
        "roi_percent": roi,
        "landed_cost": lead_data.get("landed_cost", wholesale),
        "max_allowable_buy_cost": lead_data.get("max_allowable_buy_cost", round(buy_box * 0.45, 2)),
        "suggested_order_size": lead_data.get("suggested_order_size", 6),
        "vendor": vendor,
        "supplier": vendor,
        "is_hazmat": "YES" if is_hazmat else "NO",
        "order_url": f"https://www.amazon.com/dp/{asin}",
        "notes": lead_data.get("notes", f"Approved via Telegram - Net: ${net_profit:.2f} ({roi:.1f}% ROI)"),
        "status": lead_data.get("status", "PENDING_VERIFICATION"),
        # New 3PL & Inbound Split Fields (Cols AJ, AK, AL)
        "inbound_strategy": inbound_strategy,
        "case_breakdown": case_breakdown,
        "destination_3pl": lead_data.get("destination_3pl", default_3pl),
        "destination_routing": destination_routing
    }
        
    for attempt in range(1, 4):
        try:
            resp = requests.post(webhook_url, json=payload, timeout=12)
            if resp.status_code == 200:
                logger.info(f"Successfully posted ASIN {asin} to Google Sheet '{SHEET_TAB_NAME}'.")
                return True
            else:
                logger.warning(f"Webhook rejected (Attempt {attempt}/3): HTTP {resp.status_code} - {resp.text[:100]}")
                time.sleep(1.5)
        except Exception as e:
            logger.error(f"Network error dispatching ASIN {asin} (Attempt {attempt}/3): {e}")
            time.sleep(1.5)
            
    enqueue_failed_lead(lead_data)
    return False


def retry_failed_leads() -> int:
    """Attempts to drain and re-post previously failed leads from local queue."""
    if not FAILED_QUEUE_FILE.exists():
        return 0

    try:
        with open(FAILED_QUEUE_FILE, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            leads = list(reader)

        if not leads:
            return 0

        logger.info(f"Attempting to drain {len(leads)} leads from failed queue...")
        succeeded = 0
        remaining = []

        for lead in leads:
            ok = post_to_sheets_webhook(lead)
            if ok:
                succeeded += 1
            else:
                remaining.append(lead)

        if not remaining:
            FAILED_QUEUE_FILE.unlink(missing_ok=True)
            logger.info("Failed queue completely cleared.")
        else:
            with open(FAILED_QUEUE_FILE, mode="w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=reader.fieldnames)
                writer.writeheader()
                writer.writerows(remaining)

        return succeeded
    except Exception as e:
        logger.error(f"Error retrying failed leads: {e}")
        return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--retry":
        drained = retry_failed_leads()
        print(f"Drained {drained} leads from recovery queue.")
    else:
        test_lead = {
            "asin": "B07SJ92ZF1",
            "title": "Industrial Brass Swivel Fitting 1/2 NPT",
            "brand": "Midland Metal",
            "pack_count": 5,
            "buy_box_price": 48.50,
            "wholesale_cost": 18.20,
            "net_profit": 14.80,
            "roi_percent": 81.3,
            "vendor": "BCI",
            "is_hazmat": False,
            "status": "TEST_PIPELINE",
            "inbound_strategy": "AMAZON_OPTIMIZED_SPLIT",
            "case_breakdown": "12 cases @ 25/box",
            "destination_3pl": "Makki Prep Center (Harrisburg, PA)",
            "destination_routing": "4x AVP1 | 4x MDW2 | 4x GYR3"
        }
        success = post_to_sheets_webhook(test_lead)
        print(f"Test lead dispatch result: {success}")
