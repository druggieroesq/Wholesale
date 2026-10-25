import os
import pandas as pd
from typing import Dict, List, Any, Optional
from core.keepa_client import KeepaClient
from core.auditor import CatalogAuditor
from core.state_manager import StateManager
from config import MIN_BRAND_UNITS, DISTRIBUTOR_MOQ

class ProfitFirstEngine:
    def __init__(self, keepa_client: KeepaClient):
        self.keepa = keepa_client
        self.auditor = CatalogAuditor()
        self.state_mgr = StateManager()

    def calculate_economics(self, buy_box: float, wholesale_cost: float, weight_lbs: float = 1.0) -> Dict[str, float]:
        """Calculates exact FBA referral, fulfillment, and net profit margins."""
        if buy_box <= 0:
            return {"net_profit": 0, "roi": 0, "margin": 0}

        # Amazon referral fee: 8% if under $10 for beauty, 15% standard
        referral_rate = 0.08 if buy_box < 10.0 else 0.15
        referral_fee = buy_box * referral_rate

        # Estimated standard FBA pick & pack fee
        fba_fee = 3.80 + (max(0, weight_lbs - 1.0) * 0.40)

        # Inbound shipping allowance ($0.35/lb via UPS SPD)
        inbound_freight = weight_lbs * 0.35

        net_profit = buy_box - (wholesale_cost + referral_fee + fba_fee + inbound_freight)
        roi = (net_profit / wholesale_cost) if wholesale_cost > 0 else 0
        margin = (net_profit / buy_box)

        return {
            "net_profit": round(net_profit, 2),
            "roi": round(roi * 100, 1),
            "margin": round(margin * 100, 1)
        }

    def scan_catalog_for_profit(self, df: pd.DataFrame, min_roi: float = 35.0) -> pd.DataFrame:
        ungated_brands = self.state_mgr.get_ungated_brands()
        print(f"[*] Auditing catalog for optimal ROI (Target: >= {min_roi}% ROI)...")

        high_roi_prizes = []

        # 1. First Pass: Scan EVERY item for pure profit and sales velocity
        for _, row in df.iterrows():
            upc = str(row['upc']).zfill(12)
            cost = float(row['unit_cost'])
            
            keepa_data = self.keepa.get_product_by_upc(upc)
            if not keepa_data:
                continue

            audit = self.auditor.audit_sku_safety(keepa_data, cost)
            if not audit:
                continue

            econ = self.calculate_economics(audit['buy_box'], cost)
            
            # High-ROI threshold filter
            if econ['roi'] >= min_roi and econ['net_profit'] >= 3.00:
                high_roi_prizes.append({
                    "brand": row['brand'].strip(),
                    "upc": upc,
                    "description": row['description'],
                    "asin": audit['asin'],
                    "unit_cost": cost,
                    "case_qty": int(row.get('case_qty', 12)),
                    "buy_box": audit['buy_box'],
                    "roi": econ['roi'],
                    "net_profit": econ['net_profit'],
                    "monthly_velocity": audit['monthly_drops']
                })

        print(f"[+] Found {len(high_roi_prizes)} profitable target products across the catalog.")

        # Sort all lucrative products from highest ROI down
        prizes_df = pd.DataFrame(high_roi_prizes)
        if prizes_df.empty:
            return pd.DataFrame()

        prizes_df = prizes_df.sort_values(by="roi", ascending=False)

        # 2. Second Pass: Build the PO prioritizing Profit, Ungating ONLY as needed
        final_po_lines = []
        running_total = 0.0
        gated_brands_already_handled = set()

        for _, prize in prizes_df.iterrows():
            if running_total >= DISTRIBUTOR_MOQ:
                break

            brand_norm = prize['brand'].lower()

            if brand_norm in ungated_brands:
                # BRAND IS ALREADY UNGATED -> Buy the prize directly for pure profit!
                order_units = prize['case_qty']
                line_cost = prize['unit_cost'] * order_units
                
                final_po_lines.append({
                    "type": "PROFIT_INVENTORY",
                    "brand": prize['brand'],
                    "upc": prize['upc'],
                    "description": prize['description'],
                    "asin": prize['asin'],
                    "order_qty": order_units,
                    "unit_cost": prize['unit_cost'],
                    "line_total": round(line_cost, 2),
                    "roi_%": prize['roi'],
                    "note": "Brand already ungated; purchased for direct profit"
                })
                running_total += line_cost

            else:
                # BRAND IS GATED -> Check if we can ungate it from this same wholesaler
                if brand_norm not in gated_brands_already_handled:
                    # Find the cheapest SKU from this same brand in the wholesaler's catalog
                    brand_skus = df[df['brand'].str.lower() == brand_norm].sort_values(by="unit_cost", ascending=True)
                    
                    cheapest_key = None
                    for _, k_row in brand_skus.iterrows():
                        k_cost = float(k_row['unit_cost'])
                        k_units = int(k_row.get('case_qty', MIN_BRAND_UNITS))
                        if k_units < MIN_BRAND_UNITS:
                            k_units = MIN_BRAND_UNITS
                        
                        cheapest_key = {
                            "upc": str(k_row['upc']).zfill(12),
                            "description": k_row['description'],
                            "unit_cost": k_cost,
                            "order_qty": k_units,
                            "line_total": round(k_cost * k_units, 2)
                        }
                        break  # Found cheapest key

                    if cheapest_key:
                        # Add the cheap ungate sample to the PO
                        final_po_lines.append({
                            "type": "UNGATE_SAMPLE",
                            "brand": prize['brand'],
                            "upc": cheapest_key['upc'],
                            "description": cheapest_key['description'],
                            "asin": "PENDING_INVOICE",
                            "order_qty": cheapest_key['order_qty'],
                            "unit_cost": cheapest_key['unit_cost'],
                            "line_total": cheapest_key['line_total'],
                            "roi_%": 0.0,
                            "note": f"Sample key ordered to unlock {prize['description']} ({prize['roi']}% ROI)"
                        })
                        running_total += cheapest_key['line_total']

                        # Add the high-ROI prize product right alongside it
                        order_units = prize['case_qty']
                        line_cost = prize['unit_cost'] * order_units
                        final_po_lines.append({
                            "type": "TARGET_PROFIT_ITEM",
                            "brand": prize['brand'],
                            "upc": prize['upc'],
                            "description": prize['description'],
                            "asin": prize['asin'],
                            "order_qty": order_units,
                            "unit_cost": prize['unit_cost'],
                            "line_total": round(line_cost, 2),
                            "roi_%": prize['roi'],
                            "note": "Target product to list immediately once sample invoice clears"
                        })
                        running_total += line_cost
                        gated_brands_already_handled.add(brand_norm)

        return pd.DataFrame(final_po_lines)
