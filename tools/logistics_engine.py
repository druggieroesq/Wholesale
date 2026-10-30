"""
tools/logistics_engine.py
National Logistics, Multi-3PL Routing & Landed Freight Optimization Engine.
Dynamically maps purchases to the optimal 3PL facility based on geographic proximity,
prep fees, freight minimums, and physical warehouse constraints.
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

from dotenv import load_dotenv
load_dotenv(ROOT_DIR / ".env", override=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] LogisticsEngine: %(message)s")
logger = logging.getLogger("LogisticsEngine")

REGISTRY_PATH = ROOT_DIR / Path(os.getenv("REGISTRY_3PL_FILE", "data/3pl_registry.json"))

STATE_REGIONS = {
    "FL": "SOUTH_FLORIDA", "GA": "SOUTHEAST", "NC": "SOUTHEAST", "SC": "SOUTHEAST", "AL": "SOUTHEAST",
    "PA": "NORTHEAST", "NY": "NORTHEAST", "NJ": "NORTHEAST", "MA": "NORTHEAST", "CT": "NORTHEAST",
    "OH": "MIDWEST", "IL": "MIDWEST", "IN": "MIDWEST", "MI": "MIDWEST", "WI": "MIDWEST", "MO": "MIDWEST",
    "TX": "SOUTH_CENTRAL", "OK": "SOUTH_CENTRAL", "LA": "SOUTH_CENTRAL",
    "CA": "WEST_COAST", "NV": "WEST_COAST", "OR": "PACIFIC_NW", "WA": "PACIFIC_NW", "AZ": "SOUTHWEST"
}

DISTRIBUTOR_ORIGINS = {
    "midland_metal": {"state": "MO", "free_freight_min": float(os.getenv("MIDLAND_FREE_FREIGHT_MIN", 250.0)), "under_min_rate_per_lb": 0.40, "is_will_call": False},
    "atlantic_spice": {"state": "MA", "free_freight_min": float(os.getenv("ATLANTIC_SPICE_FREE_FREIGHT_MIN", 99.0)), "under_min_rate_per_lb": 0.35, "is_will_call": False},
    "frontier_coop": {"state": "IA", "free_freight_min": 250.00, "under_min_rate_per_lb": 0.40, "is_will_call": False},
    "jb_tools": {"state": "MI", "free_freight_min": float(os.getenv("JB_TOOLS_FREE_FREIGHT_MIN", 99.0)), "under_min_rate_per_lb": 0.35, "is_will_call": False},
    "bci": {"state": "FL", "free_freight_min": None, "is_will_call": False, "ltl_freight_pct": float(os.getenv("BCI_ESTIMATED_LTL_FREIGHT_PCT", 0.08))},
    "global_industrial": {"state": "GA", "free_freight_min": None, "is_will_call": False, "under_min_rate_per_lb": 0.45},
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
        self.parkland_prep = float(os.getenv("PARKLAND_PREP_FEE_UNIT", 0.06))

    def load_registry(self) -> List[Dict[str, Any]]:
        if not self.registry_file.exists():
            return [
                {
                    "id": "PARKLAND_HQ",
                    "name": "Parkland Self-Prep & Inbound Station",
                    "region": "SOUTH_FLORIDA",
                    "state": "FL",
                    "zip": "33076",
                    "pricing": {"receiving_fee_unit": 0.0, "base_prep_unit": float(os.getenv("PARKLAND_PREP_FEE_UNIT", 0.06)), "multipack_addon_unit": 0.10},
                    "constraints": {"max_weight_per_unit_lbs": 45.0, "max_dimensions_inches": [24.0, 24.0, 24.0], "accepts_hazmat": True}
                }
            ]
        try:
            with open(self.registry_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("nodes", [])
        except Exception as e:
            logger.error(f"Error reading 3PL registry file: {e}")
            return []

    def estimate_transit_freight(self, origin_state: str, dest_node: Dict[str, Any], weight_lbs: float) -> float:
        dest_state = dest_node.get("state", "FL")
        origin_region = STATE_REGIONS.get(origin_state, "MIDWEST")
        dest_region = dest_node.get("region", "SOUTH_FLORIDA")

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
        vendor_clean = vendor_key.lower().replace(" ", "_").replace(".", "")
        vendor_info = DISTRIBUTOR_ORIGINS.get(vendor_clean, {"state": "FL", "free_freight_min": None, "is_will_call": False})

        # Local South Florida Will-Call Rule
        if "will_call" in vendor_clean or vendor_info.get("is_will_call", False):
            prep_unit = self.parkland_prep if not is_multipack else (self.parkland_prep + 0.10)
            return {
                "assigned_node_id": "PARKLAND_HQ",
                "assigned_node_name": "Parkland Self-Prep & Inbound Station",
                "inbound_freight_unit": 0.00,
                "prep_fee_unit": prep_unit,
                "total_fulfillment_overhead": prep_unit,
                "routing_reason": "LOCAL_WILL_CALL_ZERO_FREIGHT"
            }

        candidate_evaluations = []
        origin_state = vendor_info.get("state", "FL")
        free_freight_threshold = vendor_info.get("free_freight_min")

        for node in self.nodes:
            constraints = node.get("constraints", {})
            pricing = node.get("pricing", {})

            if weight_lbs > constraints.get("max_weight_per_unit_lbs", 50.0):
                continue

            max_dims = constraints.get("max_dimensions_inches", [24.0, 24.0, 24.0])
            if any(dim > max_dim for dim, max_dim in zip(sorted(dimensions_inches), sorted(max_dims))):
                continue

            if is_hazmat and not constraints.get("accepts_hazmat", False):
                continue

            base_prep = pricing.get("base_prep_unit", 1.25)
            receiving_fee = pricing.get("receiving_fee_unit", 0.0)
            multipack_addon = pricing.get("multipack_addon_unit", 0.0) if is_multipack else 0.0
            total_prep = round(base_prep + receiving_fee + multipack_addon, 2)

            if free_freight_threshold is not None and order_total >= free_freight_threshold:
                inbound_freight = 0.00
                freight_desc = f"THRESHOLD_MET_FREE_SHIPPING (>=${free_freight_threshold})"
            elif "ltl_freight_pct" in vendor_info:
                inbound_freight = round(unit_cost * vendor_info["ltl_freight_pct"], 2)
                freight_desc = "ESTIMATED_LTL_FREIGHT"
            else:
                inbound_freight = self.estimate_transit_freight(origin_state, node, weight_lbs)
                freight_desc = f"ZONE_ESTIMATE ({origin_state} -> {node.get('state')})"

            total_overhead = round(inbound_freight + total_prep, 2)

            candidate_evaluations.append({
                "assigned_node_id": node["id"],
                "assigned_node_name": node["name"],
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
                "prep_fee_unit": self.parkland_prep,
                "total_fulfillment_overhead": round(0.65 + self.parkland_prep, 2),
                "routing_reason": "DEFAULT_FALLBACK_NO_QUALIFIED_3PL"
            }

        candidate_evaluations.sort(key=lambda x: x["total_fulfillment_overhead"])
        return candidate_evaluations[0]


if __name__ == "__main__":
    engine = LogisticsEngine()
    res = engine.route_purchase_order(vendor_key="hydraulic_supply", unit_cost=15.0)
    print(f"Routed to: {res['assigned_node_name']} | Overhead: ${res['total_fulfillment_overhead']}")
