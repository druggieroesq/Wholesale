import os
import time
import requests
from typing import List, Dict, Any, Union

class KeepaClient:
    """
    Production Keepa API Client.
    Batches up to 100 UPCs per call, tracks token balance, dynamically throttles
    to prevent 429 errors, and parses Amazon + Walmart product data.
    """
    BASE_URL = "https://api.keepa.com/product"

    def __init__(self, api_key: str = None):
        # Reads key passed directly or pulls from system environment / Railway variables
        self.api_key = (api_key or os.getenv("KEEPA_API_KEY", "")).strip()

    def fetch_products_by_upc(
        self, 
        upcs: Union[List[str], str], 
        domain: int = 1
    ) -> List[Dict[str, Any]]:
        """
        Batches UPCs (up to 100 per API request) and queries Keepa product endpoint.
        Domain 1 = Amazon.com (US).
        """
        if not self.api_key:
            print("[!] KEEPA_API_KEY is not configured. Skipping live Keepa lookup.")
            return []

        if isinstance(upcs, str):
            upcs = [upcs]

        # Sanitize and zero-pad UPCs (standard GTIN-12)
        clean_upcs = [str(u).strip().zfill(12) for u in upcs if str(u).strip()]
        if not clean_upcs:
            return []

        all_products = []
        batch_size = 100

        for i in range(0, len(clean_upcs), batch_size):
            batch = clean_upcs[i:i + batch_size]
            code_string = ",".join(batch)

            # stats=90 pulls 90-day averages (Buy Box, FBA counts, drop counts)
            # history=1 enables the CSV arrays (Index 0 = Amz 1P, Index 11 = Walmart)
            params = {
                "key": self.api_key,
                "domain": domain,
                "code": code_string,
                "stats": 90,
                "history": 1
            }

            try:
                response = requests.get(self.BASE_URL, params=params, timeout=30)
                if response.status_code == 200:
                    data = response.json()
                    products = data.get("products", [])
                    all_products.extend(products)

                    tokens_left = data.get("tokensLeft", 0)
                    refill_rate = data.get("refillRate", 0)
                    
                    # Keepa Token Guard: Dynamic throttling if tokens run low
                    if tokens_left < 25:
                        wait_seconds = max(2.0, (25 - tokens_left) / (refill_rate if refill_rate > 0 else 1))
                        print(f"[*] Keepa tokens low ({tokens_left} left). Throttling for {wait_seconds:.1f}s...")
                        time.sleep(wait_seconds)
                else:
                    print(f"[!] Keepa API Error {response.status_code}: {response.text[:150]}")
            except Exception as e:
                print(f"[!] Network error connecting to Keepa API: {e}")

        return all_products

    def get_product_by_upc(self, upc: str, domain: int = 1) -> Dict[str, Any]:
        """
        Single-UPC lookup wrapper for backwards compatibility.
        """
        results = self.fetch_products_by_upc([upc], domain=domain)
        return results[0] if results else {}

    # Aliases to prevent import or method naming mismatches
    fetch_product_by_upc = get_product_by_upc
    get_products_by_upc = fetch_products_by_upc
