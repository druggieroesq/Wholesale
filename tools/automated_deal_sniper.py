import os
import sys
import logging
from typing import List, Dict, Any

from tools.keepa_scanner import KeepaScanner
from tools.master_vacuum_engine import MasterVacuumEngine
from tools.sku_resolver import SKUResolver
from tools.telegram_action_handler import TelegramActionHandler
from tools.git_state_sync import GitStateSync

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("AutomatedDealSniper")

class AutomatedDealSniper:
    """
    Automated Deal Sniper:
    1. Scans Keepa commercial departments for 0-seller vacuums.
    2. Filters deals through MasterVacuumEngine (50% ROI floor, Parkland $0.35 prep).
    3. Resolves vendor links (Global Industrial, Midland Metal, etc.).
    4. Dispatches actionable deal cards directly to Telegram.
    5. Syncs state to GitHub.
    """
    def __init__(self):
        self.scanner = KeepaScanner()
        self.engine = MasterVacuumEngine(prep_override=0.35)
        self.resolver = SKUResolver()
        self.telegram = TelegramActionHandler()
        self.syncer = GitStateSync()

    def run_sniper_mission(self, max_alerts: int = 5) -> int:
        logger.info("⚡ [SNIPER] Initializing 0-seller vacuum reconnaissance...")

        # 1. Harvest raw leads
        raw_candidates = self.scanner.run_full_scan()
        if not raw_candidates:
            logger.warning("[SNIPER] No raw leads detected.")
            return 0

        # 2. Filter & evaluate margins through Master Vacuum Engine
        viable_deals = self.engine.process_catalog(raw_candidates)
        if not viable_deals:
            logger.info("[SNIPER] Zero items met the 50% ROI floor threshold.")
            return 0

        # 3. Enrich top candidates with direct distributor URLs
        dispatched_count = 0
        logger.info(f"[SNIPER] Found {len(viable_deals)} high-margin targets. Dispatching top {max_alerts}...")

        for deal in viable_deals[:max_alerts]:
            vendor_name = deal.get("vendor", "global_industrial")
            mpn = deal.get("mpn", "")
            title = deal.get("title", "")
            asin = deal.get("asin", "")

            # Resolve direct distributor order link
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
        
        # 4. Commit and persist
        self.syncer.sync_state(f"sniper: alerted {dispatched_count} deals")
        return dispatched_count


if __name__ == "__main__":
    sniper = AutomatedDealSniper()
    max_deals = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    sniper.run_sniper_mission(max_alerts=max_deals)
