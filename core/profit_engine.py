import os
import pandas as pd
from typing import Dict, List, Any, Optional
from core.keepa_client import KeepaClient
from core.auditor import CatalogAuditor
from core.state_manager import StateManager
from core.amazon_gatekeeper import AmazonGatekeeper
from config import MIN_BRAND_UNITS, DISTRIBUTOR_MOQ

class DynamicFeeCalculator:
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
    def calculate_economics(cls, buy_box: float, unit_cost: float, keepa_prod: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
        if buy_box <= 0:
            return {"net_profit": 0.0, "roi": 0.0, "margin": 0.0, "fba_fee": 0.0, "referral_fee": 0.0}

        fba_fee = cls.get_fba_fulfillment_fee(keepa_prod=keepa_prod)
        referral_fee = round(buy_box * cls.get_referral_fee_rate(buy_box, keepa_prod), 2)
        inbound = 0.30  # estimated inbound freight

        net_profit = round(buy_box - (unit_cost + referral_fee + fba_fee + inbound), 2)
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
        self.gatekeeper = AmazonGatekeeper()

    def scan_catalog_for_profit(self, df: pd.DataFrame, min_roi: float = 35.0, moq: float = DISTRIBUTOR_MOQ) -> pd.DataFrame:
        print(f"[*] Scanning catalog for high-ROI prizes (Min ROI: {min_roi}% | Target PO: ${moq:,.2f})...")

        # 1. Identify all high-ROI products passing safety audit
        high_roi_prizes = []
        for _, row in df.iterrows():
            upc = str(row['upc']).zfill(12)
            cost = float(row['unit_cost'])

            keepa_data = self.keepa.get_product_by_upc(upc)
            if not keepa_data:
                continue

            audit = self.auditor.audit_sku_safety(keepa_data, cost)
            if not audit:
                continue

            econ = DynamicFeeCalculator.calculate_economics(audit['buy_box'], cost, keepa_data)
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
                    "drops": audit['monthly_drops']
                })

        prizes_df = pd.DataFrame(high_roi_prizes)
        if prizes_df.empty:
            print("[!] No products met safety and ROI criteria.")
            return pd.DataFrame()

        # Sort prizes by highest ROI descending
        prizes_df = prizes_df.sort_values(by="roi", ascending=False)
        print(f"[+] Found {len(prizes_df)} high-ROI products. Verifying gating & building PO...")

        final_po_lines = []
        running_total = 0.0
        handled_gated_brands = set()

        ungated_brands = self.state_mgr.get_ungated_brands()
        pending_brands = self.state_mgr.get_pending_brands()

        # 2. Iterate through prizes and evaluate live Seller Central restrictions
        for _, prize in prizes_df.iterrows():
            if running_total >= moq:
                break

            brand_norm = prize['brand'].lower()

            # A. Check local state first
            is_approved = brand_norm in ungated_brands

            # B. If not verified in state, query Amazon SP-API live
            if not is_approved and brand_norm not in pending_brands:
                eval_result = self.gatekeeper.evaluate_asin_approval(prize['asin'])
                status = eval_result.get("status")

                if status in ["UNRESTRICTED", "AUTO_UNGATED"]:
                    print(f" [✓ AUTO-UNGATED] Amazon approved {prize['brand']} ({prize['asin']}) with 0 invoice required!")
                    self.state_mgr.verify_ungated_on_amazon(prize['brand'])
                    is_approved = True
                else:
                    print(f" [! HARD-GATED] Amazon requires wholesale invoice for {prize['brand']}.")

            # 3. Branching Logic:
            if is_approved:
                # BRAND IS UNGATED -> Buy full case quantity for pure profit
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
                    "action": "Ship direct to Amazon FBA for profit"
                })
                running_total += line_total

            else:
                # BRAND IS GATED -> Find the cheapest key to ungate
                if brand_norm not in handled_gated_brands and brand_norm not in pending_brands:
                    # Scan entire distributor catalog for the cheapest item from this brand
                    brand_catalog = df[df['brand'].str.lower() == brand_norm].sort_values(by="unit_cost", ascending=True)

                    cheapest_key = None
                    for _, k_row in brand_catalog.iterrows():
                        k_cost = float(k_row['unit_cost'])
                        # Amazon requires minimum 10 units on the wholesale invoice
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
                        print(f"  └─► Found lowest-cost ungate key: {cheapest_key['description']} @ ${cheapest_key['unit_cost']:.2f}/ea")
                        final_po_lines.append({
                            "type": "UNGATE_SAMPLE_KEY",
                            "brand": prize['brand'],
                            "upc": cheapest_key['upc'],
                            "description": cheapest_key['description'],
                            "asin": "PENDING_INVOICE",
                            "order_qty": cheapest_key['order_qty'],
                            "unit_cost": cheapest_key['unit_cost'],
                            "line_total": cheapest_key['line_total'],
                            "roi_%": 0.0,
                            "net_profit_ea": 0.0,
                            "action": f"Order 10 units to unlock {prize['brand']} (Targets {prize['description']} @ {prize['roi']}% ROI)"
                        })
                        running_total += cheapest_key['line_total']
                        self.state_mgr.mark_sample_ordered(prize['brand'])
                        handled_gated_brands.add(brand_norm)

        # 4. Fill remainder of order up to MOQ with approved inventory
        if running_total < moq:
            print(f"[*] Cart at ${running_total:,.2f} / ${moq:,.2f}. Adding verified ungated stock to meet distributor MOQ...")
            for _, prize in prizes_df.iterrows():
                if running_total >= moq:
                    break
                brand_norm = prize['brand'].lower()
                if brand_norm in self.state_mgr.get_ungated_brands():
                    # Check if already added
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
                        "action": "Added to satisfy distributor MOQ with high-ROI volume"
                    })
                    running_total += line_total

        return pd.DataFrame(final_po_lines)
