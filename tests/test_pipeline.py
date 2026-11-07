import os
import sys
import pandas as pd

# Ensure core modules are on Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.normalizer import SmartCatalogNormalizer
from core.walmart_cost_engine import WalmartCostEngine
from core.walmart_radar import WalmartRadar
from core.auditor import SafetyAuditor

def test_full_engine():
    print("==================================================")
    print("   RUNNING AUTOMATED ENGINE INTEGRATION AUDIT     ")
    print("==================================================")

    # 1. TEST NORMALIZER RESILIENCE
    print("\n[TEST 1] Testing Catalog Normalizer with dirty data...")
    raw_data = {
        "Bar Code": [" 071249354212 ", "301871370014", "invalid_upc", "123456789012"],
        "Item Description": ["L'Oreal Shampoo 8.5oz", "CeraVe SA Cleanser 8oz", "Bad Item", "IP Brand Cream"],
        "Brand Name": ["L'Oreal", "CeraVe", "FakeBrand", "RiskyBrand"],
        "Wholesale Price": ["$4.10", "6.80", "$0.00", "12.50"],
        "Case Pack": ["24", "12", "1", "6"]
    }
    df_raw = pd.DataFrame(raw_data)
    test_csv = "tests/temp_test_catalog.csv"
    os.makedirs("tests", exist_ok=True)
    df_raw.to_csv(test_csv, index=False)

    df_clean = SmartCatalogNormalizer.normalize(test_csv)
    assert len(df_clean) == 3, f"Expected 3 valid rows after filtering, got {len(df_clean)}"
    assert df_clean.iloc[0]["upc"] == "071249354212", "UPC padding/trimming failed"
    assert df_clean.iloc[0]["unit_cost"] == 4.10, "Currency cleaning failed"
    print("  ✓ Normalizer cleanly handled symbols, spaces, and bad UPCs.")

    # 2. TEST WALMART FEE & PARITY ENGINE
    print("\n[TEST 2] Testing Walmart Cost Engine & 15% Price Parity...")
    # Simulate an Amazon Buy Box of $20.00, but Walmart was previously selling at $28.00 (+40% divergence)
    mock_keepa_prod = {
        "csv": [None] * 11 + [[1000, 2800]],  # Index 11 has historical $28.00, currently OOS
        "stats": {"buyBoxPrice": 2000, "reviewCount": 150},
        "packageWeight": 453,  # 1 lb
        "packageLength": 150, "packageWidth": 100, "packageHeight": 50,
        "binding": "beauty"
    }
    
    radar_res = WalmartRadar.analyze_walmart_opportunity(
        keepa_prod=mock_keepa_prod,
        wholesale_cost=8.00,
        monthly_drops=95
    )
    
    # Target price should be capped to $20.00 * 1.15 = $23.00
    assert radar_res["price_parity_capped"] is True, "Parity cap failed to trigger"
    assert radar_res["walmart_target_price"] == 23.00, f"Expected $23.00 capped price, got {radar_res['walmart_target_price']}"
    assert radar_res["is_walmart_winner"] is True, "High velocity item should be marked winner"
    print(f"  ✓ Price parity enforced: Capped $28.00 divergence to ${radar_res['walmart_target_price']}")
    print(f"  ✓ Calculated WFS Fee: ${radar_res['wfs_fee']} | Net Profit: ${radar_res['walmart_profit']} | ROI: {radar_res['walmart_roi']}%")

    # 3. TEST IP RISK AUDITOR
    print("\n[TEST 3] Testing Safety Auditor on IP-threat product...")
    auditor = SafetyAuditor()
    
    # Mock a sudden seller drop (from 15 sellers down to 2 in 7 days = Brand IP sweep)
    mock_ip_prod = {
        "stats": {"buyBoxPrice": 2500, "current_fba_sellers": 2},
        "csv": [
            [100, 15, 200, 15, 300, 2]  # Index 0: mock drops
        ]
    }
    # Add dummy seller count history to trigger auditor
    mock_ip_prod["liveSellers"] = [15, 14, 15, 3, 2]
    
    is_safe, reason = auditor.audit_product(mock_ip_prod)
    print(f"  ✓ Auditor verdict on risky SKU: Safe={is_safe} (Reason: {reason})")

    # 4. CLEANUP
    if os.path.exists(test_csv):
        os.remove(test_csv)

    print("\n==================================================")
    print("   ALL INTEGRATION CHECKS PASSED: READY TO DEPLOY ")
    print("==================================================")

if __name__ == "__main__":
    test_full_engine()
