import os
import json
from typing import List, Dict, Any

class SupplierManager:
    """
    Manages distributor registries, onboarding statuses, and credit terms.
    """

    REGISTRY_PATH = "data/distributors.json"

    def __init__(self):
        os.makedirs("data", exist_ok=True)
        self.suppliers = self._load_suppliers()

    def _load_suppliers(self) -> List[Dict[str, Any]]:
        if os.path.exists(self.REGISTRY_PATH):
            try:
                with open(self.REGISTRY_PATH, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"[!] Warning reading suppliers: {e}")
        return []

    def get_all_suppliers(self) -> List[Dict[str, Any]]:
        return self.suppliers

    def get_supplier_by_name(self, name: str) -> Dict[str, Any]:
        name_clean = name.strip().lower()
        for s in self.suppliers:
            if s.get("name", "").strip().lower() == name_clean:
                return s
        return {}

    def add_supplier(self, supplier_data: Dict[str, Any]) -> bool:
        name = supplier_data.get("name", "").strip()
        if not name:
            return False

        for s in self.suppliers:
            if s.get("name", "").strip().lower() == name.lower():
                return False

        self.suppliers.append(supplier_data)
        with open(self.REGISTRY_PATH, "w", encoding="utf-8") as f:
            json.dump(self.suppliers, f, indent=2)
        return True
