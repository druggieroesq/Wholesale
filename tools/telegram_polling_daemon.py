import os
import sys
import time
import json
import requests
from datetime import datetime
from tools.git_state_sync import GitStateSync

ACTIONS_FILE = "data/product_actions.json"

class TelegramPollingDaemon:
    def __init__(self):
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "8501395612").strip()
        self.api_url = f"https://api.telegram.org/bot{self.bot_token}"
        self.offset = 0
        self.syncer = GitStateSync()
        os.makedirs("data", exist_ok=True)

    def load_actions(self) -> dict:
        if os.path.exists(ACTIONS_FILE):
            try:
                with open(ACTIONS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {"purchased": {}, "declined": {}}
        return {"purchased": {}, "declined": {}}

    def save_actions(self, data: dict):
        with open(ACTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def log_decision(self, action_type: str, asin: str, cost: float = 0.0, vendor: str = "") -> dict:
        ledger = self.load_actions()
        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

        if action_type == "buy":
            ledger.setdefault("purchased", {})[asin] = {
                "decision_time": now_str,
                "vendor": vendor,
                "unit_cost": cost,
                "fulfillment_route": "LOCAL_HQ",
                "prep_fee_per_unit": 0.35,
                "ship_to": "Parkland, FL Station"
            }
        else:
            ledger.setdefault("declined", {})[asin] = {
                "decision_time": now_str,
                "reason": "Telegram Operator Decline"
            }

        self.save_actions(ledger)
        # Commit back to GitHub to ensure Railway does not wipe state
        self.syncer.sync_state(f"state: {action_type.upper()} {asin}")
        return ledger

    def answer_callback(self, callback_id: str, alert_text: str):
        url = f"{self.api_url}/answerCallbackQuery"
        requests.post(url, json={"callback_query_id": callback_id, "text": alert_text, "show_alert": False}, timeout=10)

    def edit_message(self, chat_id: int, message_id: int, updated_text: str):
        url = f"{self.api_url}/editMessageText"
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": updated_text,
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": []}  # Clear buttons after action
        }
        requests.post(url, json=payload, timeout=10)

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
                        self.answer_callback(cb_id, "Unauthorized operator.")
                        continue

                    parts = cb_data.split(":")
                    action = parts[0]
                    asin = parts[1] if len(parts) > 1 else "UNKNOWN"

                    if action == "buy":
                        cost = float(parts[2]) if len(parts) > 2 else 0.0
                        vendor = parts[3] if len(parts) > 3 else "Wholesaler"
                        self.log_decision("buy", asin, cost, vendor)
                        self.answer_callback(cb_id, f"✅ Logged BUY for {asin} -> Parkland HQ")
                        
                        orig_text = message.get("text", "")
                        self.edit_message(
                            int(self.chat_id),
                            msg_id,
                            f"{orig_text}\n\n<b>[STATUS: PURCHASED - ROUTED TO PARKLAND HQ ($0.35 PREP)]</b>"
                        )

                    elif action == "decline":
                        self.log_decision("decline", asin)
                        self.answer_callback(cb_id, f"❌ Logged DECLINE for {asin}")
                        orig_text = message.get("text", "")
                        self.edit_message(
                            int(self.chat_id),
                            msg_id,
                            f"{orig_text}\n\n<b>[STATUS: DECLINED - EXCLUDED]</b>"
                        )

        except Exception as e:
            print(f"[polling error] {e}")

    def run(self):
        print(f"[*] Starting Telegram Polling Daemon for Chat ID {self.chat_id}...")
        while True:
            self.process_updates()
            time.sleep(1)


if __name__ == "__main__":
    daemon = TelegramPollingDaemon()
    daemon.run()
