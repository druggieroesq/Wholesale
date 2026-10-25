import os
import re
import imaplib
import email
from email.header import decode_header
from typing import List

class EmailCatalogFetcher:
    def __init__(self):
        self.user = os.getenv("DISTRIBUTOR_EMAIL_USER", "")
        self.password = os.getenv("DISTRIBUTOR_EMAIL_PASS", "")
        self.imap_server = os.getenv("IMAP_SERVER", "imap.gmail.com")

    def download_catalog_attachments(self, save_dir: str = "data/incoming") -> List[str]:
        """
        Connects via IMAP, searches for unread emails with CSV/Excel attachments,
        saves them locally, and returns the list of file paths.
        """
        if not self.user or not self.password:
            print("[!] Email credentials not set in .env. Skipping email fetch.")
            return []

        os.makedirs(save_dir, exist_ok=True)
        saved_files = []

        try:
            print(f"[*] Connecting to {self.imap_server} for {self.user}...")
            mail = imaplib.IMAP4_SSL(self.imap_server)
            mail.login(self.user, self.password)
            mail.select("inbox")

            # Search for unread emails
            status, response = mail.search(None, "UNSEEN")
            if status != "OK" or not response[0]:
                print("[*] No new unread distributor emails found.")
                mail.logout()
                return []

            email_ids = response[0].split()
            print(f"[*] Found {len(email_ids)} unread email(s). Checking for catalog files...")

            for e_id in email_ids:
                status, msg_data = mail.fetch(e_id, "(RFC822)")
                for part_data in msg_data:
                    if isinstance(part_data, tuple):
                        msg = email.message_from_bytes(part_data[1])
                        
                        # Process each attachment in the email
                        for part in msg.walk():
                            if part.get_content_maintype() == "multipart":
                                continue
                            if part.get("Content-Disposition") is None:
                                continue

                            raw_filename = part.get_filename()
                            if not raw_filename:
                                continue

                            # Decode subject/filename encoding if needed
                            decoded_parts = decode_header(raw_filename)
                            filename = decoded_parts[0][0]
                            if isinstance(filename, bytes):
                                filename = filename.decode(decoded_parts[0][1] or "utf-8")

                            # Check for spreadsheet extensions
                            if filename.lower().endswith((".csv", ".xlsx", ".xls")):
                                clean_filename = re.sub(r"[^\w\-.]", "_", filename)
                                file_path = os.path.join(save_dir, clean_filename)
                                
                                with open(file_path, "wb") as f:
                                    f.write(part.get_payload(decode=True))

                                print(f"[✓] Downloaded new catalog attachment: {file_path}")
                                saved_files.append(file_path)

            mail.close()
            mail.logout()

        except Exception as e:
            print(f"[!] IMAP Email Error: {e}")

        return saved_files
