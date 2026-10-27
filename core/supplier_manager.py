import os
import json
from typing import List, Dict, Any, Optional

class SupplierManager:
    """
    Manages registered wholesale distributors, payment settlement terms,
    and in-house self-pickup rules (Parkland, FL & West Bloomfield, MI).
    """
    REGISTRY_PATH = "data/distributors.json"

    def __init__(self, registry_path: Optional[str] = None):
        if registry_path:
            self.REGISTRY_PATH = registry_path
        os.makedirs("data", exist_ok=True)
        self.suppliers = self.load_suppliers()

    def load_suppliers(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.REGISTRY_PATH):
            default_suppliers = [
                {
                    "name": "Frontier Co-op",
                    "city": "Norway",
                    "state": "IA",
                    "zip": "52318",
                    "email": "customercare@frontiercoop.com",
                    "self_pickup": False,
                    "payment_terms": {
                        "payment_method": "CREDIT_CARD",
                        "cc_surcharge_pct": 0.0,
                        "early_pay_discount_pct": 0.0,
                        "settlement_window_days": 30
                    },
                    "notes": "Specialty spices, extracts & Aura Cacia. 0% CC fees."
                },
                {
                    "name": "Universal General Products",
                    "city": "Doral",
                    "state": "FL",
                    "zip": "33122",
                    "email": "info@universalgeneral.com",
                    "self_pickup": True,
                    "payment_terms": {
                        "payment_method": "ACH",
                        "cc_surcharge_pct": 0.03,
                        "early_pay_discount_pct": 0.0,
                        "settlement_window_days": 30
                    },
                    "notes": "Local South Florida pickup ($0 inbound freight)."
                }
            ]
            with open(self.REGISTRY_PATH, "w", encoding="utf-8") as f:
                json.dump(default_suppliers, f, indent=2)
            return default_suppliers

        try:
            with open(self.REGISTRY_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[!] Error loading suppliers: {e}")
            return []

    def get_all_suppliers(self) -> List[Dict[str, Any]]:
        return self.suppliers

    def find_supplier_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        clean_name = name.strip().lower()
        for sup in self.suppliers:
            if sup.get("name", "").strip().lower() == clean_name:
                return sup
        return None

    def save_suppliers(self) -> bool:
        try:
            with open(self.REGISTRY_PATH, "w", encoding="utf-8") as f:
                json.dump(self.suppliers, f, indent=2)
            return True
        except Exception as e:
            print(f"[!] Error saving suppliers: {e}")
            return False
