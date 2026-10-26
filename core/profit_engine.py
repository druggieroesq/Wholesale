import os
import math
import pandas as pd
from typing import List, Dict, Any
from core.keepa_client import KeepaClient
from core.auditor import SafetyAuditor
from core.state_manager import StateManager
from core.walmart_radar import WalmartRadar

class ProfitFirstEngine:
    """
    Multichannel ROI Engine with True-Demand Enforcement.
    Only allocates units to Walmart when hard proof of recent velocity exists.
    """
    def __init__(self, keepa_client: KeepaClient):
        self.keepa = keepa_client
        self.auditor = SafetyAuditor()
        self.state_mgr = StateManager()

    def calculate_fba_fees(self, sell_price: float, weight_lbs: float = 1.0) -> float:
        referral_fee = sell_price * 0.15
        if weight_lbs <= 0.5:
            fulfillment = 3.22
        elif weight_lbs <= 1.0:
            fulfillment = 3.77
        elif weight_lbs <= 2.0:
            fulfillment = 4.75
        else:
            fulfillment = 5.40 + (weight_lbs - 2.0) * 0.40
        return round(referral_fee + fulfillment, 2)

    def compute_strict_allocation(
        self, 
        monthly_drops: int, 
        fba_sellers: int, 
        case_qty: int, 
        strategy: str
    ) -> Dict[str, int]:
        case_qty = max(1, case_qty)
        seller_pool = max(1, fba_sellers + 1)
        amz_est_demand = max(10, int(monthly_drops / seller_pool))

        if strategy == "WALMART_HIGH_DEMAND_OOS":
            # High proven demand + Walmart stockout: Split 60% Amz / 40% Walmart
            total_target = int(amz_est_demand * 1.5)
            walmart_target = int(total_target * 0.40)
            amz_target = total_target - walmart_target
        elif strategy == "DUAL_CHANNEL_SPLIT":
            # Both channels healthy: 75% Amz / 25% Walmart
            total_target = int(amz_est_demand * 1.25)
            walmart_target = max(case_qty, int(total_target * 0.25))
            amz_target = total_target - walmart_target
        else:
            # Amazon only: Do NOT risk sending inventory to Walmart
            total_target = amz_est_demand
            amz_target = total_target
            walmart_target = 0

        # Snap to whole cases
        amz_cases = max(1, round(amz_target / case_qty))
        walmart_cases = 0 if walmart_target == 0 else max(1, round(walmart_target / case_qty))

        amz_units = amz_cases * case_qty
        walmart_units = walmart_cases * case_qty
        total_units = amz_units + walmart_units

        return {
            "total_buy_qty": total_units,
            "send_to_amazon": amz_units,
            "send_to_walmart": walmart_units
        }

    def scan_catalog_for_profit(
        self, 
        normalized_df: pd.DataFrame, 
        distributor_name: str = "Distributor",
        min_roi: float = 35.0, 
        moq: float = 3000.00
    ) -> pd.DataFrame:
        print(f"[*] Sifting {distributor_name} catalog for verified high-velocity winners...")

        upc_list = normalized_df['upc'].dropna().unique().tolist()
        keepa_results = self.keepa.fetch_products_by_upc(upc_list)
        
        keepa_map: Dict[str, Dict[str, Any]] = {}
        for prod in keepa_results:
            prod_upcs = prod.get("upcList", [])
            for u in prod_upcs:
                keepa_map[str(u).zfill(12)] = prod

        prizes: List[Dict[str, Any]] = []
        brand_sacrifices: Dict[str, Dict[str, Any]] = {}

        for _, row in normalized_df.iterrows():
            upc = str(row['upc']).zfill(12)
            cost = float(row['unit_cost'])
            desc = row.get('description', '')
            brand = row.get('brand', 'Unknown')
            case_qty = int(row.get('case_qty', 12))

            keepa_prod = keepa_map.get(upc)
            if not keepa_prod:
                continue

            asin = keepa_prod.get("asin", "")
            stats = keepa_prod.get("stats", {})
            buy_box_cents = stats.get("buyBoxPrice", -1)
            buy_box = round(buy_box_cents / 100.0, 2) if buy_box_cents > 0 else 0.0

            if brand not in brand_sacrifices or cost < brand_sacrifices[brand]['unit_cost']:
                brand_sacrifices[brand] = {
                    "upc": upc, "asin": asin, "description": desc,
                    "brand": brand, "unit_cost": cost, "case_qty": case_qty
                }

            # Safety Audit (IP drops, Amazon 1P dominance, review anomalies)
            is_safe, reason = self.auditor.audit_product(keepa_prod)
            if not is_safe or buy_box <= 0:
                continue

            # Monthly sales drops (velocity)
            monthly_drops = self.auditor.get_monthly_sales_drops(keepa_prod)

            # Amazon Margins
            fba_fees = self.calculate_fba_fees(buy_box)
            inbound_freight = 0.35
            net_amz_profit = round(buy_box - cost - fba_fees - inbound_freight, 2)
            amz_roi = round((net_amz_profit / cost) * 100.0, 1)

            # Walmart Demand & OOS Analysis
            walmart_intel = WalmartRadar.analyze_walmart_opportunity(
                keepa_prod=keepa_prod, 
                wholesale_cost=cost, 
                monthly_drops=monthly_drops
            )

            # Only accept if it clears the minimum ROI threshold
            if amz_roi >= min_roi or walmart_intel['is_walmart_winner']:
                strategy = walmart_intel['channel_strategy']
                fba_sellers = stats.get("current_fba_sellers", 2)
                
                allocation = self.compute_strict_allocation(
                    monthly_drops=monthly_drops,
                    fba_sellers=fba_sellers,
                    case_qty=case_qty,
                    strategy=strategy
                )

                prizes.append({
                    "upc": upc,
                    "asin": asin,
                    "brand": brand,
                    "description": desc,
                    "unit_cost": cost,
                    "case_qty": case_qty,
                    "monthly_drops": monthly_drops,
                    "amz_buy_box": buy_box,
                    "amz_net_profit": net_amz_profit,
                    "amz_roi": amz_roi,
                    "walmart_intel": walmart_intel,
                    "allocation": allocation
                })

        if not prizes:
            print("[!] No catalog items met strict velocity, safety, and ROI thresholds.")
            return pd.DataFrame()

        # Sort by verified sales velocity first, then ROI
        prizes = sorted(prizes, key=lambda x: (x['monthly_drops'], x['amz_roi']), reverse=True)

        final_po_lines = []
        current_cart_total = 0.0
        gated_brands_in_po = set()

        for item in prizes:
            brand = item['brand']
            asin = item['asin']
            cost = item['unit_cost']
            w_info = item['walmart_intel']
            alloc = item['allocation']

            is_ungated = self.state_mgr.is_brand_ungated(brand)
            amz_link = f"https://sellercentral.amazon.com/productsearch?q={asin}"
            walmart_link = f"https://seller.walmart.com/items-and-inventory/add-items?search={item['upc']}"

            total_buy = alloc['total_buy_qty']
            amz_send = alloc['send_to_amazon']
            wmt_send = alloc['send_to_walmart']
            line_total = round(total_buy * cost, 2)

            strat = w_info['channel_strategy']
            if strat == "WALMART_HIGH_DEMAND_OOS":
                summary_tag = "🔥 HIGH DEMAND WMT OOS"
            elif strat == "DUAL_CHANNEL_SPLIT":
                summary_tag = "⚖️ DUAL-CHANNEL WINNER"
            else:
                summary_tag = "📦 AMZ FBA WINNER"

            if not is_ungated:
                gated_brands_in_po.add(brand)
                directive = (
                    f"⚠️ GATED ON AMZ | {summary_tag} | BUY {total_buy} from {distributor_name} -> "
                    f"SEND {amz_send} to Amazon (after ungate), SEND {wmt_send} to Walmart"
                )
            else:
                directive = (
                    f"🎯 {summary_tag} | BUY {total_buy} from {distributor_name} -> "
                    f"SEND {amz_send} to Amazon, SEND {wmt_send} to Walmart"
                )

            final_po_lines.append({
                "brand": brand,
                "description": item['description'],
                "unit_cost": cost,
                "total_order_qty": total_buy,
                "line_total": line_total,
                "send_to_amazon": amz_send,
                "send_to_walmart": wmt_send,
                "monthly_velocity_drops": item['monthly_drops'],
                "amz_roi_%": item['amz_roi'],
                "walmart_roi_%": w_info['walmart_roi'],
                "walmart_oos": "YES" if w_info['walmart_oos'] else "NO",
                "direct_instruction": directive,
                "upc": item['upc'],
                "asin": asin,
                "amazon_ungate_link": amz_link,
                "walmart_link": walmart_link
            })
            current_cart_total += line_total

        # Ungate keys insertion
        for brand in gated_brands_in_po:
            if brand in brand_sacrifices:
                sac = brand_sacrifices[brand]
                if not any(l['upc'] == sac['upc'] for l in final_po_lines):
                    sac_qty = max(10, sac['case_qty'])
                    sac_total = round(sac_qty * sac['unit_cost'], 2)
                    final_po_lines.insert(0, {
                        "brand": brand,
                        "description": f"[UNGATE KEY] {sac['description']}",
                        "unit_cost": sac['unit_cost'],
                        "total_order_qty": sac_qty,
                        "line_total": sac_total,
                        "send_to_amazon": sac_qty,
                        "send_to_walmart": 0,
                        "monthly_velocity_drops": 0,
                        "amz_roi_%": 0.0,
                        "walmart_roi_%": 0.0,
                        "walmart_oos": "N/A",
                        "direct_instruction": f"🔑 UNGATE KEY: BUY {sac_qty} from {distributor_name} -> Submit invoice to Amazon",
                        "upc": sac['upc'],
                        "asin": sac['asin'],
                        "amazon_ungate_link": f"https://sellercentral.amazon.com/productsearch?q={sac['asin']}",
                        "walmart_link": f"https://seller.walmart.com/items-and-inventory/add-items?search={sac['upc']}"
                    })
                    current_cart_total += sac_total

        # Top off cart to hit MOQ using highest velocity item
        if current_cart_total < moq and final_po_lines:
            deficit = moq - current_cart_total
            profit_items = [l for l in final_po_lines if "UNGATE KEY" not in l["description"]]
            if profit_items:
                top_item = profit_items[0]
                unit_cost = top_item["unit_cost"]
                extra_units = math.ceil(deficit / unit_cost)
                top_item["total_order_qty"] += extra_units
                top_item["send_to_amazon"] += extra_units
                top_item["line_total"] = round(top_item["total_order_qty"] * unit_cost, 2)
                top_item["direct_instruction"] += f" (Scaled +{extra_units} units to meet ${moq:,.2f} MOQ)"

        return pd.DataFrame(final_po_lines)
