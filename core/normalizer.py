import os
import re
import pandas as pd
from typing import Optional, Dict

class SmartCatalogNormalizer:
    """
    Normalizes chaotic distributor CSV/Excel files into a strict schema:
    ['upc', 'description', 'brand', 'unit_cost', 'case_qty', 'sku']
    Handles currency symbols, commas, corrupted barcodes, leading banner rows, and custom column names.
    """
    HEADER_MAP = {
        "upc": [
            "upc12", "upc", "upc code", "barcode", "ean", "item upc", "product upc", 
            "upc/ean", "bar code", "gtin", "upc_code", "upc10", "code"
        ],
        "description": [
            "description50", "description", "description32", "item description", 
            "product description", "title", "item name", "product name", "desc", "name", "item_desc"
        ],
        "brand": [
            "brand", "mfr", "manufacturer", "brand name", "line", "vendor", "vendorname"
        ],
        "unit_cost": [
            "dealerprice", "dealer price", "unit price", "price (contarplus+)", "cost", 
            "unit cost", "price", "whsl", "wholesale", "wholesale price", 
            "price each", "our price", "unit_cost", "whsl price", "wsl", "ea cost"
        ],
        "case_qty": [
            "caseqty", "case qty", "case pack", "pack", "pk", "master pack", "case count", 
            "units/case", "qty/case", "moq", "case_qty", "pack size", "cs pk", "ordermult"
        ],
        "sku": [
            "item", "sku", "item number", "item_number", "part", "mfgnumber", "part number"
        ]
    }

    @classmethod
    def clean_currency(cls, val: any) -> float:
        if pd.isna(val):
            return 0.0
        if isinstance(val, (int, float)):
            return float(val)
        cleaned = re.sub(r"[^\d.]", "", str(val))
        try:
            return float(cleaned) if cleaned else 0.0
        except ValueError:
            return 0.0

    @classmethod
    def clean_upc(cls, val: any) -> Optional[str]:
        if pd.isna(val):
            return None
        raw_str = str(val).strip()
        if "." in raw_str and ("e" in raw_str.lower() or raw_str.endswith(".0")):
            try:
                raw_str = f"{int(float(raw_str))}"
            except ValueError:
                pass
        
        digits = re.sub(r"\D", "", raw_str)
        if not digits or len(digits) < 8:
            return None

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
    def find_header_row(cls, file_path: str, max_search_rows: int = 20) -> int:
        """Finds row index that contains recognizable catalog headers."""
        ext = os.path.splitext(file_path)[-1].lower()
        try:
            if ext in [".xlsx", ".xls"]:
                sample_df = pd.read_excel(file_path, nrows=max_search_rows, header=None)
            else:
                sample_df = pd.read_csv(file_path, nrows=max_search_rows, header=None, on_bad_lines="skip")
        except Exception:
            return 0

        target_aliases = {alias.lower() for aliases in cls.HEADER_MAP.values() for alias in aliases}
        best_row = 0
        best_score = 0

        for r_idx in range(len(sample_df)):
            row_vals = [re.sub(r"[^a-z0-9]", "", str(v).lower()) for v in sample_df.iloc[r_idx] if pd.notna(v)]
            score = 0
            for v in row_vals:
                if any(re.sub(r"[^a-z0-9]", "", a) in v or v in re.sub(r"[^a-z0-9]", "", a) for a in target_aliases):
                    score += 1
            if score > best_score:
                best_score = score
                best_row = r_idx

        return best_row if best_score >= 2 else 0

    @classmethod
    def normalize(cls, file_path: str) -> pd.DataFrame:
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Catalog file not found: {file_path}")

        header_row = cls.find_header_row(file_path)
        ext = os.path.splitext(file_path)[-1].lower()

        if ext in [".xlsx", ".xls"]:
            df_raw = pd.read_excel(file_path, skiprows=header_row)
        else:
            try:
                df_raw = pd.read_csv(file_path, skiprows=header_row, encoding="utf-8", on_bad_lines="skip")
            except UnicodeDecodeError:
                df_raw = pd.read_csv(file_path, skiprows=header_row, encoding="latin1", on_bad_lines="skip")

        if df_raw.empty:
            raise ValueError(f"Catalog {file_path} contains no data.")

        # Clean column names by stripping spaces, symbols, and lowercasing
        col_cleaned_map = {}
        for c in df_raw.columns:
            clean_name = re.sub(r"[^a-z0-9]", "", str(c).lower().strip())
            col_cleaned_map[clean_name] = c

        matched_cols: Dict[str, str] = {}
        for canonical, aliases in cls.HEADER_MAP.items():
            for alias in aliases:
                norm_alias = re.sub(r"[^a-z0-9]", "", alias.lower())
                # Exact match first
                if norm_alias in col_cleaned_map:
                    matched_cols[canonical] = col_cleaned_map[norm_alias]
                    break
                # Substring fallback
                found = False
                for c_clean, orig_col in col_cleaned_map.items():
                    if norm_alias in c_clean or c_clean in norm_alias:
                        matched_cols[canonical] = orig_col
                        found = True
                        break
                if found:
                    break

        if "upc" not in matched_cols or "unit_cost" not in matched_cols:
            raise KeyError(
                f"Could not reliably map mandatory headers ('upc' and 'unit_cost') in {file_path}. "
                f"Detected columns: {list(df_raw.columns)}"
            )

        normalized = pd.DataFrame()
        normalized["upc"] = df_raw[matched_cols["upc"]].apply(cls.clean_upc)
        normalized["unit_cost"] = df_raw[matched_cols["unit_cost"]].apply(cls.clean_currency)
        
        if "description" in matched_cols:
            normalized["description"] = df_raw[matched_cols["description"]].astype(str).str.strip()
        else:
            normalized["description"] = "Wholesale Item"

        if "brand" in matched_cols:
            normalized["brand"] = df_raw[matched_cols["brand"]].astype(str).str.strip().str.title()
        else:
            normalized["brand"] = normalized["description"].apply(lambda d: d.split()[0] if d else "Generic")

        if "case_qty" in matched_cols:
            normalized["case_qty"] = df_raw[matched_cols["case_qty"]].apply(lambda q: cls.clean_integer(q, default=12))
        else:
            normalized["case_qty"] = 12

        if "sku" in matched_cols:
            normalized["sku"] = df_raw[matched_cols["sku"]].astype(str).str.strip()
        else:
            normalized["sku"] = normalized["upc"]

        cleaned_df = normalized.dropna(subset=["upc"]).copy()
        cleaned_df = cleaned_df[cleaned_df["unit_cost"] > 0.0]
        cleaned_df = cleaned_df.drop_duplicates(subset=["upc"])

        return cleaned_df.reset_index(drop=True)


# Standalone module aliases
def clean_upc(val: any) -> Optional[str]:
    return SmartCatalogNormalizer.clean_upc(val)

def clean_cost(val: any) -> float:
    return SmartCatalogNormalizer.clean_currency(val)

def find_best_header_row(df: pd.DataFrame, max_search_rows: int = 20) -> int:
    return 0
