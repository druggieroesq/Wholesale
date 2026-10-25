import os
import time
import requests
from typing import Dict, List, Any, Optional

class KeepaClient:
    """
    Production-ready Keepa client with:
    - 100-UPC bulk batching (1 API call per 100 products)
    - Dynamic token-bucket rate limiter (prevents 429 errors in cloud runs)
    - Seamless fallback to offline Mock Mode if no API key is set
    """
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("KEEPA_API_KEY", "")
        self.base_url = "https://api.keepa.com/product"
        self.is_live = bool(self.api_key and self.api_key.strip() and self.api_key != "your_keepa_api_key_here")

    def get_products_batch(self, upc_list: List[str], chunk_size: int = 100) -> Dict[str, Dict[str, Any]]:
        """
        Queries Keepa for up to 100 UPCs in a single HTTP request.
        Returns a dictionary mapping cleaned 12-digit UPC -> Keepa product payload.
        """
        cleaned_upcs = [str(u).strip().zfill(12) for u in upc_list if u]
        if not cleaned_upcs:
            return {}

        if not self.is_live:
            return {upc: self._mock_product_response(upc) for upc in cleaned_upcs}

        results: Dict[str, Dict[str, Any]] = {}

        for i in range(0, len(cleaned_upcs), chunk_size):
            chunk = cleaned_upcs[i:i + chunk_size]
            code_param = ",".join(chunk)

            params = {
                "key": self.api_key,
                "domain": 1,           # 1 = Amazon.com (US)
                "code": code_param,
                "stats": 90,           # Include 90-day historical stats
                "buybox": 1,           # Include Buy Box history
                "history": 1           # Include CSV historical arrays for IP audits
            }

            try:
                res = requests.get(self.base_url, params=params, timeout=25)

                if res.status_code == 200:
                    data = res.json()
                    tokens_left = data.get("tokensLeft", 100)

                    # Token Bucket Protection: Back off if token pool is depleted
                    if tokens_left < 20:
                        refill_rate = data.get("refillRate", 10)
                        sleep_time = max(5, int((30 - tokens_left) / (refill_rate / 60.0))) if refill_rate else 10
                        print(f"[*] Keepa tokens low ({tokens_left} left). Throttling {sleep_time}s...")
                        time.sleep(sleep_time)

                    products = data.get("products", [])
                    for prod in products:
                        # Extract all UPCs/EANs associated with this product
                        upcs = prod.get("upcList", [])
                        ean_list = prod.get("eanList", [])
                        all_codes = [str(c).zfill(12) for c in (upcs + ean_list)]

                        for code in all_codes:
                            results[code] = prod

                        # Also index by ASIN
                        asin = prod.get("asin")
                        if asin:
                            results[asin] = prod

                elif res.status_code == 429:
                    print("[!] Keepa 429 Rate Limit hit. Backing off for 15 seconds...")
                    time.sleep(15)
                else:
                    print(f"[!] Keepa API error {res.status_code}: {res.text[:120]}")

            except Exception as e:
                print(f"[!] Keepa connection error: {e}")

        return results

    def get_product_by_upc(self, upc: str) -> Optional[Dict[str, Any]]:
        """Single-UPC convenience wrapper around batch query."""
        res = self.get_products_batch([upc])
        return res.get(str(upc).strip().zfill(12))

    def _mock_product_response(self, upc: str) -> Dict[str, Any]:
        """Provides deterministic mock data for offline testing."""
        upc_clean = str(upc).zfill(12)
        last_digits = int(upc_clean[-2:]) if upc_clean[-2:].isdigit() else 10

        # Simulate varied mock products based on barcode
        buy_box_cents = 1899 + (last_digits * 40)
        drops = 22 + (last_digits % 30)
        sellers = 3 + (last_digits % 6)
        fba_fee_cents = 377 + (last_digits * 5)

        return {
            "asin": f"B00MOCK{upc_clean[-4:]}",
            "title": f"Mock Wholesale Product {upc_clean}",
            "fbaFees": fba_fee_cents,
            "referralFeePercent": 15,
            "packageWeight": 450,
            "stats": {
                "outOfStockPercentageAmazon90": 100,  # Amazon 1P 0% in stock
                "salesRankDrops30": drops,
                "current": [0]*11 + [sellers] + [0]*6 + [buy_box_cents],
                "avg90": [0]*11 + [float(sellers)] + [0]*6 + [float(buy_box_cents)]
            },
            "csv": [[] for _ in range(12)]
        }
