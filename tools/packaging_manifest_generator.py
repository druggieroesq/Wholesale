import os
import sys
import json
from datetime import datetime
from typing import Dict, Any, List

ACTIONS_FILE = "data/product_actions.json"
MANIFEST_DIR = "reports/manifests"

class PackagingManifestGenerator:
    """
    Generates thermal packing manifests and prep instruction sheets
    calibrated for the Parkland, FL Self-Prep fulfillment station.
    """
    def __init__(self):
        os.makedirs(MANIFEST_DIR, exist_ok=True)

    def load_purchased_inventory(self) -> Dict[str, Any]:
        if not os.path.exists(ACTIONS_FILE):
            return {}
        try:
            with open(ACTIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("purchased", {})
        except Exception:
            return {}

    def generate_daily_manifest(self) -> str:
        purchases = self.load_purchased_inventory()
        date_stamp = datetime.utcnow().strftime("%Y-%m-%d")
        file_path = os.path.join(MANIFEST_DIR, f"manifest_parkland_hq_{date_stamp}.txt")

        total_units = 0
        total_prep_materials_cost = 0.0
        total_inventory_spend = 0.0

        manifest_lines = [
            "================================================================================",
            "                   WHOLESALE AUTOMATION: PHYSICAL PREP MANIFEST                ",
            "                  FULFILLMENT DOCK: PARKLAND, FL STATION (LOCAL_HQ)            ",
            f"                  DATE GENERATED: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}                  ",
            "================================================================================",
            "",
            f"{'ASIN':<12} | {'QTY':<5} | {'UNIT COST':<10} | {'TOTAL COST':<11} | {'PREP FEE':<9} | {'VENDOR':<18}",
            "--------------------------------------------------------------------------------"
        ]

        if not purchases:
            manifest_lines.append("  [NO NEW PURCHASE ORDERS CURRENTLY LOGGED IN LEDGER]")
        else:
            for asin, details in purchases.items():
                qty = int(details.get("quantity", 0))
                unit_cost = float(details.get("unit_cost", 0.0))
                total_cost = float(details.get("total_inventory_cost", qty * unit_cost))
                prep_cost = float(details.get("total_prep_cost", qty * 0.35))
                vendor = details.get("vendor", "Wholesaler")[:18]

                total_units += qty
                total_prep_materials_cost += prep_cost
                total_inventory_spend += total_cost

                manifest_lines.append(
                    f"{asin:<12} | {qty:<5} | ${unit_cost:<9.2f} | ${total_cost:<10.2f} | ${prep_cost:<8.2f} | {vendor:<18}"
                )

        manifest_lines.extend([
            "--------------------------------------------------------------------------------",
            f"TOTAL INVENTORY UNITS:      {total_units}",
            f"TOTAL INVENTORY OUTLAY:     ${total_inventory_spend:.2f}",
            f"TOTAL PACKAGING MATERIALS:  ${total_prep_materials_cost:.2f} (@ $0.35 / unit standard)",
            "================================================================================",
            "",
            "WORK ORDER CHECKLIST FOR PARKLAND DOCK:",
            "  [ ] 1. Inspect distributor shipping boxes for exterior transport damage.",
            "  [ ] 2. Cross-reference received quantity against vendor packing slip.",
            "  [ ] 3. Print 4x6 thermal barcode labels (FNSKU) for each individual unit.",
            "  [ ] 4. Suffocation warning polybags applied to any soft goods / open liquids.",
            "  [ ] 5. Apply 'Sold As Set' stickers if bundled / multipack.",
            "  [ ] 6. Weigh packed carton, apply Amazon Inbound SPD label, stage for UPS pickup.",
            "================================================================================"
        ])

        output_content = "\n".join(manifest_lines)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(output_content)

        print(f"[MANIFEST] Generated: {file_path}")
        print(output_content)
        return file_path


if __name__ == "__main__":
    generator = PackagingManifestGenerator()
    generator.generate_daily_manifest()
