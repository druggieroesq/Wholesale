import os
import pandas as pd
from typing import Dict, Any

class CatalogDiffEngine:
    """
    Compares two versions of a distributor catalog to spot price decreases,
    new SKU additions, and discontinued items.
    """
    @staticmethod
    def compare_catalogs(old_df: pd.DataFrame, new_df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        # Merge on UPC
        merged = pd.merge(
            old_df, 
            new_df, 
            on="upc", 
            suffixes=("_old", "_new"), 
            how="outer", 
            indicator=True
        )

        # 1. New SKUs added to the catalog
        new_skus = merged[merged["_merge"] == "right_only"].copy()

        # 2. Dropped / Discontinued SKUs
        dropped_skus = merged[merged["_merge"] == "left_only"].copy()

        # 3. Existing items with price changes
        common = merged[merged["_merge"] == "both"].copy()
        common["price_diff"] = common["unit_cost_new"] - common["unit_cost_old"]
        common["pct_change"] = ((common["price_diff"]) / common["unit_cost_old"]) * 100.0

        # Filter for price cuts (discounts)
        price_drops = common[common["price_diff"] < 0].sort_values(by="pct_change", ascending=True)

        return {
            "new_skus": new_skus,
            "dropped_skus": dropped_skus,
            "price_drops": price_drops
        }
