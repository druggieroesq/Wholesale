"""
tools/automated_deal_sniper.py
Automated Deal Sniper:
1. Scans Keepa and distributor queues for zero-supply inventory vacuums.
2. Filters deals through MasterVacuumEngine (Dynamic .env MIN_ROI_FLOOR).
3. Routes fulfillment and freight through LogisticsEngine.
4. Resolves direct distributor ordering URLs.
5. Dispatches formatted deal cards to Telegram and syncs state to Git.
"""

import os
import sys
import logging
from pathlib import Path
from typing import List, Dict, Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from dotenv import load_dotenv
load_dotenv(ROOT_DIR / ".env", override=True)

from tools.keepa_scanner import KeepaScanner
from tools.master_vacuum_engine import MasterVacuumEngine
from tools.sku_resolver import SKUResolver
from tools.telegram_action_handler import TelegramActionHandler
from tools.git_state_sync import GitStateSync
from tools.logistics_engine import LogisticsEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] Sniper: %(message)s")
logger = logging.getLogger("AutomatedDealSniper")


class AutomatedDealSniper:
    def __init__(self):
        self.scanner = KeepaScanner()
        # Automatically loads MIN_ROI_FLOOR and PARKLAND_PREP_FEE_UNIT from .env
        self.engine = MasterVacuumEngine()
        self.logistics = LogisticsEngine()
        self.resolver = SKUResolver()
        self.telegram = TelegramActionHandler()
        self.syncer = GitStateSync()

    def run_sniper_mission(self, max_alerts: int = 5) -> int:
        logger.info(f"⚡ [SNIPER] Initializing reconnaissance (ROI Floor: {self.engine.min_roi_floor}%)...")

        # 1. Harvest raw vacuum leads
        raw_candidates = self.scanner.run_full_scan()
        if not raw_candidates:
            logger.warning("[SNIPER] No raw candidate leads detected from scanner.")
            return 0

        # 2. Filter & evaluate margins through Master Vacuum Engine
        viable_deals = self.engine.process_catalog(raw_candidates)
        if not viable_deals:
            logger.info(f"[SNIPER] Zero items cleared the {self.engine.min_roi_floor}% ROI floor threshold.")
            return 0

        # 3. Enrich candidates with dynamic logistics and distributor order links
        dispatched_count = 0
        logger.info(f"[SNIPER] Verified {len(viable_deals)} high-margin targets. Dispatching top {max_alerts}...")

        for deal in viable_deals[:max_alerts]:
            vendor_name = deal.get("vendor", "midland_metal")
            mpn = deal.get("mpn", "")
            title = deal.get("title", "")
            asin = deal.get("asin", "")
            wholesale_cost = float(deal.get("wholesale_cost", 0.0))
            weight_lbs = float(deal.get("package_weight_lbs", 1.5))

            # Dynamic logistics routing
            route = self.logistics.route_purchase_order(
                vendor_key=vendor_name,
                unit_cost=wholesale_cost,
                weight_lbs=weight_lbs
            )
            deal["assigned_node_name"] = route["assigned_node_name"]
            deal["inbound_freight_unit"] = route["inbound_freight_unit"]
            deal["routing_reason"] = route["routing_reason"]

            # Resolve direct distributor ordering URL
            deal["vendor_url"] = self.resolver.get_order_url(
                vendor_name,
                mpn=mpn,
                title=title,
                asin=asin
            )

            # Fire Telegram Deal Card
            success = self.telegram.send_deal_card(deal)
            if success:
                dispatched_count += 1

        logger.info(f"⚡ [SNIPER] Successfully pushed {dispatched_count} live deal cards to Telegram.")

        # 4. Commit and persist state
        if dispatched_count > 0:
            self.syncer.sync_state(f"sniper: alerted {dispatched_count} zero-supply vacuum deals")

        return dispatched_count


if __name__ == "__main__":
    sniper = AutomatedDealSniper()
    max_deals = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.getenv("TELEGRAM_ALERT_LIMIT", 5))
    sniper.run_sniper_mission(max_alerts=max_deals)
