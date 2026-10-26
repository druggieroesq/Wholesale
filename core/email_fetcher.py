import os
import imaplib
import email
from email.header import decode_header
import json
from typing import List, Dict, Any

DISTRIBUTORS_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "distributors.json")
INCOMING_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "incoming")

class DistributorEmailWatcher:
    """
    Connects to Gmail via IMAP, inspects incoming emails from registered
    distributor domains, and saves attached price sheets to data/incoming/.
    """
    def __init__(self):
        self.user = os.getenv("DISTRIBUTOR_EMAIL_USER", "").strip()
        self.password = os.getenv("DISTRIBUTOR_EMAIL_PASS", "").strip()
        self.imap_server = os.getenv("IMAP_SERVER", "imap.gmail.com").strip()
        self.distributors = self._load_distributors()
        os.makedirs(INCOMING_DIR, exist_ok=True)

    def _load_distributors(self) -> List[Dict[str, Any]]:
        if not os.path.exists(DISTRIBUTORS_FILE):
            return []
        try:
            with open(DISTRIBUTORS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("distributors", [])
        except Exception as e:
            print(f"[!] Error reading distributors.json: {e}")
            return []

    @classmethod
    def register_distributor(cls, name: str, email_addr: str, moq: float):
        os.makedirs(os.path.dirname(DISTRIBUTORS_FILE), exist_ok=True)
        distributors = []
        if os.path.exists(DISTRIBUTORS_FILE):
            try:
                with open(DISTRIBUTORS_FILE, "r", encoding="utf-8") as f:
                    distributors = json.load(f).get("distributors", [])
            except Exception:
                distributors = []

        domain = "@" + email_addr.split("@")[-1].lower() if "@" in email_addr else email_addr.lower()
        distributors = [d for d in distributors if d.get("name", "").lower() != name.lower()]
        
        distributors.append({
            "name": name,
            "authorized_emails": [email_addr.lower()],
            "domain_match": domain,
            "moq": float(moq)
        })

        with open(DISTRIBUTORS_FILE, "w", encoding="utf-8") as f:
            json.dump({"distributors": distributors}, f, indent=2)

        print(f"[✓] Registered distributor: {name} (MOQ: ${moq:,.2f}, Domain: {domain})")

    def _find_matching_distributor(self, sender: str) -> Dict[str, Any]:
        sender_lower = sender.lower()
        for d in self.distributors:
            for auth in d.get("authorized_emails", []):
                if auth.lower() in sender_lower:
                    return d
            domain = d.get("domain_match", "").lower()
            if domain and domain in sender_lower:
                return d
        return {"name": "Unknown_Distributor", "moq": 3000.00}

    def download_catalog_attachments(self) -> List[Dict[str, Any]]:
        if not self.user or not self.password:
            print("[*] Email credentials not configured. Skipping catalog polling.")
            return []

        jobs = []
        try:
            mail = imaplib.IMAP4_SSL(self.imap_server)
            mail.login(self.user, self.password)
            mail.select("INBOX")

            status, response = mail.search(None, '(UNSEEN)')
            if status != "OK" or not response[0]:
                mail.logout()
                return []

            email_ids = response[0].split()
            print(f"[*] Found {len(email_ids)} unread email(s). Checking for catalogs...")

            for e_id in email_ids:
                res, data = mail.fetch(e_id, "(RFC822)")
                if res != "OK":
                    continue

                msg = email.message_from_bytes(data[0][1])
                sender = msg.get("From", "")
                distributor = self._find_matching_distributor(sender)

                for part in msg.walk():
                    if part.get_content_maintype() == "multipart":
                        continue
                    if part.get("Content-Disposition") is None:
                        continue

                    filename = part.get_filename()
                    if filename:
                        dh = decode_header(filename)
                        filename = dh[0][0]
                        if isinstance(filename, bytes):
                            filename = filename.decode(dh[0][1] or "utf-8", errors="ignore")

                        ext = os.path.splitext(filename)[-1].lower()
                        if ext in [".csv", ".xlsx", ".xls"]:
                            clean_fname = f"{distributor['name'].replace(' ', '_')}_{filename}"
                            save_path = os.path.join(INCOMING_DIR, clean_fname)

                            with open(save_path, "wb") as f:
                                f.write(part.get_payload(decode=True))

                            print(f"[✓] Downloaded catalog: {clean_fname} from {distributor['name']}")
                            jobs.append({
                                "file_path": save_path,
                                "distributor": distributor
                            })

            mail.logout()
        except Exception as e:
            print(f"[!] IMAP polling error: {e}")

        return jobs

# Alias to support both import naming conventions
EmailCatalogFetcher = DistributorEmailWatcher
