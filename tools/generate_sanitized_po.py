#!/usr/bin/env python3
"""
tools/generate_sanitized_po.py
------------------------------
Air-Gapped Purchase Order & Manifest Sanitizer for Offshore Logistics VAs.
Strips wholesale costs, supplier identities, and financial margins.
Outputs blind tracking manifest with internal tokens (e.g. PO-MI-042).
"""

import os
import json
import logging
from pathlib import Path
from typing import List, Dict, Any
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parent.parent
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] BlindPO: %(message)s")
logger = logging.getLogger("BlindPO")

OUTPUT_DIR = ROOT_DIR / "data" / "va_logistics_manifests"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def create_sanitized_manifest(
    internal_po_id: str,
    carrier: str,
    tracking_number: str,
    destination_facility: str,
    fba_inbound_id: str,
    items: List[Dict[str, Any]]
) -> str:
    sanitized_items = []
    for item in items:
        sanitized_items.append({
            "Internal_PO": internal_po_id,
            "ASIN": item.get("asin", ""),
            "FNSKU": item.get("fnsku", "LABEL_AT_3PL"),
            "Product_Title": str(item.get("title", ""))[:45],
            "Units_Expected": item.get("quantity", 0),
            "Carrier": carrier,
            "Tracking_Number": tracking_number,
            "Destination_Prep_Hub": destination_facility,
            "Amazon_FBA_Shipment_ID": fba_inbound_id,
            "3PL_Receiving_Status": "In Transit",
            "Amazon_Receiving_Status": "Pending Dock Check-In",
            "Discrepancies": "None"
        })

    df = pd.DataFrame(sanitized_items)
    out_file = OUTPUT_DIR / f"{internal_po_id}_sanitized_manifest.csv"
    df.to_csv(out_file, index=False)
    logger.info(f"Sanitized manifest saved for offshore VA: {out_file}")
    return str(out_file)
