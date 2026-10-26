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
        #
