import os
import re
import json
import time
from typing import List, Dict, Any, Optional
import requests
from bs4 import BeautifulSoup

class AutomatedDealSniper:
    """
    Automated Deal Sniper:
    1. Scrapes or parses live distributor inventory/catalog endpoints.
    2. Queries live Amazon metrics via Keepa API or SP-API.
    3. Identifies Zero-Seller Monopoly gaps:
       - FBA Sellers == 0
       - Amazon 1P OOS
       - ROI >= 50%
       - Net Profit >= $1,000/mo (projected via drops)
    """

    OUTPUT_FILE = "data/live_scraped_deals.json"
    MIN_ROI = 0.50
    MIN_MONTHLY_PROFIT = 1000.0

    def __init__(self, keepa_api_key: Optional[str] = None):
        self.keepa_key = keepa_api_key or os.getenv("KEEPA_API_KEY", "")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })

    def scrape_sample_distributor_feed(self, url: str) -> List[Dict[str, Any]]:
        """
        Extracts product entities from a public/dealer catalog page.
        Adapts to standard HTML tables or JSON endpoints provided by wholesale portals.
        """
        print(f"[*] Crawling distributor feed: {url}...")
        items = []
        try:
            resp = self.session.get(url, timeout=15)
            if resp.status_code != 200:
                print(f"[!] Warning: HTTP {resp.status_code} fetching {url}")
                return items

            soup = BeautifulSoup(resp.text, "html.parser")
            
            # Example generic parser for catalog cards/tables:
            # Adjust selectors based on target structure (e.g., product-card, table rows)
            for card in soup.select(".product-item, .product-card, tr.catalog-row"):
                title_elem = card.select_one(".product-title, .title, td.name")
                price_elem = card.select_one(".price, .wholesale-price, td.cost")
                upc_elem = card.select_one(".upc, .sku, td.upc")

                if title_elem and price_elem:
                    title = title_elem.get_text(strip=True)
                    price_str = re.sub(r"[^\d.]", "", price_elem.get_text())
                    upc = upc_elem.get_text(strip=True) if upc_elem else ""

                    if price_str:
                        items.append({
                            "title": title,
                            "wholesale_cost": float(price_str),
                            "upc": upc,
                            "source_url": url
                        })
        except Exception as e:
            print(f"[!] Error crawling distributor {url}: {e}")

        print(f"[✓] Extracted {len(items)} catalog products from source.")
        return items

    def query_amazon_live_metrics(self, upc: str, title: str) -> Optional[Dict[str, Any]]:
        """
        Queries Amazon live data via Keepa API.
        If UPC is missing, attempts a product search query.
        """
        if not self.keepa_key:
            # Fallback mock metrics structure if no live API token configured yet
            return None

        # Endpoint: Query Amazon product by UPC/EAN or search terms
        endpoint = "https://api.keepa.com/product"
        params = {
            "key": self.keepa_key,
            "domain": "1", # 1 = Amazon.com
            "code": upc if upc else "",
            "stats": "30",
            "offers": "20"
        }

        try:
            resp = requests.get(endpoint, params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                products = data.get("products", [])
                if not products:
                    return None

                p = products[0]
                stats = p.get("stats", {})
                
                # Keepa prices are integers in cents (-1 = OOS)
                buy_box_cents = stats.get("buyBoxPrice", -1)
                amazon_1p_cents = stats.get("current", [0])[0] # Index 0 is Amazon 1P
                fba_seller_count = stats.get("fbaOfferCount", 0)
                sales_drops_30 = stats.get("salesRankDrops30", 0)

                buy_box = buy_box_cents / 100.0 if buy_box_cents > 0 else 0.0
                amazon_oos = (amazon_1p_cents == -1)

                return {
                    "asin": p.get("asin"),
                    "buy_box": buy_box,
                    "fba_seller_count": fba_seller_count,
                    "amazon_oos": amazon_oos,
                    "sales_drops_30": sales_drops_30,
                    "package_weight_lbs": (p.get("packageWeight", 453) / 453.592) # grams to lbs
                }
        except Exception as e:
            print(f"[!] Live Keepa lookup failed: {e}")

        return None

    def calculate_deal(self, wholesale_cost: float, retail_price: float, weight_lbs: float, drops: int) -> Optional[Dict[str, Any]]:
        """Applies exact fee schedule and ROI/Profit filters."""
        referral_fee = round(retail_price * 0.15, 2)
        
        # FBA Fee Schedule
        if weight_lbs <= 0.5:
            fba_fee = 3.65
        elif weight_lbs <= 1.0:
            fba_fee = 4.25
        elif weight_lbs <= 2.0:
            fba_fee = 5.15
        elif weight_lbs <= 3.0:
            fba_fee = 5.85
        elif weight_lbs <= 4.0:
            fba_fee = 6.45
        else:
            fba_fee = 6.95 + (weight_lbs - 4.0) * 0.45

        inbound_freight = round(weight_lbs * 0.45, 2)
        total_cost = wholesale_cost + referral_fee + fba_fee + inbound_freight
        net_profit = round(retail_price - total_cost, 2)

        if wholesale_cost <= 0:
            return None

        roi = net_profit / wholesale_cost
        projected_monthly_units = int(drops * 1.25)
        projected_monthly_profit = round(net_profit * projected_monthly_units, 2)

        if roi >= self.MIN_ROI and projected_monthly_profit >= self.MIN_MONTHLY_PROFIT:
            return {
                "retail_price": retail_price,
                "wholesale_cost": wholesale_cost,
                "net_profit_per_unit": net_profit,
                "roi_pct": round(roi * 100, 1),
                "monthly_net_profit": projected_monthly_profit,
                "projected_monthly_units": projected_monthly_units
            }
        return None

    def run_discovery_pipeline(self, distributor_urls: List[str]):
        print("=" * 80)
        print("STARTING LIVE AUTOMATED DEAL DISCOVERY ENGINE")
        print("=" * 80)

        all_deals = []

        for url in distributor_urls:
            products = self.scrape_sample_distributor_feed(url)
            for item in products:
                # 1. Search Amazon for the product
                amazon_metrics = self.query_amazon_live_metrics(item.get("upc", ""), item.get("title", ""))
                
                if not amazon_metrics:
                    continue

                # 2. Check Zero-Seller Monopoly criteria
                if amazon_metrics["fba_seller_count"] == 0 and amazon_metrics["amazon_oos"]:
                    deal = self.calculate_deal(
                        wholesale_cost=item["wholesale_cost"],
                        retail_price=amazon_metrics["buy_box"],
                        weight_lbs=amazon_metrics["package_weight_lbs"],
                        drops=amazon_metrics["sales_drops_30"]
                    )
                    if deal:
                        deal.update({
                            "asin": amazon_metrics["asin"],
                            "title": item["title"],
                            "distributor_source": url
                        })
                        all_deals.append(deal)
                        print(f"[FOUND DEAL] {deal['title'][:35]} -> ${deal['monthly_net_profit']}/mo ({deal['roi_pct']}%)")

                # Respect API rate limits
                time.sleep(0.5)

        with open(self.OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(all_deals, f, indent=2)

        print("=" * 80)
        print(f"[✓] Discovery complete. Found {len(all_deals)} live vacuum opportunities.")
        print(f"[✓] Saved to: {self.OUTPUT_FILE}")

if __name__ == "__main__":
    # Test runner: Supply distributor catalog URLs and Keepa key
    hunter = AutomatedDealSniper()
    test_urls = [
        # Replace with specific authenticated or public catalog endpoints
        "https://wholesale.frontiercoop.com/specials",
        "https://www.marlobeauty.com/promotions"
    ]
    hunter.run_discovery_pipeline(test_urls)
