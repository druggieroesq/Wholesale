import os
import json
import logging
import requests
from typing import Dict, Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("TelegramDaemon")

ACTIONS_LOG_PATH = "data/telegram_actions.json"

class TelegramPollingDaemon:
    def __init__(self):
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        # Strip all whitespace/quotes to prevent auth mismatches
        raw_chat_id = os.getenv("TELEGRAM_CHAT_ID", "8501395612").strip().strip('"').strip("'")
        self.chat_id = str(raw_chat_id)
        self.api_url = f"https://api.telegram.org/bot{self.bot_token}"
        self.offset = 0
        os.makedirs(os.path.dirname(ACTIONS_LOG_PATH), exist_ok=True)

    def load_actions(self) -> dict:
        if os.path.exists(ACTIONS_LOG_PATH):
            try:
                with open(ACTIONS_LOG_PATH, "r") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Error loading actions log: {e}")
        return {"buy": [], "decline": []}

    def save_actions(self, data: dict):
        try:
            with open(ACTIONS_LOG_PATH, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Error writing actions log: {e}")

    def log_decision(self, action_type: str, asin: str, cost: float = 0.0, vendor: str = "") -> dict:
        actions = self.load_actions()
        record = {
            "asin": asin,
            "cost": cost,
            "vendor": vendor,
            "timestamp": requests.utils.default_user_agent()
        }
        if action_type not in actions:
            actions[action_type] = []
        actions[action_type].append(record)
        self.save_actions(actions)
        return record

    def answer_callback(self, callback_id: str, alert_text: str, show_alert: bool = True):
        """Sends an immediate modal popup to the Telegram screen."""
        url = f"{self.api_url}/answerCallbackQuery"
        payload = {
            "callback_query_id": callback_id,
            "text": alert_text,
            "show_alert": show_alert  # True forces an unmistakable dialog box on screen
        }
        try:
            requests.post(url, json=payload, timeout=10)
        except Exception as e:
            logger.error(f"Failed to answer callback: {e}")

    def edit_message(self, chat_id: str, message_id: int, updated_text: str):
        """Edits message text and removes inline buttons to show resolved status."""
        url = f"{self.api_url}/editMessageText"
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": updated_text,
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": []}  # Clears buttons so it cannot be double-clicked
        }
        try:
            requests.post(url, json=payload, timeout=10)
        except Exception as e:
            logger.error(f"Failed to edit message: {e}")

    def process_updates(self):
        url = f"{self.api_url}/getUpdates"
        params = {"offset": self.offset, "timeout": 20}

        try:
            res = requests.get(url, params=params, timeout=25)
            data = res.json()
            if not data.get("ok"):
                return

            for update in data.get("result", []):
                self.offset = update["update_id"] + 1

                if "callback_query" in update:
                    cb = update["callback_query"]
                    cb_id = cb["id"]
                    cb_data = cb.get("data", "")
                    message = cb.get("message", {})
                    msg_id = message.get("message_id")
                    from_id = str(cb.get("from", {}).get("id"))

                    # Authenticate operator
                    if from_id != self.chat_id:
                        logger.warning(f"Unauthorized click from {from_id} (expected {self.chat_id})")
                        self.answer_callback(cb_id, "⚠️ Unauthorized operator.", show_alert=True)
                        continue

                    parts = cb_data.split(":")
                    action = parts[0]
                    asin = parts[1] if len(parts) > 1 else "UNKNOWN"
                    orig_text = message.get("text", "")

                    if action == "buy":
                        cost = float(parts[2]) if len(parts) > 2 else 0.0
                        vendor = parts[3] if len(parts) > 3 else "Wholesaler"
                        self.log_decision("buy", asin, cost, vendor)

                        # 1. Trigger prominent modal pop-up on mobile/desktop
                        self.answer_callback(
                            cb_id,
                            f"✅ ORDER APPROVED!\nASIN: {asin}\nCost: ${cost:.2f}\nRouted to Parkland HQ prep dock.",
                            show_alert=True
                        )

                        # 2. Update card text and remove clickable buttons
                        self.edit_message(
                            self.chat_id,
                            msg_id,
                            f"{orig_text}\n\n<b>━━━━━━━━━━━━━━━━━━━━</b>\n<b>[STATUS: APPROVED & PURCHASED]</b>\n<i>Dock Location: Parkland, FL Station ($0.35 Prep)</i>"
                        )

                    elif action == "decline":
                        self.log_decision("decline", asin)

                        # 1. Trigger popup
                        self.answer_callback(
                            cb_id,
                            f"❌ DEAL DECLINED\nASIN: {asin} excluded from purchasing queue.",
                            show_alert=False
                        )

                        # 2. Update card text and clear buttons
                        self.edit_message(
                            self.chat_id,
                            msg_id,
                            f"{orig_text}\n\n<b>━━━━━━━━━━━━━━━━━━━━</b>\n<b>[STATUS: DECLINED & EXCLUDED]</b>"
                        )

        except Exception as e:
            logger.error(f"Error in polling daemon: {e}")

    def run(self):
        logger.info(f"Telegram Polling Daemon active for operator {self.chat_id}")
        while True:
            self.process_updates()


if __name__ == "__main__":
    daemon = TelegramPollingDaemon()
    daemon.run()
