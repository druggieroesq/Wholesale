import os
import re
import pandas as pd
from typing import Optional, Dict

class SmartCatalogNormalizer:
    """
    Normalizes chaotic distributor CSV/Excel files into a strict schema:
    ['upc', 'description', 'brand', 'unit_cost', 'case_qty']
    Handles currency symbols, commas, corrupted barcodes, and varying header names.
    """
    HEADER_MAP = {
        "upc": [
            "upc", "upc code", "barcode", "ean", "item upc", "product upc", 
            "upc/ean", "bar code", "gtin", "upc_code"
        ],
        "description": [
            "description", "item description", "product description", "title", 
            "item name", "product name", "desc", "name", "item_desc"
        ],
        "brand": [
            "brand", "mfr", "manufacturer", "brand name", "line", "vendor"
        ],
        "unit_cost": [
            "cost", "unit cost", "price", "whsl", "wholesale", "wholesale price", 
            "price each", "our price", "unit_cost", "whsl price", "wsl", "ea cost"
        ],
        "case_qty": [
            "case qty", "case pack", "pack", "pk", "master pack", "case count", 
            "units/case", "qty/case", "moq", "case_qty", "pack size", "cs pk"
        ]
    }

    @classmethod
    def clean_currency(cls, val: any) -> float:
        if pd.isna(val):
            return 0.0
        if isinstance(val, (int, float)):
            return float(val)
        # Remove '$', commas, spaces, and non-numeric characters except '.'
        cleaned = re.sub(r"[^\d.]", "", str(val))
        try:
            return float(cleaned) if cleaned else 0.0
        except ValueError:
            return 0.0

    @classmethod
    def clean_upc(cls, val: any) -> Optional[str]:
        if pd.isna(val):
            return None
        # Convert floating point scientific notation (e.g. 1.23456E+11) or raw integers
        raw_str = str(val).strip()
        if "." in raw_str and ("e" in raw_str.lower() or raw_str.endswith(".0")):
            try:
                raw_str = f"{int(float(raw_str))}"
            except ValueError:
                pass
        
        # Strip all non-digit characters
        digits = re.sub(r"\D", "", raw_str)
        if not digits or len(digits) < 8:
            return None

        # Standard US retail barcodes are 12 digits (UPC-A) or 13 digits (EAN-13)
        if len(digits) <= 12:
            return digits.zfill(12)
        elif len(digits) == 13:
            return digits.zfill(13)
        return digits

    @classmethod
    def clean_integer(cls, val: any, default: int = 1) -> int:
        if pd.isna(val):
            return default
        try:
            digits = re.sub(r"\D", "", str(val))
            return int(digits) if digits and int(digits) > 0 else default
        except ValueError:
            return default

    @classmethod
    def normalize(cls, file_path: str) -> pd.DataFrame:
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Catalog file not found: {file_path}")

        ext = os.path.splitext(file_path)[-1].lower()
        if ext in [".xlsx", ".xls"]:
            # Try reading without headers first if rows start further down
            df_raw = pd.read_excel(file_path)
        else:
            try:
                df_raw = pd.read_csv(file_path, encoding="utf-8", on_bad_lines="skip")
            except UnicodeDecodeError:
                df_raw = pd.read_csv(file_path, encoding="latin1", on_bad_lines="skip")

        if df_raw.empty:
            raise ValueError(f"Catalog {file_path} contains no data.")

        # Match columns fuzzily
        matched_cols: Dict[str, str] = {}
        df_cols_clean = {re.sub(r"[_\s\-]+", " ", str(col).lower().strip()): col for col in df_raw.columns}

        for canonical_name, aliases in cls.HEADER_MAP.items():
            for alias in aliases:
                if alias in df_cols_clean:
                    matched_cols[canonical_name] = df_cols_clean[alias]
                    break

        if "upc" not in matched_cols or "unit_cost" not in matched_cols:
            raise KeyError(
                f"Could not reliably map mandatory headers ('upc' and 'unit_cost') in {file_path}. "
                f"Detected columns: {list(df_raw.columns)}"
            )

        # Build clean DataFrame
        normalized = pd.DataFrame()
        normalized["upc"] = df_raw[matched_cols["upc"]].apply(cls.clean_upc)
        normalized["unit_cost"] = df_raw[matched_cols["unit_cost"]].apply(cls.clean_currency)
        
        # Optional fields
        if "description" in matched_cols:
            normalized["description"] = df_raw[matched_cols["description"]].astype(str).str.strip()
        else:
            normalized["description"] = "Wholesale Item"

        if "brand" in matched_cols:
            normalized["brand"] = df_raw[matched_cols["brand"]].astype(str).str.strip().str.title()
        else:
            # Fallback: extract first word of description
            normalized["brand"] = normalized["description"].apply(lambda d: d.split()[0] if d else "Generic")

        if "case_qty" in matched_cols:
            normalized["case_qty"] = df_raw[matched_cols["case_qty"]].apply(lambda q: cls.clean_integer(q, default=12))
        else:
            normalized["case_qty"] = 12

        # Filter out invalid rows (zero cost or missing barcode)
        cleaned_df = normalized.dropna(subset=["upc"]).copy()
        cleaned_df = cleaned_df[cleaned_df["unit_cost"] > 0.0]
        cleaned_df = cleaned_df.drop_duplicates(subset=["upc"])

        print(f"[✓] Successfully parsed {len(cleaned_df)} actionable SKUs from {os.path.basename(file_path)}")
        return cleaned_df.reset_index(drop=True)
