"""
tools/drive_catalog_sync.py
Automated Google Drive Catalog Synchronizer.
Monitors the 'Wholesale_Catalogs' folder in Google Drive, downloads any new or
updated spreadsheets directly into /app/, and triggers local parser pipelines.
"""

import os
import sys
import io
import json
import logging
from pathlib import Path
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google.oauth2 import service_account

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from dotenv import load_dotenv
load_dotenv(ROOT_DIR / ".env", override=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] DriveSync: %(message)s")
logger = logging.getLogger("DriveSync")

CREDENTIALS_FILE = ROOT_DIR / os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "config/google_credentials.json")
FOLDER_NAME = "Wholesale_Catalogs"


def get_drive_service():
    if not CREDENTIALS_FILE.exists():
        logger.error(f"Google credentials file not found at {CREDENTIALS_FILE}")
        return None
    creds = service_account.Credentials.from_service_account_file(
        str(CREDENTIALS_FILE),
        scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    return build("drive", "v3", credentials=creds)


def sync_catalogs_from_drive():
    service = get_drive_service()
    if not service:
        return

    # Find the Wholesale_Catalogs folder
    res = service.files().list(
        q=f"name = '{FOLDER_NAME}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
        fields="files(id, name)"
    ).execute()
    folders = res.get("files", [])
    if not folders:
        logger.error(f"Could not locate '{FOLDER_NAME}' folder in Google Drive.")
        return

    folder_id = folders[0]["id"]

    # List all files inside the folder
    query = f"'{folder_id}' in parents and trashed = false"
    items = service.files().list(query=query, fields="files(id, name, modifiedTime)").execute().get("files", [])

    if not items:
        logger.info(f"Folder '{FOLDER_NAME}' is currently empty. Drop your catalog spreadsheets there.")
        return

    for item in items:
        file_name = item["name"]
        file_id = item["id"]
        dest_path = ROOT_DIR / file_name

        logger.info(f"Syncing '{file_name}' from Google Drive...")
        request = service.files().get_media(fileId=file_id)
        fh = io.FileIO(str(dest_path), "wb")
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()

        logger.info(f"Successfully downloaded '{file_name}' to {dest_path}")

        # Auto-trigger BCI parser if it's the BCI inventory
        if "bci" in file_name.lower():
            logger.info("Triggering BCI catalog parser...")
            os.system(f"python {ROOT_DIR}/tools/bci_catalog_parser.py")


if __name__ == "__main__":
    sync_catalogs_from_drive()
