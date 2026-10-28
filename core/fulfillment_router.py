import os
import logging
from typing import Dict, Any, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("FulfillmentRouter")

class FulfillmentRouter:
    """
    Directs physical inventory flow. Defaults strictly to Parkland, FL
    Self-Prep Station (LOCAL_HQ) to capture maximum margin on wholesale MRO.
    """
    PARKLAND_HQ_PREP_RATE = 0.35  # Polybag, 4x6 thermal barcode, suffocation warning
    DEFAULT_INBOUND_FREIGHT_PER_UNIT = 0.65

    def __init__(self, override_prep_fee: Optional[float] = None):
        self.prep_fee = override_prep_fee if override_prep_fee is not None else self.PARKLAND_HQ_PREP_RATE

    def route_inventory(self, asin: str, quantity: int, vendor: str, unit_weight_lbs: float = 1.0) -> Dict[str, Any]:
        """
        Calculates exact prep and routing economics for Parkland self-prep dock.
        """
        total_prep_materials = round(quantity * self.prep_fee, 2)
        total_inbound_freight = round(quantity * self.DEFAULT_INBOUND_FREIGHT_PER_UNIT, 2)
        
        # Heavy/Oversized handling adjustment
        oversize_surcharge = 0.0
        if unit_weight_lbs > 5.0:
            oversize_surcharge = round((unit_weight_lbs - 5.0) * 0.20 * quantity, 2)

        total_dock_cost = round(total_prep_materials + total_inbound_freight + oversize_surcharge, 2)

        return {
            "asin": asin.upper().strip(),
            "destination_id": "LOCAL_HQ",
            "destination_name": "Parkland, FL Prep Station",
            "quantity": quantity,
            "vendor": vendor,
            "unit_prep_fee": self.prep_fee,
            "total_prep_materials": total_prep_materials,
            "estimated_inbound_freight": total_inbound_freight,
            "oversize_surcharge": oversize_surcharge,
            "total_dock_prep_cost": total_dock_cost,
            "work_order_ready": True
        }


if __name__ == "__main__":
    router = FulfillmentRouter()
    routing = router.route_inventory("B08N5XYZ12", quantity=24, vendor="Global Industrial", unit_weight_lbs=1.8)
    print("Fulfillment Route Allocation:")
    print(routing)
