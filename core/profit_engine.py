import os
import math
import pandas as pd
from typing import List, Dict, Any

from core.keepa_client import KeepaClient
from core.safety_auditor import SafetyAuditor
from core.walmart_cost_engine import WalmartCostEngine
from core.walmart_radar import WalmartRadar
from core.inventory_planner import InventoryPlanner
from core.incentive_optimizer import IncentiveOptimizer
from core.quantity_resolver import PackageQuantityResolver
from core.amazon_stockout_radar import AmazonStockoutRadar
from core.opportunity_scorer import OpportunityScorer
from core.multipack_synthesizer import MultiPackSynthesizer
from core.buybox_leak_unsuppressor import BuyBoxLeakUnsuppressor
from core.catalog_delta_sniper import CatalogDeltaSniper
from core.geo_hub_resolver import GeoHubResolver
from core.fulfillment_router import FulfillmentRouter

class ProfitEngine:
    """
    Central Decision & Capital Allocation Engine.
    
    Orchestrates:
    - Zero-tolerance safety gating (IP complaints, meltable, Amazon 1P dominance).
    - Multi-pack quantity resolution.
    - Proximity routing for in-house hubs (Parkland, FL / West Bloomfield, MI) vs. commercial 3PLs.
    - Tiered prep cost modeling (1pk, 2pk, 3pk, 4pk bundling fees).
    - Payment term evaluation (+10 bonus points for 0% fee credit card float).
    - Dual-marketplace profit calculation (Amazon FBA vs. Walmart WFS).
    - Purchase Order (PO) synthesis and inventory order ceilings.
    """

    def __init__(self, keepa_client: KeepaClient = None):
        self.keepa = keepa_client or KeepaClient()
        self.safety_auditor = SafetyAuditor()
        self.walmart_costs = WalmartCostEngine()
        self.walmart_radar = WalmartRadar()
        self.inventory_planner = InventoryPlanner(target_days=35, max_days=45)
        self.incentive_optimizer = IncentiveOptimizer()
        self.quantity_resolver = PackageQuantityResolver()
        self.amz_stockout_radar = AmazonStockoutRadar()
        self.scorer = OpportunityScorer()
        self.multipack_synthesizer = MultiPackSynthesizer()
        self.suppression_radar = BuyBoxLeakUnsuppressor()
        self.delta_sniper = CatalogDeltaSniper()
        self.geo_resolver = GeoHubResolver()
        self.fulfillment_router = FulfillmentRouter()

    def scan_catalog_for_profit(
        self,
        catalog_df: pd.DataFrame,
        distributor_meta: Dict[str, Any] = None,
        force_self_pickup: bool = False,
        min_roi: float = 35.0,
        max_amz_buybox_share: float = 0.15,
        parity_cap_pct: float = 0.15
    ) -> pd.DataFrame:
        """
        Scans a normalized distributor catalog and generates an executable,
        prioritized Purchase Order DataFrame.
        """
        if catalog_df.empty or "upc" not in catalog_df.columns:
            print("[!] Catalog is empty or missing 'upc' column.")
            return pd.DataFrame()

        distributor_meta = distributor_meta or {}
        wh_city = distributor_meta.get("city", "")
        wh_state = distributor_meta.get("state", "")
        wh_zip = distributor_meta.get("zip", "")

        # -------------------------------------------------------------
        # 1. GEO-PROXIMITY & SELF-PICKUP RESOLUTION
        # -------------------------------------------------------------
        is_confirmed_pickup = force_self_pickup or distributor_meta.get("self_pickup", False)
        geo_eval = self.geo_resolver.evaluate_supplier_location(
            warehouse_city=wh_city,
            warehouse_state=wh_state,
            warehouse_zip=wh_zip,
            confirmed_pickup=is_confirmed_pickup
        )
        print(f"[Geo Hub] {geo_eval['note']}")

        # -------------------------------------------------------------
        # 2. PAYMENT TERMS & CREDIT CARD SURCHARGE RESOLUTION
        # -------------------------------------------------------------
        payment_terms = distributor_meta.get("payment_terms", {})
        payment_method = str(payment_terms.get("payment_method", "ACH")).upper()
        cc_surcharge_pct = float(payment_terms.get("cc_surcharge_pct", 0.0))
        early_pay_discount_pct = float(payment_terms.get("early_pay_discount_pct", 0.0))

        cost_multiplier = 1.0
        if payment_method == "CREDIT_CARD" and cc_surcharge_pct > 0:
            cost_multiplier += cc_surcharge_pct
            print(f"[Payment Engine] Factoring {cc_surcharge_pct * 100:.1f}% Credit Card Surcharge into unit costs.")
        elif (payment_method in ["TERMS_2_10_NET_30", "ACH"]) and early_pay_discount_pct > 0:
            cost_multiplier -= early_pay_discount_pct
            print(f"[Payment Engine] Factoring {early_pay_discount_pct * 100:.1f}% Early Pay Cash Discount into unit costs.")

        # -------------------------------------------------------------
        # 3. KEEPA PRODUCT INGESTION
        # -------------------------------------------------------------
        upc_list = [str(u).strip().zfill(12) for u in catalog_df["upc"].dropna().unique()]
        keepa_products = self.keepa.fetch_products_by_upc(upc_list)
        if not keepa_products:
            print("[Keepa] No product records returned from catalog query.")
            return pd.DataFrame()

        # Build fast-lookup UPC map
        product_map = {}
        for kp in keepa_products:
            for code in (kp.get("upcList", []) or []) + (kp.get("eanList", []) or []):
                product_map[str(code).strip().zfill(12)] = kp

        preliminary_po = []

        # -------------------------------------------------------------
        # 4. ITEM-BY-ITEM EVALUATION PIPELINE
        # -------------------------------------------------------------
        for _, row in catalog_df.iterrows():
            clean_upc = str(row.get("upc", "")).strip().zfill(12)
            kp = product_map.get(clean_upc)
            if not kp:
                continue

            # Step A: Safety Audit Gating
            is_safe, safety_reason = self.safety_auditor.audit_product(
                kp, 
                row.to_dict(), 
                max_amz_share=max_amz_buybox_share
            )
            if not is_safe:
                continue

            base_cost = float(row.get("cost", 0.0))
            if base_cost <= 0:
                continue

            # Step B: Amazon Buy Box & Suppression Pricing
            stats = kp.get("stats", {}) or {}
            curr = stats.get("current", [])
            suppress_info = self.suppression_radar.evaluate_suppression(kp)
            bb_cents = curr[18] if len(curr) > 18 else -1

            if bb_cents and bb_cents > 0:
                amz_retail = round(bb_cents / 100.0, 2)
            elif suppress_info.get("is_suppressed_opportunity", False):
                amz_retail = suppress_info["target_unsuppress_price"]
            else:
                continue

            # Step C: Multi-Pack Resolution & Real Wholesale Unit Cost
            pack_qty = self.quantity_resolver.resolve_pack_quantity(kp)
            raw_item_cost = round((base_cost * pack_qty) * cost_multiplier, 2)

            weight_grams = kp.get("packageWeight", 453) or 453
            weight_lbs = max(0.5, round(weight_grams / 453.592, 2))

            # Step D: Optimal Fulfillment Routing (Parkland vs. MI vs. Commercial 3PL)
            best_route = self.fulfillment_router.find_optimal_fulfillment(
                supplier_meta=distributor_meta,
                retail_price=amz_retail,
                raw_product_cost=raw_item_cost,
                unit_weight_lbs=weight_lbs,
                pack_qty=pack_qty,
                allow_self_pickup=is_confirmed_pickup
            )

            landed_cost = best_route["landed_cost"]
            amz_profit = best_route["net_profit"]
            amz_roi = best_route["roi"]

            # Step E: Walmart WFS Economics & Price Parity Cap
            wm_retail = round(min(amz_retail * (1.0 + parity_cap_pct), amz_retail * 1.05), 2)
            wm_costs = self.walmart_costs.calculate_fees(wm_retail, weight_lbs)
            wm_profit = round(wm_retail - wm_costs["total_cost"] - landed_cost, 2)
            wm_roi = round((wm_profit / landed_cost) * 100.0, 1) if landed_cost > 0 else 0.0

            # Step F: Supply Gap Detection
            amz_gap = self.amz_stockout_radar.evaluate_opportunity(kp).get("is_stockout_opportunity", False)
            wm_gap = self.walmart_radar.check_opportunity(clean_upc).get("is_walmart_needed", False)

            # Step G: Opportunity Scoring (Includes Payment Float Bonus)
            score, grade, breakdown = self.scorer.score_product(
                keepa_product=kp,
                amz_roi=amz_roi,
                wm_roi=wm_roi,
                is_amz_gap=amz_gap,
                is_wm_gap=wm_gap,
                min_roi=min_roi,
                payment_terms=payment_terms
            )

            # Strict ROI threshold filter (unless A++ dual marketplace vacuum)
            if grade != "A++" and (amz_roi < min_roi and wm_roi < min_roi):
                continue

            # Step H: Working Capital Order Ceilings (35-Day Rotation Target)
            fba_sellers = breakdown["fba_sellers"]
            case_pack = int(row.get("case_pack", 6))
            ceilings = self.inventory_planner.calculate_order_ceiling(kp, fba_sellers, case_pack)

            amz_units = (
                ceilings["amz_max_ceiling"] if grade in ["A++", "A+"] 
                else (ceilings["amz_recommended"] if amz_roi >= min_roi else 0)
            )
            wm_units = (
                ceilings["wm_max_ceiling"] if grade == "A++" 
                else (ceilings["wm_recommended"] if wm_roi >= min_roi else 0)
            )

            total_units = amz_units + wm_units
            if total_units == 0:
                continue

            total_spend = round(total_units * raw_item_cost, 2)

            preliminary_po.append({
                "Grade": grade,
                "Score": score,
                "Fulfillment_Route": best_route["facility_name"],
                "Fulfillment_Type": best_route.get("facility_type", "COMMERCIAL_3PL"),
                "Hub_Strategy": geo_eval["strategy"],
                "Financing_Perk": breakdown["payment_benefit"],
                "SKU": row.get("sku", clean_upc),
                "UPC": clean_upc,
                "Title": str(kp.get("title", "Unknown"))[:80],
                "Pack_Qty": pack_qty,
                "Raw_Unit_Cost": raw_item_cost,
                "Prep_Fee": best_route["prep_cost"],
                "Inbound_Freight": best_route["inbound_freight"],
                "Landed_Cost": landed_cost,
                "Amazon_Retail": amz_retail,
                "Amazon_ROI%": amz_roi,
                "Amazon_Units": amz_units,
                "Walmart_Retail": wm_retail,
                "Walmart_ROI%": wm_roi,
                "Walmart_Units": wm_units,
                "Total_Units": total_units,
                "Total_PO_Spend": total_spend
            })

        if not preliminary_po:
            print("[ProfitEngine] No products satisfied safety, ROI, and demand thresholds.")
            return pd.DataFrame()

        # -------------------------------------------------------------
        # 5. SORT & FINALIZE PURCHASE ORDER
        # -------------------------------------------------------------
        po_df = pd.DataFrame(preliminary_po)
        grade_order = {"A++": 0, "A+": 1, "A": 2, "B": 3, "C": 4}
        po_df["Rank"] = po_df["Grade"].map(grade_order)
        po_df.sort_values(by=["Rank", "Score"], ascending=[True, False], inplace=True)
        po_df.drop(columns=["Rank"], inplace=True)

        return po_df

# Alias to maintain backwards compatibility with legacy main.py imports
ProfitFirstEngine = ProfitEngine
