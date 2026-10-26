import os
import json
import pandas as pd
from typing import Tuple, Dict, Any

class CatalogDeltaSniper:
    """
    Compares supplier price sheet versions to detect price drops >= 15%
    and newly introduced manufacturer clearance SKUs.
    """

    CACHE_DIR = "data/catalog_cache"

    def __init__(self):
        os.makedirs(self.CACHE_DIR, exist_ok=True)

    def analyze_catalog_deltas(
        self,
        distributor_name: str,
        current_df: pd.DataFrame,
        min_drop_pct: float = 0.15
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        Compares incoming catalog against historical cached price lists.
        Returns:
            Tuple[price_drop_opportunities_df, new_sku_opportunities_df]
        """
        if current_df.empty or "upc" not in current_df.columns or "cost" not in current_df.columns:
            return pd.DataFrame(), pd.DataFrame()

        clean_dist = "".join([c if c.isalnum() else "_" for c in distributor_name.lower()])
        cache_file = os.path.join(self.CACHE_DIR, f"{clean_dist}_baseline.csv")

        current_df = current_df.copy()
        current_df["upc"] = current_df["upc"].astype(str).str.strip().str.zfill(12)
        current_df["cost"] = pd.to_numeric(current_df["cost"], errors="coerce").fillna(0.0)

        if not os.path.exists(cache_file):
            # Save first baseline and exit
            current_df.to_csv(cache_file, index=False)
            return pd.DataFrame(), pd.DataFrame()

        baseline_df = pd.read_csv(cache_file)
        baseline_df["upc"] = baseline_df["upc"].astype(str).str.strip().str.zfill(12)
        baseline_df["cost"] = pd.to_numeric(baseline_df["cost"], errors="coerce").fillna(0.0)

        # Merge current with baseline
        merged = pd.merge(
            current_df,
            baseline_df[["upc", "cost"]],
            on="upc",
            how="left",
            suffixes=("", "_previous")
        )

        # Detect new SKUs
        new_skus = merged[merged["cost_previous"].isna()].copy()

        # Detect price drops >= threshold
        price_drops = merged[
            (merged["cost_previous"] > 0) & 
            (merged["cost"] < merged["cost_previous"])
        ].copy()

        if not price_drops.empty:
            price_drops["drop_pct"] = (price_drops["cost_previous"] - price_drops["cost"]) / price_drops["cost_previous"]
            price_drops = price_drops[price_drops["drop_pct"] >= min_drop_pct]

        # Update cache with current catalog
        current_df.to_csv(cache_file, index=False)

        return price_drops, new_skus
