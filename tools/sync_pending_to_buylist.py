#!/usr/bin/env python3
"""
Syncs high-profit BCI leads from lead_decisions.json directly into saved_buy_list.csv
and dispatches them to the Google Sheet webhook.
"""

import os
import sys
import json
import requests
import pandas as pd
from pathlib import Path
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

DATA_DIR = ROOT_DIR / "data"
LEAD_FILE = DATA_DIR / "lead_decisions.json"
BUY_CSV = DATA_DIR / "saved_buy_list.csv"
WEBHOOK_URL = (
    os.getenv("BUY_LIST_WEBHOOK_URL") or 
    os.getenv("GOOGLE_SHEET_WEBHOOK_URL") or 
    os.getenv("DISTRIBUTOR_WEBHOOK_URL") or 
    ""
).strip()

if not LEAD_FILE.exists():
    print("No lead_decisions.json found.")
    sys.exit(0)

raw = json.load(open(LEAD_FILE))
leads = raw if isinstance(raw, list) else list(raw.values())

target_asins = ["B07SJ92ZF1", "B003Z717PW", "B01MDUIUCA", "B071LNJKPV"]
matches = [d for d in leads if d.get("asin") in target_asins]
if len(matches) < 4:
    matches = sorted([d for d in leads if d.get("net_profit", 0) > 0], key=lambda x: x.get("net_profit", 0), reverse=True)[:4]

header = [
    "ASIN", "Title", "Brand", "Pack_Count", "Category", 
    "Sales_Rank", "Monthly_Velocity", "Deal_Rating", 
    "Buy_Box_Price", "Wholesale_Cost", "Vendor", 
    "Order_URL", "Subscribe_Save", "Unit_Cost", 
    "Supplier", "Notes", "Status"
]

rows = []
for d in matches:
    asin = d.get("asin")
    row = [
        asin,
        d.get("title", "MRO Item"),
        d.get("brand", "MRO Commercial"),
        d.get("pack_count", 1),
        "MRO Commercial",
        d.get("sales_rank", 0),
        0,
        85,
        d.get("buybox_price", 0.0),
        d.get("wholesale_cost", 0.0),
        d.get("vendor", "BCI"),
        f"https://www.amazon.com/dp/{asin}",
        "No",
        d.get("wholesale_cost", 0.0),
        d.get("vendor", "BCI"),
        f"Approved via Telegram - Net Profit: ${d.get('net_profit', 0)} ({d.get('roi_percent', 0)}% ROI)",
        "PENDING_VERIFICATION"
    ]
    rows.append(row)

    if WEBHOOK_URL:
        try:
            requests.post(
                WEBHOOK_URL,
                json={
                    "action": "ADD_BUY_LEAD",
                    "asin": asin,
                    "title": d.get("title"),
                    "vendor": d.get("vendor", "BCI"),
                    "buy_box_price": d.get("buybox_price", 0.0),
                    "wholesale_cost": d.get("wholesale_cost", 0.0),
                    "net_profit": d.get("net_profit", 0.0),
                    "roi_percent": d.get("roi_percent", 0.0),
                    "status": "PENDING_VERIFICATION"
                },
                timeout=8
            )
            print(f"Dispatched {asin} to Google Sheet Webhook.")
        except Exception as e:
            print(f"Webhook forward error for {asin}: {e}")

df = pd.DataFrame(rows)
df.to_csv(BUY_CSV, mode="w", index=False, header=header)
print(f"Successfully recorded {len(rows)} leads into {BUY_CSV}")
