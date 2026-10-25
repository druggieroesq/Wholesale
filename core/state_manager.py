import json
import os
from typing import Set, List

STATE_FILE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "state.json")

class StateManager:
    def __init__(self, file_path: str = STATE_FILE_PATH):
        self.file_path = file_path
        self._ensure_file_exists()

    def _ensure_file_exists(self):
        if not os.path.exists(self.file_path):
            os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump({"ungated_brands": [], "pending_applications": []}, f, indent=2)

    def load_state(self) -> dict:
        self._ensure_file_exists()
        with open(self.file_path, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {"ungated_brands": [], "pending_applications": []}

    def get_ungated_brands(self) -> Set[str]:
        state = self.load_state()
        return set(b.lower().strip() for b in state.get("ungated_brands", []))

    def get_pending_brands(self) -> Set[str]:
        state = self.load_state()
        return set(b.lower().strip() for b in state.get("pending_applications", []))

    def mark_brand_as_ungated(self, brand: str):
        """Marks a brand as officially ungated in Amazon Seller Central."""
        state = self.load_state()
        normalized = brand.lower().strip()
        
        ungated = set(b.lower().strip() for b in state.get("ungated_brands", []))
        ungated.add(normalized)
        
        # Remove from pending if present
        pending = [b for b in state.get("pending_applications", []) if b.lower().strip() != normalized]
        
        state["ungated_brands"] = sorted(list(ungated))
        state["pending_applications"] = pending

        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        print(f"[✓] Brand '{brand}' is now UNGATED. The optimizer will bypass it.")

    def mark_brands_pending(self, brands: List[str]):
        """Records brands that were ordered and submitted to Amazon for invoice review."""
        state = self.load_state()
        pending = set(b.lower().strip() for b in state.get("pending_applications", []))
        for b in brands:
            pending.add(b.lower().strip())
        state["pending_applications"] = sorted(list(pending))

        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
