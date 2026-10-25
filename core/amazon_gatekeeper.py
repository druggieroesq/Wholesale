import os
import requests
from typing import Dict, Any, Optional

class AmazonGatekeeper:
    """
    Direct interface with Amazon SP-API.
    Inspects account listing restrictions and submits automated soft-ungate applications.
    """
    def __init__(self):
        self.client_id = os.getenv("SP_API_CLIENT_ID", "")
        self.client_secret = os.getenv("SP_API_CLIENT_SECRET", "")
        self.refresh_token = os.getenv("SP_API_REFRESH_TOKEN", "")
        self.seller_id = os.getenv("SP_API_SELLER_ID", "")
        self.marketplace_id = os.getenv("SP_API_MARKETPLACE_ID", "ATVPDKIKX0DER")  # US Marketplace
        self.base_url = "https://sellingpartnerapi-na.amazon.com"
        self._access_token: Optional[str] = None

    def _get_access_token(self) -> Optional[str]:
        if self._access_token:
            return self._access_token

        if not self.refresh_token or not self.client_id or not self.client_secret:
            return None

        url = "https://api.amazon.com/auth/o2o/token"
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret
        }
        try:
            res = requests.post(url, data=payload, timeout=10)
            if res.status_code == 200:
                self._access_token = res.json().get("access_token")
                return self._access_token
        except Exception as e:
            print(f"[!] Error authenticating with Amazon LWA: {e}")
        return None

    def evaluate_asin_approval(self, asin: str) -> Dict[str, Any]:
        """
        1. Checks if already ungated.
        2. If gated, executes automated approval request.
        3. Returns: 'UNRESTRICTED', 'AUTO_UNGATED', or 'INVOICE_REQUIRED'.
        """
        token = self._get_access_token()
        if not token:
            # Safe Fallback: When offline or in mock mode without SP-API keys
            return {"status": "MOCK_OFFLINE"}

        headers = {
            "x-amz-access-token": token,
            "User-Agent": "WholesaleSniper/1.0"
        }

        # Query Restrictions Endpoint
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

                # 1. Zero restrictions = Already clean and ungated
                if not restrictions:
                    return {"status": "UNRESTRICTED"}

                # 2. Restrictions exist -> Attempt Automated Approval
                return self._attempt_auto_ungate(asin, restrictions, headers)

        except Exception as e:
            print(f"[!] SP-API Error inspecting ASIN {asin}: {e}")

        return {"status": "INVOICE_REQUIRED"}

    def _attempt_auto_ungate(self, asin: str, restrictions: list, headers: dict) -> Dict[str, Any]:
        """Submits programmatic soft-ungate application to trigger Amazon's auto-approval algorithm."""
        for r in restrictions:
            for reason in r.get("reasons", []):
                links = reason.get("links", [])
                for link in links:
                    # Check if an automated approval submission endpoint is available
                    if "approval" in link.get("resource", "").lower() or link.get("verb") == "POST":
                        try:
                            app_url = link.get("resource")
                            if not app_url.startswith("http"):
                                app_url = f"{self.base_url}{app_url}"
                            
                            apply_res = requests.post(app_url, headers=headers, json={"asin": asin}, timeout=10)
                            if apply_res.status_code in [200, 204]:
                                res_json = apply_res.json() if apply_res.text else {}
                                if res_json.get("status") == "APPROVED" or apply_res.status_code == 204:
                                    return {"status": "AUTO_UNGATED"}
                        except Exception:
                            pass

        return {"status": "INVOICE_REQUIRED"}
