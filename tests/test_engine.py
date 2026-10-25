import unittest
from core.fee_calculator import FBACalculator
from core.auditor import CatalogAuditor

class TestWholesaleEngine(unittest.TestCase):
    def test_beauty_fee_threshold(self):
        # Under $10 should use 8% referral fee
        econ_sub_10 = FBACalculator.calculate_net_economics(buy_box=9.99, unit_cost=3.00, is_beauty=True)
        self.assertEqual(econ_sub_10["referral_fee"], 0.80)

        # Over $10 should use 15% referral fee
        econ_over_10 = FBACalculator.calculate_net_economics(buy_box=20.00, unit_cost=5.00, is_beauty=True)
        self.assertEqual(econ_over_10["referral_fee"], 3.00)

    def test_auditor_rejects_amazon_1p(self):
        # When Amazon is in stock > 15% of the time, auditor must reject
        bad_keepa_prod = {
            "stats": {"outOfStockPercentageAmazon90": 60, "salesRankDrops30": 30, "current": [0]*12 + [5] + [0]*5 + [2000]}
        }
        result = CatalogAuditor.audit_sku_safety(bad_keepa_prod, wholesale_cost=5.0)
        self.assertIsNone(result)

    def test_auditor_accepts_clean_product(self):
        # 100% Amazon OOS, 4 sellers, active rank drops
        good_keepa_prod = {
            "asin": "B00TEST123",
            "stats": {"outOfStockPercentageAmazon90": 100, "salesRankDrops30": 25, "current": [0]*12 + [4] + [0]*5 + [2500]}
        }
        result = CatalogAuditor.audit_sku_safety(good_keepa_prod, wholesale_cost=5.0)
        self.assertIsNotNone(result)
        self.assertEqual(result["asin"], "B00TEST123")

if __name__ == "__main__":
    unittest.main()
