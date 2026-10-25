import os
import imaplib
import email
from email.header import decode_header
from typing import List

class DistributorEmailWatcher:
    def __init__(self, imap_server: str = "imap.gmail.com"):
        self.imap_server = imap_server
        self.email_user = os.getenv("DISTRIBUTOR_EMAIL_USER", "")
        self.email_pass = os.getenv("DISTRIBUTOR_EMAIL_PASS", "")  # App password

    def fetch_latest_catalogs(self, save_dir: str = "data/incoming") -> List[str]:
        """Polls inbox for new distributor attachments and saves them locally."""
        if not self.email_user or not self.email_pass:
            print("[*] Email credentials not configured. Skipping automated email fetch.")
            return []

        os.makedirs(save_dir, exist_ok=True)
        downloaded_files = []

        try:
            mail = imaplib.IMAP4_SSL(self.imap_server)
            mail.login(self.email_user, self.email_pass)
            mail.select("inbox")

            # Search for unread emails with attachments
            status, messages = mail.search(None, '(UNSEEN)')
            if status != "OK":
                return []

            for num in messages[0].split():
                res, data = mail.fetch(num, "(RFC822)")
                for response_part in data:
                    if isinstance(response_part, tuple):
                        msg = email.message_from_bytes(response_part[1])
                        for part in msg.walk():
                            if part.get_content_maintype() == "multipart":
                                continue
                            if part.get("Content-Disposition") is None:
                                continue

                            filename = part.get_filename()
                            if filename and filename.lower().endswith((".csv", ".xlsx", ".xls")):
                                clean_name = re.sub(r"[^\w\-.]", "_", filename)
                                target_path = os.path.join(save_dir, clean_name)
                                with open(target_path, "wb") as f:
                                    f.write(part.get_payload(decode=True))
                                print(f"[✓] Automated attachment downloaded: {target_path}")
                                downloaded_files.append(target_path)

            mail.close()
            mail.logout()
        except Exception as e:
            print(f"[!] Email fetch error: {e}")

        return downloaded_files
