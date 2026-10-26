import re
from typing import Dict, Any

class PackageQuantityResolver:
    """
    Parses product titles and Keepa attributes to identify multi-pack bundles (2pk, 3pk, etc.).
    Protects margins by ensuring multi-unit listings are priced against bundled wholesale cost.
    """

    def __init__(self):
        pass

    def resolve_pack_quantity(self, keepa_product: Dict[str, Any]) -> int:
        """
        Extracts verified pack count from Keepa packageQuantity attribute
        or regex matching on product title keywords.
        """
        keepa_product = keepa_product or {}
        title = (keepa_product.get("title") or "").lower()

        # 1. Direct Keepa package attribute
        pkg_qty = keepa_product.get("packageQuantity")
        if pkg_qty and isinstance(pkg_qty, int) and pkg_qty > 1:
            return pkg_qty

        # 2. High-precision regex pattern match
        patterns = [
            r'\b(?:pack of|count of|pk of)\s*(\d+)\b',
            r'\b(\d+)\s*(?:pack|pk|count|ct)\b',
            r'\((\d+)-pack\)',
            r'\((\d+)\s*pack\)'
        ]

        for pat in patterns:
            match = re.search(pat, title)
            if match:
                try:
                    qty = int(match.group(1))
                    if 1 < qty <= 48:
                        return qty
                except Exception:
                    pass

        return 1
