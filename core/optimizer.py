import pandas as pd
from typing import List, Dict, Any
from core.keepa_client import KeepaClient
from core.auditor import CatalogAuditor
from config import MIN_BRAND_UNITS, DISTRIBUTOR_MOQ

class UngateOptimizer:
    def __init__(self, keepa_client: KeepaClient):
        self.keepa = keepa_client
        self.auditor = CatalogAuditor()

    def process_catalog(self, df: pd.DataFrame) -> pd.DataFrame:
        brands = df['brand'].dropna().str.strip().unique()
        print(f"[*] Processing {len(brands)} unique brands...")

        brand_keys = []

        for brand in brands:
            brand_items = df[df['brand'].str.lower() == brand.lower()]
            cheapest_items = brand_items.sort_values(by="unit_cost", ascending=True)

            for _, row in cheapest_items.iterrows():
                upc = str(row['upc']).zfill(12)
                keepa_data = self.keepa.get_product_by_upc(upc)
                if not keepa_data:
                    continue

                audit_result = self.auditor.audit_sku_safety(keepa_data, float(row['unit_cost']))
                if audit_result:
                    case_qty = int(row.get('case_qty', MIN_BRAND_UNITS))
                    order_units = case_qty if case_qty >= MIN_BRAND_UNITS else MIN_BRAND_UNITS
                    line_total = round(float(row['unit_cost']) * order_units, 2)

                    brand_keys.append({
                        "brand": brand,
                        "upc": upc,
                        "description": row['description'],
                        "asin": audit_result['asin'],
                        "unit_cost": float(row['unit_cost']),
                        "order_qty": order_units,
                        "line_total": line_total,
                        "buy_box": audit_result['buy_box'],
                        "monthly_velocity": audit_result['monthly_drops']
                    })
                    break

        sorted_keys = sorted(brand_keys, key=lambda x: x["line_total"])

        po_items: List[Dict[str, Any]] = []
        running_total = 0.0

        for item in sorted_keys:
            if running_total >= DISTRIBUTOR_MOQ:
                break
            po_items.append(item)
            running_total += item["line_total"]

        print(f"[+] Ungate PO constructed: {len(po_items)} brands selected. Total: ${running_total:,.2f}")
        return pd.DataFrame(po_items)
