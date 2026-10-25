import os
import pandas as pd
from typing import Dict, List, Any, Optional
from core.keepa_client import KeepaClient
from core.auditor import CatalogAuditor
from core.state_manager import StateManager
from config import MIN_BRAND_UNITS, DISTRIBUTOR_MOQ

class DynamicFeeCalculator:
    """
    Calculates Amazon FBA referral and fulfillment fees dynamically.
    Prioritizes real-time fee fields from Keepa; falls back to a 
    configurable tier schedule if API values are absent.
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
            live_fee_cents = keepa_prod.get("fbaFees")
            if live_fee_cents and isinstance(live_fee_cents, (int, float)) and live_fee_cents > 0:
                return round(live_fee_cents / 100.0, 2)

            package_weight_grams = keepa_prod.get("packageWeight")
            if package_weight_grams and package_weight_grams > 0:
                fallback_weight_lbs = package_weight_grams / 453.592

        for tier in cls.FALLBACK_FBA_TIERS:
            if fallback_weight_lbs <= tier["max_weight_lbs"]:
                return tier["fee"]

        base_fee = cls.FALLBACK_FBA_TIERS[-1]["fee"]
        extra = fallback_weight_lbs - cls.FALLBACK_FBA_TIERS[-1]["max_weight_lbs"]
        return round(base_fee + (extra * cls.PER_LB_OVER_3_LBS), 2)

    @classmethod
    def get_referral_fee_rate(cls, buy_box: float, keepa_prod: Optional[Dict[str, Any]] = None) -> float:
        if keepa_prod:
            api_pct = keepa_prod.get("referralFeePercent")
            if api_pct and isinstance(api_pct, (int, float)) and api_pct > 0:
                return api_pct / 100.0
        return 0.08 if buy_box <= 10.0 else 0.15

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
        referral_fee = round(buy_box * cls.get_referral_fee_rate(buy_box, keepa_prod), 2)
        inbound = round(weight_lbs * inbound_freight_per_lb, 2)

        total_deductions = referral_fee + fba_fee + inbound
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

    def scan_catalog_for_profit(
        self, 
        df: pd.DataFrame, 
        min_roi: float = 35.0, 
        moq: float = DISTRIBUTOR_MOQ
    ) -> pd.DataFrame:
        print(f"[*] Scanning catalog for high-ROI opportunities (Target PO: ${moq:,.2f} | Min ROI: {min_roi}%)...")

        # 1. Extract and batch all valid UPCs for Keepa lookup
        all_upcs = [str(u).zfill(12) for u in df['upc'].dropna().unique()]
        keepa_dataset = self.keepa.get_products_batch(all_upcs)

        high_roi_prizes = []

        # 2. First Pass: Filter for pure margin, velocity, and brand safety
        for _, row in df.iterrows():
            upc = str(row['upc']).zfill(12)
            cost = float(row['unit_cost'])

            keepa_data = keepa_dataset.get(upc)
            if not keepa_data:
                continue

            audit = self.auditor.audit_sku_safety(keepa_data, cost)
            if not audit:
                continue

            econ = DynamicFeeCalculator.calculate_economics(
                buy_box=audit['buy_box'],
                unit_cost=cost,
                keepa_prod=keepa_data,
                weight_lbs=1.0
            )

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
                    "monthly_drops": audit['monthly_drops']
                })

        prizes_df = pd.DataFrame(high_roi_prizes)
        if prizes_df.empty:
            print("[!] No catalog items met ROI, velocity, and IP safety thresholds.")
            return pd.DataFrame()

        # Sort prizes by highest ROI descending
        prizes_df = prizes_df.sort_values(by="roi", ascending=False)
        print(f"[+] Found {len(prizes_df)} high-ROI products. Building PO with ungate keys...")

        final_po_lines = []
        running_total = 0.0
        gated_brands_handled = set()

        ungated_brands = self.state_mgr.get_ungated_brands()
        pending_brands = self.state_mgr.get_pending_brands()

        # 3. Second Pass: Build PO prioritizing verified ungated brands & attaching ungate keys
        for _, prize in prizes_df.iterrows():
            if running_total >= moq:
                break

            brand_norm = prize['brand'].lower()
            seller_central_link = f"https://sellercentral.amazon.com/productsearch?q={prize['asin']}"

            if brand_norm in ungated_brands:
                # BRAND IS UNGATED ON SELLER CENTRAL: Buy full case packs for profit
                order_qty = prize['case_qty']
                line_total = round(prize['unit_cost'] * order_qty, 2)

                final_po_lines.append({
                    "type": "PROFIT_INVENTORY",
                    "brand": prize['brand'],
                    "upc": prize['upc'],
                    "description": prize['description'],
                    "asin": prize['asin'],
                    "order_qty": order_qty,
                    "unit_cost": prize['unit_cost'],
                    "line_total": line_total,
                    "roi_%": prize['roi'],
                    "net_profit_ea": prize['net_profit'],
                    "action": "Ship directly to Amazon FBA for profit",
                    "seller_central_link": seller_central_link
                })
                running_total += line_total

            else:
                # BRAND IS GATED / PENDING: Isolate the cheapest sample key to unlock it
                if brand_norm not in gated_brands_handled and brand_norm not in pending_brands:
                    brand_skus = df[df['brand'].str.lower() == brand_norm].sort_values(by="unit_cost", ascending=True)

                    cheapest_key = None
                    for _, k_row in brand_skus.iterrows():
                        k_cost = float(k_row['unit_cost'])
                        # Amazon requires minimum 10 units on the wholesale invoice to ungate
                        k_qty = max(10, int(k_row.get('case_qty', 10)))

                        cheapest_key = {
                            "upc": str(k_row['upc']).zfill(12),
                            "description": str(k_row['description']),
                            "unit_cost": k_cost,
                            "order_qty": k_qty,
                            "line_total": round(k_cost * k_qty, 2)
                        }
                        break

                    if cheapest_key:
                        final_po_lines.append({
                            "type": "UNGATE_SAMPLE_KEY",
                            "brand": prize['brand'],
                            "upc": cheapest_key['upc'],
                            "description": cheapest_key['description'],
                            "asin": prize['asin'],
                            "order_qty": cheapest_key['order_qty'],
                            "unit_cost": cheapest_key['unit_cost'],
                            "line_total": cheapest_key['line_total'],
                            "roi_%": 0.0,
                            "net_profit_ea": 0.0,
                            "action": f"Order 10 units to ungate {prize['brand']} (Targets {prize['description']} @ {prize['roi']}% ROI)",
                            "seller_central_link": seller_central_link
                        })
                        running_total += cheapest_key['line_total']
                        gated_brands_handled.add(brand_norm)

        # 4. Third Pass: Top off cart to satisfy distributor MOQ using verified ungated winners
        if running_total < moq:
            print(f"[*] PO balance at ${running_total:,.2f} / ${moq:,.2f}. Adding verified ungated stock to meet distributor MOQ...")
            for _, prize in prizes_df.iterrows():
                if running_total >= moq:
                    break

                brand_norm = prize['brand'].lower()
                if brand_norm in ungated_brands:
                    if any(p['upc'] == prize['upc'] for p in final_po_lines):
                        continue

                    order_qty = prize['case_qty']
                    line_total = round(prize['unit_cost'] * order_qty, 2)
                    final_po_lines.append({
                        "type": "PROFIT_INVENTORY",
                        "brand": prize['brand'],
                        "upc": prize['upc'],
                        "description": prize['description'],
                        "asin": prize['asin'],
                        "order_qty": order_qty,
                        "unit_cost": prize['unit_cost'],
                        "line_total": line_total,
                        "roi_%": prize['roi'],
                        "net_profit_ea": prize['net_profit'],
                        "action": "Added to meet distributor MOQ with high-ROI volume",
                        "seller_central_link": f"https://sellercentral.amazon.com/productsearch?q={prize['asin']}"
                    })
                    running_total += line_total

        return pd.DataFrame(final_po_lines)
