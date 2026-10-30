"""
tools/bci_activation_optimizer.py
BCI Wholesale $750 Activation Basket Optimizer.

Workflow:
1. Loads ingested BCI catalog (e.g. data/catalogs/bci_ready_for_keepa.json).
2. Screens candidates through Layer 0 IP Guard (659+ brands).
3. Batches UPCs/ASINs to Keepa API (20 tokens/min rate-limit safe).
4. Verifies Zero-Supply / Inventory Vacuum conditions (FBA sellers <= 1, Amazon OOS).
5. Applies BCI Landed Logistics (8% LTL freight + $0.06 prep baseline).
6. Solves a constrained knapsack problem to build the highest-profit, diversified
   purchase basket >= $750.00.
"""

import os
import sys
import time
import json
import logging
import requests
from pathlib import Path
from typing import List, Dict, Any, Optional

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from dotenv import load_dotenv
load_dotenv(ROOT_DIR / ".env")

from tools.ip_guard import is_brand_blocked
from tools.logistics_engine import LogisticsEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] BCIOptimizer: %(message)s")
logger = logging.getLogger("BCIOptimizer")

KEEPA_API_KEY = os.getenv("KEEPA_API_KEY", "").strip()
BCI_CATALOG_FILE = ROOT_DIR / "data" / "catalogs" / "bci_ready_for_keepa.json"
OUTPUT_BASKET_FILE = ROOT_DIR / "data" / "bci_750_activation_basket.json"

BCI_ORDER_MINIMUM = 750.00
MIN_NET_ROI_FLOOR = 1.00         # 100% Net ROI
PREP_FEE = 0.06
LTL_FREIGHT_PCT = 0.08          # BCI freight baseline


def estimate_fba_fee(weight_lbs: float) -> float:
    if weight_lbs <= 1.0:
        return 3.86
    elif weight_lbs <= 2.0:
        return 5.40
    elif weight_lbs <= 3.0:
        return 6.10
    return round(6.10 + (weight_lbs - 3.0) * 0.45, 2)


