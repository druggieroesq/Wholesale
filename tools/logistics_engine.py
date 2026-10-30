"""
tools/logistics_engine.py
National Logistics, Multi-3PL Routing & Landed Freight Optimization Engine.

Features:
- Dynamically loads national 3PL nodes and constraint matrices from data/3pl_registry.json.
- Checks distributor origin points and free freight thresholds (Midland $250, Atlantic Spice $99, etc.).
- Evaluates zero-freight local will-call for South Florida hubs.
- Factors in unit weight, box dimensions, Hazmat status, receiving fees, and prep costs.
- Automatically assigns the purchase order to the facility minimizing landed cost.
"""

import os
import sys
import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] LogisticsEngine: %(message)s")
logger = logging.getLogger("LogisticsEngine")

REGISTRY_PATH = ROOT_DIR / "data" / "3pl_registry.json"

# State-to-Region Zone Mapping for Inbound Transit Modeling
STATE_REGIONS = {
    "FL": "SOUTH_FLORIDA", "GA": "SOUTHEAST", "NC": "SOUTHEAST", "SC": "SOUTHEAST", "AL": "SOUTHEAST",
    "PA": "NORTHEAST", "NY": "NORTHEAST", "NJ": "NORTHEAST", "MA": "NORTHEAST", "CT": "NORTHEAST",
    "OH": "MIDWEST", "IL": "MIDWEST", "IN": "MIDWEST", "MI": "MIDWEST", "WI": "MIDWEST", "MO": "MIDWEST",
    "TX": "SOUTH_CENTRAL", "OK": "SOUTH_CENTRAL", "LA": "SOUTH_CENTRAL",
    "CA": "WEST_COAST", "NV": "WEST_COAST", "OR": "PACIFIC_NW", "WA": "PACIFIC_NW", "AZ": "SOUTHWEST"
}

# Known Wholesaler Origin Locations & Shipping Policies
DISTRIBUTOR_ORIGINS = {
    # Free Freight Threshold Wholesalers
    "midland_metal": {"state": "MO", "free_freight_min": 250.00, "under_min_rate_per_lb": 0.40, "is_will_call": False},
    "atlantic_spice": {"state": "MA", "free_freight_min": 99.00, "under_min_rate_per_lb": 0.35, "is_will_call": False},
    "frontier_coop": {"state": "IA", "free_freight_min": 250.00, "under_min_rate_per_lb": 0.40, "is_will_call": False},
    "jb_tools": {"state": "MI", "free_freight_min": 99.00, "under_min_rate_per_lb": 0.35, "is_will_call": False},
    "marlo_beauty": {"state": "MI", "free_freight_min": 99.00, "under_min_rate_per_lb": 0.35, "is_will_call": False},

    # Common Carrier / LTL / Parcel Distributors
    "bci": {"state": "FL", "free_freight_min": None, "is_will_call": False, "ltl_freight_pct": 0.08},
    "global_industrial": {"state": "GA", "free_freight_min": None, "is_will_call": False, "under_min_rate_per_lb": 0.45},

    # South Florida Will-Call Counters (0 Freight)
    "action_bolt": {"state": "FL", "free_freight_min": 0.0, "is_will_call": True},
    "aetna_hardware": {"state": "FL", "free_freight_min": 0.0, "is_will_call": True},
    "hydraulic_supply": {"state": "FL", "free_freight_min": 0.0, "is_will_call": True},
    "american_fasteners": {"state": "FL", "free_freight_min": 0.0, "is_will_call": True},
    "south_florida_fasteners": {"state": "FL", "free_freight_min": 0.0, "is_will_call": True},
    "florida_tool": {"state": "FL", "free_freight_min": 0.0, "is_will_call": True},
    "standard_supply": {"state": "FL", "free_freight_min": 0.0, "is_will_call": True},
    "atlantic_pipe": {"state": "FL", "free_freight_min": 0.0, "is_will_call": True},
    "hughes_supply": {"state": "FL", "free_freight_min": 0.0, "is_will_call": True},
    "lehman_pipe": {"state": "FL", "free_freight_min": 0.0, "is_will_call": True},
    "best_line_industrial": {"state": "FL", "free_freight_min": 0.0, "is_will_call": True},
    "grainger_local": {"state": "FL", "free_freight_min": 0.0, "is_will_call": True}
}


