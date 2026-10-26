import os
import json
from typing import Dict, Any, List

class FulfillmentRouter:
    """
    Evaluates in-house hubs vs. registered commercial 3PLs.
    Calculates precise multi-pack bundling surcharges (2pk, 3pk, 4pk)
    and selects the facility yielding the highest net profit.
    """
    CONFIG_PATH = "data/3pl_registry.json"

    def __init__(self):
        self.facilities = self._load_facilities()

    def _load_facilities(self) -> List[Dict[str, Any]]:
        if os.path.exists(self.CONFIG_PATH):
            try:
                with open(self.CONFIG_PATH, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"[!] Warning reading 3PL registry: {e}")
        
        # Fallback default configuration if 3pl_registry.json is missing
        return [
            {
                "id": "hub_parkland",
                "name": "Parkland Self-Prep Hub (In-House)",
                "type": "IN_HOUSE",
                "city": "Parkland",
                "state": "FL",
                "rates": {"standard_label_fee": 0.0, "bundle_2pk": 0.15, "bundle_3pk": 0.20, "bundle_4pk": 0.25}
            },
            {
                "id": "hub_west_bloomfield",
                "name": "West Bloomfield Self-Prep Hub (In-House)",
                "type": "IN_HOUSE",
                "city": "West Bloomfield",
                "state": "MI",
                "rates": {"standard_label_fee": 0.0, "bundle_2pk": 0.15, "bundle_3pk": 0.20, "bundle_4pk": 0.25}
            },
            {
                "id": "commercial_3pl_default",
                "name": "Standard Regional 3PL",
                "type": "COMMERCIAL_3PL",
                "city": "Columbus",
                "state": "OH",
                "rates": {"standard_label_fee": 1.15, "bundle_2pk": 1.65, "bundle_3pk": 1.95, "bundle_4pk": 2.25}
            }
        ]

    def calculate_prep_cost(self, facility: Dict[str, Any], pack_qty: int = 1) -> float:
        rates = facility.get("rates", {})
        if pack_qty <= 1:
            return float(rates.get("standard_label_fee", 1.25))
        elif pack_qty == 2:
            return float(rates.get("bundle_2pk", 1.65))
        elif pack_qty == 3:
            return float(rates.get("bundle_3pk", 1.95))
        else:
            return float(rates.get("bundle_4pk", 2.25))

    def estimate_freight_per_lb(self, supplier_state: str, facility_state: str, is_pickup: bool = False) -> float:
        if is_pickup:
            return 0.00
        sup = str(supplier_state).strip().upper()
        fac = str(facility_state).strip().upper()
        if sup == fac:
            return 0.25
        east = {"FL", "GA", "SC", "NC", "VA", "MD", "PA", "NY", "NJ"}
        midwest = {"MI", "OH", "IN", "IL", "WI", "KY"}
        if (sup in east and fac in east) or (sup in midwest and fac in midwest):
            return 0.35
        return 0.50

    def find_optimal_fulfillment(
        self,
        supplier_meta: Dict[str, Any],
        retail_price: float,
        raw_product_cost: float,
        unit_weight_lbs: float,
        pack_qty: int = 1,
        allow_self_pickup: bool = False
    ) -> Dict[str, Any]:
        supplier_state = supplier_meta.get("state", "FL")
        best_option = None
        highest_net_profit = -999999.0

        for fac in self.facilities:
            is_in_house = fac.get("type") == "IN_HOUSE"
            is_pickup = False

            if is_in_house:
                if not allow_self_pickup:
                    continue
                is_pickup = True

            prep_cost = self.calculate_prep_cost(fac, pack_qty=pack_qty)
            freight_rate = self.estimate_freight_per_lb(supplier_state, fac.get("state", ""), is_pickup=is_pickup)
            total_freight = round(unit_weight_lbs * freight_rate, 2)
            landed_cost = round(raw_product_cost + prep_cost + total_freight, 2)

            referral = round(retail_price * 0.15, 2)
            fba_base = 3.50 + (0.40 * max(0, unit_weight_lbs - 1.0))
            net_profit = round(retail_price - referral - fba_base - landed_cost, 2)
            roi = round((net_profit / landed_cost) * 100.0, 1) if landed_cost > 0 else 0.0

            option = {
                "facility_name": fac["name"],
                "facility_type": fac.get("type", "COMMERCIAL_3PL"),
                "prep_cost": prep_cost,
                "inbound_freight": total_freight,
                "landed_cost": landed_cost,
                "net_profit": net_profit,
                "roi": roi
            }

            if net_profit > highest_net_profit:
                highest_net_profit = net_profit
                best_option = option

        return best_option or {
            "facility_name": "Standard Regional 3PL",
            "facility_type": "COMMERCIAL_3PL",
            "prep_cost": 1.25,
            "inbound_freight": round(unit_weight_lbs * 0.45, 2),
            "landed_cost": round(raw_product_cost + 1.25 + (unit_weight_lbs * 0.45), 2),
            "net_profit": 0.0,
            "roi": 0.0
        }
