import os
import requests
import smtplib
from email.message import EmailMessage
from typing import Optional

class AlertDispatcher:
    def __init__(self):
        # Telegram Secrets
        self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

        # Email Secrets
        self.email_user = os.getenv("DISTRIBUTOR_EMAIL_USER", "").strip()
        self.email_pass = os.getenv("DISTRIBUTOR_EMAIL_PASS", "").strip()
        self.alert_recipient = os.getenv("ALERT_EMAIL_RECIPIENT", self.email_user).strip()

    def send_telegram_alert(self, message: str, file_path: Optional[str] = None):
        """Dispatches real-time alert text and attaches the PO CSV to Telegram."""
        if not self.telegram_bot_token or not self.telegram_chat_id:
            return

        base_url = f"https://api.telegram.org/bot{self.telegram_bot_token}"

        # 1. Send summary message
        try:
            requests.post(
                f"{base_url}/sendMessage",
                json={
                    "chat_id": self.telegram_chat_id,
                    "text": message,
                    "parse_mode": "Markdown"
                },
                timeout=10
            )
        except Exception as e:
            print(f"[!] Telegram message error: {e}")

        # 2. Upload the PO CSV file as a document
        if file_path and os.path.exists(file_path):
            try:
                with open(file_path, "rb") as doc:
                    requests.post(
                        f"{base_url}/sendDocument",
                        data={"chat_id": self.telegram_chat_id},
                        files={"document": doc},
                        timeout=25
                    )
                print(f"[✓] PO document pushed to Telegram.")
            except Exception as e:
                print(f"[!] Telegram document send error: {e}")

    def send_email_alert(self, subject: str, body: str, file_path: Optional[str] = None):
        """Sends an email with the PO CSV attached."""
        if not self.email_user or not self.email_pass or not self.alert_recipient:
            return

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.email_user
        msg["To"] = self.alert_recipient
        msg.set_content(body)

        if file_path and os.path.exists(file_path):
            with open(file_path, "rb") as f:
                file_data = f.read()
                file_name = os.path.basename(file_path)
            msg.add_attachment(file_data, maintype="text", subtype="csv", filename=file_name)

        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(self.email_user, self.email_pass)
                server.send_message(msg)
            print(f"[✓] PO email alert delivered to {self.alert_recipient}.")
        except Exception as e:
            print(f"[!] Email alert error: {e}")
