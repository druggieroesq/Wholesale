#!/usr/bin/env python3
"""
Distributor Email Catalog Fetcher
---------------------------------
Automated Gmail Ingestion Agent:
1. Scans inbox for emails containing wholesale catalog keywords or vendor names.
2. Downloads .csv and .xlsx attachments directly into data/catalogs/incoming/.
3. Staged files are automatically ingested and scanned by universal_catalog_scanner.py.
"""

import os
import sys
import base64
import logging
from pathlib import Path
from typing import List
from googleapiclient.discovery import build
from google.auth.default import default
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env", override=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("EmailFetcher")

INCOMING_CATALOG_DIR = ROOT_DIR / "data" / "catalogs" / "incoming"


class DistributorEmailFetcher:
    VENDOR_KEYWORDS = [
        "midland", "frontier", "contour", "bci", "atlantic spice",
        "jb tools", "marlo beauty", "global industrial", "catalog",
        "price sheet", "wholesale"
    ]

    def __init__(self):
        INCOMING_CATALOG_DIR.mkdir(parents=True, exist_ok=True)
        self.service = None
        self._init_service()

    def _init_service(self):
        try:
            credentials, _ = default()
            self.service = build("gmail", "v1", credentials=credentials)
            logger.info("Connected to Gmail API successfully.")
        except Exception as e:
            logger.warning(f"Gmail API default credentials unavailable: {e}")

    def fetch_distributor_attachments(self) -> List[str]:
        if not self.service:
            logger.warning("Gmail service unavailable. Ensure GOOGLE_APPLICATION_CREDENTIALS is set.")
            return []

        query = (
            'has:attachment (filename:csv OR filename:xlsx OR filename:xls) '
            '("midland" OR "frontier" OR "contour" OR "bci" OR "catalog" OR "price sheet" OR "wholesale")'
        )
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
                            dest_path = INCOMING_CATALOG_DIR / filename
                            if dest_path.exists() or dest_path.with_name(f"{filename}.processed").exists():
                                logger.info(f"File {filename} already staged/processed. Skipping.")
                                continue

                            logger.info(f"Downloading catalog attachment: {filename}")
                            att = self.service.users().messages().attachments().get(
                                userId="me", messageId=msg_id, id=attachment_id
                            ).execute()

                            file_data = base64.urlsafe_b64decode(att["data"].encode("UTF-8"))
                            with open(dest_path, "wb") as f:
                                f.write(file_data)

                            saved_files.append(str(dest_path))
                            logger.info(f"Staged in incoming queue: {dest_path.name}")
            except Exception as e:
                logger.error(f"Error fetching message {msg_id}: {e}")

        if saved_files:
            logger.info(f"Staged {len(saved_files)} new price sheets in data/catalogs/incoming/. Ready for daemon scanner.")

        return saved_files


if __name__ == "__main__":
    fetcher = DistributorEmailFetcher()
    downloads = fetcher.fetch_distributor_attachments()
    print(f"\nCompleted. Staged {len(downloads)} catalog attachments into incoming queue.")
