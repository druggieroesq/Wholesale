import os
import json
import re
import imaplib
import email
from email.header import decode_header
from typing import List, Dict, Any, Optional
from core.normalizer import SmartCatalogNormalizer

DISTRIBUTORS_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "distributors.json")

class EmailCatalogFetcher:
    def __init__(self):
        self.user = os.getenv("DISTRIBUTOR_EMAIL_USER", "")
        self.password = os.getenv("DISTRIBUTOR_EMAIL_PASS", "")
        self.imap_server = os.getenv("IMAP_SERVER", "imap.gmail.com")
        self.distributors = self._load_distributor_registry()

    def _load_distributor_registry(self) -> List[Dict[str, Any]]:
        """Loads authorized distributor emails and domains."""
        if not os.path.exists(DISTRIBUTORS_FILE):
            os.makedirs(os.path.dirname(DISTRIBUTORS_FILE), exist_ok=True)
            with open(DISTRIBUTORS_FILE, "w", encoding="utf-8") as f:
                json.dump({"distributors": []}, f, indent=2)
            return []
        try:
            with open(DISTRIBUTORS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("distributors", [])
        except Exception:
            return []

    def get_authorized_distributor(self, sender_header: str) -> Optional[Dict[str, Any]]:
        """Matches an incoming email address against whitelist domains or addresses."""
        sender_clean = sender_header.lower()
        for dist in self.distributors:
            for auth_email in dist.get("authorized_emails", []):
                if auth_email.lower() in sender_clean:
                    return dist
            domain = dist.get("domain_match", "").lower()
            if domain and domain in sender_clean:
                return dist
        return None

    def download_catalog_attachments(self, save_dir: str = "data/incoming") -> List[Dict[str, Any]]:
        if not self.user or not self.password:
            print("[*] Email credentials not set. Skipping automated email poll.")
            return []

        os.makedirs(save_dir, exist_ok=True)
        downloaded_jobs = []

        try:
            mail = imaplib.IMAP4_SSL(self.imap_server)
            mail.login(self.user, self.password)
            mail.select("inbox")

            status, response = mail.search(None, "UNSEEN")
            if status != "OK" or not response[0]:
                mail.logout()
                return []

            email_ids = response[0].split()

            for e_id in email_ids:
                status, msg_data = mail.fetch(e_id, "(RFC822)")
                for part_data in msg_data:
                    if isinstance(part_data, tuple):
                        msg = email.message_from_bytes(part_data[1])
                        sender = msg.get("From", "")

                        # Check whitelist
                        distributor_info = self.get_authorized_distributor(sender)
                        if not distributor_info:
                            print(f"[!] Email from '{sender}' ignored (not on authorized distributor whitelist).")
                            continue

                        print(f"[✓] Recognized distributor email: {distributor_info['name']} ({sender})")

                        for part in msg.walk():
                            if part.get_content_maintype() == "multipart" or part.get("Content-Disposition") is None:
                                continue

                            raw_filename = part.get_filename()
                            if not raw_filename:
                                continue

                            decoded = decode_header(raw_filename)
                            filename = decoded[0][0]
                            if isinstance(filename, bytes):
                                filename = filename.decode(decoded[0][1] or "utf-8")

                            if filename.lower().endswith((".csv", ".xlsx", ".xls")):
                                clean_name = f"{distributor_info['name'].replace(' ', '_')}_{re.sub(r'[^\\w\\-.]', '_', filename)}"
                                file_path = os.path.join(save_dir, clean_name)

                                with open(file_path, "wb") as f:
                                    f.write(part.get_payload(decode=True))

                                if SmartCatalogNormalizer.is_valid_price_catalog(file_path):
                                    print(f"[✓] Valid catalog stored: {file_path}")
                                    downloaded_jobs.append({
                                        "file_path": file_path,
                                        "distributor": distributor_info
                                    })
                                else:
                                    os.remove(file_path)

            mail.close()
            mail.logout()
        except Exception as e:
            print(f"[!] Email Fetcher Error: {e}")

        return downloaded_jobs
