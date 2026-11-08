#!/usr/bin/env python3
"""
Core IP & Account Health Guard (v5.0 Enterprise)
-----------------------------------------------
1. Static Shield: Enforces strict alphanumeric brand matching against 
   data/ip_blocklist.json (180+ litigious brands, Amazon private labels, LOA gates).
2. Dynamic Canary Shield: Evaluates Keepa historical sellerCount arrays to detect
   sudden seller cliffs (>= 60% drop within 14 days) indicative of Vorys/C&D sweeps.
3. Fail-Safe: Caches normalized brand lookups in memory for zero-latency execution.
"""

import os
import re
import json
import logging
from typing import Union, List, Dict, Any, Tuple

logger = logging.getLogger("IPGuard")

DEFAULT_BLOCKLIST_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 
    "data", 
    "ip_blocklist.json"
)

# Core fallback list if data/ip_blocklist.json is missing or corrupted
EMERGENCY_FALLBACK_BRANDS = [
    "3M", "ANDIS", "ARM & HAMMER", "ARM AND HAMMER", "BAG BALM", "BALL", 
    "BAYER", "BENEBONE", "BIL JAC", "BLACK FLAG", "BOEHRINGER INGELHEIM", 
    "BONIDE", "CADET", "CHUCKIT", "COMMAND", "ELANCO", "FIRST ALERT", 
    "FRONTLINE", "GREENIES", "HARTZ", "KIDDE", "KONG", "OSTER", "PETSAFE", 
    "POST-IT", "PURINA", "RADIO SYSTEMS", "SCOTCH", "SERESTO", "WAHL", 
    "ZOETIS", "DEWALT", "STANLEY", "CRAFTSMAN", "IRWIN", "MILWAUKEE", 
    "MAKITA", "BLACK & DECKER", "PORTER-CABLE", "BOSTITCH", "LENOX", 
    "RYOBI", "RIDGID", "BOSCH", "DREMEL", "SKIL", "KLEIN TOOLS", "SNAP-ON", 
    "FLIR", "LEATHERMAN", "SPYDERCO", "BENCHMADE", "OLAPLEX", "MOROCCANOIL", 
    "PAUL MITCHELL", "KERASTASE", "REDKEN", "MATRIX", "BIOLAGE", "WELLA", 
    "OPI", "DERMALOGICA", "CERAVE", "LA ROCHE-POSAY", "DYSON", "LOREAL",
    "AMAZON BASICS", "AMAZONBASICS", "SOLIMO", "PRESTO", "AMAZON ELEMENTS", 
    "HAPPY BELLY", "AMAZON FRESH", "MAMA BEAR", "WAG", "BASIC CARE",
    "OTTERBOX", "LIFEPROOF", "POPSOCKETS", "YETI", "HYDRO FLASK", "TRAEGER", 
    "BOSE", "SONOS", "LEGO", "ANKER", "THERAGUN", "GOVEE", "TIDE", "BOUNTY", 
    "CHARMIN", "DAWN", "CLOROX", "LYSOL", "ORAL-B", "RUBBERMAID", "SHARK"
]


class IPGuard:
    """Enterprise Brand Protection, Amazon Private Label & IP Sweep Interceptor."""

    def __init__(self, blocklist_path: str = DEFAULT_BLOCKLIST_PATH):
        self.blocklist_path = blocklist_path
        self.raw_brands: List[str] = []
        self.normalized_brands: set = set()
        self.reload_blocklist()

    @staticmethod
    def normalize_brand(name: Any) -> str:
        """Strips punctuation, spaces, accents, and converts to uppercase."""
        if not name:
            return ""
        return re.sub(r"[^A-Z0-9]", "", str(name).upper())

    def reload_blocklist(self) -> int:
        """Loads and indexes the blocklist with normalized alphanumeric hashing."""
        loaded = []
        if os.path.exists(self.blocklist_path):
            try:
                with open(self.blocklist_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        loaded = data
                    elif isinstance(data, dict):
                        loaded = list(data.keys())
            except Exception as e:
                logger.error(f"Error loading {self.blocklist_path}: {e}")

        if not loaded:
            loaded = EMERGENCY_FALLBACK_BRANDS

        # Clean metadata artifacts
        banned_meta = {"BRANDS", "SKUS", "UPDATED_AT", "VERSION"}
        self.raw_brands = [str(b).strip().upper() for b in loaded if str(b).strip().upper() not in banned_meta]
        self.normalized_brands = {self.normalize_brand(b) for b in self.raw_brands if self.normalize_brand(b)}
        
        logger.info(f"IPGuard loaded {len(self.normalized_brands)} normalized brand shields.")
        return len(self.normalized_brands)

    def is_ip_restricted(self, brand: str, title: str = "") -> Tuple[bool, str]:
        """
        Checks if a brand or title contains a litigious, gated, or Amazon private label brand.
        Returns: (is_blocked: bool, reason: str)
        """
        if not brand and not title:
            return False, ""

        clean_brand = self.normalize_brand(brand)

        # 1. Exact normalized brand match
        if clean_brand in self.normalized_brands:
            return True, f"Brand '{brand}' matches protected IP blocklist."

        # 2. Substring brand matching for compound variations (len > 3)
        for b in self.normalized_brands:
            if len(b) >= 4 and (b in clean_brand or clean_brand in b):
                return True, f"Brand '{brand}' contains litigious brand token '{b}'."

        # 3. Title token scanning (catches private-label accessories or disguised listings)
        if title:
            clean_title = self.normalize_brand(title)
            critical_tokens = [
                "AMAZONBASICS", "SOLIMO", "PRESTO", "HAPPYBELLY", "MAMABEAR",
                "OTTERBOX", "POPSOCKETS", "YETI", "OLAPLEX", "DEWALT", "MILWAUKEE"
            ]
            for tok in critical_tokens:
                if tok in clean_title:
                    return True, f"Title contains high-risk IP keyword '{tok}'."

        return False, ""

    def evaluate_seller_dropoff(self, seller_history: List[int], days_window: int = 14) -> Tuple[bool, str]:
        """
        Dynamic Canary: Detects sudden cliff drop-offs in competitive seller counts.
        If seller count plunges >= 60% within 14 days down to 0-2 sellers, flags as IP threat.
        """
        if not seller_history or len(seller_history) < 3:
            return False, ""

        recent_slice = seller_history[-days_window:]
        peak_sellers = max(recent_slice)
        current_sellers = recent_slice[-1]

        # Trigger threshold: Was 5+ sellers, dropped by >= 60%, and currently <= 2
        if peak_sellers >= 5 and current_sellers <= 2:
            drop_pct = (peak_sellers - current_sellers) / float(peak_sellers)
            if drop_pct >= 0.60:
                reason = (
                    f"Suspected C&D / IP Sweep: Seller count collapsed from "
                    f"{peak_sellers} to {current_sellers} (-{round(drop_pct * 100)}%) in {days_window}d."
                )
                return True, reason

        return False, ""


# Global singleton instance for high-throughput pipeline calls
_global_guard = IPGuard()

def is_ip_restricted(brand: str, title: str = "") -> bool:
    blocked, _ = _global_guard.is_ip_restricted(brand, title)
    return blocked

def is_blocked_with_reason(brand: str, title: str = "") -> Tuple[bool, str]:
    return _global_guard.is_ip_restricted(brand, title)

def check_seller_dropoff_sweep(seller_history: List[int]) -> Tuple[bool, str]:
    return _global_guard.evaluate_seller_dropoff(seller_history)
