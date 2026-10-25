import time
import requests
from typing import List, Dict, Any

class BatchKeepaClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.endpoint = "https://api.keepa.com/product"

    def fetch_batch_upcs(self, upc_list: List[str], chunk_size: int = 100) -> Dict[str, Dict[str, Any]]:
        """Queries up to 100 UPCs per request to optimize API tokens and runtime."""
        if not self.api_key or self.api_key == "your_keepa_api_key_here":
            return {upc: self._mock_product(upc) for upc in upc_list}

        results = {}
        for i in range(0, len(upc_list), chunk_size):
            chunk = upc_list[i:i + chunk_size]
            code_param = ",".join(chunk)
            
            params = {
                "key": self.api_key,
                "domain": 1,
                "code": code_param,
                "stats": 90,
                "buybox": 1
            }
            
            try:
                res = requests.get(self.endpoint, params=params, timeout=20)
                if res.status_code == 200:
                    products = res.json().get("products", [])
                    for prod in products:
                        # Extract the UPC or EAN returned
                        upcs_returned = prod.get("upcList", [])
                        for u in upcs_returned:
                            results[str(u).zfill(12)] = prod
                elif res.status_code == 429:
                    print("[!] Keepa token bucket exhausted. Sleeping 10s...")
                    time.sleep(10)
            except Exception as e:
                print(f"[!] Error fetching batch: {e}")
                
        return results

    def _mock_product(self, upc: str) -> Dict[str, Any]:
        return {
            "asin": f"B00MOCK{upc[-4:]}",
            "title": f"Mock Item {upc}",
            "stats": {"outOfStockPercentageAmazon90": 100, "salesRankDrops30": 35, "current": [0]*12 + [4] + [0]*5 + [2499]},
            "variations": []
        }
