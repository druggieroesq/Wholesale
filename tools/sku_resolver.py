import os
import re
import urllib.parse
from typing import Dict, Any, Optional

class SKUResolver:
    """
    Direct vendor link resolver for high-demand wholesale sourcing.
    Maps ASINs, MPNs, and search terms directly into distributor portals.
    """
    SEARCH_PATTERNS = {
        "global_industrial": "https://www.globalindustrial.com/search?q={query}",
        "midland_metal": "https://www.midlandmetal.com/search?q={query}",
        "atlantic_spice": "https://atlanticspice.com/search.asp?keyword={query}",
        "frontier_coop": "https://www.frontiercoop.com/search?q={query}",
        "jb_tools": "https://www.jbtools.com/search.php?search_query={query}",
        "marlo_beauty": "https://www.marlobeauty.com/search.aspx?keyword={query}"
    }

    @staticmethod
    def clean_identifier(text: str) -> str:
        if not text:
            return ""
        # Strip trailing package specs or excess noise
        cleaned = re.sub(r'[\(\)\[\]\{\}]', ' ', text)
        return cleaned.strip()

    def get_order_url(self, distributor_key: str, mpn: Optional[str] = None, title: Optional[str] = None, asin: Optional[str] = None) -> str:
        dist_key = distributor_key.lower().replace(" ", "_").replace("-", "_")
        pattern = self.SEARCH_PATTERNS.get(dist_key)
        
        if not pattern:
            # Fallback to Google Search directly targeting vendor
            query = f"{distributor_key} {mpn or title or asin or ''}".strip()
            return f"https://www.google.com/search?q={urllib.parse.quote_plus(query)}"

        # Priority 1: Manufacturer Part Number (MPN)
        if mpn and len(mpn.strip()) > 2:
            search_term = self.clean_identifier(mpn)
            return pattern.format(query=urllib.parse.quote_plus(search_term))

        # Priority 2: Product Title (first 4 distinct keywords)
        if title:
            words = [w for w in re.split(r'\s+', title) if len(w) > 2 and not w.lower().startswith("pack")][:4]
            search_term = " ".join(words)
            return pattern.format(query=urllib.parse.quote_plus(search_term))

        # Priority 3: ASIN
        return pattern.format(query=urllib.parse.quote_plus(asin or ""))


if __name__ == "__main__":
    resolver = SKUResolver()
    print("Testing Global Industrial MRO Resolution:")
    print(resolver.get_order_url("global_industrial", mpn="123-ABC-MRO", title="Industrial Air Filter"))
    print("\nTesting Midland Metal Resolution:")
    print(resolver.get_order_url("midland_metal", mpn="BRASS-90-DEG-1/2"))
