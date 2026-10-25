import os
import re
import pandas as pd
from typing import Dict, Optional

class CatalogNormalizer:
    SYNONYMS = {
        "upc": ["upc", "upc_code", "barcode", "gtin", "item_upc", "ean"],
        "brand": ["brand", "vendor", "manufacturer", "brand_name", "mfg"],
        "description": ["description", "desc", "item_name", "title", "product_name"],
        "unit_cost": ["unit_cost", "cost", "price", "whsl", "wholesale", "wholesale_price", "price_ea"],
        "case_qty": ["case_qty", "pack", "pack_size", "case_pack", "moq", "qty_per_case"]
    }

    @classmethod
    def load_and_normalize(cls, file_path: str, custom_map: Optional[Dict[str, str]] = None) -> pd.DataFrame:
        if file_path.endswith((".xlsx", ".xls")):
            df = pd.read_excel(file_path)
        else:
            df = pd.read_csv(file_path, low_memory=False)

        # Standardize column strings
        clean_cols = {c: re.sub(r'[^a-z0-9_]', '', str(c).strip().lower().replace(" ", "_")) for c in df.columns}
        df = df.rename(columns=clean_cols)

        # Resolve column mappings
        resolved_map = {}
        for target, aliases in cls.SYNONYMS.items():
            if custom_map and target in custom_map:
                resolved_map[custom_map[target]] = target
                continue
            for col in df.columns:
                if col in aliases:
                    resolved_map[col] = target
                    break

        df = df.rename(columns=resolved_map)
        missing = [req for req in ["upc", "brand", "unit_cost"] if req not in df.columns]
        if missing:
            raise ValueError(f"Catalog is missing mandatory columns: {missing}. Available: {list(df.columns)}")

        # Format UPCs: clean non-digits, left-pad to 12 digits
        df["upc"] = df["upc"].astype(str).str.replace(r"\D", "", regex=True).str.zfill(12)
        df["unit_cost"] = pd.to_numeric(df["unit_cost"].astype(str).str.replace(r"[$,]", "", regex=True), errors="coerce")
        df["case_qty"] = pd.to_numeric(df.get("case_qty", 12), errors="coerce").fillna(12).astype(int)
        df["description"] = df.get("description", df["brand"] + " Item").astype(str).str.strip()
        df["brand"] = df["brand"].astype(str).str.strip()

        return df.dropna(subset=["upc", "brand", "unit_cost"])
