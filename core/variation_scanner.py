from typing import List, Dict, Any

class VariationScanner:
    @staticmethod
    def identify_multipack_opportunities(parent_prod: Dict[str, Any], unit_cost: float) -> List[Dict[str, Any]]:
        variations = parent_prod.get("variations", [])
        if not variations:
            return []

        multipack_deals = []
        for var in variations:
            # Check package quantity attribute
            dimensions = var.get("attributes", [])
            pack_count = 1
            for dim in dimensions:
                if dim.get("dimension") in ["Package Quantity", "Number of Items"]:
                    try:
                        pack_count = int(dim.get("value", 1))
                    except ValueError:
                        pack_count = 1

            if pack_count > 1:
                # Bundled cost = (Unit Cost * Pack Count) + $0.35 Polybag/Prep Cost
                bundle_cost = (unit_cost * pack_count) + 0.35
                multipack_deals.append({
                    "pack_count": pack_count,
                    "bundle_asin": var.get("asin"),
                    "bundle_cost": round(bundle_cost, 2)
                })
        return multipack_deals
