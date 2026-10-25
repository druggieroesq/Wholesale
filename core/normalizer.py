import os
import re
import pandas as pd
from typing import Optional, Dict, List

class SmartCatalogNormalizer:
    """
    Ingests raw distributor spreadsheets (.csv or .xlsx), validates that the file
    is an authentic wholesale price list, and normalizes columns and UPCs
    to a standardized schema.
    """
    SYNONYM_MAP = {
        "upc": [
            "upc", "upc_code", "upccode", "barcode", "gtin", "ean", 
            "item_upc", "upc_number", "bar_code", "product_code"
        ],
        "brand": [
            "brand", "brand_name", "vendor", "mfr", "manufacturer", 
            "mfg", "line", "make"
        ],
        "description": [
            "description", "desc", "item_description", "title", 
            "product_name", "item_name", "product_description", "item"
        ],
        "unit_cost": [
            "unit_cost", "cost", "price", "wholesale", "wholesale_price", 
            "whsl", "unit_price", "price_ea", "our_price", "net_price"
        ],
        "case_qty": [
            "case_qty", "pack", "case_pack", "pack_size", "moq", 
            "master_pack", "inner_pack", "qty_per_case", "box_qty"
        ]
    }

    @classmethod
    def clean_column_name(cls, col: str) -> str:
        """Converts arbitrary column strings to snake_case alphanumeric."""
        return re.sub(r"[^a-z0-9_]", "", str(col).strip().lower().replace(" ", "_"))

    @classmethod
    def format_upc(cls, raw_val: any) -> Optional[str]:
        """Restores truncated leading zeros and cleans scientific notation artifacts."""
        if pd.isna(raw_val):
            return None
        
        val_str = str(raw_val).strip()
        # Handle scientific notation like 7.0066E+10
        if "e" in val_str.lower():
            try:
                val_str = f"{int(float(val_str))}"
            except ValueError:
                pass
        
        # Strip trailing decimal artifacts like 70066120015.0
        val_str = re.sub(r"\.0$", "", val_str)
        digits_only = re.sub(r"\D", "", val_str)

        # Standard barcode lengths (UPC-A: 12, EAN: 13, GTIN-14: 14)
        if len(digits_only) in [10, 11]:
            return digits_only.zfill(12)
        elif len(digits_only) in [12, 13, 14]:
            return digits_only
        elif 0 < len(digits_only) < 10:
            return digits_only.zfill(12)
        return None

    @classmethod
    def is_valid_price_catalog(cls, file_path: str) -> bool:
        """
        Structural Fingerprint Test:
        Validates row threshold and presence of the 3 anchor columns (UPC, Cost, Brand).
        Used by automated fetchers to filter out PDFs, receipts, and order forms.
        """
        try:
            if file_path.endswith((".xlsx", ".xls")):
                preview_df = pd.read_excel(file_path, nrows=20)
            else:
                try:
                    preview_df = pd.read_csv(file_path, nrows=20, low_memory=False, encoding="utf-8")
                except UnicodeDecodeError:
                    preview_df = pd.read_csv(file_path, nrows=20, low_memory=False, encoding="latin1")
        except Exception:
            return False

        if len(preview_df) < 5:
            return False

        clean_cols = [cls.clean_column_name(c) for c in preview_df.columns]
        
        has_upc = any(any(alias == col or alias in col for alias in cls.SYNONYM_MAP["upc"]) for col in clean_cols)
        has_cost = any(any(alias == col or alias in col for alias in cls.SYNONYM_MAP["unit_cost"]) for col in clean_cols)
        has_brand = any(any(alias == col or alias in col for alias in cls.SYNONYM_MAP["brand"]) for col in clean_cols)

        return has_upc and has_cost and has_brand

    @classmethod
    def normalize(cls, file_path: str) -> pd.DataFrame:
        """Loads and converts messy CSV or Excel sheets into a unified schema."""
        if file_path.endswith((".xlsx", ".xls")):
            df = pd.read_excel(file_path)
        else:
            try:
                df = pd.read_csv(file_path, low_memory=False, encoding="utf-8")
            except UnicodeDecodeError:
                df = pd.read_csv(file_path, low_memory=False, encoding="latin1")

        raw_to_clean = {c: cls.clean_column_name(c) for c in df.columns}
        df = df.rename(columns=raw_to_clean)

        resolved_headers: Dict[str, str] = {}
        # Priority mapping: map exact matches before broad substring matches
        for canonical, variants in cls.SYNONYM_MAP.items():
            matched_col = None
            for col in df.columns:
                if col in variants:
                    matched_col = col
                    break
            if not matched_col:
                for col in df.columns:
                    if any(v in col for v in variants):
                        matched_col = col
                        break
            if matched_col and matched_col not in resolved_headers.values():
                resolved_headers[matched_col] = canonical

        df = df.rename(columns=resolved_headers)

        missing = [req for req in ["upc", "brand", "unit_cost"] if req not in df.columns]
        if missing:
            raise ValueError(f"Could not automatically detect columns: {missing}. Found: {list(df.columns)}")

        # Clean and typecast columns
        df["upc"] = df["upc"].apply(cls.format_upc)
        df["unit_cost"] = pd.to_numeric(
            df["unit_cost"].astype(str).str.replace(r"[$,\s]", "", regex=True), 
            errors="coerce"
        )
        
        if "case_qty" not in df.columns:
            df["case_qty"] = 12
        else:
            df["case_qty"] = pd.to_numeric(df["case_qty"], errors="coerce").fillna(12).astype(int)

        if "description" not in df.columns:
            df["description"] = df["brand"].astype(str) + " Item"

        # Prune dead/invalid data
        df = df.dropna(subset=["upc", "brand", "unit_cost"])
        df = df[df["unit_cost"] > 0]

        return df[["upc", "brand", "description", "unit_cost", "case_qty"]]
