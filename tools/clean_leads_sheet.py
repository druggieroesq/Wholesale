#!/usr/bin/env python3
"""
tools/clean_leads_sheet.py
--------------------------
One-Click Google Sheet Cleanup & Migration Tool.
Filters existing rows in 'Sourcing & Buy List' against the strict criteria:
  1. Monthly Velocity >= 100 units/mo
  2. Wholesale Cost between $12.00 and $65.00
  3. Net Profit >= $15.00
  4. Keeps valid records on 'Sourcing & Buy List', moves rejects to 'Archived Leads'.
"""

import os
import sys
import re
import json
import logging
from pathlib import Path
from typing import List, Dict, Any
import requests
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env", override=False)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] SheetCleaner: %(message)s")
logger = logging.getLogger("SheetCleaner")

WEBHOOK_URL = (
    os.getenv("BUY_LIST_WEBHOOK_URL") 
    or os.getenv("GOOGLE_SHEET_WEBHOOK_URL") 
    or ""
).strip()

STRICT_MIN_VELOCITY = 100
STRICT_MIN_COST = 12.00
STRICT_MAX_COST = 65.00
STRICT_MIN_PROFIT = 15.00

def parse_num(val: Any) -> float:
    if val is None:
        return 0.0
    clean = re.sub(r'[^0-9.-]', '', str(val))
    try:
        return float(clean)
    except ValueError:
        return 0.0

def execute_sheet_prune():
    if not WEBHOOK_URL:
        logger.error("Missing BUY_LIST_WEBHOOK_URL in environment.")
        return

    logger.info("Triggering remote sheet audit & cleanup via Google Apps Script Webhook...")

    payload = {
        "action": "PRUNE_NON_COMPLIANT_LEADS",
        "rules": {
            "min_velocity": STRICT_MIN_VELOCITY,
            "min_cost": STRICT_MIN_COST,
            "max_cost": STRICT_MAX_COST,
            "min_profit": STRICT_MIN_PROFIT,
            "archive_tab": "Archived Leads",
            "active_tab": "Sourcing & Buy List"
        }
    }

    try:
        res = requests.post(WEBHOOK_URL, json=payload, timeout=90)
        if res.status_code == 200:
            data = res.json()
            logger.info(f"Cleanup Complete! Retained: {data.get('retained', 0)} | Archived: {data.get('archived', 0)}")
            print(f"\n✅ Sheet Cleaned Successfully!\n• Retained Top Leads: {data.get('retained', 0)}\n• Moved to 'Archived Leads': {data.get('archived', 0)}")
        else:
            logger.warning(f"Webhook responded with HTTP {res.status_code}. Using local spreadsheet fallback.")
            run_manual_csv_clean()
    except Exception as e:
        logger.error(f"Error communicating with Apps Script Webhook: {e}")
        run_manual_csv_clean()

def run_manual_csv_clean():
    """Local fallback: cleans data/saved_buy_list_v10.csv if webhook is offline."""
    csv_file = ROOT_DIR / "data" / "saved_buy_list_v10.csv"
    if not csv_file.exists():
        logger.warning("No local CSV backup found.")
        return

    import pandas as pd
    df = pd.read_csv(csv_file)
    initial_count = len(df)

    # Convert numeric fields
    cost_col = next((c for c in df.columns if "cost" in c.lower() or "wholesale" in c.lower()), None)
    vel_col = next((c for c in df.columns if "velocity" in c.lower() or "monthly" in c.lower()), None)
    profit_col = next((c for c in df.columns if "profit" in c.lower() and "pool" not in c.lower()), None)

    if not (cost_col and vel_col and profit_col):
        logger.error("Could not map columns in local CSV.")
        return

    df['clean_cost'] = df[cost_col].apply(parse_num)
    df['clean_vel'] = df[vel_col].apply(parse_num)
    df['clean_profit'] = df[profit_col].apply(parse_num)

    mask = (
        (df['clean_vel'] >= STRICT_MIN_VELOCITY) &
        (df['clean_cost'] >= STRICT_MIN_COST) &
        (df['clean_cost'] <= STRICT_MAX_COST) &
        (df['clean_profit'] >= STRICT_MIN_PROFIT)
    )

    clean_df = df[mask].drop(columns=['clean_cost', 'clean_vel', 'clean_profit'])
    archived_df = df[~mask].drop(columns=['clean_cost', 'clean_vel', 'clean_profit'])

    clean_df.to_csv(csv_file, index=False)
    archived_file = ROOT_DIR / "data" / "archived_leads_v10.csv"
    archived_df.to_csv(archived_file, index=False)

    print(f"\n📁 Local CSV Cleaned:\n• Retained in active list: {len(clean_df)} (from {initial_count})\n• Saved rejects to: {archived_file.name}")

if __name__ == "__main__":
    execute_sheet_prune()
