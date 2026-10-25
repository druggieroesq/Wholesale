import os
import pandas as pd
from typing import Dict, List, Any, Optional
from core.keepa_client import KeepaClient
from core.auditor import CatalogAuditor
from core.state_manager import StateManager
from config import MIN_BRAND_UNITS, DISTRIBUTOR_MOQ

class DynamicFeeCalculator:
    """
    Self-updating Amazon fee engine.
    Extracts live, verified fee numbers from Keepa's database (which mirrors Amazon's rate cards).
    Falls back gracefully to standard tiered rate formulas when run in mock/offline mode.
    """
    FALLBACK_FBA_TIERS = [
        {"max_weight_lbs": 0.25, "fee": 3.22},
        {"max_weight_lbs": 0.50, "fee": 3.40},
        {"max_weight_lbs": 0.75, "fee": 3.58},
        {"max_weight_lbs": 1.00, "fee": 3.77},
        {"max_weight_lbs": 1.50, "fee": 4.42},
        {"max_weight_lbs": 2.00, "fee": 4.95},
        {"max_weight_lbs": 3.00, "fee": 5.45},
    ]
    PER_LB_OVER_3_LBS = 0.38

    @classmethod
    def get_fba_fulfillment_fee(cls, keepa_prod: Optional[Dict[str, Any]] = None, fallback_weight_lbs: float = 1.0) -> float:
        if keepa_prod:
            # Keepa reports live FBA pick-and-pack fee in cents (e.g., 377 = $3.77)
            live_fee_cents = keepa_prod.get("fbaFees")
            if live_fee_cents and isinstance(live_fee_cents, (int, float)) and live_fee_cents > 0:
                return round(live_fee_cents / 100.0, 2)

            # Check if Keepa recorded package weight in grams
            package_weight_grams = keepa_prod.get("packageWeight")
            if package_weight_grams and package_weight_grams > 0:
                fallback_weight_lbs = package_weight_grams / 453.592

        for tier in cls.FALLBACK_FBA_TIERS:
            if fallback_weight_lbs <= tier["max_weight_lbs"]:
                return tier["fee"]

        base_fee = cls.FALLBACK_FBA_TIERS[-1]["fee"]
        extra_weight = fallback_weight_lbs - cls.FALLBACK_FBA_TIERS[-1]["max_weight_lbs"]
        return round(base_fee + (extra_weight * cls.PER_LB_OVER_3_LBS), 2)

    @classmethod
    def get_referral_fee_rate(cls, buy_box_price: float, keepa_prod: Optional[Dict[str, Any]] = None, is_beauty: bool = True) -> float:
        if keepa_prod:
            api_referral_pct = keepa_prod.get("referralFeePercent")
            if api_referral_pct and isinstance(api_referral_pct, (int, float)) and api_referral_pct > 0:
                return api_referral_pct / 100.0

        # Amazon Beauty & Personal Care fee tier: 8% under $10, 15% standard
        if is_beauty and buy_box_price <= 10.0:
            return 0.08
        return 0.15

    @classmethod
    def calculate_economics(
        cls, 
        buy_box: float, 
        unit_cost: float, 
        keepa_prod: Optional[Dict[str, Any]] = None,
        weight_lbs: float = 1.0,
        inbound_freight_per_lb: float = 0.30
    ) -> Dict[str, float]:
        if buy_box <= 0:
            return {"net_profit": 0.0, "roi": 0.0, "margin": 0.0, "fba_fee": 0.0, "referral_fee": 0.0}

        fba_fee = cls.get_fba_fulfillment_fee(keepa_prod=keepa_prod, fallback_weight_lbs=weight_lbs)
        referral_rate = cls.get_referral_fee_rate(buy_box, keepa_prod=keepa_prod)
        referral_fee = round(buy_box * referral_rate, 2)
        inbound_shipping = round(weight_lbs * inbound_freight_per_lb, 2)

        total_deductions = referral_fee + fba_fee + inbound_shipping
        net_profit = round(buy_box - (unit_cost + total_deductions), 2)
        roi = round((net_profit / unit_cost) * 100, 1) if unit_cost > 0 else 0.0
        margin = round((net_profit / buy_box) * 100, 1)

        return {
            "net_profit": net_profit,
            "roi": roi,
            "margin": margin,
            "fba_fee": fba_fee,
            "referral_fee": referral_fee
        }


