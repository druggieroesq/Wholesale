#!/usr/bin/env python3
"""
tools/logistics_fee_engine.py
-----------------------------
Landed Logistics Calculator:
- BCI -> Makki Prep Center (PA) [$0.80 base + $0.25/lb freight]
- Marlo / JB Tools -> Detroit 3PL TBD Estimate [$0.65 base + $0.12/lb freight]
- Other / Future Mfrs -> Origin-based routing or fallback
"""

import sys
from pathlib import Path
from typing import Dict, Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config import get_logistics_profile


def compute_landed_logistics_cost(
    vendor: str,
    weight_lbs: float,
    is_hazmat: bool = False,
    is_bundle: bool = False
) -> Dict[str, Any]:
    profile = get_logistics_profile(vendor)
    
    prep_cost = profile["prep_base"]
    freight_rate = profile["freight_per_lb"]

    if is_hazmat:
        prep_cost += 0.60
        freight_rate += 0.20
    if is_bundle:
        prep_cost += 0.25

    inbound_freight = round(weight_lbs * freight_rate, 2)
    inbound_placement = 0.27
    total_logistics = round(prep_cost + inbound_freight + inbound_placement, 2)

    return {
        "facility": profile["facility"],
        "status": profile["status"],
        "prep_cost": prep_cost,
        "inbound_freight": inbound_freight,
        "inbound_placement": inbound_placement,
        "total_landed_logistics": total_logistics
    }
