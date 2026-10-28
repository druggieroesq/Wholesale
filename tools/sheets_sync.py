import os
import sys
import json
import logging
import requests
import pandas as pd
from typing import List, Dict, Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("SheetsSync")

LOCAL_DISTRIBUTORS_FILE = "data/distributors.json"

class GoogleSheetsDistributorSync:
    """
    Synchronizes distributor parameters from Google Drive Sheets
    into the bot fleet's local execution registry (data/distributors.json).
    """
    def __init__(self, spreadsheet_url: str = None):
        # Default to your live Distributor List spreadsheet
        self.spreadsheet_url = spreadsheet_url or os.getenv(
            "DISTRIBUTOR_SHEET_URL",
            "https://docs.google.com/spreadsheets/d/1mFDA849b_PGKbkTJ_EZgLa869FV90yiSerr0JVI84qo/export?format=csv&gid=0"
        )

    def fetch_sheet_csv(self) -> pd.DataFrame:
        logger.info(f"Fetching distributor records from Google Sheets...")
        try:
            # Direct CSV export from public/shared sheet link
            csv_url = self.spreadsheet_url
            if "/edit" in csv_url:
                csv_url = csv_url.split("/edit")[0] + "/export?format=csv&gid=0"
            df = pd.read_csv(csv_url)
            logger.info(f"Retrieved {len(df)} distributor records from Google Sheets.")
            return df
        except Exception as e:
            logger.error(f"Failed to fetch CSV from Google Sheet: {e}")
            return pd.DataFrame()

    def update_distributors_json(self) -> bool:
        df = self.fetch_sheet_csv()
        if df.empty:
            logger.warning("Empty dataframe received; aborting registry overwrite.")
            return False

        distributors = []
        for _, row in df.iterrows():
            name = str(row.get("Name", "")).strip()
            if not name or name.lower() == "nan":
                continue

            dist_id = name.lower().replace(" ", "_").replace("-", "_")
            moq_raw = str(row.get("MOQ", "0")).replace("$", "").strip()
            ff_raw = str(row.get("Free freight Threshold ($ or units)", "0")).replace("$", "").strip()

            try:
                moq_val = float(moq_raw)
            except ValueError:
                moq_val = 0.0

            try:
                ff_val = float(ff_raw)
            except ValueError:
                ff_val = 0.0

            distributors.append({
                "id": dist_id,
                "name": name,
                "website": str(row.get("Website", "")),
                "phone": str(row.get("Phone Number", "")),
                "order_method": str(row.get("Order Method", "")),
                "moq_dollars": moq_val,
                "free_freight_threshold": ff_val,
                "shipping_estimate": str(row.get("Estimated Shipping price", "")),
                "payment_terms": str(row.get("Payment terms (CC or CC Fee)", "")),
                "catalog_method": str(row.get("Catalog Method", "")),
                "cadence": str(row.get("Cadence (changes to catalog)", "")),
                "brand_gating": str(row.get("Brand Gating", "")),
                "inbound_lead_days": str(row.get("Inbound lead time", "")),
                "package_config": str(row.get("Package Config", ""))
            })

        os.makedirs("data", exist_ok=True)
        with open(LOCAL_DISTRIBUTORS_FILE, "w", encoding="utf-8") as f:
            json.dump({"distributors": distributors}, f, indent=2)

        logger.info(f"Successfully synced {len(distributors)} distributors into {LOCAL_DISTRIBUTORS_FILE}.")
        return True


if __name__ == "__main__":
    syncer = GoogleSheetsDistributorSync()
    syncer.update_distributors_json()
