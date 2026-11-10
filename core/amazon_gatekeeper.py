#!/usr/bin/env python3
"""
Amazon Gatekeeper & Automated SP-API Soft-Ungater
-------------------------------------------------
Direct interface with Amazon Selling Partner API (SP-API).
Inspects account listing restrictions and submits programmatic soft-ungate applications.
"""

import os
import sys
import time
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List
import requests
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env", override=True)

logger = logging.getLogger("AmazonGatekeeper")
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )


class AmazonGatekeeper:
    def __init__(self):
        self.client_id = (
            os.getenv("SP_API_CLIENT_ID")
            or os.getenv("LWA_CLIENT_ID")
            or os.getenv("CLIENT_ID", "")
        ).strip()
        
        self.client_secret = (
            os.getenv("SP_API_CLIENT_SECRET")
            or os.getenv("LWA_CLIENT_SECRET")
            or os.getenv("CLIENT_SECRET", "")
        ).strip()
        
        self.refresh_token = (
            os.getenv("SP_API_REFRESH_TOKEN")
            or os.getenv("LWA_REFRESH_TOKEN")
            or os.getenv("REFRESH_TOKEN", "")
        ).strip()
        
        self.seller_id = (
            os.getenv("SP_API_SELLER_ID")
            or os.getenv("AMAZON_SELLER_ID")
            or os.getenv("SELLER_ID", "")
        ).strip()
        
        self.marketplace_id = (
            os.getenv("SP_API_MARKETPLACE_ID")
            or os.getenv("MARKETPLACE_ID", "ATVPDKIKX0DER")
        ).strip()
        
        self.base_url = "https://sellingpartnerapi-na.amazon.com"
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    def _get_access_token(self) -> Optional[str]:
        if self._access_token and time.time() < (self._token_expires_at - 60):
            return self._access_token

        if not self.refresh_token or not self.client_id or not self.client_secret:
            logger.error("Missing required LWA credentials in environment variables.")
            return None

        url = "https://api.amazon.com/auth/o2/token"
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret
        }
        
        try:
            res = requests.post(url, data=payload, timeout=15)
            if res.status_code == 200:
                data = res.json()
                self._access_token = data.get("access_token")
                expires_in = data.get("expires_in", 3600)
                self._token_expires_at = time.time() + float(expires_in)
                logger.info("Successfully refreshed Amazon LWA access token.")
                return self._access_token
            else:
                logger.error(f"LWA authentication failed ({res.status_code}): {res.text}")
        except Exception as e:
            logger.error(f"Error authenticating with Amazon LWA: {e}")
            
        return None

    def evaluate_asin_approval(self, asin: str) -> Dict[str, Any]:
        token = self._get_access_token()
        if not token:
            logger.warning("No valid access token available. Returning MOCK_OFFLINE.")
            return {"status": "MOCK_OFFLINE", "asin": asin}

        headers = {
            "x-amz-access-token": token,
            "Accept": "application/json",
            "User-Agent": "WholesaleSniper/1.0"
        }

        restrictions_url = f"{self.base_url}/listings/2021-08-01/restrictions"
        params = {
            "asin": asin,
            "sellerId": self.seller_id,
            "marketplaceIds": self.marketplace_id,
            "conditionType": "new_new"
        }

        try:
            res = requests.get(restrictions_url, headers=headers, params=params, timeout=10)
            
            if res.status_code == 200:
                data = res.json()
                restrictions = data.get("restrictions", [])

                if not restrictions:
                    return {"status": "UNRESTRICTED", "asin": asin, "reasons": []}

                return self._attempt_auto_ungate(asin, restrictions, headers)

            elif res.status_code == 429:
                logger.warning(f"Rate limited (429) checking restrictions for {asin}. Sleeping 2 seconds.")
                time.sleep(2)
                return {"status": "RATE_LIMITED", "asin": asin}
            else:
                logger.error(f"SP-API returned status {res.status_code} for ASIN {asin}: {res.text[:120]}")
                return {"status": "ERROR", "asin": asin, "code": res.status_code}

        except Exception as e:
            logger.error(f"SP-API error inspecting ASIN {asin}: {e}")

        return {"status": "INVOICE_REQUIRED", "asin": asin}

    def _attempt_auto_ungate(self, asin: str, restrictions: List[Dict[str, Any]], headers: Dict[str, str]) -> Dict[str, Any]:
        collected_reasons = []

        for r in restrictions:
            for reason in r.get("reasons", []):
                reason_code = reason.get("reasonCode", "UNKNOWN")
                collected_reasons.append(reason_code)
                links = reason.get("links", [])
                
                for link in links:
                    resource = link.get("resource", "")
                    verb = link.get("verb", "").upper()
                    
                    if "approval" in resource.lower() or verb == "POST":
                        try:
                            app_url = resource if resource.startswith("http") else f"{self.base_url}{resource}"
                            apply_res = requests.post(app_url, headers=headers, json={"asin": asin}, timeout=12)
                            
                            if apply_res.status_code in [200, 204]:
                                res_json = apply_res.json() if apply_res.text else {}
                                if res_json.get("status") == "APPROVED" or apply_res.status_code == 204:
                                    logger.info(f"Auto-ungate application approved for ASIN {asin}!")
                                    return {"status": "AUTO_UNGATED", "asin": asin}
                        except Exception as e:
                            logger.error(f"Error submitting auto-ungate application for ASIN {asin}: {e}")

        return {
            "status": "INVOICE_REQUIRED",
            "asin": asin,
            "reasons": collected_reasons
        }


if __name__ == "__main__":
    gatekeeper = AmazonGatekeeper()
    test_asin = sys.argv[1] if len(sys.argv) > 1 else "B00004SQ5G"
    print(f"Testing AmazonGatekeeper with ASIN: {test_asin}")
    result = gatekeeper.evaluate_asin_approval(test_asin)
    print("Result:", result)
