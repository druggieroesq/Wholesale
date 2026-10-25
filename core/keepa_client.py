import requests
from typing import Dict, Any, Optional

class KeepaClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.keepa.com/product"

    def get_product_by_upc(self, upc: str) -> Optional[Dict[str, Any]]:
        """Queries Keepa API by 12-digit UPC with 90-day historical stats."""
        if not self.api_key or self.api_key == "your_keepa_api_key_here":
            return self._mock_keepa_response(upc)

        params = {
            "key": self.api_key,
            "domain": 1,
            "code": upc,
            "stats": 90
        }
        try:
            res = requests.get(self.base_url, params=params, timeout=12)
            if res.status_code == 200:
                data = res.json()
                products = data.get("products", [])
                return products[0] if products else None
        except requests.RequestException as e:
            print(f"[!] Error querying UPC {upc}: {e}")
            return None
        return None

    def _mock_keepa_response(self, upc: str) -> Dict[str, Any]:
        """Synthetic payload to run offline without paid API key."""
        return {
            "asin": f"B00MOCK{upc[-4:]}",
            "title": f"Simulated Consumable Product for UPC {upc}",
            "stats": {
                "outOfStockPercentageAmazon90": 100,
                "salesRankDrops30": 42,
                "current": [0] * 12 + [5] + [0] * 5 + [1999]
            },
            "variations": [
                {"asin": f"B00MOCK{upc[-4:]}", "attributes": [{"dimension": "Package Quantity", "value": "1"}]},
                {"asin": f"B00MULTI{upc[-4:]}", "attributes": [{"dimension": "Package Quantity", "value": "2"}]}
            ]
        }
