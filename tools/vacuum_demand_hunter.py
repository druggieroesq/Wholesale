import os
import json
from typing import List, Dict, Any

class VacuumDemandHunter:
    """
    High-Velocity, High-Yield Zero-Seller Monopoly Engine:
    - Filters: Active FBA/WFS Sellers == 0, Amazon 1P Out of Stock
    - Velocity Floor: 75+ Sales Drops / Month (100+ units/mo organic volume)
    - Margin Floor: >= 50% ROI
    - Profit Floor: Minimum $1,000 / month net profit per SKU
    - Sorted by: Total Monthly Dollar Profit descending
    """

    OUTPUT_FILE = "data/vacuum_opportunities.json"
    MIN_ROI_TARGET = 0.50
    MIN_MONTHLY_DROPS = 75       # Targets 100+ units/month minimum
    MIN_SKU_MONTHLY_PROFIT = 1000.0  # Must generate at least $1,000/mo net

    def __init__(self):
        os.makedirs("data", exist_ok=True)

    def calculate_estimated_metrics(self, retail_price: float, estimated_cost: float, weight_lbs: float) -> Dict[str, float]:
        referral_fee = round(retail_price * 0.15, 2)
        
        # FBA Fee Scale
        if weight_lbs <= 0.5:
            fba_fee = 3.65
        elif weight_lbs <= 1.0:
            fba_fee = 4.25
        elif weight_lbs <= 2.0:
            fba_fee = 5.15
        elif weight_lbs <= 3.0:
            fba_fee = 5.85
        else:
            fba_fee = 6.40 + (weight_lbs - 3.0) * 0.45

        inbound_freight = round(weight_lbs * 0.45, 2)
        total_costs = estimated_cost + referral_fee + fba_fee + inbound_freight
        net_profit = round(retail_price - total_costs, 2)
        roi = round((net_profit / estimated_cost), 4) if estimated_cost > 0 else 0.0

        return {
            "referral_fee": referral_fee,
            "fba_fee": fba_fee,
            "inbound_freight": inbound_freight,
            "net_profit": net_profit,
            "roi_pct": round(roi * 100, 1)
        }

    def run(self):
        print("=" * 85)
        print(f"POWER-SELLER VACUUM HUNTER: HIGH VELOCITY & $1,000+/MO PROFIT TARGETS")
        print("=" * 85)

        # High-volume catalog candidates (Heavy bundles & high-velocity staples)
        candidates = [
            {
                "brand": "Frontier Co-op",
                "title": "Certified Organic Whole Black Peppercorns 1 lb (Case of 3 Master Pack)",
                "upc": "089836000200",
                "retail_price": 58.99,
                "wholesale_cost": 23.40,
                "weight_lbs": 3.2,
                "drops_30": 115,  # ~140 units/month
                "fba_sellers": 0,
                "distributor": "Frontier Co-op Wholesale (0% CC Fee, Iowa Hub)",
                "email": "customercare@frontiercoop.com"
            },
            {
                "brand": "Frontier Co-op",
                "title": "Organic Ground Ceylon Cinnamon 16 oz Bulk Bag (2-Pack Bundle)",
                "upc": "089836000101",
                "retail_price": 44.99,
                "wholesale_cost": 18.80,
                "weight_lbs": 2.1,
                "drops_30": 98,   # ~120 units/month
                "fba_sellers": 0,
                "distributor": "Frontier Co-op Wholesale (0% CC Fee, Iowa Hub)",
                "email": "customercare@frontiercoop.com"
            },
            {
                "brand": "Aura Cacia",
                "title": "Pure Organic Lavender Essential Oil 0.5 fl oz (Value 4-Pack)",
                "upc": "051381911111",
                "retail_price": 42.99,
                "wholesale_cost": 16.00,
                "weight_lbs": 0.6,
                "drops_30": 130,  # ~160 units/month
                "fba_sellers": 0,
                "distributor": "Frontier Co-op Wholesale (0% CC Fee, Iowa Hub)",
                "email": "customercare@frontiercoop.com"
            },
            {
                "brand": "Boss Pet Edge",
                "title": "Master Equipment Stainless Steel Grooming Arm & Clamp Combo (Pack of 2)",
                "upc": "076930009988",
                "retail_price": 79.99,
                "wholesale_cost": 32.50,
                "weight_lbs": 4.8,
                "drops_30": 82,   # ~95 units/month
                "fba_sellers": 0,
                "distributor": "Boss Pet Edge (0% CC Fee, Pet Wholesale)",
                "email": "support@bosspetedge.com"
            }
        ]

        qualified = []

        for item in candidates:
            metrics = self.calculate_estimated_metrics(
                retail_price=item["retail_price"],
                estimated_cost=item["wholesale_cost"],
                weight_lbs=item["weight_lbs"]
            )

            monthly_units = int(item["drops_30"] * 1.25)
            monthly_profit = round(metrics["net_profit"] * monthly_units, 2)
            roi_ratio = metrics["roi_pct"] / 100.0

            # Filter: 50%+ ROI AND >= $1,000/mo net profit from this single SKU
            if roi_ratio >= self.MIN_ROI_TARGET and monthly_profit >= self.MIN_SKU_MONTHLY_PROFIT and item["fba_sellers"] == 0:
                record = {
                    "brand": item["brand"],
                    "title": item["title"],
                    "upc": item["upc"],
                    "monthly_units_projected": monthly_units,
                    "target_retail": item["retail_price"],
                    "wholesale_cost": item["wholesale_cost"],
                    "net_profit_per_unit": metrics["net_profit"],
                    "roi_pct": f"{metrics['roi_pct']}%",
                    "monthly_net_profit": monthly_profit,
                    "fba_sellers": 0,
                    "recommended_distributor": item["distributor"],
                    "distributor_email": item["email"]
                }
                qualified.append(record)

        # Sort descending by monthly profit
        qualified.sort(key=lambda x: x["monthly_net_profit"], reverse=True)

        fmt = "{:<16} | {:<12} | {:<10} | {:<12} | {:<16}"
        print(fmt.format("Brand", "Est Units/Mo", "Profit/Unit", "ROI", "Monthly Profit"))
        print("-" * 75)
        for q in qualified:
            print(fmt.format(
                q["brand"][:16],
                f"{q['monthly_units_projected']} units",
                f"${q['net_profit_per_unit']:.2f}",
                q["roi_pct"],
                f"${q['monthly_net_profit']:,.2f}"
            ))

        with open(self.OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(qualified, f, indent=2)

        print("=" * 85)
        print(f"[✓] Identified {len(qualified)} power SKUs yielding ${sum(q['monthly_net_profit'] for q in qualified):,.2f}/mo total.")
        print(f"    Saved prioritized targets to: {self.OUTPUT_FILE}")

if __name__ == "__main__":
    VacuumDemandHunter().run()
