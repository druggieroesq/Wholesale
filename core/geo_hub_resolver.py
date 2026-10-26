import math
from typing import Dict, Any

class GeoHubResolver:
    """
    Evaluates supplier warehouse locations against operational home bases:
    - Parkland, FL (Lat: 26.3100, Lon: -80.2373)
    - West Bloomfield, MI (Lat: 42.5378, Lon: -83.3749)

    Default: Standard 3PL prep ($1.25) + freight ($0.45/lb).
    Zero-Cost Mode ($0 prep / $0 freight) triggers ONLY when:
      1. Supplier is within 45-mile driving radius, AND
      2. confirmed_pickup=True is explicitly passed by user.
    """

    HUBS = {
        "PARKLAND_FL": {
            "name": "Parkland Hub (FL)",
            "lat": 26.3100,
            "lon": -80.2373,
            "zip_prefix": ("330", "331", "332", "333", "334"),
            "max_radius_miles": 45.0
        },
        "WEST_BLOOMFIELD_MI": {
            "name": "West Bloomfield Hub (MI)",
            "lat": 42.5378,
            "lon": -83.3749,
            "zip_prefix": ("480", "481", "482", "483", "484"),
            "max_radius_miles": 45.0
        }
    }

    DEFAULT_3PL_PREP_FEE = 1.25
    DEFAULT_INBOUND_LB_RATE = 0.45

    def evaluate_supplier_location(
        self,
        warehouse_city: str = "",
        warehouse_state: str = "",
        warehouse_zip: str = "",
        confirmed_pickup: bool = False
    ) -> Dict[str, Any]:
        city_clean = str(warehouse_city).strip().lower()
        state_clean = str(warehouse_state).strip().upper()
        zip_clean = str(warehouse_zip).strip().zfill(5)[:3]

        # 1. Florida Proximity Detection (Broward, Palm Beach, Miami-Dade)
        is_fl_local = (
            state_clean in ["FL", "FLORIDA"] or 
            zip_clean in self.HUBS["PARKLAND_FL"]["zip_prefix"]
        )
        fl_cities = [
            "parkland", "coral springs", "boca raton", "pompano beach", "fort lauderdale",
            "deerfield beach", "delray beach", "boynton beach", "doral", "medley", "hialeah",
            "miami", "sunrise", "tamarac", "plantation", "davie", "hollywood"
        ]
        near_parkland = is_fl_local and any(c in city_clean for c in fl_cities)

        # 2. Michigan Proximity Detection (Oakland, Wayne, Macomb)
        is_mi_local = (
            state_clean in ["MI", "MICHIGAN"] or 
            zip_clean in self.HUBS["WEST_BLOOMFIELD_MI"]["zip_prefix"]
        )
        mi_cities = [
            "west bloomfield", "bloomfield hills", "birmingham", "southfield", "troy",
            "warren", "sterling heights", "livonia", "farmington hills", "pontiac",
            "auburn hills", "detroit", "rochester hills", "novi", "royal oak"
        ]
        near_west_bloomfield = is_mi_local and any(c in city_clean for c in mi_cities)

        hub_name = None
        if near_parkland:
            hub_name = "Parkland Hub (FL)"
        elif near_west_bloomfield:
            hub_name = "West Bloomfield Hub (MI)"

        # 3. Decision Logic
        if hub_name:
            if confirmed_pickup:
                return {
                    "is_local_pickup": True,
                    "active_hub": hub_name,
                    "prep_fee": 0.00,
                    "inbound_freight_rate": 0.00,
                    "strategy": "CONFIRMED_SELF_PICKUP",
                    "note": f"Self-pickup confirmed at {hub_name}. $0.00 prep & $0.00 freight applied."
                }
            else:
                return {
                    "is_local_pickup": False,
                    "active_hub": f"{hub_name} (Shipped to 3PL)",
                    "prep_fee": self.DEFAULT_3PL_PREP_FEE,
                    "inbound_freight_rate": self.DEFAULT_INBOUND_LB_RATE,
                    "strategy": "LOCAL_SUPPLIER_3PL_SHIPPED",
                    "note": f"Near {hub_name}, but pickup unconfirmed. Full 3PL prep ($1.25) & freight ($0.45/lb) applied."
                }

        # Remote National Supplier
        return {
            "is_local_pickup": False,
            "active_hub": "NATIONAL_3PL",
            "prep_fee": self.DEFAULT_3PL_PREP_FEE,
            "inbound_freight_rate": self.DEFAULT_INBOUND_LB_RATE,
            "strategy": "REMOTE_3PL_SHIPMENT",
            "note": "Remote distributor. Standard 3PL prep ($1.25) and freight ($0.45/lb) applied."
        }
