import os
import sys
import logging
from typing import Dict, Any, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("MultipackSynthesizer")

MAX_BUNDLE_WEIGHT_LBS = 45.0  # Amazon FBA physical shipping cap

class MultipackSynthesizer:
    """
    Evaluates multi-pack bundles (e.g., Atlantic Spice 1 lb / 5 lb bags or Midland fittings)
    and enforces maximum allowable bundle weights.
    """
    PARKLAND_PREP_BASE = 0.35
    MULTIPACK_STICKER_FEE = 0.10  # 'Sold As Set' label + additional polybag wrap

    @classmethod
    def synthesize_bundle(
        cls,
        single_unit_cost: float,
        bundle_pack_qty: int,
        target_amazon_price: float,
        unit_weight_lbs: float,
        vendor: str = "Wholesale Vendor"
    ) -> Optional[Dict[str, Any]]:
        total_bundle_weight = round(unit_weight_lbs * bundle_pack_qty, 2)

        # Enforce Amazon physical limit
        if total_bundle_weight > MAX_BUNDLE_WEIGHT_LBS:
            logger.warning(
                f"Bundle rejected: {bundle_pack_qty}x @ {unit_weight_lbs} lbs = {total_bundle_weight} lbs "
                f"(exceeds {MAX_BUNDLE_WEIGHT_LBS} lb limit)."
            )
            return None

        total_wholesale_cost = round(single_unit_cost * bundle_pack_qty, 2)
        total_prep_fee = round(cls.PARKLAND_PREP_BASE + cls.MULTIPACK_STICKER_FEE, 2)
        inbound_shipping_est = round(max(0.75, total_bundle_weight * 0.40), 2)

        landed_cost = round(total_wholesale_cost + total_prep_fee + inbound_shipping_est, 2)

        referral_fee = round(target_amazon_price * 0.15, 2)
        if total_bundle_weight <= 1.0:
            fba_fee = 3.86
        elif total_bundle_weight <= 2.0:
            fba_fee = 5.40
        elif total_bundle_weight <= 3.0:
            fba_fee = 6.10
        else:
            fba_fee = round(6.10 + (total_bundle_weight - 3.0) * 0.45, 2)

        total_amazon_fees = round(referral_fee + fba_fee, 2)
        net_profit = round(target_amazon_price - total_amazon_fees - landed_cost, 2)
        roi_pct = round((net_profit / landed_cost) * 100.0, 1) if landed_cost > 0 else 0.0

        passes_floor = roi_pct >= 50.0

        return {
            "vendor": vendor,
            "bundle_pack_qty": bundle_pack_qty,
            "unit_weight_lbs": unit_weight_lbs,
            "total_bundle_weight_lbs": total_bundle_weight,
            "wholesale_cost_total": total_wholesale_cost,
            "parkland_prep_total": total_prep_fee,
            "landed_cost": landed_cost,
            "amazon_price": target_amazon_price,
            "total_amazon_fees": total_amazon_fees,
            "net_profit": net_profit,
            "roi_pct": roi_pct,
            "passes_50pct_roi": passes_floor,
            "required_stickers": ["FNSKU Barcode", "Sold As Set - Do Not Separate", "Suffocation Warning"]
        }


if __name__ == "__main__":
    synth = MultipackSynthesizer()
    print("Testing 2-Pack 5 lb Atlantic Spice Bundle (10 lbs total):")
    res1 = synth.synthesize_bundle(single_unit_cost=18.50, bundle_pack_qty=2, target_amazon_price=69.99, unit_weight_lbs=5.0, vendor="atlantic_spice")
    print(res1)

    print("\nTesting 50 lb Bulk Sack (Should Be Rejected):")
    res2 = synth.synthesize_bundle(single_unit_cost=45.00, bundle_pack_qty=1, target_amazon_price=120.00, unit_weight_lbs=52.0, vendor="atlantic_spice")
    print(res2)
