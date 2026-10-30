"""
tools/test_reverse_sourcing.py
Executes a test pass of the reverse sourcing engine with mock ASIN data.
"""

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from tools.reverse_sourcing_engine import run_reverse_sourcing_batch

mock_candidates = [
    {
        "asin": "B003NWN19K",
        "title": "Loctite Heavy Duty Threadlocker 242 50ml",
        "brand": "Loctite",
        "mpn": "24231",
        "buybox_price": 42.50,
        "monthly_velocity": 45,
        "active_fba_sellers": 1
    },
    {
        "asin": "B01MY01MRO",
        "title": "Parker 3/8 Male NPT Brass Hex Nipple",
        "brand": "Parker Hannifin",
        "mpn": "216P-6-6",
        "buybox_price": 28.00,
        "monthly_velocity": 30,
        "active_fba_sellers": 2
    },
    {
        "asin": "B00HIGHCOMP",
        "title": "DeWalt 20V Battery Pack",
        "brand": "DeWalt",
        "mpn": "DCB205",
        "buybox_price": 89.00,
        "monthly_velocity": 200,
        "active_fba_sellers": 15 # Rejected: exceeds FBA competition ceiling
    }
]

if __name__ == "__main__":
    results = run_reverse_sourcing_batch(mock_candidates)
    print("\n--- Qualified Reverse Sourcing RFQ Targets ---")
    for r in results:
        print(f"\nASIN: {r['asin']} | {r['brand']} {r['mpn']}")
        print(f"  Amazon Price: ${r['buybox_price']:.2f}")
        print(f"  Max Buy Cost (100% ROI Floor): ${r['max_allowable_cost']:.2f}")
        print(f"  Suppliers: {[s['distributor'] for s in r['matched_distributors']]}")