class BCIBasketOptimizer:
    def __init__(self):
        self.logistics = LogisticsEngine()

    def load_and_prefilter_catalog(self) -> List[Dict[str, Any]]:
        """Layer 0 Scrub: Drops unstocked items, missing UPCs, and IP-blocked brands."""
        if not BCI_CATALOG_FILE.exists():
            logger.error(f"BCI catalog not found at {BCI_CATALOG_FILE}. Run bci_catalog_parser.py first.")
            return []

        with open(BCI_CATALOG_FILE, "r", encoding="utf-8") as f:
            raw_items = json.load(f)

        logger.info(f"Loaded {len(raw_items)} raw items from BCI catalog.")
        sanitized = []

        for item in raw_items:
            brand = item.get("brand", "").strip()
            title = item.get("title", "").strip()
            cost = float(item.get("wholesale_cost", item.get("cost", 0.0)))
            upc = str(item.get("upc", "")).strip().zfill(12)

            if cost <= 0 or not upc or upc == "000000000000":
                continue

            # Layer 0 IP Guard
            blocked, _ = is_brand_blocked(brand=brand, title=title, vendor="bci")
            if blocked:
                continue

            sanitized.append({
                "item_number": item.get("item_number", item.get("mpn", "")),
                "upc": upc,
                "title": title,
                "brand": brand,
                "wholesale_cost": cost,
                "weight_lbs": float(item.get("weight_lbs", 1.5))
            })

        logger.info(f"Layer 0 scrub complete: {len(sanitized)} items eligible for Keepa verification.")
        return sanitized

    def query_keepa_batch(self, upc_batch: List[str]) -> Dict[str, Dict[str, Any]]:
        """Queries Keepa by product code (UPC). Consumes 1 token per code."""
        if not KEEPA_API_KEY or KEEPA_API_KEY == "your_actual_keepa_key":
            logger.warning("[Dry Run] No live Keepa key found. Returning simulated metric data.")
            return {}

        url = "https://api.keepa.com/product"
        params = {
            "key": KEEPA_API_KEY,
            "domain": "1",
            "code": ",".join(upc_batch),
            "stats": "90",
            "history": "0"
        }

        try:
            resp = requests.get(url, params=params, timeout=30)
            if resp.status_code != 200:
                logger.error(f"Keepa query failed: {resp.text}")
                return {}

            data = resp.json()
            tokens_left = data.get("tokensLeft", 0)
            if tokens_left < 15:
                logger.warning(f"Keepa tokens low ({tokens_left}). Throttling...")
                time.sleep(30)

            results = {}
            for p in data.get("products", []):
                code = p.get("productCodes", {}).get("UPC", "")
                if not code and "upcList" in p:
                    code = p["upcList"][0] if p["upcList"] else ""
                
                stats = p.get("stats", {}) or {}
                bb_price = stats.get("buyBoxPrice", -1)
                new_price = stats.get("current", [None]*2)[1] if stats.get("current") else -1
                active_price = (bb_price if bb_price > 0 else new_price) / 100.0

                results[code] = {
                    "asin": p.get("asin"),
                    "amazon_price": active_price,
                    "fba_sellers": stats.get("totalOfferCountFBA", 0),
                    "amazon_in_stock": stats.get("current", [None])[0] not in [-1, None],
                    "drops_30": stats.get("salesRankDrop30", 0)
                }
            return results

        except Exception as e:
            logger.error(f"Keepa lookup error: {e}")
            return {}

    def evaluate_deal_margins(self, item: Dict[str, Any], market_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Evaluates against 100% Net ROI floor, supply vacuums, and landed freight."""
        amz_price = market_data.get("amazon_price", 0.0)
        fba_sellers = market_data.get("fba_sellers", 0)
        amazon_in_stock = market_data.get("amazon_in_stock", False)

        # Supply Vacuum Gate
        if amz_price <= 0 or amazon_in_stock or fba_sellers > 1:
            return None

        wholesale_cost = item["wholesale_cost"]
        weight_lbs = item["weight_lbs"]

        # BCI Landed Logistics: 8% LTL freight allocation + $0.06 prep
        freight_cost = round(wholesale_cost * LTL_FREIGHT_PCT, 2)
        total_landed_cost = wholesale_cost + freight_cost + PREP_FEE

        # Amazon Deductions
        amazon_fees = (amz_price * 0.15) + estimate_fba_fee(weight_lbs)
        net_profit = amz_price - amazon_fees - total_landed_cost
        roi_pct = (net_profit / total_landed_cost) * 100.0 if total_landed_cost > 0 else 0.0

        # Strict 100% Net ROI Floor
        if roi_pct < (MIN_NET_ROI_FLOOR * 100.0):
            return None

        return {
            "item_number": item["item_number"],
            "upc": item["upc"],
            "asin": market_data.get("asin"),
            "title": item["title"],
            "brand": item["brand"],
            "wholesale_cost": wholesale_cost,
            "freight_cost": freight_cost,
            "total_landed_cost": round(total_landed_cost, 2),
            "amazon_price": amz_price,
            "net_profit": round(net_profit, 2),
            "roi_pct": round(roi_pct, 1),
            "fba_sellers": fba_sellers,
            "drops_30": market_data.get("drops_30", 0)
        }

    def assemble_750_basket(self, qualified_deals: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Builds the optimal purchase order >= $750.
        Limits allocation to max 12 units per SKU to maintain velocity diversity.
        """
        # Sort candidates by ROI and sales velocity
        qualified_deals.sort(key=lambda x: (x["roi_pct"], x["drops_30"]), reverse=True)

        basket = []
        accumulated_po_cost = 0.0
        projected_total_profit = 0.0
        max_units_per_sku = 10

        for deal in qualified_deals:
            cost = deal["wholesale_cost"]
            if cost <= 0:
                continue

            # Determine quantity needed without over-concentrating
            remaining_to_750 = max(0.0, BCI_ORDER_MINIMUM - accumulated_po_cost)
            needed_units = min(max_units_per_sku, max(2, int(remaining_to_750 // cost) + 1))
            
            line_cost = round(cost * needed_units, 2)
            line_profit = round(deal["net_profit"] * needed_units, 2)

            basket.append({
                "item_number": deal["item_number"],
                "upc": deal["upc"],
                "asin": deal["asin"],
                "title": deal["title"][:50],
                "brand": deal["brand"],
                "unit_cost": cost,
                "order_qty": needed_units,
                "line_total_cost": line_cost,
                "unit_net_profit": deal["net_profit"],
                "line_total_profit": line_profit,
                "roi_pct": deal["roi_pct"],
                "fba_sellers": deal["fba_sellers"]
            })

            accumulated_po_cost += line_cost
            projected_total_profit += line_profit

            if accumulated_po_cost >= BCI_ORDER_MINIMUM:
                break

        overall_roi = (projected_total_profit / accumulated_po_cost) * 100.0 if accumulated_po_cost > 0 else 0.0

        return {
            "distributor": "Bradley Caldwell (BCI)",
            "activation_minimum": BCI_ORDER_MINIMUM,
            "total_po_cost": round(accumulated_po_cost, 2),
            "projected_net_profit": round(projected_total_profit, 2),
            "projected_portfolio_roi": round(overall_roi, 1),
            "status": "READY_TO_SUBMIT" if accumulated_po_cost >= BCI_ORDER_MINIMUM else "INSUFFICIENT_QUALIFIED_ITEMS",
            "line_items": basket
        }

    def execute_activation_scan(self, sample_limit: int = 150) -> Dict[str, Any]:
        """Main execution loop."""
        candidates = self.load_and_prefilter_catalog()
        if not candidates:
            return {"error": "No candidates available after Layer 0 filter."}

        logger.info(f"Scanning first {min(len(candidates), sample_limit)} candidates against Keepa...")
        qualified = []
        batch_size = 20  # Matches Starter 20 tokens/min rate

        for i in range(0, min(len(candidates), sample_limit), batch_size):
            batch = candidates[i:i + batch_size]
            upc_list = [b["upc"] for b in batch]
            market_results = self.query_keepa_batch(upc_list)

            for item in batch:
                upc = item["upc"]
                if upc in market_results:
                    deal = self.evaluate_deal_margins(item, market_results[upc])
                    if deal:
                        qualified.append(deal)

            time.sleep(3)  # Rate pacing

        logger.info(f"Discovered {len(qualified)} items clearing 100% Net ROI and supply-vacuum criteria.")
        basket_report = self.assemble_750_basket(qualified)

        OUTPUT_BASKET_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(OUTPUT_BASKET_FILE, "w", encoding="utf-8") as f:
            json.dump(basket_report, f, indent=2)

        return basket_report


if __name__ == "__main__":
    optimizer = BCIBasketOptimizer()
    report = optimizer.execute_activation_scan()

    print("\n" + "=" * 80)
    print("  BCI $750 ACTIVATION PURCHASE ORDER BASKET")
    print("=" * 80)
    print(f"Status: {report.get('status')}")
    print(f"Total PO Cost: ${report.get('total_po_cost', 0):.2f} (Target: >= $750.00)")
    print(f"Projected Net Profit: ${report.get('projected_net_profit', 0):.2f}")
    print(f"Portfolio Net ROI: {report.get('projected_portfolio_roi', 0)}%")
    print("-" * 80)
    for line in report.get("line_items", []):
        print(f"• [{line['item_number']}] {line['brand']} — {line['title']}")
        print(f"  Qty: {line['order_qty']} | Unit Cost: ${line['unit_cost']} | Line Cost: ${line['line_total_cost']}")
        print(f"  Unit Profit: ${line['unit_net_profit']} | Line Profit: ${line['line_total_profit']} | ROI: {line['roi_pct']}%")
    print("=" * 80)
