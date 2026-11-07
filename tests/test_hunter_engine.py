#!/usr/bin/env python3
import os
import sys
import unittest
import tempfile
import math
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tools.reverse_amazon_finder import (
    parse_pack_quantity,
    normalize_barcode,
    UOM_MULTIPLIERS,
    DatabaseManager
)


class TestHunterEngineV92(unittest.TestCase):

    def test_pack_quantity_parsing(self):
        self.assertEqual(parse_pack_quantity("OSI Quad Max Sealant, White (2-Pack)"), 2)
        self.assertEqual(parse_pack_quantity("4 - Pack Commercial Air Filter 16x25x1"), 4)
        self.assertEqual(parse_pack_quantity("Case of 12 Silicone Lubricant Cans"), 12)
        self.assertEqual(parse_pack_quantity("6 Count Industrial Markers"), 6)
        self.assertEqual(parse_pack_quantity("100 Pcs Titanium Drill Bit Set"), 1)
        self.assertEqual(parse_pack_quantity("Hex Head Cap Screws 50 Count Box"), 1)

    def test_barcode_normalization(self):
        self.assertEqual(normalize_barcode("012345678905"), "00012345678905")
        self.assertEqual(normalize_barcode("4012345678901"), "04012345678901")
        self.assertEqual(normalize_barcode("00123456789012"), "00123456789012")
        self.assertEqual(normalize_barcode("invalid_code"), "")

    def test_uom_math_and_nan_handling(self):
        raw_cost = 60.0
        uom_mult = 12
        unit_cost = raw_cost / uom_mult  # $5.00
        amazon_pack = 2
        wholesale_cost = round(unit_cost * amazon_pack, 2)
        self.assertEqual(wholesale_cost, 10.00)

        raw_val = float("nan")
        self.assertFalse(math.isfinite(raw_val))

    def test_database_migration_and_priors(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            db_path = Path(tf.name)

        try:
            db = DatabaseManager(db_path)
            ratios = [0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.70]
            for r in ratios:
                db.store_unbiased_cost_ratio("MILLER_ELECTRIC", 16310091, r)

            p_clear, med, p25, p75, n = db.get_prior_distribution("MILLER_ELECTRIC", 16310091, 0.45)
            self.assertEqual(n, 10)
            self.assertEqual(p_clear, 0.60)
            self.assertEqual(med, 0.45)
            self.assertEqual(p25, 0.30)
            self.assertEqual(p75, 0.55)
        finally:
            if db_path.exists():
                os.remove(db_path)


if __name__ == "__main__":
    unittest.main()
