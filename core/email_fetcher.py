import os
import sys
import base64
import logging
from typing import List, Dict, Any
from googleapiclient.discovery import build
from google.auth.default import default

from tools.universal_catalog_ingestor import UniversalCatalogIngestor

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("EmailFetcher")

RAW_CATALOG_DIR = "data/catalogs/raw"

class DistributorEmailFetcher:
    """
    Automated Gmail Ingestion Agent:
    1. Scans inbox for emails containing wholesale catalog keywords or vendor names.
    2. Downloads .csv and .xlsx attachments into data/catalogs/raw/.
    3. Triggers UniversalCatalogIngestor to immediately index newly received SKUs.
    """
    VENDOR_KEYWORDS = [
        "midland", "frontier", "atlantic spice", "jb tools", 
        "marlo beauty", "global industrial", "catalog", "price sheet", "wholesale"
    ]

    def __init__(self):
        os.makedirs(RAW_CATALOG_DIR, exist_ok=True)
        self.service = None
        self._init_service()

    def _init_service(self):
        try:
            credentials, _ = default()
            self.service = build("gmail", "v1", credentials=credentials)
            logger.info("Connected to Gmail API successfully.")
        except Exception as e:
            logger.error(f"Failed to authenticate with Gmail API: {e}")

    def fetch_distributor_attachments(self) -> List[str]:
        if not self.service:
            logger.warning("Gmail service unavailable. Check API credentials.")
            return []

        query = 'has:attachment (filename:csv OR filename:xlsx OR filename:xls) ("midland" OR "frontier" OR "atlantic" OR "jb tools" OR "marlo" OR "global industrial" OR "catalog" OR "wholesale")'
        logger.info(f"Querying Gmail: {query}")

        try:
            results = self.service.users().messages().list(userId="me", q=query, maxResults=20).execute()
            messages = results.get("messages", [])
            logger.info(f"Found {len(messages)} potential distributor catalog emails.")
        except Exception as e:
            logger.error(f"Error listing messages: {e}")
            return []

        saved_files = []

        for msg_ref in messages:
            msg_id = msg_ref["id"]
            try:
                msg = self.service.users().messages().get(userId="me", id=msg_id).execute()
                payload = msg.get("payload", {})
                parts = payload.get("parts", [])

                for part in parts:
                    filename = part.get("filename", "")
                    body = part.get("body", {})
                    attachment_id = body.get("attachmentId")

                    if filename and attachment_id:
                        lower_name = filename.lower()
                        if lower_name.endswith((".csv", ".xlsx", ".xls")):
                            logger.info(f"Downloading attachment: {filename}")
                            att = self.service.users().messages().attachments().get(
                                userId="me", messageId=msg_id, id=attachment_id
                            ).execute()

                            file_data = base64.urlsafe_b64decode(att["data"].encode("UTF-8"))
                            dest_path = os.path.join(RAW_CATALOG_DIR, filename)

                            with open(dest_path, "wb") as f:
                                f.write(file_data)

                            saved_files.append(dest_path)
                            logger.info(f"Saved: {dest_path}")
            except Exception as e:
                logger.error(f"Error fetching message {msg_id}: {e}")

        # If new files were retrieved, trigger ingestion immediately
        if saved_files:
            logger.info(f"Retrieved {len(saved_files)} new price sheets. Running Universal Catalog Ingestor...")
            ingestor = UniversalCatalogIngestor()
            total_indexed = ingestor.build_master_index()
            logger.info(f"Catalog update complete. Active SKUs in universe: {total_indexed}")

        return saved_files


if __name__ == "__main__":
    fetcher = DistributorEmailFetcher()
    downloads = fetcher.fetch_distributor_attachments()
    print(f"\nCompleted. Processed {len(downloads)} distributor attachments.")
