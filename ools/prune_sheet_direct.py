#!/usr/bin/env python3
"""
tools/prune_sheet_direct.py
---------------------------
Direct Google Sheet Pruning Tool using existing sheets credentials or CSV backup.
Moves leads failing the Top-20 criteria to 'Archived Leads':
  - Monthly Velocity >= 100
  - Wholesale Cost between $12.00 and $65.00
  - Net Profit >= $15.00
"""

import os
import sys
import re
import csv
import logging
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("SheetPrune")

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

def clean_local_csv_files():
    """Cleans all local cached lead CSVs in data/."""
    data_dir = ROOT_DIR / "data"
    csv_targets = [
        data_dir / "saved_buy_list_v10.csv",
        data_dir / "saved_buy_list.csv"
    ]

    for target in csv_targets:
        if not target.exists():
            continue

        with open(target, mode="r", encoding="utf-8") as f:
            reader = list(csv.reader(f))

        if len(reader) <= 1:
            continue

        header = reader[0]
        # Map columns
        col_cost = next((i for i, h in enumerate(header) if re.search(r"cost|wholesale", h, re.I)), None)
        col_vel = next((i for i, h in enumerate(header) if re.search(r"velocity|monthly", h, re.I)), None)
        col_profit = next((i for i, h in enumerate(header) if re.search(r"net.*profit", h, re.I)), None)

        if col_cost is None or col_vel is None or col_profit is None:
            logger.warning(f"Could not map columns in {target.name}. Skipping.")
            continue

        retained = [header]
        archived = [header]

        for row in reader[1:]:
            cost = parse_num(row[col_cost]) if col_cost < len(row) else 0.0
            vel = parse_num(row[col_vel]) if col_vel < len(row) else 0.0
            profit = parse_num(row[col_profit]) if col_profit < len(row) else 0.0

            if vel >= STRICT_MIN_VELOCITY and STRICT_MIN_COST <= cost <= STRICT_MAX_COST and profit >= STRICT_MIN_PROFIT:
                retained.append(row)
            else:
                archived.append(row)

        # Write back retained
        with open(target, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerows(retained)

        # Append to archive file
        archive_path = data_dir / f"archived_{target.name}"
        with open(archive_path, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerows(archived)

        print(f"\n✅ Cleaned {target.name}:")
        print(f"   • Retained: {len(retained) - 1} top leads")
        print(f"   • Moved to {archive_path.name}: {len(archived) - 1} leads")

if __name__ == "__main__":
    clean_local_csv_files()