class ProfitFirstEngine:
    def __init__(self, keepa_client: KeepaClient):
        self.keepa = keepa_client
        self.auditor = CatalogAuditor()
        self.state_mgr = StateManager()

    def scan_catalog_for_profit(self, df: pd.DataFrame, min_roi: float = 35.0) -> pd.DataFrame:
        ungated_brands = self.state_mgr.get_ungated_brands()
        print(f"[*] Auditing catalog for optimal ROI (Target: >= {min_roi}% ROI)...")

        high_roi_prizes = []

        # 1. First Pass: Scan every catalog line item for pure ROI and safety
        for _, row in df.iterrows():
            upc = str(row['upc']).zfill(12)
            cost = float(row['unit_cost'])
            
            keepa_data = self.keepa.get_product_by_upc(upc)
            if not keepa_data:
                continue

            audit = self.auditor.audit_sku_safety(keepa_data, cost)
            if not audit:
                continue

            # Calculate precise economics using live/dynamic Amazon fee engine
            econ = DynamicFeeCalculator.calculate_economics(
                buy_box=audit['buy_box'],
                unit_cost=cost,
                keepa_prod=keepa_data,
                weight_lbs=1.0
            )
            
            # Filter strictly by profitability floors
            if econ['roi'] >= min_roi and econ['net_profit'] >= 3.00:
                high_roi_prizes.append({
                    "brand": str(row['brand']).strip(),
                    "upc": upc,
                    "description": str(row['description']),
                    "asin": audit['asin'],
                    "unit_cost": cost,
                    "case_qty": int(row.get('case_qty', 12)),
                    "buy_box": audit['buy_box'],
                    "roi": econ['roi'],
                    "net_profit": econ['net_profit'],
                    "fba_fee": econ['fba_fee'],
                    "referral_fee": econ['referral_fee'],
                    "monthly_velocity": audit['monthly_drops']
                })

        print(f"[+] Found {len(high_roi_prizes)} profitable target products across catalog.")

        prizes_df = pd.DataFrame(high_roi_prizes)
        if prizes_df.empty:
            return pd.DataFrame()

        # Sort all discovered prizes by highest ROI descending
        prizes_df = prizes_df.sort_values(by="roi", ascending=False)

        # 2. Second Pass: Construct PO prioritizing ROI, ungate ONLY as needed
        final_po_lines = []
        running_total = 0.0
        gated_brands_already_handled = set()

        for _, prize in prizes_df.iterrows():
            if running_total >= DISTRIBUTOR_MOQ:
                break

            brand_norm = prize['brand'].lower()

            if brand_norm in ungated_brands:
                # BRAND IS UNGATED -> Buy the prize directly for pure profit
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
                    "net_profit_ea": prize['net_profit'],
                    "note": "Brand already ungated; purchased for direct profit"
                })
                running_total += line_cost

            else:
                # BRAND IS GATED -> Check if we can ungate it from this same wholesaler
                if brand_norm not in gated_brands_already_handled:
                    brand_skus = df[df['brand'].str.lower() == brand_norm].sort_values(by="unit_cost", ascending=True)
                    
                    cheapest_key = None
                    for _, k_row in brand_skus.iterrows():
                        k_cost = float(k_row['unit_cost'])
                        k_units = int(k_row.get('case_qty', MIN_BRAND_UNITS))
                        if k_units < MIN_BRAND_UNITS:
                            k_units = MIN_BRAND_UNITS
                        
                        cheapest_key = {
                            "upc": str(k_row['upc']).zfill(12),
                            "description": str(k_row['description']),
                            "unit_cost": k_cost,
                            "order_qty": k_units,
                            "line_total": round(k_cost * k_units, 2)
                        }
                        break

                    if cheapest_key:
                        # 1. Add the cheap ungate sample key to the PO
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
                            "net_profit_ea": 0.0,
                            "note": f"Sample key ordered to unlock {prize['description']} ({prize['roi']}% ROI)"
                        })
                        running_total += cheapest_key['line_total']

                        # 2. Add the high-ROI prize item right alongside it
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
                            "net_profit_ea": prize['net_profit'],
                            "note": "Target product to list immediately once sample invoice clears"
                        })
                        running_total += line_cost
                        gated_brands_already_handled.add(brand_norm)

        return pd.DataFrame(final_po_lines)