class LogisticsEngine:
    def __init__(self, registry_file: Path = REGISTRY_PATH):
        self.registry_file = registry_file
        self.nodes = self.load_registry()

    def load_registry(self) -> List[Dict[str, Any]]:
        """Loads registered 3PL nodes and prep centers from JSON."""
        if not self.registry_file.exists():
            logger.warning(f"3PL Registry not found at {self.registry_file}. Falling back to Parkland HQ.")
            return [
                {
                    "id": "PARKLAND_HQ",
                    "name": "Parkland Self-Prep & Inbound Station",
                    "region": "SOUTH_FLORIDA",
                    "state": "FL",
                    "zip": "33076",
                    "pricing": {"receiving_fee_unit": 0.0, "base_prep_unit": 0.06, "multipack_addon_unit": 0.10},
                    "constraints": {"max_weight_per_unit_lbs": 45.0, "max_dimensions_inches": [24.0, 24.0, 24.0], "accepts_hazmat": True}
                }
            ]
        try:
            with open(self.registry_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("nodes", [])[cite: 2]
        except Exception as e:
            logger.error(f"Error reading 3PL registry file: {e}")
            return []

    def estimate_transit_freight(self, origin_state: str, dest_node: Dict[str, Any], weight_lbs: float) -> float:
        """
        Estimates commercial ground/LTL transit cost per unit based on shipping zones.
        Intra-state short haul: $0.25/lb | Regional: $0.35-$0.40/lb | Long-haul cross-country: $0.65/lb
        """
        dest_state = dest_node.get("state", "FL")[cite: 2]
        origin_region = STATE_REGIONS.get(origin_state, "MIDWEST")
        dest_region = dest_node.get("region", "SOUTH_FLORIDA")[cite: 2]

        if origin_state == dest_state:
            rate_per_lb = 0.25
        elif origin_region == dest_region:
            rate_per_lb = 0.35
        elif (origin_region in ["NORTHEAST", "MIDWEST"] and dest_region in ["NORTHEAST", "MIDWEST"]):
            rate_per_lb = 0.40
        else:
            rate_per_lb = 0.65

        return round(max(0.75, weight_lbs * rate_per_lb), 2)

    def route_purchase_order(
        self,
        vendor_key: str,
        unit_cost: float,
        weight_lbs: float = 1.5,
        dimensions_inches: Tuple[float, float, float] = (6.0, 6.0, 6.0),
        order_total: float = 0.0,
        is_hazmat: bool = False,
        is_multipack: bool = False
    ) -> Dict[str, Any]:
        """
        Evaluates physical constraints and compares all active 3PL facilities to find
        the destination node that yields the lowest landed fulfillment overhead.
        """
        vendor_clean = vendor_key.lower().replace(" ", "_").replace(".", "")
        vendor_info = DISTRIBUTOR_ORIGINS.get(vendor_clean, {"state": "FL", "free_freight_min": None, "is_will_call": False})

        # Local South Florida Will-Call Rule: Always routes to Parkland HQ for $0 freight
        if "will_call" in vendor_clean or vendor_info.get("is_will_call", False):
            return {
                "assigned_node_id": "PARKLAND_HQ",
                "assigned_node_name": "Parkland Self-Prep & Inbound Station",
                "inbound_freight_unit": 0.00,
                "prep_fee_unit": 0.06 if not is_multipack else 0.16,
                "total_fulfillment_overhead": 0.06 if not is_multipack else 0.16,
                "routing_reason": "LOCAL_WILL_CALL_ZERO_FREIGHT"
            }

        candidate_evaluations = []
        origin_state = vendor_info.get("state", "FL")
        free_freight_threshold = vendor_info.get("free_freight_min")

        for node in self.nodes:
            constraints = node.get("constraints", {})[cite: 2]
            pricing = node.get("pricing", {})[cite: 2]

            # 1. Physical Constraint: Max Weight
            if weight_lbs > constraints.get("max_weight_per_unit_lbs", 50.0):[cite: 2]
                continue

            # 2. Physical Constraint: Dimensions
            max_dims = constraints.get("max_dimensions_inches", [24.0, 24.0, 24.0])[cite: 2]
            if any(dim > max_dim for dim, max_dim in zip(sorted(dimensions_inches), sorted(max_dims))):
                continue

            # 3. Regulatory Constraint: Hazmat Acceptance
            if is_hazmat and not constraints.get("accepts_hazmat", False):[cite: 2]
                continue

            # 4. Prep & Intake Fee Modeling
            base_prep = pricing.get("base_prep_unit", 1.25)[cite: 2]
            receiving_fee = pricing.get("receiving_fee_unit", 0.0)[cite: 2]
            multipack_addon = pricing.get("multipack_addon_unit", 0.0) if is_multipack else 0.0[cite: 2]
            total_prep = round(base_prep + receiving_fee + multipack_addon, 2)

            # 5. Inbound Freight Modeling
            if free_freight_threshold is not None and order_total >= free_freight_threshold:
                inbound_freight = 0.00
                freight_desc = f"THRESHOLD_MET_FREE_SHIPPING (>=${free_freight_threshold})"
            elif "ltl_freight_pct" in vendor_info:
                inbound_freight = round(unit_cost * vendor_info["ltl_freight_pct"], 2)
                freight_desc = "ESTIMATED_LTL_FREIGHT"
            else:
                inbound_freight = self.estimate_transit_freight(origin_state, node, weight_lbs)
                freight_desc = f"ZONE_ESTIMATE ({origin_state} -> {node.get('state')})"[cite: 2]

            total_overhead = round(inbound_freight + total_prep, 2)

            candidate_evaluations.append({
                "assigned_node_id": node["id"],[cite: 2]
                "assigned_node_name": node["name"],[cite: 2]
                "inbound_freight_unit": inbound_freight,
                "prep_fee_unit": total_prep,
                "total_fulfillment_overhead": total_overhead,
                "routing_reason": f"OPTIMAL_LANDED_COST: {freight_desc}"
            })

        if not candidate_evaluations:
            return {
                "assigned_node_id": "PARKLAND_HQ",
                "assigned_node_name": "Parkland Self-Prep & Inbound Station",
                "inbound_freight_unit": 0.65,
                "prep_fee_unit": 0.06,
                "total_fulfillment_overhead": 0.71,
                "routing_reason": "DEFAULT_FALLBACK_NO_QUALIFIED_3PL"
            }

        # Select the facility providing lowest total fulfillment overhead (Freight + Prep)
        candidate_evaluations.sort(key=lambda x: x["total_fulfillment_overhead"])
        return candidate_evaluations[0]


if __name__ == "__main__":
    engine = LogisticsEngine()

    print("\n--- Logistics Engine Operational Routing Tests ---")
    
    # 1. Local Will Call Pickup
    res_wc = engine.route_purchase_order(vendor_key="hydraulic_supply_will_call", unit_cost=18.50, weight_lbs=3.0)
    print(f"1. Will-Call: {res_wc['assigned_node_name']} | Freight: ${res_wc['inbound_freight_unit']} | Overhead: ${res_wc['total_fulfillment_overhead']}")

    # 2. Midland Metal under threshold (Missouri to Parkland vs Regional Hubs)
    res_mid = engine.route_purchase_order(vendor_key="midland_metal", unit_cost=24.00, weight_lbs=6.5, order_total=110.0)
    print(f"2. Midland Sub-Threshold: {res_mid['assigned_node_name']} | Freight: ${res_mid['inbound_freight_unit']} | Overhead: ${res_mid['total_fulfillment_overhead']}")

    # 3. Atlantic Spice clearing $99 free freight threshold
    res_spc = engine.route_purchase_order(vendor_key="atlantic_spice", unit_cost=32.00, weight_lbs=2.5, order_total=145.0)
    print(f"3. Atlantic Spice Free Freight: {res_spc['assigned_node_name']} | Freight: ${res_spc['inbound_freight_unit']} | Overhead: ${res_spc['total_fulfillment_overhead']}")

    # 4. BCI LTL Order ($750 Min Order)
    res_bci = engine.route_purchase_order(vendor_key="bci", unit_cost=15.00, weight_lbs=1.2, order_total=750.0)
    print(f"4. BCI Inbound Order: {res_bci['assigned_node_name']} | Freight: ${res_bci['inbound_freight_unit']} | Overhead: ${res_bci['total_fulfillment_overhead']}")
